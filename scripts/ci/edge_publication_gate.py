"""Authorize edge publication using the merged PR's exact tree and applicable CI.

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


def applicable_workflows(directory, files):
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
        if "paths" in filters and not any(matches(file, filters["paths"]) for file in files):
            continue
        if "paths-ignore" in filters and all(matches(file, filters["paths-ignore"]) for file in files):
            continue
        expected.add(f".github/workflows/{path.name}")
    return expected


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
    if trigger_head is not None and trigger_head != head:
        return False, "Completed CI belongs to a different PR revision"
    if api(f"git/commits/{revision}")["tree"]["sha"] != api(f"git/commits/{head}")["tree"]["sha"]:
        return False, "Merged tree differs from the PR head; update the branch and test the combined tree"

    files = pages(api, f"pulls/{pr['number']}/files")
    if len(files) != pr["changed_files"]:
        raise ValueError("Incomplete PR file list; cannot determine required CI")
    # A rename must cover both the deleted path and its replacement.
    names = {file["filename"] for file in files}
    names.update(file["previous_filename"] for file in files if "previous_filename" in file)
    expected = applicable_workflows(directory, names)
    if not expected:
        return False, "No applicable PR CI; no source update will be published"

    runs = pages(api, f"actions/runs?event=pull_request&head_sha={head}", "workflow_runs", limit=10)
    latest = {}
    for run in runs:
        if (run.get("event") != "pull_request" or run.get("head_sha") != head
                or (run.get("head_repository") or {}).get("full_name") != REPOSITORY):
            continue
        path = run["path"]
        if path not in latest or (run["id"], run.get("run_attempt", 1)) > (
                latest[path]["id"], latest[path].get("run_attempt", 1)):
            latest[path] = run
    blocked = [path for path in sorted(expected) if path not in latest
               or latest[path]["status"] != "completed" or latest[path]["conclusion"] != "success"]
    if blocked:
        return False, "CI missing, pending, or unsuccessful: " + ", ".join(blocked)
    # Recheck after the API reads. The publisher also checks main before writing its pointer.
    if api("branches/main")["commit"]["sha"] != revision:
        return False, "main moved during CI verification; skipping superseded revision"
    return True, f"PR #{pr['number']}: all {len(expected)} applicable workflows passed for the exact merged tree"


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
    return (kind == "workflow_run" and run.get("event") == "pull_request"
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
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"ready={str(ready).lower()}\n")
    if env.get("GITHUB_STEP_SUMMARY"):
        with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(reason + "\n")


if __name__ == "__main__":
    main()
