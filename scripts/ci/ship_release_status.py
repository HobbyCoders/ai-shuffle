"""One-shot, read-only release readiness summary for the ship-release skill.

Gathers every gate the owner otherwise has to be walked through one question at
a time: what changed since the last final tag, whether ``main`` CI is green on
the release candidate, whether edge publication caught up, the Play gates, and
which platforms actually need a new build, and the live smoke gate: a passing
smoke result file for the candidate revision on every covered worker (see
``docs/internal/live-smoke.md``). Nothing is tagged, dispatched or published.
Worker running revisions are NOT visible from here; the skill gets those from
the ``DeployStatus`` agent tool.

Usage::

    python scripts/ci/ship_release_status.py            # human summary
    python scripts/ci/ship_release_status.py --json     # machine-readable
    python scripts/ci/ship_release_status.py --smoke-worker Unraid --smoke-worker DesktopWorker
    python scripts/ci/ship_release_status.py --skip-smoke "DesktopWorker away this week"
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live_smoke import gate as smoke_gate_mod
from scripts.live_smoke import results as smoke_results
from scripts.live_smoke import scenarios as smoke_scenarios

REPOSITORY = "HobbyCoders/ai-shuffle"
FINAL_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
# Workflows whose latest run on the candidate must be green before tagging.
REQUIRED_CI = ("CI Backend", "CI JavaScript", "CI fluid")
EDGE_WORKFLOW = "Publish desktop source update"

# Path prefixes -> platform that needs a fresh artifact when they change.
# The phone UI is served by the worker (android/ only ships native shell and
# phone tools), so frontend-only changes never need a Play build.
PLATFORM_PATHS = {
    "android": ("android/",),
    "desktop_installer": ("desktop/",),
}


def _run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()


def _try(*args: str) -> Optional[str]:
    try:
        return _run(*args)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def latest_final_tag(tags: Iterable[str]) -> Optional[str]:
    finals = [t for t in tags if FINAL_TAG.fullmatch(t)]
    if not finals:
        return None
    return max(finals, key=lambda t: tuple(int(p) for p in t[1:].split(".")))


def release_tags() -> list:
    """Tags on origin. A checkout can carry local-only tags (an old v5.0.0 on
    DesktopWorker once made the preflight suggest v5.0.1), so local tags are read
    only when origin cannot be reached."""
    remote = _try("git", "ls-remote", "--tags", "--refs", "origin")
    if remote is None:
        return (_try("git", "tag", "--list") or "").splitlines()
    return [line.split("refs/tags/", 1)[1] for line in remote.splitlines() if "refs/tags/" in line]


def next_patch(tag: Optional[str]) -> str:
    if not tag:
        return "v0.1.0"
    major, minor, patch = (int(p) for p in tag[1:].split("."))
    return f"v{major}.{minor}.{patch + 1}"


def platforms_needing_builds(changed: Iterable[str]) -> dict:
    changed = list(changed)
    result = {
        name: sorted({p for p in changed if p.startswith(prefixes)})[:5]
        for name, prefixes in PLATFORM_PATHS.items()
    }
    return {name: {"needed": bool(paths), "example_paths": paths} for name, paths in result.items()}


def ci_state(runs: list, sha: str) -> dict:
    """Latest run per required workflow on ``sha`` -> success/pending/failed/missing."""
    state = {}
    for name in REQUIRED_CI + (EDGE_WORKFLOW,):
        mine = [r for r in runs if r.get("workflowName") == name and r.get("headSha") == sha]
        if not mine:
            state[name] = "missing"
            continue
        run = mine[0]  # gh returns newest first
        if run.get("status") != "completed":
            state[name] = "pending"
        else:
            state[name] = "success" if run.get("conclusion") == "success" else (run.get("conclusion") or "failed")
    return state


def _gh_json(*args: str):
    out = _try("gh", *args)
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return None


def _variable(name: str, env: Optional[str] = None) -> Optional[str]:
    args = ["gh", "variable", "get", name, "--repo", REPOSITORY]
    if env:
        args += ["--env", env]
    return _try(*args)


def smoke_gate(revision: str, workers: Optional[list] = None, skip_reason: Optional[str] = None,
               smoke_dir: Optional[str] = None, scenario_rows: Optional[list] = None) -> dict:
    """Live smoke verdict for the candidate (``pass``, ``block`` or ``skipped``).

    ``workers`` are the covered workers; when omitted, every worker with a
    result file for ``revision`` counts, and none at all blocks.
    """
    base = smoke_results.smoke_dir(smoke_dir)
    if scenario_rows is None:
        scenario_rows = smoke_scenarios.rows(smoke_scenarios.load())
    return smoke_gate_mod.evaluate(base, revision, workers, scenario_rows, skip_reason=skip_reason)


def collect(smoke_workers: Optional[list] = None, skip_smoke: Optional[str] = None,
            smoke_dir: Optional[str] = None) -> dict:
    _try("git", "fetch", "--quiet", "--tags", "origin", "main")
    head = _try("git", "rev-parse", "origin/main") or ""
    tag = latest_final_tag(release_tags())
    tag_sha = _try("git", "rev-list", "-n", "1", tag) if tag else None
    commits = []
    changed: list = []
    if tag:
        log = _try("git", "log", "--format=%h %s", f"{tag}..origin/main") or ""
        commits = [line for line in log.splitlines() if line]
        changed = (_try("git", "diff", "--name-only", f"{tag}..origin/main") or "").splitlines()
    runs = _gh_json("run", "list", "--repo", REPOSITORY, "--branch", "main", "--limit", "60",
                    "--json", "workflowName,status,conclusion,headSha") or []
    ci = ci_state(runs, head)
    gates = {
        "ANDROID_WORKERS_READY": _variable("ANDROID_WORKERS_READY"),
        "PLAY_SIGNING_READY": _variable("PLAY_SIGNING_READY", env="google-play-internal"),
    }
    blockers = []
    if not commits:
        blockers.append("Nothing new on main since the last release tag.")
    for name in REQUIRED_CI:
        if ci.get(name) != "success":
            blockers.append(f"{name} on {head[:9]} is {ci.get(name)}.")
    smoke = smoke_gate(head, smoke_workers, skip_smoke, smoke_dir)
    blockers += [f"Live smoke: {b}" for b in smoke["blockers"]]
    return {
        "main_head": head,
        "last_release": {"tag": tag, "sha": tag_sha},
        "suggested_tag": next_patch(tag),
        "commits_since_release": len(commits),
        "commit_subjects": commits[:30],
        "ci_on_main_head": ci,
        "edge_published": ci.get(EDGE_WORKFLOW) == "success",
        "platform_builds": platforms_needing_builds(changed),
        "play_gates": gates,
        "smoke": smoke,
        "release_notes_lines": smoke["release_notes_lines"],
        "blockers": blockers,
        "ready": not blockers,
    }


def render(report: dict) -> str:
    lines = [
        f"Candidate: origin/main {report['main_head'][:9]}  "
        f"(last release {report['last_release']['tag']}, {report['commits_since_release']} commits since)",
        f"Suggested tag: {report['suggested_tag']}",
        "CI on candidate: " + ", ".join(f"{k}={v}" for k, v in report["ci_on_main_head"].items()),
        "Play gates: " + ", ".join(f"{k}={v}" for k, v in report["play_gates"].items()),
    ]
    smoke = report.get("smoke")
    if smoke:
        lines.append(f"Live smoke: {smoke['verdict']}"
                     + (f" ({smoke['skip_reason']})" if smoke.get("skip_reason") else ""))
        lines += [f"  owner to-do: {t}" for t in smoke.get("owner_todo", [])]
        lines += [f"  skipped blocker: {b}" for b in smoke.get("would_block", [])]
        lines += [f"  release notes must say: {line}" for line in smoke.get("release_notes_lines", [])]
    for name, info in report["platform_builds"].items():
        lines.append(f"{name}: {'changed' if info['needed'] else 'no changes'}"
                     + (f" (e.g. {', '.join(info['example_paths'][:3])})" if info["needed"] else ""))
    lines.append("Ready to tag." if report["ready"] else "Blockers:\n  - " + "\n  - ".join(report["blockers"]))
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of a summary")
    parser.add_argument("--smoke-worker", action="append", default=None, metavar="LABEL",
                        help="worker that must have a passing live smoke file (repeat; default: "
                             "every worker with a file for the candidate)")
    parser.add_argument("--skip-smoke", metavar="REASON", default=None,
                        help="tag without a passing live smoke; the reason goes into the release notes")
    parser.add_argument("--smoke-dir", default=None,
                        help="smoke results folder (default $AI_SHUFFLE_SMOKE_DIR or <data dir>/smoke)")
    args = parser.parse_args(argv)
    report = collect(args.smoke_worker, args.skip_smoke, args.smoke_dir)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
