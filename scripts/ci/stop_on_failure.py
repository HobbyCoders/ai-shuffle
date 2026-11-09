"""Stop a main-suite run as soon as one required job fails.

A run with a failed job can never publish, yet it used to keep every other
job going for another 20 minutes or more and hold the single main-suite lane
while the fix waited behind it. This watcher polls the run's own jobs, names
the first failures, and cancels the run. Advisory: it never fails the run
itself, and a run it cannot read simply finishes the slow way.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
FAILED = ("failure", "timed_out")
# Jobs whose failure does not fail the run: the optional UI bundle cache, the
# recovered-journey reporter and this watcher.
ADVISORY = ("source-ui /", "Report recovered UI journeys", "Stop on first failure")
GATE = "Main suite"
POLL_SECONDS = 30
MAX_FAILED_READS = 5


def request(method, path, token, urlopen=urllib.request.urlopen):
    call = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(call, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def jobs_of(repository, run, attempt, token, call=request):
    found, page = [], 1
    while True:
        data = call("GET", f"/repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}", token)
        batch = data.get("jobs") or []
        found.extend(batch)
        if not batch or len(found) >= data.get("total_count", 0):
            return found
        page += 1


def is_advisory(name):
    return any(name.startswith(prefix) for prefix in ADVISORY)


def failures(jobs):
    return [job for job in jobs if job.get("conclusion") in FAILED and not is_advisory(job.get("name", ""))]


def gate_finished(jobs):
    return any(job.get("name") == GATE and job.get("status") == "completed" for job in jobs)


def nothing_to_watch(jobs):
    """True when no job that can fail the run is queued or running."""
    return not any(job.get("status") != "completed" and not is_advisory(job.get("name", "")) for job in jobs)


def report(failed, out=sys.stdout, summary_path=None):
    lines = [f"- {job['name']}: {job.get('html_url', '')}" for job in failed]
    print("::error title=Main suite stopped early::" + "; ".join(job["name"] for job in failed), file=out)
    print("\n".join(lines), file=out, flush=True)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("### Stopped on the first failure\n\nThese jobs failed, so the rest of the run was cancelled:\n\n")
            handle.write("\n".join(lines) + "\n")


def watch(repository, run, attempt, token, call=request, sleep=time.sleep, out=sys.stdout, summary_path=None):
    """Return "cancelled", "finished" or "unreadable"."""
    unreadable = idle = 0
    while True:
        try:
            jobs = jobs_of(repository, run, attempt, token, call)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            unreadable += 1
            print(f"could not read the run's jobs ({error})", file=out, flush=True)
            if unreadable >= MAX_FAILED_READS:
                return "unreadable"
            sleep(POLL_SECONDS)
            continue
        unreadable = 0
        failed = failures(jobs)
        if failed:
            report(failed, out, summary_path)
            call("POST", f"/repos/{repository}/actions/runs/{run}/cancel", token)
            return "cancelled"
        if gate_finished(jobs):
            return "finished"
        # A re-run of only this watcher has no gate in its attempt: the suites
        # passed earlier. Seen twice, so jobs still registering are not missed.
        idle = idle + 1 if str(attempt) != "1" and nothing_to_watch(jobs) else 0
        if idle >= 2:
            return "finished"
        sleep(POLL_SECONDS)


def main():
    result = watch(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"],
                   os.environ.get("GITHUB_RUN_ATTEMPT", "1"), os.environ["GH_TOKEN"],
                   summary_path=os.environ.get("GITHUB_STEP_SUMMARY"))
    print(f"watch ended: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
