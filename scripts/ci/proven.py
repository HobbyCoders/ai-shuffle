"""The UI journeys a pull request already passed, so main need not run them again.

A pull request's checks run on GitHub's merge of the branch into main. When its
selected UI journeys all pass there, ``record-pass.yml`` uploads a tiny artifact
named ``proven-journeys-<tree>-<selection>``: the tree is the content of every
file in that merge. A squash merge onto an unmoved main produces the same tree,
so the run on main finds the record and starts no journey shard.

The tree covers the workflows and tests too, so a record can only stand for
the same journeys on the same files. A record counts only when it came from
the checks of the pull request that was merged as this very commit (never a
fork's, never another branch's run), and only a push to main looks: scheduled
and dispatched runs run every journey again.

With a merge queue on main no record is needed for the queued merge: the
queue's own run checked the very commit main then moves to, so the push that
follows finds that run by the commit (``queue_passed``). Because the queue
runs the full selection, a pull request's own runs then keep to the journeys a
rule names for its files (``merge_queue_guards``).

Every other check keeps its record by the files it reads: scripts/ci/plan.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com"
PREFIX = "proven"
WORKFLOW = "ci.yml"
MAIN_EVENTS = ("push", "schedule", "workflow_dispatch")
# GitHub's merge queue tests a pull request on a branch of this form, named for
# the branch the group is headed for, and starts the run with this event.
QUEUE_EVENT = "merge_group"
QUEUE_BRANCH = "gh-readonly-queue/main/"


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


# The jobs that stand behind each area's record: they start with this, inside
# the one workflow every pull request runs.
JOBS = {"journeys": "javascript / "}


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
    """True when that run is the pull request's own CI run, on these files, with none of the area's jobs failed."""
    run = call(f"/repos/{repository}/actions/runs/{run_id}", token)
    if (run.get("event") != "pull_request" or run.get("head_sha") != head or run.get("head_branch") != pull["ref"]
            or (run.get("head_repository") or {}).get("full_name") != repository
            or run.get("path") != f".github/workflows/{WORKFLOW}"
            or not ran_on(run, pull, tree, repository, token, call)):
        return False
    data = call(f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs or data.get("total_count") != len(jobs):
        return False
    if not all(isinstance(job, dict) and isinstance(job.get("name"), str) for job in jobs):
        return False
    own = [job for job in jobs if job["name"].startswith(JOBS[area])]
    # Other jobs may still be running when the record is read; anything else must have passed.
    return bool(own) and all(
        (job.get("status") == "completed" and job.get("conclusion") in ("success", "skipped"))
        or (job.get("status") in WAITING and job.get("conclusion") is None) for job in own)


def exists(name: str, repository: str, token: str, call=request, *, area: str, tree: str,
           commit: str | None = None) -> bool:
    """True when the checks of the pull request merged as ``commit`` left a live record with that name.

    The rule: every workflow in the run came from main plus the reviewed,
    merged head, and the run used exactly the files of ``tree``. So the record
    counts only when all of this holds, and any doubt means it does not:

    - its run is the CI workflow, started by a pull request of this
      repository (never a fork) whose head was merged to main as this commit;
    - that pull request is the only one its branch ever opened and its base
      was never changed, so no run on that head used another base's workflows;
    - GitHub's own record of the run names that pull request's test merge
      commit, and that commit holds exactly ``tree``;
    - none of the area's jobs in the run failed, was cancelled or has an
      unknown state.
    """
    commit = commit or os.environ.get("GITHUB_SHA", "")
    if area not in JOBS or not commit or not tree:
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
    """Commits whose CI run on main succeeded, newest run first."""
    data = call(f"/repos/{repository}/actions/workflows/{WORKFLOW}/runs"
                "?branch=main&status=success&per_page=30", token)
    # Pull requests run the same workflow: one from a fork's branch named main is not main.
    return [run["head_sha"] for run in data.get("workflow_runs") or []
            if run.get("head_branch") == "main" and run.get("event") in MAIN_EVENTS
            and run.get("conclusion") == "success" and run.get("head_sha")]


def queue_runs(repository: str, token: str, call=request, commit: str | None = None) -> list[dict]:
    """This repository's successful CI runs the merge queue started for main, newest first.

    With ``commit`` only the runs on that commit. Only GitHub starts a run with
    this event, on the commit the queue built, and that commit becomes main's
    when the group merges.
    """
    query = f"?event={QUEUE_EVENT}&status=success&per_page=30"
    if commit:
        query += f"&head_sha={urllib.parse.quote(commit)}"
    data = call(f"/repos/{repository}/actions/workflows/{WORKFLOW}/runs{query}", token)
    return [run for run in data.get("workflow_runs") or []
            if isinstance(run, dict) and run.get("event") == QUEUE_EVENT
            and run.get("status") == "completed" and run.get("conclusion") == "success"
            and str(run.get("head_branch") or "").startswith(QUEUE_BRANCH)
            and run.get("path") == f".github/workflows/{WORKFLOW}"
            and (run.get("repository") or {}).get("full_name") == repository
            and (run.get("head_repository") or {}).get("full_name") == repository
            and run.get("head_sha") and (commit is None or run["head_sha"] == commit)]


def green_queue_commits(repository: str, token: str, call=request) -> list[str]:
    """Commits whose CI run in the merge queue for main succeeded, newest run first."""
    return [run["head_sha"] for run in queue_runs(repository, token, call)]


def queue_passed(repository: str, token: str, call=request, commit: str | None = None) -> bool:
    """True when the merge queue's CI run succeeded on exactly ``commit`` (by default the one checked out).

    That run selected its journeys the way a push to main does, from the last
    commit CI passed on, so the push that moves main to the same commit has
    nothing left to run.
    """
    commit = commit or os.environ.get("GITHUB_SHA", "")
    return bool(commit) and bool(queue_runs(repository, token, call, commit))


def merge_queue_guards(repository: str, token: str, branch: str = "main", call=request) -> bool:
    """True when GitHub's active rules send every merge into ``branch`` through a merge queue."""
    rules = call(f"/repos/{repository}/rules/branches/{urllib.parse.quote(branch, safe='')}?per_page=100", token)
    return isinstance(rules, list) and any(isinstance(rule, dict) and rule.get("type") == "merge_queue"
                                           for rule in rules)


def _where() -> tuple[str, str] | None:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    return (repository, token) if repository and token else None


def _push_to_main() -> bool:
    return os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("GITHUB_REF") == "refs/heads/main"


def environment() -> tuple[str, str] | None:
    """(repository, token) when a push to main may look for records; else None."""
    return _where() if _push_to_main() else None


def history_environment() -> tuple[str, str] | None:
    """(repository, token) for a run that narrows from the last commit CI passed on; else None.

    A push to main, and the merge queue's run of a group headed for main.
    """
    queued = (os.environ.get("GITHUB_EVENT_NAME") == QUEUE_EVENT
              and os.environ.get("GITHUB_REF", "").startswith("refs/heads/" + QUEUE_BRANCH))
    return _where() if queued or _push_to_main() else None
