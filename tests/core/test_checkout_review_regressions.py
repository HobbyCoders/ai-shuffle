"""Regression coverage for the independent checkout/model review of #1040."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import agent_messaging, model_override, model_registry, remote_task, turn_scheduler, worker_checkout
from app.db import turns
from tests.core.test_backend_followup_regressions import bind_worker
from tests.core.test_worker_checkout import repo, git  # noqa: F401
from tests.core.network.test_remote_task_control import network, started_child, UNRAID_URL
from tests.core.network.conftest import workers  # noqa: F401
from tests.shared_fixtures import foreign_keys_off, pin_runner, clean_task_registry  # noqa: F401


@pytest.mark.parametrize("detached", [False, True])
async def test_live_worker_still_takes_messages_after_its_own_branch_switch(repo, monkeypatch, detached):
    checkout = bind_worker(repo)
    turn = turns.enqueue_turn("followup", "user", prompt="work", profile_id="coder")
    turns.start_turn(turn["id"])
    git(checkout.path, *(["checkout", "--detach"] if detached else ["switch", "-c", "fix/live"]))
    enqueue = AsyncMock(return_value={"id": "steering"})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    monkeypatch.setattr(agent_messaging, "message_provenance_async", AsyncMock(return_value="parent"))
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(get=lambda _: None))
    monkeypatch.setattr("app.core.session_runtime.try_steer_pending", AsyncMock())
    assert await agent_messaging.deliver_message("followup", "parent", "read this") == ("queued", "steering")
    enqueue.assert_awaited_once()
    assert Path(checkout.path).is_dir()


async def test_held_progress_does_not_restore_a_reclaimed_checkout(repo, monkeypatch):
    checkout = bind_worker(repo)
    assert await worker_checkout.reclaim_finished_worker_checkout("followup")
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    monkeypatch.setattr(agent_messaging, "message_provenance_async", AsyncMock(return_value="parent"))
    assert await turn_scheduler.on_agent_message("followup", "parent", "status only", kind="progress") == turn_scheduler.HELD
    assert not Path(checkout.path).exists()
    assert not turns.session_has_unsettled_turns("followup")


@pytest.mark.parametrize("direction", ["child", "main"])
async def test_remote_checkout_refusal_is_definite(mock_db, monkeypatch, direction):
    monkeypatch.setattr(remote_task, "_require_worker_member", lambda _: None)
    monkeypatch.setattr("app.db.remote_tasks.message_answer", lambda _: None)
    refusal = AsyncMock(side_effect=worker_checkout.WorkerCheckoutError("switch back without discarding work"))
    monkeypatch.setattr(remote_task, "_message_" + direction, refusal)
    body = {"version": remote_task.WIRE_VERSION, "to": direction, "worker_session_id": "child", "message_id": "message-1",
            "sender_session_id": "parent", "text": "continue"}
    with pytest.raises(ValueError, match="switch back without discarding work"):
        await remote_task.handle_task_message(body, "peer")


def test_signed_remote_control_returns_definite_checkout_refusal(workers, monkeypatch):
    import asyncio
    from app.network import control
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(turn_scheduler, "on_agent_message", AsyncMock(
        side_effect=worker_checkout.WorkerCheckoutError("switch back without discarding work")))
    body = {"version": remote_task.WIRE_VERSION, "to": "child", "worker_session_id": child,
            "message_id": "checkout-refusal", "sender_session_id": "caller", "text": "continue"}
    with b.active(), pytest.raises(control.ControlRefused, match="switch back without discarding work"):
        asyncio.run(control.call(a.device_id, remote_task.TASK_MESSAGE_ACTION, body,
                                timeout=remote_task.MESSAGE_TIMEOUT))
    assert router.requests(UNRAID_URL).count(("POST", "/api/v1/network/peer/control/task-message")) == 1


async def test_limit_resume_refusal_is_dropped_and_reported(mock_db, monkeypatch):
    from app.core import limit_resume
    from app.core.task_registry import task_registry
    task_registry.start_task("parent", "child", "agent", "work")
    monkeypatch.setattr(turn_scheduler, "on_agent_message", AsyncMock(
        side_effect=worker_checkout.WorkerCheckoutError("switch back without discarding work")))
    record, terminal = AsyncMock(), AsyncMock()
    monkeypatch.setattr(limit_resume, "_record", record)
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    assert await limit_resume._resume_worker("child", "parent", {"limit_key": "limit"}) is None
    assert {call.args[0] for call in record.await_args_list} == {"child", "parent"}
    assert all(call.args[1] == "dropped" and "switch back" in call.args[2]["reason"]
               for call in record.await_args_list)
    terminal.assert_awaited_once()
    assert "switch back" in terminal.await_args.args[1]["summary"]
    assert task_registry.get_task("parent", "child")["status"] == "error"


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("family,version", [("haiku", "4-5"), ("haiku", "5-5"),
                                           ("opus", "5"), ("opus", "5-5"),
                                           ("sonnet", "5"), ("sonnet", "5-5")])
@pytest.mark.parametrize("runs_here", [False, True])
def test_native_version_names_never_substitute(mock_db, monkeypatch, cloud, family, version, runs_here):
    from app.core import claude_auth_mode
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: claude_auth_mode.MODE_CLOUD if cloud else "first_party")
    requested = f"claude-{family}-{version}"
    actual = model_registry.delegated_model_aliases()[family]
    for wording in (requested, family.title() + " " + version.replace("-", ".")):
        if requested == actual:
            assert model_override.resolve(wording, runs_here=runs_here).model == family
        else:
            with pytest.raises(ValueError, match="Choose one of"):
                model_override.resolve(wording, runs_here=runs_here)


@pytest.mark.parametrize("queued", [False, True])
async def test_remote_finisher_uses_admission_lock_and_real_busy_recheck(repo, monkeypatch, queued):
    import asyncio
    checkout = bind_worker(repo)
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    def owe(sid):
        summary = remote_task.git_summary(sid)
        assert summary["head"] == checkout.base_commit
        assert Path(checkout.path).is_dir(), "a git snapshot must not delete a concurrent continuation's checkout"
        loop.call_soon_threadsafe(entered.set)
        return True
    monkeypatch.setattr(remote_task, "owe_if_finished", owe)
    monkeypatch.setattr(remote_task, "_checkout_mode", lambda _: worker_checkout.REMOTE_BRANCH)
    async with turn_scheduler.message_resume_lock("followup"):
        finisher = asyncio.create_task(remote_task._owe_and_reclaim("followup"))
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0)
        assert not finisher.done()
        if queued:
            turns.enqueue_turn("followup", "user", prompt="continue", profile_id="coder")
    assert await finisher
    assert Path(checkout.path).exists() == queued


async def test_stored_pr_url_is_not_looked_up_in_another_repository(repo, monkeypatch):
    from app.core import session_pr_state
    from app.core.github_service import github_service
    from app.db import database, sessions
    checkout = bind_worker(repo)
    git(checkout.path, "switch", "-c", "owner-work")
    sessions.update_session_pr_state("followup", pr_number=17,
        pr_url="https://github.com/previous/repo/pull/17", pr_state="open")
    monkeypatch.setattr(github_service, "get_repo_from_remote", lambda _: "current/repo")
    monkeypatch.setattr(github_service, "list_pulls", lambda *a, **kw: [])
    monkeypatch.setattr(github_service, "get_pull", lambda *a: pytest.fail("foreign stored PR"))
    await session_pr_state.schedule_session_pr_reconcile("followup", force=True)
    assert database.get_session("followup")["pr_url"] == "https://github.com/previous/repo/pull/17"


async def test_execution_preflight_restores_non_task_message_and_user_enqueue(repo):
    from app.core import query_options
    from app.db import database
    checkout = bind_worker(repo)
    assert await worker_checkout.reclaim_finished_worker_checkout("followup")
    assert not Path(checkout.path).exists()
    # MessageSession and the owner's user enqueue share this preflight. It is
    # invoked after a live turn is created, so it must not skip missing folders.
    turn = turns.enqueue_turn("followup", "user", prompt="continue", profile_id="coder")
    turns.start_turn(turn["id"])
    assert await query_options.ensure_pending_checkout(database.get_session("followup"))
    assert Path(checkout.path).is_dir()


async def test_actual_message_queue_restores_after_completed_turn_and_cleanup(repo, monkeypatch):
    checkout = bind_worker(repo)
    first = turns.enqueue_turn("followup", "user", prompt="work", profile_id="coder")
    turns.start_turn(first["id"])
    turns.settle_turn(first["id"], "completed", stop_reason="end_turn")
    assert await worker_checkout.reclaim_finished_worker_checkout("followup")
    assert not Path(checkout.path).exists(), "cleanup must precede the follow-up"
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    monkeypatch.setattr(agent_messaging, "message_provenance_async", AsyncMock(return_value="parent"))
    resumed = await turn_scheduler.on_agent_message("followup", "parent", "continue")
    assert resumed not in (None, turn_scheduler.HELD)
    assert turns.get_turn(resumed)["state"] == "queued"
    assert Path(checkout.path).is_dir()
    assert git(checkout.path, "branch", "--show-current") == checkout.branch


async def test_restore_runs_in_the_dedicated_provisioning_pool(mock_db, monkeypatch):
    import threading
    seen = []
    def restore(sid):
        seen.append(threading.current_thread().name)
        return True, None
    monkeypatch.setattr(worker_checkout, "restore_worker_checkout", restore)
    assert await worker_checkout.restore_worker_checkout_async("child") == (True, None)
    assert seen[0].startswith("task-start")
