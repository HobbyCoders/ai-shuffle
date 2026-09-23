"""Reconcile historical duplicate usage bookings of cancelled workers (#1407).

Before the shared settlement claim (#1416), a cancelled native worker could be
booked twice: once by its own turn-end persistence (the engine's Interrupted
totals) and once by the kill path's event-log recovery. Both wrote a
``usage_log`` row and incremented the session counters by the same amounts.
Those rows carry no ``settlement_key``, so the key cannot pair them.

This tool pairs them from execution identity instead, and never from matching
token totals alone. A duplicate group is proposed only when every check holds:

1. The session's engine store (``sessions.sdk_session_id`` in
   ``fluid_session_events``) holds the attempt as one fluid turn id with
   exactly one ``TurnStarted``, exactly one ``TurnComplete`` or
   ``Interrupted`` terminal, and no ``ErrorEvent``.
2. The attempt's per-call ``Usage`` events (one per billed model call) sum to
   the terminal's own totals, and the terminal's ``num_model_calls`` equals the
   number of calls.
3. The attempt maps to exactly one app ``turns`` row by time, and that row is
   settled. When the app recorded per-call ``turn.usage`` records for that
   turn, their distinct call indices agree with the engine's call count.
4. The unkeyed ``usage_log`` rows in the attempt's booking window (from the
   attempt's start to the next attempt or app turn of the same session) all
   equal the per-call sum. The window cannot hold another attempt, so a
   legitimate retry or continuation, which starts a new engine turn, is never
   paired with the first attempt's row.
5. The window holds exactly two such rows and no keyed row.

Anything else is reported as NOT ASSESSABLE with the reason and left alone.

For each group the earliest row (lowest id) is kept. ``--apply`` deletes the
other row and subtracts its tokens, cache tokens and cost from the session
counters. The model-call counter is reduced by the attempt's call count only
when both old booking paths provably counted those calls (the terminal's
``num_model_calls`` and the app's per-call records both equal it); otherwise it
is left unchanged and the report says so. ``turn_count`` is never touched: the
kill path always booked zero turns.

All changes happen in one transaction, planned under row locks on the affected
sessions. If a planned row has changed or vanished, or a counter would go
negative, the transaction rolls back and nothing is written. A second run finds
nothing to do.

The running app caches usage summaries in memory; they refresh on the next
booking for the session or on restart.

Usage (from the managed worker environment, like the usage backfill)::

    # Read-only report, the default:
    python -m scripts.dedupe_cancelled_usage
    python -m scripts.dedupe_cancelled_usage --session-id <app session id>

    # Remove the duplicates and correct the counters:
    python -m scripts.dedupe_cancelled_usage --apply
    python -m scripts.dedupe_cancelled_usage --session-id <app session id> --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("dedupe_cancelled_usage")

#: Seconds of skew allowed between the engine's event clock and the database's.
CLOCK_TOLERANCE_SECONDS = 5.0

_TERMINAL_CLASSES = ("TurnComplete", "Interrupted")
_SETTLED_TURN_STATES = ("completed", "aborted", "parked")
_TOKEN_COLUMNS = (
    "tokens_in", "tokens_out", "cache_read_tokens", "cache_creation_tokens",
)
#: usage_log column -> key in the engine terminal's usage dict.
_TERMINAL_USAGE_KEYS = {
    "tokens_in": "input_tokens",
    "tokens_out": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_creation_tokens": "cache_creation_input_tokens",
}
#: usage_log column -> field on the engine ``Usage`` event.
_CALL_USAGE_KEYS = {
    "tokens_in": "input_tokens",
    "tokens_out": "output_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_creation_tokens": "cache_creation_tokens",
}
#: usage_log column -> the session counter it was added to.
_SESSION_COUNTERS = {
    "tokens_in": "total_tokens_in",
    "tokens_out": "total_tokens_out",
    "cache_read_tokens": "total_cache_read_tokens",
    "cache_creation_tokens": "total_cache_creation_tokens",
    "cost_usd": "total_cost_usd",
}


class StaleEvidence(RuntimeError):
    """The database changed between planning and writing; nothing was applied."""


@dataclass
class EngineAttempt:
    turn_id: str
    starts: List[Dict[str, Any]] = field(default_factory=list)
    terminals: List[Dict[str, Any]] = field(default_factory=list)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    errors: int = 0

    @property
    def start(self) -> Optional[Dict[str, Any]]:
        return self.starts[0] if self.starts else None

    @property
    def terminal(self) -> Optional[Dict[str, Any]]:
        return self.terminals[0] if self.terminals else None

    def call_sums(self) -> Dict[str, int]:
        return {
            column: sum(_int(call.get(key)) for call in self.calls)
            for column, key in _CALL_USAGE_KEYS.items()
        }


@dataclass
class DuplicateGroup:
    session_id: str
    engine_store: str
    attempt: EngineAttempt
    app_turn: Dict[str, Any]
    app_call_indices: int
    keep: Dict[str, Any]
    remove: List[Dict[str, Any]]
    model_calls_correction: int
    notes: List[str] = field(default_factory=list)


@dataclass
class SessionPlan:
    session_id: str
    groups: List[DuplicateGroup] = field(default_factory=list)
    not_assessable: List[str] = field(default_factory=list)

    def corrections(self) -> Dict[str, float]:
        totals: Dict[str, float] = {c: 0 for c in _SESSION_COUNTERS.values()}
        totals["total_model_calls"] = 0
        for group in self.groups:
            for row in group.remove:
                for column, counter in _SESSION_COUNTERS.items():
                    value = row.get(column)
                    totals[counter] += (
                        _number(value) if column == "cost_usd" else _int(value)
                    )
            totals["total_model_calls"] += group.model_calls_correction * len(group.remove)
        return totals


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    """A JSONB column comes back as a dict on psycopg, as text on some drivers."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _epoch(value: Any) -> Optional[float]:
    """Epoch seconds for a timestamp column or an engine ``ts``; None if unknown."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(epoch: Optional[float]) -> str:
    if epoch is None:
        return "?"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


def _row_tokens(row: Dict[str, Any]) -> Dict[str, int]:
    return {column: _int(row.get(column)) for column in _TOKEN_COLUMNS}


# ---------------------------------------------------------------------------
# Evidence reads (SELECT only)
# ---------------------------------------------------------------------------

def _candidate_sessions(cursor, session_id: Optional[str]) -> List[str]:
    where = "settlement_key IS NULL AND session_id IS NOT NULL"
    params: List[Any] = []
    if session_id:
        where += " AND session_id = %s"
        params.append(session_id)
    cursor.execute(
        f"SELECT session_id FROM usage_log WHERE {where} "
        "GROUP BY session_id HAVING COUNT(*) > 1 ORDER BY session_id",
        params,
    )
    return [row["session_id"] for row in cursor.fetchall()]


def _engine_attempts(cursor, engine_store: str) -> Dict[str, EngineAttempt]:
    cursor.execute(
        "SELECT seq, event FROM fluid_session_events WHERE session_id = %s "
        "ORDER BY seq",
        (engine_store,),
    )
    attempts: Dict[str, EngineAttempt] = {}
    for row in cursor.fetchall():
        event = _as_dict(row["event"])
        cls = event.get("_cls")
        turn_id = event.get("turn_id")
        if not turn_id or cls not in (
            "TurnStarted", "Usage", "ErrorEvent", *_TERMINAL_CLASSES,
        ):
            continue
        event.setdefault("seq", row["seq"])
        attempt = attempts.setdefault(turn_id, EngineAttempt(turn_id))
        if cls == "TurnStarted":
            attempt.starts.append(event)
        elif cls == "Usage":
            attempt.calls.append(event)
        elif cls == "ErrorEvent":
            attempt.errors += 1
        else:
            attempt.terminals.append(event)
    return attempts


def _app_turns(cursor, session_id: str) -> List[Dict[str, Any]]:
    cursor.execute(
        "SELECT id, state, stop_reason, run_id, started_at, ended_at FROM turns "
        "WHERE session_id = %s AND started_at IS NOT NULL ORDER BY started_at, id",
        (session_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _app_call_indices(cursor, session_id: str) -> Dict[str, set]:
    cursor.execute(
        "SELECT turn_id, payload FROM session_events "
        "WHERE session_id = %s AND event_type = 'turn.usage'",
        (session_id,),
    )
    indices: Dict[str, set] = {}
    for row in cursor.fetchall():
        usage = _as_dict(row["payload"]).get("usage")
        if not isinstance(usage, dict):
            continue
        index = usage.get("model_call_index")
        if index is not None and row["turn_id"]:
            indices.setdefault(row["turn_id"], set()).add(index)
    return indices


def _usage_rows(cursor, session_id: str) -> List[Dict[str, Any]]:
    cursor.execute(
        "SELECT id, settlement_key, model, tokens_in, tokens_out, "
        "cache_read_tokens, cache_creation_tokens, cost_usd, created_at "
        "FROM usage_log WHERE session_id = %s ORDER BY created_at, id",
        (session_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _attempt_problem(attempt: EngineAttempt) -> Optional[str]:
    """Why this engine turn is not one proven execution attempt, if it is not."""
    if len(attempt.starts) != 1:
        return f"{len(attempt.starts)} TurnStarted events"
    if len(attempt.terminals) != 1:
        return f"{len(attempt.terminals)} TurnComplete/Interrupted terminals"
    if attempt.errors:
        return f"{attempt.errors} ErrorEvent(s) in the turn"
    if _epoch(attempt.start.get("ts")) is None or _epoch(attempt.terminal.get("ts")) is None:
        return "engine events carry no timestamp"
    terminal = attempt.terminal
    if terminal.get("num_model_calls") != len(attempt.calls):
        return (f"terminal num_model_calls={terminal.get('num_model_calls')} but "
                f"{len(attempt.calls)} Usage events")
    usage = terminal.get("usage") if isinstance(terminal.get("usage"), dict) else {}
    sums = attempt.call_sums()
    for column, key in _TERMINAL_USAGE_KEYS.items():
        if _int(usage.get(key)) != sums[column]:
            return (f"terminal {key}={_int(usage.get(key))} disagrees with the "
                    f"per-call Usage sum {sums[column]}")
    return None


def plan_session(cursor, session_id: str) -> SessionPlan:
    plan = SessionPlan(session_id)
    cursor.execute(
        "SELECT id, sdk_session_id FROM sessions WHERE id = %s", (session_id,),
    )
    session = cursor.fetchone()
    if session is None:
        plan.not_assessable.append("no sessions row (usage rows are orphaned)")
        return plan
    engine_store = session["sdk_session_id"]
    if not engine_store:
        plan.not_assessable.append("no engine store linked (sdk_session_id empty)")
        return plan

    attempts = _engine_attempts(cursor, engine_store)
    if not attempts:
        plan.not_assessable.append(f"engine store {engine_store} has no turn events")
        return plan
    app_turns = _app_turns(cursor, session_id)
    call_indices = _app_call_indices(cursor, session_id)
    rows = _usage_rows(cursor, session_id)
    for row in rows:
        row["_created"] = _epoch(row.get("created_at"))
    if any(row["_created"] is None for row in rows):
        plan.not_assessable.append("usage_log rows without a readable created_at")
        return plan

    # Every start in the session bounds the previous attempt's booking window.
    boundaries = sorted(
        {t for t in (_epoch(a.start.get("ts")) for a in attempts.values() if a.start) if t}
        | {t for t in (_epoch(turn.get("started_at")) for turn in app_turns) if t}
    )

    for attempt in sorted(
        attempts.values(), key=lambda a: _epoch((a.start or {}).get("ts")) or math.inf,
    ):
        label = f"engine turn {attempt.turn_id}"
        problem = _attempt_problem(attempt)
        if problem:
            plan.not_assessable.append(f"{label}: {problem}")
            continue
        start = _epoch(attempt.start.get("ts"))
        terminal_ts = _epoch(attempt.terminal.get("ts"))

        matches = [
            turn for turn in app_turns
            if (_epoch(turn.get("started_at")) or math.inf) - CLOCK_TOLERANCE_SECONDS
            <= start
            <= (_epoch(turn.get("ended_at")) or math.inf) + CLOCK_TOLERANCE_SECONDS
        ]
        if len(matches) != 1:
            plan.not_assessable.append(
                f"{label}: maps to {len(matches)} app turn rows, need exactly one")
            continue
        app_turn = matches[0]
        if app_turn.get("state") not in _SETTLED_TURN_STATES:
            plan.not_assessable.append(
                f"{label}: app turn {app_turn['id']} is still {app_turn.get('state')}")
            continue
        indices = call_indices.get(app_turn["id"], set())
        if indices and len(indices) != len(attempt.calls):
            plan.not_assessable.append(
                f"{label}: app turn {app_turn['id']} recorded {len(indices)} call "
                f"indices, the engine {len(attempt.calls)} Usage events")
            continue

        app_start = _epoch(app_turn.get("started_at"))
        window_start = min(start, app_start) - CLOCK_TOLERANCE_SECONDS
        window_end = next(
            (b for b in boundaries if b > max(start, app_start) + CLOCK_TOLERANCE_SECONDS),
            math.inf,
        )
        if window_end < terminal_ts:
            plan.not_assessable.append(
                f"{label}: another turn starts before this attempt's terminal")
            continue
        in_window = [
            row for row in rows if window_start <= row["_created"] < window_end
        ]
        if any(row.get("settlement_key") for row in in_window):
            plan.not_assessable.append(
                f"{label}: booking window holds a keyed (post-fix) row")
            continue
        if len(in_window) < 2:
            continue
        sums = attempt.call_sums()
        mismatched = [row for row in in_window if _row_tokens(row) != sums]
        if mismatched:
            plan.not_assessable.append(
                f"{label}: {len(in_window)} rows in the booking window but ids "
                f"{', '.join(str(r['id']) for r in mismatched)} differ from the "
                "per-call Usage sum")
            continue
        if len(in_window) != 2:
            plan.not_assessable.append(
                f"{label}: {len(in_window)} identical rows in one window; the two "
                "historical booking paths explain at most two")
            continue

        ordered = sorted(in_window, key=lambda r: _int(r["id"]))
        notes: List[str] = []
        calls = len(attempt.calls)
        both_counted = (
            attempt.terminal.get("num_model_calls") == calls and len(indices) == calls
        )
        if not both_counted:
            notes.append(
                "model-call counter left unchanged: the app has no per-call "
                "records for this turn, so the kill path's call count is unproven")
        plan.groups.append(DuplicateGroup(
            session_id=session_id,
            engine_store=engine_store,
            attempt=attempt,
            app_turn=app_turn,
            app_call_indices=len(indices),
            keep=ordered[0],
            remove=ordered[1:],
            model_calls_correction=calls if both_counted else 0,
            notes=notes,
        ))
    return plan


# ---------------------------------------------------------------------------
# Reporting and applying
# ---------------------------------------------------------------------------

def _describe_row(verb: str, row: Dict[str, Any], keep: Optional[Dict[str, Any]] = None) -> str:
    gap = ""
    if keep is not None and row["_created"] is not None and keep["_created"] is not None:
        gap = f" (+{(row['_created'] - keep['_created']) * 1000:.1f} ms after kept row)"
    return (
        f"      {verb:<6} usage_log id={row['id']} created={_iso(row['_created'])}"
        f" in={_int(row.get('tokens_in'))} out={_int(row.get('tokens_out'))}"
        f" cache_r={_int(row.get('cache_read_tokens'))}"
        f" cache_c={_int(row.get('cache_creation_tokens'))}"
        f" cost={_number(row.get('cost_usd')):.6f}{gap}"
    )


def print_plan(plan: SessionPlan) -> None:
    if not plan.groups and not plan.not_assessable:
        return
    print(f"  session {plan.session_id}")
    for group in plan.groups:
        attempt = group.attempt
        sums = attempt.call_sums()
        start, terminal = attempt.start, attempt.terminal
        print(f"    DUPLICATE  engine store {group.engine_store}, turn {attempt.turn_id}")
        print(f"      evidence: TurnStarted seq={start.get('seq')} at "
              f"{_iso(_epoch(start.get('ts')))}; {terminal.get('_cls')} "
              f"seq={terminal.get('seq')} at {_iso(_epoch(terminal.get('ts')))}")
        print(f"      evidence: {len(attempt.calls)} per-call Usage events sum to "
              f"in={sums['tokens_in']} out={sums['tokens_out']} "
              f"cache_r={sums['cache_read_tokens']} "
              f"cache_c={sums['cache_creation_tokens']}; the terminal reports the "
              f"same totals and num_model_calls={terminal.get('num_model_calls')}")
        turn = group.app_turn
        print(f"      evidence: app turn {turn['id']} state={turn.get('state')} "
              f"stop_reason={turn.get('stop_reason')} started="
              f"{_iso(_epoch(turn.get('started_at')))} ended="
              f"{_iso(_epoch(turn.get('ended_at')))}; app per-call records: "
              f"{group.app_call_indices}")
        print(_describe_row("keep", group.keep))
        for row in group.remove:
            print(_describe_row("remove", row, group.keep))
        for note in group.notes:
            print(f"      note: {note}")
    if plan.groups:
        corrections = plan.corrections()
        parts = ", ".join(
            f"{name} -{value:.6f}" if name == "total_cost_usd" else f"{name} -{int(value)}"
            for name, value in corrections.items()
        )
        print(f"    counter correction: {parts}")
    for reason in plan.not_assessable:
        print(f"    NOT ASSESSABLE  {reason}. Nothing proposed.")


def _apply_plan(cursor, plan: SessionPlan) -> None:
    for group in plan.groups:
        for row in group.remove:
            tokens = _row_tokens(row)
            cursor.execute(
                "DELETE FROM usage_log WHERE id = %s AND session_id = %s "
                "AND settlement_key IS NULL AND tokens_in = %s AND tokens_out = %s "
                "AND cache_read_tokens = %s AND cache_creation_tokens = %s "
                "RETURNING id",
                (row["id"], plan.session_id, tokens["tokens_in"], tokens["tokens_out"],
                 tokens["cache_read_tokens"], tokens["cache_creation_tokens"]),
            )
            if cursor.fetchone() is None:
                raise StaleEvidence(
                    f"usage_log id={row['id']} changed or vanished during the run")
    corrections = plan.corrections()
    assignments = ", ".join(f"{name} = {name} - %s" for name in corrections)
    guards = " AND ".join(f"COALESCE({name}, 0) >= %s" for name in corrections)
    values = list(corrections.values())
    cursor.execute(
        f"UPDATE sessions SET {assignments} WHERE id = %s AND {guards} RETURNING id",
        (*values, plan.session_id, *values),
    )
    if cursor.fetchone() is None:
        raise StaleEvidence(
            f"session {plan.session_id}: a counter would go negative; its "
            "counters do not include the duplicate this plan would remove")


def reconcile(*, session_id: Optional[str] = None, apply: bool = False) -> int:
    """Report (and with ``apply``, remove) identity-proven duplicate bookings.

    Returns the process exit code: 0 on success, 1 when an apply was refused.
    """
    from app.db.base import get_db

    plans: List[SessionPlan] = []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            for sid in _candidate_sessions(cursor, session_id):
                if apply:
                    # Planned under the row lock the counter update needs.
                    cursor.execute(
                        "SELECT id FROM sessions WHERE id = %s FOR UPDATE", (sid,),
                    )
                plan = plan_session(cursor, sid)
                print_plan(plan)
                plans.append(plan)
            if apply:
                for plan in plans:
                    if plan.groups:
                        _apply_plan(cursor, plan)
    except StaleEvidence as exc:
        logger.error("Nothing was written: %s. Re-run to re-plan.", exc)
        return 1

    groups = sum(len(plan.groups) for plan in plans)
    rows = sum(len(group.remove) for plan in plans for group in plan.groups)
    skipped = sum(len(plan.not_assessable) for plan in plans)
    logger.info(
        "%d session(s) with more than one unkeyed usage row examined: %d duplicate "
        "group(s), %d row(s) %s; %d item(s) not assessable",
        len(plans), groups, rows, "removed" if apply else "would be removed", skipped,
    )
    if not apply and rows:
        logger.info("Read-only run: nothing was written. Re-run with --apply to fix.")
    return 0


def report_duplicate_cancelled_usage(session_id=None) -> int:
    """Read-only entry kept for ``backfill_usage_from_events --dedupe-cancelled``."""
    return reconcile(session_id=session_id, apply=False)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find usage_log rows that book the same cancelled execution attempt "
            "twice, proven from engine and turn identity. Read-only unless "
            "--apply is given."
        )
    )
    parser.add_argument(
        "--session-id", default=None,
        help="examine a single app session id instead of every session",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=("delete the duplicate rows and correct the session counters in one "
              "transaction"),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    from app.db.base import close_pool, init_pool

    init_pool(min_conn=1, max_conn=2)
    try:
        return reconcile(session_id=args.session_id, apply=args.apply)
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
