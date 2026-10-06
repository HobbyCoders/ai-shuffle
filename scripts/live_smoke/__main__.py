"""Command line for the live smoke. See docs/internal/live-smoke.md.

    python -m scripts.live_smoke run                 # drive the script rows on this worker
    python -m scripts.live_smoke record REV --worker NAME --row ID --status pass --evidence "..."
    python -m scripts.live_smoke import FILE         # bring another worker's result file here
    python -m scripts.live_smoke check REV --worker A --worker B [--skip-smoke REASON]
    python -m scripts.live_smoke render [--check]    # regenerate scenarios.md from scenarios.json

Nothing here runs on a schedule; it starts only when someone runs it.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from scripts.live_smoke import gate
from scripts.live_smoke import results as results_mod
from scripts.live_smoke import scenarios as scen


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def cmd_run(args) -> int:
    from scripts.live_smoke.client import HttpWorkerClient
    from scripts.live_smoke.runner import Runner, SetupError

    data = scen.load(args.scenarios)
    client = HttpWorkerClient(args.url)
    runner = Runner(
        client, data, results_base=results_mod.smoke_dir(args.results_dir), model=args.model,
        token_cap=args.token_cap, peer=args.peer, project=args.project, agent=args.agent,
        only=[r.strip() for r in args.rows.split(",") if r.strip()] if args.rows else None,
        log=lambda m: print(m, flush=True),
    )
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        result, path = runner.run()
    except SetupError as exc:
        print(f"live smoke not started: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # interrupted during preflight, before any session
        print("live smoke interrupted before it started", file=sys.stderr)
        return 130
    finally:
        client.close()
    counts = {}
    for row in result["rows"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"outcome: {result['outcome']}  rows: {counts}  tokens counted: "
          f"{result['tokens']['counted']} / cap {result['token_cap']}")
    print(f"result file: {path}")
    if result["outcome"] == "interrupted":
        return 130
    return 0 if result["outcome"] == "pass" else 1


def cmd_record(args) -> int:
    path = results_mod.result_path(results_mod.smoke_dir(args.results_dir), args.revision, args.worker)
    try:
        row = results_mod.record(path, args.row, args.status, args.evidence, by=args.by)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"not recorded: {exc}", file=sys.stderr)
        return 1
    print(f"{row['id']}: {row['status']} recorded in {path}")
    return 0


def cmd_import(args) -> int:
    data = results_mod.read(Path(args.file))
    if not data or not data.get("revision") or not data.get("worker") or "rows" not in data:
        print("not a smoke result file", file=sys.stderr)
        return 1
    if data.get("partial"):
        print("a partial run (--rows) cannot be imported as a worker's result", file=sys.stderr)
        return 1
    dest = results_mod.result_path(results_mod.smoke_dir(args.results_dir), data["revision"], data["worker"])
    results_mod.write(dest, data)
    print(f"imported {data['worker']} at {data['revision'][:9]} -> {dest}")
    return 0


def cmd_check(args) -> int:
    data = scen.load(args.scenarios)
    report = gate.evaluate(results_mod.smoke_dir(args.results_dir), args.revision, args.worker,
                           scen.rows(data), skip_reason=args.skip_smoke)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"smoke gate for {args.revision[:9]}: {report['verdict']}")
        for line in report["blockers"]:
            print(f"  - {line}")
        for line in report["owner_todo"]:
            print(f"  owner: {line}")
        for line in report["release_notes_lines"]:
            print(f"  release notes: {line}")
    return 1 if report["verdict"] == "block" else 0


def cmd_render(args) -> int:
    text = scen.render_markdown(scen.load(args.scenarios))
    if args.check:
        current = scen.SCENARIOS_MD.read_text(encoding="utf-8") if scen.SCENARIOS_MD.exists() else ""
        if current != text:
            print(f"{scen.SCENARIOS_MD} is out of date; run python -m scripts.live_smoke render", file=sys.stderr)
            return 1
        return 0
    scen.SCENARIOS_MD.write_text(text, encoding="utf-8")
    print(f"wrote {scen.SCENARIOS_MD}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.live_smoke", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--results-dir", help="override the smoke dir (default: <data dir>/smoke or $AI_SHUFFLE_SMOKE_DIR)")
        sp.add_argument("--scenarios", type=Path, default=None, help="scenarios JSON (default: the skill's)")

    run = sub.add_parser("run", help="drive the script rows on this worker")
    common(run)
    run.add_argument("--url", default="http://127.0.0.1:8000", help="this worker's loopback address")
    run.add_argument("--model", help="model for every session (default: cheapest-looking usable model)")
    run.add_argument("--token-cap", type=int, help="stop the run past this many tokens (default 600000)")
    run.add_argument("--peer", help="the other worker's name (default: the only other paired worker)")
    run.add_argument("--rows", help="comma-separated row ids; writes a partial file the gate ignores")
    run.add_argument("--project", help="project name or id for rows that need one (default from scenarios)")
    run.add_argument("--agent", help="agent (profile) id for the sessions (default: the worker's default)")
    run.set_defaults(fn=cmd_run)

    rec = sub.add_parser("record", help="record a browser or owner row result")
    common(rec)
    rec.add_argument("revision")
    rec.add_argument("--worker", required=True)
    rec.add_argument("--row", required=True)
    rec.add_argument("--status", required=True, choices=("pass", "fail"))
    rec.add_argument("--evidence", required=True, help="session id, snapshot path or what you saw")
    rec.add_argument("--by", default="manual", help="who checked it (owner, agent browser, ...)")
    rec.set_defaults(fn=cmd_record)

    imp = sub.add_parser("import", help="copy another worker's result file into this smoke dir")
    common(imp)
    imp.add_argument("file")
    imp.set_defaults(fn=cmd_import)

    chk = sub.add_parser("check", help="release gate verdict for a revision")
    common(chk)
    chk.add_argument("revision")
    chk.add_argument("--worker", action="append", help="worker the release covers (repeat); default: every file present")
    chk.add_argument("--skip-smoke", metavar="REASON", help="override the gate; the reason goes into the release notes")
    chk.add_argument("--json", action="store_true")
    chk.set_defaults(fn=cmd_check)

    ren = sub.add_parser("render", help="regenerate scenarios.md from scenarios.json")
    ren.add_argument("--scenarios", type=Path, default=None)
    ren.add_argument("--check", action="store_true", help="exit 1 when scenarios.md is out of date")
    ren.set_defaults(fn=cmd_render)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
