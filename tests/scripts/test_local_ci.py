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
    assert suites_for(["docs/content/setup.md"]) == {"docs"}
    assert suites_for(["README.md"]) == set()


def test_cloud_split_keeps_android_bridge_and_app_docs_independent():
    assert suites_for(["android/app/src/test/js/chat-downloads.test.cjs"]) >= {"android-bridge"}
    assert not {"hub", "site", "license", "uptime"} & {s.name for s in local_ci.SUITES}
    assert suites_for(["docs/content/getting-started/first-run.md"]) == {"docs"}


def test_full_mode_reaches_every_suite():
    ctx = local_ci.Context(mode="full", base="base", files=None, python=sys.executable)
    assert all(ctx.changed(suite.paths) for suite in local_ci.SUITES)


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
    monkeypatch.setattr(local_ci, "scan_push", lambda *args: None)
    monkeypatch.setattr(local_ci, "push_update_reach", lambda *args: None)
    monkeypatch.setattr(local_ci, "tree_of", lambda sha: (trees or {}).get(sha, "t-" + sha))
    ran = []
    monkeypatch.setattr(local_ci, "cmd_check", lambda args: ran.append(args.command) or check)
    result = local_ci.cmd_pre_push(local_ci.argparse.Namespace())
    return result, ran


def ref_line(sha, ref="refs/heads/topic"):
    return f"refs/heads/topic {sha} {ref} {'0' * 40}"


def test_pre_push_trusts_a_recorded_tree(records, monkeypatch):
    local_ci.save_record("t-" + "h" * 40, {"at": 1, "security_gate": local_ci.SECURITY_GATE_VERSION})
    assert push(monkeypatch, [ref_line("h" * 40)]) == (0, [])


def test_pre_push_runs_checks_for_a_clean_unrecorded_head(records, monkeypatch):
    assert push(monkeypatch, [ref_line("h" * 40)]) == (0, ["check"])
    assert push(monkeypatch, [ref_line("h" * 40)], check=1) == (1, ["check"])


def test_pre_push_refuses_what_it_cannot_check(records, monkeypatch):
    # Not HEAD, or a dirty tree: the checks would not test what is pushed.
    assert push(monkeypatch, [ref_line("x" * 40)]) == (1, [])
    assert push(monkeypatch, [ref_line("h" * 40)], clean=False) == (1, [])


def test_pre_push_ignores_deletions_and_tags(records, monkeypatch):
    lines = [ref_line("0" * 40), ref_line("x" * 40, ref="refs/tags/v1.0.0")]
    assert push(monkeypatch, lines) == (0, [])


@pytest.fixture
def asset_repo(tmp_path):
    """Real hook, scanner and git history; no git or check-pass mocks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "LOCAL_CI_PYTHON": sys.executable}

    def git(*args, check=True, input=None):
        return subprocess.run(["git", *args], cwd=repo, env=env, input=input,
                              text=True, capture_output=True, check=check)

    git("init", "-q")
    git("config", "user.name", "Local CI test")
    git("config", "user.email", "local-ci@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", ".githooks")
    for name in ("scripts/local_ci.py", "scripts/secret_scan.py", ".githooks/pre-push", ".gitleaks.toml"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    (repo / ".githooks/pre-push").chmod(0o755)
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

    def pre_push(local, remote="0" * 40, ref="refs/heads/pr-assets"):
        return subprocess.run(
            [sys.executable, "scripts/local_ci.py", "pre-push"], cwd=repo, env=env,
            input=f"refs/heads/assets {local} {ref} {remote}\n",
            text=True, capture_output=True,
        )

    return repo, git, commit, pre_push


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("ref", ["refs/heads/pr-assets", "refs/heads/pr-assets/2281"])
def test_pre_push_accepts_image_only_range(asset_repo, existing, ref):
    _, _, commit, pre_push = asset_repo
    base = commit("README.md", "PR screenshots")
    for extension in ("png", "jpg", "jpeg", "webp", "gif"):
        tip = commit(f"screenshots/example.{extension}")
    result = pre_push(tip, base if existing else "0" * 40, ref)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pr-assets image-only range accepted after secret scan" in result.stdout
    assert "Secret scan (offline provider rules): 0 finding(s)" in result.stdout
    assert "running local checks" not in result.stdout


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("name", ["code.py", "code.ts", "notes.md", "nested/README.md"])
def test_pre_push_refuses_non_image_anywhere_in_range(asset_repo, existing, name):
    _, git, commit, pre_push = asset_repo
    base = commit("README.md")
    commit(name)
    # A clean final image-only tree is insufficient: all pushed commits count.
    git("rm", "--", name)
    git("commit", "-qm", "Remove non-image")
    tip = commit("screenshot.png")
    result = pre_push(tip, base if existing else "0" * 40)
    assert result.returncode == 1
    assert "has no local check pass" in result.stderr
    assert "image-only range accepted" not in result.stdout


def test_pre_push_asset_paths_are_not_trimmed(asset_repo):
    _, _, commit, pre_push = asset_repo
    tip = commit(" README.md")
    assert pre_push(tip).returncode == 1


def test_pre_push_existing_assets_checks_only_outgoing_range(asset_repo):
    _, _, commit, pre_push = asset_repo
    base = commit("old.py")
    tip = commit("screenshot.png")
    assert pre_push(tip, base).returncode == 0
    # A new branch must inspect the root commit too, even if main knows it.
    _, git, _, _ = asset_repo
    git("update-ref", "refs/remotes/origin/main", base)
    result = pre_push(tip)
    assert result.returncode == 1
    assert "has no local check pass" in result.stderr


@pytest.mark.parametrize("divergent", [False, True])
def test_pre_push_assets_refuses_restored_code(asset_repo, divergent):
    _, git, commit, pre_push = asset_repo
    ancestor = commit("code.py")
    git("rm", "code.py")
    remote = commit("remote.png")
    local = ancestor
    if divergent:
        git("switch", "--detach", ancestor)
        local = commit("local.png")
    result = pre_push(local, remote)
    assert result.returncode == 1
    assert "has no local check pass" in result.stderr
    assert "image-only range accepted" not in result.stdout


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_pre_push_assets_refuses_nonregular_entries(asset_repo, existing, deleted, mode):
    _, git, commit, pre_push = asset_repo
    base = commit("README.md")
    # Build real index entries without requiring OS symlink privileges. Gitlinks
    # point at an actual commit; symlinks contain an ordinary target blob.
    oid = base if mode == "160000" else git("hash-object", "-w", "--stdin", input="README.md").stdout.strip()
    git("update-index", "--add", "--cacheinfo", f"{mode},{oid},evil.png")
    git("commit", "-qm", "Add nonregular asset")
    if deleted:
        git("update-index", "--force-remove", "evil.png")
        git("commit", "-qm", "Remove nonregular asset")
    tip = git("rev-parse", "HEAD").stdout.strip()
    result = pre_push(tip, base if existing else "0" * 40)
    assert result.returncode == 1
    assert "image-only range accepted" not in result.stdout


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("name", ["evil.PNG", "README.md/evil.png"])
def test_pre_push_assets_enforces_exact_filename_rules(asset_repo, existing, name):
    _, _, commit, pre_push = asset_repo
    base = commit("base.png")
    tip = commit(name)
    result = pre_push(tip, base if existing else "0" * 40)
    assert result.returncode == 1
    assert "has no local check pass" in result.stderr
    assert "image-only range accepted" not in result.stdout


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_pre_push_nonregular_assets_refused_after_successful_scan(asset_repo, existing, mode):
    _, git, commit, pre_push = asset_repo
    base = commit("README.md")
    oid = base if mode == "160000" else git("hash-object", "-w", "--stdin", input="README.md").stdout.strip()
    git("update-index", "--add", "--cacheinfo", f"{mode},{oid},evil.png")
    git("commit", "-qm", "Add nonregular asset")
    with_link = git("rev-parse", "HEAD").stdout.strip()
    if existing:
        # Deletions are not scanned as blobs, but their old modes still count.
        git("update-index", "--force-remove", "evil.png")
        git("commit", "-qm", "Delete nonregular asset")
    else:
        # The scanner excludes known main history on new refs. Asset validation
        # must still inspect it, including gitlinks the blob scanner cannot read.
        git("update-ref", "refs/remotes/origin/main", with_link)
    tip = git("rev-parse", "HEAD").stdout.strip()
    result = pre_push(tip, with_link if existing else "0" * 40)
    assert result.returncode == 1
    assert "Secret scan (offline provider rules): 0 finding(s)" in result.stdout
    assert "has no local check pass" in result.stderr
    assert "image-only range accepted" not in result.stdout


def test_pre_push_asset_scan_failure_still_refuses(asset_repo):
    _, _, commit, pre_push = asset_repo
    tip = commit("README.md", "LOCAL_CI_TEST_SECRET")
    result = pre_push(tip)
    assert result.returncode == 1
    assert "local-ci-test" in result.stdout
    assert "image-only range accepted" not in result.stdout


@pytest.mark.parametrize("ref", ["refs/heads/topic", "refs/heads/pr-assets-other"])
def test_pre_push_image_only_normal_ref_still_needs_checks(asset_repo, ref):
    _, _, commit, pre_push = asset_repo
    tip = commit("screenshot.png")
    result = pre_push(tip, ref=ref)
    assert result.returncode == 1
    assert "has no local check pass" in result.stderr
    assert "image-only range accepted" not in result.stdout


def test_image_only_push_through_installed_hook(asset_repo, tmp_path):
    _, git, commit, _ = asset_repo
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote))
    commit("README.md")
    for filename in ("first.png", "second.jpeg"):
        tip = commit(filename)
        result = git("push", str(remote), "HEAD:refs/heads/pr-assets", check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "image-only range accepted after secret scan" in result.stdout + result.stderr
        assert git("ls-remote", str(remote), "refs/heads/pr-assets").stdout.split()[0] == tip


def test_windows_known_failures_list_is_well_formed():
    sys.path.insert(0, str(ROOT / "scripts" / "ci"))
    import windows_known_failures as known  # noqa: E402  pyright: ignore[reportMissingImports]

    tests, files = known.load()
    assert tests or files
    for entry in tests | files:
        path = entry.split("::", 1)[0]
        assert "\\" not in entry and "[" not in entry, entry
        assert (ROOT / path).is_file(), f"{entry}: the file is gone; drop the line"
    assert known.base_id("fluid\\tests\\unit\\test_x.py::test_y[a-b]") == "fluid/tests/unit/test_x.py::test_y"

    # fluid's rootdir is fluid/, so its ids lack the prefix; the path restores it.
    class Item:
        path = ROOT / "fluid/tests/unit/test_x.py"
        nodeid = "tests/unit/test_x.py::TestA::test_y[1]"
    assert known.repo_id(Item()) == "fluid/tests/unit/test_x.py::TestA::test_y"


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
    monkeypatch.setattr(reach, "reader", lambda rev: lambda path:
                        b"export const version = 2;" if path == reach.LAUNCHER_ENTRY else None)
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


def test_cached_draft_pass_rechecks_reach_on_push(records, monkeypatch):
    local_ci.save_record("tree", {"security_gate": local_ci.SECURITY_GATE_VERSION})
    monkeypatch.setattr(local_ci, "git", lambda *a: "head")
    monkeypatch.setattr(local_ci, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(local_ci, "scan_push", lambda *a: None)
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
    monkeypatch.setattr(local_ci, "attest", lambda state, *a: posted.append(state))
    assert local_ci.main(["full", "--attest"]) == (0 if accepted else 1)
    assert posted == (["pending", "success"] if accepted else [])


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
