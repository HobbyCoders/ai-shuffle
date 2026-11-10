"""What a green run already proved, so a later run of the same code can skip it.

A pull request's checks run on GitHub's merge of the branch into main. When an
area's checks all pass there, ``record-pass.yml`` uploads a tiny artifact named
``proven-<area>-<tree>``: the tree is the content of every file in that merge.
A squash merge onto an unmoved main produces the same tree, so main-suite finds
the record and skips that area instead of running it a second time.

The tree covers the workflows and tests too, so a record can only stand for
the same checks on the same files. Nothing here lists what an area depends on.
Only records uploaded by this repository's own runs count (never a fork's), and
only a push to main looks: scheduled and dispatched runs prove everything again.

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


def exists(name: str, repository: str, token: str, call=request) -> bool:
    """True when this repository's own run uploaded a live record with that name."""
    data = call(f"/repos/{repository}/actions/artifacts?per_page=100&name={urllib.parse.quote(name)}", token)
    for artifact in data.get("artifacts") or []:
        run = artifact.get("workflow_run") or {}
        own = run.get("repository_id") is not None and run.get("repository_id") == run.get("head_repository_id")
        if artifact.get("name") == name and artifact.get("expired") is False and own:
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
            found[area] = exists(record_name(area, tree), *where, call=call)
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
