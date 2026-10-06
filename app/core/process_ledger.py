"""A small on-disk ledger of the processes this server started.

Why it exists: the session pool, the Monitor shells and the CLI subprocesses
are all tracked in memory. When the server dies abruptly (a crash, a forced
``taskkill`` of the desktop app) nothing is left that knows which of the
surviving processes were ours, so an agent CLI and the dev server it started
kept running as orphans after the restart.

Each entry names one process the app spawned: its pid, its start time, its
command line, and the server process that recorded it. The resource monitor
also refreshes, for every still-running entry, the pids and start times of its
descendants, which is what lets a dev server whose parent shell already
exited be found again.

At boot, :func:`reap_previous_run` stops an entry's process only when ALL of
these hold:

- the server that recorded it is gone (a second server still running on the
  same data directory keeps its children);
- a process with that pid exists and its start time matches the recorded one
  (within a second), so a recycled pid is never touched;
- for a recorded root, its command line still matches the recorded one.

Descendants found by walking a verified root are taken only when they started
after it. Nothing is ever matched by image name. The file is per install
(``process-ledger-<instance id>.json`` in the data directory) and every
function here is best-effort and never raises.

The ledger only knows the roots it was told about, and a child is found
through a root that is still alive or through the last snapshot. So
:func:`mark_run` also gives each server run a random id, which it records
in ``runs-process-ledger-<instance id>.json`` and puts in this process's
environment as :data:`RUN_MARKER`. Every child inherits it: agent CLIs,
Monitor shells, native Bash tool calls and whatever they start (#1711). At
boot, a process is also stopped when its own environment carries the id of a
recorded run whose server is gone, and it started after that server did.
Long-lived services the server starts for itself (the native Chrome) are
launched without the marker.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Start times are compared with this tolerance (psutil derives them from the
#: boot time, which a clock step can shift slightly). A recycled pid belongs to
#: a process started later, so a one-second window cannot admit it.
START_TIME_TOLERANCE_S = 1.0

#: Recorded command lines are clipped to this many characters.
_CMDLINE_CHARS = 512

#: How many descendants one entry may remember.
_MAX_DESCENDANTS = 64

#: How many entries the file may hold; the oldest are dropped first.
_MAX_ENTRIES = 512

#: Inherited by every process this server starts; names the server run.
RUN_MARKER = "AI_SHUFFLE_SERVER_RUN"

#: How many server runs the runs file may remember; the oldest go first.
_MAX_RUNS = 32

_lock = threading.Lock()
_run_id: Optional[str] = None


def _psutil():
    try:
        import psutil
    except ImportError:  # pragma: no cover — psutil ships with every runtime
        return None
    return psutil


def ledger_path() -> Path:
    from app.core.config import settings

    if settings.data_dir is None:
        raise RuntimeError("no data directory configured")
    return Path(settings.data_dir) / f"process-ledger-{settings.effective_instance_id}.json"


def runs_path() -> Path:
    """The server runs whose children carry :data:`RUN_MARKER`, beside the ledger."""
    path = ledger_path()
    return path.with_name("runs-" + path.name)


def _same_start(current: Any, recorded: Any) -> bool:
    try:
        return abs(float(current) - float(recorded)) < START_TIME_TOLERANCE_S
    except (TypeError, ValueError):
        return False


def _cmdline(proc: Any) -> str:
    try:
        return " ".join(proc.cmdline() or ())[:_CMDLINE_CHARS]
    except Exception:  # noqa: BLE001 — unreadable command line
        return ""


_server_identity: Optional[Tuple[int, float]] = None


def _server() -> Tuple[int, float]:
    """This server process's (pid, start time), cached."""
    global _server_identity
    if _server_identity is None:
        pid = os.getpid()
        created = 0.0
        ps = _psutil()
        if ps is not None:
            try:
                created = ps.Process(pid).create_time()
            except Exception:  # noqa: BLE001
                created = 0.0
        _server_identity = (pid, created)
    return _server_identity


def _load(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt ledger is an empty one
        logger.debug("process ledger unreadable at %s", path, exc_info=True)
        return {}
    entries = data.get("processes") if isinstance(data, dict) else None
    return {str(k): v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}


def _save(path: Path, entries: Dict[str, Dict[str, Any]]) -> None:
    if len(entries) > _MAX_ENTRIES:
        ordered = sorted(entries.items(), key=lambda kv: kv[1].get("recorded_at") or 0)
        entries = dict(ordered[-_MAX_ENTRIES:])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "processes": entries}), encoding="utf-8")
    os.replace(tmp, path)


def _load_runs(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt file is an empty one
        logger.debug("process ledger runs unreadable at %s", path, exc_info=True)
        return {}
    runs = data.get("runs") if isinstance(data, dict) else None
    return {str(k): v for k, v in runs.items() if isinstance(v, dict)} if isinstance(runs, dict) else {}


def _save_runs(path: Path, runs: Dict[str, Dict[str, Any]]) -> None:
    if len(runs) > _MAX_RUNS:
        ordered = sorted(runs.items(), key=lambda kv: kv[1].get("started_at") or 0)
        runs = dict(ordered[-_MAX_RUNS:])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "runs": runs}), encoding="utf-8")
    os.replace(tmp, path)


def mark_run() -> Optional[str]:
    """Record this server run and put its id in the environment children inherit.

    Call once at boot, before anything is spawned. Idempotent; never raises.
    Returns the run id, or None when it could not be recorded (then no child
    is marked, since no later boot could match the id).
    """
    global _run_id
    if _run_id is not None:
        return _run_id
    import time
    import uuid

    run_id = uuid.uuid4().hex
    server_pid, server_created = _server()
    try:
        with _lock:
            path = runs_path()
            runs = _load_runs(path)
            runs[run_id] = {
                "server_pid": server_pid,
                "server_create_time": server_created,
                "started_at": time.time(),
            }
            _save_runs(path, runs)
    except Exception:  # noqa: BLE001
        logger.debug("process ledger: recording this server run failed", exc_info=True)
        return None
    os.environ[RUN_MARKER] = run_id
    _run_id = run_id
    return run_id


def without_run_marker(env: Dict[str, str]) -> Dict[str, str]:
    """``env`` for a service that must outlive this server run (never reaped as its orphan)."""
    return {key: value for key, value in env.items() if key != RUN_MARKER}


def record_process(
    pid: Optional[int],
    kind: str,
    *,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> bool:
    """Remember that this server started ``pid``. Idempotent; never raises.

    Returns True when a new entry was written.
    """
    ps = _psutil()
    if ps is None or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        proc = ps.Process(pid)
        created = proc.create_time()
        cmdline = _cmdline(proc)
    except Exception:  # noqa: BLE001 — already gone: nothing to remember
        return False
    server_pid, server_created = _server()
    try:
        import time

        with _lock:
            path = ledger_path()
            entries = _load(path)
            existing = entries.get(str(pid))
            if existing and _same_start(existing.get("create_time"), created):
                return False
            entries[str(pid)] = {
                "pid": pid,
                "create_time": created,
                "cmdline": cmdline,
                "kind": kind,
                "session_id": session_id,
                "task_id": task_id,
                "server_pid": server_pid,
                "server_create_time": server_created,
                "recorded_at": time.time(),
                "descendants": [],
            }
            _save(path, entries)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("process ledger record failed for pid %s", pid, exc_info=True)
        return False


def forget_process(pid: Optional[int]) -> None:
    """Drop ``pid``'s entry (it exited normally). Never raises."""
    if not isinstance(pid, int):
        return
    try:
        with _lock:
            path = ledger_path()
            entries = _load(path)
            if entries.pop(str(pid), None) is not None:
                _save(path, entries)
    except Exception:  # noqa: BLE001
        logger.debug("process ledger forget failed for pid %s", pid, exc_info=True)


def _live_root(ps: Any, entry: Dict[str, Any]) -> Any:
    """The entry's process if it is still the very process recorded, else None."""
    try:
        proc = ps.Process(int(entry["pid"]))
        if not _same_start(proc.create_time(), entry.get("create_time")):
            return None
        recorded = entry.get("cmdline") or ""
        if recorded and _cmdline(proc) != recorded:
            return None
        return proc
    except Exception:  # noqa: BLE001 — gone or unreadable: not ours to touch
        return None


def _descendants_after(root: Any, root_created: float) -> List[Any]:
    """Live descendants of ``root`` that started after it (no recycled links)."""
    out = []
    try:
        children = root.children(recursive=True)
    except Exception:  # noqa: BLE001
        return out
    for child in children:
        try:
            if child.create_time() + START_TIME_TOLERANCE_S >= float(root_created):
                out.append(child)
        except Exception:  # noqa: BLE001
            continue
    return out


def refresh() -> int:
    """Prune exited entries and re-snapshot descendants of the live ones.

    Runs on the resource monitor tick, in a worker thread. A dev server that
    outlives the shell that started it is only findable through a snapshot
    taken while that shell was alive. Returns the number of live entries.
    """
    ps = _psutil()
    if ps is None:
        return 0
    server_pid, _ = _server()
    try:
        with _lock:
            path = ledger_path()
            entries = _load(path)
            changed = False
            for key, entry in list(entries.items()):
                if entry.get("server_pid") != server_pid:
                    continue  # another run's entry: only boot may act on it
                root = _live_root(ps, entry)
                if root is None:
                    # Keep an exited root while its snapshot still names live
                    # descendants; drop it once none is left.
                    alive = [
                        [pid, created] for pid, created in entry.get("descendants") or []
                        if _is_same_process(ps, pid, created)
                    ]
                    if alive:
                        if alive != entry.get("descendants"):
                            entry["descendants"] = alive
                            changed = True
                        continue
                    entries.pop(key, None)
                    changed = True
                    continue
                snapshot = []
                for child in _descendants_after(root, entry.get("create_time") or 0.0):
                    try:
                        snapshot.append([child.pid, child.create_time()])
                    except Exception:  # noqa: BLE001
                        continue
                snapshot = snapshot[:_MAX_DESCENDANTS]
                if snapshot != entry.get("descendants"):
                    entry["descendants"] = snapshot
                    changed = True
            if changed:
                _save(path, entries)
            return len(entries)
    except Exception:  # noqa: BLE001
        logger.debug("process ledger refresh failed", exc_info=True)
        return 0


def _is_same_process(ps: Any, pid: Any, created: Any) -> bool:
    try:
        return _same_start(ps.Process(int(pid)).create_time(), created)
    except Exception:  # noqa: BLE001
        return False


def _server_alive(ps: Any, entry: Dict[str, Any]) -> bool:
    """True when the server that recorded ``entry`` is still running."""
    server_pid = entry.get("server_pid")
    if not isinstance(server_pid, int):
        return False
    if server_pid == os.getpid():
        return True
    return _is_same_process(ps, server_pid, entry.get("server_create_time"))


def _protected_pids(ps: Any) -> set:
    """Never touch this server, its ancestors or its own children."""
    me = ps.Process()
    protected = {me.pid}
    try:
        protected.update(p.pid for p in me.parents())
        protected.update(c.pid for c in me.children(recursive=True))
    except Exception:  # noqa: BLE001
        pass
    return protected


def reap_previous_run(*, timeout: float = 5.0) -> List[int]:
    """Stop the processes a previous, now-dead run of this server left behind.

    Called once at boot, before this run spawns anything. Entries whose
    recording server is still alive are kept untouched; everything else is
    verified (pid + start time, and command line for a root) before it is
    stopped, then removed from the ledger. Returns the pids stopped.
    """
    ps = _psutil()
    if ps is None:
        return []
    try:
        with _lock:
            path = ledger_path()
            entries = _load(path)
            runs_file = runs_path()
            runs = _load_runs(runs_file)
            if not entries and not runs:
                return []
            protected = _protected_pids(ps)
            victims: Dict[int, Any] = {}

            def take(proc: Any) -> None:
                if proc.pid not in protected:
                    victims.setdefault(proc.pid, proc)

            kept: Dict[str, Dict[str, Any]] = {}
            for key, entry in entries.items():
                if _server_alive(ps, entry):
                    kept[key] = entry
                    continue
                root = _live_root(ps, entry)
                if root is not None:
                    take(root)
                    for child in _descendants_after(root, entry.get("create_time") or 0.0):
                        take(child)
                for pid, created in entry.get("descendants") or []:
                    try:
                        proc = ps.Process(int(pid))
                        if _same_start(proc.create_time(), created):
                            take(proc)
                    except Exception:  # noqa: BLE001
                        continue
            if entries:
                _save(path, kept)
            # Processes no entry names, found by the run id they inherited.
            dead = {run_id: run for run_id, run in runs.items() if not _server_alive(ps, run)}
            if dead:
                for proc in _marked_by(ps, dead):
                    take(proc)
                _save_runs(runs_file, {run_id: run for run_id, run in runs.items() if run_id not in dead})
    except Exception:  # noqa: BLE001
        logger.warning("process ledger: reading the previous run failed", exc_info=True)
        return []

    if not victims:
        return []
    procs = list(victims.values())
    _stop(ps, procs, timeout=timeout)
    pids = sorted(victims)
    logger.warning(
        "Boot reconcile: stopped %d process(es) the previous server run started "
        "and left behind: pids=%s", len(pids), pids,
    )
    return pids


def _marked_by(ps: Any, runs: Dict[str, Dict[str, Any]]) -> List[Any]:
    """Live processes whose own environment carries one of ``runs``' ids.

    A process inherits the id from that run's server, so it cannot have
    started before the server did; one that claims to is not taken. A process
    whose environment cannot be read is never taken.
    """
    starts = {run_id: float(run.get("server_create_time") or 0.0) for run_id, run in runs.items()}
    earliest = min(starts.values()) - START_TIME_TOLERANCE_S
    out = []
    for proc in ps.process_iter(["create_time"]):
        try:
            created = proc.info.get("create_time")
            if created is None or created < earliest:
                continue
            run_id = proc.environ().get(RUN_MARKER)
        except Exception:  # noqa: BLE001 — gone, or not ours to read
            continue
        if run_id in starts and created + START_TIME_TOLERANCE_S >= starts[run_id]:
            out.append(proc)
    return out


def _stop(ps: Any, procs: Iterable[Any], *, timeout: float) -> None:
    procs = list(procs)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        _gone, alive = ps.wait_procs(procs, timeout=timeout)
    except Exception:  # noqa: BLE001
        alive = procs
    for proc in alive:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
