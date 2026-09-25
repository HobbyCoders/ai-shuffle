"""
Persistence helpers for query turn results.

Extracted from query_engine.py to isolate DB write logic.
"""

import asyncio
import collections
import collections.abc
import logging
import json
import uuid
from typing import Any, Deque, Optional, Callable, Set

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


_STDERR_CAPTURE_MAXLEN = 50

# Bound on the live SDK get_context_usage() control call. This is the SINGLE
# source of truth for the timeout: both the turn-end capture below and the GET
# context-usage endpoint drive the call through ``shielded_get_context_usage``.
# A wedged CLI must never hang or delay a request or turn completion.
SDK_CONTEXT_USAGE_TIMEOUT_SECONDS = 5.0

# Strong references to detached capture tasks. asyncio only keeps a weak
# reference to a bare create_task() result, so without this the task could be
# garbage-collected mid-flight; the done-callback discards it on completion.
_context_usage_capture_tasks: "Set[asyncio.Task]" = set()


async def shielded_get_context_usage(adapter: Any) -> Optional[dict]:
    """Run ``adapter.get_context_usage()`` bounded, without cancelling it midflight.

    This is the SINGLE copy of the pattern shared by the turn-end capture and
    the GET context-usage endpoint. The control call runs on a DETACHED task
    guarded by a SHIELD: on timeout we must NOT cancel the in-flight control
    request — cancelling it mid-flight can leave its entry in the delegated
    loop's pending-control map, which can wedge later control calls. Shielding
    lets the ``wait_for`` time out while the underlying call keeps running to
    completion in the background.

    Returns the raw usage dict on success, or ``None`` when the SDK returns
    ``None`` (control call unsupported). Raises ``asyncio.TimeoutError`` when the
    call does not complete within the bound, so callers can distinguish a
    timeout from an unsupported call.
    """
    usage_task = asyncio.create_task(adapter.get_context_usage())
    # Retrieve any exception on the detached task so a failure after we've timed
    # out doesn't surface as "Task exception was never retrieved".
    usage_task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.wait_for(
        asyncio.shield(usage_task),
        timeout=SDK_CONTEXT_USAGE_TIMEOUT_SECONDS,
    )


async def capture_context_usage(
    session_id: str,
    log_prefix: str = "",
) -> None:
    """Capture and persist the SDK context-usage breakdown for a finished turn.

    Self-contained and best-effort: this peeks the session pool itself (so
    callers need only pass the session id), runs ``adapter.get_context_usage()``
    via ``shielded_get_context_usage``, and writes the payload to
    ``sessions.context_usage_json``. Intended to run DETACHED — see
    ``schedule_context_usage_capture`` — so it never delays a turn's terminal
    event. Any failure/timeout is logged at debug and swallowed: capturing must
    NEVER raise or delay the turn.

    Known gap: ``execute_query`` in query_engine.py holds no live adapter, so a
    turn that completes only through that path is not captured here; the GET
    endpoint's live path still serves such a session while it stays pooled.
    """
    # Lazy import to mirror the endpoint and avoid an import cycle with the pool.
    from app.core.session_pool import get_session_pool
    try:
        state = get_session_pool().peek(session_id)
        if state is None or not state.is_connected or state.adapter is None:
            return
        result = await shielded_get_context_usage(state.adapter)
        if result is None:
            logger.debug(
                f"{log_prefix}context-usage capture unavailable for {session_id}"
            )
            return
        await run_sync(
            database.set_session_context_usage,
            session_id=session_id,
            data=result,
        )
        logger.debug(f"{log_prefix}Captured context-usage for {session_id}")
    except Exception as e:
        logger.debug(
            f"{log_prefix}context-usage capture failed for {session_id}: {e}"
        )


def schedule_context_usage_capture(session_id: str, log_prefix: str = "") -> None:
    """Fire-and-forget the turn-end context-usage capture on a detached task.

    ``capture_context_usage`` self-guards (peeks the pool, checks the
    connection), so turn-end call sites just hand off the session id and move on
    — the capture runs detached and never delays the terminal event. The task is
    held in a module-level set with a done-callback that discards it, giving the
    event loop a strong reference so the detached task isn't GC'd mid-flight.
    """
    task = asyncio.create_task(
        capture_context_usage(session_id, log_prefix=log_prefix)
    )
    _context_usage_capture_tasks.add(task)
    task.add_done_callback(_context_usage_capture_tasks.discard)


def _wrap_stderr_capture(
    original_stderr: Optional[Callable],
) -> tuple[Callable[[str], None], "Deque[str]"]:
    """Wrap a stderr callback to also capture lines into a bounded ring buffer.

    Returns (capturing_callback, captured_lines) so callers can inspect
    captured stderr output for error reporting.

    ``captured_lines`` is a ``collections.deque(maxlen=50)`` rather than an
    unbounded list: the buffer is stored on ``SessionState`` and shared across
    every turn of a long-lived pooled subprocess, so an unbounded list leaked
    for the subprocess's whole lifetime. Only the lines nearest a crash are
    diagnostic, so the ring keeps the newest 50 and drops the oldest.
    """
    captured: "Deque[str]" = collections.deque(maxlen=_STDERR_CAPTURE_MAXLEN)

    def capturing_stderr(line: str):
        captured.append(line)
        if original_stderr:
            original_stderr(line)

    return capturing_stderr, captured


def _extract_connection_error(
    exc: Exception, cli_stderr_lines: "Optional[collections.abc.Iterable[str]]"
) -> str:
    """Extract the most useful error message from a connection failure.

    Priority order:
    0. A missing ``claude`` CLI: its setup message, verbatim
    1. Captured CLI stderr lines (real subprocess output)
    2. ProcessError.stderr attribute (if not a placeholder)
    3. Exception message as-is
    """
    from fluid.engine.delegated import ClaudeCliMissingError

    if isinstance(exc, ClaudeCliMissingError):
        return str(exc)

    # 1. Prefer captured stderr lines — these are the real subprocess output
    if cli_stderr_lines:
        stderr_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
        if stderr_msg:
            return stderr_msg

    # 2. Check stderr retained by a failed subprocess invocation
    from subprocess import CalledProcessError
    if isinstance(exc, CalledProcessError) and exc.stderr:
        # Skip the SDK's useless placeholder
        if exc.stderr != "Check stderr output for details":
            return f"Connection failed (exit code {exc.returncode}): {exc.stderr}"
        # Placeholder stderr — build message from exit code alone
        if exc.returncode is not None:
            return f"Connection failed: CLI process exited with code {exc.returncode}"

    # 3. Fall back to raw exception message
    return f"Connection failed: {exc}"


def log_captured_cli_stderr(
    session_id: str,
    cli_stderr_lines: "Optional[collections.abc.Iterable[str]]",
    context: str = "",
) -> None:
    """Record captured CLI subprocess stderr at ERROR after a mid-stream crash.

    The connect-time handlers already surface captured stderr through
    ``_extract_connection_error`` (it lands in the user-facing error message and
    an ERROR log line). But a subprocess that dies MID-STREAM — the common case
    behind a background task that silently vanishes — raises from
    ``receive_events()``, where the connect-scoped ``cli_stderr_lines`` is out
    of scope. Persisting the capture list on ``SessionState`` lets the
    streaming exception handlers call this so an exit-1 records WHY.

    Best-effort and never raises — a logging helper must not mask the original
    error it is trying to explain.
    """
    try:
        if not cli_stderr_lines:
            return
        # A long-lived pooled subprocess accumulates stderr across turns; the
        # lines nearest the crash are the most diagnostic, so keep the tail.
        # ``cli_stderr_lines`` is a bounded ``deque`` (see _wrap_stderr_capture)
        # which does NOT support slice indexing, so materialise to a list first.
        recent = list(cli_stderr_lines)[-40:]
        tail = [ln.strip() for ln in recent if ln and ln.strip()]
        if not tail:
            return
        ctx = f" [{context}]" if context else ""
        logger.error(
            "[Captured CLI stderr]%s session=%s: %s",
            ctx, session_id, " | ".join(tail),
        )
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        pass


def usage_settlement_key(session_id: str, turn: dict) -> str:
    """Identity includes the lease attempt: a requeued turn is new paid work."""
    return json.dumps(
        [session_id, turn["id"], turn.get("run_id"), str(turn.get("started_at"))],
        separators=(",", ":"),
    )


async def capture_usage_identity(session_id: str) -> str:
    """Capture before execution, while the owning turn is still live."""
    from app.db import turns as turns_db

    turn = await run_sync(turns_db.get_live_turn, session_id)
    if turn:
        return usage_settlement_key(session_id, turn)
    # Legacy non-queued entry points still need a distinct producer identity.
    return json.dumps([session_id, "legacy", str(uuid.uuid4())])


async def _persist_turn_results(
    session_id: str,
    sdk_session_id: Optional[str],
    metadata: dict,
    profile_id: Optional[str],
    interrupted: bool = False,
    log_prefix: str = "",
) -> None:
    """Persist a completed turn's session-row metadata and settle its usage.

    The transcript itself is written by the session store, not here.
    """
    # 1. Update session metadata (sdk_session_id, cost, tokens, turns)
    #
    # The `sessions` row is canonical session state that the transcript store
    # does NOT replace: it holds the cost/token/turn aggregates AND,
    # critically, the `sdk_session_id` that links an app session to its
    # transcript. The READ path (GET /{session_id}) looks the session_store
    # entries up by `sessions.sdk_session_id`; if this write is skipped the
    # column stays NULL and a session that streamed and stored its turn
    # correctly reads back EMPTY on reload.
    #
    # Context-window columns must store the LAST API call's per-call usage,
    # not ResultMessage aggregates. SDK's ResultMessage.usage sums every API
    # hop in the agent loop (one user turn can make many calls via tools), so
    # `metadata["tokens_in"]` and cache fields there are cumulative per turn
    # and inflate the context bar on reload.
    #
    # Each AssistantMessage in the query loop writes the per-call values into
    # metadata["last_call_*"], overwriting so the final value is the last API
    # call's usage == current context window size. We fall back to the
    # ResultMessage aggregate only when the per-call capture is missing
    # (pre-fix metadata, non-streaming paths without AssistantMessages, etc.)
    # so the update is still safe.
    try:
        if sdk_session_id:
            last_input_tokens = metadata.get(
                "last_call_tokens_in",
                metadata.get("tokens_in", 0),
            )
            last_cache_creation = metadata.get(
                "last_call_cache_creation_tokens",
                metadata.get("cache_creation_tokens", 0),
            )
            last_cache_read = metadata.get(
                "last_call_cache_read_tokens",
                metadata.get("cache_read_tokens", 0),
            )
            await run_sync(
                database.update_session,
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                # Persist per-turn context-window values so /live can render
                # the context bar on session reload without scanning JSONL.
                last_input_tokens=last_input_tokens,
                cache_creation_tokens=last_cache_creation,
                cache_read_tokens=last_cache_read,
            )
            logger.debug(f"{log_prefix}Updated session {session_id}, sdk_session_id={sdk_session_id}")
    except Exception as e:
        logger.error(f"{log_prefix}Failed to update session {session_id} in database: {e}")

    # Counters and ledger must share one durable claim, including errors.
    settlement_key = metadata.get("usage_settlement_key")
    if settlement_key:
        try:
            booking = await _turn_end_booking(
                session_id, settlement_key, metadata, interrupted,
            )
            if booking is None:
                logger.info(
                    "%sNo usage evidence for %s at turn end; settlement claim "
                    "left open", log_prefix, session_id,
                )
            else:
                await run_sync(
                    database.settle_turn_usage,
                    session_id=session_id, settlement_key=settlement_key,
                    profile_id=profile_id, **booking,
                )
        except Exception:
            logger.exception("%sUsage settlement failed for %s", log_prefix, session_id)
    else:
        logger.warning("%sUsage not settled: missing producer identity for %s",
                       log_prefix, session_id)


def settlement_turn_id(settlement_key: str) -> Optional[str]:
    """The ``turns`` row a settlement key names, or ``None`` for a legacy key."""
    try:
        parts = json.loads(settlement_key)
    except (TypeError, ValueError):
        return None
    if isinstance(parts, list) and len(parts) == 4 and parts[1] != "legacy":
        return parts[1]
    return None


async def _turn_end_booking(
    session_id: str, settlement_key: str, metadata: dict, interrupted: bool,
) -> Optional[dict]:
    """What the turn-end finalizer books under ``settlement_key``.

    A terminal result (``num_turns`` is set only from one) carries the turn's
    own totals. Without one, the pump was cancelled or crashed before the
    provider's terminal arrived, and the metadata holds no usage at all.
    Booking those zeros would win the claim and lock out the kill path's
    event-log recovery for the same attempt (#1407), so the finalizer recovers
    from the same durable per-call ``turn.usage`` records instead, when they
    are native per-call records. Otherwise nothing is booked and the claim
    stays open for a later recovery path.
    """
    if "num_turns" in metadata:
        return {
            "model": metadata.get("model"),
            "tokens_in": metadata.get("tokens_in") or 0,
            "tokens_out": metadata.get("tokens_out") or 0,
            "cache_read_tokens": metadata.get("cache_read_tokens") or 0,
            "cache_creation_tokens": metadata.get("cache_creation_tokens") or 0,
            "cost_usd": metadata.get("total_cost_usd") or 0,
            "duration_ms": metadata.get("duration_ms") or 0,
            "model_calls": metadata.get("num_turns") or 0,
            "turn_increment": 0 if interrupted or metadata.get("error")
            or metadata.get("is_error") else 1,
        }
    turn_id = settlement_turn_id(settlement_key)
    if turn_id is None:
        # A legacy key has no turn to scope the event log to; summing the
        # whole session's history would book earlier turns again.
        return None
    totals = await run_sync(_partial_usage_from_events, session_id, turn_id)
    if totals["unindexed_records"]:
        # Unindexed records (a delegated session from before fluid indexed
        # its calls) are coalesced gauges, several per call; summing them is
        # the kill path's best-effort fallback, not a finalizer's booking.
        # Leave the claim to it.
        return None
    if not _has_usage(totals):
        return None
    return {
        "model": totals["model"] or metadata.get("model"),
        "tokens_in": totals["tokens_in"],
        "tokens_out": totals["tokens_out"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "cache_creation_tokens": totals["cache_creation_tokens"],
        "cost_usd": totals["cost_usd"],
        "model_calls": totals["model_calls"],
        "turn_increment": 0,
    }


def _has_usage(totals: Optional[dict]) -> bool:
    return bool(totals) and bool(
        totals["tokens_in"] or totals["tokens_out"]
        or totals["cache_read_tokens"] or totals["cache_creation_tokens"]
        or totals["cost_usd"] or totals["model_calls"]
    )


# ---------------------------------------------------------------------------
# Kill-path accounting
# ---------------------------------------------------------------------------

#: ``sessions.status`` stamped on a session whose turn was killed under it. The
#: written status vocabulary is exactly "active"/"archived"/"failed" (see
#: ``app.core.startup_reconcile._LIVE_LOOKING_STATUSES`` and
#: ``app.core.provider_agent.WORKER_FAILED_STATUS``); a killed run is a settled
#: verdict about a run that is over, which is what "failed" already means here.
#: Deliberately NOT a new value: several queries filter on
#: ``status NOT IN ('archived','failed')`` and a fresh token would slip past them.
KILLED_SESSION_STATUS = "failed"

#: ``turns.stop_reason`` recorded when the flush is the thing that settles the
#: row. ``turns.state`` itself goes to ``'aborted'`` — the only non-completed
#: terminal ``settle_turn`` accepts.
KILLED_TURN_STOP_REASON = "killed"


def _partial_usage_from_events(session_id: str, turn_id: Optional[str]) -> dict:
    """Sum the durable ``turn.usage`` records of ``turn_id`` into one total.

    A killed session never reaches its own turn-end persistence: the pool
    eviction ends the CLI subprocess mid-stream, so no ``ResultMessage`` is ever
    translated and ``_persist_turn_results`` never runs. The evidence that
    survives is the ``session_events`` log, where the turn recorder has already
    written one ``turn.usage`` row per model call.

    Both fluid legs stamp ``model_call_index`` on every such record (the
    delegated leg's context-snapshot record repeats its call's index) and the
    recorder never coalesces those away, so the records are dedupable and
    countable: the call count is the number of distinct indices, and the token
    columns are their sum. Records without an index (delegated sessions from
    before that, which ARE coalesced) are still summed, but contribute no call
    count — a partial flush is best-effort evidence recovery, not a second
    accounting system.

    ``cost_usd`` rides the per-call record too (fluid folds an estimate in when
    the provider reports none), so the spend lands with the tokens.
    """
    from app.core.turn_outcome import load_session_events

    totals = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
        "model_calls": 0,
        "model": None,
    }
    seen_indices: Set[Any] = set()
    unindexed = 0

    for event in load_session_events(session_id):
        if event.get("event_type") != "turn.usage":
            continue
        if turn_id is not None and event.get("turn_id") != turn_id:
            continue
        usage = (event.get("payload") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        index = usage.get("model_call_index")
        if index is not None:
            if index in seen_indices:
                # A retried call re-emits its index; count and bill it once.
                continue
            seen_indices.add(index)
        else:
            unindexed += 1
        totals["tokens_in"] += _as_int(usage.get("input_tokens"))
        totals["tokens_out"] += _as_int(usage.get("output_tokens"))
        totals["cache_read_tokens"] += _as_int(usage.get("cache_read_input_tokens"))
        totals["cache_creation_tokens"] += _as_int(
            usage.get("cache_creation_input_tokens")
        )
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            totals["cost_usd"] += float(cost)
        if usage.get("model"):
            totals["model"] = usage["model"]

    totals["model_calls"] = len(seen_indices) or unindexed
    totals["unindexed_records"] = unindexed
    return totals


def _as_int(value: Any) -> int:
    """Coerce a usage field to a non-negative int; anything else reads as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


async def settle_killed_session(
    session_id: str,
    *,
    stop_reason: str = KILLED_TURN_STOP_REASON,
    profile_id: Optional[str] = None,
) -> bool:
    """Book a killed session's partial usage and stamp its terminal lifecycle.

    The gap this closes: a user-killed sync child showed 0 calls / 0 tokens /
    $0 against real provider usage, with ``turns.state='completed'`` and
    ``sessions.status='active'`` while its background-task row said ``killed``.
    Nothing settled the child's own rows, because the kill removes the very
    runtime that would have.

    The turn transition selects the recovery owner. Accounting has a separate
    durable execution claim shared with normal finalization, so both paths
    may reach booking but only one increments the ledger and counters.

    Never raises — a kill must complete whatever the accounting does.
    Returns ``True`` when this call is what settled the turn.
    """
    from app.db import turns as turns_db

    claimed = False
    was_live = False
    turn_id: Optional[str] = None
    try:
        live = await run_sync(turns_db.get_live_turn, session_id)
        if live:
            was_live = True
            turn_id = live.get("id")
            run_id = live.get("run_id")
            started_at = live.get("started_at")
            settled = await run_sync(
                turns_db.settle_turn, turn_id, "aborted", stop_reason
            )
            claimed = settled is not None
    except Exception:
        logger.warning(
            "settle_killed_session: turn settle failed for %s", session_id,
            exc_info=True,
        )

    if claimed:
        try:
            totals = await run_sync(_partial_usage_from_events, session_id, turn_id)
        except Exception:
            logger.warning(
                "settle_killed_session: usage recovery failed for %s", session_id,
                exc_info=True,
            )
            totals = None
        if _has_usage(totals):
            try:
                key = usage_settlement_key(session_id, {
                    "id": turn_id, "run_id": run_id, "started_at": started_at,
                })
                settled = await run_sync(
                    database.settle_turn_usage,
                    session_id=session_id, settlement_key=key,
                    profile_id=profile_id, model=totals["model"],
                    tokens_in=totals["tokens_in"],
                    tokens_out=totals["tokens_out"],
                    cache_read_tokens=totals["cache_read_tokens"],
                    cache_creation_tokens=totals["cache_creation_tokens"],
                    cost_usd=totals["cost_usd"],
                    model_calls=totals["model_calls"],
                    turn_increment=0,
                )
            except Exception:
                logger.warning(
                    "settle_killed_session: settlement failed for %s", session_id,
                    exc_info=True,
                )
            else:
                if not settled:
                    logger.debug(
                        "settle_killed_session: usage for %s was already booked "
                        "by the turn's own finalization", session_id,
                    )

    # The session row's own verdict — the half the observed drift was missing
    # (``sessions.status='active'`` next to a ``killed`` background-task row).
    #
    # Gated on the session having been LIVE when the kill landed, not on
    # winning the claim. Turn-end reap and stop-all both sweep every registered
    # worker, and a worker that already finished its turn (and is merely
    # waiting for its dispatch to return) delivered its work — stamping a
    # failure verdict on that session would be a lie. A worker mid-turn is the
    # one this is about, whether we or its own dying runtime settled the row.
    if was_live:
        try:
            await run_sync(
                database.update_session,
                session_id=session_id,
                status=KILLED_SESSION_STATUS,
            )
        except Exception:
            logger.warning(
                "settle_killed_session: status write failed for %s", session_id,
                exc_info=True,
            )

    return claimed
