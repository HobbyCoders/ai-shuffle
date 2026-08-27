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
  ``wait_session_idle`` with NO wall-clock ceiling (a subagent may run for
  hours), then read the outcome from the durable V2 event log.

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
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from app.core import rate_limit_breaker

if TYPE_CHECKING:  # pragma: no cover — annotation-only; the DB imports in this
    # module are all lazy (inside functions), and ``from __future__ import
    # annotations`` above makes the row types free to name here.
    from app.db.sessions import SessionRow

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

#: ``sessions.status`` stamped on a WORKER session whose dispatch ended in the
#: registry status ``"error"`` — the worker ran (or tried to) and did not
#: deliver. Deliberately NOT ``"completed"``: ``worktree_manager`` compares a
#: session's status against the ``WorktreeStatus`` enum strings, where
#: ``COMPLETED`` is a WORKTREE state, so re-using it here would alias two
#: unrelated vocabularies. Workers only — an ordinary chat session never gets
#: this status, and a new turn on the session flips it back to ``"active"``
#: (``app/core/session_runtime.py``, the turn-start revive).
#:
#: The status is EXCLUDED from session SEARCH (``app/db/sessions.py``). Worker
#: sessions are also born list-hidden; their transcript remains fetchable by id
#: for Task output and diagnostics. The status drops them out of the worktree
#: active-session election and idle-edge wakeup candidates too — both test for
#: ``"active"``.
WORKER_FAILED_STATUS = "failed"

#: Cap on the report handed back to the PARENT's context. Match the native
#: task-notification result budget: detailed worker reports should normally arrive
#: intact, while pathological output remains bounded so it cannot dominate the
#: parent's context window.
MAX_REPORT_CHARS = 60_000

#: How often :func:`dispatch_worker` re-checks the provider's circuit breaker
#: while waiting for a worker to go idle. Short enough that a rate-limited
#: worker dies in seconds rather than blocking indefinitely (there is no
#: wall-clock worker timeout), long enough that the check itself — one
#: lock-guarded dict lookup — is free.
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

    The provider's ``default_model`` when it is curated, else ``None`` — an
    unconfigured (or uncurated) default has no answer here. Falling through to
    the first curated id used to look harmless and was a cost trap: curation
    order is arbitrary, so callers landed on whatever model happened to sit at
    index 0 rather than on anything anyone chose.
    """
    ids = curated_model_ids(provider)
    if not ids:
        return None
    default = (provider or {}).get("default_model")
    if isinstance(default, str) and default in ids:
        return default
    return None


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


#: Shapes a tool call takes when a model emits it as PROSE instead of a
#: structured call — the provider stack failed to parse its calling syntax
#: (or the request carried no tool schemas at all), so the "call" lands in
#: the content stream. Seen in the wild as OpenAI-harmony recipient headers
#: (`` to=run_python  code:``); the XML-ish wrappers cover the other serving
#: stacks known to leak. Matched only on a single-response worker (see
#: ``leaked_tool_call_marker``), where the surrounding text is the model's
#: OWN final answer, never quoted tool output.
_LEAKED_TOOL_CALL_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"(?:^|\s)(?:commentary\s+)?to=(?:functions\.)?\w+", re.MULTILINE),
    re.compile(r"<\|?tool_call\|?>", re.IGNORECASE),
    re.compile(r"<function(?:_call)?[=\s>]", re.IGNORECASE),
    re.compile(r"<\|python_tag\|>"),
    re.compile(r"<invoke name="),
)


def leaked_tool_call_marker(text: Optional[str]) -> Optional[str]:
    """The fragment of ``text`` that reads as a tool call emitted as prose.

    A worker whose model's tool calls never reach the tool layer "completes"
    in one turn with its opening narration as the whole report — the call
    itself leaks into the text (``to=run_python  code: ...``). Returning that
    narration as a successful report is worse than failing: the parent trusts
    it, retries against the same broken wiring, or silently drops the task.
    The caller gates this check on ``num_turns <= 1`` so a real multi-turn
    report that merely QUOTES such syntax is never flagged.
    """
    body = text or ""
    for pattern in _LEAKED_TOOL_CALL_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(0).strip()
    return None


def bound_report(text: Optional[str]) -> Tuple[str, bool]:
    """Bound a worker report without discarding either half of its answer.

    Normal detailed reports fit under :data:`MAX_REPORT_CHARS` and pass through
    intact. For pathological output, keep both the findings at the beginning and
    the conclusion at the end with an explicit omission marker. The old 2,000-char
    tail-only cap returned reports beginning mid-sentence and routinely discarded
    the load-bearing file/symbol findings.

    Returns ``(text, truncated)``.
    """
    body = (text or "").strip()
    if len(body) <= MAX_REPORT_CHARS:
        return body, False

    marker = "\n\n[... worker report truncated ...]\n\n"
    remaining = MAX_REPORT_CHARS - len(marker)
    head_chars = remaining // 2
    tail_chars = remaining - head_chars
    bounded = (
        body[:head_chars].rstrip()
        + marker
        + body[-tail_chars:].lstrip()
    )
    return bounded, True


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
       composer's per-session selection, which the FE sends with every query;
    2. else the caller SESSION's own binding (v79) — the mode chosen when that
       session was created, which is also what a voice session (never runs
       turns, VM9) and a server-initiated turn resolve from;
    3. else the caller AGENT's legacy ``config.permission_mode``, for sessions
       created before the binding moved onto the row.

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
        candidate = caller_row.get("permission_mode")
        if isinstance(candidate, str) and candidate.strip():
            mode = candidate.strip()

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
    worker_session_id: str, provider_id: Optional[str]
) -> bool:
    """Wait — with NO wall-clock ceiling — for a worker to go idle, bailing out
    early only if its provider's circuit breaker trips.

    Returns ``True`` when the breaker tripped (the caller must fail the worker
    fast) and ``False`` when the worker went idle on its own.

    A subagent may legitimately run for hours, so there is no timeout here
    (2026-08-21 ruling): a worker that never finishes blocks until it goes idle,
    the breaker trips, or the supervisor tears it down — a worker wedged on a
    silently-hung provider is stopped from the UI. The wait is still observed in
    :data:`WAIT_SLICE_SECONDS` slices so the provider's circuit breaker can be
    re-read between them — a worker whose provider is already refusing every
    request still fails fast instead of retrying forever into a closed door
    (the 2026-07-22 storm's shape, one level down).

    SCOPE LIMITATION (accepted until P3-B): the breaker is in-process state, and
    a worker turn claimed by ANOTHER runner trips THAT runner's breaker, which
    is invisible here — such a worker only settles when it goes idle. This is
    purely about how fast the parent hears about a refusing provider. A
    shared/persisted breaker (P3-G) closes the gap.

    NOTE (2026-08-12): this ``is_tripped()`` read is currently the breaker's only
    consumer. The translator gateway's pre-dispatch refusal — which actually
    stopped the requests — died with the gateway; see
    :mod:`app.core.rate_limit_breaker`.
    """
    from app.core import turn_manager

    waiter = asyncio.ensure_future(
        turn_manager.wait_session_idle(worker_session_id, timeout=float("inf"))
    )
    try:
        while True:
            done, _pending = await asyncio.wait({waiter}, timeout=WAIT_SLICE_SECONDS)
            if done:
                # Surface any exception the wait raised — notably the
                # CancelledError when the parent turn is cancelled, which MUST
                # propagate so the worker is stopped rather than stranded. The
                # idle bool itself no longer carries a verdict (no timeout).
                waiter.result()
                return False
            # No provider binding (delegated Claude-leg worker) -> no breaker
            # to consult; the CLI's own backoff handles subscription limits.
            if provider_id and rate_limit_breaker.is_tripped(provider_id):
                return True
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

#: The ``"message": "..."`` / ``'message': '...'`` pair inside a provider error
#: repr. Provider SDKs stringify the whole HTTP failure — status line, the
#: response-header dict, THEN the body — so the one sentence a human needs sits
#: last and got clipped away by the cap (the live rate-limit incident spent the
#: entire 240-char budget on ``x-ratelimit-*`` headers).
_ERROR_MESSAGE_RE = re.compile(r"['\"]message['\"]\s*:\s*['\"](.+?)['\"]", re.DOTALL)


def _message_first(text: str) -> str:
    """Re-order a provider error so its human message LEADS, headers after.

    Returns ``text`` untouched when no ``message`` field can be found — this may
    only ever improve the ordering, never invent or drop information (the
    remainder is kept verbatim behind the message, so the cap trims the header
    dump instead of the sentence).
    """
    match = _ERROR_MESSAGE_RE.search(text)
    if not match:
        return text
    message = " ".join(match.group(1).split())
    if not message:
        return text
    rest = " ".join((text[: match.start()] + " " + text[match.end():]).split())
    rest = rest.strip(" ,{}")
    return f"{message} — {rest}" if rest else message


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

    The text is re-ordered MESSAGE-FIRST (:func:`_message_first`) before the cap
    so the sentence that explains the failure survives it.
    """
    if status not in _REGISTRY_FAILURE_STATUSES:
        return None
    text = _message_first(" ".join((preview or "").split())) \
        or "no error detail was recorded"
    if len(text) > _FAILURE_SUMMARY_CHARS:
        text = text[: _FAILURE_SUMMARY_CHARS - 1].rstrip() + "…"
    prefix = "worker failed to start" if not frames_forwarded else "worker failed"
    return f"{prefix}: {text}"


def _provider_display_name(provider_id: Optional[str]) -> str:
    """A provider row's display NAME for user-facing summaries.

    ``None`` (the delegated Claude-subscription leg) reads as "agent", matching
    the placeholder the summary carried before. Any lookup failure falls back to
    the id — a summary must never be the thing that breaks a dispatch.
    """
    if not provider_id:
        return "agent"
    try:
        from app.db import database

        row = database.get_model_provider(provider_id, include_token=False)
    except Exception:  # noqa: BLE001 — cosmetic lookup, fail to the id
        logger.debug(
            "ProviderAgent: provider name lookup failed for %s",
            provider_id, exc_info=True,
        )
        return str(provider_id)
    return str((row or {}).get("name") or provider_id)


async def _notify_caller_terminal(
    parent_session_id: str,
    worker_session_id: str,
    launch_key: str,
    status: str,
    summary: Optional[str],
) -> None:
    """Proactively wake the CALLER now that this worker reached a terminal state.

    ProviderAgent workers are dispatched from an MCP tool handler, so NOTHING in
    the SDK-driven notification pipeline ever sees them: there is no SDK task and
    no ``TaskNotificationMessage``, which is why ``idle_reader`` — the only other
    producer of terminal frames — never fires for a worker. Without this call a
    parent that parked on a background worker is woken by nobody: the worker's
    result lands in the registry row and the subagent card, and the parent sits
    parked until a human types something.

    So ``dispatch_worker``'s ``finally`` calls the SAME scheduler seam the idle
    reader uses, with an equivalent frame. Every guard on the far side is left
    alone and still applies: the ownership gate (``is_own_background_task`` —
    which passes because dispatch registers the launch key, see
    ``mark_background_launch`` at the dispatch site), the one-wake-per-task
    ``mark_woken`` latch, the A-3 late-frame guard, and the batching/merge
    branches. Fired for EVERY terminal status (completed / error / killed) of a
    BACKGROUND dispatch, and only on the non-teardown path — a synchronous
    dispatch has no detached caller to wake (its report is the tool result of a
    turn that is still running), and supervisor teardown coalesces its kills
    into one system note by ruling P12 and must not also spawn N wakes.

    ``task_id`` is MANDATORY on the frame (the contentless-frame choke drops a
    frame carrying nothing renderable) and is the worker's session id — the same
    value ``start_task`` registered, so the registry lookup behind the ownership
    gate resolves.

    ``origin`` marks the frame as ProviderAgent-produced so
    ``turn_scheduler._cli_owns_at_rest_wake`` does not mistake a COMPLETED worker
    for a CLI-native backgrounded sub-agent and suppress the app's wake in favour
    of a CLI follow-up turn that will never come (there is no CLI task here).

    Never raises — a wake failure must not change how the worker's dispatch
    reports.
    """
    try:
        from app.db import database

        try:
            parent = await asyncio.to_thread(database.get_session, parent_session_id)
        except Exception:  # fail open: a transient lookup must not lose a real wake
            parent = True
        if parent is None:
            logger.info(
                "ProviderAgent: suppressing completion wake for deleted parent %s "
                "(worker %s)",
                parent_session_id,
                worker_session_id,
            )
            return

        from app.core import turn_scheduler

        await turn_scheduler.on_task_terminal(parent_session_id, {
            "task_id": worker_session_id,
            "status": status,
            "tool_use_id": launch_key,
            "summary": summary or "",
            "origin": turn_scheduler.PROVIDER_AGENT_ORIGIN,
        })
    except Exception:  # noqa: BLE001 — the worker's own outcome stands regardless
        logger.exception(
            "ProviderAgent: caller wake failed for worker %s", worker_session_id
        )


async def dispatch_worker(
    *,
    parent_session_id: str,
    profile_id: str,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    prompt: str,
    label: Optional[str] = None,
    tool_use_id: Optional[str] = None,
    background: bool = False,
    context_session_id: Optional[str] = None,
    launch_tool_use_id: Optional[str] = None,
    system_prompt_append: Optional[str] = None,
    tool_allowlist: Optional[List[str]] = None,
    agent_name: Optional[str] = None,
    worker_session_id: Optional[str] = None,
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

    Has NO wall-clock ceiling (2026-08-21 ruling): a subagent may legitimately
    run for hours, so the wait blocks until the worker goes idle, its provider's
    circuit breaker trips, or the supervisor tears it down. A worker wedged on a
    silently-hung provider is stopped from the UI (Task card / stop control).
    Every failure path still returns a :class:`WorkerOutcome` with ``ok=False``.

    Progress is forwarded to the PARENT's event feed by
    :class:`app.core.provider_agent_feed.WorkerFeed` so the existing subagent
    card renders the worker live; see that module for the fencing/bounding
    rules. Forwarding is strictly best-effort — it can degrade the card, never
    the worker.

    ``agent_name`` is the dispatched agent's DISPLAY NAME ("Frontend Designer").
    It rides the feed's ``subagent.started`` and the task-registry row so the
    card names the agent rather than the model slug it happens to run on
    (2026-08-17 ruling); purely presentational, never a join key.
    """
    # Fail closed on the registry-index-0 cost trap: a provider turn with no
    # model resolves downstream to an arbitrary curated entry, so an implicit
    # provider default is never an acceptable binding for a worker.
    if provider_id and not model_id:
        return WorkerOutcome(
            ok=False,
            session_id="",
            error=(
                "Provider-bound worker dispatch requires an explicit model; "
                "refusing to launch on an implicit provider default."
            ),
        )

    from app.core import provider_agent_supervisor, turn_manager
    from app.core.provider_agent_feed import WorkerFeed, emit_shielded
    from app.core.task_registry import task_registry
    from app.db import database

    # Inheritance (project/worktree/cwd) reads the DIRECT caller's session;
    # ``parent_session_id`` is the ROUTING parent — for nested Task-tool
    # dispatch it is the ROOT session, whose UI renders the card and answers
    # approvals (ruling A9 multi-level routing). They coincide for depth-1.
    parent_row: Optional[SessionRow] = None
    try:
        parent_row = database.get_session(context_session_id or parent_session_id)
    except Exception:  # noqa: BLE001 — inheritance is best-effort
        logger.debug("ProviderAgent: parent session read failed", exc_info=True)
        parent_row = None

    project_id = (parent_row or {}).get("project_id")
    worktree_id = (parent_row or {}).get("worktree_id")
    worktree_branch = (parent_row or {}).get("worktree_branch")
    worktree_path = (parent_row or {}).get("worktree_path")

    # Task supplies the caller's provider and the target's configured model on
    # that provider. ``provider_id=None`` is an explicit native-Claude binding,
    # not permission to fall back to the target profile's top-level provider.
    worker_session_id = worker_session_id or str(uuid.uuid4())
    title = (label or "").strip() or f"Task worker · {model_id or profile_id}"
    # The provider's NAME, not its row id: the registry summary and the card's
    # settle frame are user-facing, and a raw `model_providers` UUID told nobody
    # anything (2026-08-17 incident).
    provider_tag = _provider_display_name(provider_id)
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
            derive_provider_from_profile=False,
            model=model_id,
            hidden=True,
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

    # ONE join key for the whole surface: the worker's session id is the
    # registry task id, the registry's ``tool_use_id`` AND the ``task_id`` on
    # every forwarded subagent.* event. The frontend joins the ``/live`` spinner
    # overlay and the terminal task-settle to the card through ``tool_use_id``
    # (SubagentMessage.svelte:37-44, reducer.ts:1203), so a registry entry with
    # no tool_use_id — as shipped by P3-A1 — could never settle its card.
    launch_key = tool_use_id or worker_session_id
    task_registry.start_task(
        parent_session_id,
        worker_session_id,
        "agent",
        title,
        summary=f"{provider_tag} · {model_tag}",
        tool_use_id=launch_key,
        agent=agent_name,
    )

    # Ownership of this worker's terminal, for the parent's notification lanes.
    # ``is_own_background_task`` — the gate on BOTH the terminal chip
    # (``complete_task``) and the scheduler wake (``on_task_terminal``) — admits a
    # task only when it is an SDK-backgrounded shell or its registry record's
    # ``tool_use_id`` was recorded as a background launch. An MCP-dispatched
    # worker is neither by default, so both lanes silently dropped it and the
    # caller was never told its worker finished. Registering the SAME key the
    # registry row carries (``launch_key``) is what makes the gate pass.
    #
    # BACKGROUND DISPATCHES ONLY (2026-08-17 ruling). A SYNCHRONOUS dispatch
    # hands its report back inline as the tool result, in the caller's own
    # turn — the caller is neither parked nor uninformed, so a terminal chip and
    # a wake would both be noise (and the wake would target a turn that is still
    # running). Only the background mode detaches the outcome from the call, and
    # only it needs the notification lanes. The ``finally`` below fires the wake
    # under the same condition, so the two halves cannot disagree.
    #
    # Park-gate safety (PR #765 / #803 rulings): ``_background_launches`` is
    # CUMULATIVE and is NOT pruned in ``complete_task``, but the ``_finalize``
    # gate counts only UNRESOLVED launches — a launch whose registry record has
    # reached a terminal status no longer arms it. This worker's ``finally``
    # ALWAYS completes the row, so the launch resolves and cannot re-park the
    # session forever. The teardown path additionally purges the id outright
    # (``provider_agent_supervisor._purge_park_gate_residue`` discards
    # ``{tool_use_id, task_id, worker_session_id}`` — a superset of this key).
    if background:
        task_registry.mark_background_launch(parent_session_id, launch_key)

    feed = WorkerFeed(
        parent_session_id=parent_session_id,
        worker_session_id=worker_session_id,
        task_id=worker_session_id,
        label=label,
        provider_id=provider_id,
        model_id=model_id,
        agent_name=agent_name,
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
            tool_use_id=launch_key,
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

        rate_limited = await _await_worker_idle(worker_session_id, provider_id)

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
            # Fail fast rather than block indefinitely: the provider is refusing
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

        final_text = final_assistant_text(worker_session_id)
        num_turns = usage_seen.get("num_turns")
        marker = (
            leaked_tool_call_marker(final_text)
            if isinstance(num_turns, int) and num_turns <= 1
            else None
        )
        if marker:
            # One response, zero tool executions, and the text carries the
            # call the model TRIED to make. That is a tool-wiring failure
            # dressed up as a report — fail it so the parent knows to retry
            # differently instead of trusting narration.
            error = (
                "The worker ended after a single response without executing "
                f"any tool call, and its reply contains what looks like a "
                f"tool call emitted as plain text ({marker!r}). Its model's "
                "tool calls are not reaching the tool layer, so this is "
                "narration, not a report. Retry on a different model or "
                "provider; the partial transcript is in session "
                f"{worker_session_id}."
            )
            preview = error
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error=error,
                usage=outcome["usage"],
            )

        report, truncated = bound_report(final_text)
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
            # Terminal frames FIRST. The terminal ``task.updated`` this emits is
            # what clears the v2 spinner overlay / background-tasks chip (it
            # settles the running frame ``feed.started()`` emitted); the registry
            # drop below only feeds the legacy ``/live`` poll. Either way the
            # card must already be settled by the time the row goes, so the
            # ordering here stays as it is. On the cancellation path the final
            # drain is skipped (the loop
            # is tearing down) and the completed frame is SHIELDED — the reducer
            # settles stale cards only on turn.completed, never on turn.aborted,
            # so a lost terminal frame would leave a permanently spinning card.
            try:
                await feed.stop_following(final_drain=not cancelled)
            except BaseException:  # noqa: BLE001 — teardown must not mask the outcome
                logger.debug("ProviderAgent: feed teardown failed", exc_info=True)
            # A worker killed on THIS path (parent cancellation) never went
            # through the supervisor's teardown, so nothing has settled the
            # worker's own rows: its partial usage would be lost and its
            # turns/sessions rows would stay live-looking forever. Same flush
            # the supervisor runs, claimed by the same guarded turn settle, so
            # a worker that did finish its own turn end is untouched. Shielded
            # for the same reason the terminal frames are — this ``finally``
            # can run while the handler is already cancelling.
            if status == "killed":
                await emit_shielded(
                    provider_agent_supervisor.settle_killed_worker(
                        worker_session_id, "parent_cancelled"
                    )
                )
            # ``status`` is the registry vocabulary (completed/error/killed), which
            # is also the reducer's terminal set for the settle frame.
            await emit_shielded(feed.completed(
                status,
                result_preview=preview,
                summary=f"{provider_tag} · {model_tag}",
                usage=usage_seen,
            ))
            terminal_summary = _failure_summary(
                status, preview, feed.frames_forwarded
            )
            try:
                task_registry.complete_task(
                    parent_session_id,
                    worker_session_id,
                    status,
                    summary=terminal_summary,
                )
            except Exception:  # noqa: BLE001
                logger.exception("ProviderAgent: registry completion failed")
            # The worker session's OWN row records the verdict. Only the
            # ``"error"`` status — the worker ran and did not deliver — is
            # stamped: ``"killed"`` is a teardown (the supervisor's own path,
            # which never reaches this branch) and ``"completed"`` leaves the
            # row exactly as it was. See WORKER_FAILED_STATUS for why the value
            # is "failed" and not "completed". Best-effort: a status write must
            # never change what the dispatch reports to the caller.
            if status == "error":
                try:
                    database.update_session(
                        worker_session_id, status=WORKER_FAILED_STATUS
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "ProviderAgent: failed-status write failed for worker %s",
                        worker_session_id,
                    )
            # Wake the CALLER — BACKGROUND dispatches only, the same condition
            # that registered the launch key above. A synchronous dispatch
            # returns its report inline as the tool result of a turn that is
            # still running, so there is nothing to wake; only a background
            # dispatch detaches the outcome from the call and can leave the
            # caller parked with nobody to unpark it.
            #
            # Shielded for the same reason the terminal frames above are: this
            # ``finally`` can run while the handler is already cancelling
            # (parent client eviction / interrupt), and a wake lost to that
            # cancellation leaves a parked parent stranded. See
            # _notify_caller_terminal for the guard inventory.
            if background:
                await emit_shielded(_notify_caller_terminal(
                    parent_session_id,
                    worker_session_id,
                    launch_key,
                    status,
                    terminal_summary or preview,
                ))
            # Attribution: the authoritative per-provider numbers, recorded
            # while the record is still live so the parent's turn can read them.
            provider_agent_supervisor.record_usage(
                parent_session_id, worker_session_id, usage_seen
            )
            provider_agent_supervisor.unregister_worker(
                parent_session_id, worker_session_id
            )
