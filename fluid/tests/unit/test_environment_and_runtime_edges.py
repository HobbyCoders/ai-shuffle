"""Environment/runtime-fact gathering edges: git state rendering without a
real repository, degraded subprocesses, receipt failures and fact harvest."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from fluid.context import environment as env
from fluid.context import runtime_facts as runtime


def _fake_git(monkeypatch, answers: dict[tuple[str, ...], str | None]) -> None:
    monkeypatch.setattr(env, "_run_git", lambda cwd, *args: answers.get(args))


_TREE = ("rev-parse", "--is-inside-work-tree")
_STATUS = ("status", "--porcelain=2", "--branch")
_LOG = ("log", "--oneline", "-5")


def test_git_context_upstream_ahead_behind_and_counts(monkeypatch) -> None:
    _fake_git(monkeypatch, {
        _TREE: "true",
        _STATUS: "\n".join([
            "# branch.oid abc",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "1 M. N... 100644 100644 100644 a b f1",
            "1 .M N... 100644 100644 100644 a b f2",
            "2 R. N... 100644 100644 100644 a b R100 new\told",
            "u UU N... 100644 100644 100644 100644 a b c f3",
            "? f4",
        ]),
        _LOG: "abc first",
    })
    ctx = env.git_context("/repo")
    assert "Current branch: main → origin/main (ahead 2, behind 1)" in ctx
    assert "Working tree: 2 staged, 1 modified, 1 untracked, 1 conflicted" in ctx
    assert ctx.endswith("Recent commits:\nabc first")


def test_git_context_in_sync_upstream_has_no_ahead_behind(monkeypatch) -> None:
    _fake_git(monkeypatch, {
        _TREE: "true",
        _STATUS: "# branch.head dev\n# branch.upstream origin/dev\n# branch.ab +0 -0",
    })
    ctx = env.git_context("/repo")
    assert "Current branch: dev → origin/dev\n" in ctx + "\n"
    assert "ahead" not in ctx and "Working tree: clean" in ctx


def test_git_context_detached_and_fallback_branch(monkeypatch) -> None:
    _fake_git(monkeypatch, {_TREE: "true", _STATUS: "# branch.head (detached)"})
    assert "Current branch: (detached HEAD)" in env.git_context("/repo")

    # Status failed entirely: the branch comes from rev-parse, no tree line.
    _fake_git(monkeypatch, {_TREE: "true", ("rev-parse", "--abbrev-ref", "HEAD"): "topic"})
    assert env.git_context("/repo") == "Current branch: topic"

    # Nothing gatherable at all inside a work tree → no section.
    _fake_git(monkeypatch, {_TREE: "true"})
    assert env.git_context("/repo") is None


def test_run_git_degrades_on_missing_binary_timeout_and_failure(monkeypatch, tmp_path) -> None:
    def missing(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(env.subprocess, "run", missing)
    assert env._run_git(str(tmp_path), "status") is None

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 3)

    monkeypatch.setattr(env.subprocess, "run", slow)
    assert env._run_git(str(tmp_path), "status") is None

    monkeypatch.setattr(
        env.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 128, stdout="", stderr="no"),
    )
    assert env._run_git(str(tmp_path), "status") is None
    assert env.is_git_work_tree(str(tmp_path)) is False


def test_os_version_darwin_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(env.sys, "platform", "darwin")
    monkeypatch.setattr(env._platform, "mac_ver", lambda: ("14.5", ("", "", ""), "arm64"))
    assert env._os_version() == "macOS 14.5"

    def broken():
        raise OSError("no uname")

    monkeypatch.setattr(env._platform, "mac_ver", broken)
    monkeypatch.setattr(env._platform, "system", lambda: "Darwin")
    assert env._os_version() == "Darwin"


def test_project_facts_tolerates_bad_package_json_and_reads_makefile(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("[1, 2]")  # valid JSON, not an object
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "Makefile").write_text("test:\n\techo\nlint :\n\techo\n")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
    facts = env.project_facts(str(tmp_path))
    assert "Project manifests: package.json, Makefile (uv/yarn)" in facts
    assert "scripts/run_tests.sh; uv run pytest; make test; make lint" in facts


def test_read_small_skips_oversize_and_unreadable(tmp_path: Path, monkeypatch) -> None:
    big = tmp_path / "big"
    big.write_bytes(b"x" * (env._MAX_FACT_FILE_BYTES + 1))
    assert env._read_small(big) == ""

    target = tmp_path / "t"
    target.write_text("ok")

    def denied(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", denied)
    assert env._read_small(target) == ""


# ─── runtime facts ───────────────────────────────────────────────────────────


def test_receipt_path_degrades_when_git_is_unavailable(tmp_path, monkeypatch) -> None:
    def missing(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(runtime.subprocess, "run", missing)
    assert runtime._receipt_path(tmp_path) is None
    runtime.record_provisioning(str(tmp_path), {"linked": [1]})  # never raises
    assert runtime._provisioning_fact(tmp_path) is None


def test_record_provisioning_cleans_temp_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(runtime, "_receipt_path", lambda root: receipt)

    def fail_replace(src, dst):
        raise OSError("read-only")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    runtime.record_provisioning(str(tmp_path), {"linked": ["a", "b"]})
    assert not receipt.exists()
    assert list(tmp_path.glob(".fluid-runtime-*.tmp")) == []


@pytest.mark.parametrize("payload", [
    {"version": 2, "counts": {k: 0 for k in runtime._KEYS}},
    {"version": 1, "counts": {**{k: 0 for k in runtime._KEYS}, "linked": 10_001}},
    {"version": 1, "counts": {**{k: 0 for k in runtime._KEYS}, "linked": True}},
    {"version": 1},
    ["not", "an", "object"],
])
def test_provisioning_fact_rejects_malformed_receipts(tmp_path, monkeypatch, payload) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(payload))
    monkeypatch.setattr(runtime, "_receipt_path", lambda root: receipt)
    assert runtime._provisioning_fact(tmp_path) is None


def test_runtime_facts_node_modules_states_and_frontend_assets(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_provisioning_fact", lambda root: None)
    for i in range(10):
        pkg = tmp_path / f"pkg{i}"
        pkg.mkdir()
        (pkg / "package.json").write_text("{}")
    (tmp_path / "pkg0" / "node_modules").mkdir()
    (tmp_path / "pkg1" / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    note = runtime.runtime_facts(str(tmp_path))
    assert 'Node dependencies "pkg0": EMPTY' in note
    assert 'Node dependencies "pkg1": present (not validated)' in note
    assert "Additional package directories omitted" in note
    assert "Built frontend app/static: MISSING" in note

    (tmp_path / "app" / "static").mkdir()
    assert "INCOMPLETE; index.html missing" in runtime.runtime_facts(str(tmp_path))
    (tmp_path / "app" / "static" / "index.html").write_text("<html>")
    assert "present (build identity unverified)" in runtime.runtime_facts(str(tmp_path))


def test_runtime_facts_survives_find_spec_errors_and_unlistable_root(tmp_path, monkeypatch) -> None:
    def bad_spec(name):
        raise ValueError("broken meta path")

    monkeypatch.setattr(runtime.importlib.util, "find_spec", bad_spec)
    monkeypatch.setattr(runtime, "_provisioning_fact", lambda root: None)
    missing = tmp_path / "nope"
    note = runtime.runtime_facts(str(missing))
    assert "pytest in worker Python: not found" in note
    assert "Dependency presence could not be inspected." in note
    facts = dict(runtime.harvest_environment_facts(str(missing)))
    assert "not importable" in facts["env.pytest"]


def test_harvest_environment_facts_reports_empty_node_modules(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: object())
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "node_modules").mkdir()
    facts = dict(runtime.harvest_environment_facts(str(tmp_path)))
    assert "is importable" in facts["env.pytest"]
    assert "missing in ." in facts["env.node_modules"]


def test_harvested_node_modules_fact_is_the_same_in_every_worktree(tmp_path, monkeypatch) -> None:
    # #1814: the fact named only the packages missing in the session's own
    # worktree, so each session in another worktree rewrote it (revision 74).
    from fluid.codemode.skills import SkillStore

    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: object())
    fresh, older = tmp_path / "fresh", tmp_path / "older"
    for checkout in (fresh, older):
        for package in ("desktop", "frontend"):
            (checkout / package).mkdir(parents=True)
            (checkout / package / "package.json").write_text("{}")
    (older / "frontend" / "node_modules" / "left-pad").mkdir(parents=True)
    texts = [dict(runtime.harvest_environment_facts(str(c)))["env.node_modules"] for c in (fresh, older)]
    assert texts[0] == texts[1] and "desktop, frontend" in texts[0]

    store = SkillStore(tmp_path / "store")
    for checkout in (fresh, older, fresh):
        runtime.write_harvested_facts(store, "project:x", runtime.harvest_environment_facts(str(checkout)))
    fact = next(m for m in store.memories() if m.key == "env.node_modules")
    assert fact.revision == 1


def test_harvest_skips_node_modules_fact_when_every_package_is_installed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: object())
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    assert "env.node_modules" not in dict(runtime.harvest_environment_facts(str(tmp_path)))


@pytest.mark.parametrize("origin, expected", [
    ("http://10.0.0.5:8080", "http://10.0.0.5:<port>"),
    ("http://10.0.0.5", "http://10.0.0.5:<port>"),  # portless: was "http:<port>"
    ("https://[fd00::5]:3000", "https://[fd00::5]:<port>"),
])
def test_browser_host_fact_keeps_scheme_and_host(origin, expected) -> None:
    facts = runtime.harvest_tool_facts(
        "mcp__browser__browser_navigate", f"Browse {origin} instead of localhost."
    )
    assert facts and f"browsed via {expected} " in facts[0][1]


def test_browser_host_fact_ignores_loopback_and_other_tools() -> None:
    assert runtime.harvest_tool_facts("browser_navigate", "Browse http://[::1]:80 instead") == []
    assert runtime.harvest_tool_facts("browser_navigate", "Browse http://localhost instead") == []
    assert runtime.harvest_tool_facts("Bash", "Browse http://10.0.0.5 instead") == []
    assert runtime.harvest_tool_facts("browser_navigate", "no redirect here") == []


def test_write_harvested_facts_skips_refused_saves() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls = 0

        def save_memory(self, text, **kw):
            self.calls += 1
            if kw["key"] == "env.pytest":
                raise ValueError("protected")
            return type("Saved", (), {"id": "m1"})()

    store = Store()
    assert runtime.write_harvested_facts(None, None, [("k", "t")]) == []
    written = runtime.write_harvested_facts(
        store, "proj", [("env.pytest", "a"), ("env/node modules", "b")]
    )
    assert written == ["m1"] and store.calls == 2
