"""scripts/local_ci.py: suite selection, pass records and the pre-push decision."""
import io
from pathlib import Path
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
    # Unraid: python3 on PATH is the app venv (no pytest); /usr/local/bin/python3 has it.
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
    monkeypatch.setattr(local_ci.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "security_check", lambda *a, **kw: None)
    monkeypatch.setattr(local_ci, "run_suites", lambda *a: ([], [], []))
    posted = []
    monkeypatch.setattr(local_ci, "attest", lambda state, *a: posted.append(state))
    assert local_ci.main(["full", "--attest"]) == 1
    assert posted == ["pending", "failure"]
