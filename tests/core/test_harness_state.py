"""#2042: real state/report producers and parent Read, without a provider call."""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fluid.codemode import HostBridge, Projection, SubprocessKernel
from fluid.engine.native import NativeLoop
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.rules import Rule
from fluid.state import safe_key, scratch_dir
from fluid.store.base import MemoryStore
from fluid.tools.builtins import ledger, note, read
from fluid.tools.registry import ToolCtx, ToolRegistry

from app.core import harness_state, task_dispatch
from app.core.config import settings
from app.core.provider_agent import WorkerOutcome, _persist_worker_report
from app.db import database


@pytest.fixture
def state_fixture(tmp_path, monkeypatch, mock_db):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setattr(settings, "fluid_projects_dir", str(home / ".fluid/projects"))
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    database.create_project("project", "fixture", None, str(repo))
    database.create_profile("parent", "Parent", None, {"model": "fixture"})
    database.create_profile("helper", "Helper", None, {"model": "fixture"})
    database.create_session(session_id="session", profile_id="parent", project_id="project", title="fixture")
    return repo


def _ctx(repo, sid="session"):
    return harness_state.bind_state_context(ToolCtx(cwd=str(repo), session_id=sid), lambda: sid)


@pytest.mark.parametrize("mode", ["sync", "background"])
async def test_fresh_repo_real_producers_receipt_and_gated_parent_read(state_fixture, monkeypatch, mode):
    repo = state_fixture
    ctx = _ctx(repo)
    registry = ToolRegistry()
    for module in (ledger, note, read):
        registry.register_builtin(module.SPEC, module.handler)
    permissions = PermissionEngine("on_write")
    loop = NativeLoop(provider=None, store=MemoryStore(), model="fixture", ctx=ctx,
                      registry=registry, permissions=permissions)
    for name, args in [("Ledger", {"action": "add", "section": "state", "text": "pinned"}),
                       ("Note", {"action": "add", "text": "plan"})]:
        outcome = await loop._gated_call("session", "turn", name, name, args)
        assert not outcome.is_error, outcome.text
    body = "line one\nline two\nline three"
    if mode == "sync":
        # Only execution is stubbed: the real dispatch produces the report and
        # bounded receipt using the parent's actual isolated DB record.
        from app.core import project_resolver
        monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(repo))
        monkeypatch.setattr("app.core.provider_agent.dispatch_worker", AsyncMock(
            return_value=WorkerOutcome(ok=True, session_id="worker-sync", report=body)))
        receipt = await task_dispatch.dispatch_task_tool(
            {"agent": "helper", "prompt": "fixture", "mode": "sync"}, caller_session_id="session")
        assert receipt["ok"], receipt
        assert receipt["report_head"] == body and receipt["report_chars"] == len(body)
        path = receipt["report_path"]
    else:
        # The background completion producer shares the writer. The existing
        # provider_agent suite also drives its registry/completion-frame path.
        path, chars = _persist_worker_report(database.get_session("session"), "worker-background", body)
        assert chars == len(body)
    target = Path(path)
    assert target.is_absolute() and not target.is_relative_to(repo)
    global_root = harness_state.session_state_root("session", str(repo))
    assert target.parent == global_root / "task-reports"
    assert "pinned" in (global_root / "ledger/session.md").read_text()
    assert "plan" in (global_root / "notes/session.md").read_text()
    out = await loop._gated_call("session", "turn", "Read", "read", {"file_path": path, "offset": 2, "limit": 1})
    assert not out.is_error and "line two" in out.text and "line one" not in out.text
    assert str(target) in ctx.read_files

    # Exercise the real projected Read behind the host gate as well, not a
    # direct file read masquerading as parent Read verification.
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        bridge = HostBridge(registry, permissions, ctx=ctx)
        result = await kernel.exec(
            f"from tools import Read\nprint(Read(file_path={path!r}, offset=3, limit=1))",
            timeout=8, on_host_call=bridge.on_host_call)
        assert result.error is None, result.error
        assert "line three" in result.stdout
        permissions.rules.append(Rule(tool="Read", decision="deny", input_globs={"file_path": path}))
        denied = await kernel.exec(f"Read(file_path={path!r})", timeout=8, on_host_call=bridge.on_host_call)
        assert denied.error is not None and "denied" in denied.error.lower()
    finally:
        await kernel.stop()
    blocked = await loop._gated_call("session", "turn", "Read", "blocked", {"file_path": path})
    assert blocked.is_error and "permission denied" in blocked.text
    assert len(permissions.rules) == 1  # no allow added for reports or a global tree
    assert not (repo / ".fluid").exists()
    assert await asyncio.to_thread(subprocess.check_output, ["git", "status", "--porcelain"], cwd=repo) == b""
    restarted = NativeLoop(provider=None, store=MemoryStore(), model="fixture", ctx=_ctx(repo))
    assert "pinned" in restarted._ledger_reminder()
    assert "plan" in restarted._notes_reminder()


async def test_canonical_base_checkout_across_branches_and_restart(state_fixture, tmp_path):
    from app.db import checkouts, projects_v2
    repo = state_fixture
    project = projects_v2.upsert_project("local:fixture", name="fixture")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(repo))
    branch = tmp_path / "arbitrary-branch-checkout"
    branch.mkdir()
    database.create_session(session_id="branch-session", profile_id="parent", project_v2_id=project["id"], title="branch")
    ctx = _ctx(branch, "branch-session")
    await ledger.handler({"action": "add", "section": "decisions", "text": "survives"}, ctx)
    assert harness_state.session_state_root("branch-session", str(branch)) == harness_state.project_state_root(cwd=str(repo))
    assert "survives" in ledger.read_ledger(_ctx(repo, "branch-session"))
    other = tmp_path / "other"
    other.mkdir()
    database.create_project("other", "Other", None, str(other))
    database.create_session(session_id="other-session", profile_id="parent", project_id="other", title="other")
    assert harness_state.session_state_root("other-session", str(other)) != harness_state.session_state_root("session", str(repo))
    assert note.read_notes(_ctx(other, "other-session")) == ""
    assert not (branch / ".fluid").exists()


async def test_configured_data_dir_projects_root_is_honored(state_fixture, monkeypatch):
    repo = state_fixture
    configured = settings.effective_data_dir / "fluid"
    monkeypatch.setattr(settings, "fluid_projects_dir", str(configured))
    ctx = _ctx(repo)
    root = harness_state.session_state_root("session", str(repo))
    assert root.parent == configured
    assert harness_state.project_state_root(cwd=str(repo)) == root
    assert not (await ledger.handler({"action": "add", "section": "state", "text": "in data"}, ctx)).is_error
    report = task_dispatch.write_task_report(database.get_session("session"), "worker-data", "report")
    assert "in data" in (root / "ledger/session.md").read_text()
    assert Path(report).parent == root / "task-reports"
    assert not (repo / ".fluid").exists()


@pytest.mark.parametrize("sid", ["../escape", "/absolute", r"..\escape"])
def test_report_worker_ids_cannot_traverse(state_fixture, sid):
    row = database.get_session("session")
    path = Path(task_dispatch.write_task_report(row, sid, "report"))
    assert path.parent == harness_state.project_state_root(row, str(state_fixture)) / "task-reports"
    assert path.name == safe_key(sid) + ".md"


def test_scratch_prompt_is_absolute_session_scoped_and_survives_compaction(state_fixture, monkeypatch):
    from app.core.provider.harness_adapter import HarnessAdapter
    adapter = HarnessAdapter()
    adapter._options = SimpleNamespace(cwd=str(state_fixture))
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "session")
    for name in ("_env_note", "_memory_snapshot", "_away_note"):
        monkeypatch.setattr(adapter, name, lambda: None)
    designated = scratch_dir(_ctx(state_fixture))
    assert str(designated) in adapter._turn_context_note()
    assert str(designated) in adapter._compact_note()
    assert not designated.exists()
    assert not (state_fixture / ".fluid").exists()
