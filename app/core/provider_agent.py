"""Worker sessions for Task dispatch (any provider).

Framework-free (no fastapi), so it is unit-testable on its own and shared with
the API layer:

* :func:`resolve_provider_instance` — resolve an ``instance`` argument against
  the ENABLED ``model_providers`` rows (id first, then a case-insensitive name;
  an ambiguous name is an error listing the candidates).
* :func:`validate_provider_model` — provider/model legality checks returning a
  :class:`ModelValidation` with a stable ``code``; never raises
  ``HTTPException``.
* :func:`dispatch_worker` — mint a session bound to the target provider, submit
  the turn, wait for idle (see :func:`_await_worker_idle` for the deadlines),
  and read the outcome from the V2 event log.

Related: :mod:`app.core.provider_agent_feed` (progress on the parent card),
:mod:`app.core.provider_agent_approvals` (approvals routed to the parent),
:mod:`app.core.provider_agent_supervisor` (teardown and attribution) and
:mod:`app.core.rate_limit_breaker`.

The worker session is the full report: the tool result carries a bounded
summary plus the worker's ``session_id``.
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
from typing import (
    TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Tuple,
)

from app.core import rate_limit_breaker
from app.core.model_effort import EFFORT_LEVELS

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
#: re-queueing it (``turns.reap_expired``) — a stranded worker must fail, not
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
# Worker-session detection
# ---------------------------------------------------------------------------

#: Session ids minted by :func:`dispatch_worker`, newest last. Read by
#: ``task_dispatch``'s depth/cycle walk and by ``query_options``, which
#: withholds ``AskUserQuestion`` from workers.
#:
#: In-process and bounded on purpose: a restart kills every live worker anyway,
#: so a forgotten id can only matter for a session a human later resumes by
#: hand. Its two holes (post-restart resume, and a worker whose turn is claimed
#: by ANOTHER runner under V2 placement) are covered by :func:`is_worker_turn`.
_WORKER_SESSION_IDS: "OrderedDict[str, None]" = OrderedDict()
_WORKER_SESSION_LIMIT = 10_000


def register_worker_session(session_id: str) -> None:
    """Record ``session_id`` as a worker session (idempotent)."""
    if not session_id:
        return
    _WORKER_SESSION_IDS.pop(session_id, None)
    _WORKER_SESSION_IDS[session_id] = None
    while len(_WORKER_SESSION_IDS) > _WORKER_SESSION_LIMIT:
        _WORKER_SESSION_IDS.popitem(last=False)


def is_worker_session(session_id: Optional[str]) -> bool:
    """True iff ``session_id`` was minted as a worker session."""
    return bool(session_id) and session_id in _WORKER_SESSION_IDS


def reset_worker_sessions() -> None:
    """Drop every recorded worker id (tests only)."""
    _WORKER_SESSION_IDS.clear()


def is_worker_turn(session_id: Optional[str]) -> bool:
    """True iff ``session_id``'s CURRENT turn is a worker turn.

    The cluster-safe half of worker detection. :func:`is_worker_session` above
    is a process-local set, so under V2 placement a worker whose turn is
    claimed by a DIFFERENT runner is simply absent from that runner's set. It
    also empties on restart.

    The signal used instead is already persisted: ``dispatch_worker`` submits
    the worker's turn with ``source=WORKER_TURN_SOURCE``, and
    ``turns.enqueue_turn`` writes ``source`` to the row. A live turn carrying
    that source IS a worker turn, on any runner, across restarts.

    Deliberately reads the LIVE turn only (``running``/``interrupting``/
    ``draining``) rather than the session's history: a worker session a human
    later resumes and types into is running a ``user`` turn at that point and is
    not acting as a worker, so it correctly gets its tools back.

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
    """The worker check callers should use.

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
    """The model to use when a caller omits ``model``.

    The provider's ``default_model`` when it is curated, else ``None`` — an
    unconfigured (or uncurated) default has no answer here. Falling through to
    the first curated id would be a cost trap: curation order is arbitrary, so
    callers would land on whatever model sits at index 0 rather than on
    anything anyone chose.
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
    its curated ``models_json`` ids. This is the ONE implementation of these
    rules.

    ``provider_row`` may be passed by a caller that already fetched the row
    (including an explicit ``None`` meaning "I looked and it does not exist");
    omit it and this function does the lookup itself.

    ``require_servable=False`` skips ONLY the
    :func:`~app.core.environment.is_provider_servable` gate. That predicate is
    the admission rule for *cross-provider WORKER* models, and it still excludes
    ``custom`` rows (whose leg depends on a user-supplied ``base_url``) — but a
    MAIN session may bind to any enabled provider (the profile editor has always
    allowed it). The composer's model override validates
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
            # No raw id: the owner cannot act on one. Callers add what to do.
            reason="The AI provider for this model has been removed.",
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
    #: Progress frames the worker streamed before it settled. ``0`` means the
    #: parent saw no worker progress; it does not prove that an admitted
    #: provider request never began. :mod:`app.core.task_dispatch` combines this
    #: with the failure reason before deciding whether a retry is safe.
    frames_forwarded: int = 0
    #: True when the worker was TORN DOWN (stop-all, parent turn end, tripped
    #: circuit breaker) rather than failing on its own. Never retried: the
    #: teardown is the answer, not a transport hiccup.
    killed: bool = False


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
    the conclusion at the end with an explicit omission marker. A tail-only cap
    would return reports beginning mid-sentence and discard the load-bearing
    file/symbol findings.

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
    identically, so without it a worker whose log was never written would
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
    """The worker's REPORT: the text it wrote after its last tool result.

    The report is the answer the worker addressed to whoever dispatched it, and
    an agentic worker writes it in one place — at the end, once the tools have
    stopped. Everything before that is working narration addressed to nobody:
    "I'll start by mapping the call sites", "Next I'll read the adapter".
    Concatenating EVERY ``assistant.text_delta`` of the last turn would hand
    the parent the whole monologue, and :func:`_completion_summary` — which
    takes the first non-empty line — would put the worker's OPENING sentence on
    the registry row as the headline of what came back.

    So: the last turn's text, cut at its last ``tool.result``. The whole
    concatenation is the fallback for the one worker shape that has no such cut
    — a single answer with no tool calls at all — and for a final turn that ends
    on a tool result with nothing after it.

    The turn is the LAST completed turn, falling back to the
    turn of the last assistant text event when nothing completed. Never raises
    (returns "").
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
    #: Text seen since the LAST ``tool.result`` — reset every time one lands, so
    #: what survives the loop is exactly the closing statement.
    tail: List[str] = []
    for ev in events:
        if ev.get("turn_id") != target_turn_id:
            continue
        etype = ev.get("event_type")
        payload = ev.get("payload") or {}
        if etype == "tool.result":
            tail = []
            continue
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
            tail.append(text)
    return "".join(tail).strip() or "".join(parts).strip()


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


_EFFORT_UNSET = object()


def _caller_effort(caller_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Snapshot the direct caller's selected effort, before a worker queues.

    The running turn overrides the session binding; voice uses its live binding.
    Legacy caller profiles are only a fallback. Target-model validation stays
    in the adapter, since a worker may run on a different model or provider.
    """
    from app.db import database, turns

    row = caller_row or {}
    if row.get("id") and row.get("mode") != "voice":
        try:
            turn = turns.get_live_turn(str(row["id"]))
            if turn is None:
                turn = turns.get_latest_turn(str(row["id"])) or {}
                # A queued user message has not configured an engine yet.
                if turn.get("state") == "queued":
                    turn = {}
            value = (turn.get("overrides") or {}).get("effort")
            if isinstance(value, str) and value in EFFORT_LEVELS:
                return value
        except Exception:  # noqa: BLE001 — legacy/session fallback
            logger.debug("ProviderAgent: caller effort turn read failed", exc_info=True)
    value = row.get("effort")
    if isinstance(value, str) and value in EFFORT_LEVELS:
        return value
    if row.get("profile_id") and not row.get("is_worker"):
        try:
            profile = database.get_profile(row["profile_id"]) or {}
            value = (profile.get("config") or {}).get("effort")
            if isinstance(value, str) and value in EFFORT_LEVELS:
                return value
        except Exception:  # noqa: BLE001 — no legacy binding
            logger.debug("ProviderAgent: caller effort profile read failed", exc_info=True)
    return None


def _positive_window(value: Any) -> Optional[int]:
    """``value`` as a positive token count, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        window = int(value)
    except (TypeError, ValueError):
        return None
    return window if window > 0 else None


def _caller_context_window(caller_row: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The direct caller's selected input window, for worker inheritance.

    Resolved like :func:`_caller_effort`: the running turn's override, else the
    latest configured turn's, else the session binding (voice reads only its
    live binding). ``None`` means the caller never chose one, so the worker
    keeps its model's default window.
    """
    from app.db import turns

    row = caller_row or {}
    if row.get("id") and row.get("mode") != "voice":
        try:
            turn = turns.get_live_turn(str(row["id"]))
            if turn is None:
                turn = turns.get_latest_turn(str(row["id"])) or {}
                if turn.get("state") == "queued":
                    turn = {}
            window = _positive_window((turn.get("overrides") or {}).get("context_window"))
            if window is not None:
                return window
        except Exception:  # noqa: BLE001 — session binding fallback
            logger.debug("ProviderAgent: caller window turn read failed", exc_info=True)
    return _positive_window(row.get("context_window"))


def worker_context_window(
    window: Optional[int], provider_id: Optional[str], model_id: Optional[str],
) -> Optional[int]:
    """Fit the caller's window choice onto the worker's own model.

    The picker's choice is "standard" or "1M"; the stored value is that
    choice's token count for the caller's model (200K for Claude, 256K/272K
    for GPT). A worker on another model keeps the same choice: an exact match
    passes through, otherwise a sub-1M window becomes the worker model's
    standard window and a 1M window its 1M one. When the worker's choices
    cannot be resolved here (an inherited model, an unknown row) the value
    passes through unchanged and ``query_options`` drops it if the model
    cannot run it — the same fallback a session turn gets.
    """
    window = _positive_window(window)
    if window is None:
        return None
    from app.core.model_registry import context_window_choices

    row = None
    if provider_id:
        try:
            from app.db import database

            row = database.get_model_provider(provider_id, include_token=False)
        except Exception:  # noqa: BLE001 — query_options validates instead
            logger.debug("ProviderAgent: worker provider row read failed", exc_info=True)
            return window
        if not row:
            return window
    try:
        choices = context_window_choices(model_id, row) if model_id else []
    except Exception:  # noqa: BLE001 — query_options validates instead
        logger.debug("ProviderAgent: worker window choices failed", exc_info=True)
        return window
    if not choices or window in choices:
        return window
    if window >= 1_000_000:
        return max(choices) if max(choices) >= 1_000_000 else None
    return min(choices)


def _caller_permission_mode(caller_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The caller's EFFECTIVE permission mode, for worker inheritance.

    Workers inherit the parent's permission level rather than their own agent's
    configured mode, so a call started with bypassPermissions does not produce
    approval prompts from its workers.

    Voice uses its live session binding rather than a historical typed turn.
    For other sessions, resolution mirrors what actually governed the caller:

    0. the connected caller's live policy, including mid-turn picker changes;
    1. the caller's most recent turn's ``overrides.permission_mode`` — the
       composer's per-session selection, which the FE sends with every query;
    2. else the caller SESSION's own binding — the mode chosen when that
       session was created, which is also what a voice session (never runs
       turns) and a server-initiated turn resolve from;
    3. else the caller AGENT's legacy ``config.permission_mode``, for sessions
       created before the binding moved onto the row.

    Retired ``plan`` settings are ignored. Returns ``None`` (no override — the
    worker's own agent config applies) when nothing resolves.
    """
    if not caller_row:
        return None

    mode: Optional[str] = None
    session_id = caller_row.get("id")
    if session_id and caller_row.get("mode") != "voice":
        from app.core.session_pool import get_session_pool

        live = get_session_pool().peek(str(session_id))
        if live is not None and live.is_connected:
            candidate = live.callback_refs.get("permission_mode")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip() if candidate != "plan" else None
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


def worker_session_fields(
    *,
    worker_session_id: str,
    profile_id: str,
    title: str,
    parent_session_id: Optional[str],
    provider_id: Optional[str],
    model_id: Optional[str],
    effort: Optional[str],
    api_user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    project_v2_id: Optional[str] = None,
    target_instance_id: Optional[str] = None,
    worktree_id: Optional[str] = None,
    worktree_branch: Optional[str] = None,
    worktree_path: Optional[str] = None,
    permission_mode: Optional[str] = None,
    context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """``database.create_session`` keywords for a Task worker's own row.

    One shape for a worker this process dispatches and one another worker
    asks it to run (:mod:`app.core.remote_task`): hidden, flagged as a worker,
    and bound to an explicit provider (``None`` is native Claude, never the
    profile's provider). ``permission_mode`` is stamped only when given.
    ``context_window`` (the caller's window choice, fitted to this worker by
    :func:`worker_context_window`) is stamped on the row, so every later turn
    of the worker — a message, a restart, a limit-reset resume — runs with
    the same window as its first.
    """
    fields: Dict[str, Any] = dict(
        session_id=worker_session_id,
        profile_id=profile_id,
        api_user_id=api_user_id,
        project_id=project_id,
        project_v2_id=project_v2_id,
        target_instance_id=target_instance_id,
        title=title,
        parent_session_id=parent_session_id,
        worktree_id=worktree_id,
        worktree_branch=worktree_branch,
        worktree_path=worktree_path,
        provider_id=provider_id,
        derive_provider_from_profile=False,
        model=model_id,
        effort=effort,
        hidden=True,
        # Materialized flag backing the sidebar's worker filter. `hidden`
        # alone still hides this row, but stamping it here keeps the column
        # self-maintaining instead of relying on the one-shot backfill.
        is_worker=True,
    )
    if permission_mode:
        fields["permission_mode"] = permission_mode
    if context_window:
        fields["context_window"] = context_window
    return fields


def worker_turn_overrides(
    *,
    provider_id: Optional[str],
    model_id: Optional[str],
    effort: Optional[str],
    cwd: Optional[str],
    permission_mode: Optional[str],
    system_prompt_append: Optional[str] = None,
    checkout: Any = None,
    tool_allowlist: Optional[List[str]] = None,
    tool_denylist: Optional[List[str]] = None,
    voice_row: Optional[Mapping[str, Any]] = None,
    context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """The overrides a Task worker's turn is submitted with, local or remote.

    ``checkout`` is a :class:`app.core.worker_checkout.WorkerCheckout`; its
    instructions follow ``system_prompt_append``. ``context_window`` is the
    caller's window choice fitted to the worker (see
    :func:`worker_context_window`). ``voice_row`` is a voice caller, whose
    fast mode the worker keeps.
    """
    overrides: Dict[str, Any] = {}
    if provider_id:
        overrides["provider"] = provider_id
    if model_id:
        overrides["model"] = model_id
    if effort is not None:
        overrides["effort"] = effort
    if cwd:
        overrides["cwd"] = cwd
    if permission_mode:
        overrides["permission_mode"] = permission_mode
    if context_window:
        overrides["context_window"] = context_window
    if voice_row and voice_row.get("fast") is not None:
        overrides["fast"] = voice_row.get("fast")
    # A role definition the worker's profile does not carry — today: an agent
    # defined in a ``.fluid/agents`` markdown file or a plugin, dispatched on
    # the caller's profile because it has no ``agents`` row of its own (ruling
    # CE6, see app/core/task_dispatch). Rides the same append seam the
    # connectors use, so it lands after the profile's own instructions.
    append_parts = [system_prompt_append] if system_prompt_append else []
    if checkout:
        append_parts.append(checkout.instructions())
    if append_parts:
        overrides["system_prompt_append"] = "\n\n".join(append_parts)
    # Tool-surface restriction for this worker (ruling CE12). Today's only
    # source is a file/plugin agent definition's ``tools:`` frontmatter, which
    # the caller's profile knows nothing about — so it rides the turn rather
    # than the profile row. Non-empty = the worker's ENTIRE tool surface; absent
    # = inherit the profile's. See app/core/query_options (stamping) and
    # app/core/provider/harness_adapter (per-leg enforcement).
    if tool_allowlist:
        overrides["tool_allowlist"] = list(tool_allowlist)
    # A file/plugin agent's ``disallowedTools``: never callable by this
    # worker, whatever it inherits (added to ``disallowed_tools`` in
    # app/core/query_options, which both legs enforce).
    if tool_denylist:
        overrides["tool_denylist"] = list(tool_denylist)
    return overrides


async def _await_worker_idle(
    worker_session_id: str,
    provider_id: Optional[str],
    *,
    feed: Any = None,
    first_frame_deadline: Optional[float] = None,
    wall_cap: Optional["_WallCap"] = None,
) -> Optional[str]:
    """Wait for a worker to go idle, bailing out early on a liveness verdict.

    Returns ``None`` when the worker went idle on its own, or one of
    :data:`WAIT_RATE_LIMITED` / :data:`WAIT_NO_FIRST_FRAME` /
    :data:`WAIT_DEADLINE` — in every non-``None`` case the caller must fail the
    worker rather than read an outcome that does not exist.

    A WORKING subagent may legitimately run for hours, so there is no ceiling
    on one: it blocks until it goes idle, the breaker trips, or the supervisor
    tears it down. Two deadlines bound only the states where "running for
    hours" describes nothing:

    * ``first_frame_deadline`` — an absolute ``loop.time()`` by which the
      worker must have shown progress: a forwarded frame, or a worker row that
      proves its provider is answering (reasoning or tool-argument deltas, a
      finished call's usage, or ``turn.provider_alive``, which proves only that
      the provider accepted the request and was streaming). A reasoning model
      can stream only invisible reasoning for minutes, and those rows count as
      liveness even though the parent card never shows them. What is left for
      the deadline is a request that never started. Once progress has been
      seen the deadline stops applying, permanently.
    * ``wall_cap`` — a hard wall-clock ceiling (:class:`_WallCap`) for a
      dispatch whose caller cannot survive an unbounded wait (the SYNCHRONOUS
      lane: it blocks the parent's turn, so "no timeout" would mean "no steer,
      no park, no progress signal" for as long as the worker runs), or for a
      worker the subscription loop guard covers. The loop guard's limit is
      re-read while waiting, so a changed setting reaches this worker.

    The wait is observed in :data:`WAIT_SLICE_SECONDS` slices so the provider's
    circuit breaker can be re-read between them — a worker whose provider is
    already refusing every request fails fast instead of retrying forever. Both
    deadlines are checked on the same cadence, so their resolution is one slice.

    The breaker is in-process state: a worker turn claimed by ANOTHER runner
    trips THAT runner's breaker, which is invisible here, so such a worker only
    settles when it goes idle.
    """
    from app.core import turn_manager

    loop = asyncio.get_event_loop()
    waiter = asyncio.ensure_future(
        turn_manager.wait_session_idle(worker_session_id, timeout=float("inf"))
    )
    try:
        while True:
            now = loop.time()
            if wall_cap is not None:
                await wall_cap.refresh(now)
            hard_deadline = wall_cap.deadline() if wall_cap is not None else None
            if hard_deadline is not None and now >= hard_deadline:
                return WAIT_DEADLINE
            wait_seconds = WAIT_SLICE_SECONDS
            if hard_deadline is not None:
                wait_seconds = min(wait_seconds, hard_deadline - now)
            done, _pending = await asyncio.wait({waiter}, timeout=wait_seconds)
            if hard_deadline is not None and loop.time() >= hard_deadline:
                return WAIT_DEADLINE
            if done:
                # Surface any exception the wait raised — notably the
                # CancelledError when the parent turn is cancelled, which MUST
                # propagate so the worker is stopped rather than stranded. The
                # idle bool itself carries no verdict (no timeout).
                waiter.result()
                return None
            # No provider binding (delegated Claude-leg worker) -> no breaker
            # to consult; the CLI's own backoff handles subscription limits.
            if provider_id and rate_limit_breaker.is_tripped(provider_id):
                return WAIT_RATE_LIMITED
            now = loop.time()
            if (
                first_frame_deadline is not None
                and now >= first_frame_deadline
                and not await _worker_showed_progress(feed)
            ):
                return WAIT_NO_FIRST_FRAME
            if hard_deadline is not None and now >= hard_deadline:
                return WAIT_DEADLINE
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(BaseException):
                await waiter


def _feed_shows_progress(feed: Any) -> bool:
    return bool(
        int(getattr(feed, "frames_forwarded", 0) or 0)
        or int(getattr(feed, "provider_progress", 0) or 0)
    )


async def _worker_showed_progress(feed: Any) -> bool:
    """Whether the worker has done anything the first-frame watchdog must honour.

    The feed polls on its own cadence, so a verdict taken from its counters
    alone can miss rows written since its last tick. Before answering "no",
    drain it once: rows already in the worker's log count, and any text or tool
    rows among them are forwarded as they would be on the next tick.
    """
    if _feed_shows_progress(feed):
        return True
    drain = getattr(feed, "drain", None)
    if drain is None:
        return False
    try:
        await drain()
    except Exception:  # noqa: BLE001 — a failed catch-up read keeps the verdict
        logger.debug("ProviderAgent: watchdog catch-up drain failed", exc_info=True)
    return _feed_shows_progress(feed)


#: How long to wait between re-reads while a worker sits PARKED on background
#: work of its own. Long enough that a worker parked on a half-hour CI run costs
#: two cheap indexed reads a minute, short enough that its wake is noticed.
PARK_RECHECK_SECONDS = 5.0


def _worker_still_working(worker_session_id: str) -> bool:
    """True when a worker that LOOKS idle has not actually finished.

    A parked turn releases its run lease and its live-turn slot, so a worker that
    ended its turn to wait on its OWN background work (say, a CI run) reads as
    an idle session. ``_await_worker_idle`` returns on it, and the outcome
    reader treats ``turn.parked`` as a clean, empty end, so without this check
    the dispatch would book the worker ``completed`` with a half-written report.

    A park is not an ending. It is an ending only once the work being waited on
    is gone: either the worker unparks and finishes for real, or its background
    tasks drain and the park is stranded, in which case there is nothing left to
    wait for and the caller settles on what it has.

    Deliberately narrow — a PARKED latest turn with live background work behind
    it, and nothing else. The caller has just come back from
    ``_await_worker_idle``, so a running or queued turn is already its problem,
    and anything this predicate is unsure about must answer "finished": it gates
    a wait, and a wait that never ends is worse than the settle it replaces.

    "Latest" skips the rows of messages steered into the worker's turn: each
    settles its own queue row ``completed`` the moment it is delivered, so it is
    newer than the turn that read it and later parked, and is not that turn.

    Blocking (two indexed reads) — call it off the loop. Never raises: anything
    unreadable answers "not working".
    """
    try:
        from app.db import turns as turns_db

        row = turns_db.get_latest_run_turn(worker_session_id)
    except Exception:  # noqa: BLE001 — an unreadable row settles the dispatch
        logger.debug(
            "ProviderAgent: park probe failed for worker %s",
            worker_session_id, exc_info=True,
        )
        return False
    if (row or {}).get("state") != "parked":
        return False
    return bool(_active_worker_background_work(worker_session_id))


def _worker_deadlines(
    background: bool,
    first_frame_seconds: Optional[float],
    max_wall_seconds: Optional[float],
    provider_id: Optional[str] = None,
    origin: Optional[str] = None,
) -> Tuple[float, float, bool]:
    """``(first_frame_seconds, max_wall_seconds, loop_guarded)`` for one
    dispatch; 0 = off.

    ``None`` means "take the configured default", which differs by lane: BOTH
    lanes get the first-frame watchdog (a worker that never connects is dead in
    either), but only the SYNCHRONOUS lane gets a hard wall-clock cap, because
    only it is holding a parent turn hostage while it waits. A background
    parent has parked and can be woken; a sync parent can do nothing at all.

    Exception: when the lane default would leave it uncapped, a worker the
    subscription loop guard covers (``loop_guard.covers_worker``: on the Claude
    CLI, and ``origin`` — what started its chain of work — is unattended) gets
    the guard's per-worker limit (60 minutes unless changed in Settings), so one
    looping automation worker cannot spend a plan all night. ``loop_guarded``
    is then True, and the limit is re-read while the worker runs
    (:class:`_WallCap`). A worker a person's turn started is never capped here.
    """
    from app.core.config import settings

    if first_frame_seconds is None:
        first_frame_seconds = float(
            getattr(settings, "task_worker_first_frame_seconds", 90.0) or 0.0
        )
    if max_wall_seconds is None:
        max_wall_seconds = (
            0.0
            if background
            else float(getattr(settings, "task_worker_sync_max_seconds", 0.0) or 0.0)
        )
    max_wall_seconds = max(0.0, float(max_wall_seconds))
    loop_guarded = False
    if max_wall_seconds == 0.0:
        try:
            from app.core import loop_guard

            loop_guarded = loop_guard.covers_worker(provider_id, origin)
            if loop_guarded:
                max_wall_seconds = loop_guard.worker_max_seconds(provider_id, origin)
        except Exception:  # noqa: BLE001 — a settings hiccup keeps the lane default
            logger.warning("Loop guard worker cap unavailable", exc_info=True)
    return max(0.0, float(first_frame_seconds)), max_wall_seconds, loop_guarded


#: How often a worker the loop guard covers re-reads its time limit, so a limit
#: changed in Settings reaches workers already running.
LOOP_GUARD_RECHECK_SECONDS = 30.0


class _WallCap:
    """One dispatch's hard wall-clock ceiling, measured from ``started``.

    The lane's cap (the synchronous one, or an explicit ``max_wall_seconds``)
    is fixed at dispatch. A worker the loop guard covers (``loop_guarded``)
    re-reads the guard's per-worker limit every
    :data:`LOOP_GUARD_RECHECK_SECONDS`: lowering it stops a worker already past
    the new value, and raising or switching it off spares one.
    """

    def __init__(
        self,
        started: float,
        seconds: float,
        *,
        provider_id: Optional[str] = None,
        origin: Optional[str] = None,
        loop_guarded: bool = False,
    ) -> None:
        self.started = started
        self.seconds = seconds
        self.provider_id = provider_id
        self.origin = origin
        self.loop_guarded = loop_guarded
        self._checked = started

    def deadline(self) -> Optional[float]:
        return self.started + self.seconds if self.seconds > 0 else None

    async def refresh(self, now: float) -> None:
        if not self.loop_guarded or now - self._checked < LOOP_GUARD_RECHECK_SECONDS:
            return
        self._checked = now
        try:
            from app.core import loop_guard

            self.seconds = await asyncio.to_thread(
                loop_guard.worker_max_seconds, self.provider_id, self.origin
            )
        except Exception:  # noqa: BLE001 — keep the last value on a settings hiccup
            logger.warning("Loop guard worker cap re-read failed", exc_info=True)

    def error(self, worker_session_id: str) -> str:
        if self.loop_guarded:
            from app.core import loop_guard

            return loop_guard.worker_cap_error(worker_session_id, self.seconds, self.origin)
        return _hard_deadline_error(worker_session_id, self.seconds)


def _no_first_frame_error(worker_session_id: str, seconds: float) -> str:
    """The failure text for a worker that produced no parent-visible frame.

    Worded so :func:`_failure_summary` — which prefixes ``"worker failed to
    start"`` on ordinary zero-progress failures — reads as one sentence on the
    registry row. The request's provider-side state is deliberately left
    unknown; this error is what makes the watchdog non-retryable.
    """
    return (
        f"The worker forwarded no output within {seconds:.0f}s; its provider "
        "request state is unknown after the watchdog interrupt. Its session is "
        f"{worker_session_id}."
    )


def _hard_deadline_error(worker_session_id: str, seconds: float) -> str:
    """The failure text for a synchronous worker that blew its wall-clock cap."""
    return (
        f"The worker did not finish within the {seconds:.0f}s cap for a "
        "synchronous Task, so the call was abandoned rather than blocking the "
        "parent turn any further. Re-dispatch it in the background if the work "
        f"genuinely takes this long. Its partial transcript is in session "
        f"{worker_session_id}."
    )


async def _stop_wedged_worker(worker_session_id: str) -> None:
    """Interrupt a worker the watchdog just gave up on.

    Deliberately NOT the supervisor's teardown: teardown latches ``torn_down``,
    which makes ``dispatch_worker``'s ``finally`` skip the registry completion
    and the caller wake — and a watchdog verdict is precisely the case where
    the parent MUST be told with a clear no-output summary. So this only stops
    the turn and lets the normal failure path do the reporting. Never raises.
    """
    from app.core import turn_manager

    try:
        await turn_manager.interrupt_async(worker_session_id, "stop")
    except Exception:  # noqa: BLE001 — the failure verdict stands either way
        logger.debug(
            "ProviderAgent: could not interrupt wedged worker %s",
            worker_session_id, exc_info=True,
        )


def _active_worker_background_work(worker_session_id: str) -> List[Dict[str, Any]]:
    """The worker's own still-running background tasks (Monitors, children).

    In-memory registry read; never raises (an unreadable registry answers
    "nothing outstanding", which is the pre-existing behaviour).
    """
    try:
        from app.core.task_registry import task_registry

        return [record for record in task_registry.get_active(worker_session_id) or []
                if not task_registry.is_serving_monitor(worker_session_id, _work_task_id(record))]
    except Exception:  # noqa: BLE001
        logger.debug(
            "ProviderAgent: background-work probe failed for worker %s",
            worker_session_id, exc_info=True,
        )
        return []


def _work_task_id(record: Mapping[str, Any]) -> Optional[str]:
    """The task id off a registry record in EITHER shape.

    ``get_active`` hands back the camelCase WIRE dict (``id``/``subject``), not
    the internal record — reading ``task_id`` there is how the first cut of
    this reported "Monitor ?".
    """
    value = record.get("id") or record.get("task_id")
    return str(value) if value else None


def _describe_background_work(records: List[Dict[str, Any]]) -> str:
    """A short "Monitor abc123 (npm test)" list for a failure message."""
    parts = []
    for record in records[:4]:
        kind = "Monitor" if record.get("kind") == "shell" else "worker"
        title = " ".join(
            str(record.get("subject") or record.get("title") or "").split()
        )[:60]
        tid = _work_task_id(record) or "?"
        parts.append(f"{kind} {tid}" + (f" ({title})" if title else ""))
    if len(records) > 4:
        parts.append(f"and {len(records) - 4} more")
    return ", ".join(parts)


def _background_settle_seconds() -> float:
    """Configured drain grace for a finished worker's own background work."""
    try:
        from app.core.config import settings

        return max(
            0.0,
            float(
                getattr(settings, "task_worker_background_settle_seconds", 0.0) or 0.0
            ),
        )
    except Exception:  # noqa: BLE001 — a missing setting must not fail a worker
        return 0.0


async def _force_settle_background_work(
    worker_session_id: str, records: List[Dict[str, Any]]
) -> None:
    """Retire a dead worker's abandoned Monitor rows.

    Nothing else ever will: a Monitor record is scoped to the session that made
    it (``TaskGet`` resolves against the CALLER's session), so once its worker
    is gone the row can only sit "running" forever, keeping the session's
    background-work predicates true. Child WORKER rows are left alone — their
    own dispatch owns them and completes them properly. Never raises.
    """
    try:
        from app.core.task_registry import task_registry

        for record in records:
            task_id = _work_task_id(record)
            if (record.get("kind") != "shell" or not task_id
                    or task_registry.is_serving_monitor(worker_session_id, task_id)):
                continue
            # The stop primitive kills and reaps the owned process tree before
            # recording its outcome. A registry-only transition strands it.
            from app.mcp.builtin_tools import _background_tasks, stop_background_shell_task

            tracked = _background_tasks.get(task_id)
            if tracked is not None and tracked.session_id != worker_session_id:
                logger.warning("Refusing to stop shell %s owned by another session", task_id)
                continue
            try:
                result = await stop_background_shell_task(
                    task_id, session_id=worker_session_id,
                    reason="worker_abandoned",
                    summary=(
                        "Abandoned: the worker that started it ended first, so "
                        "nothing can read its result."
                    ),
                )
            except Exception:
                logger.warning("Could not stop abandoned shell %s", task_id, exc_info=True)
                continue
            if result is None or result.get("running"):
                logger.warning("Worker %s could not stop abandoned shell %s: %s",
                               worker_session_id, task_id, result)
    except Exception:  # noqa: BLE001 — bookkeeping, never fatal
        logger.debug(
            "ProviderAgent: force-settle of worker %s background work failed",
            worker_session_id, exc_info=True,
        )


def _outstanding_work_error(
    worker_session_id: str, records: List[Dict[str, Any]]
) -> str:
    """The failure text for a worker that ended with its own work still live."""
    return (
        "worker ended with background work outstanding: "
        f"{_describe_background_work(records)}. Its final message is an interim "
        "note, not a result, and nothing can reach that work now — a worker has "
        "no wake seam. Re-dispatch with the work done inside the turn. Its "
        f"transcript is in session {worker_session_id}."
    )


def _settle_owner_row(
    owner_session_id: str,
    worker_session_id: str,
    launch_key: str,
    status: str,
    summary: Optional[str],
    *,
    report_path: Optional[str] = None,
    report_chars: Optional[int] = None,
    notify: bool = True,
) -> None:
    """Terminate the OWNER's copy of a nested worker's registry row.

    The owner's row is what arms its park gate and its wake. Leaving it
    "running" would park the dispatching worker forever on a child that is
    already gone. Never raises: this is bookkeeping beside an outcome that is
    already decided.
    """
    from app.core.task_registry import task_registry

    try:
        task_registry.complete_task(
            owner_session_id,
            worker_session_id,
            status,
            summary=summary,
            report_path=report_path,
            report_chars=report_chars,
            notify=notify,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "ProviderAgent: owner registry completion failed for worker %s",
            worker_session_id,
        )
    if status in _REGISTRY_FAILURE_STATUSES:
        # A failed/killed worker must not keep the owner's park gate armed. The
        # gate counts UNRESOLVED launches and ``complete_task`` above resolves
        # this one, but a row that never existed (registry cleared mid-flight)
        # would leave the launch id behind — so discard it outright, exactly as
        # the supervisor does for the routing parent.
        try:
            task_registry.discard_background_launch(owner_session_id, launch_key)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ProviderAgent: owner park-gate purge failed for worker %s",
                worker_session_id, exc_info=True,
            )


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

    Fail-closed (this is the point): an empty event log must not derive a clean
    empty success, or a worker whose turn never ran — never claimed, killed
    before recording, or writing to a log this process cannot read — would
    report back to the parent as if it had done the job. The durable ``turns``
    row is the second opinion; when even that is unreadable the outcome is
    still a failure, just a less specific one.
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


# ---------------------------------------------------------------------------
# Provider rate-limit refusals at worker connect
# ---------------------------------------------------------------------------

#: The provider's own "come back in N" hint, in the two shapes that reach us:
#: the sentence OpenAI puts in a 429 body ("Please try again in 1.049s",
#: "...in 980ms") and the ``retry-after`` header a provider SDK stringifies
#: into the same error repr. Seconds is the unit both are normalized to.
_RETRY_AFTER_SENTENCE_RE = re.compile(
    r"try\s+again\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s|sec|secs|seconds?)\b",
    re.IGNORECASE,
)
_RETRY_AFTER_HEADER_RE = re.compile(
    r"['\"]?retry[-_]after['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

#: Substrings that identify a capacity refusal rather than a broken request.
#: Deliberately narrow — a mis-classification here would retry a worker whose
#: brief is the problem, which is how a bad dispatch becomes four bad
#: dispatches.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "tokens per min",
    " tpm",
    "(tpm)",
    "quota exceeded",
    "over capacity",
    "overloaded",
)

#: The HTTP status, as its own token. A bare ``"429"`` substring is NOT safe
#: here: every failure message carries the worker's session id, and a UUID
#: containing ``-429c-`` made a first-frame timeout read as a rate limit.
_HTTP_429_RE = re.compile(r"(?<![0-9A-Za-z])429(?![0-9A-Za-z])")


def looks_rate_limited(text: Optional[str]) -> bool:
    """True when ``text`` reads as a provider CAPACITY refusal.

    A capacity refusal is the one failure a re-dispatch reliably fixes: the
    brief is fine, the org's token window was simply full. Everything else —
    a bad model id, a missing credential, a rejected prompt — comes back
    identical however many times it is retried.
    """
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return True
    return bool(_HTTP_429_RE.search(lowered))


def looks_usage_limited(text: Optional[str]) -> bool:
    """True when ``text`` says a provider USAGE limit stopped the worker.

    A spent session, 5-hour, weekly or plan allowance ("You've hit your
    session limit · resets 11:10pm", "Usage limit reached: ...") is not a
    capacity refusal: no re-dispatch clears it before the reset, so it is
    never retried. Checked before :func:`looks_rate_limited`, which also
    matches it (it carries a 429).
    """
    if not text:
        return False
    from fluid.providers._retry import is_usage_limit_text

    return is_usage_limit_text(text)


def rate_limit_retry_after(text: Optional[str]) -> Optional[float]:
    """Seconds the provider asked us to wait, or ``None``.

    ``None`` means "this is not a reschedulable capacity refusal" — either the
    text is not a rate limit at all, or it carries no usable hint, in which
    case guessing an interval is worse than reporting the failure. A parsed
    value is clamped to a sane band: providers have asked for ``980ms`` (too
    short to be worth a round trip on its own — the CALLER's backoff widens it)
    and, rarely, for hours (which is not a retry, it is an outage).
    """
    if not looks_rate_limited(text):
        return None
    assert text is not None
    match = _RETRY_AFTER_SENTENCE_RE.search(text)
    if match:
        value = float(match.group(1))
        if match.group(2).lower() == "ms":
            value /= 1000.0
    else:
        header = _RETRY_AFTER_HEADER_RE.search(text)
        if not header:
            return None
        value = float(header.group(1))
    if value <= 0 or value > 600.0:
        return None
    return value


# ---------------------------------------------------------------------------
# Liveness watchdog verdicts
# ---------------------------------------------------------------------------

#: The worker's provider connection tripped its install-wide circuit breaker.
WAIT_RATE_LIMITED = "rate_limited"
#: The worker forwarded ZERO frames inside the first-frame deadline — it never
#: started. See :data:`app.core.config.Settings.task_worker_first_frame_seconds`.
WAIT_NO_FIRST_FRAME = "no_first_frame"
#: The worker blew a hard wall-clock cap (synchronous dispatch only).
WAIT_DEADLINE = "deadline"


#: Registry statuses that mean the worker did not deliver (task_registry's
#: ``_FAILURE_STATUSES``). Only these carry the failure text on the row.
_REGISTRY_FAILURE_STATUSES = ("error", "killed")

#: How much of a failure message rides the registry row's one-line summary.
_FAILURE_SUMMARY_CHARS = 240

#: The ``"message": "..."`` / ``'message': '...'`` pair inside a provider error
#: repr. Provider SDKs stringify the whole HTTP failure — status line, the
#: response-header dict, THEN the body — so the one sentence a human needs sits
#: last and gets clipped away by the cap (``x-ratelimit-*`` headers alone can
#: fill the 240-char budget).
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


#: How much of a completed worker's report becomes its registry ``summary``.
_COMPLETION_SUMMARY_CHARS = 160


def _completion_summary(report: Optional[str]) -> Optional[str]:
    """A one-line headline for a cleanly finished worker, or None.

    The counterpart to :func:`_failure_summary`. It replaces the
    "<provider> · <model>" summary the row started with, so a parent reading
    TaskList/TaskGet after a wake learns what came back, not just which model
    ran. The report's first non-empty line is what the worker itself chose to
    lead with, so it is the cheapest honest answer to "what came back".
    """
    for line in (report or "").splitlines():
        text = " ".join(line.split()).lstrip("# ").strip()
        if not text:
            continue
        if len(text) > _COMPLETION_SUMMARY_CHARS:
            text = text[: _COMPLETION_SUMMARY_CHARS - 1].rstrip() + "…"
        return text
    return None


def _persist_worker_report(
    parent_row: Optional[Mapping[str, Any]],
    worker_session_id: str,
    report: str,
) -> Tuple[Optional[str], Optional[int]]:
    """Write a background worker's report to the parent's report dir.

    Shares ONE writer with the sync path
    (:func:`app.core.task_dispatch.write_task_report`) so both modes land in
    ``<parent cwd>/.fluid/task-reports/<task_id>.md`` — the path a parent can
    actually reach.

    Strictly best-effort: a failure returns ``(None, len(report))`` and the
    completion proceeds untouched. Import is lazy because ``task_dispatch``
    imports this module.
    """
    try:
        from app.core import task_dispatch

        path = task_dispatch.write_task_report(parent_row, worker_session_id, report)
    except Exception:  # noqa: BLE001 — a completed worker is never failed on I/O
        logger.warning(
            "ProviderAgent: could not persist the report for worker %s",
            worker_session_id,
            exc_info=True,
        )
        path = None
    return path, len(report or "")


def _failure_summary(
    status: str, preview: Optional[str], frames_forwarded: int
) -> Optional[str]:
    """The registry ``summary`` for a terminal worker, or None to keep it as-is.

    A BACKGROUND dispatch answers the caller the moment the worker registers, so
    the registry row is the only place a later failure can still be told. A
    failure status therefore replaces the "<provider> · <model>" summary the
    row started with, so TaskList/TaskOutput never show a dead worker with no
    reason.

    ``frames_forwarded == 0`` means the parent saw no worker progress. It can
    describe a preflight failure, a transport failure, or a watchdog interrupt
    that caught an admitted request before its first frame.

    The text is re-ordered MESSAGE-FIRST (:func:`_message_first`) before the cap
    so the sentence that explains the failure survives it.
    """
    if status not in _REGISTRY_FAILURE_STATUSES:
        return None
    text = _message_first(" ".join((preview or "").split())) \
        or "no error detail was recorded"
    if len(text) > _FAILURE_SUMMARY_CHARS:
        text = text[: _FAILURE_SUMMARY_CHARS - 1].rstrip() + "…"
    if not frames_forwarded:
        # Say WHICH kind of no-progress failure this is. A capacity refusal is a
        # queueing problem the harness has already retried; a parent that reads
        # only the raw provider string ("Rate limit reached for gpt-5.6-luna in
        # organization org-…") learns to work around the harness instead, for
        # example by capping its own workers' tool calls.
        if "forwarded no output within" in text:
            prefix = "worker stopped before first output"
        elif looks_usage_limited(text):
            prefix = "worker stopped by a usage limit (not retried)"
        elif looks_rate_limited(text):
            prefix = "worker failed to start (provider at capacity, already retried)"
        else:
            prefix = "worker failed to start"
    else:
        prefix = "worker failed"
    return f"{prefix}: {text}"


class _WorkerRefused(Exception):
    """An admission refusal from the dispatch prologue (not a DB failure)."""


def _provider_display_name(provider_id: Optional[str]) -> str:
    """A provider row's display NAME for user-facing summaries.

    ``None`` (the delegated Claude-subscription leg) reads as "agent". Any
    lookup failure falls back to
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


def _log_status_write_failure(task: "asyncio.Future[Any]") -> None:
    """Retrieve (and log) the outcome of a status write that outlived our own
    cancellation, so a failure is neither hidden nor left as an unretrieved
    exception on the loop."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception(
            "ProviderAgent: failed-status write failed", exc_info=exc
        )


async def _notify_caller_terminal(
    parent_session_id: str,
    worker_session_id: str,
    launch_key: str,
    status: str,
    summary: Optional[str],
) -> None:
    """Proactively wake the CALLER now that this worker reached a terminal state.

    Task workers are dispatched from an app tool handler, so there is no CLI
    task and no ``TaskNotificationMessage`` for them. Without this call a
    parent that parked on a background worker is woken by nobody: the worker's
    result lands in the registry row and the subagent card, and the parent sits
    parked until a human types something.

    So ``dispatch_worker``'s ``finally`` calls the scheduler's unpark seam
    (``turn_scheduler.on_task_terminal``) with a terminal frame. Every guard on the far side is left
    alone and still applies: the ownership gate (``is_own_background_task`` —
    which passes because dispatch registers the launch key, see
    ``mark_background_launch`` at the dispatch site), the one-wake-per-task
    ``mark_woken`` latch, the late-frame guard, and the batching/merge
    branches. Fired for EVERY terminal status (completed / error / killed) of a
    BACKGROUND dispatch, and only on the non-teardown path — a synchronous
    dispatch has no detached caller to wake (its report is the tool result of a
    turn that is still running), and supervisor teardown coalesces its kills
    into one system note and must not also spawn N wakes.

    ``task_id`` is MANDATORY on the frame (the contentless-frame choke drops a
    frame carrying nothing renderable) and is the worker's session id — the same
    value ``start_task`` registered, so the registry lookup behind the ownership
    gate resolves.

    Never raises — a wake failure must not change how the worker's dispatch
    reports.
    """
    from app.core.swarm import owns_terminal
    if owns_terminal(parent_session_id):
        return  # The retry-wrapper settles Swarm; only its aggregate wakes.
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
        })
    except Exception:  # noqa: BLE001 — the worker's own outcome stands regardless
        logger.exception(
            "ProviderAgent: caller wake failed for worker %s", worker_session_id
        )


async def _pause_for_usage_limit(
    worker_session_id: str, worker_turn_id: str, owner_session_id: str,
) -> Optional[str]:
    """Pause a background worker a usage limit stopped, if its provider says so.

    Reads the worker turn's terminal payload; on a usage-limit stop it asks
    :func:`app.core.limit_resume.on_limit_stop` to schedule the resume (the
    provider's "Resume when the limit resets" setting, a known reset time, a
    parent that was not stopped). Returns the paused report text when a resume
    was scheduled, else None and the worker settles as it always has. Never
    raises.
    """
    try:
        from app.core import account_failover, limit_resume, turn_scheduler
        from app.db import turns as turns_db

        terminal = await asyncio.to_thread(
            turn_scheduler._turn_terminal_payload, worker_session_id, worker_turn_id,
        )
        if not account_failover.is_usage_limit_error(terminal):
            return None
        turn = await asyncio.to_thread(turns_db.get_turn, worker_turn_id) or {}
        scheduled = await limit_resume.on_limit_stop(
            worker_session_id, turn, terminal, worker_owner=owner_session_id,
        )
        return limit_resume.PAUSED_SUMMARY if scheduled is not None else None
    except Exception:  # noqa: BLE001 — the worker settles as before
        logger.warning(
            "ProviderAgent: usage-limit pause check failed for worker %s",
            worker_session_id, exc_info=True,
        )
        return None


async def announce_abandoned_retry(
    owner_session_id: str, worker_session_id: str, summary: str
) -> None:
    """Tell the caller about a failure whose wake a planned retry suppressed.

    ``dispatch_worker``'s ``suppress_caller_wake`` predicate takes the chip and
    the wake off an attempt the caller said it was going to re-dispatch. If that
    re-dispatch then never happens — it could not be admitted — nobody has told
    the parent anything, and a parent parked on the receipt would sit there
    forever. This is the one caller-side escape hatch for that window.

    Never raises: it runs beside an outcome that is already decided.
    """
    from app.core.task_registry import task_registry

    try:
        task_registry.complete_task(
            owner_session_id, worker_session_id, "error", summary=summary,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "ProviderAgent: abandoned-retry row update failed for %s",
            worker_session_id, exc_info=True,
        )
    await _notify_caller_terminal(
        owner_session_id, worker_session_id, worker_session_id, "error", summary,
    )


async def dispatch_worker(
    *,
    parent_session_id: str,
    profile_id: str,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    effort: Optional[str] = None,
    inherited_effort: Any = _EFFORT_UNSET,
    prompt: str,
    label: Optional[str] = None,
    tool_use_id: Optional[str] = None,
    background: bool = False,
    context_session_id: Optional[str] = None,
    launch_tool_use_id: Optional[str] = None,
    system_prompt_append: Optional[str] = None,
    tool_allowlist: Optional[List[str]] = None,
    tool_denylist: Optional[List[str]] = None,
    agent_name: Optional[str] = None,
    roster_name: Optional[str] = None,
    worker_session_id: Optional[str] = None,
    first_frame_seconds: Optional[float] = None,
    max_wall_seconds: Optional[float] = None,
    suppress_caller_wake: Optional[Callable[[str, Optional[str], int], bool]] = None,
    model_note: Optional[str] = None,
) -> WorkerOutcome:
    """Run ``prompt`` in a fresh session bound to ``provider_id``/``model_id``.

    Binding: workers inherit the direct caller's project, not its transcript.
    Coding workers receive a host-provisioned worktree from the caller's exact
    committed HEAD; research/non-Git workers retain normal cwd inheritance.
    Each coding session is bound to its own branch/path before its first turn.
    All workers carry ``parent_session_id`` plus an explicit ``provider_id``. The turn is
    submitted with ``overrides={"provider": ..., "model": ...}`` so both the row
    binding and the per-turn override agree.

    The worker is registered in the parent's task registry with ``kind="agent"``
    for the duration. That is load-bearing, not cosmetic: tool handlers run as
    cancellable children of the parent's pooled adapter, and pool eviction is
    gated on ``has_active_background_work``, which counts ``kind=="agent"``
    rows — without it the parent's adapter can be evicted mid-flight and the
    worker is stranded.

    A WORKING worker has no wall-clock ceiling: the wait blocks until it goes
    idle, its provider's circuit breaker trips, or the supervisor tears it
    down. Two bounded exceptions cover states that are not "working" at all
    (see :func:`_await_worker_idle`; ``None`` takes the configured default from
    :func:`_worker_deadlines`):

    * ``first_frame_seconds`` — a worker that has streamed NOTHING by then is
      failed. No progress frame does not prove the provider request never began;
    * ``max_wall_seconds`` — a hard cap the SYNCHRONOUS lane passes, because a
      sync dispatch blocks the parent's turn for as long as it waits. A worker
      whose work an unattended source started (automation, wake-up, connector)
      on the Claude subscription gets the loop guard's per-worker limit instead
      when the lane sets none; a worker a person's turn started never does.

    Every failure path returns a :class:`WorkerOutcome` with ``ok=False``,
    ``frames_forwarded`` (0 = no parent progress) and ``killed`` (a teardown,
    never retried). :mod:`app.core.task_dispatch` decides retry safety.

    ``suppress_caller_wake`` lets a RETRYING caller keep a doomed attempt off
    the parent's wake lane: it is handed ``(status, preview, frames_forwarded)``
    in the ``finally`` and returns True to skip the caller wake and terminal
    chip. The card, registry row and report are unaffected. See
    :func:`app.core.task_dispatch._dispatch_worker_with_retries`.

    Progress is forwarded best-effort to the PARENT's event feed by
    :class:`app.core.provider_agent_feed.WorkerFeed`. ``agent_name`` is the
    agent's DISPLAY NAME for the card and registry row, never a join key.

    OWNERSHIP vs ROUTING. ``parent_session_id`` is the ROUTING parent (the root
    for nested dispatch): card, approvals and worker budget. Notification
    bookkeeping (terminal chip, scheduler wake, task-notification steer) is
    armed on the OWNER, ``context_session_id``, the session that actually
    called Task. ``_live_worker_count_for_root`` counts nested workers wherever
    they registered.
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

    from app.core import api_user_scope, provider_agent_supervisor, turn_manager
    from app.core.provider_agent_feed import WorkerFeed, emit_shielded
    from app.core.task_registry import task_registry
    from app.db import database
    from app.db.async_database import run_sync

    # Task supplies the caller's provider and the target's configured model on
    # that provider. ``provider_id=None`` is an explicit native-Claude binding,
    # not permission to fall back to the target profile's top-level provider.
    # (Pure computation — hoisted above the offloaded prologue below so the
    # worker's identity is known to both the closure and this coroutine.)
    worker_session_id = worker_session_id or str(uuid.uuid4())
    title = (label or "").strip() or f"Task worker · {model_id or profile_id}"
    model_tag = model_id or "inherit"
    if model_note:
        # A model the user asked for on this dispatch (#1830), named as such.
        model_tag = f"{model_tag} ({model_note})"
    # The session that actually dispatched this worker — the one whose turn is
    # waiting on it and whose notification lanes must be armed. Equals
    # ``parent_session_id`` at depth 1; differs for a nested dispatch, where
    # ``parent_session_id`` is the ROOT (card routing only).
    owner_session_id = context_session_id or parent_session_id
    if owner_session_id:
        from app.db import turns as turns_db
        def proactive_caller():
            turn = turns_db.get_live_turn(owner_session_id) or turns_db.get_latest_turn(owner_session_id) or {}
            return ((turn.get("payload") or {}).get("_assistant_proactive") is not None
                    or (turn.get("overrides") or {}).get("_assistant_proactive") is not None)
        if await run_sync(proactive_caller):
            return WorkerOutcome(ok=False, session_id="", error="Background observation cannot delegate execution.")
    nested = bool(owner_session_id) and owner_session_id != parent_session_id
    from app.core import loop_guard

    def _limits() -> Tuple[Optional[str], Tuple[float, float, bool]]:
        # What started this chain of work, read from the caller's own turn (the
        # one calling Task). Stamped on the worker's turn so the loop guard's
        # budget, and any worker it dispatches, see the same origin.
        origin = loop_guard.session_origin(owner_session_id)
        return origin, _worker_deadlines(
            background, first_frame_seconds, max_wall_seconds, provider_id, origin
        )

    origin, (first_frame_seconds, max_wall_seconds, loop_guarded) = (
        await asyncio.to_thread(_limits)
    )

    from app.core.worker_checkout import prepare_worker_checkout
    worker_checkout = None
    # The caller's context-window choice, fitted to this worker's model. Set by
    # the prologue below; pinned on the worker row and its first turn.
    worker_window: Optional[int] = None

    def _prepare_worker_blocking() -> Tuple[
        Optional[SessionRow], Optional[str], Optional[str], Optional[str],
        Optional[str], str, Optional[BaseException], Optional[str],
        Optional[str],
    ]:
        """Prepare checkout and durable session binding in one thread hop.

        Read the direct caller, provision Git isolation when required, then
        insert the worker session. Checkout/INSERT failures are returned so the
        caller can refuse launch before registration or provider submission.
        Nothing here needs a running loop.
        """
        nonlocal worker_checkout, worker_window
        # Inheritance (project/worktree/cwd) reads the DIRECT caller's session;
        # ``parent_session_id`` is the ROUTING parent — for nested Task-tool
        # dispatch it is the ROOT session, whose UI renders the card and answers
        # approvals. They coincide for depth-1.
        parent_row: Optional[SessionRow] = None
        try:
            parent_row = database.get_session(context_session_id or parent_session_id)
        except Exception:  # noqa: BLE001 — inheritance is best-effort
            logger.debug("ProviderAgent: parent session read failed", exc_info=True)
            parent_row = None

        project_id = (parent_row or {}).get("project_id")
        # The worker runs as whoever owns its caller: an API key's worker is
        # that key's session (its tool surface, credentials and memory), held
        # to the key's assigned agents — never an owner turn.
        api_user_id = api_user_scope.session_api_user_id(parent_row)
        # Task snapshots this before admission, including a deliberate None.
        # Direct callers retain the same inheritance without consulting globals.
        selected_effort = (
            effort if isinstance(effort, str) and effort in EFFORT_LEVELS
            else _caller_effort(parent_row) if inherited_effort is _EFFORT_UNSET
            else inherited_effort
        )
        worker_window = worker_context_window(
            _caller_context_window(parent_row), provider_id, model_id,
        )
        project_v2_id = (parent_row or {}).get("project_v2_id")
        branch = (parent_row or {}).get("branch")
        target_instance_id = (parent_row or {}).get("instance_id")
        worktree_id = (parent_row or {}).get("worktree_id")
        worktree_branch = (parent_row or {}).get("worktree_branch")
        worktree_path = (parent_row or {}).get("worktree_path")

        # The provider's NAME, not its row id: the registry summary and the
        # card's settle frame are user-facing, and a raw `model_providers` UUID
        # tells nobody anything.
        provider_tag = _provider_display_name(provider_id)

        create_error: Optional[BaseException] = None
        refusal = api_user_scope.worker_refusal(api_user_id, profile_id)
        if not refusal:
            # The owner's daily spending limit for the assistant and its jobs.
            from app.core import assistant_limits
            refusal = assistant_limits.start_refusal(context_session_id or parent_session_id)
        if refusal:
            return (
                parent_row, project_id, worktree_id, worktree_branch,
                worktree_path, provider_tag, _WorkerRefused(refusal),
                selected_effort, api_user_id,
            )
        try:
            worker_checkout = prepare_worker_checkout(
                parent_row, worker_session_id, profile_id, tool_allowlist,
            )
            if worker_checkout:
                project_v2_id = worker_checkout.project_v2_id
                branch = worker_checkout.branch
                worktree_id = None  # v2 checkout, not the caller's legacy row
                worktree_branch = branch
                worktree_path = worker_checkout.path
            database.create_session(**worker_session_fields(
                worker_session_id=worker_session_id,
                profile_id=profile_id,
                api_user_id=api_user_id,
                project_id=project_id,
                project_v2_id=project_v2_id,
                target_instance_id=target_instance_id,
                title=title,
                parent_session_id=parent_session_id,
                worktree_id=worktree_id,
                worktree_branch=worktree_branch,
                worktree_path=worktree_path,
                provider_id=provider_id,
                model_id=model_id,
                effort=selected_effort,
                context_window=worker_window,
            ))
            from app.core.assistant_service import home_for_session, link_job
            home = home_for_session(context_session_id or parent_session_id)
            if home:
                link_job(home, worker_session_id)
            if branch:
                database.set_session_branch(
                    worker_session_id, branch, project_v2_id
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ProviderAgent: worker session creation failed")
            create_error = exc
            if worker_checkout:
                # Provisioned, but no session will ever run in it.
                from app.core.worker_checkout import discard_unused_worker_checkout
                discard_unused_worker_checkout(worker_checkout)
        return (
            parent_row, project_id, worktree_id, worktree_branch,
            worktree_path, provider_tag, create_error, selected_effort,
            api_user_id,
        )

    (
        parent_row,
        project_id,
        worktree_id,
        worktree_branch,
        worktree_path,
        provider_tag,
        create_error,
        selected_effort,
        api_user_id,
    ) = await run_sync(_prepare_worker_blocking)
    if isinstance(create_error, _WorkerRefused):
        return WorkerOutcome(ok=False, session_id="", error=str(create_error))
    if create_error is not None:
        return WorkerOutcome(
            ok=False,
            session_id=worker_session_id,
            error=f"Could not create the worker session: {create_error}",
        )

    # Mark BEFORE the turn starts, so the worker's first turn is already
    # recognized as a worker's (depth walk, AskUserQuestion withheld).
    register_worker_session(worker_session_id)

    # --- approval routing (ONE contiguous region) --------------------------
    # The worker's tool-approval prompts surface on the PARENT's UI, tagged with
    # this worker's label. Registered HERE, before submit, because
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
    # --- end approval routing ----------------------------------------------

    # Permission inheritance: the worker runs at the CALLER's effective
    # permission level, so a bypassPermissions parent (e.g. a voice call on a
    # bypass profile) never has its workers stall on approval prompts.
    overrides = worker_turn_overrides(
        provider_id=provider_id,
        model_id=model_id,
        effort=selected_effort,
        cwd=worker_checkout.path if worker_checkout else _parent_cwd(parent_row),
        permission_mode=_caller_permission_mode(parent_row),
        system_prompt_append=system_prompt_append,
        checkout=worker_checkout,
        tool_allowlist=tool_allowlist,
        tool_denylist=tool_denylist,
        voice_row=parent_row if (parent_row or {}).get("mode") == "voice" else None,
        context_window=worker_window,
    )

    # ONE join key for the whole surface: the worker's session id is the
    # registry task id, the registry's ``tool_use_id`` AND the ``task_id`` on
    # every forwarded subagent.* event. The frontend joins the ``/live`` spinner
    # overlay and the terminal task-settle to the card through ``tool_use_id``
    # (``SubagentMessage.svelte`` and the v2 ``reducer.ts``), so a registry
    # entry with no tool_use_id could never settle its card.
    launch_key = tool_use_id or worker_session_id
    task_registry.start_task(
        parent_session_id,
        worker_session_id,
        "agent",
        title,
        # ``summary`` answers "what is this task", not "which model is it on":
        # a parent listing its own workers learns nothing from a provider/model
        # label on every row. The label keeps its own field.
        summary=title,
        model=f"{provider_tag} · {model_tag}",
        tool_use_id=launch_key,
        agent=agent_name,
        name=roster_name,
    )
    if nested:
        # A SECOND row, on the session that dispatched it. The root's row above
        # is what the human's UI renders; this one is what makes the owner's
        # own TaskList honest, what keeps the owner's dispatch from settling
        # while its child runs (the bounded background-settle loop in
        # ``dispatch_worker``), and what ``is_own_background_task`` reads when
        # the terminal lands.
        task_registry.start_task(
            owner_session_id,
            worker_session_id,
            "agent",
            title,
            summary=title,
            model=f"{provider_tag} · {model_tag}",
            tool_use_id=launch_key,
            agent=agent_name,
            name=roster_name,
        )

    # Ownership of this worker's terminal, for the parent's notification lanes.
    # ``is_own_background_task`` — the gate on BOTH the terminal chip
    # (``complete_task``) and the scheduler wake (``on_task_terminal``) — admits a
    # task only when it is an SDK-backgrounded shell or its registry record's
    # ``tool_use_id`` was recorded as a background launch. A Task-dispatched
    # worker is neither by default, so without this both lanes would drop it
    # and the caller would never be told its worker finished. Registering the
    # SAME key the registry row carries (``launch_key``) makes the gate pass.
    #
    # BACKGROUND DISPATCHES ONLY. A SYNCHRONOUS dispatch
    # hands its report back inline as the tool result, in the caller's own
    # turn — the caller is neither parked nor uninformed, so a terminal chip and
    # a wake would both be noise (and the wake would target a turn that is still
    # running). Only the background mode detaches the outcome from the call, and
    # only it needs the notification lanes. The ``finally`` below fires the wake
    # under the same condition, so the two halves cannot disagree.
    #
    # Park-gate safety: ``_background_launches`` is
    # CUMULATIVE and is NOT pruned in ``complete_task``, but the ``_finalize``
    # gate counts only UNRESOLVED launches — a launch whose registry record has
    # reached a terminal status no longer arms it. This worker's ``finally``
    # ALWAYS completes the row, so the launch resolves and cannot re-park the
    # session forever. The teardown path additionally purges the id outright
    # (``provider_agent_supervisor._purge_park_gate_residue`` discards
    # ``{tool_use_id, task_id, worker_session_id}`` — a superset of this key).
    #
    # ARMED ON THE OWNER, not the routing parent. For a depth-1 dispatch these
    # are the same session. For a NESTED one, arming the root would make the
    # root's scheduler wake, task-notification steer and terminal chip fire for
    # a worker it never dispatched and cannot act on. The root fails
    # ``is_own_background_task`` for these, which is exactly the condition
    # ``turn_scheduler.on_task_terminal`` suppresses on, and the OWNER gets the
    # wake it is waiting for.
    if background:
        task_registry.mark_background_launch(owner_session_id, launch_key)

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

    # Turn-lifetime safety. The supervisor is the ONLY thing that knows
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
    #: Retrieval handle for a completed BACKGROUND worker's report, stamped on
    #: the registry row in ``finally`` so ``TaskGet`` can serve it to the woken
    #: parent. See ``_persist_worker_report``.
    report_path: Optional[str] = None
    report_chars: Optional[int] = None
    #: The registry ``summary`` for a CLEAN completion — the report's first
    #: line, so a listing says what came back rather than which model ran.
    completion_summary: Optional[str] = None
    #: Background work the worker left running; a checkout still in use by it
    #: is never reclaimed from under it.
    outstanding: List[Dict[str, Any]] = []
    worker_payload: Dict[str, Any] = {}
    if api_user_id:
        worker_payload["api_user_id"] = api_user_id
    if origin:
        worker_payload[loop_guard.ORIGIN_KEY] = origin
    try:
        # Emit the card BEFORE the turn is submitted so a submit failure still
        # renders as a failed worker rather than as silence.
        await feed.started()
        worker_turn_id = await turn_manager.submit_async(
            worker_session_id,
            WORKER_TURN_SOURCE,
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            worktree_id=worktree_id,
            overrides=overrides,
            # SessionRuntime runs the turn as the payload's principal; with
            # none it runs as the key that owns the worker session (walking
            # worker parents, ``api_user_scope.turn_api_user_id``), and only as
            # the owner when no key owns it. Stamped explicitly anyway, so the
            # worker never depends on that fallback.
            payload=worker_payload or None,
        )

        # Tail the worker's durable log from here on — its rows only start
        # existing once the turn is submitted.
        feed.start_following()

        # Deadlines measured from one start, so a park loop cannot reset them.
        # The first-frame one catches a worker that never starts; the hard one
        # is the synchronous lane's ceiling, or the loop guard's per-worker
        # limit (re-read as it runs). 0 = disabled.
        _clock = asyncio.get_event_loop().time()
        first_frame_deadline = (
            _clock + first_frame_seconds if first_frame_seconds > 0 else None
        )
        wall_cap = _WallCap(
            _clock, max_wall_seconds, provider_id=provider_id, origin=origin,
            loop_guarded=loop_guarded,
        )

        wait_verdict = await _await_worker_idle(
            worker_session_id,
            provider_id,
            feed=feed,
            first_frame_deadline=first_frame_deadline,
            wall_cap=wall_cap,
        )

        # An idle worker is not necessarily a finished one. Two shapes:
        #
        # 1. it PARKED on its own background work (``_worker_still_working``) —
        #    a park is not an ending, so wait, unbounded;
        # 2. its turn ENDED with background work still live. A worker has no
        #    wake seam it can rely on, so "tests are running, I'll report when
        #    they finish" is not describing a future and must not be booked as
        #    its report. Give the work a
        #    BOUNDED drain (``task_worker_background_settle_seconds``): if it
        #    settles, whatever turn the worker ends on is its real report; if it
        #    does not, the dispatch fails rather than reporting an interim note.
        #
        # Either way the loop re-enters ``_await_worker_idle``, so a wake turn
        # the settling work triggers is waited out and read as the final turn.
        announced_park = False
        announced_drain = False
        settle_deadline: Optional[float] = None
        settle_budget = _background_settle_seconds()
        outstanding: List[Dict[str, Any]] = []
        while not wait_verdict and not supervised.torn_down:
            await wall_cap.refresh(asyncio.get_event_loop().time())
            hard_deadline = wall_cap.deadline()
            if hard_deadline is not None and asyncio.get_event_loop().time() >= hard_deadline:
                wait_verdict = WAIT_DEADLINE
                break
            if await asyncio.to_thread(_worker_still_working, worker_session_id):
                if not announced_park:
                    announced_park = True
                    logger.info(
                        "ProviderAgent: worker %s is parked on its own "
                        "background work — waiting for it to finish rather "
                        "than settling the task", worker_session_id,
                    )
            else:
                outstanding = _active_worker_background_work(worker_session_id)
                if not outstanding:
                    break
                now = asyncio.get_event_loop().time()
                if settle_deadline is None:
                    settle_deadline = now + settle_budget
                if not announced_drain:
                    announced_drain = True
                    logger.info(
                        "ProviderAgent: worker %s ended its turn with %d "
                        "background task(s) still running — draining for up to "
                        "%.0fs before writing its report",
                        worker_session_id, len(outstanding), settle_budget,
                    )
                if now >= settle_deadline:
                    break
            pause = PARK_RECHECK_SECONDS
            if hard_deadline is not None:
                pause = min(pause, max(0, hard_deadline - asyncio.get_event_loop().time()))
            await asyncio.sleep(pause)
            wait_verdict = await _await_worker_idle(
                worker_session_id,
                provider_id,
                feed=feed,
                # A worker that reached this loop has streamed frames, so the
                # first-frame watchdog is spent; the hard cap is not.
                first_frame_deadline=None,
                wall_cap=wall_cap,
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
                frames_forwarded=feed.frames_forwarded, killed=True,
            )

        if wait_verdict in (WAIT_NO_FIRST_FRAME, WAIT_DEADLINE):
            # The watchdog, not the provider, is ending this. Stop the turn (the
            # worker may be wedged on a connection that will never open) and let
            # the ordinary failure path do the reporting — the parent needs the
            # no-output summary now.
            error = (
                _no_first_frame_error(worker_session_id, first_frame_seconds)
                if wait_verdict == WAIT_NO_FIRST_FRAME
                else wall_cap.error(worker_session_id)
            )
            preview = error
            status = "error"
            logger.warning(
                "ProviderAgent: worker %s abandoned by the %s watchdog "
                "(%d frame(s) forwarded, %d provider progress row(s))",
                worker_session_id, wait_verdict, feed.frames_forwarded,
                feed.provider_progress,
            )
            await _stop_wedged_worker(worker_session_id)
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error=error,
                frames_forwarded=feed.frames_forwarded,
            )

        if wait_verdict == WAIT_RATE_LIMITED:
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
                frames_forwarded=feed.frames_forwarded, killed=True,
            )

        outcome = final_turn_outcome(worker_session_id)
        usage_seen = outcome["usage"] if isinstance(outcome["usage"], dict) else {}
        if background and not nested and worker_turn_id:
            # A usage limit stopped the worker and its provider resumes by
            # itself after the reset (#1831): the worker stays on the parent's
            # running tasks as paused and the parent is not woken now. Its
            # resume reports through the worker-continuation path instead.
            paused = await _pause_for_usage_limit(
                worker_session_id, worker_turn_id, owner_session_id,
            )
            if paused is not None:
                status = "paused"
                preview = paused
                return WorkerOutcome(
                    ok=True,
                    session_id=worker_session_id,
                    report=paused,
                    usage=outcome["usage"],
                    frames_forwarded=feed.frames_forwarded,
                )
        if outcome["error"]:
            preview = str(outcome["error"])
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error=str(outcome["error"]),
                usage=outcome["usage"],
                frames_forwarded=feed.frames_forwarded,
            )
        if outcome["interrupted"]:
            preview = "The worker session was interrupted before it finished."
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                error="The worker session was interrupted before it finished.",
                usage=outcome["usage"],
                frames_forwarded=feed.frames_forwarded,
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
                frames_forwarded=feed.frames_forwarded,
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
                frames_forwarded=feed.frames_forwarded,
            )

        report, truncated = bound_report(final_text)

        # The drain above ran its course and this worker's own background work
        # is STILL live. Its final message is an interim note (and any Monitor
        # id it hands back resolves only in its own session), so the dispatch
        # fails; the abandoned Monitor rows are retired on the way out because
        # nothing else can reach them once this session is gone.
        outstanding = _active_worker_background_work(worker_session_id)
        if outstanding:
            await _force_settle_background_work(worker_session_id, outstanding)
            error = _outstanding_work_error(worker_session_id, outstanding)
            preview = error
            status = "error"
            if background:
                # The interim text is still the best thing the worker produced;
                # persist it so a parent that wants to see how far it got can,
                # even though the outcome is a failure.
                report_path, report_chars = _persist_worker_report(
                    parent_row, worker_session_id, report
                )
            return WorkerOutcome(
                ok=False,
                session_id=worker_session_id,
                report=report,
                truncated=truncated,
                error=error,
                usage=outcome["usage"],
                frames_forwarded=feed.frames_forwarded,
            )

        status = "completed"
        preview = report
        completion_summary = _completion_summary(report)
        if background:
            # A SYNC dispatch still returns its report inline to a live caller
            # frame, and ``task_dispatch`` writes the file from there. A
            # background dispatch has no caller frame left — this is the only
            # place its report can be persisted, and without it the woken
            # parent has no way to read what its child produced.
            report_path, report_chars = _persist_worker_report(
                parent_row, worker_session_id, report
            )
        return WorkerOutcome(
            ok=True,
            session_id=worker_session_id,
            report=report,
            truncated=truncated,
            usage=outcome["usage"],
            frames_forwarded=feed.frames_forwarded,
        )
    except asyncio.CancelledError:
        # The parent's pooled adapter is going away (eviction / interrupt). Stop
        # the worker rather than leaving it running against a parent that is gone.
        status = "killed"
        cancelled = True
        preview = "The parent turn ended before the worker finished."
        try:
            await turn_manager.interrupt_async(worker_session_id, "stop")
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
            # Same count the ``finally``'s retry predicate reads, so the caller
            # and the wake-suppression decision cannot disagree about whether
            # this worker ever started.
            frames_forwarded=feed.frames_forwarded,
        )
    finally:
        # Turn-end teardown may have already settled this worker (card, registry
        # row, park-gate purge and process kill). Repeating any of that would
        # re-emit a terminal frame and re-complete a row that is already
        # terminal, so the latch wins and this block only unregisters.
        if supervised.torn_down:
            if nested:
                # The supervisor settles the ROOT's row and purges the root's
                # park-gate residue; it knows nothing about the owner's copy.
                # Settle it here or the dispatching worker keeps an unresolved
                # launch and a "running" row for a worker that is already dead.
                _settle_owner_row(
                    owner_session_id, worker_session_id, launch_key, "killed",
                    _torn_down_error(
                        supervised.torn_down_reason, worker_session_id
                    ),
                )
            if provider_agent_supervisor.get_worker(parent_session_id, worker_session_id) is supervised:
                provider_agent_supervisor.unregister_worker(
                    parent_session_id, worker_session_id
                )
        elif status == "paused":
            # Paused by a usage limit with a resume scheduled (#1831). Nothing
            # terminal happens: the card and the registry row stay running,
            # marked paused, the worker row keeps its status and checkout, and
            # the parent is not woken. The resume continues this worker and
            # its report settles all of it.
            try:
                await feed.stop_following(final_drain=True)
            except BaseException:  # noqa: BLE001 — teardown must not mask the outcome
                logger.debug("ProviderAgent: feed teardown failed", exc_info=True)
            try:
                from app.core.limit_resume import PAUSED_SUMMARY

                task_registry.update_task(
                    parent_session_id, worker_session_id, summary=PAUSED_SUMMARY,
                )
            except Exception:  # noqa: BLE001
                logger.debug("ProviderAgent: paused summary write failed", exc_info=True)
            provider_agent_supervisor.record_usage(
                parent_session_id, worker_session_id, usage_seen
            )
            if provider_agent_supervisor.get_worker(parent_session_id, worker_session_id) is supervised:
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
            ) or completion_summary
            # A caller that is about to RE-DISPATCH this brief takes the chip
            # and the wake off this attempt: the parent should hear once, about
            # the attempt that settles it, not once per transport hiccup the
            # harness is already recovering from. The row itself is written
            # either way, so the attempt stays visible in the task list.
            retry_pending = False
            if suppress_caller_wake is not None:
                try:
                    retry_pending = bool(
                        suppress_caller_wake(
                            status, preview, feed.frames_forwarded
                        )
                    )
                except Exception:  # noqa: BLE001 — a predicate must not lose a wake
                    logger.debug(
                        "ProviderAgent: retry predicate raised for worker %s",
                        worker_session_id, exc_info=True,
                    )
                    retry_pending = False
            try:
                task_registry.complete_task(
                    parent_session_id,
                    worker_session_id,
                    status,
                    summary=terminal_summary,
                    report_path=report_path,
                    report_chars=report_chars,
                    notify=not retry_pending,
                )
            except Exception:  # noqa: BLE001
                logger.exception("ProviderAgent: registry completion failed")
            if nested:
                _settle_owner_row(
                    owner_session_id, worker_session_id, launch_key, status,
                    terminal_summary,
                    report_path=report_path,
                    report_chars=report_chars,
                    notify=not retry_pending,
                )
            # The worker session's OWN row records the verdict. Only the
            # ``"error"`` status — the worker ran and did not deliver — is
            # stamped: ``"killed"`` is a teardown (the supervisor's own path,
            # which never reaches this branch) and ``"completed"`` leaves the
            # row exactly as it was. See WORKER_FAILED_STATUS for why the value
            # is "failed" and not "completed". Best-effort: a status write must
            # never change what the dispatch reports to the caller.
            if status == "error":
                # SHIELDED, like the terminal frames above: this ``finally`` can
                # run while the handler is already cancelling. A bare ``await``
                # would be cancelled at its first suspension and silently lose
                # the verdict; the shield promotes it to an independent task
                # that finishes either way. Same swallow-our-own-cancellation
                # rule as ``emit_shielded`` — the caller owns cancellation.
                _status_write = asyncio.ensure_future(
                    run_sync(
                        database.update_session,
                        worker_session_id,
                        status=WORKER_FAILED_STATUS,
                    )
                )
                try:
                    await asyncio.shield(_status_write)
                except asyncio.CancelledError:
                    _status_write.add_done_callback(_log_status_write_failure)
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
            #
            # The wake goes to the OWNER — the session that dispatched this
            # worker and is waiting on it — not to the routing parent. They are
            # the same session for a depth-1 dispatch; for a nested one the
            # routing parent is the ROOT, which never launched this worker and
            # whose ``is_own_background_task`` gate (correctly) suppresses
            # the frame anyway.
            if background and not retry_pending:
                await emit_shielded(_notify_caller_terminal(
                    owner_session_id,
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
            if provider_agent_supervisor.get_worker(parent_session_id, worker_session_id) is supervised:
                provider_agent_supervisor.unregister_worker(
                    parent_session_id, worker_session_id
                )
        # A dedicated checkout the worker finished without touching holds
        # nothing to review: reclaim it rather than leak a worktree per
        # research-only worker. Anything committed or changed is retained.
        # A resumed worker re-materializes it from the recorded tip.
        if worker_checkout is not None and not outstanding and status != "paused":
            from app.core.worker_checkout import reclaim_untouched_worker_checkout
            await emit_shielded(asyncio.to_thread(
                reclaim_untouched_worker_checkout, worker_session_id, worker_checkout,
            ))
