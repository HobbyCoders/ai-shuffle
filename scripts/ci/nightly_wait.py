"""Wait until a main commit is in a published nightly. Read-only.

    python scripts/ci/nightly_wait.py <commit>      # or: --pr <number>

Follow the commit, never one main-suite run. main-suite keeps one running and
one waiting run; a newer merge replaces the waiting run, which GitHub then
reports as "cancelled". That run never started and nothing is lost: the newer
run tests a main revision that contains the commit, and the publisher ships
the newest proven revision with everything before it. A replaced run is
therefore not an event and is not reported here.

Exit codes: 0 the published nightly contains the commit; 1 nothing in flight
can publish it (act on the printed reason); 2 timed out while still in flight;
3 ``--once`` and not published yet. One line is printed per change of state.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nightly = _load("nightly_changed")
gate = _load("edge_publication_gate")

RUNS_URL = f"https://github.com/{gate.REPOSITORY}/actions/workflows/main-suite.yml"
PUBLISHER_URL = f"https://github.com/{gate.REPOSITORY}/actions/workflows/desktop-source.yml"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
# A pushed commit's run takes a moment to register, and a failed run may be
# re-run: a state with nothing in flight must persist this long before exit 1.
SETTLE_SECONDS = 300
# The publisher follows a green run within minutes (signing takes 2-12).
PUBLISH_SECONDS = 1800
# A long wait survives brief GitHub or pointer outages (each read already retries).
READ_ERRORS = (subprocess.CalledProcessError, subprocess.TimeoutExpired, urllib.error.URLError, TimeoutError)
MAX_FAILED_POLLS = 5


def published(fetch=nightly.fetch):
    """(revision, version) of the live nightly pointer. Information, not proof."""
    manifest = json.loads(base64.b64decode(json.loads(fetch())["payload"], validate=True))
    if manifest.get("channel") not in ("edge", "nightly"):
        raise ValueError("Expected nightly pointer")
    revision = manifest["revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid published revision")
    return revision, str(manifest.get("version") or revision[:9])


def resolve(api, commit=None, pr=None):
    """Full SHA of the commit on main to follow."""
    if pr is not None:
        pull = api(f"pulls/{pr}")
        if not pull.get("merged") or not pull.get("merge_commit_sha"):
            raise ValueError(f"PR #{pr} is not merged; nothing to wait for")
        if (pull.get("base") or {}).get("ref") != "main":
            raise ValueError(f"PR #{pr} did not merge into main")
        return pull["merge_commit_sha"]
    if not re.fullmatch(r"[a-f0-9]{7,40}", commit or ""):
        raise ValueError("Give a commit SHA (7-40 hex characters) or --pr <number>")
    return api(f"commits/{commit}")["sha"]


def contains(api, commit, revision):
    """True when ``revision`` is ``commit`` or a descendant of it."""
    return commit == revision or api(f"compare/{commit}...{revision}").get("status") in ("ahead", "identical")


def covering(api, commit):
    """The commit and every later main commit: a pass on any of them proves it."""
    comparison = api(f"compare/{commit}...main")
    if comparison.get("status") not in ("ahead", "identical"):
        raise ValueError(f"{commit[:9]} is not on main; merge it first")
    # GitHub returns at most 250 commits here. Runs on later ones are not
    # recognised, so a very long backlog can only end in exit 1/2, never a false 0.
    return {commit} | {item["sha"] for item in comparison.get("commits") or []}


def suite_state(api, commit, *, now):
    """(state, detail). States: testing, publishing, unpublished, failed, none."""
    shas = covering(api, commit)
    listing = api("actions/workflows/main-suite.yml/runs?branch=main&per_page=100")
    newest = {}
    for run in listing.get("workflow_runs") or []:
        sha = run.get("head_sha")
        if (sha in shas and run.get("head_branch") == "main"
                and run.get("event") in ("push", "workflow_dispatch")
                and run.get("id", 0) > newest.get(sha, {}).get("id", 0)):
            newest[sha] = run
    runs = sorted(newest.values(), key=lambda run: run["id"], reverse=True)
    active = [run for run in runs if run.get("status") in ACTIVE]
    if active:
        run = active[0]
        return "testing", f"main-suite is {run['status'].replace('_', ' ')} on {run['head_sha'][:9]}: {run['html_url']}"
    passed = [run for run in runs if run.get("status") == "completed" and run.get("conclusion") == "success"]
    if passed:
        run = passed[0]
        age = now - _epoch(run.get("updated_at"))
        if age < PUBLISH_SECONDS:
            return "publishing", f"main-suite passed on {run['head_sha'][:9]}; waiting for the publisher"
        return "unpublished", (f"main-suite passed on {run['head_sha'][:9]} {int(age // 60)} minutes ago but no nightly "
                               f"contains it. Check the publisher's latest run for a refusal: {PUBLISHER_URL}")
    if runs:
        run = runs[0]
        return "failed", (f"The newest main-suite run covering this commit ended {run.get('conclusion') or run.get('status')} "
                          f"on {run['head_sha'][:9]} and nothing newer is waiting: {run['html_url']}\n"
                          "The nightly waits for the next green main run: fix or re-run it.")
    return "none", f"No main-suite run covers this commit yet: {RUNS_URL}"


def _epoch(stamp):
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        return 0.0


def wait(api, commit, *, fetch=nightly.fetch, timeout=7200, interval=60, once=False,
         clock=time.time, sleep=time.sleep, say=print):
    deadline = clock() + timeout
    last = stalled = None
    failed = 0
    version = "unknown"
    while True:
        try:
            code, last, stalled, version = _poll(api, commit, fetch, last, stalled, clock, say)
            failed = 0
        except READ_ERRORS as error:
            failed += 1
            if once or failed >= MAX_FAILED_POLLS:
                raise
            code = None
            say(f"Could not read GitHub or the nightly pointer ({type(error).__name__}); retrying.")
        if code is not None:
            return code
        if once:
            return 3
        if last in ("unpublished", "failed", "none") and not failed and clock() - stalled >= SETTLE_SECONDS:
            say(f"Blocked: {commit[:9]} is not in a nightly and nothing in flight will publish it.")
            return 1
        if clock() >= deadline:
            say(f"Timed out: {commit[:9]} is still not in a nightly (live: {version}).")
            return 2
        sleep(interval)


def _poll(api, commit, fetch, last, stalled, clock, say):
    revision, version = published(fetch)
    if contains(api, commit, revision):
        say(f"Published: nightly {version} ({revision[:9]}) contains {commit[:9]}.")
        return 0, last, stalled, version
    state, detail = suite_state(api, commit, now=clock())
    if state != last:
        say(detail if state != "testing" else f"Not in a nightly yet (live: {version}). {detail}")
        last, stalled = state, clock()
    return None, last, stalled, version


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("commit", nargs="?", help="commit on main (the squash-merge commit)")
    parser.add_argument("--pr", type=int, help="merged pull request number, instead of a commit")
    parser.add_argument("--timeout-minutes", type=float, default=120)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--once", action="store_true", help="report the current state and exit")
    args = parser.parse_args(argv)
    if (args.commit is None) == (args.pr is None):
        parser.error("give exactly one of <commit> or --pr")
    if args.timeout_minutes <= 0 or args.interval_seconds < 5:
        parser.error("--timeout-minutes must be positive and --interval-seconds at least 5")
    api = gate.GitHub()
    try:
        commit = resolve(api, args.commit, args.pr)
        return wait(api, commit, timeout=args.timeout_minutes * 60, interval=args.interval_seconds, once=args.once)
    except ValueError as error:
        print(f"nightly_wait: {error}", file=sys.stderr)
        return 1
    except READ_ERRORS as error:
        # gh's own diagnostic is already on stderr.
        print(f"nightly_wait: could not read GitHub or the nightly pointer ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
