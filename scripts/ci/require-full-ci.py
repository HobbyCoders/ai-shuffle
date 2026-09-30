"""Fail closed when required PR checks did not pass.

Expensive platform jobs (emulators, Windows installs, real browsers) are
deferred on pull requests unless the PR carries the ci:full label. A deferred
job may be skipped; every other job must succeed. With the label, or on a
manual or release run, every job must succeed.
"""
import json
import os
from pathlib import Path


def readiness_error(requested, results, deferrable=()):
    if not isinstance(results, dict) or not results:
        return "Required job results are missing."
    full = requested == "true"
    for name, job in results.items():
        if not isinstance(job, dict):
            return "Required job results are missing."
        result = job.get("result")
        if result == "success":
            continue
        if not full and result == "skipped" and name in deferrable:
            continue
        return "Required CI did not pass. All prerequisite jobs must succeed on this revision."
    return None


def parse_json(name, default):
    try:
        return json.loads(os.environ.get(name, "null")) or default
    except json.JSONDecodeError:
        return None


def main():
    results = parse_json("CI_REQUIRED_RESULTS", None)
    deferrable = parse_json("CI_DEFERRABLE_JOBS", [])
    if not isinstance(deferrable, list):
        results = None
    requested = os.environ.get("CI_FULL_REQUESTED")
    error = readiness_error(requested, results, set(deferrable or []))
    if error:
        message = error
    elif requested == "true":
        message = "All required full checks passed for this revision."
    else:
        message = ("Fast checks passed. Platform checks were deferred; add the ci:full "
                   "label or run this workflow manually to include them.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")
    print(("::error::" if error else "") + message)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
