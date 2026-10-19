"""Harness head-to-head bench: Claude Code vs fluid (bare) vs fluid in the app.

    python -m scripts.harness_h2h validate [--task ID ...]
    python -m scripts.harness_h2h run --out DIR [--arms cc,fluid,app] [--task ID ...] [--trials N]
    python -m scripts.harness_h2h report --out DIR

See scripts/harness_h2h/README.md for the protocol.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.harness_h2h import tasks as T


def _select(task_ids: list[str] | None) -> list[T.Task]:
    tasks = T.load_tasks()
    if not task_ids:
        return tasks
    known = {t.id: t for t in tasks}
    missing = [i for i in task_ids if i not in known]
    if missing:
        raise SystemExit(f"unknown task(s): {', '.join(missing)}")
    return [known[i] for i in task_ids]


def cmd_validate(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    bad = 0
    for task in _select(args.task):
        result = T.validate(task, scratch)
        bad += 0 if result["ok"] else 1
        print(json.dumps(result), flush=True)
    return 1 if bad else 0


def cmd_run(args: argparse.Namespace) -> int:
    from scripts.harness_h2h import runner
    return runner.run_matrix(
        tasks=_select(args.task), arms=args.arms.split(","), trials=args.trials,
        out=Path(args.out), workspace=Path(args.workspace), model=args.model,
        effort=args.effort, token=Path(args.token) if args.token else None,
        app_url=args.app_url, skip_done=not args.redo,
    )


def cmd_report(args: argparse.Namespace) -> int:
    from scripts.harness_h2h import report
    path = report.write_report(Path(args.out))
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness_h2h")
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="hidden tests fail on base and pass on the fix")
    v.add_argument("--task", action="append")
    v.add_argument("--scratch", default="/tmp/harness_h2h_validate")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="run tasks x arms x trials")
    r.add_argument("--out", required=True)
    r.add_argument("--arms", default="cc,fluid,app")
    r.add_argument("--task", action="append")
    r.add_argument("--trials", type=int, default=1)
    r.add_argument("--workspace", default="/workspace/harness-h2h-ws",
                   help="the one directory every arm works in (also the app project's path)")
    r.add_argument("--model", default="gpt-6.1-sol")
    r.add_argument("--effort", default="high")
    r.add_argument("--token", default=None)
    r.add_argument("--app-url", default="http://127.0.0.1:8000")
    r.add_argument("--redo", action="store_true", help="re-run trials that already have a result")
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="summarise a run directory into HTML + JSON")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
