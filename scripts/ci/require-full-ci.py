"""Fail closed when expensive PR checks were deferred or did not pass."""
import json
import os
from pathlib import Path


def readiness_error(requested, results):
    if requested != "true":
        return (
            "Full CI has not been requested. Add the ci:full PR label when ready "
            "to merge; leave it applied so every subsequent commit is rechecked."
        )
    if not isinstance(results, dict) or not results:
        return "Required job results are missing."
    if any(not isinstance(job, dict) or job.get("result") != "success" for job in results.values()):
        return "Required CI did not pass. All prerequisite jobs must succeed on this revision."
    return None


def main():
    try:
        results = json.loads(os.environ.get("CI_REQUIRED_RESULTS", "null"))
    except json.JSONDecodeError:
        results = None
    error = readiness_error(os.environ.get("CI_FULL_REQUESTED"), results)
    message = error or "All required full checks passed for this revision."
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")
    print(("::error::" if error else "") + message)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
