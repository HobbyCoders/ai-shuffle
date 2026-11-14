"""Which areas of CI a commit has to run, and which already passed on the same files.

Every area lists the files its checks read (``AREAS``). The area's key is a
hash of exactly those files in the commit under test, so two commits that
differ only elsewhere share the key. When an area's checks all pass, the
``CI`` job uploads a tiny artifact named ``pass-<area>-<key>``. A later run
(the next push to the pull request, another pull request, or main after the
merge) finds that record and skips the area: Android runs only when Android's
files changed since Android last passed.

A record's name is written by the run and could say anything, so it counts
only when GitHub's own data agrees:

- the run is ``ci.yml`` of this repository, never a fork's;
- the commit GitHub says the run took its workflows from holds the same files
  for this area, hashed again here from GitHub's copy of that commit;
- the area's jobs in that run finished and none failed.

Every area's files include ``ci.yml``, this script, the gate and the area's
own workflow, so a record can only stand for the same checks. Any doubt or
error answers "run it". The scheduled run and a manual run look nothing up and
run everything.

    python scripts/ci/plan.py plan >> "$GITHUB_OUTPUT"
    python scripts/ci/plan.py keys            # the keys of the checked-out commit
    python scripts/ci/plan.py affected FILE.. # the areas whose files include these
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com"
PREFIX = "pass"
WORKFLOW = ".github/workflows/ci.yml"
# What decides and judges every area. A change here runs everything once.
COMMON = (WORKFLOW, "scripts/ci/plan.py", "scripts/ci/main_gate.py", "scripts/ci/main_suite.py")
PYTHON_PINS = ("requirements.txt", "requirements.lock", "requirements-dev.txt", "requirements-native.txt",
               "requirements-native.lock", "requirements-voice.txt", "requirements-voice.lock",
               "requirements-build.txt")
# Prose, and the UI's own source: the JavaScript checks build and test that.
PROSE = ("docs/", "README.md", "SUPPORT.md", "SECURITY.md", "TRADEMARKS.md", "LOCAL_SETUP.md")
WEB_UI_SOURCE = ("frontend/src/", "frontend/tests/")
UI_SOURCE = (*WEB_UI_SOURCE, "mobile/src/")
# The desktop tests start the real backend, build the app from the checkout
# and read package files all over it: everything but prose, UI source and the
# phone apps. Of Android they read only its version resolver (android/ci/).
DESKTOP = {"include": ("", "android/ci/"), "exclude": (*PROSE, *UI_SOURCE, "ios/", "android/")}

# name -> files the area reads. A path ending in "/" is a folder, "" is the
# whole repository; of an include and an exclude that both match, the longer
# one wins. `jobs` are the area's jobs in ci.yml; `except` names jobs under
# them that belong to another record; `advisory` names jobs that may fail.
AREAS = {
    # Python tests read sources all over the repository (docs, workflows, the
    # UI, the Android and desktop shells), so every change is a backend change.
    "backend": {"jobs": ("backend",), "include": ("",)},
    "javascript-checks": {
        "jobs": ("javascript",),
        "except": ("javascript / Select UI journeys", "javascript / UI journeys",
                   "javascript / record-journeys"),
        "include": (
            "frontend/", "mobile/", "tools/", "browser-mcp/", "docs/content/", "docs/ci/pr-checks.md",
            "scripts/perf/", "scripts/ci/bounded_apt.py", ".github/workflows/ci-frontend.yml",
            # Read by the frontend and tools tests from outside their folders.
            "app/core/transcript_views/", "app/contract/", "app/core/connectors/catalog.json",
            "fluid/fluid/context/peer.py", "tests/fixtures/", "tests/core/network/",
        ),
    },
    "fluid": {
        "jobs": ("fluid",),
        # Its tests run and load the repository's scripts.
        "include": ("fluid/", "scripts/", ".github/workflows/ci-fluid.yml"),
    },
    "windows": {"jobs": ("windows",), **DESKTOP},
    "desktop": {"jobs": ("desktop",), **DESKTOP},
    "desktop-source": {"jobs": ("desktop-source",), **DESKTOP},
    "browser-workspace": {
        "jobs": ("browser-workspace",),
        "include": ("browser-mcp/", "frontend/", "app/", "fluid/", "desktop/", *PYTHON_PINS,
                    "conftest.py", "pytest.ini", "tests/conftest.py",
                    "tests/test_browser_workspace_integration.py", "scripts/ci/bounded_apt.py",
                    ".github/workflows/ci-browser-workspace.yml"),
    },
    # Everything the image is built from and started with. The UI's own source
    # is left to the JavaScript build; the image build still runs on any
    # change to how the UI is built.
    "container": {
        "jobs": ("container",),
        "include": ("", ".github/workflows/container-postgres.yml", "desktop/build/application-update-key.pem",
                    "THIRD_PARTY_NOTICES.md"),
        "exclude": (*PROSE, *UI_SOURCE, ".github/", "tests/", "android/", "ios/", "desktop/", "fluid/tests/"),
    },
    "identity": {
        "jobs": ("identity",),
        "include": ("app/", "fluid/", "tests/", "conftest.py", "pytest.ini", *PYTHON_PINS,
                    ".github/workflows/ci-instance-identity.yml"),
    },
    # The app shell, the phone UI it packages as its offline demo and the build
    # setup of both. The one deliberate gap: the web UI's own source, which the
    # phone UI imports (`$lib` is frontend/src/lib). It changes in most pull
    # requests, the JavaScript checks build and test it with the phone UI every
    # time, and the paired worker, not the APK, serves the UI people chat in
    # (android/scripts/sync-offline-demo.sh). The emulators run again when the
    # shell or the phone UI itself changes, and in the scheduled run.
    "android": {
        "jobs": ("android",),
        # android.yml lets this emulator fail (`continue-on-error`) until it is reliable.
        "advisory": ("android / Android emulator verification (API 36)",),
        "include": ("android/", "mobile/", "frontend/", ".github/workflows/android.yml"),
        "exclude": WEB_UI_SOURCE,
    },
    "android-minified": {
        "jobs": ("android-minified",),
        "include": ("android/", "mobile/", "frontend/", ".github/workflows/ci-android-minified.yml"),
        "exclude": WEB_UI_SOURCE,
    },
}
# The verdict job: it uploads the records, so it is never part of an area.
GATE = "CI"
# Runs that look nothing up.
EVERYTHING = ("schedule", "workflow_dispatch")
FINISHED_WELL = ("success", "skipped")


def reads(area: str, path: str) -> bool:
    """True when ``path`` is one of the files the area's checks read."""
    if path in COMMON:
        return True
    rule = AREAS[area]

    def longest(prefixes):
        return max((len(prefix) for prefix in prefixes
                    if path == prefix or prefix == "" or (prefix.endswith("/") and path.startswith(prefix))),
                   default=-1)

    included = longest(rule["include"])
    return included >= 0 and included >= longest(rule.get("exclude", ()))


def key_of(area: str, entries) -> str:
    """One id for the content of every file the area reads. ``entries``: (mode, object id, path)."""
    digest = hashlib.sha256()
    for mode, object_id, path in sorted(entries, key=lambda entry: entry[2]):
        if reads(area, path):
            digest.update(f"{mode} {object_id}\t{path}\n".encode())
    return digest.hexdigest()[:40]


def record_name(area: str, key: str) -> str:
    """The artifact the ``CI`` job uploads when the area passed."""
    return f"{PREFIX}-{area}-{key}"


def local_entries(root: Path = ROOT, commit: str = "HEAD") -> list[tuple[str, str, str]]:
    """Every file of the checked-out commit, as git records it."""
    out = subprocess.run(["git", "ls-tree", "-r", "-z", commit], cwd=root, capture_output=True,
                         timeout=120, check=True).stdout.decode("utf-8", "surrogateescape")
    entries = []
    for line in out.split("\0"):
        if line:
            head, path = line.split("\t", 1)
            mode, _kind, object_id = head.split(" ")
            entries.append((mode, object_id, path))
    return entries


def request(path, token, urlopen=urllib.request.urlopen):
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-shuffle-ci"}
    with urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


class Lookup:
    """Reads GitHub once per run or commit, however many areas ask about it."""

    def __init__(self, repository: str, token: str, call=request):
        self.repository, self.token, self.call = repository, token, call
        self._runs, self._jobs, self._entries = {}, {}, {}

    def get(self, path):
        return self.call(f"/repos/{self.repository}{path}", self.token)

    def run(self, run_id):
        if run_id not in self._runs:
            self._runs[run_id] = self.get(f"/actions/runs/{run_id}")
        return self._runs[run_id]

    def jobs(self, run_id):
        """The latest attempt's jobs, or None when the list cannot be read whole."""
        if run_id not in self._jobs:
            found, page = [], 1
            while True:
                data = self.get(f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}")
                batch = data.get("jobs")
                if not isinstance(batch, list) or not isinstance(data.get("total_count"), int):
                    found = None
                    break
                found.extend(batch)
                if not batch or len(found) >= data["total_count"]:
                    found = found if len(found) == data["total_count"] else None
                    break
                page += 1
            self._jobs[run_id] = found
        return self._jobs[run_id]

    def entries(self, commit):
        """Every file of ``commit`` as GitHub holds it, or None when the list is cut short."""
        if commit not in self._entries:
            tree = (self.get(f"/git/commits/{commit}").get("tree") or {}).get("sha")
            data = self.get(f"/git/trees/{tree}?recursive=1") if tree else {}
            items = data.get("tree")
            if not isinstance(items, list) or data.get("truncated") is not False:
                self._entries[commit] = None
            else:
                self._entries[commit] = [(item["mode"], item["sha"], item["path"])
                                         for item in items if item.get("type") != "tree"]
        return self._entries[commit]


def tested_commit(run: dict, repository: str) -> str | None:
    """The commit GitHub says the run took this repository's workflows from.

    A pull request's run is listed under the branch's head commit but ran on
    GitHub's merge of the branch into main. Only the workflows a run called
    name that merge commit, and every one of them must name the same one.
    """
    prefix = f"{repository}/.github/workflows/"
    used = [item for item in run.get("referenced_workflows") or []
            if isinstance(item, dict) and str(item.get("path", "")).startswith(prefix)]
    commits = {item.get("sha") for item in used}
    if len(commits) != 1:
        return None
    (commit,) = commits
    if not isinstance(commit, str) or any(not item["path"].endswith("@" + commit) for item in used):
        return None
    return commit


def area_passed(area: str, jobs) -> bool:
    """True when every job of the area finished without a failure and at least one ran."""
    rule = AREAS[area]
    mine = [job for job in jobs or [] if isinstance(job, dict)
            and any(str(job.get("name", "")).startswith(key + " / ") for key in rule["jobs"])
            and not any(str(job.get("name", "")).startswith(other) for other in rule.get("except", ()))]
    advisory = rule.get("advisory", ())
    return (any(job.get("conclusion") == "success" and job.get("name") not in advisory for job in mine)
            and all(job.get("status") == "completed"
                    and (job.get("conclusion") in FINISHED_WELL or job.get("name") in advisory) for job in mine))


def proven(area: str, key: str, lookup: Lookup) -> str | None:
    """The run that passed this area on these files, or None."""
    name = record_name(area, key)
    data = lookup.get(f"/actions/artifacts?per_page=100&name={urllib.parse.quote(name)}")
    for artifact in data.get("artifacts") or []:
        origin = artifact.get("workflow_run") or {}
        own = origin.get("repository_id") is not None and origin.get("repository_id") == origin.get("head_repository_id")
        if artifact.get("name") != name or artifact.get("expired") is not False or not own or not origin.get("id"):
            continue
        run = lookup.run(origin["id"])
        if (run.get("path") != WORKFLOW
                or (run.get("repository") or {}).get("full_name") != lookup.repository
                or (run.get("head_repository") or {}).get("full_name") != lookup.repository):
            continue
        commit = tested_commit(run, lookup.repository)
        entries = lookup.entries(commit) if commit else None
        if entries is None or key_of(area, entries) != key:
            continue
        if area_passed(area, lookup.jobs(origin["id"])):
            return str(run.get("html_url") or origin["id"])
    return None


def plan(root: Path = ROOT, environ=os.environ, call=request, areas=None) -> dict[str, dict]:
    """{area: {key, record, proven, by}}. Any doubt or error means not proven: run it."""
    names = list(areas or AREAS)
    try:
        entries = local_entries(root)
    except Exception as error:  # noqa: BLE001 - without keys nothing can be skipped or recorded
        print(f"[plan] could not read the commit's files, running everything: {error}", file=sys.stderr)
        return {area: {"key": "", "record": "", "proven": False, "by": ""} for area in names}
    found = {}
    for area in names:
        key = key_of(area, entries)
        found[area] = {"key": key, "record": record_name(area, key), "proven": False, "by": ""}
    repository = environ.get("GITHUB_REPOSITORY", "")
    token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or ""
    # The scheduled run proves everything again, so a file an area reads
    # without listing it cannot stay unnoticed for more than a day. So does a
    # manual run: the owner starts one to prove the whole tree before a release.
    if not repository or not token or environ.get("GITHUB_EVENT_NAME") in EVERYTHING:
        return found
    lookup = Lookup(repository, token, call)
    for area in names:
        try:
            by = proven(area, found[area]["key"], lookup)
        except Exception as error:  # noqa: BLE001 - a lookup must never fail the run
            print(f"[plan] lookup for {area} failed, it runs: {error}", file=sys.stderr)
            continue
        if by:
            found[area].update(proven=True, by=by)
    return found


def affected(files, areas=None) -> list[str]:
    """The areas whose files include any of ``files``."""
    return [area for area in (areas or AREAS) if any(reads(area, path) for path in files)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("plan", "keys", "affected"))
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if args.stage == "affected":
        print("\n".join(affected(args.files)))
        return 0
    if args.stage == "keys":
        entries = local_entries()
        for area in AREAS:
            print(record_name(area, key_of(area, entries)))
        return 0
    found = plan()
    lines = ["### What this run has to check", "", "| Area | This run | Why |", "| --- | --- | --- |"]
    for area, state in found.items():
        print(f"{area}={'true' if state['proven'] else 'false'}")
        if state["proven"]:
            lines.append(f"| `{area}` | skipped | its files are unchanged since it passed in {state['by']} |")
        else:
            lines.append(f"| `{area}` | runs | no pass is recorded for these files |")
    print("records=" + json.dumps({area: state["record"] for area, state in found.items() if state["record"]},
                                  sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
