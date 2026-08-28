#!/usr/bin/env python3
"""Find synchronous DB calls that run directly on the asyncio event loop.

Any attribute call on a synchronous DB facade (``database``, ``git_ops_db``, …)
that sits *directly* inside an ``async def`` blocks the event loop for the
duration of the query. While it blocks, every other request, WebSocket frame
and SSE stream on that worker is stalled — which is why a handful of these
present as "the whole app goes slow at once".

Calls nested inside a sync closure (the body handed to ``run_sync``) are NOT
reported: the innermost enclosing function wins, and that one is sync.

Usage:
    python scripts/scan_blocking_db.py                # whole app/, summary
    python scripts/scan_blocking_db.py app/api/x.py   # specific paths, detailed
    python scripts/scan_blocking_db.py --json         # machine readable
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import sys

# Module names that are synchronous DB facades.
SYNC_MODULES = {"database", "git_ops_db", "turns_db", "session_events_db"}

# If any of these appear on the call's source line, it is already offloaded
# (or awaited via an async wrapper) and is not a finding.
OFFLOAD_HINTS = ("await", "run_sync", "to_thread", "run_in_executor")


def _enclosing(funcs, line):
    """Innermost function containing `line`, as (start, end, name, is_async)."""
    best = None
    for start, end, name, is_async in funcs:
        if start <= line <= end and (best is None or start > best[0]):
            best = (start, end, name, is_async)
    return best


def scan_file(path: pathlib.Path):
    try:
        src = path.read_text()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    lines = src.splitlines()
    funcs = [
        (n.lineno, n.end_lineno, n.name, isinstance(n, ast.AsyncFunctionDef))
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        base = getattr(getattr(node.func, "value", None), "id", None)
        if base not in SYNC_MODULES:
            continue
        enc = _enclosing(funcs, node.lineno)
        if not (enc and enc[3]):  # only calls directly inside an async def
            continue
        line = lines[node.lineno - 1].strip()
        if any(h in line for h in OFFLOAD_HINTS):
            continue
        out.append(
            {
                "file": str(path),
                "line": node.lineno,
                "call": f"{base}.{getattr(node.func, 'attr', '?')}",
                "handler": enc[2],
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.paths:
        targets: list[pathlib.Path] = []
        for p in args.paths:
            path = pathlib.Path(p)
            targets.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
        detailed = True
    else:
        targets = sorted(pathlib.Path("app").rglob("*.py"))
        detailed = False

    findings = [f for t in targets for f in scan_file(t)]

    if args.json:
        json.dump(findings, sys.stdout, indent=2)
        print()
        return 1 if findings else 0

    if not findings:
        print("No blocking sync DB calls inside async def. Clean.")
        return 0

    per_file = collections.Counter(f["file"] for f in findings)
    print(f"TOTAL blocking sync DB calls inside async def: {len(findings)}\n")
    for path, count in per_file.most_common():
        print(f"{count:>4}  {path}")
        if detailed:
            for f in sorted(
                (f for f in findings if f["file"] == path), key=lambda x: x["line"]
            ):
                print(f"        L{f['line']:<6} {f['call']:<40} in {f['handler']}")
    return 1


if __name__ == "__main__":
    # Piping into `head` closes stdout early; exit quietly rather than dumping a
    # BrokenPipeError traceback over the results.
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(0)
