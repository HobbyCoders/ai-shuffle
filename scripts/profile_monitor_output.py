"""Measure the real Monitor stdout producer in an isolated process (not app.main).

Run at base/head with the same arguments. No providers, live data, DB or server
are used. This demonstrates output retention; it does not attribute live RSS.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import shlex
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--lines", type=int, default=256)
    parser.add_argument("--line-bytes", type=int, default=131072)
    args = parser.parse_args()
    if min(args.tasks, args.lines, args.line_bytes) <= 0:
        parser.error("load dimensions must be positive")
    sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]
    with tempfile.TemporaryDirectory(prefix="ai-shuffle-memory-lab-") as directory:
        home = Path(directory) / "home"
        home.mkdir()
        empty = Path(directory) / "empty.env"
        empty.write_text("")
        env = {key: value for key, value in os.environ.items()
               if key in ("PATH", "LANG", "LD_LIBRARY_PATH")}
        env.update(HOME=str(home), DATA_DIR=str(Path(directory) / "data"), LOCAL_MODE="true",
                   AI_SHUFFLE_ENV_FILE=str(empty), WORKSPACE_DIR=str(home))
        os.environ.clear()
        os.environ.update(env)
        asyncio.run(exercise(args, directory))


async def exercise(args, directory):
    from app.core import process_ledger
    from app.core.process_tree import kill_process_tree
    from app.mcp import builtin_tools as bt

    # Do not collect process provenance; this lab only measures stdout retention.
    process_ledger.record_process = lambda *a, **kw: None
    code = ("import sys; [sys.stdout.write(str(i)+':'+'x'*"
            f"{args.line_bytes}+chr(10)) for i in range({args.lines})]")
    command = shlex.join([sys.executable, "-c", code])
    gc.collect()
    tracemalloc.start()
    started = time.monotonic()
    points = []

    def sample(label):
        gc.collect()
        status = Path("/proc/self/status").read_text()
        rss = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS:")))
        points.append({"label": label, "seconds": time.monotonic() - started, "rss_kib": rss,
                       "traced_current": tracemalloc.get_traced_memory()[0],
                       "retained_output_bytes": sum(sum(sys.getsizeof(x) for x in task.lines)
                                                    for task in bt._background_tasks.values())})

    try:
        sample("warm")
        for index in range(args.tasks):
            task = await bt._spawn_background(command, directory, None)
            await task._reader_task
            if task.exit_code != 0:
                raise RuntimeError("Synthetic output producer failed")
            sample(f"task-{index + 1}")
        top = [{"file": str(stat.traceback[0].filename).replace(str(ROOT), "CHECKOUT"),
                "line": stat.traceback[0].lineno, "bytes": stat.size, "count": stat.count}
               for stat in tracemalloc.take_snapshot().statistics("lineno")[:8]]
        print(json.dumps({"points": points, "top": top}))
    finally:
        for task in bt._background_tasks.values():
            if task.is_running():
                await kill_process_tree(task.process, own_process_group=True)
        bt._background_tasks.clear()
        tracemalloc.stop()


if __name__ == "__main__":
    main()
