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
    monkeypatch.setattr(local_ci, "tree_of", lambda sha: (trees or {}).get(sha, "t-" + sha))
    ran = []
    monkeypatch.setattr(local_ci, "cmd_check", lambda args: ran.append(args.command) or check)
    result = local_ci.cmd_pre_push(local_ci.argparse.Namespace())
    return result, ran


def ref_line(sha, ref="refs/heads/topic"):
    return f"refs/heads/topic {sha} {ref} {'0' * 40}"


def test_pre_push_trusts_a_recorded_tree(records, monkeypatch):
    local_ci.save_record("t-" + "h" * 40, {"at": 1})
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


def test_hook_finds_the_script():
    hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
    assert hook.startswith("#!/bin/sh")
    assert 'scripts/local_ci.py" pre-push' in hook
