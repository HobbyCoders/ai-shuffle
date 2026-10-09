"""On-demand process attribution. Shared working sets are not private RAM."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: dict[bool, tuple[float, dict]] = {}
_PREVIOUS: dict[bool, tuple[float, dict]] = {}
_COLLECTOR_CPU_SECONDS = 0.0
CACHE_SECONDS = 5.0
MAX_STACK_PROCESSES = 256


def _role(process, backend_pid):
    name = process.name().lower()
    if process.pid == backend_pid:
        return "backend"
    if name in {"postgres", "postgres.exe"}:
        return "database"
    if name in {"electron", "electron.exe", "ai shuffle", "ai shuffle.exe"}:
        # Only classification leaves this function, never command-line contents.
        args = process.cmdline()
        if "--type=gpu-process" in args:
            return "desktop_gpu_process_cpu"
        if "--type=renderer" in args:
            return "desktop_renderer"
        if not any(arg.startswith("--type=") for arg in args):
            return "desktop_main"
        return "desktop_other"
    if name in {"chrome", "chrome.exe", "msedge.exe", "chromium"}:
        return "agent_browser"
    if name in {"node", "node.exe"}:
        return "node_helpers"
    return "helpers"


def _managed_database_process(psutil):
    from app.core.config import settings
    try:
        port = int(settings.get_database_connection()["port"])
    except (OSError, ValueError, KeyError, RuntimeError):
        return None
    root = settings.effective_data_dir.resolve()
    # Container clusters are below DATA_DIR; isolated previews use its sibling.
    # Installed desktop keeps DATA_DIR at the stable user location, separately
    # from userData/postgres. Its supervisor sets userData/worker/.env explicitly.
    candidates = [root / "postgres" / "data", root.parent / "postgres" / "data"]
    env_file = Path(os.environ.get("AI_SHUFFLE_ENV_FILE", ""))
    if env_file.is_absolute() and env_file.name == ".env" and env_file.parent.name == "worker":
        candidates.append(env_file.parent.parent.resolve() / "postgres" / "data")
    # A path hint alone never establishes attribution: validate port, cluster,
    # process type and start time against the existing private descriptor.
    for data in dict.fromkeys(candidates):
        try:
            from app.core.private_storage import service_files

            with service_files():
                lines = (data / "postmaster.pid").read_text(encoding="utf-8").splitlines()
            if len(lines) < 4 or Path(lines[1]).resolve() != data or int(lines[3]) != port:
                continue
            process = psutil.Process(int(lines[0]))
            # A stale/reused PID must not turn someone else's process into ours.
            if (process.name().lower() not in {"postgres", "postgres.exe"}
                    or abs(process.create_time() - int(lines[2])) > 5):
                continue
            return process
        except (OSError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def collect_process_metrics(whole_stack=False):
    """Off-loop, cached by scope; CPU needs two snapshots at least 5s apart.

    Desktop ancestors establish ownership, not a machine-wide name search.
    Without that ancestry only the backend tree can be safely attributed.
    """
    import psutil

    global _COLLECTOR_CPU_SECONDS
    with _LOCK:
        now = time.monotonic()
        cached = _CACHE.get(whole_stack)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        started = time.perf_counter()
        cpu_started = time.thread_time()
        backend = psutil.Process(os.getpid())
        root = backend
        launcher_pids = set()
        if whole_stack:
            for parent in backend.parents():
                try:
                    name = parent.name().lower()
                    if name in {"python", "python.exe", "python3", "pythonw.exe"}:
                        launcher_pids.add(parent.pid)
                    if name in {"electron", "electron.exe", "ai shuffle", "ai shuffle.exe"}:
                        root = parent
                        break  # Nearest shell owns us; outer shells may run unrelated workers.
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        scope = "desktop_tree" if root.pid != backend.pid else "backend_tree"
        processes = [root, *root.children(recursive=True)]
        database = _managed_database_process(psutil) if whole_stack else None
        if database is not None:
            try:
                processes.extend([database, *database.children(recursive=True)])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        processes = list({p.pid: p for p in processes}.values())
        omitted = max(0, len(processes) - MAX_STACK_PROCESSES) if whole_stack else 0
        if whole_stack:
            processes = processes[:MAX_STACK_PROCESSES]
        details = []
        old_time, old_rows = _PREVIOUS.get(whole_stack, (now, {}))
        interval = now - old_time
        rows = {}
        groups = {}
        inaccessible = 0
        uss_missing = 0
        cpu_missing = 0
        logical_cpus = psutil.cpu_count() or 1
        for process in processes:
            try:
                with process.oneshot():
                    key = (process.pid, process.create_time())
                    role = "python_launcher" if process.pid in launcher_pids else _role(process, backend.pid)
                    mem = process.memory_info()
                    cpu = process.cpu_times()
                    ticks = cpu.user + cpu.system
                    if process.pid == backend.pid:
                        # Subtract only this collector thread, not concurrent app work.
                        ticks -= _COLLECTOR_CPU_SECONDS + time.thread_time() - cpu_started
                    row = {"rss_mib": mem.rss / 2**20,
                           "private_commit_mib": getattr(mem, "private", None),
                           "threads": process.num_threads()}
                    if row["private_commit_mib"] is not None:
                        row["private_commit_mib"] /= 2**20
                    if whole_stack and hasattr(process, "num_handles"):
                        row["handles"] = process.num_handles()
                    if whole_stack:
                        try:
                            row["private_resident_mib"] = process.memory_full_info().uss / 2**20
                        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                            uss_missing += 1
                    rows[key] = ticks
                    group = groups.setdefault(role, {"processes": 0, "cpu_pct_machine": None,
                                                     "rss_mib": 0, "threads": 0})
                    group["processes"] += 1
                    for name, value in row.items():
                        if value is not None:
                            group[name] = group.get(name, 0) + value
                    cpu_pct = None
                    if key in old_rows and interval > 0:
                        cpu_pct = max(0, ticks - old_rows[key]) / interval / logical_cpus * 100
                        group["cpu_pct_machine"] = (group["cpu_pct_machine"] or 0) + cpu_pct
                    else:
                        cpu_missing += 1
                    if whole_stack:
                        details.append({"pid": key[0], "started_at": key[1], "role": role,
                                        "cpu_pct_machine": cpu_pct, **row})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                inaccessible += 1
        collector_cpu = time.thread_time() - cpu_started
        _COLLECTOR_CPU_SECONDS += collector_cpu
        result = {"scope": scope, "requested_whole_stack": whole_stack,
                  "collector_cpu_seconds": collector_cpu,
                  "collector_cpu_excluded_from_backend": True,
                  "external_observer_cpu_included": False,
                  "working_set_is_unique_ram": False,
                  "managed_database_identified": database is not None,
                  "scope_includes_all_desktop_descendants": scope == "desktop_tree",
                  "gpu_engine_utilization_measured": False,
                  "logical_cpus": logical_cpus, "cpu_interval_seconds": interval or None,
                  "cpu_unmeasured_processes": cpu_missing,
                  "inaccessible_processes": inaccessible, "private_resident_missing_processes": uss_missing,
                  "rss_counts_shared_pages_multiple_times": True,
                  "private_resident_excludes_shared_pages": True,
                  "short_lived_processes_may_be_missed": True,
                  "groups": groups, "sampled_monotonic": now,
                  "processes": details, "omitted_processes": omitted,
                  "collection_ms": (time.perf_counter() - started) * 1000}
        _PREVIOUS[whole_stack] = (now, rows)
        _CACHE[whole_stack] = (now, result)
        return result
