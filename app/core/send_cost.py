"""Whether the next message into a chat re-reads a large context cold (#1831).

A provider bills a prompt it still holds in its prompt cache at a fraction of
the input price; once the cache has expired the whole conversation is read
again at full price (Claude Code also writes it back to the cache). Measured
on this app's own usage records (2026-10-04):

* Claude Code (the delegated leg) writes 1-hour cache entries
  (``ephemeral_1h_input_tokens``). 98% of the prompt came from cache for gaps
  up to an hour and 7% past it: the first call after 81 minutes idle re-wrote
  448k tokens, and after 9.5 hours 535k, where the first call after an
  automatic compaction wrote 78k.
* The native legs follow fluid's own cold-cache gap of five minutes
  (``fluid.context.cache_signal.COLD_GAP_SECONDS``). On the ChatGPT
  subscription 43% of large prompts came from cache within a minute, 25% at
  one to five minutes and 7% past an hour, so a cold send costs there too.

:func:`cache_state` answers for one chat: its leg, context size, the time of
its last provider call, the cache lifetime for that leg and whether a send now
would be a large cold read (``warn``).
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Dict, Optional

#: A context at least this large is worth a warning when its cache is cold.
LARGE_CONTEXT_TOKENS = 150_000
#: Cache lifetime per leg: Claude Code's 1-hour entries, fluid's 5-minute gap.
CACHE_TTL_SECONDS: Dict[str, float] = {
    "delegated": 3600.0,
    "anthropic": 300.0,
    "openai_responses": 300.0,
    "openrouter": 300.0,
}


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _last_call(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's newest recorded model call (``turn.usage``). Sync (DB)."""
    from app.db.base import get_db, parse_jsonb

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT created_at, payload FROM session_events WHERE session_id=%s "
            "AND event_type='turn.usage' "
            "AND COALESCE(payload->'usage'->>'auxiliary_source', '') = '' "
            "ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    payload = parse_jsonb(row["payload"])
    usage = (payload or {}).get("usage") if isinstance(payload, dict) else None
    return {"at": _epoch(row["created_at"]), "usage": usage if isinstance(usage, dict) else {}}


def _prompt_tokens(usage: Dict[str, Any]) -> int:
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def assess(
    *, leg: Optional[str], context_tokens: Optional[int], last_call_at: Optional[float],
    now: float,
) -> Dict[str, Any]:
    """The verdict for one chat from its leg, size and last call. Pure."""
    ttl = CACHE_TTL_SECONDS.get(leg or "")
    idle = (now - last_call_at) if last_call_at is not None else None
    cold = ttl is not None and idle is not None and idle > ttl
    large = context_tokens is not None and context_tokens >= LARGE_CONTEXT_TOKENS
    return {
        "leg": leg,
        "context_tokens": context_tokens,
        "last_call_at": last_call_at,
        "idle_seconds": round(idle, 1) if idle is not None else None,
        "cache_ttl_seconds": ttl,
        "threshold_tokens": LARGE_CONTEXT_TOKENS,
        "cold": cold,
        "large": large,
        "warn": bool(cold and large),
    }


# Planning assumptions, not promises about the CLI's summary or future replies.
# The observed delegated post-compaction prompt above was ~17% of the cold one.
COMPACT_RETAINED_RATIO = 0.2
SUMMARY_OUTPUT_TOKENS = 8_000
DEFAULT_REPLY_TOKENS = 4_000


def compare_costs(
    *, model: str, leg: Optional[str], context_tokens: int,
    compacted_context_tokens: int, reply_tokens: int, summary_tokens: int,
    resolver: Any = None,
) -> Optional[Dict[str, Any]]:
    """Price one resumed call, with and without compaction, from the same budget.

    Neither path assumes a cache hit: a rewritten prompt cannot reuse the old
    cached history. Delegated calls also pay the CLI's observed 1-hour cache
    write rate. Native checkpoints also pay for a summary side call.
    Unknown input/output rates must stay unknown, not become a free option.
    """
    from fluid.providers.pricing import estimate_cost, resolve_price

    price = resolve_price(model, resolver=resolver)
    if price is not None and (price.input is None or price.output is None):
        return None

    def cost(prompt: int, output: int) -> Optional[float]:
        usage: Dict[str, Any] = {"output_tokens": output}
        if leg == "delegated":
            usage.update(cache_creation_input_tokens=prompt,
                         cache_creation={"ephemeral_1h_input_tokens": prompt})
        else:
            usage["input_tokens"] = prompt
        total = estimate_cost(model, usage, resolver=resolver)
        return total if total is not None and math.isfinite(total) and total >= 0 else None

    cold = cost(context_tokens, reply_tokens)
    compaction = cost(context_tokens, summary_tokens)
    continued = cost(compacted_context_tokens, reply_tokens)
    if cold is None or compaction is None or continued is None:
        return None
    compact = compaction + continued
    return {
        "model": model,
        "cold_resume_usd": cold,
        "compaction_usd": compaction,
        "continued_usd": continued,
        "compact_continue_usd": compact,
        "compacted_context_tokens": compacted_context_tokens,
        "reply_tokens": reply_tokens,
        "summary_tokens": summary_tokens,
        "recommendation": "compact" if compact < cold else "send",
    }


def _comparison(
    session: Dict[str, Any], leg: Optional[str], tokens: int,
    snapshot: Dict[str, Any], last: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    from app.core.model_registry import API_MODEL_IDS, DEFAULT_MODEL
    from app.core.provider.harness_adapter import (
        _catalog_pricing_resolver, _context_overhead_tokens,
    )
    from app.core.provider.types import SessionOptions
    from app.core.query_options import decode_provider_model

    raw = session.get("model") or session.get("launched_model") or snapshot.get("model")
    raw = raw or DEFAULT_MODEL
    decoded = decode_provider_model(raw)
    provider_id = decoded[0] if decoded else session.get("provider_id")
    model = decoded[1] if decoded else raw
    model = API_MODEL_IDS.get(model, model) if leg == "delegated" else model
    resolver = _catalog_pricing_resolver(SessionOptions(model=model, extra={"provider_id": provider_id}))
    # Preserve measured fixed overhead; compaction cannot remove system/tools.
    overhead = _context_overhead_tokens(snapshot) or 0
    retained = min(tokens, max(math.ceil(tokens * COMPACT_RETAINED_RATIO),
                               overhead + SUMMARY_OUTPUT_TOKENS))
    projection = _native_projection(leg, snapshot)
    if projection is not None:
        retained = projection
    reply = (last or {}).get("usage", {}).get("output_tokens")
    reply = int(reply) if isinstance(reply, (int, float)) and not isinstance(reply, bool) \
        and math.isfinite(reply) and reply > 0 else DEFAULT_REPLY_TOKENS
    return compare_costs(
        model=model, leg=leg, context_tokens=tokens, compacted_context_tokens=retained,
        reply_tokens=reply, summary_tokens=SUMMARY_OUTPUT_TOKENS, resolver=resolver,
    )


def _native_projection(leg: Optional[str], snapshot: Dict[str, Any]) -> Optional[int]:
    if leg == "delegated":
        return None
    compaction = snapshot.get("compaction")
    value = compaction.get("projectedAfterCompactTokens") if isinstance(compaction, dict) else None
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        return int(value)
    return None


def cache_state(session: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """Whether a send into ``session`` now re-reads a large context cold. Sync."""
    from app.core.limit_resume import session_leg
    from app.db import database

    session_id = session["id"]
    leg = session_leg(session)
    last = _last_call(session_id)
    context_tokens: Optional[int] = None
    data: Dict[str, Any] = {}
    try:
        snapshot = database.get_session_context_usage_snapshot(session_id) or {}
        data = snapshot.get("data") or {}
        total = data.get("totalTokens")
        if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            context_tokens = int(total)
    except Exception:  # noqa: BLE001 — fall back to the last call's prompt
        context_tokens = None
    if context_tokens is None and last is not None:
        context_tokens = _prompt_tokens(last["usage"]) or None
    state = assess(
        leg=leg, context_tokens=context_tokens,
        last_call_at=last["at"] if last else None,
        now=time.time() if now is None else now,
    )
    projection = _native_projection(leg, data)
    if projection is not None and context_tokens is not None and projection > context_tokens * 0.75:
        state["warn"] = False
    state["cost_comparison"] = None
    if state["warn"] and context_tokens is not None:
        try:
            state["cost_comparison"] = _comparison(session, leg, context_tokens, data, last)
        except Exception:  # noqa: BLE001 — missing pricing must not hold a send
            pass
    return state
