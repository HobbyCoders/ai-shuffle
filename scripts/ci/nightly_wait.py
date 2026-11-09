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
import calendar
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
MAIN_EVENTS = ("push", "workflow_dispatch")
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
    sha = api(f"commits/{commit}")["sha"]
    if not contains(api, sha, "main"):
        raise ValueError(f"{sha[:9]} is not on main; merge it first")
    return sha


def contains(api, commit, revision, cache=None):
    """True when ``revision`` is ``commit`` or a descendant of it.

    Ancestry between two SHAs never changes, so a per-wait cache is safe.
    """
    if commit == revision:
        return True
    if cache is not None and revision in cache:
        return cache[revision]
    result = api(f"compare/{commit}...{revision}").get("status") in ("ahead", "identical")
    if cache is not None:
        cache[revision] = result
    return result


def _runs(api, workflow, status, events=None):
    listing = api(f"actions/workflows/{workflow}/runs?branch=main&status={status}&per_page=100")
    runs = [run for run in listing.get("workflow_runs") or []
            if run.get("head_branch") == "main" and (events is None or run.get("event") in events)]
    return sorted(runs, key=lambda run: run.get("id", 0), reverse=True)


def _active(api, workflow, events=None):
    # Asked for by status, never read off the newest page: a re-run keeps its old
    # ID and position, so an active run can sit behind 100 newer finished ones.
    return [run for status in ACTIVE for run in _runs(api, workflow, status, events)]


def suite_state(api, commit, *, now, cache=None):
    """(state, detail). States: testing, publishing, unpublished, failed, none.

    A run covers the commit when its head is the commit or a descendant. That is
    asked of each run's own head, never of a commit list read earlier: a merge
    landing mid-poll must not make its replacement run look unrelated.
    """
    cache = {} if cache is None else cache
    for run in _active(api, "main-suite.yml", MAIN_EVENTS):
        if contains(api, commit, run["head_sha"], cache):
            return "testing", f"main-suite is {run['status'].replace('_', ' ')} on {run['head_sha'][:9]}: {run['html_url']}"
    finished, seen = [], set()
    for run in _runs(api, "main-suite.yml", "completed", MAIN_EVENTS):
        sha = run["head_sha"]
        if sha in seen:
            continue  # newest run per commit, as the publication gate reads it
        seen.add(sha)
        if not contains(api, commit, sha, cache):
            break  # main is linear: every older run is on an older revision
        finished.append(run)
    passed = next((run for run in finished if run.get("conclusion") == "success"), None)
    age = now - _epoch(passed.get("updated_at")) if passed else 0
    if passed and age < PUBLISH_SECONDS:
        return "publishing", f"main-suite passed on {passed['head_sha'][:9]}; waiting for the publisher"
    # The publisher also runs on a schedule, on owner dispatch and from an owner
    # local pass: while one is in flight the commit may still ship.
    publishers = _active(api, "desktop-source.yml")
    if publishers:
        return "publishing", f"A nightly publisher run is in flight: {publishers[0]['html_url']}"
    if passed:
        return "unpublished", (f"main-suite passed on {passed['head_sha'][:9]} {int(age // 60)} minutes ago but no nightly "
                               f"contains it. Check the publisher's latest run for a refusal: {PUBLISHER_URL}")
    # A replaced waiting run ("cancelled", no jobs) is not a failure; only report
    # a run that ran. A run stopped on its first failed job is also "cancelled".
    run = next((run for run in finished if run.get("conclusion") != "cancelled" or _started(api, run)), None)
    if run:
        ended = ("was stopped after a job failed (or cancelled by hand)" if run.get("conclusion") == "cancelled"
                 else f"ended {run.get('conclusion')}")
        return "failed", (f"The newest main-suite run covering this commit {ended} "
                          f"on {run['head_sha'][:9]} and nothing newer is waiting: {run['html_url']}\n"
                          "The nightly waits for the next green main run: fix or re-run it.")
    return "none", f"No main-suite run in flight covers this commit yet: {RUNS_URL}"


def _started(api, run):
    """True when a cancelled run had jobs: a replaced waiting run has none."""
    return bool(api(f"actions/runs/{run['id']}/jobs?per_page=1").get("total_count"))


def _epoch(stamp):
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0.0


def wait(api, commit, *, fetch=nightly.fetch, timeout=7200, interval=60, once=False,
         clock=time.time, sleep=time.sleep, say=print):
    deadline = clock() + timeout
    last = stalled = None
    failed = 0
    cache = {}
    version = "unknown"
    while True:
        try:
            code, last, stalled, version = _poll(api, commit, fetch, last, stalled, clock, say, cache)
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


def _poll(api, commit, fetch, last, stalled, clock, say, cache):
    revision, version = published(fetch)
    if contains(api, commit, revision, cache):
        say(f"Published: nightly {version} ({revision[:9]}) contains {commit[:9]}.")
        return 0, last, stalled, version
    state, detail = suite_state(api, commit, now=clock(), cache=cache)
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
