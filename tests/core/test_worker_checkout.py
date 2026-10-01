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
    database.create_profile("research", "Research", None, {"allowed_tools": ["Read", "Grep", "Glob"]})
    database.create_profile("shell-research", "Shell research", None, {"allowed_tools": ["Read", "Grep", "Bash"]})
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


@pytest.mark.parametrize("tools", [["Read", "Grep"], ["mcp__builtins__Read", "Grep", "Glob"]])
def test_explicit_read_only_file_workers_do_not_provision(repo, monkeypatch, tools):
    row, *_ = repo
    create = lambda *a, **kw: pytest.fail("research should not create a branch")
    monkeypatch.setattr(git_service, "create_branch", create)
    assert worker_checkout.prepare_worker_checkout(row, "reader", "coder", tools) is None
    assert worker_checkout.prepare_worker_checkout(row, "reader", "research") is None


def test_read_only_worker_shares_even_without_a_caller_row(repo, monkeypatch):
    # A transient caller read failure must not refuse a worker that would
    # never have been isolated in the first place.
    monkeypatch.setattr(git_service, "create_branch",
                        lambda *a, **kw: pytest.fail("no branch for a read-only worker"))
    assert worker_checkout.prepare_worker_checkout(None, "reader", "research") is None
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="calling session is unavailable"):
        worker_checkout.prepare_worker_checkout(None, "writer", "coder")


@pytest.mark.parametrize("tools", [["Read", "Bash"], ["Read", "Grep", "Monitor"],
                                   ["mcp__builtins__Read", "mcp__builtins__Bash"]])
def test_shell_capable_surfaces_are_isolated(repo, tools):
    # A shell can `git checkout`, `sed -i` or `npm install` in the caller's
    # tree as surely as Edit can, so it never shares the parent checkout.
    row, parent, *_ = repo
    checkout = worker_checkout.prepare_worker_checkout(row, "sheller", "coder", tools)
    assert checkout and checkout.path != str(parent)
    assert worker_checkout.prepare_worker_checkout(row, "explorer", "shell-research")


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


def test_provisioning_failure_never_falls_back_and_reclaims_its_empty_branch(repo, monkeypatch):
    row, parent, *_ = repo
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: None)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="refusing shared-checkout fallback"):
        worker_checkout.prepare_worker_checkout(row, "failed", "coder")
    # The branch never held a commit of its own, so nothing is lost by
    # removing it — and a retry is not left a stray ref to trip over.
    assert not git(parent, "branch", "--list", "task/failed")
    assert git(parent, "branch", "--show-current") == "parent-branch"


def test_post_provision_refusal_reclaims_the_unused_checkout(repo, monkeypatch):
    row, parent, *_ = repo
    real = checkout_manager._require_branch_binding
    calls = []

    def flaky(path, branch):
        calls.append(path)
        if path == str(parent) and len(calls) > 1:
            raise checkout_manager.WorktreeError("parent switched mid-provisioning")
        return real(path, branch)

    monkeypatch.setattr(checkout_manager, "_require_branch_binding", flaky)
    with pytest.raises(checkout_manager.WorktreeError):
        worker_checkout.prepare_worker_checkout(row, "raced", "coder")
    assert not git(parent, "branch", "--list", "task/raced")
    assert "task/raced" not in git(parent, "worktree", "list")


def test_unbound_caller_reports_its_actual_branch_as_integration_target(repo):
    row, parent, base, _ = repo
    database.create_session("base-caller", "coder", project_v2_id=row["project_v2_id"])
    caller = database.get_session("base-caller")
    assert not caller.get("branch")
    checkout = worker_checkout.prepare_worker_checkout(caller, "from-base", "coder")
    assert checkout.parent_branch == "main"
    assert checkout.base_commit == git(base, "rev-parse", "HEAD")
    assert '"integration_target": "main"' in checkout.instructions()


def test_parallel_provisioning_in_one_repo_all_succeed(repo):
    from concurrent.futures import ThreadPoolExecutor

    row, *_ = repo
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: worker_checkout.prepare_worker_checkout(row, f"swarm-{i}", "coder"),
            range(8),
        ))
    assert len({c.path for c in results}) == 8
    assert all(Path(c.path).is_dir() for c in results)


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


def test_unmaterialized_non_git_project_keeps_legacy_cwd_behavior(mock_db, tmp_path):
    # No checkout here and no branch binding: nothing to protect and no Git
    # to fork from, so the worker resolves its own cwd as it always did.
    project = projects_v2.upsert_project("local:plain-folder", name="Plain")
    database.create_profile("plain-coder", "Coder", None, {"allowed_tools": ["Read", "Edit"]})
    database.create_session("plain-parent", "plain-coder", project_v2_id=project["id"])
    row = database.get_session("plain-parent")
    assert worker_checkout.prepare_worker_checkout(row, "plain", "plain-coder") is None
    folder = tmp_path / "plain"
    folder.mkdir()
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(folder))
    assert worker_checkout.prepare_worker_checkout(row, "plain-2", "plain-coder") is None


def test_bound_caller_without_a_local_checkout_still_refuses(repo, monkeypatch):
    row, *_ = repo
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: None)
    monkeypatch.setattr(project_resolver, "resolve_local_checkout", lambda row: None)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="unavailable"):
        worker_checkout.prepare_worker_checkout(row, "orphan", "coder")


@pytest.fixture
def worker_runtime(monkeypatch):
    """Stub the provider only; Git provisioning and session binding stay real.

    Each launch records what its checkout looked like when the turn was
    submitted, since an untouched one is reclaimed once the worker finishes.
    ``work`` maps a worker id to a callable that edits its cwd mid-turn.
    """
    from app.core import turn_manager, turn_scheduler
    class Launches(list):
        work: dict

    submitted = Launches()
    work = {}

    async def submit(sid, source, **kwargs):
        cwd = (kwargs.get("overrides") or {}).get("cwd")
        launch = {"session_id": sid, "source": source, **kwargs}
        if cwd and Path(cwd).is_dir():
            launch["head"] = git(cwd, "rev-parse", "HEAD")
            launch["branch"] = git(cwd, "branch", "--show-current")
        submitted.append(launch)
        if sid in work:
            work[sid](Path(cwd))
        return "test-turn"

    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "wait_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    monkeypatch.setattr(provider_agent, "final_turn_outcome", lambda sid: {
        "terminal_found": True, "error": None, "interrupted": False, "usage": {},
    })
    monkeypatch.setattr(provider_agent, "final_assistant_text", lambda sid: "completed")
    submitted.work = work
    return submitted


def dispatch(parent="parent", worker="spawn-a", **kwargs):
    return asyncio.run(provider_agent.dispatch_worker(
        parent_session_id="parent", context_session_id=parent,
        profile_id="coder", prompt="edit the parent's code", worker_session_id=worker,
        **kwargs,
    ))


def commit_in(path, name="worker.txt", text="worker change"):
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-m", f"add {name}")


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
    launch = worker_runtime[0]
    overrides = launch["overrides"]
    assert overrides["cwd"] == worker["worktree_path"] != str(parent)
    assert launch["branch"] == "task/spawn-a" and launch["head"] == tip
    assert "FILE ROLE" in overrides["system_prompt_append"]
    assert "Host-provisioned worker checkout" in overrides["system_prompt_append"]
    assert tip in overrides["system_prompt_append"]
    assert database.get_session("parent")["branch"] == "parent-branch"
    assert git(parent, "branch", "--show-current") == "parent-branch"
    # The worker finished without touching its checkout: reclaimed, with the
    # tip recorded so a resume can bring it back.
    assert not Path(worker["worktree_path"]).exists()
    assert not git(parent, "branch", "--list", "task/spawn-a")
    record = checkouts.get_checkout(row["project_v2_id"], settings.effective_instance_id,
                                    branch="task/spawn-a")
    assert record["status"] == "archived" and record["head_sha"] == tip


@pytest.mark.parametrize("change", ["commit", "untracked"])
def test_worker_checkout_with_work_is_retained_for_review(repo, worker_runtime, change):
    row, parent, *_ = repo
    worker_runtime.work["keeper"] = (
        commit_in if change == "commit"
        else lambda path: (path / "notes.txt").write_text("uncommitted findings")
    )
    outcome = dispatch(worker="keeper")
    assert outcome.ok, outcome.error
    worker = database.get_session("keeper")
    assert Path(worker["worktree_path"]).is_dir()
    assert git(parent, "branch", "--list", "task/keeper")
    assert project_resolver.resolve_existing_cwd(worker) == worker["worktree_path"]


def test_reclaimed_checkout_is_restored_for_a_resumed_worker(repo, worker_runtime, monkeypatch):
    row, parent, _, tip = repo
    outcome = dispatch(worker="resumable")
    assert outcome.ok, outcome.error
    path = database.get_session("resumable")["worktree_path"]
    assert not Path(path).exists()
    monkeypatch.setattr(git_service, "fetch", lambda *a, **kw: pytest.fail("restore must not fetch"))
    from app.core import worktree_manager
    monkeypatch.setattr(worktree_manager, "fetch_repo_cached",
                        lambda *a, **kw: pytest.fail("restore must not fetch"))
    assert worker_checkout.restore_reclaimed_worker_checkout("resumable")
    assert Path(path).is_dir()
    assert git(path, "branch", "--show-current") == "task/resumable"
    assert git(path, "rev-parse", "HEAD") == tip
    # Still reclaimable once the resumed turn leaves it untouched again.
    assert worker_checkout.reclaim_untouched_worker_checkout("resumable")
    assert not Path(path).exists()


def test_reclaim_ignores_branches_that_are_not_the_workers_own(repo):
    row, parent, *_ = repo
    database.create_session("impostor", "coder", project_v2_id=row["project_v2_id"])
    database.set_session_branch("impostor", "parent-branch", row["project_v2_id"])
    assert not worker_checkout.reclaim_untouched_worker_checkout("impostor")
    assert Path(parent).is_dir()
    assert git(parent, "branch", "--show-current") == "parent-branch"


def test_nested_dispatch_forks_direct_callers_branch_not_routing_root(repo, worker_runtime):
    worker_runtime.work["first"] = lambda path: commit_in(path, "nested.txt", "first worker commit")
    first = dispatch(worker="first")
    assert first.ok, first.error
    row = database.get_session("first")
    cwd = project_resolver.resolve_existing_cwd(row)
    nested_tip = git(cwd, "rev-parse", "HEAD")
    second = dispatch(parent="first", worker="nested")
    assert second.ok, second.error
    launch = worker_runtime[1]
    nested_cwd = launch["overrides"]["cwd"]
    assert nested_cwd != cwd
    assert launch["head"] == nested_tip
    assert launch["branch"] == "task/nested"


def test_checkout_failure_prevents_session_creation_and_provider_submit(repo, worker_runtime, monkeypatch):
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: None)
    outcome = dispatch(worker="failed-spawn")
    assert not outcome.ok
    assert "refusing shared-checkout fallback" in outcome.error
    assert not worker_runtime
    assert database.get_session("failed-spawn") is None


def test_session_insert_failure_reclaims_the_provisioned_checkout(repo, worker_runtime, monkeypatch):
    row, parent, *_ = repo

    def boom(*a, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(database, "create_session", boom)
    outcome = dispatch(worker="never-inserted")
    assert not outcome.ok and "insert failed" in outcome.error
    assert not worker_runtime
    assert not git(parent, "branch", "--list", "task/never-inserted")
    assert "task/never-inserted" not in git(parent, "worktree", "list")


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
        cwd = launch["overrides"]["cwd"]
        paths.add(cwd)
        assert cwd == worker["worktree_path"] != str(parent)
        assert launch["head"] == tip
        assert worker["branch"].startswith("task/") and launch["branch"] == worker["branch"]
    assert len(paths) == len(worker_runtime)
    assert git(parent, "branch", "--show-current") == "parent-branch"


def test_start_retry_reclaims_the_failed_attempts_checkout(repo, worker_runtime, monkeypatch):
    """A zero-frame attempt is re-dispatched as a new worker; its own empty
    checkout must not be left behind."""
    from app.core import task_dispatch
    from app.core.config import settings as cfg

    row, parent, _, tip = repo
    monkeypatch.setattr(cfg, "task_worker_start_retries", 1, raising=False)
    monkeypatch.setattr(task_dispatch, "_START_RETRY_BACKOFF_SECONDS", 0.01)
    attempts = []

    async def dispatcher(**kwargs):
        outcome = await provider_agent.dispatch_worker(**kwargs)
        attempts.append(kwargs["worker_session_id"])
        if len(attempts) == 1:
            return provider_agent.WorkerOutcome(
                ok=False, session_id=outcome.session_id, frames_forwarded=0,
                error="RemoteProtocolError: peer closed connection",
            )
        return outcome

    outcome = asyncio.run(task_dispatch._dispatch_worker_with_retries(
        kwargs=dict(parent_session_id="parent", context_session_id="parent",
                    profile_id="coder", prompt="edit", label="edit",
                    worker_session_id="first-try"),
        background=True, lane="native", provider_id=None,
        parent_session_id="parent", dispatcher=dispatcher,
    ))
    assert outcome.ok, outcome.error
    assert attempts[0] == "first-try" and len(attempts) == 2
    assert [launch["branch"] for launch in worker_runtime] == [f"task/{sid}" for sid in attempts]
    assert all(launch["head"] == tip for launch in worker_runtime)
    assert not git(parent, "branch", "--list", "task/first-try")
    assert not Path(worker_runtime[0]["overrides"]["cwd"]).exists()
