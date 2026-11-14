"""The CI plan: an area is skipped only when its own files already passed in this repository's CI."""
import ast
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SPEC = importlib.util.spec_from_file_location("ci_plan_under_test", ROOT / "scripts/ci/plan.py")
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)

sys.path.insert(0, str(ROOT / "scripts"))
from ci import main_gate  # noqa: E402

REPO = "owner/repo"
WORKFLOW_FILE = ".github/workflows/ci.yml"
MERGE = "c" * 40
RUN = 7


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


# --- Which files an area reads -------------------------------------------------

@pytest.mark.parametrize(("area", "path", "expected"), [
    # Every change is a backend change.
    ("backend", "docs/ci/pr-checks.md", True), ("backend", "android/app/build.gradle.kts", True),
    # Android runs for the shell and the phone UI it packages, nothing else.
    ("android", "android/app/src/main/AndroidManifest.xml", True), ("android", "mobile/src/App.svelte", True),
    ("android", "frontend/package-lock.json", True), ("android", ".github/workflows/android.yml", True),
    ("android", "frontend/vite.config.js", True), ("android", "frontend/scripts/build-mobile-ui.mjs", True),
    # The one deliberate gap: the web UI's source, which the phone UI imports.
    ("android", "frontend/src/lib/a.ts", False), ("android", "frontend/tests/a.spec.ts", False),
    ("android-minified", "frontend/src/lib/a.ts", False), ("android-minified", "mobile/src/App.svelte", True),
    ("android", "app/main.py", False), ("android", "fluid/fluid/engine.py", False),
    ("android", "desktop/main.js", False), ("android", "docs/android.md", False),
    # android/ci/test-ci-webview.py reads both Android workflows.
    ("android", ".github/workflows/ci-android-minified.yml", True),
    ("android-minified", ".github/workflows/ci-android-minified.yml", True),
    ("android-minified", ".github/workflows/android.yml", False),
    # The server suites leave the UI's own source to the JavaScript checks.
    ("fluid", "fluid/fluid/engine.py", True), ("fluid", "scripts/devloop/ci.py", True),
    ("fluid", "app/main.py", False), ("fluid", "frontend/src/lib/a.ts", False),
    ("fluid", "app/core/temp_scratch.py", True),
    ("windows", "app/main.py", True), ("windows", "desktop/main.js", True), ("windows", "frontend/package.json", True),
    ("windows", "frontend/src/lib/a.ts", False), ("windows", "docs/x.md", False), ("windows", "ios/App.swift", False),
    ("windows", "README.md", False), ("windows", "android/app/src/main/AndroidManifest.xml", False),
    ("windows", "android/ci/resolve-version-name.py", True), ("desktop", "android/gradle.properties", False),
    ("desktop-source", "android/ci/resolve-version-name.py", True),
    ("identity", "tests/core/test_x.py", True), ("identity", "frontend/src/lib/a.ts", False),
    ("identity", "android/x", False), ("identity", "scripts/devloop/seed.py", True),
    ("identity", "docker/container_runtime.py", True),
    ("browser-workspace", "frontend/src/lib/a.ts", True), ("browser-workspace", "android/x", False),
    # Of tests/ only its one test and the helpers every test loads.
    ("browser-workspace", "tests/settings_guard.py", True), ("browser-workspace", "tests/pg_shim.py", True),
    ("browser-workspace", "scripts/ci/test_scratch.py", True), ("browser-workspace", "docker/agent_isolation.py", True),
    ("browser-workspace", "tests/core/test_x.py", False),
    ("javascript-checks", "frontend/src/lib/a.ts", True), ("javascript-checks", "docs/content/a.md", True),
    ("javascript-checks", "app/contract/events.json", True), ("javascript-checks", "app/main.py", False),
    ("javascript-checks", "docs/ci/release-channels.md", False),
    # Of an include and an exclude that both match, the longer one wins.
    ("container", "app/main.py", True), ("container", "Dockerfile", True), ("container", "tests/x.py", False),
    ("container", "desktop/main.js", False), ("container", "desktop/build/application-update-key.pem", True),
    ("container", ".github/workflows/ci-backend.yml", False),
    ("container", ".github/workflows/container-postgres.yml", True),
    ("container", "frontend/src/lib/a.ts", False), ("container", "frontend/package.json", True),
    ("container", "fluid/tests/unit/test_a.py", False), ("container", "fluid/fluid/engine.py", True),
    # The buyer's install files and the UI smoke script are read from excluded folders.
    ("container", "docs/install/compose/compose.yaml", True), ("container", "docs/install/docker.md", False),
    ("container", "tests/integration/container_ui_smoke.mjs", True),
    ("container", "tests/integration/other.mjs", False),
])
def test_an_area_reads_only_its_own_files(area, path, expected):
    assert plan.reads(area, path) is expected


def test_what_decides_and_judges_the_run_is_part_of_every_area():
    assert plan.COMMON == (".github/workflows/ci.yml", "scripts/ci/plan.py", "scripts/ci/main_gate.py",
                           "scripts/ci/main_suite.py")
    for area in plan.AREAS:
        for path in plan.COMMON:
            assert plan.reads(area, path), (area, path)
    assert plan.affected(["scripts/ci/plan.py"]) == list(plan.AREAS)
    assert plan.affected(["docs/ci/release-channels.md"]) == ["backend"]
    assert plan.affected(["android/gradle.properties"]) == ["backend", "android", "android-minified"]
    assert plan.affected(["app/main.py"]) == [
        "backend", "windows", "desktop", "desktop-source", "browser-workspace", "container", "identity"]
    assert plan.affected(["frontend/src/routes/+page.svelte"]) == ["backend", "javascript-checks", "browser-workspace"]


def test_a_folder_prefix_never_matches_a_longer_name():
    assert not plan.reads("android", "android-extra/x")
    assert not plan.reads("android", "mobile.md")
    assert not plan.reads("javascript-checks", "docs/ci/pr-checks.md.bak")


def test_the_key_changes_only_with_the_files_the_area_reads():
    files = [("100644", "1" * 40, "android/app/a.kt"), ("100644", "2" * 40, "app/main.py"),
             ("100644", "3" * 40, ".github/workflows/ci.yml")]
    key = plan.key_of("android", files)
    assert re.fullmatch(r"[0-9a-f]{40}", key)
    assert plan.record_name("android", key) == f"pass-android-{key}"
    # Order never matters.
    assert plan.key_of("android", list(reversed(files))) == key
    # Another area's file changes nothing; a new, changed, renamed or re-moded file of its own does.
    assert plan.key_of("android", [*files[:1], ("100644", "9" * 40, "app/main.py"), files[2]]) == key
    assert plan.key_of("android", [*files, ("100644", "4" * 40, "docs/a.md")]) == key
    assert plan.key_of("android", [*files, ("100644", "4" * 40, "android/b.kt")]) != key
    assert plan.key_of("android", [("100644", "8" * 40, "android/app/a.kt"), *files[1:]]) != key
    assert plan.key_of("android", [("100644", "1" * 40, "android/app/b.kt"), *files[1:]]) != key
    assert plan.key_of("android", [("100755", "1" * 40, "android/app/a.kt"), *files[1:]]) != key
    # A change to what decides the run changes every key.
    assert plan.key_of("android", [*files[:2], ("100644", "7" * 40, ".github/workflows/ci.yml")]) != key
    # The same files give different areas different records.
    assert plan.key_of("backend", files) != plan.key_of("android", files)


def test_the_local_keys_read_the_commit_not_the_working_folder(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "a@example.invalid")
    git("config", "user.name", "a")
    (tmp_path / "android").mkdir()
    (tmp_path / "android" / "a b.kt").write_text("one\n")
    (tmp_path / "app.py").write_text("one\n")
    git("add", "-A")
    git("commit", "-qm", "one")
    entries = plan.local_entries(tmp_path)
    assert sorted(path for _mode, _sha, path in entries) == ["android/a b.kt", "app.py"]
    key = plan.key_of("android", entries)
    (tmp_path / "android" / "a b.kt").write_text("uncommitted\n")
    assert plan.key_of("android", plan.local_entries(tmp_path)) == key
    (tmp_path / "app.py").write_text("two\n")
    git("commit", "-qam", "two")
    assert plan.key_of("android", plan.local_entries(tmp_path)) != key
    (tmp_path / "android" / "a b.kt").write_text("one\n")
    git("commit", "-qam", "three")
    assert plan.key_of("android", plan.local_entries(tmp_path)) == key


# --- The areas and the workflow agree --------------------------------------------

# Jobs of a called workflow that never run from ci.yml (a manual run of the
# workflow itself starts them), or that keep another record.
NOT_IN_CI = {
    "windows-suite.yml": {"suite", "selected"},
    "desktop.yml": {"package", "runtime", "windows-preview", "stable-installer-channel"},
    "ci-frontend.yml": {"journey-plan", "journeys", "record-journeys"},
    "android.yml": {"publish"},
}
TOKEN = re.compile(r"[A-Za-z0-9_.\-/]+")
MODULE = re.compile(r"-m\s+([A-Za-z_][A-Za-z0-9_.]*)")


def named_by(name):
    """Every repository path the steps of a called workflow name, with the workflow itself."""
    config = workflow(name)
    found = {f".github/workflows/{name}"}
    default = ((config.get("defaults") or {}).get("run") or {}).get("working-directory", ".")
    for key, job in config["jobs"].items():
        if key in NOT_IN_CI.get(name, ()):
            continue
        if "uses" in job:
            if job["uses"].startswith("./"):
                found |= named_by(job["uses"].rsplit("/", 1)[1])
            continue
        folder = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", default)
        for step in job.get("steps", []):
            where = step.get("working-directory", folder)
            if "${{" in where:
                continue
            if where != ".":
                found.add(where.rstrip("/") + "/x")
            text = "\n".join([step.get("run", ""), *map(str, (step.get("with") or {}).values()),
                              *map(str, (step.get("env") or {}).values())])
            candidates = [token.rstrip(".,:;") for token in TOKEN.findall(text)]
            for module in MODULE.findall(step.get("run", "")):
                candidates += [module.replace(".", "/") + ".py", module.replace(".", "/") + "/__main__.py"]
            for token in candidates:
                if not token or token.startswith("/") or ".." in token or ("/" not in token and "." not in token):
                    continue
                for base in dict.fromkeys((where, ".")):
                    path = ROOT / base / token
                    if path.exists():
                        relative = str(PurePosixPath(base) / token) if base != "." else token
                        relative = relative[2:] if relative.startswith("./") else relative
                        found.add(relative.rstrip("/") + ("/x" if path.is_dir() else ""))
                        break
    return found


def test_every_area_runs_in_ci_and_reads_the_workflows_that_run_it():
    jobs = workflow("ci.yml")["jobs"]
    seen = []
    for area, rule in plan.AREAS.items():
        assert set(rule) <= {"jobs", "include", "exclude", "except", "advisory"}, area
        for job in rule["jobs"]:
            seen.append(job)
            assert jobs[job]["uses"].startswith("./.github/workflows/"), (area, job)
            assert plan.reads(area, jobs[job]["uses"][2:]), (area, job)
    # No job stands behind two records, and the verdict job behind none.
    assert len(seen) == len(set(seen)) and "ci" not in seen
    assert jobs["ci"]["name"] == plan.GATE


@pytest.mark.parametrize("area", sorted(plan.AREAS))
def test_an_area_reads_every_path_its_steps_name(area):
    """A file a check reads but its area does not list would let a stale pass skip the check."""
    jobs = workflow("ci.yml")["jobs"]
    named = set()
    for job in plan.AREAS[area]["jobs"]:
        named |= named_by(jobs[job]["uses"].rsplit("/", 1)[1])
    assert len(named) > 1
    assert sorted(path for path in named if not plan.reads(area, path)) == []


# --- Files a check loads without its step naming them ------------------------------
#
# A step names a script or a test; that file imports modules and opens files of
# its own. Two scans follow them from the Python files the steps name: every
# module of this repository they import, however deep, and every repository
# path those files themselves spell out.

PACKAGE_ROOTS = (ROOT, ROOT / "fluid")
ROOT_NAMES = ("ROOT", "REPO", "REPO_ROOT", "root")
# Paths a named script spells out that its area need not read, each with the reason.
NOT_READ = {
    # android/ci/test-ci-webview.py opens two workflows by name; both are listed.
    ("android", ".github/workflows/x"),
}


def module_file(name):
    for base in PACKAGE_ROOTS:
        path = base / Path(*name.split("."))
        for found in (path.with_suffix(".py"), path / "__init__.py"):
            if found.is_file():
                return found
    return None


def package_of(path, level):
    folder = path.parent
    for _ in range(level - 1):
        folder = folder.parent
    for base in PACKAGE_ROOTS:
        if base in (folder, *folder.parents) and (base == ROOT or (folder / "__init__.py").is_file()):
            return ".".join(folder.relative_to(base).parts)
    return None


def loaded_by(path):
    """(modules a Python file imports, repository paths it spells out)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, paths, inner = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            package = package_of(path, node.level) if node.level else ""
            if package is None:
                continue
            name = ".".join(part for part in (package, node.module) if part)
            modules |= {name, *(f"{name}.{alias.name}" for alias in node.names)}
        elif isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "pytest_plugins" for target in node.targets):
            modules |= {item.value for item in ast.walk(node.value)
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "/" in node.value:
            paths.add(node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in inner:
            # ROOT / "docs" / "install": the whole chain, not each shorter start of it.
            parts, current = [], node
            while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
                inner.add(id(current.left))
                parts.append(current.right.value if isinstance(current.right, ast.Constant) else None)
                current = current.left
            if getattr(current, "id", "") in ROOT_NAMES and all(isinstance(part, str) for part in parts):
                paths.add("/".join(reversed(parts)))
    found = set()
    for text in paths:
        text = text.strip("/")
        if text and ".." not in text and len(text) < 200 and not text.startswith(("http", "~", "$")):
            try:
                target = ROOT / text
                if target.exists():
                    found.add(text + ("/x" if target.is_dir() else ""))
            except OSError:
                pass
    return modules, found


def python_named(area):
    """The Python files the area's steps name, with the conftest files pytest loads for a test."""
    jobs = workflow("ci.yml")["jobs"]
    named = set()
    for job in plan.AREAS[area]["jobs"]:
        named |= named_by(jobs[job]["uses"].rsplit("/", 1)[1])
    files = set()
    for name in named:
        path = ROOT / name
        if path.is_file() and path.suffix == ".py":
            files.add(path)
            if path.name.startswith("test_"):
                files |= {folder / "conftest.py" for folder in path.parents
                          if ROOT in (folder, *folder.parents) and (folder / "conftest.py").is_file()}
    return files


def imported_by(files):
    seen, waiting = set(), list(files)
    while waiting:
        path = waiting.pop()
        if path in seen:
            continue
        seen.add(path)
        for module in loaded_by(path)[0]:
            found = module_file(module)
            if found:
                waiting.append(found)
                waiting += [folder / "__init__.py" for folder in found.parents
                            if ROOT in folder.parents and (folder / "__init__.py").is_file()]
    return {str(PurePosixPath(path.relative_to(ROOT))) for path in seen}


@pytest.mark.parametrize("area", sorted(plan.AREAS))
def test_an_area_reads_every_module_its_checks_import(area):
    """A test's helpers and an app module's imports run in the check as much as the file a step names."""
    assert sorted(path for path in imported_by(python_named(area)) if not plan.reads(area, path)) == []


@pytest.mark.parametrize("area", sorted(plan.AREAS))
def test_an_area_reads_every_path_its_scripts_spell_out(area):
    """The buyer-install check opens docs/install/compose; only its script says so."""
    named = {path for script in python_named(area) for path in loaded_by(script)[1]}
    assert sorted(path for path in named if not plan.reads(area, path) and (area, path) not in NOT_READ) == []


def test_the_import_scan_follows_the_ways_a_test_loads_a_module():
    # A pytest plugin named in a conftest, a relative import and an import inside a function.
    assert "tests.settings_guard" in loaded_by(ROOT / "tests/conftest.py")[0]
    assert "scripts/ci/test_scratch.py" in imported_by({ROOT / "conftest.py"})
    assert "tests/pg_shim.py" in imported_by({ROOT / "tests/conftest.py"})
    assert "docs/install/compose/x" in loaded_by(ROOT / "scripts/ci/verify_buyer_compose.py")[1]


@pytest.mark.parametrize(("area", "path"), [
    ("container", "docs/install/compose/compose.yaml"), ("container", "tests/integration/container_ui_smoke.mjs"),
    ("browser-workspace", "scripts/ci/test_scratch.py"), ("browser-workspace", "tests/settings_guard.py"),
    ("fluid", "app/core/temp_scratch.py"), ("identity", "scripts/ci/test_scratch.py"),
])
def test_a_file_a_check_loads_changes_the_key(area, path):
    before = [("100644", "1" * 40, path), ("100644", "2" * 40, WORKFLOW_FILE)]
    after = [("100644", "3" * 40, path), ("100644", "2" * 40, WORKFLOW_FILE)]
    assert plan.key_of(area, before) != plan.key_of(area, after)


def test_the_jobs_left_out_of_the_scan_cannot_run_from_ci():
    jobs = workflow("ci.yml")["jobs"]
    for name, keys in NOT_IN_CI.items():
        config = workflow(name)["jobs"]
        assert keys <= set(config), name
    assert workflow("windows-suite.yml")["jobs"]["suite"]["if"].startswith("github.event_name == 'workflow_dispatch'")
    assert workflow("windows-suite.yml")["jobs"]["selected"]["if"].startswith("github.event_name == 'workflow_dispatch'")
    desktop = workflow("desktop.yml")["jobs"]
    for key in ("package", "runtime", "windows-preview"):
        assert desktop[key]["if"].startswith("github.event_name == 'workflow_dispatch'"), key
    assert desktop["stable-installer-channel"]["if"] == "inputs.verify_stable_installer == true"
    assert jobs["desktop"]["with"] == {"main_suite": "true"}
    # The journeys keep their own record, by the whole tree (scripts/ci/proven.py).
    javascript = workflow("ci-frontend.yml")["jobs"]
    excepted = plan.AREAS["javascript-checks"]["except"]
    assert set(excepted) == {"javascript / " + javascript[key].get("name", key).split(" (")[0]
                             for key in NOT_IN_CI["ci-frontend.yml"]}


def test_the_advisory_job_is_the_one_its_workflow_lets_fail():
    android = workflow("android.yml")["jobs"]["android-ui"]
    assert android["continue-on-error"] == "${{ matrix.advisory }}"
    names = [android["name"].replace("${{ matrix.api }}", leg["api"])
             for leg in android["strategy"]["matrix"]["include"] if leg["advisory"] == "true"]
    assert plan.AREAS["android"]["advisory"] == tuple("android / " + name for name in names)
    assert [area for area, rule in plan.AREAS.items() if rule.get("advisory")] == ["android"]


# --- Did the area pass in a run ----------------------------------------------------

def job(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


ANDROID = [job("android / Build and test Android app"), job("android / Android emulator verification (API 34)"),
           job("android / Android emulator verification (API 36)"),
           job("android / Publish verified Android download", "skipped")]
OTHER = [job("backend / backend-shard 1"), job("android-minified / Minified releaseSmoke (API 34)"), job("CI", None, "in_progress")]


def every_job(caller):
    """Every job the workflow behind a ci.yml job defines, passed."""
    gate = main_gate.Plan()
    return [job(name) for slot in gate.slots if slot.area == caller for name in (slot.members or [slot.full])]


def test_the_fixture_lists_every_job_android_defines():
    assert sorted(item["name"] for item in ANDROID) == sorted(item["name"] for item in every_job("android"))


def test_an_area_passed_when_its_own_jobs_all_finished_without_a_failure():
    assert plan.area_passed("android", ANDROID + OTHER)
    # Another area's failure, or the verdict job still running, takes nothing away.
    assert plan.area_passed("android", ANDROID + [job("backend / backend-shard 1", "failure")] + OTHER[1:])
    # The emulator its workflow lets fail.
    assert plan.area_passed("android", [ANDROID[0], ANDROID[1], {**ANDROID[2], "conclusion": "failure"}, ANDROID[3]])


@pytest.mark.parametrize("bad", [
    job("android / Build and test Android app", "failure"),
    job("android / Android emulator verification (API 34)", "failure"),
    job("android / Android emulator verification (API 34)", "cancelled"),
    job("android / Android emulator verification (API 34)", "timed_out"),
    job("android / Android emulator verification (API 34)", "neutral"),
    job("android / Android emulator verification (API 34)", None, "in_progress"),
    job("android / Android emulator verification (API 34)", None, "queued"),
    # Still running, even the tolerated emulator holds the record back.
    job("android / Android emulator verification (API 36)", None, "in_progress"),
    job("android / A job added later", "failure"),
])
def test_an_area_with_a_failed_or_unfinished_job_did_not_pass(bad):
    jobs = [item for item in ANDROID if item["name"] != bad["name"]] + [bad]
    assert not plan.area_passed("android", jobs + OTHER)


def test_an_area_that_ran_nothing_did_not_pass():
    assert not plan.area_passed("android", OTHER)
    assert not plan.area_passed("android", [])
    assert not plan.area_passed("android", None)
    assert not plan.area_passed("android", [job(item["name"], "skipped") for item in ANDROID])
    # Only the tolerated emulator succeeded.
    assert not plan.area_passed("android", [job(ANDROID[0]["name"], "skipped"), ANDROID[2]])
    # A name that only starts like the area's.
    assert not plan.area_passed("android", [job("android-minified / Minified releaseSmoke (API 34)")])
    assert not plan.area_passed("android", [job("android"), "odd", {}])


def test_the_javascript_checks_pass_without_the_journeys():
    checks = [job("javascript / Frontend tests (1/4)"), job("javascript / Mobile tests"),
              job("javascript / JavaScript package checks", "skipped")]
    journeys = [job("javascript / Select UI journeys"), job("javascript / UI journeys (3/18)", "failure"),
                job("javascript / record-journeys / Record the pass", "skipped")]
    assert plan.area_passed("javascript-checks", checks + journeys)
    assert not plan.area_passed("javascript-checks", journeys)
    assert not plan.area_passed("javascript-checks", checks + [job("javascript / Mobile tests", "failure")])


# --- A record counts only when GitHub's own data agrees ----------------------------

FILES = [{"path": "android/a.kt", "mode": "100644", "type": "blob", "sha": "1" * 40},
         {"path": "android", "mode": "040000", "type": "tree", "sha": "5" * 40},
         {"path": "app/main.py", "mode": "100644", "type": "blob", "sha": "2" * 40}]
KEY = plan.key_of("android", [(item["mode"], item["sha"], item["path"]) for item in FILES if item["type"] != "tree"])
NAME = f"pass-android-{KEY}"


def artifact(**changes):
    origin = {"id": RUN, "repository_id": 1, "head_repository_id": 1, "head_sha": "d" * 40}
    origin.update(changes.pop("origin", {}))
    return {"name": NAME, "expired": False, "workflow_run": origin, **changes}


def the_run(**changes):
    run = {"id": RUN, "path": ".github/workflows/ci.yml", "event": "pull_request", "html_url": "https://example.invalid/run/7",
           "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
           "referenced_workflows": [
               {"path": f"{REPO}/.github/workflows/android.yml@{MERGE}", "sha": MERGE, "ref": "refs/pull/5/merge"},
               {"path": f"{REPO}/.github/workflows/ci-backend.yml@{MERGE}", "sha": MERGE, "ref": "refs/pull/5/merge"},
               # A shared action or another repository's workflow is not this repository's commit.
               {"path": "other/actions/.github/workflows/x.yml@v1", "sha": "f" * 40}]}
    run.update(changes)
    return run


class World:
    """A fake GitHub API holding one record and the run behind it."""

    def __init__(self, artifacts=None, run=None, jobs=None, files=None, truncated=False, total=None):
        self.artifacts = [artifact()] if artifacts is None else artifacts
        self.run = the_run() if run is None else run
        self.jobs = ANDROID + OTHER if jobs is None else jobs
        self.files, self.truncated, self.total = FILES if files is None else files, truncated, total
        self.asked = []

    def __call__(self, path, token):
        assert token == "token"
        self.asked.append(path)
        prefix = f"/repos/{REPO}"
        assert path.startswith(prefix)
        path = path[len(prefix):]
        if path.startswith("/actions/artifacts?"):
            assert f"name={NAME}" in path or "name=pass-" in path
            return {"artifacts": self.artifacts if f"name={NAME}" in path else []}
        if path == f"/actions/runs/{RUN}":
            return self.run
        if path.startswith(f"/actions/runs/{RUN}/jobs?"):
            assert "filter=latest" in path
            page = int(re.search(r"page=(\d+)$", path).group(1))
            batch = self.jobs[(page - 1) * 100:page * 100]
            return {"total_count": len(self.jobs) if self.total is None else self.total, "jobs": batch}
        if path == f"/git/commits/{MERGE}":
            return {"tree": {"sha": "e" * 40}}
        if path == f"/git/trees/{'e' * 40}?recursive=1":
            return {"tree": self.files, "truncated": self.truncated}
        raise AssertionError(path)


def found(**world):
    return plan.proven("android", KEY, plan.Lookup(REPO, "token", World(**world)))


def test_a_record_of_this_repositorys_own_run_on_the_same_files_counts():
    assert found() == "https://example.invalid/run/7"
    # A push to main or the daily run leaves records too.
    assert found(run=the_run(event="push"))
    # Other areas' files differ between the two commits: the key is the area's own.
    assert found(files=[*FILES[:2], {**FILES[2], "sha": "9" * 40}])
    # More than one page of jobs is read whole.
    many = ANDROID + [job(f"backend / backend-shard {index}") for index in range(150)]
    assert found(jobs=many)


@pytest.mark.parametrize("world", [
    {"artifacts": []},
    {"artifacts": [artifact(expired=True)]},
    {"artifacts": [artifact(expired=None)]},
    {"artifacts": [artifact(name=NAME + "-more")]},
    # Uploaded by a fork's run: the name proves nothing.
    {"artifacts": [artifact(origin={"head_repository_id": 2})]},
    {"artifacts": [artifact(origin={"repository_id": None, "head_repository_id": None})]},
    {"artifacts": [artifact(origin={"id": None})]},
    {"artifacts": [{"name": NAME, "expired": False}]},
    {"run": the_run(path=".github/workflows/android.yml")},
    {"run": the_run(path=".github/workflows/other.yml")},
    {"run": the_run(head_repository={"full_name": "fork/repo"})},
    {"run": the_run(repository={"full_name": "fork/repo"})},
    {"run": the_run(head_repository=None)},
    # GitHub does not say which commit the run tested, or names two.
    {"run": the_run(referenced_workflows=[])},
    {"run": the_run(referenced_workflows=None)},
    {"run": the_run(referenced_workflows=[{"path": "other/actions/.github/workflows/x.yml@v1", "sha": "f" * 40}])},
    {"run": the_run(referenced_workflows=[
        {"path": f"{REPO}/.github/workflows/android.yml@{MERGE}", "sha": MERGE},
        {"path": f"{REPO}/.github/workflows/ci-backend.yml@{'a' * 40}", "sha": "a" * 40}])},
    {"run": the_run(referenced_workflows=[{"path": f"{REPO}/.github/workflows/android.yml@main", "sha": MERGE}])},
    {"run": the_run(referenced_workflows=[{"path": f"{REPO}/.github/workflows/android.yml@{MERGE}"}])},
    # The tested commit holds other files for this area: the name lied.
    {"files": [{**FILES[0], "sha": "9" * 40}, *FILES[1:]]},
    {"files": [*FILES, {"path": "android/b.kt", "mode": "100644", "type": "blob", "sha": "3" * 40}]},
    {"files": FILES[1:]},
    {"truncated": True},
    {"truncated": None},
    {"files": "odd"},
    # The area did not pass there.
    {"jobs": [{**ANDROID[0], "conclusion": "failure"}, *ANDROID[1:]]},
    {"jobs": [{**ANDROID[0], "status": "in_progress", "conclusion": None}, *ANDROID[1:]]},
    {"jobs": OTHER},
    {"jobs": []},
    # Jobs its workflow defines are not listed: the emulators wait for the build
    # and appear only when it finishes, so a run read part-way shows the build alone.
    {"jobs": [ANDROID[0]]},
    {"jobs": [ANDROID[0], ANDROID[3]]},
    {"jobs": [ANDROID[0], ANDROID[2], ANDROID[3]]},
    {"jobs": ANDROID[:3]},
    # The job list could not be read whole.
    {"total": 500},
    {"total": "odd"},
])
def test_anything_else_proves_nothing(world):
    assert found(**world) is None


def test_the_second_record_counts_when_the_first_does_not():
    assert found(artifacts=[artifact(expired=True), artifact(origin={"head_repository_id": 2}), artifact()])


def test_one_run_is_read_once_for_every_area_that_asks():
    world = World()
    lookup = plan.Lookup(REPO, "token", world)
    assert plan.proven("android", KEY, lookup) and plan.proven("android", KEY, lookup)
    assert [path for path in world.asked if "/artifacts" not in path] == [
        f"/repos/{REPO}/actions/runs/{RUN}", f"/repos/{REPO}/git/commits/{MERGE}",
        f"/repos/{REPO}/git/trees/{'e' * 40}?recursive=1",
        f"/repos/{REPO}/actions/runs/{RUN}/jobs?filter=latest&per_page=100&page=1"]


# --- The plan of a run ---------------------------------------------------------------

@pytest.fixture
def checkout(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "a@example.invalid")
    git("config", "user.name", "a")
    (tmp_path / "android").mkdir()
    (tmp_path / "android" / "a.kt").write_text("one\n")
    (tmp_path / "app.py").write_text("one\n")
    git("add", "-A")
    git("commit", "-qm", "one")
    return tmp_path


def environment(**changes):
    return {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": "token", "GITHUB_EVENT_NAME": "pull_request", **changes}


def records(checkout, proven=("android",)):
    """A fake API in which the named areas have a good record for the checkout's files."""
    entries = plan.local_entries(checkout)
    files = [{"path": path, "mode": mode, "type": "blob", "sha": sha} for mode, sha, path in entries]
    names = {plan.record_name(area, plan.key_of(area, entries)): area for area in proven}
    jobs = ANDROID + [job("backend / backend-shard 1"), *every_job("fluid")]

    def call(path, token):
        if "/actions/artifacts?" in path:
            name = re.search(r"name=([^&]+)$", path).group(1)
            return {"artifacts": [{**artifact(), "name": name}] if name in names else []}
        return World(files=files, jobs=jobs)(path, token)

    return call


def test_the_plan_skips_exactly_the_areas_with_a_record(checkout):
    found = plan.plan(checkout, environment(), records(checkout, ("android", "fluid")))
    assert found == plan.plan(checkout, environment(GITHUB_EVENT_NAME="push"), records(checkout, ("android", "fluid")))
    assert list(found) == list(plan.AREAS)
    assert [area for area, state in found.items() if state["proven"]] == ["fluid", "android"]
    assert found["android"]["by"] == "https://example.invalid/run/7"
    for area, state in found.items():
        assert state["record"] == f"pass-{area}-{state['key']}" and len(state["key"]) == 40


@pytest.mark.parametrize("changes", [
    # The daily run and a manual run prove everything again.
    {"GITHUB_EVENT_NAME": "schedule"}, {"GITHUB_EVENT_NAME": "workflow_dispatch"},
    {"GH_TOKEN": ""}, {"GITHUB_REPOSITORY": ""},
])
def test_the_plan_runs_everything_without_a_lookup(checkout, changes):
    def call(path, token):
        raise AssertionError("no lookup")

    found = plan.plan(checkout, environment(**changes), call)
    assert not any(state["proven"] for state in found.values())
    # The keys are still named, so this run's passes are recorded.
    assert all(state["record"] for state in found.values())


def test_a_lookup_that_fails_runs_the_area(checkout, capsys):
    good = records(checkout, ("android", "fluid"))

    def call(path, token):
        if "name=pass-fluid-" in path:
            raise OSError("unreachable")
        return good(path, token)

    found = plan.plan(checkout, environment(), call)
    assert [area for area, state in found.items() if state["proven"]] == ["android"]
    assert {area: state["error"] for area, state in found.items() if state["error"]} == {"fluid": "unreachable"}
    assert "lookup for fluid failed" in capsys.readouterr().err


def test_the_plan_step_names_a_failed_lookup_apart_from_no_record(checkout, monkeypatch, capsys, tmp_path):
    """A lookup that breaks runs everything; said in words, it cannot go on unnoticed."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def call(path, token):
        raise OSError("unreachable")

    real = plan.plan
    monkeypatch.setattr(plan, "plan", lambda: real(checkout, environment(), call))
    assert plan.main(["plan"]) == 0
    captured = capsys.readouterr()
    assert all(line.endswith("=false") for line in captured.out.splitlines()[:-1])
    assert "::warning title=Pass records::The lookup for android failed, so it runs: unreachable" in captured.err
    assert "| `android` | runs | the lookup failed: unreachable |" in summary.read_text()


def test_a_checkout_that_cannot_be_read_runs_everything_and_records_nothing(tmp_path, capsys):
    found = plan.plan(tmp_path / "missing", environment(), records)
    assert list(found) == list(plan.AREAS)
    assert all({**state, "error": ""} == {"key": "", "record": "", "proven": False, "by": "", "error": ""}
               and "No such file" in state["error"] for state in found.values())
    assert "running everything" in capsys.readouterr().err


def test_the_plan_step_writes_one_answer_per_area_and_the_record_names(checkout, monkeypatch, capsys, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    real = plan.plan
    monkeypatch.setattr(plan, "plan", lambda: real(checkout, environment(), records(checkout)))
    assert plan.main(["plan"]) == 0
    lines = capsys.readouterr().out.splitlines()
    answers = dict(line.split("=", 1) for line in lines)
    assert list(answers) == [*plan.AREAS, "records"]
    assert {area: answers[area] for area in plan.AREAS} == {
        area: "true" if area == "android" else "false" for area in plan.AREAS}
    named = json.loads(answers["records"])
    assert sorted(named) == sorted(plan.AREAS)
    assert all(re.fullmatch(rf"pass-{area}-[0-9a-f]{{40}}", record) for area, record in named.items())
    # One line each: $GITHUB_OUTPUT takes `name=value` lines.
    assert all("\n" not in value for value in answers.values())
    text = summary.read_text()
    assert "| `android` | skipped | its files are unchanged since it passed in https://example.invalid/run/7 |" in text
    assert "| `backend` | runs | no pass is recorded for these files |" in text


def test_the_keys_of_this_checkout_are_named_for_every_area(capsys):
    assert plan.main(["keys"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.rsplit("-", 1)[0] for line in lines] == [f"pass-{area}" for area in plan.AREAS]
    assert plan.main(["affected", "android/app/build.gradle.kts"]) == 0
    assert "android-minified" in capsys.readouterr().out.splitlines()
