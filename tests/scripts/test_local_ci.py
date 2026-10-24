"""scripts/local_ci.py: suite selection, pass records and the pre-push decision."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import local_ci  # noqa: E402  pyright: ignore[reportMissingImports]


@pytest.fixture(autouse=True)
def isolated_python_preflight(monkeypatch):
    # Environment validation is exercised with real subprocesses in test_local_ci_preflight.
    monkeypatch.setattr(local_ci, "python_preflight", lambda python: None)


def context(files):
    return local_ci.Context(mode="changed", base="base", files=files, python=sys.executable)


def suites_for(files):
    ctx = context(files)
    return {suite.name for suite in local_ci.SUITES if ctx.changed(suite.paths)}


def test_changes_reach_their_suites():
    assert suites_for(["app/core/deploy_status.py"]) >= {"backend"}
    assert "frontend" not in suites_for(["app/core/deploy_status.py"])
    assert suites_for(["frontend/src/lib/api.ts"]) >= {"frontend", "mobile"}
    assert suites_for(["fluid/fluid/loop.py"]) >= {"fluid", "backend"}
    assert suites_for([".github/workflows/release.yml"]) >= {"workflows", "android-play"}
    assert suites_for([".github/workflows/android.yml"]) >= {"workflows", "android-play"}
    assert suites_for(["docs/content/setup.md"]) == {"docs"}
    assert suites_for(["README.md"]) == set()


@pytest.mark.parametrize("path", [
    "Dockerfile", "entrypoint.sh", "docker/update_contract.py", "requirements.txt",
    "desktop/src/main/installed-entry.ts", "desktop/scripts/publish-source.mjs",
    "desktop/build/installer.nsh", "desktop/electron-builder.yml",
    "desktop/src/pages/recovery.html", "desktop/package-lock.json", "desktop/launcher.json",
    "scripts/source_archive.py", "scripts/ci/update_reach.py",
])
def test_update_reach_watched_paths_run_regression_tests(monkeypatch, path):
    ctx = context([path])
    suite = next(suite for suite in local_ci.SUITES if suite.name == "update-reach")
    assert ctx.changed(suite.paths)
    commands = []
    verdicts = []
    monkeypatch.setattr(local_ci, "run", lambda argv, **kwargs: commands.append((argv, kwargs)))
    monkeypatch.setattr(local_ci, "pytest_args", lambda python: ["-q"])
    monkeypatch.setattr(local_ci, "update_reach", verdicts.append)

    suite.run(ctx)

    assert len(commands) == 1
    argv, kwargs = commands[0]
    assert argv[:3] == [ctx.python, "-m", "pytest"]
    assert set(arg for arg in argv[3:] if arg.endswith(".py")) == {
        "tests/test_update_reach.py",
        "tests/scripts/test_local_ci.py",
        "tests/scripts/test_edge_publication_gate.py",
    }
    assert kwargs["env"] == local_ci.python_env()
    assert verdicts == [ctx]


@pytest.mark.parametrize("files", [
    ["docker/container_runtime.py"],
    ["docker/nested/helper.py"],
    ["Dockerfile"],
    ["entrypoint.sh"],
    ["docker-compose.yml"],
    ["app/main.py"],
    ["tests/core/test_container_runtime.py"],
    ["tests/core/test_container_image_contract.py"],
    None,  # full mode must run the dedicated files despite fast-subset exclusions
])
@pytest.mark.parametrize("pg_source", ["path", "smoke", None])
@pytest.mark.parametrize("pressure_optin", [False, True])
@pytest.mark.parametrize("pg_optin", [False, True])
def test_container_tests_run_in_linux_suite(monkeypatch, files, pg_source, pressure_optin, pg_optin):
    ctx = local_ci.Context(mode="full" if files is None else "changed", base="base",
                           files=files, python=sys.executable)
    suite = next(suite for suite in local_ci.SUITES if suite.name == "backend-linux")
    assert ctx.changed(suite.paths)
    commands = []
    monkeypatch.setattr(local_ci, "run", lambda argv, **kwargs: commands.append((argv, kwargs)))
    monkeypatch.setattr(local_ci.shutil, "which",
                        lambda name: "/test/pg/bin/pg_ctl" if name == "pg_ctl" and pg_source == "path" else None)
    monkeypatch.setattr(local_ci, "has_module", lambda *args: True)
    monkeypatch.setattr(local_ci, "WORKERS", "2")
    monkeypatch.setenv("PATH", "/test/original-path")
    monkeypatch.delenv("SMOKE_PG_BIN", raising=False)
    if pg_source == "smoke":
        monkeypatch.setenv("SMOKE_PG_BIN", "/test/pg/bin")
    # A launching shell must not opt these tests into a live database or enable
    # PG/pressure cases on a machine without the required binaries.
    monkeypatch.setenv("ALLOW_REAL_PG", "1")
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://never@external.invalid/live")
    monkeypatch.setenv("CONTAINER_RUNTIME_TEST_PG", "1")
    monkeypatch.setenv("CONTAINER_RUNTIME_TEST_PRESSURE", "1")
    monkeypatch.delenv("LOCAL_CI_CONTAINER_PG", raising=False)
    if pg_optin:
        monkeypatch.setenv("LOCAL_CI_CONTAINER_PG", "1")
    monkeypatch.delenv("LOCAL_CI_CONTAINER_PRESSURE", raising=False)
    if pressure_optin:
        monkeypatch.setenv("LOCAL_CI_CONTAINER_PRESSURE", "1")

    if ctx.mode == "full" and (pg_optin or pressure_optin) and not pg_source:
        with pytest.raises(local_ci.Failure, match="opt-in.*no PostgreSQL binaries"):
            suite.run(ctx)
        assert commands == []
        return

    suite.run(ctx)

    container_files = {
        "tests/core/test_container_runtime.py",
        "tests/core/test_container_image_contract.py",
    }
    selected = [(argv, kw) for argv, kw in commands if container_files.intersection(argv)]
    assert len(selected) == 1
    argv, kwargs = selected[0]
    assert argv[:3] == [ctx.python, "-m", "pytest"]
    assert container_files <= set(argv)
    assert container_files <= set(local_ci.FAST_SUBSET_EXCLUDED)
    assert "-n" not in argv  # process-global supervisor state and idle/disk timing
    assert "--timeout=120" in argv
    env = local_ci.child_env(kwargs["env"])
    assert env["ALLOW_REAL_PG"] == "0"
    assert env["TEST_POSTGRES_DSN"] == ""
    assert env["AI_SHUFFLE_ENV_FILE"] == "/dev/null"
    expected = "1" if pg_source and ctx.mode == "full" else "0"
    assert env["CONTAINER_RUNTIME_TEST_PG"] == expected
    expected_pressure = "1" if expected == "1" and pressure_optin else "0"
    assert env["CONTAINER_RUNTIME_TEST_PRESSURE"] == expected_pressure
    coverage_notes = [note for note in ctx.notes if "#350" in note]
    if expected_pressure == "1":
        assert coverage_notes == []  # both opt-in groups actually ran
    elif expected == "1":
        assert len(coverage_notes) == 1
        assert "checkpoint" in coverage_notes[0].lower()
        assert "LOCAL_CI_CONTAINER_PRESSURE=1" in coverage_notes[0]
        assert "LOCAL_CI_CONTAINER_PG=1" not in coverage_notes[0]
    else:
        assert coverage_notes == []  # missing binaries/check-mode notes are above
    # The pg_ctl folder comes back in the host's path form (\test\pg\bin on
    # Windows) and PATH entries join with the host's separator.
    pg_bin = {"path": str(Path("/test/pg/bin/pg_ctl").parent), "smoke": "/test/pg/bin"}.get(pg_source)
    assert env["PATH"] == (pg_bin + os.pathsep if pg_bin else "") + "/test/original-path"
    if ctx.mode != "full":
        assert any("Container" in note and "full" in note for note in ctx.notes)
    elif not pg_source:
        assert any("Container" in note and "PostgreSQL" in note for note in ctx.notes)


def test_full_container_pg_functional_coverage_needs_no_optin(monkeypatch):
    commands = []
    ctx = local_ci.Context(mode="full", base="base", files=None, python=sys.executable)
    monkeypatch.delenv("LOCAL_CI_CONTAINER_PG", raising=False)
    monkeypatch.delenv("LOCAL_CI_CONTAINER_PRESSURE", raising=False)
    monkeypatch.delenv("SMOKE_PG_BIN", raising=False)
    monkeypatch.setattr(local_ci.shutil, "which", lambda name: "/pg/bin/pg_ctl" if name == "pg_ctl" else None)
    monkeypatch.setattr(local_ci, "run", lambda argv, **kw: commands.append((argv, kw)))
    monkeypatch.setattr(local_ci, "pytest_args", lambda *a, **kw: ["-q"])
    local_ci.backend_linux(ctx)
    [(argv, kw)] = [(argv, kw) for argv, kw in commands if "tests/core/test_container_runtime.py" in argv]
    assert kw["env"]["CONTAINER_RUNTIME_TEST_PG"] == "1"
    assert kw["env"]["CONTAINER_RUNTIME_TEST_PRESSURE"] == "0"
    assert not any("PG coverage is opt-in" in note for note in ctx.notes)
    assert any("LOCAL_CI_CONTAINER_PRESSURE=1" in note for note in ctx.notes)


def test_container_collection_drops_host_handover(tmp_path):
    # The source supervisor reads these directly (not through Settings). A test
    # launched by an updated worker must not read its installed image metadata.
    names = ("SHUFFLE_HANDED_OVER", "SHUFFLE_IMAGE_DIR", "SHUFFLE_HANDOVER")
    env = {**os.environ, "SHUFFLE_HANDED_OVER": "a" * 40,
           "SHUFFLE_IMAGE_DIR": str(tmp_path), "SHUFFLE_HANDOVER": "1"}
    code = ("import os; import tests.conftest; "
            f"assert not set({names!r}).intersection(os.environ)")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_android_safety_suite_includes_emulator_contracts(monkeypatch):
    commands = []
    monkeypatch.setattr(local_ci, "run", lambda argv, **kwargs: commands.append(argv))
    local_ci.android_play(context([".github/workflows/android.yml"]))
    for script in ("test-emulator-readiness.py", "test-instrumentation-parser.py", "test-ui-runner.py"):
        assert [sys.executable, f"android/ci/{script}"] in commands
    if Path("/bin/bash").exists():
        assert [sys.executable, "android/ci/test-play-workflow.py"] in commands


def test_cloud_split_keeps_android_bridge_and_app_docs_independent():
    assert suites_for(["android/app/src/test/js/chat-downloads.test.cjs"]) >= {"android-bridge"}
    assert not {"hub", "site", "license", "uptime"} & {s.name for s in local_ci.SUITES}
    assert suites_for(["docs/content/getting-started/first-run.md"]) == {"docs"}


def test_full_mode_reaches_every_suite():
    ctx = local_ci.Context(mode="full", base="base", files=None, python=sys.executable)
    assert all(ctx.changed(suite.paths) for suite in local_ci.SUITES)


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_desktop_always_runs_unit_tests(monkeypatch, platform_name):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(local_ci, "os", SimpleNamespace(name=platform_name))
    monkeypatch.setattr(
        local_ci, "node_package",
        lambda *args: lambda ctx: calls.append(args),
    )
    local_ci.desktop(context(["desktop/test/launcher-swap.test.mjs"]))
    assert calls == [("desktop", "typecheck", "test")]


def test_windows_not_covered_note_names_the_staged_command():
    # Pinned text: a Windows full run outlasts one Monitor job (docs/ci/pr-checks.md).
    notes = [note for _paths, note in local_ci.NOT_LOCAL if note.startswith("Windows:")]
    if os.name == "nt":
        assert notes == []
        return
    assert notes == [
        "Windows: not tested on this machine. A stable release waits for "
        "`python scripts/local_ci.py full --step --attest` (one Monitor job per run; "
        "repeat the same command until it prints COMPLETE) on a Windows worker; edge does not."]


def test_name_selection():
    assert local_ci.select_by_name(["requirements.txt"]) == "all"
    assert local_ci.select_by_name(["tests/conftest.py"]) == "all"
    selected = local_ci.select_by_name(["app/core/deploy_status.py"])
    assert "tests/core/test_deploy_status.py" in selected
    assert not set(selected) & set(local_ci.FAST_SUBSET_EXCLUDED)
    assert local_ci.select_by_name(["tests/core/test_deploy_status.py"]) == ["tests/core/test_deploy_status.py"]
    assert local_ci.select_by_name(["README.md"]) == []


@pytest.fixture
def records(tmp_path, monkeypatch):
    path = tmp_path / "passes.json"
    monkeypatch.setattr(local_ci, "records_path", lambda: path)
    return path


def test_records_keep_the_newest_trees(records):
    for index in range(205):
        local_ci.save_record(f"tree{index}", {"at": index})
    kept = local_ci.load_records()
    assert len(kept) == 200
    assert "tree204" in kept and "tree0" not in kept


def push(monkeypatch, lines, *, head="h" * 40, clean=True, trees=None, check=0):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines)))
    monkeypatch.setattr(local_ci, "git", lambda *args: head)
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: clean)
    monkeypatch.setattr(local_ci, "tree_of", lambda sha: (trees or {}).get(sha, "t-" + sha))
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    ran = []
    monkeypatch.setattr(local_ci, "scan_push", lambda local, remote, target: ran.append(("scan", local)))
    monkeypatch.setattr(local_ci, "security_check", lambda python, **kw: ran.append(("tree", kw["audit"])))
    monkeypatch.setattr(local_ci, "fetch_push_base", lambda remote: ran.append(("fetch", remote)))
    monkeypatch.setattr(local_ci, "push_update_reach", lambda sha, ref: ran.append(("reach", sha, ref)))
    monkeypatch.setattr(local_ci, "cmd_fast", lambda args: pytest.fail("the push gate ran test suites"))
    result = local_ci.cmd_pre_push(local_ci.argparse.Namespace())
    return result, ran


def ref_line(sha, ref="refs/heads/topic"):
    return f"refs/heads/topic {sha} {ref} {'0' * 40}"


@pytest.mark.parametrize("recorded", [False, True])
def test_pre_push_runs_scans_and_reach_never_tests(records, monkeypatch, recorded):
    if recorded:
        # A pass record (old or current) never replaces any push check.
        local_ci.save_record("t-" + "h" * 40, {"at": 1, "mode": "fast",
                                               "security_gate": local_ci.SECURITY_GATE_VERSION})
    assert push(monkeypatch, [ref_line("h" * 40)]) == (0, [
        ("scan", "h" * 40), ("tree", False), ("fetch", None), ("reach", "h" * 40, "refs/heads/topic")])


def test_pre_push_scans_every_ref_before_anything_else(records, monkeypatch):
    lines = [ref_line("a" * 40), ref_line("b" * 40, ref="refs/heads/other")]
    result, ran = push(monkeypatch, lines)
    assert result == 0
    assert ran[:2] == [("scan", "a" * 40), ("scan", "b" * 40)]
    assert ran[2:] == [("tree", False), ("fetch", None),
                       ("reach", "a" * 40, "refs/heads/topic"), ("reach", "b" * 40, "refs/heads/other")]


def test_pre_push_allows_any_commit_or_dirty_tree_after_the_scan(records, monkeypatch):
    # Nothing is tested locally, so a commit other than HEAD or uncommitted
    # changes need no refusal: the scan covers every outgoing commit.
    assert push(monkeypatch, [ref_line("x" * 40)])[0] == 0
    assert push(monkeypatch, [ref_line("h" * 40)], clean=False)[0] == 0


def test_pre_push_ignores_deletions_and_scans_tags_only(records, monkeypatch):
    lines = [ref_line("0" * 40), ref_line("x" * 40, ref="refs/tags/v1.0.0")]
    assert push(monkeypatch, lines) == (0, [("scan", "x" * 40)])


@pytest.fixture
def asset_repo(tmp_path):
    """Real hook, scanner and git history; no git or check-pass mocks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "LOCAL_CI_PYTHON": sys.executable}
    # This isolated repository supplies its own base; never inherit the caller's.
    env.pop("LOCAL_CI_BASE", None)
    # Exercise ordinary CPython bytecode generation, even when the caller
    # disables it or redirects caches outside the checkout.
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env.pop("PYTHONPYCACHEPREFIX", None)

    def git(*args, check=True, input=None):
        return subprocess.run(["git", *args], cwd=repo, env=env, input=input,
                              text=True, capture_output=True, check=check)

    git("init", "-q")
    git("config", "user.name", "Local CI test")
    git("config", "user.email", "local-ci@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", ".githooks")
    for name in ("scripts/local_ci.py", "app/__init__.py", "app/core/__init__.py",
                 "app/core/temp_scratch.py", "scripts/secret_scan.py", ".githooks/pre-push", ".gitleaks.toml"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    (repo / ".githooks/pre-push").chmod(0o755)
    # Match the real checkout: importing the scratch helper must not make an
    # otherwise clean fixture dirty just because Python writes its cache.
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    # A harmless deterministic scanner rule exercises the real failure path.
    with (repo / ".gitleaks.toml").open("a", encoding="utf-8") as config:
        config.write('\n[[rules]]\nid = "local-ci-test"\ndescription = "Test sentinel"\n'
                     'regex = "LOCAL_CI_TEST_SECRET"\n')

    def commit(name, content="screenshot fixture"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git("add", "--", name)
        git("commit", "-qm", "Add fixture")
        return git("rev-parse", "HEAD").stdout.strip()

    def pre_push(local, remote="0" * 40, ref="refs/heads/topic"):
        return subprocess.run(
            [sys.executable, "scripts/local_ci.py", "pre-push"], cwd=repo, env=env,
            input=f"refs/heads/assets {local} {ref} {remote}\n",
            text=True, capture_output=True,
        )

    return repo, git, commit, pre_push


def test_pre_push_scan_failure_refuses_the_push(asset_repo):
    _, _, commit, pre_push = asset_repo
    tip = commit("README.md", "LOCAL_CI_TEST_SECRET")
    result = pre_push(tip)
    assert result.returncode == 1
    assert "local-ci-test" in result.stdout
    assert "LOCAL_CI_TEST_SECRET" not in result.stdout + result.stderr


@pytest.mark.parametrize("ref", ["refs/heads/pr-assets", "refs/heads/topic"])
def test_secret_added_then_removed_still_blocks_a_real_push(asset_repo, tmp_path, ref):
    _, git, commit, _ = asset_repo
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))
    commit("README.md")
    commit("notes.txt", "LOCAL_CI_TEST_SECRET")
    git("rm", "-q", "notes.txt")
    git("commit", "-qm", "Remove it again")
    commit("screenshot.png")
    result = git("push", str(remote), f"HEAD:{ref}", check=False)
    assert result.returncode != 0
    assert "local-ci-test" in result.stdout + result.stderr
    assert git("ls-remote", str(remote)).stdout == ""


def test_clean_orphan_pr_assets_push_is_accepted(asset_repo, tmp_path):
    # The documented screenshot flow: an orphan pr-assets branch shares no
    # history with main, so it gets the secret scans but not update-reach.
    repo, git, commit, _ = asset_repo
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))
    # The fixture's sentinel rule matches its own text in the whole-tree scan;
    # a self-excluding regex keeps this clean push clean.
    config = repo / ".gitleaks.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        'regex = "LOCAL_CI_TEST_SECRET"', 'regex = "LOCAL_CI_TEST_SECRE[T]"'), encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "Code history")
    code_branch = git("branch", "--show-current").stdout.strip()
    git("checkout", "-q", "--orphan", "pr-assets")
    git("rm", "-rq", "--cached", ".")
    commit("pr-123/screenshot.png")
    # As documented: push it from the code checkout, not from the orphan tree.
    git("checkout", "-q", "-f", code_branch)
    result = git("push", str(remote), "pr-assets:refs/heads/pr-assets", check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret scan passed for pr-assets" in result.stdout + result.stderr
    assert git("ls-remote", str(remote)).stdout != ""


def test_windows_known_failures_list_is_well_formed():
    sys.path.insert(0, str(ROOT / "scripts" / "ci"))
    import windows_known_failures as known  # noqa: E402  pyright: ignore[reportMissingImports]

    tests, files = known.load()
    # All listed failures are retired. New regressions must not become skips.
    assert (tests, files) == (set(), set())
    assert known.base_id("fluid\\tests\\unit\\test_x.py::test_y[a-b]") == "fluid/tests/unit/test_x.py::test_y"

    # fluid's rootdir is fluid/, so its ids lack the prefix; the path restores it.
    class Item:
        path = ROOT / "fluid/tests/unit/test_x.py"
        nodeid = "tests/unit/test_x.py::TestA::test_y[1]"
    assert known.repo_id(Item()) == "fluid/tests/unit/test_x.py::TestA::test_y"


@pytest.mark.parametrize("files,expected", [
    (["app/core/deploy_status.py"], {"backend"}),
    (["fluid/fluid/engine/delegated.py"], {"fluid", "backend"}),
    (["frontend/src/lib/api.ts"], {"frontend", "mobile"}),
    (["desktop/src/new.ts"], {"desktop"}),
    (["frontend/src/lib/docs/search.ts"], {"frontend", "mobile"}),
    (["scripts/local_ci.py", "scripts/ci/test-cost-policy.py"], {"workflows"}),
    (["CONTRIBUTING.md", "docs/ci/pr-checks.md", "frontend/src/lib/docs/content/setup.md"], {"docs"}),
])
def test_fast_reuses_area_map_and_docs_only(files, expected):
    assert {suite.name for suite in local_ci.affected_suites(files)} == expected


def test_fast_unknown_area_defers_to_pr_ci():
    assert local_ci.affected_suites(["new-area/config.json"]) == []
    assert {suite.name for suite in local_ci.affected_suites([
        "new-area/config.json", "frontend/src/lib/api.ts",
    ])} == {"frontend", "mobile"}


@pytest.mark.parametrize("remote", [None, "unknown", "https://example.invalid/repo.git"])
def test_push_base_falls_back_for_non_remote_targets(monkeypatch, remote):
    monkeypatch.setattr(local_ci, "git", lambda *args: "origin")
    monkeypatch.setattr(local_ci, "resolve_base", lambda: "origin/main")
    fetched = []
    monkeypatch.setattr(local_ci, "fetch_base", fetched.append)
    assert local_ci.fetch_push_base(remote) == "origin/main"
    assert fetched == ["origin/main"]


def test_push_base_prefers_the_configured_push_remote(monkeypatch):
    monkeypatch.setattr(local_ci, "git", lambda *args: "origin\nfork")
    monkeypatch.setattr(local_ci, "resolve_base", lambda: pytest.fail("push remote has main"))
    fetched = []
    monkeypatch.setattr(local_ci, "fetch_base", fetched.append)
    assert local_ci.fetch_push_base("fork") == "fork/main"
    assert fetched == ["fork/main"]


def test_push_base_falls_back_when_configured_remote_has_no_main(monkeypatch):
    monkeypatch.setattr(local_ci, "git", lambda *args: "origin\nfork")
    monkeypatch.setattr(local_ci, "resolve_base", lambda: "origin/main")
    fetched = []
    def fetch(target):
        fetched.append(target)
        if target == "fork/main":
            raise local_ci.Failure("no main branch")
    monkeypatch.setattr(local_ci, "fetch_base", fetch)
    assert local_ci.fetch_push_base("fork") == "origin/main"
    assert fetched == ["fork/main", "origin/main"]


def test_fast_backend_unmapped_paths_defer_to_pr_ci(monkeypatch, tmp_path):
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    mapped = local_ci.affected_backend_tests(context(["app/core/deploy_status.py"]))
    assert "tests/core/test_deploy_status.py" in mapped
    for unknown in ("app/core/unmapped.py", "app/core/new-schema.json"):
        ctx = context([unknown])
        assert local_ci.affected_backend_tests(ctx) == []
        assert ctx.notes == [f"No local test mapping for {unknown}; covered by the full suite on PR CI"]
        assert local_ci.affected_backend_tests(context([
            "app/core/deploy_status.py", unknown])) == mapped


@pytest.mark.parametrize("name", [
    "fluid/pyproject.toml", "fluid/tests/unit/data/schema.json", "fluid/fluid/unmapped.py",
])
def test_fast_fluid_only_paths_use_fluid_not_backend(monkeypatch, tmp_path, name):
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    assert {suite.name for suite in local_ci.affected_suites([name])} == {"fluid"}
    ctx = context([name])
    assert local_ci.affected_backend_tests(ctx) == []
    assert ctx.notes == []  # These are covered locally by the Fluid suite.


@pytest.mark.parametrize("test_name,content", [
    ("test_importer.py", "from fluid.engine import sample\n"),
    ("test_sample.py", "# Coverage selected by test filename.\n"),
])
def test_fast_fluid_module_keeps_matching_backend_tests(monkeypatch, tmp_path, test_name, content):
    monkeypatch.setattr(local_ci, "ROOT", tmp_path)
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    name = "tests/core/" + test_name
    test = tmp_path / name
    test.parent.mkdir(parents=True)
    test.write_text(content, encoding="utf-8")
    paths = ["fluid/fluid/engine/sample.py"]
    assert local_ci.affected_backend_tests(context(paths)) == [name]
    assert {suite.name for suite in local_ci.affected_suites(paths)} == {"backend", "fluid"}


def test_fast_unmapped_backend_helper_defers_with_note(monkeypatch, tmp_path):
    monkeypatch.setattr(local_ci, "ROOT", tmp_path)
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    test = tmp_path / "tests/core/test_unrelated.py"
    test.parent.mkdir(parents=True)
    test.write_text("# No helper references.\n", encoding="utf-8")
    name = "tests/core/unmapped_helper.py"
    ctx = context([name])
    assert local_ci.affected_backend_tests(ctx) == []
    assert ctx.notes == [f"No local test mapping for {name}; covered by the full suite on PR CI"]


@pytest.mark.parametrize("test_name,content", [
    ("test_flow.py", "from .query_engine_flow_harness import make_flow\n"),
    ("test_flow.py", "from tests.core.query_engine_flow_harness import make_flow\n"),
    ("test_flow.py", "from tests.core import query_engine_flow_harness\n"),
    ("test_flow.py", "import query_engine_flow_harness\n"),
    ("test_flow.py", "from . import query_engine_flow_harness\n"),
    ("test_query_engine_flow_harness.py", "# Named coverage for the helper.\n"),
])
def test_fast_backend_test_helpers_select_importers(monkeypatch, tmp_path, test_name, content):
    monkeypatch.setattr(local_ci, "ROOT", tmp_path)
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    name = "tests/core/" + test_name
    test = tmp_path / name
    test.parent.mkdir(parents=True)
    test.write_text(content, encoding="utf-8")
    assert local_ci.affected_backend_tests(context([
        "tests/core/query_engine_flow_harness.py",
    ])) == [name]


@pytest.mark.parametrize("name", [
    "requirements.txt", "requirements-dev.txt", "pytest.ini", "tests/conftest.py",
    "tests/core/conftest.py",
])
def test_fast_shared_pytest_inputs_still_select_all(monkeypatch, tmp_path, name):
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    assert local_ci.affected_backend_tests(context([name])) == "all"


@pytest.mark.parametrize("name", [
    "tests/new_area/test_added.py",
    "tests/core/test_container_runtime.py",
])
def test_changed_backend_test_selects_itself_outside_the_fast_subset(monkeypatch, tmp_path, name):
    monkeypatch.setattr(local_ci, "ROOT", tmp_path)
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("def test_example(): pass\n", encoding="utf-8")
    assert local_ci.select_by_name([name]) == [name]
    assert local_ci.affected_backend_tests(context([name])) == [name]


def test_changed_fluid_test_is_owned_by_fluid_not_backend(monkeypatch, tmp_path):
    name = "fluid/tests/unit/test_delegated_spawn_contract.py"
    monkeypatch.setattr(local_ci, "MAP_PATH", tmp_path / "absent-map")
    assert local_ci.select_by_name([name]) == [name]
    assert local_ci.affected_backend_tests(context([name])) == []
    assert {suite.name for suite in local_ci.affected_suites([name])} == {"fluid"}
    selected = local_ci.affected_backend_tests(context([
        "tests/core/test_deploy_status.py", name,
    ]))
    assert selected == ["tests/core/test_deploy_status.py"]


def test_fast_fluid_test_change_runs_only_that_file(monkeypatch):
    name = "fluid/tests/unit/test_delegated_spawn_contract.py"
    commands = []
    monkeypatch.setattr(local_ci, "run", lambda argv, **kwargs: commands.append(argv))
    monkeypatch.setattr(local_ci, "pytest_args", lambda python: ["-q"])
    ctx = context([name])
    ctx.mode = "fast"
    local_ci.fluid(ctx)
    assert commands == [[ctx.python, "-m", "pytest", name, "-q"]]


def test_fast_failed_dependency_map_still_falls_back_safely(monkeypatch, tmp_path):
    from types import SimpleNamespace
    mapping = tmp_path / "map"
    mapping.touch()
    monkeypatch.setattr(local_ci, "MAP_PATH", mapping)
    monkeypatch.setattr(local_ci.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stderr="unreadable map", stdout=""))
    ctx = context(["app/core/deploy_status.py", "app/core/unmapped.py"])
    ctx.mode = "fast"
    monkeypatch.setattr(local_ci, "changed_files", lambda base: ctx.files)
    assert "tests/core/test_deploy_status.py" in local_ci.affected_backend_tests(ctx)
    assert local_ci.affected_backend_tests(context(["unknown-area/config.json"]), use_map=False) == []


def test_fast_map_does_not_include_unrelated_workflow_paths(monkeypatch, tmp_path):
    mapping = tmp_path / "map"
    mapping.touch()
    monkeypatch.setattr(local_ci, "MAP_PATH", mapping)
    monkeypatch.setattr(local_ci, "changed_files", lambda base: [
        "app/core/deploy_status.py", "scripts/local_ci.py"])
    monkeypatch.setattr(local_ci, "select_backend_tests",
                        lambda ctx: pytest.fail("map CLI would include workflow changes"))
    assert "tests/core/test_deploy_status.py" in local_ci.affected_backend_tests(
        context(["app/core/deploy_status.py"]))


def test_fast_reuses_recorded_backend_map(monkeypatch, tmp_path):
    mapping = tmp_path / "map"
    mapping.touch()
    monkeypatch.setattr(local_ci, "MAP_PATH", mapping)
    calls = []
    ctx = context(["app/core/deploy_status.py"])
    monkeypatch.setattr(local_ci, "changed_files", lambda base: ctx.files)
    monkeypatch.setattr(local_ci, "select_backend_tests",
                        lambda ctx: calls.append(ctx) or ["tests/core/test_deploy_status.py"])
    assert local_ci.affected_backend_tests(ctx) == ["tests/core/test_deploy_status.py"]
    assert calls == [ctx]


def test_fast_recorded_map_all_uses_name_matching_unless_shared_inputs(monkeypatch, tmp_path):
    mapping = tmp_path / "map"
    mapping.touch()
    monkeypatch.setattr(local_ci, "MAP_PATH", mapping)
    monkeypatch.setattr(local_ci, "changed_files", lambda base: ["app/core/unmapped.py"])
    monkeypatch.setattr(local_ci, "select_backend_tests", lambda ctx: "all")
    ctx = context(["app/core/unmapped.py"])
    assert local_ci.affected_backend_tests(ctx) == []
    assert ctx.notes == ["No local test mapping for app/core/unmapped.py; covered by the full suite on PR CI"]
    assert local_ci.affected_backend_tests(context(["requirements.txt"])) == "all"


def gate_fixture(repo, git):
    # This commits the gate itself; the fixture's sentinel rule deliberately
    # matches its own regex, so use the production config here.
    (repo / ".gitleaks.toml").write_bytes((ROOT / ".gitleaks.toml").read_bytes())
    for name in ("scripts/ci/update_reach.py", "scripts/source_archive.py"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    git("add", ".")
    git("commit", "-qm", "Gate fixture baseline")
    git("branch", "main")


@pytest.mark.parametrize("target_kind", ["configured-remote", "url"])
def test_real_push_falls_back_when_target_has_no_main(asset_repo, tmp_path, target_kind):
    repo, git, commit, _ = asset_repo
    gate_fixture(repo, git)
    git("remote", "add", "compare", str(repo))
    git("config", "local-ci.base", "compare/main")
    remote = tmp_path / "publish.git"
    git("init", "--bare", "-q", str(remote))
    target = str(remote)
    if target_kind == "configured-remote":
        git("remote", "add", "publish", target)
        target = "publish"
    tip = commit("docs/change.md", "Documentation change")
    for ref in ("docs-fixture", "docs-second"):
        result = git("push", target, f"HEAD:refs/heads/{ref}", check=False)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        # Every push scans again; none runs a test suite.
        assert output.count("Secret scan (offline provider rules): 0 finding(s)") == 2
        assert "update-reach passed; tests run in PR CI" in output
        assert "Affected checks" not in output and "=== " not in output
        assert git("ls-remote", str(remote), f"refs/heads/{ref}").stdout.split()[0] == tip


@pytest.mark.parametrize("name", ["docs/change.md", "app/core/example.py", "new-area/config.json"])
def test_real_hook_push_gate_runs_no_tests(asset_repo, name):
    repo, git, commit, pre_push = asset_repo
    gate_fixture(repo, git)
    git("remote", "add", "test", str(repo))
    git("config", "local-ci.base", "test/main")
    tip = commit(name, "value = 1\n")
    for _ in range(2):
        result = pre_push(tip)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("Secret scan (offline provider rules): 0 finding(s)") == 2
        assert "Passed: every change" in result.stdout  # update-reach, live on every push
        assert "Affected checks" not in result.stdout and "pytest" not in result.stdout
    assert git("status", "--porcelain").stdout == ""


@pytest.mark.parametrize("cached", [False, True])
def test_scan_failure_blocks_even_a_recorded_tree(records, monkeypatch, cached):
    head = "h" * 40
    if cached:
        local_ci.save_record("tree", {"mode": "fast", "security_gate": local_ci.SECURITY_GATE_VERSION})
    monkeypatch.setattr(sys, "stdin", io.StringIO(ref_line(head)))
    monkeypatch.setattr(local_ci, "git", lambda *args: head)
    monkeypatch.setattr(local_ci, "tree_of", lambda sha: "tree")
    monkeypatch.setattr(local_ci, "push_update_reach", lambda *args: pytest.fail("reach before scan"))
    def fail(*args):
        raise local_ci.Failure("scanner rejected range")
    monkeypatch.setattr(local_ci, "scan_push", fail)
    with pytest.raises(local_ci.Failure, match="scanner rejected"):
        local_ci.cmd_pre_push(local_ci.argparse.Namespace())


def test_fast_explicit_runs_scan_on_every_invocation(monkeypatch):
    scans = []
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "run", lambda argv, **kwargs: scans.append(argv))
    monkeypatch.setattr(local_ci, "resolve_base", lambda: "test/main")
    monkeypatch.setattr(local_ci, "fetch_base", lambda target: None)
    monkeypatch.setattr(local_ci, "comparison_base", lambda *args: "base")
    monkeypatch.setattr(local_ci, "changed_files", lambda base: ["docs/change.md"])
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: False)
    args = local_ci.argparse.Namespace(attest=False, only=None)
    for _ in range(2):
        assert local_ci.cmd_fast(args) == 0
    assert scans == [[sys.executable, "scripts/secret_scan.py"]] * 2


@pytest.mark.parametrize("command,mode", [("check", "changed"), ("full", "full")])
def test_existing_check_and_full_still_run_original_suites(records, monkeypatch, command, mode):
    calls = []
    monkeypatch.setattr(local_ci, "git", lambda *args: "head")
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: False)
    monkeypatch.setattr(local_ci, "security_check", lambda *args, **kwargs: calls.append("security"))
    monkeypatch.setattr(local_ci, "run_suites", lambda mode, *args: calls.append(mode) or ([], [], []))
    monkeypatch.setattr(local_ci, "cmd_fast", lambda args: pytest.fail("fast replaced existing command"))
    assert local_ci.cmd_check(local_ci.argparse.Namespace(command=command, attest=False, only=None)) == 0
    assert calls == ["security", mode]


def test_fast_cannot_attest_or_limit_suites():
    for attest, only in [(True, None), (False, ["docs"])]:
        with pytest.raises(local_ci.Failure, match="fast does not accept"):
            local_ci.cmd_fast(local_ci.argparse.Namespace(attest=attest, only=only))


def test_hook_finds_the_script_and_chains():
    hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
    assert hook.startswith("#!/bin/sh")
    assert local_ci.HOOK_MARKER in hook
    assert 'scripts/local_ci.py" pre-push' in hook
    assert "pre-push.user" in hook


def test_install_adds_a_hook_without_touching_hooks_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(local_ci.subprocess, "run", lambda *a, **k: calls.append(a))
    message = local_ci.install_hook(tmp_path)
    installed = (tmp_path / "pre-push").read_bytes()
    assert installed == (ROOT / ".githooks/pre-push").read_bytes().replace(b"\r\n", b"\n")
    assert not (tmp_path / "pre-push.user").exists()
    assert "pre-push.user" not in message
    assert calls == []  # core.hooksPath is left alone
    # Installing again replaces our own hook in place.
    local_ci.install_hook(tmp_path)
    assert not (tmp_path / "pre-push.user").exists()


@pytest.mark.parametrize("source_version,installed_version", [(3, 4), (4, 5)])
def test_install_refuses_to_downgrade_shared_hook(tmp_path, monkeypatch, source_version, installed_version):
    source = tmp_path / "source"
    source.write_text(f"#!/bin/sh\n{local_ci.HOOK_MARKER} v{source_version}\n")
    monkeypatch.setattr(local_ci, "HOOK_SOURCE", source)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    target = hooks / "pre-push"
    installed = f"#!/bin/sh\n{local_ci.HOOK_MARKER} v{installed_version}\n"
    target.write_text(installed)
    with pytest.raises(local_ci.Failure, match="newer.*--force"):
        local_ci.install_hook(hooks)
    assert target.read_text() == installed
    assert not (hooks / "pre-push.user").exists()
    configured = []
    monkeypatch.setattr(local_ci, "find_test_python", lambda explicit: "new-python")
    monkeypatch.setattr(local_ci, "hooks_dir", lambda: hooks)
    monkeypatch.setattr(local_ci, "git", lambda *args: configured.append(args))
    args = local_ci.argparse.Namespace(python=None, force=False)
    with pytest.raises(local_ci.Failure, match="newer.*--force"):
        local_ci.cmd_install(args)
    assert configured == []
    assert target.read_text() == installed
    args.force = True
    assert local_ci.cmd_install(args) == 0
    assert configured == [("config", local_ci.PYTHON_CONFIG, "new-python")]
    assert target.read_bytes() == source.read_bytes().replace(b"\r\n", b"\n")


def test_install_skips_a_python_without_pytest(tmp_path, monkeypatch):
    # ServerWorker: python3 on PATH is the app venv (no pytest); /usr/local/bin/python3 has it.
    venv, system = tmp_path / "venv-python", tmp_path / "system-python"
    venv.write_text("")
    system.write_text("")
    monkeypatch.delenv("LOCAL_CI_PYTHON", raising=False)
    monkeypatch.setattr(local_ci, "saved_python", lambda: "")
    monkeypatch.setattr(local_ci.sys, "executable", str(venv))
    monkeypatch.setattr(local_ci, "can_run_tests", lambda python: python == str(system))
    with pytest.raises(local_ci.Failure):
        local_ci.find_test_python(str(venv))
    monkeypatch.setattr(local_ci.shutil, "which", lambda name: str(system) if name == "python3" else None)
    assert local_ci.find_test_python() == str(system)

    configured = []
    monkeypatch.setattr(local_ci, "git", lambda *args: configured.append(args) or "")
    monkeypatch.setattr(local_ci, "hooks_dir", lambda: tmp_path / "hooks")
    assert local_ci.cmd_install(local_ci.argparse.Namespace(python=None)) == 0
    assert configured == [("config", local_ci.PYTHON_CONFIG, str(system))]


def test_hook_prefers_the_saved_python():
    hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
    assert "git config --get local-ci.python" in hook
    assert '"$python" "$root/scripts/local_ci.py" pre-push' in hook


def test_install_keeps_an_earlier_hook_as_user(tmp_path):
    (tmp_path / "pre-push").write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
    message = local_ci.install_hook(tmp_path)
    assert (tmp_path / "pre-push.user").read_text(encoding="utf-8") == "#!/bin/sh\necho theirs\n"
    assert local_ci.HOOK_MARKER in (tmp_path / "pre-push").read_text(encoding="utf-8")
    assert "pre-push.user" in message


def test_install_refuses_to_overwrite_two_foreign_hooks(tmp_path):
    (tmp_path / "pre-push").write_text("theirs", encoding="utf-8")
    (tmp_path / "pre-push.user").write_text("older", encoding="utf-8")
    with pytest.raises(local_ci.Failure):
        local_ci.install_hook(tmp_path)
    assert (tmp_path / "pre-push").read_text(encoding="utf-8") == "theirs"

@pytest.mark.parametrize("state,labels,gh_error,expected,note", [
    (None, [], "no pull requests found for branch", 0, "no PR exists"),
    (True, [], "", 0, "PR is a draft"),
    (False, [], "", 1, ""),
    (False, ["update-reach:windows-launcher"], "", 0, ""),
    (None, [], "authentication required", 0, "authentication or API failure"),
])
def test_update_reach_pr_policy(monkeypatch, capsys, state, labels, gh_error, expected, note):
    import importlib.util
    import json
    import subprocess

    spec = importlib.util.spec_from_file_location("reach_policy", ROOT / "scripts/ci/update_reach.py")
    reach = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reach)
    # Drive the real classifier and CLI verdict using a changed launcher entry.
    monkeypatch.setattr(reach, "git", lambda *a: "desktop/src/main/managed-entry.ts")
    # The head raises launcher.json's version, as a launcher change must (#97).
    monkeypatch.setattr(reach, "reader", lambda rev: lambda path:
                        b"export const version = 2;" if path == reach.LAUNCHER_ENTRY
                        else b'{"version": 2, "minimum": 0}' if path == reach.LAUNCHER_JSON and rev == "HEAD"
                        else None)
    monkeypatch.setattr(local_ci.shutil, "which", lambda name: "/bin/gh")
    calls = []

    def gh(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if gh_error else 0,
                                           json.dumps({"isDraft": state, "labels": [{"name": x} for x in labels]}),
                                           gh_error)

    def run(argv):
        assert argv[1] == "scripts/ci/update_reach.py"
        if reach.main(argv[2:]):
            raise local_ci.Failure("update-reach failed")

    monkeypatch.setattr(local_ci.subprocess, "run", gh)
    monkeypatch.setattr(local_ci, "run", run)
    monkeypatch.setattr(local_ci, "run_suites", lambda *a: (local_ci.update_reach(context([])), [], []))
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: False)
    assert local_ci.main(["check"]) == expected
    assert calls == [["gh", "pr", "view", "--json", "isDraft,labels"]]
    output = capsys.readouterr().out
    if note:
        assert note in output
        assert "NOTE — OUT OF REACH: windows-launcher" in output
        assert "update-reach:windows-launcher" in output
        assert "FAILED:" not in output
    elif expected:
        assert "FAILED:" in output and "update-reach:windows-launcher" in output
    else:
        assert "ACKNOWLEDGED: windows-launcher" in output


@pytest.mark.parametrize("failure", ["missing", "timeout", "invalid"])
def test_unavailable_gh_is_explained(monkeypatch, capsys, failure):
    import subprocess

    monkeypatch.setattr(local_ci.shutil, "which", lambda name: None if failure == "missing" else "/bin/gh")
    def gh(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("gh", 20)
        return subprocess.CompletedProcess([], 0, "not JSON", "")
    monkeypatch.setattr(local_ci.subprocess, "run", gh)
    commands = []
    monkeypatch.setattr(local_ci, "run", commands.append)
    local_ci.update_reach(context([]))
    assert "--draft" in commands[0]
    assert "unavailable" in capsys.readouterr().out


def test_recorded_draft_pass_rechecks_reach_on_push(records, monkeypatch):
    local_ci.save_record("tree", {"security_gate": local_ci.SECURITY_GATE_VERSION})
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    monkeypatch.setattr(local_ci, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(local_ci, "scan_push", lambda *a: None)
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "fetch_push_base", lambda remote: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(ref_line("head")))
    calls = []
    def refuse(sha, ref):
        calls.append((sha, ref))
        raise local_ci.Failure("ready PR lacks update-reach:windows-launcher")
    monkeypatch.setattr(local_ci, "push_update_reach", refuse)
    assert local_ci.main(["pre-push"]) == 1
    assert calls == [("head", "refs/heads/topic")]


@pytest.mark.parametrize("accepted", [False, True])
def test_attestation_requires_reach_acceptance(monkeypatch, accepted):
    from ci import edge_publication_gate

    monkeypatch.setattr(edge_publication_gate, "update_reach_readiness", lambda sha: (accepted, "PR #42 needs label"))
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: True)
    monkeypatch.setattr(local_ci, "resolve_base", lambda **kw: "public/main")
    monkeypatch.setattr(local_ci, "fetch_base", lambda base, **kw: None)
    monkeypatch.setattr(local_ci.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "run_suites", lambda *a: ([], [], []))
    monkeypatch.setattr(local_ci, "save_record", lambda *a: None)
    posted = []
    descriptions = []

    def record(state, description, revision):
        posted.append(state)
        descriptions.append(description)

    monkeypatch.setattr(local_ci, "attest", record)
    assert local_ci.main(["full", "--attest"]) == (0 if accepted else 1)
    assert posted == (["pending", "success"] if accepted else [])
    assert all("known Windows failures skipped" not in text for text in descriptions)


@pytest.mark.parametrize("state", ["pending", "success", "failure"])
def test_attestation_warns_after_main_moves(monkeypatch, capsys, state):
    posted = []
    monkeypatch.setattr(local_ci.subprocess, "run", lambda argv, **kw: posted.append(argv))
    monkeypatch.setattr(local_ci.subprocess, "check_output", lambda *a, **kw: "b" * 40)
    local_ci.attest(state, "Full suite result", "a" * 40)
    assert posted and f"state={state}" in posted[0]
    output = capsys.readouterr().out
    assert "WARNING: main moved" in output
    assert f"posted {state}" in output
    assert "first-parent ancestor" in output and "no newer attestation wins" in output
    assert "Later commits still need their own full attestation" in output


def test_attestation_main_recheck_unavailable_warns(monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(local_ci.subprocess, "check_output", unavailable)
    local_ci.warn_attestation_main_moved("a" * 40, "success")
    assert "could not recheck main" in capsys.readouterr().out


def test_attestation_unchanged_main_does_not_warn(monkeypatch, capsys):
    monkeypatch.setattr(local_ci.subprocess, "check_output", lambda *a, **kw: "a" * 40)
    local_ci.warn_attestation_main_moved("a" * 40, "success")
    assert not capsys.readouterr().out


def test_attestation_rechecks_labels_before_success(monkeypatch):
    from ci import edge_publication_gate

    decisions = iter([(True, "accepted"), (False, "PR #42 label removed")])
    monkeypatch.setattr(edge_publication_gate, "update_reach_readiness", lambda sha: next(decisions))
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: True)
    monkeypatch.setattr(local_ci, "resolve_base", lambda **kw: "public/main")
    monkeypatch.setattr(local_ci, "fetch_base", lambda base, **kw: None)
    monkeypatch.setattr(local_ci.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "run_suites", lambda *a: ([], [], []))
    posted = []
    monkeypatch.setattr(local_ci, "attest", lambda state, *a: posted.append(state))
    assert local_ci.main(["full", "--attest"]) == 1
    assert posted == ["pending", "failure"]

@pytest.fixture
def base_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(local_ci, "ROOT", tmp_path)
    monkeypatch.delenv("LOCAL_CI_BASE", raising=False)
    git = local_ci.git
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("remote", "add", "origin", "https://github.com/example/archive.git")
    git("remote", "add", "public", "https://github.com/HobbyCoders/ai-shuffle.git")
    return git


@pytest.mark.parametrize("url", [
    "https://github.com/HobbyCoders/ai-shuffle.git",
    "https://github.com/hobbycoders/ai-shuffle/",
    "git@github.com:HobbyCoders/ai-shuffle.git",
    "ssh://git@github.com/HobbyCoders/ai-shuffle.git",
])
def test_base_selects_public_remote_not_origin(base_repo, url):
    base_repo("remote", "set-url", "public", url)
    assert local_ci.resolve_base() == "public/main"
    base_repo("remote", "set-url", "origin", url)
    assert local_ci.resolve_base() == "origin/main"


def test_base_override_precedence_and_attest_is_independent(base_repo, monkeypatch):
    base_repo("config", "local-ci.base", "origin/topic")
    assert local_ci.resolve_base() == "origin/topic"
    monkeypatch.setenv("LOCAL_CI_BASE", "refs/remotes/public/release/topic")
    assert local_ci.resolve_base() == "public/release/topic"
    assert local_ci.resolve_base(public_only=True) == "public/main"


def test_missing_public_remote_explains_override_but_cannot_attest(base_repo, monkeypatch):
    base_repo("remote", "remove", "public")
    with pytest.raises(local_ci.Failure, match="No remote points to.*LOCAL_CI_BASE"):
        local_ci.resolve_base()
    monkeypatch.setenv("LOCAL_CI_BASE", "origin/main")
    assert local_ci.resolve_base() == "origin/main"
    with pytest.raises(local_ci.Failure, match="for --attest"):
        local_ci.resolve_base(public_only=True)


def test_fetch_targets_resolved_tracking_ref(base_repo, monkeypatch):
    calls = []

    def git(*args):
        calls.append(args)
        if args[0] == "fetch":
            return ""
        return base_repo(*args)

    monkeypatch.setattr(local_ci, "git", git)
    local_ci.fetch_base("public/release/topic")
    assert calls[-1] == ("fetch", "-q", "--", "public",
                         "+refs/heads/release/topic:refs/remotes/public/release/topic")
    with pytest.raises(local_ci.Failure, match="expected <remote>/<branch>"):
        local_ci.fetch_base("HEAD")


def test_fetch_refreshes_a_real_remote_tracking_ref(base_repo):
    base_repo("commit", "--allow-empty", "-qm", "Root")
    revision = base_repo("rev-parse", "HEAD")
    base_repo("update-ref", "refs/heads/main", revision)
    base_repo("remote", "add", "mirror", str(local_ci.ROOT))
    local_ci.fetch_base("mirror/main")
    assert base_repo("rev-parse", "refs/remotes/mirror/main") == revision


def _failing_fetch(base_repo, monkeypatch):
    calls = []

    def git(*args):
        if args[0] == "fetch":
            calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        return base_repo(*args)

    monkeypatch.setattr(local_ci, "git", git)
    monkeypatch.setattr(local_ci.time, "sleep", lambda seconds: None)
    return calls


def test_fetch_failure_without_a_local_base_stops_checks(base_repo, monkeypatch):
    calls = _failing_fetch(base_repo, monkeypatch)
    with pytest.raises(local_ci.Failure, match="Could not refresh.*public/main"):
        local_ci.run_suites("changed", None, sys.executable)
    assert len(calls) == 2  # retried once


def test_fetch_failure_keeps_the_existing_base_for_changed_checks(base_repo, monkeypatch, capsys):
    # Concurrent local CI runs in worktrees of one clone race for the ref lock.
    base_repo("commit", "--allow-empty", "-qm", "Public root")
    base_repo("update-ref", "refs/remotes/public/main", base_repo("rev-parse", "HEAD"))
    _failing_fetch(base_repo, monkeypatch)
    local_ci.fetch_base("public/main")
    assert "comparing with the copy already fetched" in capsys.readouterr().out
    with pytest.raises(local_ci.Failure, match="Could not refresh"):
        local_ci.fetch_base("public/main", strict=True)


def test_changed_suites_and_push_use_public_history(base_repo, monkeypatch):
    base_repo("commit", "--allow-empty", "-qm", "Public root")
    public = base_repo("rev-parse", "HEAD")
    tree = base_repo("rev-parse", "HEAD^{tree}")
    archive = base_repo("commit-tree", tree, "-m", "Unrelated archive root")
    base_repo("update-ref", "refs/remotes/origin/main", archive)
    base_repo("update-ref", "refs/remotes/public/main", public)
    (local_ci.ROOT / "example.py").write_text("value = 1\n")
    base_repo("add", "example.py")
    base_repo("commit", "-qm", "Change")
    fetched, contexts = [], []
    monkeypatch.setattr(local_ci, "fetch_base", lambda base, **kw: fetched.append(base))
    monkeypatch.setattr(local_ci, "SUITES", [local_ci.Suite("example", ["*.py"], contexts.append, "test")])
    monkeypatch.setattr(local_ci, "NOT_LOCAL", [])
    assert local_ci.run_suites("changed", None, sys.executable) == (["example"], [], [])
    assert fetched == ["public/main"]
    assert contexts[0].base == public
    assert contexts[0].files == ["example.py"]
    monkeypatch.setattr(local_ci, "update_reach", lambda ctx, **kw: contexts.append(ctx))
    local_ci.push_update_reach("HEAD", "refs/heads/topic")
    assert contexts[-1].base == public
    with pytest.raises(local_ci.Failure, match="No common history.*origin/main"):
        local_ci.comparison_base("origin/main", "HEAD")


@pytest.mark.parametrize("clean,public_head", [(False, "head"), (True, "other")])
def test_attestation_refuses_dirty_or_non_public_head(base_repo, monkeypatch, clean, public_head):
    monkeypatch.setenv("LOCAL_CI_BASE", "origin/main")
    fetched, posted = [], []
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: clean)
    monkeypatch.setattr(local_ci, "fetch_base", lambda base, **kw: fetched.append(base))
    monkeypatch.setattr(local_ci, "attest", lambda *args: posted.append(args))

    def git(*args):
        if args == ("rev-parse", "HEAD") or args == ("rev-parse", "origin/main"):
            return "head"
        if args == ("rev-parse", "public/main"):
            return public_head
        return base_repo(*args)

    monkeypatch.setattr(local_ci, "git", git)
    with pytest.raises(local_ci.Failure, match="current public main"):
        local_ci.cmd_check(local_ci.argparse.Namespace(command="full", attest=True, only=None))
    assert fetched == ["public/main"]
    assert posted == []


@pytest.mark.parametrize("resumed,history,ok", [
    (True, "tip\nhead\nroot", True),   # staged run started on head, then main moved: finish it
    (False, "tip\nhead\nroot", False),  # a new run must start on the current tip
    (True, "tip\nroot", False),          # head is not (or no longer) a merged main revision
])
def test_a_started_staged_attest_survives_main_moving(base_repo, monkeypatch, capsys, resumed, history, ok):
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: True)
    monkeypatch.setattr(local_ci, "fetch_base", lambda base, **kw: None)
    monkeypatch.setattr(local_ci, "resolve_base", lambda public_only=False: "public/main")
    monkeypatch.setattr(local_ci, "use_private_checkout", lambda: None)
    reach = []
    monkeypatch.setattr(local_ci, "attest_update_reach", reach.append)

    def git(*args):
        if args == ("rev-parse", "public/main"):
            return "tip"
        if args == ("rev-list", "--first-parent", "public/main"):
            return history
        return base_repo(*args)

    monkeypatch.setattr(local_ci, "git", git)
    if ok:
        local_ci.attest_preflight("head", resumed=resumed)
        assert reach == ["head"]
        assert "Main moved to tip after this staged run started; finishing head" in capsys.readouterr().out
    else:
        with pytest.raises(local_ci.Failure, match="current public main"):
            local_ci.attest_preflight("head", resumed=resumed)
        assert reach == []


def test_step_attest_resumes_only_a_run_started_on_this_checkout(monkeypatch):
    import local_ci_stages
    seen = []
    monkeypatch.setattr(local_ci, "choose_python", lambda: sys.executable)
    monkeypatch.setattr(local_ci, "python_preflight", lambda python: None)
    monkeypatch.setattr(local_ci, "attest_preflight", lambda revision, resumed=False: seen.append(resumed))
    monkeypatch.setattr(local_ci_stages, "run_step", lambda *a: 0)
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    for started, restart, expected in [(True, False, True), (False, False, False), (True, True, False)]:
        monkeypatch.setattr(local_ci_stages, "started", lambda ci, revision, s=started: s)
        args = local_ci.argparse.Namespace(command="full", attest=True, only=None, step=True, restart=restart)
        assert local_ci.cmd_check(args) == 0
        assert seen[-1] is expected


def test_checks_never_inherit_the_pushing_repository_from_git(monkeypatch, tmp_path):
    """A pre-push hook runs with GIT_DIR set (in a worktree). Test fixtures
    that call `git -C <tmp> init/commit/config` must not reach this repo."""
    import subprocess

    real = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True)
    if real.returncode == 0:  # the list matches this git's own
        assert set(real.stdout.split()) <= local_ci.GIT_LOCAL_ENV
    monkeypatch.setenv("GIT_DIR", "/repo/.git/worktrees/w")
    monkeypatch.setenv("GIT_INDEX_FILE", "/repo/.git/worktrees/w/index")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kept")
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen.update(kwargs["env"])

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(local_ci.subprocess, "Popen", FakePopen)
    local_ci.run(["true"], env={"PYTHONPATH": "x"})
    assert not {"GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_KEY_0"} & set(seen)
    assert seen["GIT_AUTHOR_NAME"] == "kept" and seen["PYTHONPATH"] == "x" and "PATH" in seen


@pytest.mark.parametrize("argv", [["full", "--attest"], ["full", "--step", "--attest"]])
def test_attest_refusal_comes_before_any_status_or_suite(monkeypatch, argv):
    """Update reach (and its private-archive bridge) fails in seconds, not after the run."""
    order = []
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    monkeypatch.setattr(local_ci, "clean_checkout", lambda: True)
    monkeypatch.setattr(local_ci, "resolve_base", lambda **kw: "public/main")
    monkeypatch.setattr(local_ci, "fetch_base", lambda base, **kw: order.append("fetch"))
    monkeypatch.setattr(local_ci, "use_private_checkout", lambda: order.append("private"))

    def refuse(revision):
        order.append("reach")
        raise local_ci.Failure("Update reach refused: set AI_SHUFFLE_PRIVATE_CHECKOUT ...")
    monkeypatch.setattr(local_ci, "attest_update_reach", refuse)
    monkeypatch.setattr(local_ci, "python_preflight", lambda python: order.append("python"))
    monkeypatch.setattr(local_ci, "attest", lambda state, *a: order.append(state))
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: order.append("security"))
    monkeypatch.setattr(local_ci, "run_suites", lambda *a: order.append("suites"))
    assert local_ci.main(argv) == 1
    assert order == ["python", "fetch", "private", "reach"]


def _private_remote(git):
    git("remote", "set-url", "origin", "https://github.com/HobbyCoders/ai-shuffle-private.git")


def _no_fetch(monkeypatch, fetched):
    real = local_ci.git

    def git(*args):
        if args[0] == "fetch":
            fetched.append(args)
            raise subprocess.CalledProcessError(1, "git fetch")
        return real(*args)
    monkeypatch.setattr(local_ci, "git", git)


def test_private_checkout_is_detected_from_this_clones_archive_remote(base_repo, monkeypatch, capsys):
    monkeypatch.delenv("AI_SHUFFLE_PRIVATE_CHECKOUT", raising=False)
    _private_remote(base_repo)
    fetched = []
    _no_fetch(monkeypatch, fetched)
    local_ci.use_private_checkout()
    common = (local_ci.ROOT / ".git").resolve()
    assert os.environ["AI_SHUFFLE_PRIVATE_CHECKOUT"] == str(common)
    assert fetched == [("fetch", "-q", "--no-tags", "--", "origin", "+refs/heads/main:refs/remotes/origin/main")]
    out = capsys.readouterr().out
    assert "AI_SHUFFLE_PRIVATE_CHECKOUT is unset; using this clone's HobbyCoders/ai-shuffle-private remote 'origin'" in out
    assert "could not refresh origin/main" in out


def test_private_checkout_from_a_worktree_is_the_shared_git_directory(base_repo, monkeypatch, tmp_path):
    monkeypatch.delenv("AI_SHUFFLE_PRIVATE_CHECKOUT", raising=False)
    _private_remote(base_repo)
    base_repo("commit", "--allow-empty", "-qm", "root")
    worktree = tmp_path / "wt"
    base_repo("worktree", "add", "-q", "--detach", str(worktree))
    monkeypatch.setattr(local_ci, "ROOT", worktree)
    _no_fetch(monkeypatch, [])
    local_ci.use_private_checkout()
    assert os.environ["AI_SHUFFLE_PRIVATE_CHECKOUT"] == str((tmp_path / ".git").resolve())


def test_explicit_private_checkout_wins(base_repo, monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_PRIVATE_CHECKOUT", "/elsewhere/private")
    _private_remote(base_repo)
    local_ci.use_private_checkout()
    assert os.environ["AI_SHUFFLE_PRIVATE_CHECKOUT"] == "/elsewhere/private"


def test_no_archive_remote_leaves_the_variable_unset(base_repo, monkeypatch):
    monkeypatch.delenv("AI_SHUFFLE_PRIVATE_CHECKOUT", raising=False)
    local_ci.use_private_checkout()  # origin is example/archive, public is the public repo
    assert "AI_SHUFFLE_PRIVATE_CHECKOUT" not in os.environ


def test_shallow_clone_is_not_used_as_the_private_checkout(base_repo, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AI_SHUFFLE_PRIVATE_CHECKOUT", raising=False)
    for n in range(2):
        base_repo("commit", "--allow-empty", "-qm", f"c{n}")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=1", (tmp_path).as_uri(), str(shallow)], check=True,
                   env=local_ci.child_env())
    monkeypatch.setattr(local_ci, "ROOT", shallow)
    local_ci.git("remote", "set-url", "origin", "https://github.com/HobbyCoders/ai-shuffle-private.git")
    local_ci.use_private_checkout()
    assert "AI_SHUFFLE_PRIVATE_CHECKOUT" not in os.environ
    assert "this clone is shallow" in capsys.readouterr().out
