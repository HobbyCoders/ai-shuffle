"""Checkout continuation and PR polling regressions, with real Git bindings."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import model_override, session_pr_state, turn_scheduler, worker_checkout
from app.core.git_service import git_service
from app.db import database
from tests.core.test_worker_checkout import repo, git  # noqa: F401
from tests.core.test_model_override import world, chats  # noqa: F401
from tests.shared_fixtures import foreign_keys_off, pin_runner, clean_task_registry  # noqa: F401


@pytest.mark.parametrize("wording", ["claude-opus-5", "Opus 5", "CLAUDE OPUS 5", "opus-5"])
@pytest.mark.parametrize("remote", [False, True])
def test_versioned_native_override_is_refused_with_family_choices(mock_db, wording, remote):
    with pytest.raises(ValueError, match="family name") as refused:
        model_override.resolve(wording, runs_here=not remote)
    assert "Choose one of:" in str(refused.value) and "opus" in str(refused.value)


def bind_worker(repo, sid="followup"):
    row, _, _, _ = repo
    checkout = worker_checkout.prepare_worker_checkout(row, sid, "coder")
    database.create_session(sid, "coder", project_v2_id=row["project_v2_id"],
                            worktree_path=checkout.path, worktree_branch=checkout.branch)
    database.set_session_branch(sid, checkout.branch, checkout.project_v2_id)
    return checkout


@pytest.mark.asyncio
async def test_message_admission_restores_checkout_even_without_completed_task_row(repo, monkeypatch):
    checkout = bind_worker(repo)
    # Actual finisher path: a branch switch at the same commit is eligible for
    # untouched cleanup, even though the resolver correctly refuses that folder.
    git(checkout.path, "switch", "-c", "review-followup")
    assert worker_checkout.reclaim_untouched_worker_checkout("followup")
    assert not Path(checkout.path).exists()

    async def enqueue(*args, **kwargs):
        assert Path(checkout.path).is_dir(), "message admitted with the reclaimed saved cwd"
        assert git(checkout.path, "branch", "--show-current") == checkout.branch
        return {}  # no execution in this producer test

    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr("app.core.agent_messaging.message_provenance_async", AsyncMock(return_value="parent"))
    await turn_scheduler.on_agent_message("followup", "parent", "continue")


@pytest.mark.asyncio
async def test_checkout_mismatch_refuses_message_before_enqueue_and_preserves_work(repo, monkeypatch):
    checkout = bind_worker(repo)
    git(checkout.path, "switch", "-c", "owner-work")
    marker = Path(checkout.path) / "unsaved.txt"
    marker.write_text("keep this")
    enqueue = AsyncMock(return_value={})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    with pytest.raises(worker_checkout.WorkerCheckoutError, match="Switch.*back|switch.*back"):
        await turn_scheduler.on_agent_message("followup", "parent", "continue")
    enqueue.assert_not_awaited()
    assert marker.read_text() == "keep this"
    assert git(checkout.path, "branch", "--show-current") == "owner-work"


@pytest.mark.asyncio
async def test_unavailable_checkout_pr_discovery_uses_base_not_refused_folder(repo, monkeypatch, caplog):
    from app.api import git_helpers
    from app.core.github_service import github_service
    checkout = bind_worker(repo)
    git(checkout.path, "switch", "-c", "owner-work")
    pr = SimpleNamespace(number=17, url="https://github.com/example/repo/pull/17",
                         state="OPEN", head_branch=checkout.branch, title="Worker fix")
    calls = []
    def repo_from(path):
        calls.append(path)
        assert path == str(repo[2])
        return "example/repo"
    monkeypatch.setattr(github_service, "get_repo_from_remote", repo_from)
    monkeypatch.setattr(github_service, "list_pulls", lambda *a, **kw: [pr])
    monkeypatch.setattr(session_pr_state, "persist_pr_state", AsyncMock(return_value=True))
    session_pr_state._reconcile_recent.invalidate()
    task = session_pr_state.schedule_session_pr_reconcile("followup", force=True)
    await task
    assert len(calls) == 1
    session_pr_state.persist_pr_state.assert_awaited_once()
    assert session_pr_state.persist_pr_state.await_args.kwargs["pr_number"] == 17
    assert "exhausted retries" not in caplog.text
    with pytest.raises(git_helpers.SessionCheckoutUnavailable):
        await git_helpers.get_working_dir_async(checkout.project_v2_id, session_id="followup")


@pytest.mark.asyncio
async def test_finisher_waits_for_admission_and_keeps_queued_continuation(repo, monkeypatch):
    checkout = bind_worker(repo)
    entered, release = asyncio.Event(), asyncio.Event()
    busy = False

    async def enqueue(*args, **kwargs):
        nonlocal busy
        entered.set()
        await release.wait()
        busy = True
        return {}

    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr("app.core.agent_messaging.message_provenance_async", AsyncMock(return_value="parent"))
    monkeypatch.setattr("app.db.turns.session_has_unsettled_turns", lambda _: busy)
    admission = asyncio.create_task(turn_scheduler.on_agent_message("followup", "parent", "continue"))
    await entered.wait()
    cleanup = asyncio.create_task(worker_checkout.reclaim_finished_worker_checkout("followup"))
    await asyncio.sleep(0)  # let cleanup contend for the actual admission lock
    assert not cleanup.done()
    release.set()
    await admission
    assert not await cleanup
    assert Path(checkout.path).is_dir()


@pytest.mark.asyncio
async def test_cleanup_before_admission_restores_recorded_tip(repo, monkeypatch):
    checkout = bind_worker(repo)
    assert await worker_checkout.reclaim_finished_worker_checkout("followup")
    assert not Path(checkout.path).exists()
    enqueue = AsyncMock(return_value={})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr("app.core.agent_messaging.message_provenance_async", AsyncMock(return_value="parent"))
    await turn_scheduler.on_agent_message("followup", "parent", "continue")
    assert Path(checkout.path).is_dir()
    assert git(checkout.path, "rev-parse", "HEAD") == checkout.base_commit
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_running_message_returns_actionable_checkout_refusal(repo, monkeypatch):
    from app.core import agent_messaging, task_registry
    checkout = bind_worker(repo)
    git(checkout.path, "switch", "-c", "owner-work")
    registry = task_registry.task_registry
    registry.start_task("parent", "followup", "agent", "work", agent="Helper")
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda _: None)
    monkeypatch.setattr(agent_messaging, "peer_rows_async", AsyncMock(return_value=[{
        "name": "Helper", "ref": "followup", "worker_session_id": "followup", "status": "running"}]))
    outcome = await agent_messaging.make_message(lambda: "parent")({"to": "Helper", "message": "continue"}, None)
    assert outcome.is_error
    assert "Switch that checkout back" in outcome.text
    assert "dispatch a new Task" in outcome.text


@pytest.mark.asyncio
async def test_unavailable_checkout_does_not_adopt_base_head_pr(repo, monkeypatch, caplog):
    from app.core.github_service import github_service
    checkout = bind_worker(repo)
    git(checkout.path, "switch", "-c", "owner-work")
    monkeypatch.setattr(github_service, "get_repo_from_remote", lambda _: "example/repo")
    monkeypatch.setattr(github_service, "list_pulls", lambda *a, **kw: [])
    fallback = lambda *a, **kw: pytest.fail("base HEAD is not proof of worker authorship")
    monkeypatch.setattr(session_pr_state, "find_pull_for_checkout_fallback", fallback)
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(session_pr_state, "persist_pr_state", persist)
    for _ in range(2):
        await session_pr_state.schedule_session_pr_reconcile("followup", force=True)
    persist.assert_not_awaited()
    assert "exhausted retries" not in caplog.text


@pytest.mark.asyncio
async def test_no_base_checkout_leaves_pr_unknown_without_retrying(mock_db, monkeypatch, caplog):
    from app.db import projects_v2
    from app.core.github_service import github_service
    project = projects_v2.upsert_project("local:no-pr-checkout", name="Repo")
    database.create_session("no-pr-checkout", "default", project_v2_id=project["id"])
    database.set_session_branch("no-pr-checkout", "feat/missing")
    monkeypatch.setattr(github_service, "get_repo_from_remote", lambda *args: pytest.fail("no validated repo"))
    reconcile = AsyncMock(wraps=session_pr_state.reconcile_session_pr)
    monkeypatch.setattr(session_pr_state, "reconcile_session_pr", reconcile)
    assert await reconcile("no-pr-checkout") is None
    reconcile.reset_mock()
    await session_pr_state.schedule_session_pr_reconcile("no-pr-checkout", force=True)
    reconcile.assert_awaited_once()
    assert database.get_session("no-pr-checkout")["pr_state"] is None
    assert "exhausted retries" not in caplog.text


@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.asyncio
async def test_refused_checkout_still_refreshes_known_or_listed_pr(repo, monkeypatch, listed):
    from app.core.github_service import github_service
    from app.db import sessions
    checkout = bind_worker(repo)
    git(checkout.path, "switch", "-c", "owner-work")
    sessions.update_session_pr_state("followup", pr_number=17,
        pr_url="https://github.com/example/repo/pull/17", pr_state="open", primary=not listed)
    monkeypatch.setattr(github_service, "get_repo_from_remote", lambda _: "example/repo")
    monkeypatch.setattr(github_service, "list_pulls", lambda *a, **kw: [])
    lookups = []
    def pull(repo_name, number):
        lookups.append(number)
        return SimpleNamespace(number=number, url=f"https://github.com/{repo_name}/pull/{number}",
                               state="MERGED", head_branch=checkout.branch, title="Merged fix")
    monkeypatch.setattr(github_service, "get_pull", pull)
    await session_pr_state.schedule_session_pr_reconcile("followup", force=True)
    assert lookups == [17]
    entries = sessions.decode_pull_requests(database.get_session("followup")["pull_requests"])
    assert entries[0]["state"] == "merged"


@pytest.mark.parametrize("wording", ["Opus 9.9", "claude-opus-invented", "use Opus 5.5"])
def test_version_names_are_registry_exact_not_fuzzy(mock_db, wording):
    with pytest.raises(ValueError):
        model_override.resolve(wording)


def test_native_name_matching_does_not_authorize_owner_forbidden_fable(mock_db):
    choice = model_override.resolve("FABLE")
    assert choice.model == "fable"  # eligibility is unchanged, not permission
    assert not model_override.memory_names(choice.model, "Never use any Fable model")

@pytest.mark.parametrize("wording, refused", [("claude-opus-5", True), ("Opus 5.5", True),
    ("claude-opus-4-7", True), ("opus[1m]", True), ("OPUS", False)])
@pytest.mark.parametrize("surface", ["task", "swarm", "start"])
@pytest.mark.asyncio
async def test_native_family_only_on_each_orchestration_surface(chats, monkeypatch, wording, refused, surface):
    from fluid.tools.registry import ToolCtx
    from app.core import task_dispatch, session_orchestration, swarm, provider_agent
    from app.core.provider_agent import WorkerOutcome
    from app.core.task_registry import task_registry
    from app.db import sessions
    from tests.core.test_model_override import _turn

    db, seen, prompts, _ = chats
    caller = "caller" if surface == "start" else "parent"
    _turn(caller, "user", f"use {wording} for this work", profile="agent" if surface == "start" else "caller")
    if surface == "start":
        outcome = await session_orchestration.make_handler(lambda: caller)("start", {
            "title": "Review", "prompt": "Review", "request_id": "versioned", "model": wording}, ToolCtx())
        if refused:
            assert outcome.is_error and "family name" in outcome.text and "Choose one of:" in outcome.text
            assert not seen and not prompts
            return
        assert not outcome.is_error, outcome.text
        import json
        child = sessions.get_session(json.loads(outcome.text)["session_id"])
        assert child["model"] == "opus" and child["provider_id"] is None
    elif surface == "task":
        result = await task_dispatch.dispatch_task_tool({
            "agent": "pinned", "prompt": "go", "mode": "sync", "model": wording}, caller_session_id=caller)
        if refused:
            assert not result["ok"] and "family name" in result["error"] and "Choose one of:" in result["error"]
            assert not seen and not prompts
            return
        assert result["ok"], result
        assert seen[0]["model_id"] == "opus" and seen[0]["provider_id"] is None
    else:
        for name in ("coordinators", "workers", "worker_jobs", "_locks"):
            monkeypatch.setattr(swarm, name, {})
        monkeypatch.setattr(swarm, "persist", AsyncMock())
        monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
        monkeypatch.setattr(task_dispatch, "_await_worker_registration", AsyncMock(return_value=True))
        async def worker(**kwargs):
            seen.append(kwargs)
            wid = kwargs["worker_session_id"]
            await provider_agent._notify_caller_terminal(caller, wid, wid, "completed", "done")
            return WorkerOutcome(ok=True, session_id=wid, report="done")
        monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
        ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher(caller, "caller", None)})
        outcome = await swarm.make_handler(lambda: caller)({"action": "start", "tasks": [
            {"id": "review", "agent": "pinned", "prompt": "go", "model": wording}]}, ctx)
        assert not outcome.is_error, outcome.text
        coordinator = swarm.coordinators[caller]
        key = outcome.background_task.task_id
        await coordinator.jobs[key]
        snapshot = coordinator.snapshot(key)
        if refused:
            assert snapshot["status"] == "failed"
            assert "family name" in str(snapshot) and "Choose one of:" in str(snapshot)
            assert not seen
        else:
            assert snapshot["status"] == "completed"
            assert seen[0]["model_id"] == "opus" and seen[0]["provider_id"] is None
        task_registry.clear_session(caller)
    assert not prompts
