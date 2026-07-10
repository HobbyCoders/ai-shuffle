"""Boot-time "reconcile from durable truth".

After a crash or restart the in-memory control planes are empty — the session
pool, the task registry, and the live session store hold nothing — but durable
state written by the previous run can still claim sessions are mid-flight:

- ``sessions.parked_task_kind`` (v51) is set when a turn ends with live
  background work (a sub-agent or an SDK-backgrounded shell — see
  ``live_session_store.end_streaming``). No background work survives a
  restart, so any marker found at boot is stale: left in place it reads as a
  session forever "parked" awaiting an unpark that can never arrive.
- ``sessions.status`` only ever holds idle/terminal values today (``active``,
  ``archived``) — streaming/parked signals live in memory
  (``live_session_store.is_streaming``, ``task_registry``) — but a row that
  has drifted to a live-looking value (written by a different version, or by
  hand) would render as stuck forever, so those are defensively reset too.
- CLI subprocesses are tracked in memory only (``app.core.session_pool``
  snapshots descendant trees per-eviction), so a mid-turn server death leaks
  them. Every spawn is env-marked with ``AI_SHUFFLE_MANAGED_BY=<instance id>``
  (``app.core.environment._build_env_with_ai_tools``), which is the one
  attribution that survives the parent server dying — the boot sweep only
  terminates processes carrying THIS install's marker.

Everything here is scoped to THIS install: several installs share one
Postgres (each with its own ``INSTANCE_ID``, recorded per-row in
``sessions.instance_id``), and another install's sessions may be legitimately
parked or streaming right now. Only rows with
``instance_id = settings.effective_instance_id`` are ever touched, and the
process sweep only matches this install's env marker.

Branch (b) of the reconcile plan — recomputing the automation scheduler from
persisted state — is deliberately NOT duplicated here:
``automation_manager.start()`` already calls ``_reconcile_schedules()``
synchronously at boot and re-runs it every 60s
(``SCHEDULE_RECONCILE_SECONDS``), rebuilding the APScheduler jobs from the
``automations`` table. See ``app/core/automation_manager.py``.

Each branch is independently guarded and ``reconcile_from_durable_truth``
never raises: a reconcile failure must never block boot (see the call site in
``app/main.py``). Every reconciled orphan is recorded via ``record_error`` so
the incident is visible in the app's error UI / ``GetAppErrors``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import Any, Dict

from app.core.config import settings
from app.db.base import get_db
from app.db.compat import ph
from app.db.error_logs import record_error

logger = logging.getLogger(__name__)

# Env marker stamped into every spawned CLI subprocess — must match
# app.core.environment._build_env_with_ai_tools.
CLI_ENV_MARKER = "AI_SHUFFLE_MANAGED_BY"

# sessions.status values that would read as in-flight. Current code never
# writes these (only "active"/"archived"), so any hit is drift — reset it.
_LIVE_LOOKING_STATUSES = ("streaming", "parked", "running")


def _row_value(row: Any, key: str, idx: int) -> Any:
    """Read a column from a row that may be dict-like or a plain tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def _clear_stale_parked_markers() -> int:
    """Clear ``parked_task_kind`` on this install's sessions (all are stale).

    The marker means "parent turn ended with background work still running";
    that work ran inside the previous server process, so on a fresh boot every
    own-instance marker is an orphan. Returns the number cleared.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id, parked_task_kind FROM sessions
                 WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL""",
            (instance_id,),
        )
        rows = cursor.fetchall()
        if rows:
            cursor.execute(
                f"""UPDATE sessions SET parked_task_kind = NULL
                     WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL""",
                (instance_id,),
            )

    for row in rows:
        session_id = _row_value(row, "id", 0)
        kind = _row_value(row, "parked_task_kind", 1)
        record_error(
            "backend",
            f"Boot reconcile: session {session_id} was parked on background "
            f"work (kind={kind}) when the server stopped; cleared the stale "
            "parked marker — background work does not survive a restart.",
            level="WARNING",
            session_id=session_id,
            context={"reconcile": "parked_task_kind", "kind": kind},
        )
    return len(rows)


def _mark_stale_background_task_rows() -> int:
    """Mark this install's non-terminal durable task rows killed (v55).

    ``background_tasks`` mirrors the in-memory registry (WRITE-ONLY in Phase
    1 — no reader depends on it), so any own-instance row still non-terminal
    at boot was stranded by the previous process: background work never
    survives a restart. Mirrors ``_clear_stale_parked_markers``'s scoping.
    Returns the number of rows marked.
    """
    from app.db.background_tasks import mark_stale_background_tasks_killed

    count = mark_stale_background_tasks_killed()
    if count:
        record_error(
            "backend",
            f"Boot reconcile: {count} durable background-task row(s) were "
            "still non-terminal when the server stopped; marked them killed "
            "— background work does not survive a restart.",
            level="WARNING",
            context={"reconcile": "background_tasks", "rows": count},
        )
    return count


def _reset_orphaned_session_statuses() -> int:
    """Reset own-instance sessions stuck in a live-looking ``status``.

    Defensive: the app only writes "active"/"archived" today, so streaming/
    parked/running here means the row drifted (older/newer version, manual
    edit). On a fresh boot nothing is streaming, so reset to "active".
    Returns the number reset.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    placeholders = ", ".join([_ph] * len(_LIVE_LOOKING_STATUSES))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id, status FROM sessions
                 WHERE instance_id = {_ph} AND status IN ({placeholders})""",
            (instance_id, *_LIVE_LOOKING_STATUSES),
        )
        rows = cursor.fetchall()
        if rows:
            cursor.execute(
                f"""UPDATE sessions SET status = 'active'
                     WHERE instance_id = {_ph} AND status IN ({placeholders})""",
                (instance_id, *_LIVE_LOOKING_STATUSES),
            )

    for row in rows:
        session_id = _row_value(row, "id", 0)
        status = _row_value(row, "status", 1)
        record_error(
            "backend",
            f"Boot reconcile: session {session_id} had orphaned "
            f"status='{status}' after a restart; reset to 'active' — nothing "
            "can be streaming on a fresh boot.",
            level="WARNING",
            session_id=session_id,
            context={"reconcile": "session_status", "previous_status": status},
        )
    return len(rows)


def _sweep_stray_cli_processes() -> int:
    """Terminate CLI processes leaked by a previous run of THIS install.

    Runs before this server has spawned anything, so every positively
    attributed process is by definition a stray. Attribution is conservative:

    - the process environment must carry ``AI_SHUFFLE_MANAGED_BY`` equal to
      this install's instance id (injected into every spawn; unreadable env →
      unattributable → never killed);
    - the process must look like the Claude CLI (name/cmdline), so arbitrary
      marked descendants (e.g. a dev server an agent intentionally left
      running) are not swept — only marked children OF a matched CLI go with
      it, since they die with their stdio anyway;
    - our own process, ancestors, and descendants are always excluded (the
      server itself may have been restarted from inside an agent shell, in
      which case our ancestor chain carries the marker too).

    Returns the number of processes terminated.
    """
    try:
        import psutil
    except ImportError:
        logger.debug("psutil unavailable — skipping stray CLI sweep")
        return 0

    expected = settings.effective_instance_id
    me = psutil.Process()
    protected = {me.pid}
    try:
        protected.update(p.pid for p in me.parents())
        protected.update(c.pid for c in me.children(recursive=True))
    except psutil.Error:
        pass

    def _marked(proc: "psutil.Process") -> bool:
        try:
            return proc.environ().get(CLI_ENV_MARKER) == expected
        except (psutil.Error, OSError):
            # Cannot read the environment → cannot positively attribute →
            # never kill.
            return False

    victims: Dict[int, "psutil.Process"] = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid in protected or proc.pid in victims:
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or ()).lower()
            if "claude" not in name and "claude" not in cmdline:
                continue
            if not _marked(proc):
                continue
            victims[proc.pid] = proc
            for child in proc.children(recursive=True):
                if child.pid not in protected and _marked(child):
                    victims[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not victims:
        return 0

    procs = list(victims.values())
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass

    pids = sorted(victims.keys())
    logger.warning(
        "Boot reconcile: terminated %d stray CLI process(es) leaked by a "
        "previous server run: pids=%s", len(pids), pids,
    )
    record_error(
        "backend",
        f"Boot reconcile: terminated {len(pids)} stray Claude CLI "
        "process(es) leaked by the previous server run.",
        level="WARNING",
        context={"reconcile": "stray_cli", "pids": pids},
    )
    return len(pids)


async def reconcile_from_durable_truth() -> Dict[str, int]:
    """Run every boot-time reconcile branch; never raises.

    Called once from the startup sequence (app/main.py) after core DB init and
    before any session machinery starts. Each branch is guarded independently
    so one failure cannot block the others — or boot. Returns per-branch
    counts for logging.
    """
    counts = {
        "parked_markers_cleared": 0,
        "statuses_reset": 0,
        "stray_cli_reaped": 0,
        "background_task_rows_killed": 0,
    }

    try:
        counts["parked_markers_cleared"] = await asyncio.to_thread(
            _clear_stale_parked_markers
        )
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: clearing stale parked markers failed")
        record_error(
            "backend",
            f"Boot reconcile failed while clearing stale parked markers: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "parked_task_kind"},
        )

    try:
        counts["background_task_rows_killed"] = await asyncio.to_thread(
            _mark_stale_background_task_rows
        )
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: marking stale background-task rows failed")
        record_error(
            "backend",
            f"Boot reconcile failed while marking stale background-task rows: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "background_tasks"},
        )

    try:
        counts["statuses_reset"] = await asyncio.to_thread(
            _reset_orphaned_session_statuses
        )
    except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
        logger.exception("Boot reconcile: resetting orphaned session statuses failed")
        record_error(
            "backend",
            f"Boot reconcile failed while resetting orphaned session statuses: {exc}",
            detail=traceback.format_exc(),
            context={"reconcile": "session_status"},
        )

    # Suppressed under TESTING (conftest sets TESTING=1): the sweep scans the
    # whole process table, which is slow and pointless inside the test suite.
    if settings.reap_stray_cli_on_boot and os.environ.get("TESTING") != "1":
        try:
            counts["stray_cli_reaped"] = await asyncio.to_thread(
                _sweep_stray_cli_processes
            )
        except Exception as exc:  # noqa: BLE001 — reconcile must never block boot
            logger.exception("Boot reconcile: stray CLI sweep failed")
            record_error(
                "backend",
                f"Boot reconcile failed while sweeping stray CLI processes: {exc}",
                detail=traceback.format_exc(),
                context={"reconcile": "stray_cli"},
            )

    if any(counts.values()):
        logger.info(f"Boot reconcile from durable truth: {counts}")
    else:
        logger.debug("Boot reconcile from durable truth: nothing to reconcile")
    return counts
