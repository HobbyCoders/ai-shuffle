"""One-shot, read-only release readiness summary for the ship-release skill.

Gathers every gate the owner otherwise has to be walked through one question at
a time: what changed since the last final tag, whether ``main`` CI is green on
the release candidate, whether edge publication caught up, the Play gates, and
which platforms actually need a new build. Nothing is tagged, dispatched or
published. Worker running revisions are NOT visible from here; the skill gets
those from the ``DeployStatus`` agent tool.

Usage::

    python scripts/ci/ship_release_status.py            # human summary
    python scripts/ci/ship_release_status.py --json     # machine-readable
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


def collect() -> dict:
    _try("git", "fetch", "--quiet", "--tags", "origin", "main")
    head = _try("git", "rev-parse", "origin/main") or ""
    tag = latest_final_tag((_try("git", "tag", "--list") or "").splitlines())
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
    for name, info in report["platform_builds"].items():
        lines.append(f"{name}: {'changed' if info['needed'] else 'no changes'}"
                     + (f" (e.g. {', '.join(info['example_paths'][:3])})" if info["needed"] else ""))
    lines.append("Ready to tag." if report["ready"] else "Blockers:\n  - " + "\n  - ".join(report["blockers"]))
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of a summary")
    args = parser.parse_args(argv)
    report = collect()
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
