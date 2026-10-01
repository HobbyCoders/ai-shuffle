"""Real Git producer coverage for automatic coding-worker checkout isolation."""
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import checkout_manager, project_resolver, provider_agent, worker_checkout
from app.core.config import settings
from app.core.git_service import git_service
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import clean_task_registry, foreign_keys_off, pin_runner  # noqa: F401


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(mock_db, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    git(base, "init", "-b", "main")
    git(base, "config", "user.name", "Checkout Test")
    git(base, "config", "user.email", "checkout@example.test")
    (base / ".gitignore").write_text(".fluid/\n.env\nnode_modules/\napp/static/\n")
    (base / "code.txt").write_text("origin tip\n")
    git(base, "add", ".gitignore", "code.txt")
    git(base, "commit", "-m", "origin tip")
    old = git(base, "rev-parse", "HEAD")
    parent = tmp_path / "parent"
    git(base, "worktree", "add", "-b", "parent-branch", str(parent))
    (parent / "code.txt").write_text("unpublished parent commit\n")
    git(parent, "commit", "-am", "parent only")
    tip = git(parent, "rev-parse", "HEAD")
    git(base, "update-ref", "refs/remotes/origin/parent-branch", old)
    project = projects_v2.upsert_project("local:worker-checkout-test", name="Worker repo")
    runner = settings.effective_instance_id
    checkouts.upsert_checkout(project["id"], runner, str(base))
    checkouts.upsert_checkout(project["id"], runner, str(parent), branch="parent-branch")
    database.create_profile("coder", "Coder", None, {
        "model": "sonnet", "allowed_tools": ["Read", "Edit", "Bash"],
    })
    database.create_profile("research", "Research", None, {"allowed_tools": ["Read", "Grep", "Bash"]})
    database.create_session("parent", "coder", project_v2_id=project["id"])
    database.set_session_branch("parent", "parent-branch", project["id"])
    row = database.get_session("parent")
    return row, parent, base, tip


def test_checkout_producer_forks_actual_caller_tip_and_preserves_dirty_work(repo, monkeypatch):
    monkeypatch.setattr(git_service, "fetch", lambda *a, **kw: pytest.fail("worker provisioning must not fetch"))
    row, parent, base, tip = repo
    (parent / "code.txt").write_text("dirty user work\n")
    (parent / "user.txt").write_text("untracked user work\n")
    (parent / ".env").write_text("not a credential, a copy canary\n")
    (base / "node_modules").mkdir()
    (base / "node_modules" / "canary").write_text("shared dependency canary")
    before = git(parent, "status", "--porcelain")
    first = worker_checkout.prepare_worker_checkout(row, "worker-a", "coder")
    second = worker_checkout.prepare_worker_checkout(row, "worker-b", "coder")
    assert first.path != second.path != str(parent)
    assert first.branch != second.branch
    for checkout in (first, second):
        path = Path(checkout.path)
        assert checkout.base_commit == tip == git(path, "rev-parse", "HEAD")
        assert git(path, "branch", "--show-current") == checkout.branch
        assert (path / "code.txt").read_text() == "unpublished parent commit\n"
        assert not (path / ".env").exists()
        assert not (path / "node_modules").exists()
        assert not (path / "user.txt").exists()
        bound = {"project_v2_id": row["project_v2_id"], "branch": checkout.branch}
        assert project_resolver.resolve_existing_cwd(bound) == checkout.path
    assert git(parent, "status", "--porcelain") == before
    assert git(parent, "branch", "--show-current") == "parent-branch"
    assert (parent / "code.txt").read_text() == "dirty user work\n"


@pytest.mark.parametrize("tools", [["Read", "Bash"], ["mcp__builtins__Read", "Grep"]])
def test_explicit_read_only_file_workers_do_not_provision(repo, monkeypatch, tools):
    row, *_ = repo
    create = lambda *a, **kw: pytest.fail("research should not create a branch")
    monkeypatch.setattr(git_service, "create_branch", create)
    assert worker_checkout.prepare_worker_checkout(row, "reader", "coder", tools) is None
    assert worker_checkout.prepare_worker_checkout(row, "reader", "research") is None


@pytest.mark.parametrize("tools", [None, [], ["*"], ["mcp__builtins__Edit"], ["mcp__custom__mutate"]])
def test_unknown_or_write_capable_file_surfaces_are_isolated(repo, tools):
    row, *_ = repo
    assert worker_checkout.prepare_worker_checkout(row, "writer", "coder", tools)


def test_deny_mode_with_stale_allowlist_is_not_read_only(repo):
    row, *_ = repo
    database.create_profile("deny", "Deny", None,
                            {"tool_selection_mode": "deny", "allowed_tools": ["Read"]})
    assert worker_checkout.prepare_worker_checkout(row, "writer", "deny")


def test_wrong_parent_branch_refuses_without_repair(repo):
    row, parent, *_ = repo
    git(parent, "switch", "-c", "wrong-branch")
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="branch binding mismatches"):
        worker_checkout.prepare_worker_checkout(row, "writer", "coder")
    assert git(parent, "branch", "--show-current") == "wrong-branch"
    assert not git(parent, "branch", "--list", "task/writer")


def test_provisioning_failure_never_falls_back_and_retains_branch(repo, monkeypatch):
    row, parent, *_ = repo
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: None)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="refusing shared-checkout fallback"):
        worker_checkout.prepare_worker_checkout(row, "failed", "coder")
    assert git(parent, "branch", "--list", "task/failed")
    assert git(parent, "branch", "--show-current") == "parent-branch"


def test_duplicate_identity_does_not_reuse_an_existing_workers_checkout(repo):
    row, *_ = repo
    first = worker_checkout.prepare_worker_checkout(row, "same-worker", "coder")
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="refusing to reuse"):
        worker_checkout.prepare_worker_checkout(row, "same-worker", "coder")
    assert Path(first.path).is_dir()


def test_non_project_and_non_git_tasks_do_not_create_worktrees(repo, monkeypatch, tmp_path):
    assert worker_checkout.prepare_worker_checkout({}, "no-project", "coder") is None
    row, *_ = repo
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    assert worker_checkout.prepare_worker_checkout(row, "folder", "coder") is None


@pytest.fixture
def worker_runtime(monkeypatch):
    """Stub the provider only; Git provisioning and session binding stay real."""
    from app.core import turn_manager, turn_scheduler
    submitted = []

    async def submit(sid, source, **kwargs):
        submitted.append({"session_id": sid, "source": source, **kwargs})
        return "test-turn"

    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "wait_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    monkeypatch.setattr(provider_agent, "final_turn_outcome", lambda sid: {
        "terminal_found": True, "error": None, "interrupted": False, "usage": {},
    })
    monkeypatch.setattr(provider_agent, "final_assistant_text", lambda sid: "completed")
    return submitted


def dispatch(parent="parent", worker="spawn-a", **kwargs):
    return asyncio.run(provider_agent.dispatch_worker(
        parent_session_id="parent", context_session_id=parent,
        profile_id="coder", prompt="edit the parent's code", worker_session_id=worker,
        **kwargs,
    ))


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("provider", [None, "provider-row"])
def test_real_dispatch_binds_own_session_and_turn_cwd_before_launch(repo, worker_runtime, background, provider):
    row, parent, _, tip = repo
    outcome = dispatch(background=background, system_prompt_append="FILE ROLE",
                       provider_id=provider, model_id="sonnet" if provider is None else "model")
    assert outcome.ok, outcome.error
    worker = database.get_session(outcome.session_id)
    assert worker["branch"] == worker["worktree_branch"] == "task/spawn-a"
    assert worker["worktree_id"] is None
    assert worker["project_v2_id"] == row["project_v2_id"]
    cwd = project_resolver.resolve_existing_cwd(worker)
    assert cwd != str(parent)
    overrides = worker_runtime[0]["overrides"]
    assert overrides["cwd"] == cwd == worker["worktree_path"]
    assert "FILE ROLE" in overrides["system_prompt_append"]
    assert "Host-provisioned worker checkout" in overrides["system_prompt_append"]
    assert tip in overrides["system_prompt_append"]
    assert database.get_session("parent")["branch"] == "parent-branch"
    assert git(parent, "branch", "--show-current") == "parent-branch"


def test_nested_dispatch_forks_direct_callers_branch_not_routing_root(repo, worker_runtime):
    first = dispatch(worker="first")
    assert first.ok, first.error
    row = database.get_session("first")
    cwd = project_resolver.resolve_existing_cwd(row)
    (Path(cwd) / "nested.txt").write_text("first worker commit")
    git(cwd, "add", "nested.txt")
    git(cwd, "commit", "-m", "first worker change")
    nested_tip = git(cwd, "rev-parse", "HEAD")
    second = dispatch(parent="first", worker="nested")
    assert second.ok, second.error
    nested = database.get_session("nested")
    nested_cwd = project_resolver.resolve_existing_cwd(nested)
    assert nested_cwd != cwd
    assert git(nested_cwd, "rev-parse", "HEAD") == nested_tip
    assert (Path(nested_cwd) / "nested.txt").read_text() == "first worker commit"
    assert worker_runtime[1]["overrides"]["cwd"] == nested_cwd


def test_checkout_failure_prevents_session_creation_and_provider_submit(repo, worker_runtime, monkeypatch):
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: None)
    outcome = dispatch(worker="failed-spawn")
    assert not outcome.ok
    assert "refusing shared-checkout fallback" in outcome.error
    assert not worker_runtime
    assert database.get_session("failed-spawn") is None


@pytest.mark.parametrize("use_swarm", [False, True])
def test_task_and_swarm_producing_paths_auto_provision_without_model_instructions(
    repo, worker_runtime, monkeypatch, use_swarm,
):
    from app.core import swarm, task_dispatch
    from fluid.tools.registry import ToolCtx

    row, parent, _, tip = repo
    database.create_profile("coordinator", "Coordinator", None, {"model": "sonnet"})
    database.create_session("workflow-parent", "coordinator", project_v2_id=row["project_v2_id"])
    database.set_session_branch("workflow-parent", "parent-branch", row["project_v2_id"])
    monkeypatch.setattr(task_dispatch, "_credential_gap", lambda pid: None)
    dispatcher = task_dispatch.make_fluid_dispatcher("workflow-parent")
    ctx = ToolCtx(extra={"task_dispatch": dispatcher})

    async def exercise():
        if not use_swarm:
            outcome = await dispatcher({"agent": "coder", "prompt": "edit code", "mode": "sync"}, ctx)
            assert not outcome.is_error, outcome.text
            return
        try:
            outcome = await swarm.make_handler(lambda: "workflow-parent")({
                "action": "start", "tasks": [
                    {"id": "a", "agent": "coder", "prompt": "edit code"},
                    {"id": "b", "agent": "coder", "prompt": "edit other code"},
                ],
            }, ctx)
            assert not outcome.is_error, outcome.text
            coordinator = swarm.coordinators["workflow-parent"]
            await asyncio.wait_for(coordinator.jobs[outcome.background_task.task_id], 15)
            snapshot = coordinator.snapshot(outcome.background_task.task_id)
            assert snapshot["status"] == "completed", snapshot
        finally:
            await swarm.close("workflow-parent")

    asyncio.run(exercise())
    assert len(worker_runtime) == (2 if use_swarm else 1)
    paths = set()
    for launch in worker_runtime:
        worker = database.get_session(launch["session_id"])
        cwd = project_resolver.resolve_existing_cwd(worker)
        paths.add(cwd)
        assert cwd != str(parent)
        assert git(cwd, "rev-parse", "HEAD") == tip
        assert worker["branch"].startswith("task/")
        assert launch["overrides"]["cwd"] == cwd
    assert len(paths) == len(worker_runtime)
    assert git(parent, "branch", "--show-current") == "parent-branch"
