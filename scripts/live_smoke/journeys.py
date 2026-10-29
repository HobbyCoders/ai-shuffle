"""CI evidence for live-smoke rows of kind ``journey``.

A journey row is covered by UI journeys (``frontend/tests/journeys/``), which
run on every push to main in ``main-suite.yml`` (its ``javascript`` leg calls
``ci-frontend.yml``, whose ``UI journeys (k/N)`` shards run every journey
file). The evidence for a candidate revision is one main-suite run on main at
exactly that commit whose ``UI journeys`` shards are all present and all
succeeded. Anything else (no run, a run still going, a failed or missing
shard, GitHub unreachable) is not evidence, and the gate blocks.

Stdlib only. :func:`fetch` takes the ``gh`` JSON reader so callers (and
tests) choose how GitHub is reached.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable

REPOSITORY = "HobbyCoders/ai-shuffle"
WORKFLOW = "main-suite.yml"
SHARD = re.compile(r"UI journeys \((\d+)/(\d+)\)")


def _gh_json(*args: str):
    try:
        out = subprocess.check_output(("gh", *args), text=True, stderr=subprocess.DEVNULL)
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return None


def shards_state(jobs: list) -> str:
    """``success`` when every ``UI journeys (k/N)`` shard is there and succeeded."""
    shards = {}
    for job in jobs:
        match = SHARD.search(str(job.get("name") or ""))
        if match:
            shards[(int(match.group(1)), int(match.group(2)))] = job
    if not shards:
        return "missing"
    totals = {total for _, total in shards}
    total = max(totals)
    if len(totals) != 1 or {k for k, _ in shards} != set(range(1, total + 1)):
        return "missing"
    if any(job.get("status") != "completed" for job in shards.values()):
        return "pending"
    if all(job.get("conclusion") == "success" for job in shards.values()):
        return "success"
    return "failure"


def state_from(runs: list, revision: str, jobs_for: Callable[[dict], list | None]) -> dict:
    """``{"state", "url"}`` from main-suite runs (newest first) at ``revision``.

    A run counts only when it ran on main at exactly ``revision``. Any such
    run whose journey shards all succeeded is the evidence; otherwise the
    newest run's state is reported.
    """
    mine = [r for r in runs if (r.get("head_sha") or "").lower() == revision.lower()
            and r.get("head_branch") == "main"]
    if not mine:
        return {"state": "missing", "url": None}
    newest = None
    for run in mine:
        jobs = jobs_for(run)
        state = "unknown" if jobs is None else shards_state(jobs)
        if state == "success":
            return {"state": state, "url": run.get("html_url")}
        if newest is None:
            newest = {"state": state, "url": run.get("html_url")}
    return newest


def fetch(revision: str, gh_json: Callable[..., object] | None = None) -> dict:
    """Journey evidence for ``revision`` from GitHub: ``{"state", "url"}``.

    ``state`` is ``success``, ``pending``, ``failure``, ``missing`` (no run at
    the revision, or a run without every journey shard) or ``unknown``
    (GitHub could not be read).
    """
    gh_json = gh_json or _gh_json
    data = gh_json("api", f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs"
                          f"?head_sha={revision}&per_page=20")
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return {"state": "unknown", "url": None}

    def jobs_for(run: dict) -> list | None:
        jobs: list = []
        for page in range(1, 6):  # main-suite has about 50 jobs; stop well past that
            found = gh_json("api", f"repos/{REPOSITORY}/actions/runs/{run.get('id')}/jobs"
                                   f"?per_page=100&page={page}")
            batch = found.get("jobs") if isinstance(found, dict) else None
            if not isinstance(batch, list):
                return None
            jobs += batch
            if len(batch) < 100:
                break
        return jobs

    return state_from(runs, revision, jobs_for)
