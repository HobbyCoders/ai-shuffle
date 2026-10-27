"""Whether the next send re-reads a large context cold.

The shared Fluid route policy models idle expiry, not a guarantee of cache hits.
The composer warns only for a large context whose route has likely gone cold.
No provider cache-write options are changed here.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from fluid.context.cache_ttl import (
    delegated_cache_write_ttl, resolve_cache_ttl,
)

#: A context at least this large is worth a warning when its cache is cold.
LARGE_CONTEXT_TOKENS = 150_000


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


# Keep filtering and projection in PostgreSQL: turn snapshots can contain large
# diagnostic payloads, but the send path needs at most two small usage records.
_PROMPT_SQL = " + ".join(
    f"CASE WHEN jsonb_typeof(payload->'usage'->'{key}') = 'number' "
    f"THEN (payload->'usage'->>'{key}')::numeric ELSE 0 END"
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
)
_REAL_CALL_SQL = (
    "session_id=%s AND event_type='turn.usage' "
    "AND COALESCE(payload->'usage'->>'auxiliary_source', '') = '' "
    + "".join(
        f"AND COALESCE(CAST(payload->'usage'->>'{key}' AS TEXT), 'false') NOT IN ('true', '1') "
        for key in ("usage_estimated", "input_tokens_estimated", "output_tokens_estimated")
    )
    + f"AND ({_PROMPT_SQL}) > 0 "
)
_TOKEN_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
_WRITE_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
_USAGE_SQL = ", ".join(
    f"CASE WHEN jsonb_typeof({path}->'{key}') = 'number' "
    f"THEN ({path}->>'{key}')::numeric ELSE NULL END AS {key}"
    for path, fields in (("payload->'usage'", _TOKEN_FIELDS),
                         ("payload->'usage'->'cache_creation'", _WRITE_FIELDS))
    for key in fields
)


def _projected_usage(row: Any) -> Dict[str, Any]:
    usage: Dict[str, Any] = {key: int(row[key]) for key in _TOKEN_FIELDS if row[key] is not None}
    writes = {key: int(row[key]) for key in _WRITE_FIELDS if row[key] is not None}
    if writes:
        usage["cache_creation"] = writes
    return usage


def _last_call(session_id: str, *, include_cache_write: bool = False) -> Optional[Dict[str, Any]]:
    """Newest real main call; optionally retain the last cache-writing evidence.

    Each query returns at most one projected row, never a page of whole payloads.
    Only delegated read-only calls need the second lookup, on the same connection.
    """
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT seq, created_at, {_USAGE_SQL} FROM session_events WHERE "
            + _REAL_CALL_SQL + "ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        usage = _projected_usage(row)
        result = {"at": _epoch(row["created_at"]), "usage": usage}
        if include_cache_write:
            write_usage = usage
            if not usage.get("cache_creation_input_tokens"):
                cursor.execute(
                    f"SELECT {_USAGE_SQL} FROM session_events WHERE "
                    + _REAL_CALL_SQL + "AND seq <= %s "
                    "AND CASE WHEN jsonb_typeof(payload->'usage'->'cache_creation_input_tokens') "
                    "= 'number' THEN (payload->'usage'->>'cache_creation_input_tokens')::numeric "
                    "ELSE 0 END > 0 ORDER BY seq DESC LIMIT 1",
                    (session_id, row["seq"]),
                )
                written = cursor.fetchone()
                write_usage = _projected_usage(written) if written else None
            result["cache_write_usage"] = write_usage
        return result


def _cache_write_usage(last: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return (last or {}).get("cache_write_usage", (last or {}).get("usage"))


def _prompt_tokens(usage: Dict[str, Any]) -> int:
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def assess(
    *, leg: Optional[str], context_tokens: Optional[int], last_call_at: Optional[float],
    now: float, cache_ttl_seconds: Optional[float],
    projected_after_compact_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """The verdict for one chat from its leg, size and last call. Pure.

    Native Compact has an estimated retained floor (system/tools, protected
    recent history and checkpoint). Warn only where reclaiming down to that
    floor saves at least 25%, with 150k as a minimum warning threshold.
    """
    threshold = LARGE_CONTEXT_TOKENS
    if leg != "delegated" and projected_after_compact_tokens is not None:
        # ceil(projection / .75): preserves the existing >=25% savings gate.
        threshold = max(threshold, (projected_after_compact_tokens * 4 + 2) // 3)
    ttl = cache_ttl_seconds
    idle = (now - last_call_at) if last_call_at is not None else None
    cold = ttl is not None and idle is not None and idle > ttl
    large = context_tokens is not None and context_tokens >= threshold
    return {
        "leg": leg,
        "context_tokens": context_tokens,
        "last_call_at": last_call_at,
        "idle_seconds": round(idle, 1) if idle is not None else None,
        "cache_ttl_seconds": ttl,
        "threshold_tokens": threshold,
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
    resolver: Any = None, cache_write_ttl: Literal["1h", "5m"] = "1h",
) -> Optional[Dict[str, Any]]:
    """Price one resumed call, with and without compaction, from the same budget.

    Neither path assumes a cache hit: a rewritten prompt cannot reuse the old
    cached history. Delegated calls also pay the most recent real main cache
    write's rate (1h by default, 5m on credits/API key). Native checkpoints also pay for
    a summary side call.
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
                         cache_creation={f"ephemeral_{cache_write_ttl}_input_tokens": prompt})
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
        cache_write_ttl=delegated_cache_write_ttl(_cache_write_usage(last)),
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
    from app.core.query_options import decode_provider_model
    from app.core.provider.harness_adapter import _lookup_provider_row
    from app.db import database

    session_id = session["id"]
    leg = session_leg(session)
    last = _last_call(session_id, include_cache_write=True) if leg == "delegated" else _last_call(session_id)
    raw_model = session.get("model") or session.get("launched_model")
    decoded = decode_provider_model(raw_model) if raw_model else None
    model = decoded[1] if decoded else raw_model
    provider_id = decoded[0] if decoded else session.get("provider_id")
    row = (_lookup_provider_row(provider_id) or {}) if provider_id else {}
    ttl = resolve_cache_ttl(
        leg, provider_type=row.get("provider_type"), auth_mode=row.get("auth_mode"),
        base_url=row.get("base_url"), model=model, last_usage=_cache_write_usage(last),
    ) if leg in ("delegated", "anthropic", "openai_responses", "openrouter") else None
    context_tokens: Optional[int] = None
    data: Dict[str, Any] = {}
    snapshot: Dict[str, Any] = {}
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
    projection = _native_projection(leg, data)
    captured_at = _epoch(snapshot.get("captured_at"))
    last_call_at = last["at"] if last else None
    # A long idle does not stale a projection; a newer call does. Legacy
    # snapshots without a capture time cannot justify a floor-relative threshold.
    if (captured_at is None or not math.isfinite(captured_at)
            or last_call_at is None or captured_at < last_call_at):
        projection = None
    if projection is not None:
        if model and data.get("model") and model != data["model"]:
            projection = None  # Changing models invalidates the old projection.
    state = assess(
        leg=leg, context_tokens=context_tokens, last_call_at=last_call_at,
        now=time.time() if now is None else now, cache_ttl_seconds=ttl,
        projected_after_compact_tokens=projection,
    )
    if leg != "delegated" and projection is None:
        # Preserve the existing fixed-threshold warning and estimate fallback
        # when the floor is unknown. Never price using an out-of-date projection.
        data = {**data, "compaction": None}
    state["cache_write_ttl"] = delegated_cache_write_ttl(_cache_write_usage(last)) \
        if leg == "delegated" else None
    state["cost_comparison"] = None
    if state["warn"] and context_tokens is not None:
        try:
            state["cost_comparison"] = _comparison(session, leg, context_tokens, data, last)
        except Exception:  # noqa: BLE001 — missing pricing must not hold a send
            pass
    return state
