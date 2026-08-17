"""ProviderAgent — cross-provider worker sessions (multi-provider phase 3, P3-A).

Option C of ``docs/MULTI_PROVIDER_PHASE3_PLAN.md`` §2: keep the Claude main
FULLY native (subscription OAuth, direct to Anthropic — ruling P1/F1) and
provide other-provider workers not as CLI-native
Task-tool subagents but as an ai-shuffle-provided tool that dispatches a brief
to a SEPARATE session bound to a different provider and returns its report.
Because the two halves are different processes, the "one ``ANTHROPIC_BASE_URL``
per Claude Code process" constraint simply does not apply.

This module owns the framework-free half of that capability — no fastapi, no
SDK — so it is unit-testable on its own and reusable by the API layer:

* :func:`resolve_provider_instance` — resolve the tool's ``instance`` argument
  against the ENABLED ``model_providers`` rows (id match first, then a
  case-insensitive name match; an ambiguous name is an error listing the
  candidates).
* :func:`validate_provider_model` — the provider/model legality checks, lifted
  verbatim from ``app/api/subagents.py`` so both callers share ONE
  implementation. It never raises ``HTTPException``; it returns a
  :class:`ModelValidation` carrying a stable ``code`` the API layer translates
  into its own (unchanged) HTTP error messages.
* :func:`dispatch_worker` — the worker lifecycle, mirroring the proven
  in-process pattern in ``app/core/automation_manager.py``: mint a session,
  ``create_session`` bound to the target provider, ``turn_manager.submit``,
  ``wait_session_idle`` under a bounded timeout, then read the outcome from the
  durable V2 event log.

Scope notes (deliberate — later chunks own these):

* Parent-session event forwarding (chunk A2) lives in
  :mod:`app.core.provider_agent_feed`: ``dispatch_worker`` drives a
  ``WorkerFeed`` that mirrors a BOUNDED summary of the worker's progress onto
  the parent's log as the ``subagent.started`` / ``subagent.progress`` /
  ``subagent.completed`` triple, which is what makes the existing subagent card
  render. The worker's own log is read-only to that tap.
* Permission/approval routing (chunk A3) lives in
  :mod:`app.core.provider_agent_approvals`: ``dispatch_worker`` registers the
  worker's approval route so the worker's tool-approval prompts are raised on —
  and answered from — the PARENT session's UI, tagged with the worker's label.
* Turn-lifetime teardown, stop-all and attribution (chunk A4) live in
  :mod:`app.core.provider_agent_supervisor`; the per-provider circuit
  breaker lives in :mod:`app.core.rate_limit_breaker`.
* The whole capability is gated on ``settings.provider_agent_enabled``, which
  is OFF by default until that safety work lands.

The worker session IS the persisted full report: there is no report table and
no report store. The tool result carries a BOUNDED summary plus the worker's
``session_id`` so a later chunk can link a card to the full transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.core import rate_limit_breaker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ``turns.source`` stamped on a worker's turn. Server-originated (like
#: ``automation``): it is NOT in ``turn_scheduler._USER_SOURCES``, so it never
#: re-arms the auto-continue latch, and a dead-lease reap aborts it rather than
#: re-queueing it (``app/db/turns.py:504``) — a stranded worker must fail, not
#: silently restart.
WORKER_TURN_SOURCE = "provider_agent"

#: Cap on the report handed back to the PARENT's context (plan §2 C.5). The
#: full transcript lives in the worker session; only this much rides the tool
#: result, so uncapped fan-out cannot dominate the parent's context window.
MAX_REPORT_CHARS = 2000

#: How often :func:`dispatch_worker` re-checks the provider's circuit breaker
#: while waiting for a worker to go idle. Short enough that a rate-limited
#: worker dies in seconds rather than at the (minutes-long) worker timeout,
#: long enough that the check itself — one lock-guarded dict lookup — is free.
WAIT_SLICE_SECONDS = 2.0

# Validation result codes — stable identifiers the API layer maps onto its own
# error messages. Never render these to a user directly.
PROVIDER_MISSING = "provider_missing"
PROVIDER_DISABLED = "provider_disabled"
PROVIDER_NOT_SERVABLE = "provider_not_servable"
MODEL_NOT_CURATED = "model_not_curated"

_UNSET = object()


# ---------------------------------------------------------------------------
# One-nesting-level guard (ruling P14)
# ---------------------------------------------------------------------------

#: Session ids minted by :func:`dispatch_worker`, newest last. A worker may not
#: dispatch a worker, so these sessions never get the MCP server registered and
#: the tool refuses the call if one somehow reaches it.
#:
#: In-process and bounded on purpose: a restart kills every live worker anyway,
#: so a forgotten id can only matter for a session a human later resumes by
#: hand. See the module docstring in the PR for the two known holes (post-
#: restart resume, and a worker whose turn is claimed by ANOTHER runner under
#: V2 placement).
_WORKER_SESSION_IDS: "OrderedDict[str, None]" = OrderedDict()
_WORKER_SESSION_LIMIT = 10_000


def register_worker_session(session_id: str) -> None:
    """Record ``session_id`` as a ProviderAgent worker (idempotent)."""
    if not session_id:
        return
    _WORKER_SESSION_IDS.pop(session_id, None)
    _WORKER_SESSION_IDS[session_id] = None
    while len(_WORKER_SESSION_IDS) > _WORKER_SESSION_LIMIT:
        _WORKER_SESSION_IDS.popitem(last=False)


def is_worker_session(session_id: Optional[str]) -> bool:
    """True iff ``session_id`` was minted as a ProviderAgent worker."""
    return bool(session_id) and session_id in _WORKER_SESSION_IDS


def reset_worker_sessions() -> None:
    """Drop every recorded worker id (tests only)."""
    _WORKER_SESSION_IDS.clear()


def is_worker_turn(session_id: Optional[str]) -> bool:
    """True iff ``session_id``'s CURRENT turn is a ProviderAgent worker turn.

    The cluster-safe half of the nesting guard, and the one that actually
    decides (P3-A4). :func:`is_worker_session` above is a process-local set, so
    under V2 placement a worker whose turn is claimed by a DIFFERENT runner is
    simply absent from that runner's set — and that runner would happily offer
    the worker the very tool it was dispatched by, making cross-runner
    recursion possible. It also empties on restart.

    The signal used instead is already persisted and needs no schema change:
    ``dispatch_worker`` submits the worker's turn with
    ``source=WORKER_TURN_SOURCE``, and ``turns.enqueue_turn`` writes ``source``
    to the row (``app/db/turns.py:137/145``). A live turn carrying that source
    IS a worker turn, on any runner, across restarts.

    Deliberately reads the LIVE turn only (``running``/``interrupting``/
    ``draining``) rather than the session's history: a worker session a human
    later resumes and types into is running a ``user`` turn at that point and is
    no longer acting as a worker, so it correctly gets the tool back.

    Fails CLOSED-ish by design: an unreadable turns table returns ``False``
    here, and the caller ORs this with :func:`is_worker_session`, so the
    in-process set still covers the same-runner case that a DB outage would
    otherwise open up.
    """
    if not session_id:
        return False
    try:
        from app.db import turns as turns_db

        turn = turns_db.get_live_turn(session_id)
    except Exception:  # noqa: BLE001 — the in-process set is the fallback
        logger.debug("ProviderAgent: live-turn read failed for %s", session_id, exc_info=True)
        return False
    return bool(turn) and (turn.get("source") == WORKER_TURN_SOURCE)


def is_provider_agent_worker(session_id: Optional[str]) -> bool:
    """The nesting guard callers should use (ruling P14 — one level only).

    ORs the persistent, cluster-safe :func:`is_worker_turn` with the
    process-local :func:`is_worker_session` set. Either alone has a hole; the
    union has neither: the persisted turn source covers other runners and
    restarts, and the in-process set covers the window before the worker's turn
    row goes live.
    """
    return is_worker_session(session_id) or is_worker_turn(session_id)


def worker_timeout_seconds() -> float:
    """The configured per-worker ceiling, clamped to something sane."""
    from app.core.config import settings

    try:
        value = float(getattr(settings, "provider_agent_timeout_seconds", 900) or 900)
    except (TypeError, ValueError):
        value = 900.0
    return max(30.0, value)


# ---------------------------------------------------------------------------
# Provider instance resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedProvider:
    """Outcome of resolving a tool ``instance`` argument to a provider row."""

    provider: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    candidates: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.provider is not None and self.error is None


def resolve_provider_instance(instance: str) -> ResolvedProvider:
    """Resolve ``instance`` against the ENABLED model providers.

    Exact ``id`` match wins; failing that, a case-insensitive match on the
    provider's display ``name``. Two enabled providers sharing a name is an
    error that names the candidate ids (there is no defensible tie-break, and
    silently picking one would send work to the wrong account).

    Only ENABLED providers are considered, so a disabled provider reads as
    "not found" here — :func:`validate_provider_model` is what distinguishes
    missing from disabled for callers that already hold a row.
    """
    name = (instance or "").strip()
    if not name:
        return ResolvedProvider(error="An 'instance' (provider id or name) is required.")

    try:
        from app.db import database

        rows = database.get_enabled_model_providers() or []
    except Exception as exc:  # noqa: BLE001 — a lookup failure is a tool failure
        logger.exception("ProviderAgent: could not list enabled providers")
        return ResolvedProvider(error=f"Could not read the provider list: {exc}")

    if not rows:
        return ResolvedProvider(
            error=(
                "No model providers are enabled; add and enable one in "
                "Settings before dispatching a cross-provider worker."
            )
        )

    for row in rows:
        if row.get("id") == name:
            return ResolvedProvider(provider=dict(row))

    lowered = name.casefold()
    matches = [r for r in rows if (r.get("name") or "").strip().casefold() == lowered]
    if len(matches) == 1:
        return ResolvedProvider(provider=dict(matches[0]))
    if len(matches) > 1:
        candidates = tuple(sorted(str(r.get("id")) for r in matches))
        return ResolvedProvider(
            error=(
                f"'{name}' matches {len(matches)} enabled providers "
                f"({', '.join(candidates)}); use the provider id instead."
            ),
            candidates=candidates,
        )

    known = tuple(sorted(str(r.get("id")) for r in rows))
    return ResolvedProvider(
        error=(
            f"No enabled provider matches '{name}'. Enabled providers: "
            f"{', '.join(known)}."
        ),
        candidates=known,
    )


# ---------------------------------------------------------------------------
# Provider/model validation (framework-free)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelValidation:
    """Outcome of :func:`validate_provider_model`.

    ``code`` is one of the module's ``PROVIDER_*``/``MODEL_*`` constants on
    failure and ``None`` on success. ``reason`` is a ready-to-render message;
    callers that owe a different vocabulary (the subagents API) switch on
    ``code`` and format their own.
    """

    ok: bool
    code: Optional[str] = None
    reason: Optional[str] = None
    provider: Optional[Dict[str, Any]] = None
    provider_label: str = ""
    curated_ids: Tuple[str, ...] = ()


def curated_model_ids(provider: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """The provider's curated model ids, parsed defensively.

    ``models_json`` may already be a parsed list of ``{id, label}`` dicts, a raw
    JSON string, or absent — mirrors
    ``app.core.environment._first_curated_model_id`` but collects ALL ids. An
    absent/empty/unparseable list yields ``()``.
    """
    if not provider:
        return ()
    models = provider.get("models_json")
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except Exception:  # noqa: BLE001 — an unparseable list means "no ids"
            models = None
    ids: List[str] = []
    if isinstance(models, list):
        for entry in models:
            if isinstance(entry, dict):
                entry_id = entry.get("id")
                if isinstance(entry_id, str) and entry_id and entry_id not in ids:
                    ids.append(entry_id)
    return tuple(ids)


def default_model_for_provider(provider: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The model to use when a ProviderAgent call omits ``model``.

    The provider's ``default_model`` when it is curated, else the first curated
    id, else ``None`` (the caller fails with "no curated models").
    """
    ids = curated_model_ids(provider)
    if not ids:
        return None
    default = (provider or {}).get("default_model")
    if isinstance(default, str) and default in ids:
        return default
    return ids[0]


def validate_provider_model(
    provider_id: str,
    model_id: str,
    *,
    provider_row: Any = _UNSET,
    require_servable: bool = True,
) -> ModelValidation:
    """Whether ``model_id`` is legally servable by ``provider_id``.

    Checks, in order: the provider row exists, it is ``enabled``, it is
    :func:`~app.core.environment.is_provider_servable`, and ``model_id`` is in
    its curated ``models_json`` ids. This is the ONE implementation of the
    rules ``app/api/subagents.py`` used to inline.

    ``provider_row`` may be passed by a caller that already fetched the row
    (including an explicit ``None`` meaning "I looked and it does not exist");
    omit it and this function does the lookup itself.

    ``require_servable=False`` skips ONLY the
    :func:`~app.core.environment.is_provider_servable` gate. That predicate is
    the admission rule for *cross-provider WORKER* models, and it still excludes
    ``custom`` rows (whose leg depends on a user-supplied ``base_url``) — but a
    MAIN session may bind to any enabled provider (the profile editor has always
    allowed it). The composer's model override (PA13) validates
    existence + enabled + curated-membership only, so it can offer exactly the
    grouped library the profile editor offers instead of a strict subset.

    NEVER raises for a validation failure and never imports fastapi.
    """
    pid = (provider_id or "").strip()
    mid = (model_id or "").strip()

    row = provider_row
    if row is _UNSET:
        try:
            from app.db import database

            row = database.get_model_provider(pid, include_token=False)
        except Exception:  # noqa: BLE001 — a lookup failure reads as "missing"
            logger.exception("ProviderAgent: provider lookup failed for %s", pid)
            row = None

    if not row:
        return ModelValidation(
            ok=False,
            code=PROVIDER_MISSING,
            reason=f"The provider '{pid}' does not exist.",
            provider_label=pid,
        )

    label = row.get("name") or pid

    if not row.get("enabled"):
        return ModelValidation(
            ok=False,
            code=PROVIDER_DISABLED,
            reason=(
                f"The provider '{label}' is disabled; enable it "
                f"or pick a different provider."
            ),
            provider=dict(row),
            provider_label=label,
        )

    from app.core.environment import is_provider_servable

    if require_servable and not is_provider_servable(row):
        return ModelValidation(
            ok=False,
            code=PROVIDER_NOT_SERVABLE,
            reason=(
                f"The provider '{label}' is not servable: a cross-provider "
                f"model requires an OpenAI-, ChatGPT-, OpenRouter-, or "
                f"Anthropic-API-type provider."
            ),
            provider=dict(row),
            provider_label=label,
        )

    ids = curated_model_ids(row)
    if mid not in ids:
        return ModelValidation(
            ok=False,
            code=MODEL_NOT_CURATED,
            reason=(
                f"Model '{mid}' is not in provider "
                f"'{label}''s curated model list."
            ),
            provider=dict(row),
            provider_label=label,
            curated_ids=ids,
        )

    return ModelValidation(
        ok=True,
        provider=dict(row),
        provider_label=label,
        curated_ids=ids,
    )


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

@dataclass
class WorkerOutcome:
    """What a dispatched worker produced.

    ``session_id`` is ALWAYS the worker's session id — including on failure, so
    a caller can still point at the (possibly partial) transcript.
    """

    ok: bool
    session_id: str
    report: str = ""
    error: Optional[str] = None
    truncated: bool = False
    usage: Dict[str, Any] = field(default_factory=dict)


def bound_report(text: Optional[str]) -> Tuple[str, bool]:
    """Clip a worker report to :data:`MAX_REPORT_CHARS`, keeping the TAIL.

    The TAIL, not the head: a report's conclusion is the part the parent needs,
    and head-truncation cutting it is exactly what the plan (§2 C.5) rejected.
    Returns ``(text, truncated)``.
    """
    body = (text or "").strip()
    if len(body) <= MAX_REPORT_CHARS:
        return body, False
    return body[-MAX_REPORT_CHARS:].lstrip(), True


def _load_session_events(session_id: str) -> List[Dict[str, Any]]:
    """The whole V2 ``session_events`` log for a session, in seq order.

    Never raises — a reader error yields ``[]`` (no derivable outcome).
    Mirrors ``AutomationManager._load_session_events``.
    """
    from app.db import session_events as session_events_db

    events: List[Dict[str, Any]] = []
    try:
        from_seq = 0
        while True:
            batch = session_events_db.get_events(session_id, from_seq=from_seq, limit=500)
            if not batch:
                break
            events.extend(batch)
            if len(batch) < 500:
                break
            from_seq = batch[-1].get("seq") or from_seq
    except Exception:  # noqa: BLE001
        logger.debug("ProviderAgent: event log read failed for %s", session_id, exc_info=True)
        return []
    return events


def final_turn_outcome(session_id: str) -> Dict[str, Any]:
    """Derive a worker's outcome from its LAST terminal turn event.

    Mirrors ``AutomationManager._final_turn_outcome``: ``turn.completed`` →
    usage; ``turn.aborted`` with ``reason == 'interrupt'`` → interrupted; any
    other abort → error; ``turn.parked`` / no terminal event → clean and empty.

    ``terminal_found`` says whether a terminal event existed AT ALL. It is the
    difference between "the worker finished cleanly with no usage" and "we know
    nothing about this worker" — two states the rest of the dict renders
    identically, which is how a worker whose log was never written used to
    resolve as a SUCCESS. Callers that only care about the derived outcome may
    keep ignoring it; :func:`dispatch_worker` fails closed on it.
    """
    events = _load_session_events(session_id)
    terminal = None
    for ev in reversed(events):
        if ev.get("event_type") in ("turn.completed", "turn.aborted", "turn.parked"):
            terminal = ev
            break
    result: Dict[str, Any] = {
        "usage": {},
        "error": None,
        "interrupted": False,
        "terminal_found": terminal is not None,
    }
    if terminal is None:
        return result
    etype = terminal.get("event_type")
    payload = terminal.get("payload") or {}
    if etype == "turn.completed":
        usage = payload.get("usage")
        result["usage"] = usage if isinstance(usage, dict) else {}
    elif etype == "turn.aborted":
        reason = payload.get("reason")
        if reason == "interrupt":
            result["interrupted"] = True
        else:
            result["error"] = (
                payload.get("message")
                or (f"Turn aborted: {reason}" if reason else "Turn aborted")
            )
    return result


def final_assistant_text(session_id: str) -> str:
    """The worker's final assistant text, from the V2 event log.

    Mirrors ``AutomationManager._extract_final_assistant_text``: concatenate the
    LAST completed turn's ``assistant.text_delta`` payloads in seq order, falling
    back to the turn of the last assistant text event when nothing completed.
    Never raises (returns "").
    """
    events = _load_session_events(session_id)

    target_turn_id = None
    for ev in reversed(events):
        if ev.get("event_type") == "turn.completed":
            target_turn_id = ev.get("turn_id")
            break
    if target_turn_id is None:
        for ev in reversed(events):
            if ev.get("event_type") in ("assistant.text_delta", "message.assistant"):
                target_turn_id = ev.get("turn_id")
                break
    if target_turn_id is None:
        return ""

    parts: List[str] = []
    for ev in events:
        if ev.get("turn_id") != target_turn_id:
            continue
        etype = ev.get("event_type")
        payload = ev.get("payload") or {}
        if etype == "assistant.text_delta":
            text = payload.get("text")
        elif etype == "message.assistant":
            if payload.get("kind") not in (None, "text"):
                continue
            text = payload.get("content")
        else:
            continue
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def _parent_cwd(parent_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The parent session's ALREADY-materialized cwd, or ``None``.

    Pure read (``resolve_existing_cwd``) so inheriting a cwd can never clone or
    ``git worktree add`` behind the user's back. When it is ``None`` the worker
    still resolves its own cwd normally from the inherited project/worktree.
    """
    if not parent_row:
        return None
    try:
        from app.core import project_resolver

        return project_resolver.resolve_existing_cwd(dict(parent_row))
    except Exception:  # noqa: BLE001 — best-effort inheritance
        logger.debug("ProviderAgent: parent cwd resolution failed", exc_info=True)
        return None


def _caller_permission_mode(caller_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The caller's EFFECTIVE permission mode, for worker inheritance (VM22).

    A Task-dispatched worker used to run on its own agent's configured
    permission mode, so a call started with bypassPermissions still produced
    approval prompts from its workers. Ruling VM22 (user directive,
    2026-08-15): workers inherit the parent's permission level.

    Resolution mirrors what actually governed the caller:

    1. the caller's most recent turn's ``overrides.permission_mode`` — the
       composer's per-session override, which the FE sends with every query;
    2. else the caller PROFILE's configured ``permission_mode`` — the only
       source for a voice session, which never runs turns (VM9).

    ``plan`` is deliberately NOT inherited: it is a UI conversation mode, and a
    worker dispatched to do work while forced into plan mode could only ever
    produce a plan. Returns ``None`` (no override — the worker's own agent
    config applies) when nothing resolves.
    """
    if not caller_row:
        return None

    mode: Optional[str] = None
    session_id = caller_row.get("id")
    if session_id:
        try:
            from app.db import turns as turns_db

            latest = turns_db.get_latest_turn(str(session_id))
            candidate = ((latest or {}).get("overrides") or {}).get("permission_mode")
            if isinstance(candidate, str) and candidate.strip():
                mode = candidate.strip()
        except Exception:  # noqa: BLE001 — inheritance is best-effort
            logger.debug(
                "ProviderAgent: caller turn-override read failed", exc_info=True,
            )

    if not mode:
        profile_id = caller_row.get("profile_id")
        if profile_id:
            try:
                from app.db import database

                profile = database.get_profile(profile_id)
                candidate = ((profile or {}).get("config") or {}).get("permission_mode")
                if isinstance(candidate, str) and candidate.strip():
                    mode = candidate.strip()
            except Exception:  # noqa: BLE001 — inheritance is best-effort
                logger.debug(
                    "ProviderAgent: caller profile read failed", exc_info=True,
                )

    if not mode or mode == "plan":
        return None
    return mode


async def _await_worker_idle(
    worker_session_id: str, provider_id: Optional[str], timeout: float
) -> Tuple[bool, bool]:
    """Wait for a worker to go idle, bailing out early if its provider trips.

    Returns ``(idle, rate_limited)``.

    The wait is the SAME single ``wait_session_idle(timeout=timeout)`` call as
    before — identical timeout semantics — but it is now observed in
    :data:`WAIT_SLICE_SECONDS` slices so the provider's circuit breaker can be
    re-read between them. Without this a worker whose provider is already
    refusing every request sits there burning the full (default 10-minute)
    worker timeout while the CLI inside it retries into a closed door: exactly
    the 2026-07-22 storm's shape, one level down.

    SCOPE LIMITATION (accepted until P3-B): the breaker is in-process state, and
    a worker turn claimed by ANOTHER runner trips THAT runner's breaker, which
    is invisible here — such a worker still fails only at the timeout. This is
    purely about how fast the parent hears about it. A shared/persisted breaker
    (P3-G) closes the gap.

    NOTE (2026-08-12): this ``is_tripped()`` read is currently the breaker's only
    consumer. The translator gateway's pre-dispatch refusal — which actually
    stopped the requests — died with the gateway; see
    :mod:`app.core.rate_limit_breaker`.
    """
    from app.core import turn_manager

    waiter = asyncio.ensure_future(
        turn_manager.wait_session_idle(worker_session_id, timeout=timeout)
    )
    try:
        while True:
            done, _pending = await asyncio.wait({waiter}, timeout=WAIT_SLICE_SECONDS)
            if done:
                return bool(waiter.result()), False
            # No provider binding (delegated Claude-leg worker) -> no breaker
            # to consult; the CLI's own backoff handles subscription limits.
            if provider_id and rate_limit_breaker.is_tripped(provider_id):
                return False, True
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(BaseException):
                await waiter


def _torn_down_error(reason: Optional[str], worker_session_id: str) -> str:
    """The failure message for a worker the supervisor killed underneath us."""
    from app.core import provider_agent_supervisor as sup

    if reason == sup.STOP_ALL_REASON:
        head = "The worker was stopped (stop-all) before it finished."
    elif reason == sup.TURN_END_REASON:
        head = "The worker was torn down at parent turn end before it finished."
    elif reason == sup.RATE_LIMITED_REASON:
        head = (
            "The worker was stopped because its provider is rate limited "
            "(circuit breaker open)."
        )
    else:
        head = "The worker was stopped before it finished."
    return f"{head} Its partial transcript is in session {worker_session_id}."


def _no_terminal_event_error(worker_session_id: str, turn_id: Optional[str]) -> str:
    """The failure message for a worker that left NO terminal turn event.

    Fail-closed (this is the point): an empty event log used to derive a clean
    empty success, so a worker whose turn never ran — never claimed, killed
    before recording, or writing to a log this process cannot read — reported
    back to the parent as if it had done the job. The durable ``turns`` row is
    the second opinion; when even that is unreadable the outcome is still a
    failure, just a less specific one.
    """
    detail = ""
    if turn_id:
        try:
            from app.db import turns as turns_db

            row = turns_db.get_turn(turn_id)
            if row is not None:
                parts = [f"turn state={row.get('state')}"]
                if row.get("stop_reason"):
                    parts.append(f"stop_reason={row.get('stop_reason')}")
                if row.get("error"):
                    parts.append(f"error={row.get('error')}")
                detail = f" ({', '.join(parts)})"
        except Exception:  # noqa: BLE001 — the failure verdict stands regardless
            logger.debug(
                "ProviderAgent: turns row read failed for worker %s",
                worker_session_id, exc_info=True,
            )
    return (
        f"The worker produced no terminal turn event{detail}, so its result "
        f"cannot be trusted. Its session is {worker_session_id}."
    )


#: Registry statuses that mean the worker did not deliver (task_registry's
#: ``_FAILURE_STATUSES``). Only these carry the failure text on the row.
_REGISTRY_FAILURE_STATUSES = ("error", "killed")

#: How much of a failure message rides the registry row's one-line summary.
_FAILURE_SUMMARY_CHARS = 240


def _failure_summary(
    status: str, preview: Optional[str], frames_forwarded: int
) -> Optional[str]:
    """The registry ``summary`` for a terminal worker, or None to keep it as-is.

    A BACKGROUND dispatch answers the caller the moment the worker registers, so
    the registry row is the only place a later failure can still be told — and
    until now it kept the "<provider> · <model>" summary it was started with,
    leaving TaskList/TaskOutput showing a dead worker with no reason. A failure
    status therefore replaces that summary with the failure text.

    ``frames_forwarded == 0`` means the worker never streamed anything, i.e. it
    died at or before provider connect (the ``ProviderCredentialError`` case) —
    worth saying explicitly, because "failed to start" and "failed while working"
    call for very different responses from the parent.
    """
    if status not in _REGISTRY_FAILURE_STATUSES:
        return None
    text = " ".join((preview or "").split()) or "no error detail was recorded"
    if len(text) > _FAILURE_SUMMARY_CHARS:
        text = text[: _FAILURE_SUMMARY_CHARS - 1].rstrip() + "…"
    prefix = "worker failed to start" if not frames_forwarded else "worker failed"
    return f"{prefix}: {text}"


async def dispatch_worker(
    *,
    parent_session_id: str,
    profile_id: str,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    prompt: str,
    label: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    tool_use_id: Optional[str] = None,
    background: bool = False,
    context_session_id: Optional[str] = None,
    launch_tool_use_id: Optional[str] = None,
    system_prompt_append: Optional[str] = None,
    tool_allowlist: Optional[List[str]] = None,
) -> WorkerOutcome:
    """Run ``prompt`` in a fresh session bound to ``provider_id``/``model_id``.

    Binding (decided, plan §2 C.1/C.2): the worker inherits the PARENT's
    profile, project, worktree and cwd — but NOT its transcript, exactly like
    the CLI's own Task tool — and carries ``parent_session_id`` plus an explicit
    ``provider_id``. The turn is submitted with
    ``overrides={"provider": ..., "model": ...}`` so both the row binding and
    the per-turn override agree.

    The worker is registered in the parent's task registry with ``kind="agent"``
    for the duration. That is load-bearing, not cosmetic: MCP tool handlers run
    as cancellable children of the parent's SDK client, and pool eviction is
    gated on ``has_active_background_work`` which counts ONLY ``kind=="agent"``
    — without it the parent's client can be evicted mid-flight and the worker is
    stranded.

    NEVER hangs the caller: the wait is bounded by ``timeout_seconds`` (default
    ``settings.provider_agent_timeout_seconds``) and every failure path returns
    a :class:`WorkerOutcome` with ``ok=False``.

    Progress is forwarded to the PARENT's event feed by
    :class:`app.core.provider_agent_feed.WorkerFeed` so the existing subagent
    card renders the worker live; see that module for the fencing/bounding
    rules. Forwarding is strictly best-effort — it can degrade the card, never
    the worker.
    """
    from app.core import provider_agent_supervisor, turn_manager
    from app.core.provider_agent_feed import WorkerFeed, emit_shielded
    from app.core.task_registry import task_registry
    from app.db import database

    # Inheritance (project/worktree/cwd) reads the DIRECT caller's session;
    # ``parent_session_id`` is the ROUTING parent — for nested Task-tool
    # dispatch it is the ROOT session, whose UI renders the card and answers
    # approvals (ruling A9 multi-level routing). They coincide for depth-1.
    parent_row: Optional[Dict[str, Any]] = None
    try:
        parent_row = database.get_session(context_session_id or parent_session_id)
    except Exception:  # noqa: BLE001 — inheritance is best-effort
        logger.debug("ProviderAgent: parent session read failed", exc_info=True)
        parent_row = None

    project_id = (parent_row or {}).get("project_id")
    worktree_id = (parent_row or {}).get("worktree_id")
    worktree_branch = (parent_row or {}).get("worktree_branch")
    worktree_path = (parent_row or {}).get("worktree_path")

    # Since P2c (Task tool, ruling A6) provider_id/model_id may be None: the
    # worker then runs the TARGET agent's own binding — create_session resolves
    # provider from the agent's config, and the turn inherits the agent's
    # configured model. A None provider is the delegated Claude-subscription
    # leg, which is a fully valid worker (the hard constraint is honored — it
    # runs through the claude CLI like any other delegated session).
    worker_session_id = str(uuid.uuid4())
    title = (label or "").strip() or f"Task worker · {model_id or profile_id}"
    provider_tag = provider_id or "agent"
    model_tag = model_id or "inherit"

    try:
        database.create_session(
            session_id=worker_session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            parent_session_id=parent_session_id,
            worktree_id=worktree_id,
            worktree_branch=worktree_branch,
            worktree_path=worktree_path,
            provider_id=provider_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ProviderAgent: worker session creation failed")
        return WorkerOutcome(
            ok=False,
            session_id=worker_session_id,
            error=f"Could not create the worker session: {exc}",
        )

    # Mark BEFORE the turn starts: the worker must never be offered the tool it
    # was itself dispatched by (one nesting level only, P14).
    register_worker_session(worker_session_id)

    # --- chunk A3: approval routing (ONE contiguous region) ----------------
    # The worker's tool-approval prompts surface on the PARENT's UI, tagged with
    # this worker's label (plan §2 C.3). Registered HERE, before submit, because
    # the worker's very first tool call can race the submit call's return. The
    # registry is bounded and self-trimming, so there is no unregister to pair
    # with this — see app/core/provider_agent_approvals.
    from app.core import provider_agent_approvals

    provider_agent_approvals.register_route(
        worker_session_id=worker_session_id,
        parent_session_id=parent_session_id,
        label=title,
        provider_id=provider_id,
        model_id=model_id,
    )
    # --- end chunk A3 -------------------------------------------------------

    overrides: Dict[str, Any] = {}
    if provider_id:
        overrides["provider"] = provider_id
    if model_id:
        overrides["model"] = model_id
    cwd = _parent_cwd(parent_row)
    if cwd:
        overrides["cwd"] = cwd
    # Permission inheritance (VM22): the worker runs at the CALLER's effective
    # permission level, so a bypassPermissions parent (e.g. a voice call on a
    # bypass profile) never has its workers stall on approval prompts.
    inherited_mode = _caller_permission_mode(parent_row)
    if inherited_mode:
        overrides["permission_mode"] = inherited_mode
    # A role definition the worker's profile does not carry — today: an agent
    # defined in a ``.claude/agents`` markdown file or a plugin, dispatched on
    # the caller's profile because it has no ``agents`` row of its own (ruling
    # CE6, see app/core/task_dispatch). Rides the same append seam the
    # connectors use, so it lands after the profile's own instructions.
    if system_prompt_append:
        overrides["system_prompt_append"] = system_prompt_append
    # Tool-surface restriction for this worker (ruling CE12). Today's only
    # source is a file/plugin agent definition's ``tools:`` frontmatter, which
    # the caller's profile knows nothing about — so it rides the turn rather
    # than the profile row. Non-empty = the worker's ENTIRE tool surface; absent
    # = inherit the profile's. See app/core/query_options (stamping) and
    # app/core/provider/harness_adapter (per-leg enforcement).
    if tool_allowlist:
        overrides["tool_allowlist"] = list(tool_allowlist)

    timeout = float(timeout_seconds) if timeout_seconds else worker_timeout_seconds()

    # ONE join key for the whole surface: the worker's session id is the
    # registry task id, the registry's ``tool_use_id`` AND the ``task_id`` on
    # every forwarded subagent.* event. The frontend joins the ``/live`` spinner
    # overlay and the terminal task-settle to the card through ``tool_use_id``
    # (SubagentMessage.svelte:37-44, reducer.ts:1203), so a registry entry with
    # no tool_use_id — as shipped by P3-A1 — could never settle its card.
    task_registry.start_task(
        parent_session_id,
        worker_session_id,
        "agent",
        title,
        summary=f"{provider_tag} · {model_tag}",
        tool_use_id=tool_use_id or worker_session_id,
    )

    feed = WorkerFeed(
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id,
        task_id=worker_session_id,
        label=label,
        provider_id=provider_id,
        model_id=model_id,
        prompt=prompt,
        # The model's Task tool_use_id (fluid native leg) — display-only: lets
        # the card absorb the launch tool_use block. Join keys stay task_id.
        launch_tool_use_id=launch_tool_use_id,
    )

    # Turn-lifetime safety (P3-A4). The supervisor is the ONLY thing that knows
    # this worker exists as a worker, and it is what ``SessionRuntime._finalize``
    # consults to guarantee no worker outlives its parent turn. It also carries
    # the per-provider attribution and backs the stop-all control. Registered
    # BEFORE the turn is submitted so a teardown racing the submit still finds
    # (and kills) it.
    supervised = provider_agent_supervisor.register_worker(
        provider_agent_supervisor.WorkerRecord(
            parent_session_id=parent_session_id,
            worker_session_id=worker_session_id,
            task_id=worker_session_id,
            tool_use_id=tool_use_id or worker_session_id,
            provider_id=provider_id or "",
            model_id=model_id or "",
            label=label,
            feed=feed,
            background=background,
        )
    )

    status = "error"
    preview: Optional[str] = None
    cancelled = False
    #: The worker's turn id, once submitted — the second opinion consulted when
    #: the event log carries no terminal event.
    worker_turn_id: Optional[str] = None
    #: Authoritative usage for attribution, filled from the worker's terminal
    #: turn on every path that has one (the feed's live capture is a bonus, not
    #: the source of truth).
    usage_seen: Dict[str, Any] = {}
    try:
        # Emit the card BEFORE the turn is submitted so a submit failure still
        # renders as a failed worker rather than as silence.
        await feed.started()
        worker_turn_id = turn_manager.submit(
            worker_session_id,
            WORKER_TURN_SOURCE,
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            worktree_id=worktree_id,
            overrides=overrides,
        )

        # Tail the worker's durable log from here on — its rows only start
        # existing once the turn is submitted.
        feed.start_following()

        idle, rate_limited = await _await_worker_idle(
            worker_session_id, provider_id, timeout
        )

        # torn_down FIRST, on every branch: the supervisor may have killed this
        # worker while we waited (turn end, stop-all), and a killed worker's
        # event log has no terminal event — which the outcome reader would
        # otherwise render as a clean success and report to the parent as a
        # finished job. The latch is the authoritative answer to "did this
        # worker actually get to run".
        if supervised.torn_down:
            error = _torn_down_error(supervised.torn_down_reason, worker_session_id)
            preview = error
            status = "killed"
            return WorkerOutcome(
                ok=False, session_id=worker_session_id, error=error,
            )

        if rate_limited:
            # Fail fast rather than at the timeout: the provider is refusing
            # everything install-wide, so the worker cannot make progress. Kill
            # it through the supervisor's own per-worker teardown so the card,
            # the registry row and the park-gate residue settle exactly as they
            # do for a turn-end or stop-all kill.
            error = (
                f"Provider '{provider_id}' is rate limited (circuit breaker "
                f"open); worker stopped. Its partial transcript is in session "
                f"{worker_session_id}."
            )
            preview = error
            status = "killed"
            try:
                await provider_agent_supervisor.teardown_worker(
                    supervised,
                    reason=provider_agent_supervisor.RATE_LIMITED_REASON,
                    summary=provider_agent_supervisor.RATE_LIMITED_SUMMARY,
                    preview=error,
                )
            except Exception:  # noqa: BLE001 — the failure verdict stands
                logger.exception(
                    "ProviderAgent: rate-limit teardown failed for worker %s",
                    worker_session_id,
                )
            return WorkerOutcome(
                ok=False, session_id=worker_session_id, error=error,
            )

        if not idle:
            try:
                turn_manager.interrupt(worker_session_id, "stop")
            except Exception:  # noqa: BLE001 — the timeout result stands regardless
                logger.exception("ProviderAgent: interrupt of timed-out worker failed")
            preview = (
                f"The worker did not finish within {int(timeout)}s and was stopped."
            )
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error=(
                    f"The worker did not finish within {int(timeout)}s and was "
                    f"stopped. Its partial transcript is in session "
                    f"{worker_session_id}."
                ),
            )

        outcome = final_turn_outcome(worker_session_id)
        usage_seen = outcome["usage"] if isinstance(outcome["usage"], dict) else {}
        if outcome["error"]:
            preview = str(outcome["error"])
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error=str(outcome["error"]),
                usage=outcome["usage"],
            )
        if outcome["interrupted"]:
            preview = "The worker session was interrupted before it finished."
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error="The worker session was interrupted before it finished.",
                usage=outcome["usage"],
            )
        if not outcome.get("terminal_found"):
            # No terminal turn event at all — see _no_terminal_event_error. The
            # session went idle without ever recording an outcome, which is a
            # failure we cannot describe, never a success.
            error = _no_terminal_event_error(worker_session_id, worker_turn_id)
            preview = error
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error=error,
                usage=outcome["usage"],
            )

        report, truncated = bound_report(final_assistant_text(worker_session_id))
        status = "completed"
        preview = report
        return WorkerOutcome(
            ok=True,
            session_id=worker_session_id,
            report=report,
            truncated=truncated,
            usage=outcome["usage"],
        )
    except asyncio.CancelledError:
        # The parent's SDK client is going away (eviction / interrupt). Stop the
        # worker rather than leaving it running against a parent that is gone.
        status = "killed"
        cancelled = True
        preview = "The parent turn ended before the worker finished."
        try:
            turn_manager.interrupt(worker_session_id, "stop")
        except Exception:  # noqa: BLE001
            logger.debug("ProviderAgent: interrupt on cancel failed", exc_info=True)
        raise
    except Exception as exc:  # noqa: BLE001 — a tool call must never propagate
        logger.exception("ProviderAgent: worker dispatch failed")
        preview = f"Worker dispatch failed: {exc}"
        return WorkerOutcome(
            ok=False,
            session_id=worker_session_id,
            error=f"Worker dispatch failed: {exc}",
        )
    finally:
        # Turn-end teardown may have already settled this worker (card, registry
        # row, park-gate purge and process kill). Repeating any of that would
        # re-emit a terminal frame and re-complete a row that is already
        # terminal, so the latch wins and this block only unregisters.
        if supervised.torn_down:
            provider_agent_supervisor.unregister_worker(
                parent_session_id, worker_session_id
            )
        else:
            # Terminal frames FIRST: the registry drop below is what clears the
            # ``/live`` spinner overlay, and the card must already be settled by
            # then. On the cancellation path the final drain is skipped (the loop
            # is tearing down) and the completed frame is SHIELDED — the reducer
            # settles stale cards only on turn.completed, never on turn.aborted,
            # so a lost terminal frame would leave a permanently spinning card.
            try:
                await feed.stop_following(final_drain=not cancelled)
            except BaseException:  # noqa: BLE001 — teardown must not mask the outcome
                logger.debug("ProviderAgent: feed teardown failed", exc_info=True)
            # ``status`` is the registry vocabulary (completed/error/killed), which
            # is also the reducer's terminal set for the settle frame.
            await emit_shielded(feed.completed(
                status,
                result_preview=preview,
                summary=f"{provider_tag} · {model_tag}",
            ))
            try:
                task_registry.complete_task(
                    parent_session_id,
                    worker_session_id,
                    status,
                    summary=_failure_summary(status, preview, feed.frames_forwarded),
                )
            except Exception:  # noqa: BLE001
                logger.exception("ProviderAgent: registry completion failed")
            # Attribution: the authoritative per-provider numbers, recorded
            # while the record is still live so the parent's turn can read them.
            provider_agent_supervisor.record_usage(
                parent_session_id, worker_session_id, usage_seen
            )
            provider_agent_supervisor.unregister_worker(
                parent_session_id, worker_session_id
            )
