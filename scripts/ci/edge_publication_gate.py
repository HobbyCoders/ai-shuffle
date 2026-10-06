"""Authorize edge publication using exact-tree PR or owner-started main CI.

Run only from a checkout of protected main, never from a workflow_run artifact.
This script reads GitHub metadata; it does not receive signing or Cloudflare keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import yaml

REPOSITORY = "HobbyCoders/ai-shuffle"
OWNER_ID = "76577322"
ROOT = Path(__file__).resolve().parents[2]
# Only these workflows deliberately create all-skipped runs for unrelated label
# changes. The protected-main configuration must authenticate the marker; a PR
# title or another workflow's display title must never hide real CI evidence.
LABEL_FILTERED_WORKFLOWS = {
    ".github/workflows/android.yml": "Android CI",
    ".github/workflows/desktop.yml": "Desktop CI",
    ".github/workflows/ci-browser-workspace.yml": "CI Browser Workspace",
}
UNRELATED_LABEL_SUFFIX = " (unrelated label event)"
# A combined main tree (another PR merged while this PR's CI ran) publishes only
# after the owner dispatches CI on main (#1505). That refusal needs the owner, so
# it is annotated on the run; app/core/deploy_status.py reads it by this title.
COMBINED_TREE_REFUSAL = "Combined main tree was not tested by PR CI; edge publication needs owner-dispatched CI on main"
REFUSAL_TITLE = "Edge publication refused"
UNRELATED_LABEL_RUN_NAME = (
    "${{ github.workflow }}${{ github.event_name == 'pull_request' && "
    "(github.event.action == 'labeled' || github.event.action == 'unlabeled') && "
    "github.event.label.name != 'ci:full' && ' (unrelated label event)' || '' }}"
)


class UniqueLoader(yaml.BaseLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"Duplicate workflow key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def glob_match(value, pattern):
    # These are the constructs used by the repository's Actions path filters.
    # Reject new, unsupported syntax instead of silently omitting a CI suite.
    if any(char in pattern for char in "?[]+\\{}()"):
        raise ValueError(f"Unsupported Actions filter: {pattern}")
    parts = []
    while pattern:
        if pattern.startswith("**/"):
            parts.append("(?:.*/)?")
            pattern = pattern[3:]
        elif pattern.startswith("**"):
            parts.append(".*")
            pattern = pattern[2:]
        elif pattern.startswith("*"):
            parts.append("[^/]*")
            pattern = pattern[1:]
        else:
            parts.append(re.escape(pattern[0]))
            pattern = pattern[1:]
    return re.fullmatch("".join(parts), value) is not None


def matches(value, patterns):
    matched = False
    for pattern in patterns:
        excluded = pattern.startswith("!")
        if glob_match(value, pattern[1:] if excluded else pattern):
            matched = not excluded
    return matched


def applicable_workflows(directory, files=None):
    expected = set()
    for path in sorted(directory.glob("*.yml")):
        config = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
        events = config.get("on", {})
        if "pull_request" not in events:
            continue
        filters = events["pull_request"] or {}
        if "branches" in filters and not matches("main", filters["branches"]):
            continue
        if matches("main", filters.get("branches-ignore", [])):
            continue
        if files is not None:
            if "paths" in filters and not any(matches(file, filters["paths"]) for file in files):
                continue
            if "paths-ignore" in filters and all(matches(file, filters["paths-ignore"]) for file in files):
                continue
        expected.add(f".github/workflows/{path.name}")
    return expected


def unrelated_label_run(api, run, directory):
    name = LABEL_FILTERED_WORKFLOWS.get(run["path"])
    if (run.get("event") != "pull_request" or name is None
            or run.get("display_title") != name + UNRELATED_LABEL_SUFFIX):
        return False
    # This directory is from the verified protected-main checkout. Do not trust
    # markers from default PR titles or a changed/missing run-name contract.
    path = directory / Path(run["path"]).name
    config = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    if config.get("name") != name or config.get("run-name") != UNRELATED_LABEL_RUN_NAME:
        raise ValueError(f"Untrusted unrelated-label run marker: {run['path']}")
    # A marker alone never suppresses failure, cancellation, or pending CI. A
    # completed workflow may report success even though every job was skipped.
    if run.get("status") != "completed" or run.get("conclusion") not in {"success", "skipped"}:
        return False
    endpoint = f"actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs"
    jobs = pages(api, endpoint, "jobs", limit=10)
    if not jobs or any(job.get("status") != "completed" or job.get("conclusion") != "skipped"
                       or job.get("run_id") != run["id"] or job.get("head_sha") != run["head_sha"]
                       for job in jobs):
        raise ValueError(f"Unrelated-label run does not prove all jobs skipped: {run['path']}")
    return True


class GitHub:
    def __call__(self, endpoint):
        raw = subprocess.check_output(
            ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"], text=True, encoding="utf-8"
        )
        return json.loads(raw)


def pages(api, endpoint, key=None, limit=30):
    result = []
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, limit + 1):
        response = api(f"{endpoint}{separator}per_page=100&page={page}")
        values = response[key] if key else response
        if not isinstance(values, list):
            raise ValueError("Unexpected GitHub pagination response")
        result.extend(values)
        if len(values) < 100:
            if key and response.get("total_count") != len(result):
                raise ValueError("GitHub returned incomplete run history")
            return result
    raise ValueError("GitHub result exceeded the pagination bound; refusing partial CI evidence")


def publication_readiness(api, revision, directory, trigger_head=None):
    branch = api("branches/main")
    if not branch.get("protected"):
        return False, "main is not protected; refusing publication"
    if branch["commit"]["sha"] != revision:
        return False, "main moved; a superseded revision cannot publish"

    prs = pages(api, f"commits/{revision}/pulls")
    prs = [pr for pr in prs if pr.get("merged_at") and pr.get("merge_commit_sha") == revision
           and pr["base"]["ref"] == "main"
           and pr["base"]["repo"]["full_name"] == REPOSITORY
           and (pr["head"].get("repo") or {}).get("full_name") == REPOSITORY]
    if len(prs) != 1:
        return False, "Expected one merged main PR from this repository"
    pr = api(f"pulls/{prs[0]['number']}")
    head = pr["head"]["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise ValueError("Invalid PR head revision")
    if trigger_head is not None and trigger_head not in {head, revision}:
        return False, "Completed CI belongs to a different revision"
    same_tree = api(f"git/commits/{revision}")["tree"]["sha"] == api(f"git/commits/{head}")["tree"]["sha"]
    main_ci = not same_tree or trigger_head == revision

    files = pages(api, f"pulls/{pr['number']}/files")
    if len(files) != pr["changed_files"]:
        raise ValueError("Incomplete PR file list; cannot determine required CI")
    # A rename must cover both the deleted path and its replacement.
    names = {file["filename"] for file in files}
    names.update(file["previous_filename"] for file in files if "previous_filename" in file)
    if main_ci:
        # Include changes merged into the base while the PR's CI was running.
        # GitHub compares from the common ancestor and caps files at 300; a
        # capped response requires every PR workflow, never a partial subset.
        comparison = api(f"compare/{head}...{revision}?per_page=1&page=1")
        changed = comparison.get("files")
        if not isinstance(changed, list):
            raise ValueError("Missing combined-tree file list")
        names.update(file["filename"] for file in changed)
        names.update(file["previous_filename"] for file in changed if "previous_filename" in file)
        if len(changed) >= 300:
            names = None
    expected = applicable_workflows(directory, names)
    if not expected:
        return False, "No applicable PR CI; no source update will be published"

    event, tested = ("workflow_dispatch", revision) if main_ci else ("pull_request", head)
    runs = pages(api, f"actions/runs?event={event}&head_sha={tested}", "workflow_runs", limit=10)
    latest = {}
    # Once the latest genuine run is found, older attempts (including label
    # no-ops) cannot affect its result or require additional evidence queries.
    for run in sorted(runs, key=lambda item: (item["id"], item.get("run_attempt", 1)), reverse=True):
        if (run.get("event") != event or run.get("head_sha") != tested
                or (run.get("head_repository") or {}).get("full_name") != REPOSITORY):
            continue
        path = run["path"]
        if path not in expected or path in latest or unrelated_label_run(api, run, directory):
            continue
        latest[path] = run
    blocked = [path for path in sorted(expected) if path not in latest
               or latest[path]["status"] != "completed" or latest[path]["conclusion"] != "success"
               or (main_ci and not owner_main_ci(latest[path]))]
    if blocked:
        reason = "CI missing, pending, or unsuccessful: " + ", ".join(blocked)
        if main_ci:
            reason = combined_tree_refusal(blocked, latest)
        return False, reason
    # Recheck after the API reads. The publisher also checks main before writing its pointer.
    if api("branches/main")["commit"]["sha"] != revision:
        return False, "main moved during CI verification; skipping superseded revision"
    source = "Combined main revision" if main_ci else f"PR #{pr['number']}"
    return True, f"{source}: all {len(expected)} applicable workflows passed for the exact merged tree"


def combined_tree_refusal(blocked, latest):
    """The refusal plus the exact owner commands that would unblock it."""
    running = [path for path in blocked if path in latest and latest[path]["status"] != "completed"
               and owner_main_ci(latest[path])]
    dispatch = [path for path in blocked if path not in running]
    lines = [f"{COMBINED_TREE_REFUSAL}. CI missing, pending, or unsuccessful: {', '.join(blocked)}"]
    if dispatch:
        lines.append("Dispatch on main:")
        lines.extend(f"gh workflow run {Path(path).name} --ref main" for path in dispatch)
    if running:
        lines.append("Still running on main: " + ", ".join(Path(path).name for path in running))
    return "\n".join(lines)


def warning_annotation(reason):
    # Workflow command data must escape %, CR and LF; each line stays readable.
    data = reason.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title={REFUSAL_TITLE}::{data}"


def owner_main_ci(run):
    return (run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main"
            and str((run.get("actor") or {}).get("id")) == OWNER_ID
            and str((run.get("triggering_actor") or {}).get("id")) == OWNER_ID)


def event_allowed(env, event):
    if env.get("GITHUB_REPOSITORY") != REPOSITORY or env.get("GITHUB_REF") != "refs/heads/main":
        return False
    kind = env.get("GITHUB_EVENT_NAME")
    if kind == "push":
        return not event.get("deleted", False)
    if kind == "workflow_dispatch":
        return (env.get("GITHUB_ACTOR_ID") == OWNER_ID and bool(env.get("GITHUB_ACTOR"))
                and env.get("GITHUB_TRIGGERING_ACTOR") == env["GITHUB_ACTOR"])
    run = event.get("workflow_run", {})
    return (kind == "workflow_run" and (run.get("event") == "pull_request" or owner_main_ci(run))
            and run.get("conclusion") == "success"
            and (run.get("head_repository") or {}).get("full_name") == REPOSITORY)


def main():
    env = os.environ
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if not event_allowed(env, event):
        raise ValueError("Event is not authorized for edge publication")
    revision = env["GITHUB_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid source revision")
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout != revision:
        raise ValueError("Checkout does not match the main revision")
    trigger = event["workflow_run"]["head_sha"] if env["GITHUB_EVENT_NAME"] == "workflow_run" else None
    ready, reason = publication_readiness(GitHub(), revision, ROOT / ".github/workflows", trigger)
    print(reason)
    if not ready and reason.startswith(COMBINED_TREE_REFUSAL):
        # The gate job still succeeds; this is what makes the refusal visible.
        print(warning_annotation(reason))
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"ready={str(ready).lower()}\n")
    if env.get("GITHUB_STEP_SUMMARY"):
        with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(reason.replace("\n", "  \n") + "\n")


if __name__ == "__main__":
    main()
