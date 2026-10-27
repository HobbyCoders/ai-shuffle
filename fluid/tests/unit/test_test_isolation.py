"""Regression coverage for the Fluid test suite's filesystem isolation."""

import os
from pathlib import Path

import pytest

# Import during collection, before isolate_home runs: HOME alone cannot fix
# these cached paths, including the CLI modules' imported copies.
from fluid.cli import evolve, replay
from fluid.codemode import skills
from tests.conftest import guard_skills_writes


@pytest.mark.parametrize("module", [skills, evolve, replay])
def test_cached_skills_roots_follow_isolated_home(isolate_home, module):
    assert module.DEFAULT_SKILLS_ROOT == isolate_home / ".fluid" / "skills"
    assert Path.home() == isolate_home


@pytest.mark.parametrize("iteration", range(2))
def test_default_store_is_fresh_for_each_test(isolate_home, iteration):
    store = skills.SkillStore()
    assert store.root == isolate_home / ".fluid" / "skills"
    assert not store.memories_path.exists()
    store.save_memory(f"A test-local fact {iteration}", provenance="tool")
    assert store.memories_path.is_file()


@pytest.mark.parametrize("operation", [
    "write", "append", "os_open", "mkdir", "replace_source", "replace_target",
    "unlink", "rmdir", "touch",
])
def test_write_guard_blocks_mutations_before_they_happen(tmp_path, operation):
    # A synthetic developer store proves the guard without risking the real
    # one, even if the guard itself regresses.
    root = tmp_path / "developer-home" / ".fluid" / "skills"
    root.mkdir(parents=True)
    memory = root / "memories.json"
    memory.write_text("original", encoding="utf-8")
    stamp = memory.stat().st_mtime_ns
    source = tmp_path / "replacement"
    source.write_text("replacement", encoding="utf-8")

    with guard_skills_writes(root):
        with pytest.raises(pytest.fail.Exception, match="real Fluid skills store"):
            if operation == "write":
                memory.write_text("junk", encoding="utf-8")
            elif operation == "append":
                with memory.open("a", encoding="utf-8") as stream:
                    stream.write("junk")
            elif operation == "os_open":
                fd = os.open(memory, os.O_WRONLY | os.O_TRUNC)
                os.close(fd)
            elif operation == "mkdir":
                (root / "new-skill").mkdir()
            elif operation == "replace_source":
                memory.replace(tmp_path / "moved")
            elif operation == "replace_target":
                source.replace(memory)
            elif operation == "unlink":
                memory.unlink()
            elif operation == "rmdir":
                root.rmdir()
            elif operation == "touch":
                memory.touch()
        assert memory.read_text(encoding="utf-8") == "original"  # reads allowed
        assert memory.stat().st_mtime_ns == stamp
        # A path sharing only the prefix is not inside the protected store.
        sibling = root.with_name("skills-backup")
        sibling.mkdir()
        (sibling / "allowed").write_text("allowed", encoding="utf-8")

    # The hook is inert once its context ends.
    memory.write_text("allowed after guard", encoding="utf-8")


def test_write_guard_is_not_swallowed_by_best_effort_saves(tmp_path):
    root = tmp_path / "protected-skills"
    root.mkdir()
    with guard_skills_writes(root):
        with pytest.raises(pytest.fail.Exception, match="real Fluid skills store"):
            try:
                (root / "memories.json").write_text("junk", encoding="utf-8")
            except Exception:
                pass
    assert not (root / "memories.json").exists()


def test_write_guard_tolerates_mocked_platform(tmp_path, monkeypatch):
    root = tmp_path / "protected-skills"
    root.mkdir()
    allowed = tmp_path / "allowed"
    with guard_skills_writes(root):
        # Shell-resolution tests simulate Windows on POSIX (and vice versa).
        # Restore the platform before pytest formats any failure traceback.
        with monkeypatch.context() as mp:
            mp.setattr(os, "name", "nt" if os.name == "posix" else "posix")
            allowed.write_text("allowed", encoding="utf-8")
            with pytest.raises(pytest.fail.Exception, match="real Fluid skills store"):
                (root / "memories.json").write_text("junk", encoding="utf-8")
    assert allowed.read_text(encoding="utf-8") == "allowed"
    assert not (root / "memories.json").exists()
