"""After a settled owner turn, review a small window of actual transcript text."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import time
from uuid import uuid4

from app.core import behavioral_provenance as provenance, behavioral_review as review
from app.db.async_database import run_sync
from app.db.base import get_db

logger = logging.getLogger(__name__)
DAILY_CALLS = 6  # Rolling installation-wide ceiling on proposal + verification calls.
BURST_PROPOSALS = 3
DEFER_CANDIDATES = 3
BUDGET_WINDOW_SECONDS = 86_400
RECEIPT_LIMIT = 64
DEBOUNCE_SECONDS = 1.0
TIMEOUT_SECONDS = 12.0
MAX_PENDING = 8
MAX_TEXT_ROWS = 32  # Per logical turn; reject longer transcripts rather than guess.
_STATE_KEY = "behavioral_review_budget_v1"
_queue: OrderedDict[str, tuple[str, asyncio.Event]] = OrderedDict()
_active: tuple[str, asyncio.Event] | None = None
_worker: asyncio.Task | None = None
_stopping = False


@dataclass(frozen=True)
class Scope:
    stem: str
    model: str
    provider_id: str
    account: str


def _scope(session, *, model=None, provider_id=None) -> Scope | None:
    from app.core.auth import auth_service
    from app.core.config import settings
    from app.core.preference_identity import principal_preference_identity
    from app.core.session_modes import session_mode_of
    from app.db import database

    if (not settings.fluid_continual or not session or session.get("api_user_id")
            or session.get("parent_session_id") or session.get("is_worker")
            or session.get("automation_id") or session.get("hidden")
            or session_mode_of(session) != "code"
            or session.get("instance_id") != settings.effective_instance_id
            or not auth_service.get_admin_username()):
        return None
    if session.get("owner_device_id"):
        from app.network.runners import self_device_id
        if session["owner_device_id"] != self_device_id():
            return None
    provider_id = provider_id or session.get("provider_id")
    model = model or session.get("model") or session.get("launched_model")
    row = database.get_model_provider(provider_id) if provider_id else None
    # Do not follow a saved provider pin to a different signed-in account.
    if (not row or not row.get("enabled") or row.get("auth_mode") != "chatgpt"
            or not row.get("codex_account_id") or not model):
        return None
    account = row["codex_account_id"]
    owner = principal_preference_identity({"kind": "admin"})
    key = json.dumps([settings.effective_instance_id, owner, provider_id, account, model])
    return Scope("behavioral-" + sha256(key.encode()).hexdigest()[:32], model, provider_id, account)


def _route(scope: Scope) -> dict | None:
    from app.core.generation_options import text_generation_config
    from app.db import database

    config = text_generation_config()
    selected = config.get("provider")
    if selected != scope.provider_id or not config.get("model"):
        return None
    row = database.resolve_model_provider(selected)
    if (not row or row["id"] != scope.provider_id or row.get("auth_mode") != "chatgpt"
            or row.get("codex_account_id") != scope.account or not row.get("enabled")):
        return None
    # Same previously authorized account, existing one-shot target; cap reasoning.
    return {**config, "provider": scope.provider_id, "effort": "low"}


def _owner_turn(row) -> bool:
    from app.core.auth import auth_service
    from app.db.base import parse_jsonb
    from app.db.turns import person_entered

    payload = parse_jsonb(row.get("payload")) or {}
    return (row.get("source") in ("user", "queue") and row.get("state") == "completed"
            and person_entered(row)
            and not row.get("error") and not payload.get("api_user_id")
            and payload.get("user_id") in (None, "admin", auth_service.get_admin_username())
            and not payload.get("interrupt_source"))


def _execution_scope(session_id: str, turn_id: str) -> Scope | None:
    from app.db import sessions

    binding = provenance.settled_binding(session_id, turn_id)
    if binding is None:
        return None
    scope = _scope(sessions.get_session(session_id), model=binding.model, provider_id=binding.provider_id)
    return scope if scope is not None and scope.account == binding.account else None


def _transcript(session_id: str, turn_id: str, scope: Scope) -> tuple[review.Exchange, ...]:
    from app.core.composer_suggestions import _successful
    from app.db.base import parse_jsonb

    with get_db() as conn:
        cur = conn.cursor()
        # Delimit the window by terminal events, not by the most recent journal
        # cursor. A stop/park/error is a barrier, not evidence to extrapolate.
        cur.execute("""
            SELECT e.turn_id, e.event_type, e.payload, e.seq, t.source, t.state, t.error,
                   t.payload AS turn_payload
            FROM session_events e JOIN turns t ON t.id=e.turn_id AND t.session_id=e.session_id
            WHERE e.session_id=%s AND e.event_type IN ('turn.completed','turn.aborted','turn.parked')
            ORDER BY e.seq DESC LIMIT %s
        """, (session_id, review.MAX_EXCHANGES + 1))
        terminals = cur.fetchall()
        if not terminals or terminals[0]["turn_id"] != turn_id:
            return ()
        eligible = []
        seen = set()
        for row in terminals:
            if (row["turn_id"] in seen or row["event_type"] != "turn.completed"
                    or not _owner_turn({**row, "payload": row["turn_payload"]})
                    or not _successful(parse_jsonb(row["payload"]) or {})):
                break
            seen.add(row["turn_id"])
            cur.execute("""
                SELECT event_type, seq, payload->>'source' AS source,
                       payload->>'kind' AS kind, payload->>'agent_wake' AS wake,
                       payload->>'not_sent' AS not_sent,
                       payload->>'upload_paths' AS uploads, payload->>'agent_content' AS expanded,
                       SUBSTR(COALESCE(payload->>'content', payload->>'text', ''), 1, %s) AS text
                FROM session_events
                WHERE session_id=%s AND turn_id=%s AND seq<%s AND event_type IN
                      ('message.user','message.assistant','assistant.text_delta',
                       'tool.use','tool.result','tool.input_delta')
                ORDER BY seq DESC LIMIT %s
            """, (review.MAX_EXCERPT + 1, session_id, row["turn_id"], row["seq"], MAX_TEXT_ROWS + 1))
            events = cur.fetchall()
            users = [e for e in events if e["event_type"] == "message.user"]
            if len(events) > MAX_TEXT_ROWS or len(users) != 1:
                break
            user = users[0]
            if (user["source"] not in (None, "user", "queue") or user["wake"] == "true"
                    or user["not_sent"] == "true" or user["uploads"] or user["expanded"]
                    or len(user["text"]) > review.MAX_EXCERPT):
                break
            chunks = []
            for event in events:
                kind = event["event_type"]
                if event["seq"] <= user["seq"] or kind.startswith("tool."):
                    break
                if kind == "assistant.text_delta" or (kind == "message.assistant"
                                                       and event["kind"] in (None, "text")):
                    chunks.append(event["text"])
            reply = review.excerpt("".join(reversed(chunks)))
            owner = review.excerpt(user["text"])
            if not owner or not reply or owner != user["text"].strip() or review.screen_findings(owner):
                break
            # Both ends of every exchange must be from this exact native
            # provider/account/model. A session route is not past execution proof.
            if provenance.settled_binding(session_id, row["turn_id"]) != provenance.Binding(
                scope.provider_id, scope.account, scope.model
            ):
                return ()
            eligible.append((owner, reply))
    eligible.reverse()
    return tuple(review.Exchange(previous_reply=previous[1], owner=current[0])
                 for previous, current in zip(eligible, eligible[1:]))


def _claim(session_id: str, turn_id: str, prompt: str, *, verify: bool = False) -> bool:
    """Charge each model call durably. Space later proposals without classifying text."""
    now = int(time.time())
    updated_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    turn_key = sha256(f"{session_id}:{turn_id}".encode()).hexdigest()
    prompt_key = sha256(prompt.encode()).hexdigest()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO system_settings(key,value,updated_at,revision)
                       VALUES (%s,%s,%s,%s) ON CONFLICT(key) DO NOTHING""",
                    (_STATE_KEY, "{}", updated_at, str(uuid4())))
        cur.execute("SELECT value FROM system_settings WHERE key=%s FOR UPDATE", (_STATE_KEY,))
        state = json.loads(cur.fetchone()["value"])
        claims = state.get("claims", [])
        cutoff = now - BUDGET_WINDOW_SECONDS
        active = [entry for entry in claims if entry[0] > cutoff]
        # Receipts store proposal time and, if charged, verification time.
        # Old three-field receipts already represent one call.
        used = len(active) + sum(len(entry) == 4 and entry[3] > cutoff for entry in claims)
        admitted = True
        if verify:
            receipt = next((entry for entry in active if entry[1:3] == [turn_key, prompt_key]), None)
            if receipt is None or len(receipt) != 3 or used >= DAILY_CALLS:
                return False
            receipt.append(now)
        else:
            # A proposal is useful only if there's also room to verify it.
            if used + 2 > DAILY_CALLS:
                return False
            deferred = state.get("deferred", [])
            if any(turn == turn_key or text == prompt_key for _, turn, text, *_ in (*claims, *deferred)):
                return False
            remaining = state.get("defer_remaining", 0) if len(active) >= BURST_PROPOSALS else 0
            if remaining:
                admitted = False
                state["deferred"] = (deferred + [[now, turn_key, prompt_key]])[-RECEIPT_LIMIT:]
                state["defer_remaining"] = remaining - 1
            else:
                state["claims"] = (claims + [[now, turn_key, prompt_key]])[-RECEIPT_LIMIT:]
                state["defer_remaining"] = DEFER_CANDIDATES if len(active) + 1 >= BURST_PROPOSALS else 0
        cur.execute("UPDATE system_settings SET value=%s, updated_at=%s, revision=%s WHERE key=%s",
                    (json.dumps(state), updated_at, str(uuid4()), _STATE_KEY))
    return admitted


def _prepare(session_id: str, turn_id: str):
    from app.core import guidance_storage as storage
    from app.db import turns

    scope = _execution_scope(session_id, turn_id)
    if scope is None or turns.session_has_unsettled_turns(session_id):
        return None
    route = _route(scope)
    if route is None:
        return None
    if storage.automatic_guidance_blocked(scope.stem, scoped=True):
        return None
    saved = storage.read_saved_guidance(scope.stem, scoped=True)
    if saved and saved.source != "automatic":
        return None
    revision = saved.revision if saved else None
    existing = tuple(saved.content.removeprefix(review.HEADER).splitlines()) if saved else ()
    exchanges = _transcript(session_id, turn_id, scope)
    if not exchanges:
        return None
    prompt = review.review_prompt(exchanges, existing)
    return (scope, route, revision, existing, exchanges, prompt) if prompt else None


def _commit(session_id, turn_id, scope, route, revision, content):
    from app.core import guidance_storage as storage
    from app.db import turns

    latest = turns.get_latest_turn(session_id)
    if (not latest or latest["id"] != turn_id or not _owner_turn(latest)
            or _execution_scope(session_id, turn_id) != scope or _route(scope) != route):
        return
    try:
        storage.apply_automatic_guidance(scope.stem, content, expected_revision=revision,
                                         model=scope.model, provider="ChatGPT")
    except (storage.GuidanceConflict, storage.GuidanceValidationError):
        return  # Owner changes and concurrent successful commits win.


async def review_settlement(session_id: str, turn_id: str) -> None:
    from app.api.generation import generate_with_claude

    from app.core.config import settings

    prepared = await run_sync(_prepare, session_id, turn_id)
    if prepared is None:
        return
    scope, route, revision, existing, exchanges, prompt = prepared
    claim_prompt = scope.stem + prompt
    if (not await run_sync(_claim, session_id, turn_id, claim_prompt)
            or not settings.fluid_continual or _stopping):
        return
    async with asyncio.timeout(TIMEOUT_SECONDS):
        binding = (route, scope.account)
        raw = await generate_with_claude(prompt, timeout_seconds=TIMEOUT_SECONDS, review_binding=binding)
        candidate = review.validated_preference(raw, exchanges, existing)
        if candidate is None:
            return
        check = review.verification_prompt(exchanges, existing, candidate)
        if (check is None or not settings.fluid_continual or _stopping
                or not await run_sync(_claim, session_id, turn_id, claim_prompt, verify=True)):
            return
        verdict = await generate_with_claude(check, timeout_seconds=TIMEOUT_SECONDS, review_binding=binding)
    rules = review.verified_rules(verdict, candidate, existing)
    if rules:
        await run_sync(_commit, session_id, turn_id, scope, route, revision,
                       review.HEADER + "\n".join(rules) + "\n")


def enqueue(session_id: str, turn_id: str) -> None:
    from app.core.config import settings
    global _worker
    if _stopping or not settings.fluid_continual:
        return
    pending = _queue.get(session_id)
    if pending or len(_queue) < MAX_PENDING:
        _queue[session_id] = (turn_id, pending[1] if pending else asyncio.Event())
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_drain(), name="behavioral-learning")


async def _drain() -> None:
    global _active
    while _queue:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        session_id, (turn_id, done) = _queue.popitem(last=False)
        _active = session_id, done
        try:
            await review_settlement(session_id, turn_id)
        except Exception as exc:
            # Exception messages from providers can echo the prompt.
            logger.warning("Behavioral review skipped (%s)", type(exc).__name__)
        finally:
            _active = None
            done.set()


def _matching_pending(session_id, model, provider_id, pending):
    from app.db import sessions

    target = _scope(sessions.get_session(session_id), model=model, provider_id=provider_id)
    if target is None:
        return ()
    return tuple(done for sid, done in pending if not done.is_set() and sid != session_id
                 and _scope(sessions.get_session(sid)) == target)


async def wait_for_matching_review(session_id: str, model: str, provider_id: str) -> None:
    """Give in-flight feedback a bounded chance to reach a new matching prompt."""
    pending = [(sid, done) for sid, (_, done) in _queue.items()]
    if _active:
        pending.append(_active)
    if not pending:
        return
    try:
        async with asyncio.timeout(DEBOUNCE_SECONDS + TIMEOUT_SECONDS):
            matching = await run_sync(_matching_pending, session_id, model, provider_id, pending)
            if matching:
                await asyncio.gather(*(done.wait() for done in matching))
    except Exception as exc:
        logger.debug("Behavioral guidance wait ended (%s)", type(exc).__name__)


async def stop() -> None:
    global _stopping
    _stopping = True
    for _, done in _queue.values():
        done.set()
    _queue.clear()
    if _worker is not None:
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)


def start() -> None:
    global _stopping
    _stopping = False


def prompt_block(session_id: str, model: str, provider_id: str) -> str | None:
    """Read latest validated guidance for an exact real session binding."""
    from app.core import guidance_storage as storage
    from app.db import sessions

    try:
        scope = _scope(sessions.get_session(session_id), model=model, provider_id=provider_id)
        if scope:
            saved = storage.read_saved_guidance(scope.stem, scoped=True)
            if (saved and saved.model == scope.model and saved.provider == "ChatGPT"
                    and saved.inactive_reason is None and not review.screen_findings(saved.content)):
                if saved.source == "automatic":
                    return saved.content
                return review.HEADER + saved.content
    except Exception as exc:
        logger.warning("Behavioral guidance skipped (%s)", type(exc).__name__)
    return None
