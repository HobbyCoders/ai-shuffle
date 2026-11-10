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


def merged_pulls(repository: str, commit: str, token: str, call=request) -> dict[str, dict]:
    """This repository's pull requests merged to main as ``commit``, by head commit: number and branch."""
    pulls = {}
    for pull in call(f"/repos/{repository}/commits/{commit}/pulls?per_page=100", token) or []:
        head, base = pull.get("head") or {}, pull.get("base") or {}
        if (pull.get("merged_at") and pull.get("merge_commit_sha") == commit and base.get("ref") == "main"
                and (head.get("repo") or {}).get("full_name") == repository
                and head.get("sha") and head.get("ref") and pull.get("number")):
            pulls[head["sha"]] = {"number": pull["number"], "ref": head["ref"]}
    return pulls


def only_pull(pull: dict, repository: str, token: str, call=request) -> bool:
    """True when the merged pull request is the only one its branch ever opened.

    A pull request's checks use the workflows of its base. A second pull
    request from the same branch against another base would run unreviewed
    workflows on the same head commit, and GitHub's API does not say which
    pull request started a run. So a branch with any other pull request,
    open or closed, against any base, proves nothing.
    """
    owner = repository.split("/", 1)[0]
    head = urllib.parse.quote(f"{owner}:{pull['ref']}")
    found = call(f"/repos/{repository}/pulls?state=all&per_page=100&head={head}", token)
    return isinstance(found, list) and [item.get("number") for item in found] == [pull["number"]]


def never_retargeted(pull: dict, repository: str, token: str, call=request) -> bool:
    """True when the pull request's base was main from the start, as far as its whole history shows."""
    events = call(f"/repos/{repository}/issues/{pull['number']}/events?per_page=100", token)
    if not isinstance(events, list) or len(events) >= 100:
        return False  # no history, or more than one page of it: unknown
    # base_ref_changed, automatic_base_change_succeeded and any other event about
    # the base: none may appear, whatever GitHub names the next one.
    return all(isinstance(event, dict) and isinstance(event.get("event"), str)
               and "base" not in event["event"].lower() for event in events)


def ran_on(run: dict, pull: dict, tree: str, repository: str, token: str, call=request) -> bool:
    """True when GitHub's own record of the run says it used the pull request's merge with exactly these files.

    The record's name is written by the run and could say anything. GitHub
    itself notes which commit a run took ``record-pass.yml`` from: for the
    pull request's own checks that is its test merge commit.
    """
    path = f"{repository}/.github/workflows/record-pass.yml@"
    used = [item for item in run.get("referenced_workflows") or []
            if isinstance(item, dict) and str(item.get("path", "")).startswith(path)]
    if len(used) != 1 or used[0].get("ref") != f"refs/pull/{pull['number']}/merge":
        return False
    commit = used[0].get("sha")
    if not isinstance(commit, str) or used[0]["path"] != path + commit:
        return False
    return (call(f"/repos/{repository}/git/commits/{commit}", token).get("tree") or {}).get("sha") == tree


WAITING = {"queued", "in_progress", "waiting", "pending", "requested"}


def passed(run_id, area: str, head: str, pull: dict, tree: str, repository: str, token: str, call=request) -> bool:
    """True when that run is the area's own pull request workflow, on these files, with nothing failed."""
    run = call(f"/repos/{repository}/actions/runs/{run_id}", token)
    if (run.get("event") != "pull_request" or run.get("head_sha") != head or run.get("head_branch") != pull["ref"]
            or (run.get("head_repository") or {}).get("full_name") != repository
            or run.get("path") != f".github/workflows/{WORKFLOWS[area]}"
            or not ran_on(run, pull, tree, repository, token, call)):
        return False
    data = call(f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs or data.get("total_count") != len(jobs):
        return False
    # Other jobs may still be running when the record is read; anything else must have passed.
    return all(isinstance(job, dict) and (
        (job.get("status") == "completed" and job.get("conclusion") in ("success", "skipped"))
        or (job.get("status") in WAITING and job.get("conclusion") is None)) for job in jobs)


def exists(name: str, repository: str, token: str, call=request, *, area: str, tree: str,
           commit: str | None = None) -> bool:
    """True when the checks of the pull request merged as ``commit`` left a live record with that name.

    The rule: every workflow in the run came from main plus the reviewed,
    merged head, and the run used exactly the files of ``tree``. So the record
    counts only when all of this holds, and any doubt means it does not:

    - its run is the area's own workflow, started by a pull request of this
      repository (never a fork) whose head was merged to main as this commit;
    - that pull request is the only one its branch ever opened and its base
      was never changed, so no run on that head used another base's workflows;
    - GitHub's own record of the run names that pull request's test merge
      commit, and that commit holds exactly ``tree``;
    - no job of the run failed, was cancelled or has an unknown state.
    """
    commit = commit or os.environ.get("GITHUB_SHA", "")
    if area not in WORKFLOWS or not commit or not tree:
        return False
    pulls, trusted = None, {}
    data = call(f"/repos/{repository}/actions/artifacts?per_page=100&name={urllib.parse.quote(name)}", token)
    for artifact in data.get("artifacts") or []:
        run = artifact.get("workflow_run") or {}
        own = run.get("repository_id") is not None and run.get("repository_id") == run.get("head_repository_id")
        if artifact.get("name") != name or artifact.get("expired") is not False or not own or not run.get("id"):
            continue
        if pulls is None:
            pulls = merged_pulls(repository, commit, token, call)
        head = run.get("head_sha")
        pull = pulls.get(head)
        if pull is None or run.get("head_branch") != pull["ref"]:
            continue
        if head not in trusted:
            trusted[head] = (only_pull(pull, repository, token, call)
                             and never_retargeted(pull, repository, token, call))
        if trusted[head] and passed(run["id"], area, head, pull, tree, repository, token, call):
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
            found[area] = exists(record_name(area, tree), *where, call=call, area=area, tree=tree)
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
