"""Time Glob and Grep on a local tree, ripgrep against the Python engines.

Each workload runs through the tool's own code path twice, once with ripgrep
and once with ripgrep hidden (the Python walk for Glob, the Python fallback
for Grep), and prints the best of ``--runs`` wall-clock times per engine. Grep
runs with ``no_ignore`` so both engines search the same files. Nothing leaves
the machine: no network, no model calls.

Usage, from the repository root::

    python fluid/scripts/bench_search.py [ROOT] [--runs 5]

ROOT defaults to the current directory.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fluid.tools.builtins import glob as glob_mod  # noqa: E402
from fluid.tools.builtins import grep as grep_mod  # noqa: E402
from fluid.tools.registry import ToolCtx  # noqa: E402

GLOBS = ["**/*.py", "*.md", "fluid/**/*.py", "!**/*.py"]
GREPS = [
    ("def handler", "common"),
    ("import", "many matches"),
    ("zq_no_such_token_qz", "no matches"),
]


def _best(fn, runs: int) -> tuple[float, str]:
    best, out = float("inf"), ""
    for _ in range(runs):
        start = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - start)
    return best * 1000, out


def _count(text: str) -> int:
    lines = text.splitlines()[1:]
    return sum(1 for ln in lines if ln and not ln.startswith(("[", "(no matches)")))


def _grep(root: Path, pattern: str, use_rg: bool) -> str:
    real_which = grep_mod.shutil.which
    grep_mod.shutil.which = real_which if use_rg else (lambda _name: None)
    try:
        outcome = asyncio.run(
            grep_mod.handler({"pattern": pattern, "no_ignore": True}, ToolCtx(cwd=str(root)))
        )
    finally:
        grep_mod.shutil.which = real_which
    return outcome.text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    rg = shutil.which("rg")
    if rg is None:
        sys.exit("ripgrep (rg) is not on PATH; nothing to compare")

    print(f"root: {root}\nrg:   {rg}\nbest of {args.runs} runs, milliseconds\n")
    print(f"{'workload':<44}{'python':>10}{'rg':>10}{'speedup':>9}  results")
    for pattern in GLOBS:
        py_ms, py_out = _best(lambda: glob_mod._glob(pattern, root, 200, rg=None).text, args.runs)
        rg_ms, rg_out = _best(lambda: glob_mod._glob(pattern, root, 200, rg=rg).text, args.runs)
        same = "same" if py_out == rg_out else "DIFFERENT"
        print(
            f"{'Glob ' + pattern:<44}{py_ms:>10.0f}{rg_ms:>10.0f}{py_ms / rg_ms:>8.1f}x"
            f"  {_count(rg_out)} files, {same}"
        )
    for pattern, label in GREPS:
        py_ms, py_out = _best(lambda: _grep(root, pattern, False), args.runs)
        rg_ms, rg_out = _best(lambda: _grep(root, pattern, True), args.runs)
        print(
            f"{f'Grep {pattern!r} ({label})':<44}{py_ms:>10.0f}{rg_ms:>10.0f}"
            f"{py_ms / rg_ms:>8.1f}x  {_count(rg_out)} lines"
        )


if __name__ == "__main__":
    main()
