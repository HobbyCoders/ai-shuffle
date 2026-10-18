"""One-shot, read-only release readiness summary for the ship-release skill.

Gathers every gate the owner otherwise has to be walked through one question at
a time: what changed since the last final tag, whether the local full suites
passed on the release candidate on Linux and on Windows (the ``local/full-suite``
and ``local/windows-suite`` statuses ``scripts/local_ci.py full --attest``
posts; a successful owner-dispatched ``windows-suite.yml`` run on main at the
candidate also satisfies the Windows one), whether edge publication caught up, the Play gates, and
which platforms actually need a new build, and the live smoke gate: a passing
smoke result file for the candidate revision on every covered worker (see
``docs/internal/live-smoke.md``). Nothing is tagged, dispatched or published.
Worker running revisions are NOT visible from here; the skill gets those from
the ``DeployStatus`` agent tool.

Usage::

    python scripts/ci/ship_release_status.py            # human summary
    python scripts/ci/ship_release_status.py --json     # machine-readable
    python scripts/ci/ship_release_status.py --smoke-worker ServerWorker --smoke-worker DesktopWorker
    python scripts/ci/ship_release_status.py --skip-smoke "DesktopWorker away this week"
    python scripts/ci/ship_release_status.py --skip-smoke-rows two_worker "DesktopWorker away; preview run only"
    python scripts/ci/ship_release_status.py --skip-windows "DesktopWorker away; no Windows changes"
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

from scripts import local_ci
from scripts.live_smoke import gate as smoke_gate_mod
from scripts.live_smoke import results as smoke_results
from scripts.live_smoke import scenarios as smoke_scenarios

REPOSITORY = "HobbyCoders/ai-shuffle"
FINAL_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
OWNER_ID = "76577322"
# Local full-suite passes the owner must have posted on the candidate before
# tagging. CI does not test code; scripts/local_ci.py full --attest posts these.
REQUIRED_LOCAL = ("local/full-suite", "local/windows-suite")
WINDOWS_CONTEXT = "local/windows-suite"
EDGE_WORKFLOW = "Publish desktop source update"
# The same Windows full suite on a GitHub-hosted runner (free in public
# repositories). A successful run the owner dispatched on main at the exact
# candidate counts as local/windows-suite, so a release need not wait for a
# Windows PC.
HOSTED_WINDOWS_WORKFLOW = "windows-suite.yml"
HOSTED_WINDOWS_HINT = (f"or dispatch {HOSTED_WINDOWS_WORKFLOW} on main "
                       f"(`gh workflow run {HOSTED_WINDOWS_WORKFLOW} --ref main`)")

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


def public_main_ref() -> str:
    """``<remote>/main`` of the public repository, resolved as ``local_ci.py --attest`` does.

    A shared checkout can name the private archive ``origin`` and the public
    repository ``public``; reading ``origin`` there reported the archive's main
    and its old tags as the candidate. Overrides (LOCAL_CI_BASE) are ignored:
    a release candidate is always public main.
    """
    return local_ci.resolve_base(public_only=True)


def _parse_ls_remote_tags(listing: str) -> dict:
    """``git ls-remote --tags`` output -> {tag: commit sha}, preferring peeled (``^{}``) lines."""
    tags: dict = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            tags[name[:-3]] = sha
        else:
            tags.setdefault(name, sha)
    return tags


def release_tags(remote: str = "origin", ref: Optional[str] = None) -> dict:
    """Tags on ``remote`` -> commit sha (None when only known locally).

    A checkout can carry local-only tags (an old v5.0.0 on one worker once
    made the preflight suggest v5.0.1), and a shared checkout carries the
    private archive's tags. So local tags are read only when the remote cannot
    be reached, and then only those on ``ref``'s history.
    """
    listing = _try("git", "ls-remote", "--tags", remote)
    if listing is None:
        merged = ["--merged", ref] if ref else []
        return {name: None for name in (_try("git", "tag", "--list", *merged) or "").splitlines() if name}
    return _parse_ls_remote_tags(listing)


def _commit(remote: str, tag: str, sha: Optional[str]) -> Optional[str]:
    """The tag's commit, fetched by id from ``remote`` when this clone lacks it.

    Never ``git fetch --tags``: the archive's same-named tags would clobber or
    be clobbered, and the fetch would fail outright.
    """
    if not sha:
        return _try("git", "rev-list", "-n", "1", f"refs/tags/{tag}")
    if _try("git", "cat-file", "-e", f"{sha}^{{commit}}") is None:
        _try("git", "fetch", "--quiet", "--no-tags", remote, sha)
    return _try("git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")


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


def local_state(statuses: list) -> dict:
    """Newest owner-posted status per required local suite -> success/pending/failure/missing."""
    state = {}
    for context in REQUIRED_LOCAL:
        mine = [s for s in statuses if s.get("context") == context
                and str((s.get("creator") or {}).get("id")) == OWNER_ID]
        # GitHub lists statuses newest first.
        state[context] = mine[0].get("state") or "missing" if mine else "missing"
    return state


def edge_state(runs: list, sha: str) -> str:
    """Latest edge publication run on ``sha`` -> success/pending/failed/missing."""
    mine = [r for r in runs if r.get("workflowName") == EDGE_WORKFLOW and r.get("headSha") == sha]
    if not mine:
        return "missing"
    run = mine[0]  # gh returns newest first
    if run.get("status") != "completed":
        return "pending"
    return "success" if run.get("conclusion") == "success" else (run.get("conclusion") or "failed")


def hosted_windows_runs(head: str) -> list:
    """Owner-dispatchable hosted Windows suite runs on ``head``, newest first."""
    if not head:
        return []
    data = _gh_json("api", f"repos/{REPOSITORY}/actions/workflows/{HOSTED_WINDOWS_WORKFLOW}/runs"
                           f"?head_sha={head}&event=workflow_dispatch&per_page=20")
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    return runs if isinstance(runs, list) else []


def hosted_windows_state(runs: list, head: str) -> tuple:
    """Newest owner-dispatched hosted Windows run on main at ``head`` -> (state, url).

    Only a manual run the owner started on main counts, the same authority as
    the owner-posted local/windows-suite status; anyone else's run is ignored.
    """
    def owner(run: dict) -> bool:
        actor = run.get("triggering_actor") or run.get("actor") or {}
        return str(actor.get("id")) == OWNER_ID

    mine = [r for r in runs if r.get("head_sha") == head and r.get("event") == "workflow_dispatch"
            and r.get("head_branch") == "main" and owner(r)]
    if not mine:
        return "missing", None
    run = mine[0]  # the API lists newest first
    if run.get("status") != "completed":
        return "pending", run.get("html_url")
    return ("success" if run.get("conclusion") == "success" else (run.get("conclusion") or "failure"),
            run.get("html_url"))


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
               smoke_dir: Optional[str] = None, scenario_rows: Optional[list] = None,
               skip_rows: Optional[list] = None) -> dict:
    """Live smoke verdict for the candidate (``pass``, ``block`` or ``skipped``).

    ``workers`` are the covered workers; when omitted, every worker with a
    result file for ``revision`` counts, and none at all blocks.
    """
    base = smoke_results.smoke_dir(smoke_dir)
    if scenario_rows is None:
        scenario_rows = smoke_scenarios.rows(smoke_scenarios.load())
    return smoke_gate_mod.evaluate(base, revision, workers, scenario_rows, skip_reason=skip_reason,
                                   skip_rows=skip_rows)


def collect(smoke_workers: Optional[list] = None, skip_smoke: Optional[str] = None,
            smoke_dir: Optional[str] = None, skip_windows: Optional[str] = None,
            skip_smoke_rows: Optional[list] = None) -> dict:
    blockers = []
    try:
        ref = public_main_ref()
    except local_ci.Failure as error:
        ref = None
        blockers.append(str(error))
    remote = ref.split("/", 1)[0] if ref else None
    remote_url = _try("git", "remote", "get-url", remote) if remote else None
    head, tag, tag_sha = "", None, None
    commits = []
    changed: list = []
    if ref:
        branch = ref.split("/", 1)[1]
        _try("git", "fetch", "--quiet", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{ref}")
        head = _try("git", "rev-parse", ref) or ""
        tags = release_tags(remote, ref)
        tag = latest_final_tag(tags)
        tag_sha = _commit(remote, tag, tags.get(tag)) if tag else None
        if tag and not tag_sha:
            blockers.append(f"Could not read the commit of {tag} on {remote}; fetch {ref} and retry.")
    if tag_sha and head:
        log = _try("git", "log", "--format=%h %s", f"{tag_sha}..{head}") or ""
        commits = [line for line in log.splitlines() if line]
        changed = (_try("git", "diff", "--name-only", f"{tag_sha}..{head}") or "").splitlines()
    runs = _gh_json("run", "list", "--repo", REPOSITORY, "--branch", "main", "--limit", "60",
                    "--json", "workflowName,status,conclusion,headSha") or []
    statuses = (_gh_json("api", f"repos/{REPOSITORY}/commits/{head}/statuses?per_page=100") or []) if head else []
    local = local_state(statuses)
    windows_source = {"source": "local" if local[WINDOWS_CONTEXT] == "success" else None, "url": None}
    hosted_state = None
    if local[WINDOWS_CONTEXT] != "success" and head:
        hosted_state, hosted_url = hosted_windows_state(hosted_windows_runs(head), head)
        if hosted_state == "success":
            local[WINDOWS_CONTEXT] = "success"
            windows_source = {"source": "hosted", "url": hosted_url}
    edge = edge_state(runs, head)
    gates = {
        "ANDROID_WORKERS_READY": _variable("ANDROID_WORKERS_READY"),
        "PLAY_SIGNING_READY": _variable("PLAY_SIGNING_READY", env="google-play-internal"),
    }
    if ref and not commits:
        blockers.append("Nothing new on main since the last release tag.")
    notes = []
    if windows_source["source"] == "hosted":
        notes.append(f"Windows full suite: passed on a GitHub-hosted Windows runner ({windows_source['url']}).")
    for context in REQUIRED_LOCAL:
        if local[context] == "success":
            continue
        if context == WINDOWS_CONTEXT and skip_windows:
            notes.append(f"Windows full suite not run on this release (--skip-windows): {skip_windows}")
            continue
        if context == WINDOWS_CONTEXT:
            hosted = f" (the hosted run on it is {hosted_state})" if hosted_state not in (None, "missing") else ""
            blockers.append(
                f"{context} on {head[:9]} is {local[context]}: run {local_ci.WINDOWS_FULL_HINT} "
                f"on DesktopWorker (Windows) on a clean checkout of main, {HOSTED_WINDOWS_HINT}{hosted}.")
            continue
        run = "`python scripts/local_ci.py full --attest` on a Linux worker"
        blockers.append(f"{context} on {head[:9]} is {local[context]}: run {run} on a clean checkout of main.")
    smoke = smoke_gate(head, smoke_workers, skip_smoke, smoke_dir, skip_rows=skip_smoke_rows)
    blockers += [f"Live smoke: {b}" for b in smoke["blockers"]]
    return {
        "repository": REPOSITORY,
        "main_ref": ref,
        "remote": {"name": remote, "url": remote_url},
        "main_head": head,
        "last_release": {"tag": tag, "sha": tag_sha},
        "suggested_tag": next_patch(tag),
        "commits_since_release": len(commits),
        "commit_subjects": commits[:30],
        "local_suites_on_main_head": local,
        "windows_suite": windows_source,
        "edge_published": edge == "success",
        "platform_builds": platforms_needing_builds(changed),
        "play_gates": gates,
        "smoke": smoke,
        "release_notes_lines": smoke["release_notes_lines"] + notes,
        "blockers": blockers,
        "ready": not blockers,
    }


def render(report: dict) -> str:
    remote = report.get("remote") or {}
    source = (f"{report.get('main_ref')} (remote '{remote.get('name')}' = {report.get('repository')})"
              if report.get("main_ref") else f"no remote for {report.get('repository')}")
    lines = [
        f"Candidate: {source} {report['main_head'][:9]}  "
        f"(last release {report['last_release']['tag']}, {report['commits_since_release']} commits since)",
        f"Suggested tag: {report['suggested_tag']}",
        "Local suites on candidate: " + ", ".join(f"{k}={v}" for k, v in report["local_suites_on_main_head"].items())
        + f"; edge published: {'yes' if report['edge_published'] else 'no'}",
        "Play gates: " + ", ".join(f"{k}={v}" for k, v in report["play_gates"].items()),
    ]
    smoke = report.get("smoke")
    if smoke:
        lines.append(f"Live smoke: {smoke['verdict']}"
                     + (f" ({smoke['skip_reason']})" if smoke.get("skip_reason") else ""))
        lines += [f"  owner to-do: {t}" for t in smoke.get("owner_todo", [])]
        lines += [f"  skipped blocker: {b}" for b in smoke.get("would_block", [])]
    lines += [f"  release notes must say: {line}" for line in report.get("release_notes_lines", [])]
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
    parser.add_argument("--skip-smoke-rows", nargs=2, action="append", metavar=("ROWS", "REASON"),
                        help="smoke rows that may stay not run (two_worker, or comma-separated row ids); "
                             "failed rows still block and the reason goes into the release notes")
    parser.add_argument("--skip-windows", metavar="REASON", default=None,
                        help="tag without a Windows full-suite pass; the reason goes into the release notes")
    parser.add_argument("--smoke-dir", default=None,
                        help="smoke results folder (default $AI_SHUFFLE_SMOKE_DIR or <data dir>/smoke)")
    args = parser.parse_args(argv)
    report = collect(args.smoke_worker, args.skip_smoke, args.smoke_dir, args.skip_windows, args.skip_smoke_rows)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
