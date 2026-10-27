"""Real Git producer coverage for automatic coding-worker checkout isolation."""
import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core import checkout_manager, project_resolver, provider_agent, worker_checkout
from app.core.config import settings
from app.core.git_service import git_service
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import clean_task_registry, foreign_keys_off, pin_runner  # noqa: F401



def test_checkout_prompt_scopes_isolation_to_its_repository(monkeypatch):
    from app.core import task_dispatch
    from scripts.measure_prompt_catalog import measure_worker_sections

    monkeypatch.setattr(task_dispatch, "_is_worker_session", lambda _: True)
    monkeypatch.setattr(task_dispatch, "caller_chain", lambda _: ("root", "root", 1))
    receipt = worker_checkout.WorkerCheckout("/fixture/worker", "chore/fixture", "0" * 40,
                                            "fixture-project", "main").instructions()
    role = task_dispatch.subagent_system_prompt("fixture")
    for text in (role, receipt):
        assert text.count("repository the host does not provision") == 1
        assert text.count("parent's prepared isolated checkout named in the brief under the same rules") == 1
        assert text.count("over any path the brief names") == 1
        assert "parent's bound checkout" in text
        assert "Do not merge into the parent" in text
        assert "copy credential files or dependencies" in text or "dependencies are not copied" in text
    assert "Use that assigned checkout for its repository" in role
    assert "Use it for this repository" in receipt
    sizes = measure_worker_sections()
    for section, baseline in {"subagent_role": 2712, "checkout_receipt": 1240}.items():
        assert sizes[section] <= baseline * 101 // 100


def test_explicit_review_base_is_materialized_not_parsed_from_prompt(repo, worker_runtime):
    row, parent, base, tip = repo
    review_sha = git(base, "rev-parse", "HEAD")
    outcome = dispatch(base_commit=review_sha)
    assert outcome.ok, outcome.error
    assert worker_runtime[0]["head"] == review_sha != tip
    assert git(parent, "rev-parse", "HEAD") == tip


@pytest.mark.parametrize("value", ["HEAD", "--help", "abc123", "f" * 40, 123])
def test_invalid_or_missing_review_commit_refuses_before_branch_creation(repo, value):
    row, parent, *_ = repo
    with pytest.raises(worker_checkout.WorkerCheckoutError):
        worker_checkout.prepare_worker_checkout(row, "bad-ref", "coder", base_commit=value)
    assert not git(parent, "branch", "--list", "chore/task-bad-ref")


def test_python_and_frontend_receipt_uses_local_checkout_without_copying(repo):
    import json
    row, parent, base, tip = repo
    interpreter = parent / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("interpreter presence canary")
    frontend = parent / "frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").write_text("{}")
    (frontend / "package.json").write_text(json.dumps({"devDependencies": {"@sveltejs/kit": "*"}}))
    git(parent, "add", "frontend")
    git(parent, "commit", "-m", "frontend manifests")
    checkout = worker_checkout.prepare_worker_checkout(row, "runtime-receipt", "coder")
    assert checkout.runtime["python"] == str(interpreter)
    assert not (Path(checkout.path) / ".venv").exists()
    assert checkout.runtime["frontend_monitor_inputs"] == [{
        "command": "npm ci && npx svelte-kit sync", "cwd": str(Path(checkout.path) / "frontend"),
    }]
    assert not (Path(checkout.path) / "frontend" / "node_modules").exists()
    receipt = json.loads(checkout.instructions().splitlines()[1])
    assert receipt["runtime"]["python"] == str(interpreter)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_real_setup_shell_is_tracked_in_worker_and_reports_completion(repo, monkeypatch, exit_code):
    import sys
    import shlex
    from app.core import worker_runtime as runtime, monitor_spawn, turn_scheduler
    from app.core.task_registry import task_registry
    from app.mcp import builtin_tools

    row, parent, *_ = repo
    sid = "real-setup-" + str(exit_code)
    checkout = worker_checkout.prepare_worker_checkout(row, sid, "coder")
    database.create_session(sid, "coder", project_v2_id=row["project_v2_id"])
    database.set_session_branch(sid, checkout.branch, checkout.project_v2_id)
    argv = [sys.executable, "-m", "venv", "--without-pip", ".venv"]
    from fluid.tools.shell import resolve_shell
    command = shlex.join(argv) if resolve_shell().posix else subprocess.list2cmdline(argv)
    command += f" && exit {exit_code}"
    git(parent, "config", "--local", "fluid.workerSetupCommand", command)
    done = asyncio.Event()
    frames = []

    async def terminal(session_id, frame):
        assert session_id == sid
        frames.append(frame)
        done.set()

    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    monkeypatch.setattr(builtin_tools, "_emit_shell_start_frame", AsyncMock())
    monkeypatch.setattr(builtin_tools, "_emit_shell_settle_frame", AsyncMock())
    monkeypatch.setattr(monitor_spawn, "_WATCH_POLL_SECONDS", 0.01)
    await runtime.start_worker_runtime(checkout, sid, "coder")
    task_id = checkout.runtime["setup_task_id"]
    assert task_registry.get_task(sid, task_id)["kind"] == "shell"
    try:
        await asyncio.wait_for(done.wait(), 20)
        assert frames[0]["status"] == ("completed" if exit_code == 0 else "error")
        assert frames[0]["task_id"] == task_id
        assert (Path(checkout.path) / ".venv" / "pyvenv.cfg").is_file()
        assert not (parent / ".venv").exists()
        assert builtin_tools._background_tasks[task_id].exit_code == exit_code
    finally:
        task = builtin_tools._background_tasks[task_id]
        if task.is_running():
            await builtin_tools.stop_background_shell_task(task_id, session_id=sid)
        builtin_tools._background_tasks.pop(task_id, None)
        task_registry.clear_session(sid)


def test_explicit_review_commit_requires_a_project(repo):
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="requires a Git project"):
        worker_checkout.prepare_worker_checkout({}, "no-project", "coder", base_commit="a" * 40)


def test_setup_spawn_failure_reports_status_without_leaking_command(repo, monkeypatch):
    from app.core import worker_runtime as runtime, monitor_spawn
    row, parent, *_ = repo
    git(parent, "config", "--local", "fluid.workerSetupCommand", "private-command-canary")
    checkout = worker_checkout.prepare_worker_checkout(row, "setup-failed", "coder")

    def fail(*args, **kwargs):
        raise OSError("private-command-canary")

    monkeypatch.setattr(monitor_spawn, "make_fluid_monitor_spawner", fail)
    asyncio.run(runtime.start_worker_runtime(checkout, "setup-failed", "coder"))
    assert checkout.runtime["setup_status"].startswith("failed to start")
    assert "private-command-canary" not in checkout.instructions()


def test_local_setting_read_failure_is_nonfatal(monkeypatch):
    from app.core.worker_runtime import _local_setting

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(git_service, "_run_git", timeout)
    assert _local_setting("unused", "fluid.workerSetupCommand") == ""


@pytest.mark.asyncio
async def test_runtime_setup_reads_run_off_the_event_loop(monkeypatch):
    import threading
    from types import SimpleNamespace
    from app.core import worker_runtime as runtime, monitor_spawn

    loop_thread = threading.get_ident()
    reads = []

    def setting(path, key):
        assert threading.get_ident() != loop_thread
        reads.append("config")
        return "setup-command"

    def profile(profile_id):
        assert threading.get_ident() != loop_thread
        reads.append("profile")
        return {"config": {}}

    async def spawn(inputs, ctx):
        assert threading.get_ident() == loop_thread
        return SimpleNamespace(background_task=SimpleNamespace(task_id="setup-task"))

    monkeypatch.setattr(runtime, "_local_setting", setting)
    monkeypatch.setattr(database, "get_profile", profile)
    monkeypatch.setattr(monitor_spawn, "make_fluid_monitor_spawner", lambda *a, **kw: spawn)
    checkout = SimpleNamespace(path="unused", runtime={})
    await runtime.start_worker_runtime(checkout, "worker", "coder")
    assert reads == ["config", "profile"]
    assert checkout.runtime["setup_task_id"] == "setup-task"


def test_configured_python_wins_without_running_it(repo):
    row, parent, *_ = repo
    python = parent / "project-python"
    python.write_text("not executable")
    git(parent, "config", "--local", "fluid.workerPython", str(python))
    checkout = worker_checkout.prepare_worker_checkout(row, "configured-python", "coder")
    assert checkout.runtime["python"] == str(python)


@pytest.mark.parametrize("enabled", [False, True])
def test_dispatch_starts_opted_in_runtime_without_joining(repo, worker_runtime, monkeypatch, enabled):
    from types import SimpleNamespace
    from app.core import monitor_spawn
    row, parent, *_ = repo
    calls = []
    if enabled:
        git(parent, "config", "--local", "fluid.workerSetupCommand", "project-setup-command")

    def factory(sid, **kwargs):
        assert kwargs == {"max_budget_seconds": 600}
        async def spawn(inputs, ctx):
            assert database.get_session(sid) is not None
            assert worker_runtime, "setup must follow initial submission"
            calls.append((sid, inputs))
            return SimpleNamespace(background_task=SimpleNamespace(task_id="setup-task"))
        return spawn

    monkeypatch.setattr(monitor_spawn, "make_fluid_monitor_spawner", factory)
    outcome = dispatch()
    assert outcome.ok, outcome.error
    assert bool(calls) == enabled
    if enabled:
        assert calls[0][1] == {"command": "project-setup-command", "cwd": worker_runtime[0]["overrides"]["cwd"]}
        assert "scheduled after initial turn submission" in worker_runtime[0]["overrides"]["system_prompt_append"]


@pytest.mark.parametrize("tools,blocked", [(["Read"], []), (["Bash"], ["Monitor"]), (["Bash"], ["mcp__builtins__Mon*"])])
def test_runtime_respects_shell_restrictions(repo, monkeypatch, tools, blocked):
    from app.core import worker_runtime as runtime, monitor_spawn
    row, parent, *_ = repo
    git(parent, "config", "--local", "fluid.workerSetupCommand", "must-not-run")
    checkout = worker_checkout.prepare_worker_checkout(row, "restricted-runtime", "coder")
    monkeypatch.setattr(monitor_spawn, "make_fluid_monitor_spawner", lambda *a, **kw: pytest.fail("restricted"))
    asyncio.run(runtime.start_worker_runtime(checkout, "restricted-runtime", "coder", tools, blocked))
    assert checkout.runtime["setup_status"].startswith("skipped")


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


def test_no_project_owner_task_uses_named_project_checkout(repo, worker_runtime, monkeypatch):
    from app.core import session_orchestration as orch, task_dispatch
    from app.core import turn_manager
    row, parent, base, _ = repo
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    database.create_profile("coordinator-agent", "Coordinator", None, {"model": "sonnet"})
    database.create_session("coordinator", "coordinator-agent", mode="chat", is_active=True)
    submit = turn_manager.submit_async
    async def complete(sid, source, **kwargs):
        worker_runtime.work[sid] = lambda cwd: commit_in(cwd)
        return await submit(sid, source, **kwargs)
    monkeypatch.setattr(turn_manager, "submit_async", complete)
    outcome = asyncio.run(task_dispatch.dispatch_task_tool(
        {"agent": "coder", "prompt": "Work", "project": row["project_v2_id"], "mode": "sync"},
        caller_session_id="coordinator"))
    assert outcome["ok"], outcome
    child = database.get_session(worker_runtime[0]["session_id"])
    assert child["project_v2_id"] == row["project_v2_id"]
    assert child["worktree_path"] not in {str(base), str(parent)}
    assert child["branch"] == child["worktree_branch"] == "chore/task-" + child["id"]
    assert worker_runtime[0]["head"] == git(base, "rev-parse", "HEAD")
    assert worker_runtime[0]["overrides"]["cwd"] == child["worktree_path"]


def test_ceiling_chat_coding_worker_without_project_is_refused(
        repo: Any, worker_runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A no-project chat the agent browser's preview cookie sends keeps the
    # ceiling (#641), so a code-capable worker still needs a project.
    from app.core import session_orchestration as orch
    monkeypatch.setattr(orch, "_principal", lambda sid: {
        "owner": "install-admin", "kind": "admin", "principal_id": "preview"})
    database.create_session("coordinator", "coder", mode="chat", is_active=True)
    outcome = dispatch(parent="coordinator", worker="no-project-worker")
    assert not outcome.ok and "must name a registered project" in outcome.error
    assert worker_runtime == []
    assert database.get_session("no-project-worker") is None


def test_owner_no_project_coding_worker_runs_in_the_no_project_folder(
        repo: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # #641: the owner's own no-project chat is a normal session; its coding
    # worker omits project= and shares the no-project folder (the non-Git rule).
    from app.core import config, turn_manager, turn_scheduler
    from app.core import session_orchestration as orch
    from app.core.session_modes import owner_chat_dir
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    submitted = []

    async def submit(sid: str, source: str, **kwargs: Any) -> str:
        submitted.append({"session_id": sid, **kwargs})
        return "test-turn"

    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "wait_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    monkeypatch.setattr(provider_agent, "final_turn_outcome", lambda sid: {
        "terminal_found": True, "error": None, "interrupted": False, "usage": {},
    })
    monkeypatch.setattr(provider_agent, "final_assistant_text", lambda sid: "completed")
    database.create_session("coordinator", "coder", mode="chat", is_active=True)
    outcome = dispatch(parent="coordinator", worker="no-project-worker")
    assert outcome.ok, outcome.error
    worker = database.get_session("no-project-worker")
    assert worker["mode"] is None
    assert not (worker["project_id"] or worker["project_v2_id"] or worker["worktree_path"])
    assert [launch["overrides"]["cwd"] for launch in submitted] == [str(owner_chat_dir())]


async def test_no_project_start_session_has_own_git_panel_binding(repo, monkeypatch):
    import json
    from fluid.tools.registry import ToolCtx
    from app.core import session_orchestration as orch, turn_scheduler
    from app.db import turns
    row, parent, base, _ = repo
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    database.create_session("coordinator", "coder", mode="chat", is_active=True)
    first = turns.enqueue_turn("coordinator", "user", prompt="Start coding", profile_id="coder")
    turns.start_turn(first["id"])
    turns.settle_turn(first["id"], "completed")
    result = await orch.make_handler(lambda: "coordinator")("start", {
        "project": row["project_v2_id"], "title": "Work", "prompt": "Edit code", "request_id": "named-git",
    }, ToolCtx())
    assert not result.is_error, result.text
    receipt = json.loads(result.text)
    child = database.get_session(receipt["session_id"])
    assert child["project_v2_id"] == row["project_v2_id"] and child["mode"] is None
    assert child["branch"] == child["worktree_branch"] == receipt["checkout"]["branch"]
    assert project_resolver.resolve_existing_cwd(child) == receipt["checkout"]["path"]
    assert git(base, "rev-parse", "HEAD") == receipt["checkout"]["base_commit"]
    assert child["worktree_path"] not in {str(base), str(parent)}


def test_checkout_producer_forks_actual_caller_tip_and_preserves_dirty_work(repo, monkeypatch):
    monkeypatch.setattr(git_service, "fetch", lambda *a, **kw: pytest.fail("worker provisioning must not use the long general-purpose fetch"))
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
    with pytest.raises(worker_checkout.WorkerCheckoutError) as refused:
        worker_checkout.prepare_worker_checkout(row, "writer", "coder")
    # #1606: the folder exists, so the refusal names its actual branch rather
    # than reporting the checkout as missing.
    message = str(refused.value)
    assert "on branch 'wrong-branch', not this session's branch 'parent-branch'" in message
    assert str(parent) in message
    assert "no longer exists" not in message and "unavailable" not in message
    assert git(parent, "branch", "--show-current") == "wrong-branch"
    assert not git(parent, "branch", "--list", "chore/task-writer")


def test_provisioning_failure_never_falls_back_and_reclaims_its_empty_branch(repo, monkeypatch):
    row, parent, *_ = repo
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: None)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="refusing shared-checkout fallback"):
        worker_checkout.prepare_worker_checkout(row, "failed", "coder")
    # The branch never held a commit of its own, so nothing is lost by
    # removing it — and a retry is not left a stray ref to trip over.
    assert not git(parent, "branch", "--list", "chore/task-failed")
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
    assert not git(parent, "branch", "--list", "chore/task-raced")
    assert "chore/task-raced" not in git(parent, "worktree", "list")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("submission", ["queued", "running", "failed", "cancelled"])
async def test_setup_completion_waits_for_initial_dispatch_context(
    repo, monkeypatch, clean_task_registry, submission,
):
    """Real setup shell + real terminal scheduler, not a mocked terminal hook.

    Keep the first turn queued (or running) deliberately, so a quick setup
    cannot borrow a broad saved profile while waiting for an engine slot.
    """
    import shlex
    import sys
    from app.core import turn_manager, turn_scheduler, monitor_spawn
    from app.core.provider_agent_feed import WorkerFeed
    from app.core.task_registry import task_registry
    from app.db import turns
    from app.mcp import builtin_tools

    row, parent, *_ = repo
    sid = "setup-race-" + submission
    argv = [sys.executable, "-c", "pass"]
    from fluid.tools.shell import resolve_shell
    command = shlex.join(argv) if resolve_shell().posix else subprocess.list2cmdline(argv)
    git(parent, "config", "--local", "fluid.workerSetupCommand", command)
    initial = []
    wakes = []
    wake_ready = asyncio.Event()
    real_enqueue = turn_scheduler._enqueue
    original_tasks = set(builtin_tools._background_tasks)

    async def started(_feed):
        # Dispatch can await event recording here. Setup must not exist yet.
        await asyncio.sleep(0)
        assert set(builtin_tools._background_tasks) == original_tasks
        assert turns.get_queued_turns(sid) == []

    async def submit(session_id, source, **kwargs):
        assert source == provider_agent.WORKER_TURN_SOURCE
        if submission == "failed":
            raise RuntimeError("initial submission refused")
        if submission == "cancelled":
            raise asyncio.CancelledError()
        turn = turns.enqueue_turn(session_id, source, **kwargs)
        initial.append(turn)
        if submission == "running":
            turns.start_turn(turn["id"], None, "test-runner")
        return turn["id"]

    def enqueue(session_id, source, **kwargs):
        turn = real_enqueue(session_id, source, **kwargs)
        if session_id == sid and source == "unpark":
            wakes.append((len(initial), turn))
        return turn

    def kick(session_id):
        if session_id == sid:
            wake_ready.set()

    async def idle(*args, **kwargs):
        await asyncio.wait_for(wake_ready.wait(), 10)
        # Simulate the provider completing after receiving the setup result;
        # the terminal scheduler and the actual wake rows were not stubbed.
        for turn in [*initial, *(turn for _, turn in wakes)]:
            turns.settle_turn(turn["id"], "completed", stop_reason="end_turn")
        return True

    monkeypatch.setattr(WorkerFeed, "started", started)
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "wait_session_idle", idle)
    monkeypatch.setattr(turn_scheduler, "_enqueue", enqueue)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", kick)
    monkeypatch.setattr(builtin_tools, "_emit_shell_start_frame", AsyncMock())
    monkeypatch.setattr(builtin_tools, "_emit_shell_settle_frame", AsyncMock())
    monkeypatch.setattr(monitor_spawn, "_WATCH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(provider_agent, "final_turn_outcome", lambda _sid: {
        "terminal_found": True, "error": None, "interrupted": False, "usage": {},
    })
    monkeypatch.setattr(provider_agent, "final_assistant_text", lambda _sid: "completed")
    try:
        call = provider_agent.dispatch_worker(
            parent_session_id="parent", profile_id="coder", worker_session_id=sid,
            prompt="review the assigned commit", system_prompt_append="REVIEW ROLE",
            tool_allowlist=["Bash", "Read"], tool_denylist=["TaskStop"],
        )
        if submission == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            outcome = await call
            assert outcome.ok is (submission != "failed"), outcome.error
        if submission in ("failed", "cancelled"):
            assert not initial and not wakes
            assert set(builtin_tools._background_tasks) == original_tasks
            assert turns.get_queued_turns(sid) == []
        else:
            assert len(wakes) == 1
            submitted_count, wake = wakes[0]
            assert submitted_count == 1
            assert wake["overrides"] == initial[0]["overrides"]
            assert wake["parent_turn_id"] == initial[0]["id"]
            assert initial[0]["prompt"] == "review the assigned commit"
            assert wake["overrides"]["tool_allowlist"] == ["Bash", "Read"]
            assert wake["overrides"]["tool_denylist"] == ["TaskStop"]
            assert "REVIEW ROLE" in wake["overrides"]["system_prompt_append"]
            assert "Host-provisioned worker checkout" in wake["overrides"]["system_prompt_append"]
            assert wake["overrides"]["cwd"] != str(parent)
    finally:
        for task_id in set(builtin_tools._background_tasks) - original_tasks:
            task = builtin_tools._background_tasks[task_id]
            if task.is_running():
                await builtin_tools.stop_background_shell_task(task_id, session_id=sid)
            builtin_tools._background_tasks.pop(task_id, None)
        task_registry.clear_session(sid)


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
    assert worker["branch"] == worker["worktree_branch"] == "chore/task-spawn-a"
    assert worker["worktree_id"] is None
    assert worker["project_v2_id"] == row["project_v2_id"]
    launch = worker_runtime[0]
    overrides = launch["overrides"]
    assert overrides["cwd"] == worker["worktree_path"] != str(parent)
    assert launch["branch"] == "chore/task-spawn-a" and launch["head"] == tip
    assert "FILE ROLE" in overrides["system_prompt_append"]
    assert "Host-provisioned worker checkout" in overrides["system_prompt_append"]
    assert tip in overrides["system_prompt_append"]
    assert database.get_session("parent")["branch"] == "parent-branch"
    assert git(parent, "branch", "--show-current") == "parent-branch"
    # The worker finished without touching its checkout: reclaimed, with the
    # tip recorded so a resume can bring it back.
    assert not Path(worker["worktree_path"]).exists()
    assert not git(parent, "branch", "--list", "chore/task-spawn-a")
    record = checkouts.get_checkout(row["project_v2_id"], settings.effective_instance_id,
                                    branch="chore/task-spawn-a")
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
    assert git(parent, "branch", "--list", "chore/task-keeper")
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
    assert git(path, "branch", "--show-current") == "chore/task-resumable"
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
    assert launch["branch"] == "chore/task-nested"


def test_checkout_failure_prevents_session_creation_and_provider_submit(repo, worker_runtime, monkeypatch):
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **kw: None)
    outcome = dispatch(worker="failed-spawn")
    assert not outcome.ok
    assert "refusing shared-checkout fallback" in outcome.error
    assert not worker_runtime
    assert database.get_session("failed-spawn") is None


def test_parent_on_wrong_branch_refusal_names_the_branch_and_is_not_retried(repo, worker_runtime):
    # #1606: the caller's registered folder was switched to another branch.
    # The guard must still refuse, and say so, instead of "no longer exists".
    from app.core import task_dispatch

    row, parent, *_ = repo
    git(parent, "switch", "-c", "fix/other-work")
    outcome = dispatch(worker="wrong-branch-spawn")
    assert not outcome.ok
    assert "on branch 'fix/other-work', not this session's branch 'parent-branch'" in outcome.error
    assert "no longer exists" not in outcome.error
    assert not worker_runtime
    assert database.get_session("wrong-branch-spawn") is None
    assert git(parent, "branch", "--show-current") == "fix/other-work"
    used = {"start": 0, "rate": 0}
    assert task_dispatch._retry_kind("error", outcome.error, 0, used, start_max=2, rate_max=2) is None


def test_session_insert_failure_reclaims_the_provisioned_checkout(repo, worker_runtime, monkeypatch):
    row, parent, *_ = repo

    def boom(*a, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(database, "create_session", boom)
    outcome = dispatch(worker="never-inserted")
    assert not outcome.ok and "insert failed" in outcome.error
    assert not worker_runtime
    assert not git(parent, "branch", "--list", "chore/task-never-inserted")
    assert "chore/task-never-inserted" not in git(parent, "worktree", "list")


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
        assert worker["branch"].startswith("chore/task-") and launch["branch"] == worker["branch"]
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
    assert [launch["branch"] for launch in worker_runtime] == [f"chore/task-{sid}" for sid in attempts]
    assert all(launch["head"] == tip for launch in worker_runtime)
    assert not git(parent, "branch", "--list", "chore/task-first-try")
    assert not Path(worker_runtime[0]["overrides"]["cwd"]).exists()


def test_mismatch_diagnosis_does_not_report_an_enclosing_repo_branch(repo):
    # #1606: a leftover folder with no .git of its own (a pruned worktree's
    # remains inside the main checkout) must not be reported as "on branch
    # main" with advice to switch it back, which would switch the enclosing
    # checkout. It keeps the missing-checkout wording instead.
    row, parent, base, _ = repo
    stale = base / ".fluid" / "worktrees" / "stale"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("remains\n")
    checkouts.upsert_checkout(row["project_v2_id"], settings.effective_instance_id,
                              str(stale), branch="stale-branch")
    database.create_session("stale", "coder", project_v2_id=row["project_v2_id"])
    database.set_session_branch("stale", "stale-branch", row["project_v2_id"])
    assert project_resolver.describe_checkout_mismatch(database.get_session("stale")) is None
    assert git(base, "branch", "--show-current") == "main"
    # The real wrong-branch case still names the folder's branch.
    git(parent, "switch", "-c", "elsewhere")
    assert "on branch 'elsewhere'" in project_resolver.describe_checkout_mismatch(row)


@pytest.mark.parametrize("case", ["pushed", "local-head", "local-commit-then-pushed"])
def test_reviewer_moved_head_is_reclaimed_at_finish_only_when_pushed(repo, worker_runtime, case):
    """A reviewer worker that ran ``gh pr checkout`` / ``git checkout --detach``
    leaves its own branch untouched: released at finish when HEAD and every
    commit it visited are pushed."""
    row, parent, base, _ = repo
    published = git(base, "rev-parse", "refs/remotes/origin/parent-branch")
    pushed = case == "pushed"

    def review(path):
        if case != "pushed":
            git(path, "checkout", "--detach")
            (path / "local.txt").write_text("review fixup never pushed")
            git(path, "add", "local.txt")
            git(path, "commit", "-m", "local only")
        if case != "local-head":
            git(path, "checkout", "--detach", published)

    worker_runtime.work["reviewer"] = review
    outcome = dispatch(worker="reviewer")
    assert outcome.ok, outcome.error
    path = Path(database.get_session("reviewer")["worktree_path"])
    assert path.exists() is (not pushed)
    assert bool(git(parent, "branch", "--list", "chore/task-reviewer")) is (not pushed)
    if case == "local-head":
        assert (path / "local.txt").read_text() == "review fixup never pushed"


def test_abandoned_worker_checkout_is_archived_and_restored_on_resume(repo, worker_runtime, monkeypatch):
    """Producing path: a worker leaves unmerged commits and uncommitted files,
    then it and its parent go quiet for a week. The sweeper snapshots it under
    refs/fluid-archive/ and removes it; resuming the worker brings it all back."""
    import os
    import time
    from datetime import datetime, timezone

    from app.core import worktree_reclaim
    from app.db import turns
    from app.db.base import get_db

    row, parent, base, _ = repo

    def leave_work(path):
        commit_in(path, "finding.txt", "committed finding")
        (path / "code.txt").write_text("edited, never committed\n")
        (path / "scratch.txt").write_text("untracked notes\n")

    worker_runtime.work["abandoned"] = leave_work
    outcome = dispatch(worker="abandoned")
    assert outcome.ok, outcome.error
    path = Path(database.get_session("abandoned")["worktree_path"])
    head = git(path, "rev-parse", "HEAD")
    assert path.is_dir()  # held work: retained at finish
    for sid in ("abandoned", "parent"):  # the provider is stubbed: record their finished turns
        turns.record_adhoc_turn(sid, "test", outcome="completed")
    week_ago = time.time() - 8 * 24 * 3600
    old = datetime.fromtimestamp(week_ago, timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sessions SET updated_at = %s, is_active = %s WHERE id IN (%s, %s)",
                       (old, False, "abandoned", "parent"))
        cursor.execute("UPDATE turns SET ended_at = %s WHERE session_id IN (%s, %s)", (old, "abandoned", "parent"))
        cursor.execute("UPDATE checkouts SET created_at = %s", (old,))
    os.utime(path, (week_ago, week_ago))

    result = asyncio.run(worktree_reclaim.reclaim(row["project_v2_id"], dry_run=False))
    item = next(i for i in result["items"] if Path(i["path"]) == path.resolve())
    assert item["decision"] == "archive" and item["applied"], item["reason"]
    snapshot = git(base, "rev-parse", "refs/fluid-archive/chore/task-abandoned")
    assert not path.exists() and not git(base, "branch", "--list", "chore/task-abandoned")
    assert git(base, "rev-parse", snapshot + "^") == head
    record = checkouts.get_checkout(row["project_v2_id"], settings.effective_instance_id,
                                    branch="chore/task-abandoned")
    assert record["status"] == "archived" and record["head_sha"] == snapshot

    assert worker_checkout.restore_reclaimed_worker_checkout("abandoned")
    assert git(path, "rev-parse", "HEAD") == snapshot
    assert (path / "scratch.txt").read_text() == "untracked notes\n"
    assert (path / "code.txt").read_text() == "edited, never committed\n"
    assert (path / "finding.txt").read_text() == "committed finding"
    assert subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", "--quiet",
                           "refs/fluid-archive/chore/task-abandoned"], capture_output=True).returncode != 0


def test_restore_fails_clearly_when_the_recorded_tip_is_gone(repo, worker_runtime, monkeypatch):
    row, parent, base, _ = repo
    outcome = dispatch(worker="lost")
    assert outcome.ok, outcome.error
    path = database.get_session("lost")["worktree_path"]
    assert not Path(path).exists()
    checkouts.set_head_sha_for_path(path, "f" * 40, project_id=row["project_v2_id"])
    assert worker_checkout.restore_reclaimed_worker_checkout("lost") is False
    assert not Path(path).exists()



def test_resume_refuses_a_folder_left_by_a_stopped_archive(repo, worker_runtime):
    row, parent, base, _ = repo
    worker_runtime.work["stopped"] = lambda path: (path / "scratch.txt").write_text("unfinished\n")
    outcome = dispatch(worker="stopped")
    assert outcome.ok, outcome.error
    path = Path(database.get_session("stopped")["worktree_path"])
    assert path.is_dir()
    git(base, "update-ref", "refs/fluid-archive/chore/task-stopped", git(path, "rev-parse", "HEAD"))
    ok, note = worker_checkout.restore_worker_checkout("stopped")
    assert ok is False
    assert "cleanup may have stopped part-way" in note and "refs/fluid-archive/chore/task-stopped" in note
    assert "branch chore/task-stopped-restored refs/fluid-archive/chore/task-stopped" in note
    assert worker_checkout.restore_reclaimed_worker_checkout("stopped") is False


def test_restore_onto_existing_branch_explains_the_retained_archive(repo, worker_runtime):
    row, parent, base, _ = repo
    worker_runtime.work["restored"] = commit_in
    outcome = dispatch(worker="restored")
    assert outcome.ok, outcome.error
    path = Path(database.get_session("restored")["worktree_path"])
    head = git(path, "rev-parse", "HEAD")
    ref = "refs/fluid-archive/chore/task-restored"
    snapshot = git(base, "commit-tree", head + "^{tree}", "-p", head, "-m", "archived snapshot")
    git(base, "update-ref", ref, snapshot)
    assert git_service.remove_worktree(str(base), str(path), force=False)
    assert worker_checkout.restore_worker_checkout("restored") == (True, None)
    assert git(path, "rev-parse", "HEAD") == head
    assert git(base, "rev-parse", ref) == snapshot  # branch doesn't hold this snapshot
    ok, note = worker_checkout.restore_worker_checkout("restored")
    assert ok is False
    assert "a restore retained the snapshot" in note and ref in note
    assert "was archived but removal stopped" not in note
    assert "Dispatch a new Task" in note


def test_untouched_finish_keeps_the_only_ref_to_a_parent_commit(repo, worker_runtime):
    row, parent, base, parent_tip = repo
    published = git(base, "rev-parse", "refs/remotes/origin/parent-branch")

    def parent_drops_tip(path):
        # Move the test parent's ref back during the worker's turn, without
        # touching the worker's clean checkout or its single-entry reflog.
        git(base, "update-ref", "refs/heads/parent-branch", published, parent_tip)

    worker_runtime.work["only-ref"] = parent_drops_tip
    outcome = dispatch(worker="only-ref")
    assert outcome.ok, outcome.error
    path = Path(database.get_session("only-ref")["worktree_path"])
    assert path.is_dir()
    assert git(path, "status", "--porcelain") == ""
    assert git(base, "for-each-ref", "--format=%(refname)", "--contains", parent_tip) == "refs/heads/chore/task-only-ref"
    assert not worker_checkout.reclaim_untouched_worker_checkout("only-ref")
    assert git(base, "rev-parse", "chore/task-only-ref") == parent_tip


def test_reviewer_finish_keeps_a_starting_commit_only_its_branch_holds(repo, worker_runtime, monkeypatch):
    """The parent's unpublished tip is the worker's base; the parent then resets its branch."""
    row, parent, base, parent_tip = repo
    published = git(base, "rev-parse", "refs/remotes/origin/parent-branch")

    def review(path):
        git(parent, "reset", "--hard", published)  # the parent drops its unpublished commit
        git(path, "checkout", "--detach", published)

    worker_runtime.work["orphaned"] = review
    outcome = dispatch(worker="orphaned")
    assert outcome.ok, outcome.error
    path = Path(database.get_session("orphaned")["worktree_path"])
    assert path.exists()
    assert git(base, "rev-parse", "chore/task-orphaned") == parent_tip
