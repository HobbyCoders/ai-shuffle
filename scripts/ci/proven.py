"""What a green run already proved, so a later run of the same code can skip it.

A pull request's checks run on GitHub's merge of the branch into main. When an
area's checks all pass there, ``record-pass.yml`` uploads a tiny artifact named
``proven-<area>-<tree>``: the tree is the content of every file in that merge.
A squash merge onto an unmoved main produces the same tree, so main-suite finds
the record and skips that area instead of running it a second time.

The tree covers the workflows and tests too, so a record can only stand for
the same checks on the same files. Nothing here lists what an area depends on.
A record counts only when it came from the area's own checks on the pull
request that was merged as this very commit (never a fork's, never another
branch's run), and only a push to main looks: scheduled and dispatched runs
prove everything again.

    python scripts/ci/proven.py check --areas backend,fluid >> "$GITHUB_OUTPUT"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com"
PREFIX = "proven"
MAIN_SUITE = "main-suite.yml"


def request(path, token, urlopen=urllib.request.urlopen):
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-shuffle-ci"}
    with urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def head_tree(root: Path = ROOT) -> str:
    """The tree of the checked-out commit: one id for the content of every file."""
    return subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip()


def record_name(area: str, tree: str, key: str = "") -> str:
    """Must match the name ``.github/workflows/record-pass.yml`` uploads."""
    return f"{PREFIX}-{area}-{tree}" + (f"-{key}" if key else "")


# The workflow whose checks stand behind each area's record.
WORKFLOWS = {
    "backend": "ci-backend.yml", "fluid": "ci-fluid.yml", "windows": "windows-suite.yml",
    "browser-workspace": "ci-browser-workspace.yml", "container": "container-postgres.yml",
    "identity": "ci-instance-identity.yml", "desktop-source": "desktop-source-checks.yml",
    "javascript-checks": "ci-frontend.yml", "journeys": "ci-frontend.yml",
}
BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}


def merged_heads(repository: str, commit: str, token: str, call=request) -> set[str]:
    """Head commits of this repository's pull requests that were merged to main as ``commit``."""
    heads = set()
    for pull in call(f"/repos/{repository}/commits/{commit}/pulls?per_page=100", token) or []:
        head, base = pull.get("head") or {}, pull.get("base") or {}
        if (pull.get("merged_at") and pull.get("merge_commit_sha") == commit and base.get("ref") == "main"
                and (head.get("repo") or {}).get("full_name") == repository and head.get("sha")):
            heads.add(head["sha"])
    return heads


def passed(run_id, area: str, heads: set[str], repository: str, token: str, call=request) -> bool:
    """True when that run is the area's own pull request workflow, on a merged head, with nothing failed."""
    run = call(f"/repos/{repository}/actions/runs/{run_id}", token)
    if (run.get("event") != "pull_request" or run.get("head_sha") not in heads
            or run.get("path") != f".github/workflows/{WORKFLOWS[area]}"):
        return False
    data = call(f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
    jobs = data.get("jobs") or []
    if not jobs or data.get("total_count") != len(jobs):
        return False
    return not any(job.get("conclusion") in BAD for job in jobs)


def exists(name: str, repository: str, token: str, call=request, *, area: str, commit: str | None = None) -> bool:
    """True when the checks of the pull request merged as ``commit`` left a live record with that name.

    A name alone proves nothing: any run in the repository can upload an
    artifact. The record counts only when its run is the area's own workflow,
    started by a pull request of this repository (never a fork) whose head was
    then merged to main as exactly this commit, and no job of that run failed.
    """
    commit = commit or os.environ.get("GITHUB_SHA", "")
    if area not in WORKFLOWS or not commit:
        return False
    heads = None
    data = call(f"/repos/{repository}/actions/artifacts?per_page=100&name={urllib.parse.quote(name)}", token)
    for artifact in data.get("artifacts") or []:
        run = artifact.get("workflow_run") or {}
        own = run.get("repository_id") is not None and run.get("repository_id") == run.get("head_repository_id")
        if artifact.get("name") != name or artifact.get("expired") is not False or not own or not run.get("id"):
            continue
        if heads is None:
            heads = merged_heads(repository, commit, token, call)
        if run.get("head_sha") in heads and passed(run["id"], area, heads, repository, token, call):
            return True
    return False


def green_main_commits(repository: str, token: str, call=request) -> list[str]:
    """Commits whose main-suite run on main succeeded, newest run first."""
    data = call(f"/repos/{repository}/actions/workflows/{MAIN_SUITE}/runs"
                "?branch=main&status=success&per_page=30", token)
    return [run["head_sha"] for run in data.get("workflow_runs") or []
            if run.get("head_branch") == "main" and run.get("conclusion") == "success" and run.get("head_sha")]


def environment() -> tuple[str, str] | None:
    """(repository, token) when a push to main may look for records; else None."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        return None
    return (repository, token) if repository and token else None


def check(areas: list[str], root: Path = ROOT, call=request) -> dict[str, bool]:
    """Which areas a record covers. Any doubt or error means not proven: run it."""
    found = dict.fromkeys(areas, False)
    where = environment()
    if where is None:
        return found
    try:
        tree = head_tree(root)
        for area in areas:
            found[area] = exists(record_name(area, tree), *where, call=call, area=area)
    except Exception as error:  # noqa: BLE001 - a lookup must never fail the run
        print(f"[proven] lookup failed, running everything not yet found: {error}", file=sys.stderr)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("check",))
    parser.add_argument("--areas", required=True, help="comma-separated area names")
    args = parser.parse_args(argv)
    found = check([area for area in args.areas.split(",") if area])
    lines = ["### Already proven by a pull request", ""]
    for area, proven in found.items():
        print(f"{area}={'true' if proven else 'false'}")
        lines.append(f"- `{area}`: {'skipped, a pull request passed it on these exact files' if proven else 'runs'}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
