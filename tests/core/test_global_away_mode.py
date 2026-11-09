"""Global Away applies only to the owner, never grants a pending approval."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import away_mode, global_away_mode
from app.db import database, interactions
from app.db.base import get_db

QUESTIONS = [{"question": "Track?", "options": [{"label": "Stable"},
              {"label": "Nightly (Recommended)"}]}]


def chat(session_id, **fields):
    database.create_session(session_id=session_id, profile_id="default", project_id=None,
                            title=session_id, is_active=True)
    if fields:
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE sessions SET " + ", ".join(f"{key}=%s" for key in fields) + " WHERE id=%s",
                (*fields.values(), session_id),
            )
    return session_id


@pytest.mark.parametrize("global_on", [False, True])
def test_global_coverage_matrix(global_on):
    database.create_api_user("api", "API user", "hash")
    database.create_automation("auto", "Automation", "manual", {}, "default")
    chat("owner")
    chat("task", is_worker=True, parent_session_id="owner")
    chat("api", api_user_id="api")
    chat("api-task", is_worker=True, parent_session_id="api")
    chat("automation", automation_id="auto")
    chat("voice", mode="voice")
    chat("hidden", hidden=True)
    chat("orphan", is_worker=True)
    chat("nested", is_worker=True, parent_session_id="task")
    database.set_system_setting("global_away_mode", "1" if global_on else "0")
    for session_id in ("owner", "task", "api", "api-task", "automation", "voice", "hidden",
                       "orphan", "nested", "missing"):
        assert away_mode.is_away(session_id) == (global_on and session_id in ("owner", "task"))


def test_foreign_identity_is_not_the_owner():
    chat("foreign")
    with patch("app.core.session_orchestration._principal", return_value={"owner": "paired:foreign"}):
        database.set_system_setting("global_away_mode", "1")
        assert not away_mode.is_away("foreign")


def test_explicit_flags_win_without_global_ownership_reads():
    chat("explicit", away_mode=True)
    chat("task", is_worker=True, parent_session_id="explicit")
    with patch.object(away_mode, "globally_covered", side_effect=AssertionError("unnecessary read")), \
            patch.object(away_mode, "global_away_enabled", side_effect=AssertionError("unnecessary read")):
        assert away_mode.is_away("explicit")
        assert away_mode.is_away("task")
    chat("ordinary")
    with patch.object(away_mode, "globally_covered", side_effect=AssertionError("unnecessary read")):
        assert not away_mode.is_away("ordinary")
    with patch.object(away_mode, "global_away_enabled", side_effect=RuntimeError("DB down")):
        assert not away_mode.is_away("ordinary")
    assert not away_mode.is_away(None)


async def test_flip_preserves_flags_and_owner_back_receipts():
    chat("owner")
    chat("explicit", away_mode=True)
    chat("task", is_worker=True, parent_session_id="owner")
    before = {sid: database.get_session(sid)["away_mode"] for sid in ("owner", "explicit", "task")}
    await global_away_mode.apply(True)
    for sid in ("owner", "task"):
        note = away_mode.turn_note(sid)
        assert note == away_mode.CONTEXT_NOTE
        away_mode.record_context_delivery(sid, note, sid + "-on")
    await global_away_mode.apply(False)
    assert {sid: database.get_session(sid)["away_mode"] for sid in before} == before
    assert away_mode.is_away("explicit")
    for sid in ("owner", "task"):
        assert away_mode.turn_note(sid) == away_mode.OWNER_BACK_NOTE
        away_mode.record_context_delivery(sid, away_mode.OWNER_BACK_NOTE, sid + "-off")
        assert away_mode.turn_note(sid) is None


async def test_turn_on_releases_real_question_and_permission_waiters(monkeypatch):
    from app.core.permission_handler import PermissionHandler
    from app.core.user_question_handler import user_question_handler

    chat("owner")
    question_posted, permission_posted = asyncio.Event(), asyncio.Event()

    from app.core import interaction_wait
    original_wait = interaction_wait.await_interaction

    async def parked(interaction_id, *args, **kwargs):
        kind = interactions.get_interaction(interaction_id)["kind"]
        (question_posted if kind == "question" else permission_posted).set()
        return await original_wait(interaction_id, *args, **kwargs)

    monkeypatch.setattr(interaction_wait, "await_interaction", parked)
    question_broadcast = permission_broadcast = AsyncMock()

    question = asyncio.create_task(user_question_handler.request_answers(
        "q", "tool-q", "owner", QUESTIONS, question_broadcast))
    permission = asyncio.create_task(PermissionHandler().request_permission(
        request_id="p", session_id="owner", profile_id="default", tool_name="Bash",
        tool_input={"command": "pwd"}, broadcast_func=permission_broadcast))
    try:
        await asyncio.wait_for(asyncio.gather(question_posted.wait(), permission_posted.wait()), 3)
        with patch.object(away_mode, "record_decision", AsyncMock()) as record:
            assert await global_away_mode.apply(True) == {
                "away": True, "released_questions": 1, "deferred_approvals": 1}
            answers, denied = await asyncio.wait_for(asyncio.gather(question, permission), 3)
            assert answers == {"Track?": "Nightly (Recommended)"}
            assert denied.behavior == "deny" and denied.message == away_mode.DEFERRED_MESSAGE
            assert {call.args[1]["kind"] for call in record.await_args_list} == {"answered", "deferred"}
            assert await global_away_mode.release_local_pending() == {
                "released_questions": 0, "deferred_approvals": 0}
            assert record.await_count == 2
        assert interactions.get_pending_interactions("owner") == []
        assert database.get_permission_rules(profile_id="default") == []
    finally:
        for task in (question, permission):
            if not task.done():
                task.cancel()
        await asyncio.gather(question, permission, return_exceptions=True)


@pytest.mark.parametrize("tag", [{}, {"provider_agent": {"worker_session_id": "task"}},
    {"provider_agent": {"worker_session_id": "remote", "remote_approval_id": "a"}},
    {"assistant_confirm": {"question": "Run this?"}}, {"tool_name": "ModelGuardrail"}])
async def test_every_permission_card_kind_is_away_denied_once(tag, monkeypatch):
    from app.core import remote_task_approvals

    monkeypatch.setattr(remote_task_approvals, "_ACTIVE_RELAYS", {"a"})
    chat("owner")
    payload = {"request_id": "p", "tool_name": "Bash", **tag}
    row = interactions.create_interaction("owner", "permission", payload, 300)
    with patch.object(away_mode, "record_decision", AsyncMock()) as record:
        assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
        assert (await global_away_mode.apply(True))["deferred_approvals"] == 0
        record.assert_awaited_once()
    answer = interactions.get_interaction(row["id"])["answer"]
    assert answer == {"decision": "deny", "behavior": "deny", "message": away_mode.DEFERRED_MESSAGE}


async def test_sweep_skips_remote_and_excluded_chats_and_off_is_noop():
    for sid, fields in (("owner", {}), ("voice", {"mode": "voice"}),
                        ("hidden", {"hidden": True}), ("remote", {"owner_device_id": "peer"})):
        chat(sid, **fields)
        interactions.create_interaction(sid, "permission", {"request_id": sid}, 300)
    assert (await global_away_mode.apply(False))["deferred_approvals"] == 0
    assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
    for sid in ("voice", "hidden", "remote"):
        assert len(interactions.get_pending_interactions(sid)) == 1


async def test_private_input_and_action_producing_paths_unblock_with_away_message(monkeypatch):
    from app.core import chat_access

    chat("owner")
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, None, "admin", "owner")))
    monkeypatch.setattr(chat_access, "session_bypasses_confirmation", AsyncMock(return_value=False))
    monkeypatch.setattr(broker, "_show_in_main", AsyncMock())
    monkeypatch.setattr(broker, "_track_attention", lambda _: None)
    monkeypatch.setattr(broker, "_notify", lambda *a, **kw: None)
    created = asyncio.Event()
    original = broker._defer_registered_away
    checked = []

    async def checked_away(item):
        await original(item)
        checked.append(item.id)
        if len(checked) == 2:
            created.set()

    monkeypatch.setattr(broker, "_defer_registered_away", checked_away)
    grant = SimpleNamespace(id="g", session_id="owner", expires_at=10**12, owner=(None,))
    request = asyncio.create_task(broker.request("owner", "folder", "Read source"))
    action = asyncio.create_task(broker.approve(grant, "write", {}))
    try:
        await asyncio.wait_for(created.wait(), 3)
        with patch.object(away_mode, "record_decision", AsyncMock()) as record:
            assert (await global_away_mode.apply(True))["deferred_approvals"] == 2
            results = await asyncio.wait_for(asyncio.gather(request, action, return_exceptions=True), 3)
            assert all(isinstance(r, chat_access.AccessDenied) and str(r) == away_mode.DEFERRED_MESSAGE
                       for r in results)
            assert record.await_count == 2
            assert (await global_away_mode.apply(True))["deferred_approvals"] == 0
        assert broker.grants == {}  # Nothing created or widened.
    finally:
        for task in (request, action):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, action, return_exceptions=True)


async def test_manual_requeue_while_away_cannot_park_another_card():
    from app.core.permission_handler import PermissionHandler

    chat("owner")
    row = interactions.create_interaction("owner", "permission", {
        "request_id": "old", "tool_name": "Bash", "tool_input": {},
    }, 300)
    assert interactions.answer_interaction(row["id"], {"decision": "defer"})
    database.set_system_setting("global_away_mode", "1")
    with patch.object(away_mode, "record_decision", AsyncMock()) as record:
        assert await PermissionHandler().requeue_deferred("owner", AsyncMock()) == []
        record.assert_awaited_once()
    assert interactions.get_pending_interactions("owner") == []
    answers = interactions.get_answered_interactions("owner", "permission")
    assert any(r["answer"].get("message") == away_mode.DEFERRED_MESSAGE for r in answers)


def watch_permission_waiter(monkeypatch):
    from app.core import interaction_wait

    posted, ids = asyncio.Event(), []
    original = interaction_wait.await_interaction

    async def waiting(interaction_id, *args, **kwargs):
        ids.append(interaction_id)
        posted.set()
        return await original(interaction_id, *args, **kwargs)

    monkeypatch.setattr(interaction_wait, "await_interaction", waiting)
    return posted, ids


async def test_released_owner_confirm_is_woken_without_an_approval_pass(monkeypatch):
    from app.core import owner_confirm, session_runtime
    from app.core.permission_handler import PermissionHandler

    chat("owner")
    posted, ids = watch_permission_waiter(monkeypatch)
    wake = AsyncMock()
    monkeypatch.setattr(owner_confirm, "_wake", wake)
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock())
    task = asyncio.create_task(PermissionHandler().request_permission(
        request_id="own", session_id="owner", profile_id="default", tool_name="Bash",
        tool_input={"command": "pwd"}, broadcast_func=AsyncMock(),
        assistant_confirm=owner_confirm.marker("Bash", {"command": "pwd"})))
    try:
        await asyncio.wait_for(posted.wait(), 3)
        assert await owner_confirm.park("owner", ids) == 1
        assert (await asyncio.wait_for(task, 3)).message == owner_confirm.NOT_RUN_YET
        with patch.object(away_mode, "record_decision", AsyncMock()):
            assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
            assert (await global_away_mode.apply(True))["deferred_approvals"] == 0
        wake.assert_awaited_once()
        assert away_mode.DEFERRED_MESSAGE in wake.await_args.args[1]
        assert wake.await_args.kwargs["extra"] is None  # No one-shot approval pass.
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_provider_worker_routed_to_owner_gets_away_denial(monkeypatch):
    from collections import OrderedDict
    from app.core import provider_agent_approvals as approvals
    from app.core.permission_handler import PermissionHandler

    chat("owner")
    chat("task", is_worker=True, parent_session_id="owner")
    monkeypatch.setattr(approvals, "_ROUTES", OrderedDict())
    approvals.register_route(worker_session_id="task", parent_session_id="owner", label="Task")
    posted, ids = watch_permission_waiter(monkeypatch)
    task = asyncio.create_task(approvals.route_permission({
        "request_id": "worker", "session_id": "task", "profile_id": "default",
        "tool_name": "Bash", "tool_input": {"command": "pwd"}, "broadcast_func": AsyncMock(),
    }, PermissionHandler().request_permission))
    try:
        await asyncio.wait_for(posted.wait(), 3)
        assert interactions.get_interaction(ids[0])["session_id"] == "owner"
        with patch.object(away_mode, "record_decision", AsyncMock()):
            assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
        result = await asyncio.wait_for(task, 3)
        assert result.behavior == "deny" and result.message == away_mode.DEFERRED_MESSAGE
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("restored", [False, True])
async def test_remote_task_proxy_sends_away_denial_to_the_executing_worker(monkeypatch, restored):
    from app.core import remote_task_approvals as approvals

    monkeypatch.setattr(approvals, "_ACTIVE_RELAYS", {"away-proxy"})
    chat("owner")
    posted, ids = watch_permission_waiter(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(approvals, "_send_answer", send)
    request = approvals._Request(
        approval_id="away-proxy", worker_session_id="remote-child", target_device_id="peer",
        worker_name="Peer", root_session_id="owner", label="Task", asker=None,
        tool_name="Bash", title=None, display_name=None, description=None,
        decision_reason=None, tool_input={"command": "pwd"})
    task = asyncio.create_task(approvals._proxy(request, None))
    try:
        await asyncio.wait_for(posted.wait(), 3)
        if restored:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            row = interactions.get_interaction(ids[0])
            posted.clear()
            task = asyncio.create_task(approvals._follow_existing(request, row))
            await asyncio.wait_for(posted.wait(), 3)
        with patch.object(away_mode, "record_decision", AsyncMock()):
            assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
        await asyncio.wait_for(task, 3)
        send.assert_awaited_once_with(request, "deny", approvals.AWAY_MESSAGE)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def restored_proxy(monkeypatch):
    """A durable remote card and the real restore entry point, with captured sends."""
    from collections import OrderedDict
    from app.core import interaction_wait, remote_task_approvals as approvals
    from app.db import remote_tasks

    chat("owner")
    row = interactions.create_interaction("owner", "permission", {
        "request_id": "live-proxy", "tool_name": "Bash",
        "provider_agent": {"worker_session_id": "remote-child", "remote_approval_id": "live-approval"},
    }, 300)
    monkeypatch.setattr(approvals, "_SEEN", OrderedDict())
    monkeypatch.setattr(approvals, "_ACTIVE_RELAYS", set(), raising=False)
    monkeypatch.setattr(remote_tasks, "tracked_dispatches", lambda: [{
        "root_session_id": "owner", "worker_session_id": "remote-child", "target_device_id": "peer",
    }])
    monkeypatch.setattr(interaction_wait, "_ensure_listener", lambda: None)
    tasks, send = [], AsyncMock()
    monkeypatch.setattr(approvals, "_spawn", lambda coro, name: tasks.append(asyncio.create_task(coro)))
    monkeypatch.setattr(approvals, "_send_answer", send)
    try:
        yield SimpleNamespace(row=row, tasks=tasks, send=send, approvals=approvals)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_restored_relay_retries_transient_read_and_delivers_away_once(monkeypatch, restored_proxy):
    from app.core import interaction_wait

    proxy = restored_proxy
    approvals = proxy.approvals
    original_get = interactions.get_interaction
    failed, resume = asyncio.Event(), asyncio.Event()
    first = True

    def read(interaction_id):
        nonlocal first
        if interaction_id == proxy.row["id"] and first:
            first = False
            raise RuntimeError("temporary interaction read failure")
        return original_get(interaction_id)

    async def retry_sleep(seconds):
        assert 0 < seconds <= approvals.RELAY_WAIT_RETRY_SECONDS
        # The real await_interaction finally must balance both structures before retry.
        assert not interaction_wait.has_waiters("owner")
        assert interaction_wait._blocked.get("owner", 0) == 0
        assert interaction_wait.is_awaited(proxy.row["id"])
        failed.set()
        await resume.wait()

    monkeypatch.setattr(interactions, "get_interaction", read)
    monkeypatch.setattr(approvals, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread, sleep=retry_sleep))
    assert await approvals.restore_proxies() == 1
    await asyncio.wait_for(failed.wait(), 1)
    assert approvals.has_live_relay("live-approval")
    with patch.object(away_mode, "record_decision", AsyncMock()):
        assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
        assert (await global_away_mode.apply(True))["deferred_approvals"] == 0
    # The answered row is no longer discoverable by restoration: this same relay must send it.
    assert await approvals.restore_proxies() == 0
    resume.set()
    await asyncio.wait_for(proxy.tasks[0], 1)
    proxy.send.assert_awaited_once()
    assert proxy.send.await_args.args[1:] == ("deny", approvals.AWAY_MESSAGE)
    assert not approvals.has_live_relay("live-approval")
    assert not interaction_wait.has_waiters("owner")
    assert interaction_wait._blocked.get("owner", 0) == 0
    assert not interaction_wait.is_awaited(proxy.row["id"])


@pytest.mark.parametrize("outcome", ["finished", "cancelled", "exception"])
async def test_relay_liveness_ends_with_its_body(monkeypatch, restored_proxy, outcome):
    proxy = restored_proxy
    approvals = proxy.approvals
    entered, finish = asyncio.Event(), asyncio.Event()

    async def body(request, row):
        entered.set()
        await finish.wait()
        if outcome == "exception":
            raise RuntimeError("unexpected relay failure")

    monkeypatch.setattr(approvals, "_follow_existing", body)
    assert await approvals.restore_proxies() == 1
    # Live even before the collected task has started running.
    assert approvals.has_live_relay("live-approval")
    await asyncio.wait_for(entered.wait(), 1)
    task = proxy.tasks[0]
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        finish.set()
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="unexpected relay failure"):
                await task
        else:
            await task
    assert "live-approval" in approvals._SEEN  # replay history outlives the relay
    assert not approvals.has_live_relay("live-approval")
    proxy.send.assert_not_awaited()
    # Any early end with a still-pending row must be recoverable on the next pass.
    assert interactions.get_interaction(proxy.row["id"])["state"] == "pending"
    assert await approvals.restore_proxies() == 1


async def test_live_relay_survives_replay_eviction_without_duplicate_waiter(monkeypatch, restored_proxy):
    proxy = restored_proxy
    approvals = proxy.approvals
    monkeypatch.setattr(approvals, "_SEEN_LIMIT", 1)
    assert await approvals.restore_proxies() == 1
    approvals._note(approvals._SEEN, "another-approval")
    assert "live-approval" not in approvals._SEEN
    assert approvals.has_live_relay("live-approval")
    assert await approvals.restore_proxies() == 0
    assert len(proxy.tasks) == 1
    with patch.object(away_mode, "record_decision", AsyncMock()):
        assert (await global_away_mode.apply(True))["deferred_approvals"] == 1
    await asyncio.wait_for(proxy.tasks[0], 1)
    proxy.send.assert_awaited_once()
    assert proxy.send.await_args.args[1:] == ("deny", approvals.AWAY_MESSAGE)
    assert not approvals.has_live_relay("live-approval")


async def test_restored_wait_retry_uses_original_deadline(monkeypatch, restored_proxy):
    proxy = restored_proxy
    approvals = proxy.approvals
    # A fake clock avoids sleeping but still forces every retry to recompute time left.
    left, ttls, sleeps = 7.0, [], []

    async def fail_wait(interaction_id, session_id, ttl):
        ttls.append(ttl)
        raise RuntimeError("read still unavailable")

    async def sleep(seconds):
        nonlocal left
        sleeps.append(seconds)
        left -= seconds

    monkeypatch.setattr(approvals, "_seconds_left", lambda expires: left)
    monkeypatch.setattr("app.core.interaction_wait.await_interaction", fail_wait)
    monkeypatch.setattr(approvals, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread, sleep=sleep))
    assert await approvals.restore_proxies() == 1
    await asyncio.wait_for(proxy.tasks[0], 1)
    assert ttls == [7.0, 4.0, 1.0]
    assert sleeps == [3.0, 3.0, 1.0]
    proxy.send.assert_awaited_once()
    assert proxy.send.await_args.args[1:] == ("deny", approvals.TIMED_OUT_MESSAGE)
    assert not approvals.has_live_relay("live-approval")


async def test_cancelled_restored_wait_sends_nothing_and_cleans_up(monkeypatch, restored_proxy):
    from app.core import interaction_wait

    proxy = restored_proxy
    approvals = proxy.approvals
    posted, _ = watch_permission_waiter(monkeypatch)
    assert await approvals.restore_proxies() == 1
    await asyncio.wait_for(posted.wait(), 1)
    assert interaction_wait.has_waiters("owner")
    proxy.tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await proxy.tasks[0]
    proxy.send.assert_not_awaited()
    assert not approvals.has_live_relay("live-approval")
    assert interactions.get_interaction(proxy.row["id"])["state"] == "pending"
    assert not interaction_wait.has_waiters("owner")
    assert interaction_wait._blocked.get("owner", 0) == 0
    assert not interaction_wait.is_awaited(proxy.row["id"])


async def test_failed_spawn_unmarks_relay_and_closes_wrapper(monkeypatch, restored_proxy):
    proxy = restored_proxy
    approvals = proxy.approvals
    collected, marked = [], []

    def fail(coro, name):
        assert approvals.has_live_relay("live-approval")  # marked before _spawn
        marked.append("live-approval" in approvals._ACTIVE_RELAYS)
        collected.append(coro)
        raise RuntimeError("spawn unavailable")

    monkeypatch.setattr(approvals, "_spawn", fail)
    assert await approvals.restore_proxies(report_status=True) == approvals.ProxyRestoration(0, False)
    assert not approvals.has_live_relay("live-approval")
    assert "live-approval" not in approvals._SEEN
    assert len(collected) == 1
    assert marked == [True]
    assert collected[0].cr_frame is None  # closed even though it never ran
    assert interactions.get_interaction(proxy.row["id"])["state"] == "pending"


def test_reset_clears_live_relays(monkeypatch):
    from app.core import remote_task_approvals as approvals

    monkeypatch.setattr(approvals, "_ACTIVE_RELAYS", {"live-approval"}, raising=False)
    approvals.reset()
    assert not approvals._ACTIVE_RELAYS


@pytest.mark.parametrize("enabled", [False, True])
async def test_startup_recovers_parked_cards_after_restoring_remote_waiters(monkeypatch, enabled):
    from collections import OrderedDict
    from app.core import owner_confirm, remote_task, remote_task_approvals as approvals
    from app.db import remote_tasks

    chat("owner")
    question = interactions.create_interaction("owner", "question", {
        "request_id": "boot-question", "questions": QUESTIONS,
    }, 300)
    confirm = interactions.create_interaction("owner", "permission", {
        "request_id": "boot-confirm", "tool_name": "Bash",
        owner_confirm.PAYLOAD_KEY: owner_confirm.marker("Bash", {"command": "pwd"}),
        owner_confirm.RELEASED_KEY: True,
    }, 300)
    proxy = interactions.create_interaction("owner", "permission", {
        "request_id": "boot-proxy", "tool_name": "Bash",
        "provider_agent": {"worker_session_id": "remote-child", "remote_approval_id": "boot-approval"},
    }, 300)
    database.set_system_setting("global_away_mode", "1" if enabled else "0")
    monkeypatch.setattr(approvals, "_SEEN", OrderedDict())
    monkeypatch.setattr(remote_tasks, "tracked_dispatches", lambda: [{
        "root_session_id": "owner", "worker_session_id": "remote-child", "target_device_id": "peer",
    }])
    restored_tasks = []
    monkeypatch.setattr(approvals, "_spawn", lambda coro, name: restored_tasks.append(asyncio.create_task(coro)))
    send, wake = AsyncMock(), AsyncMock()
    monkeypatch.setattr(approvals, "_send_answer", send)
    monkeypatch.setattr(owner_confirm, "_wake", wake)
    order = []
    original_restore, original_release = approvals.restore_proxies, global_away_mode.release_if_enabled

    async def restore(*, report_status):
        assert report_status
        assert order == ["reattach"]
        result = await original_restore(report_status=True)
        assert result == approvals.ProxyRestoration(1, True)
        order.append("restored")
        return result

    async def release(*, strict):
        assert strict
        assert order == ["reattach", "restored"]
        order.append("release")
        await original_release(strict=True)

    async def reattach():
        order.append("reattach")

    monkeypatch.setattr(remote_task, "reattach_all", reattach)
    monkeypatch.setattr(approvals, "restore_proxies", restore)
    monkeypatch.setattr(global_away_mode, "release_if_enabled", release)
    stops, targets = AsyncMock(), AsyncMock()
    monkeypatch.setattr(remote_task, "deliver_stops", stops)
    monkeypatch.setattr(remote_task, "check_targets", targets)
    # A second pass must track normally without repeating successful recovery.
    # Do not alter asyncio for the real restored waiters or prompt machinery.
    monkeypatch.setattr(remote_task, "asyncio", SimpleNamespace(
        sleep=AsyncMock(side_effect=[None, None, asyncio.CancelledError()]), CancelledError=asyncio.CancelledError))
    try:
        with patch.object(away_mode, "record_decision", AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await remote_task.tracking_loop()
            if enabled:
                await asyncio.wait_for(asyncio.gather(*restored_tasks), 3)
        assert order == ["reattach", "restored", "release"]
        assert stops.await_count == targets.await_count == 2
        for row in (question, confirm, proxy):
            current = interactions.get_interaction(row["id"])
            assert current["state"] == ("answered" if enabled else "pending")
        if enabled:
            assert interactions.get_interaction(question["id"])["answer"] == {
                "Track?": "Nightly (Recommended)"}
            for row in (confirm, proxy):
                assert interactions.get_interaction(row["id"])["answer"]["decision"] == "deny"
            assert send.await_args.args[1:] == ("deny", approvals.AWAY_MESSAGE)
            wake.assert_awaited_once()
            assert wake.await_args.kwargs["extra"] is None
        else:
            send.assert_not_awaited()
            wake.assert_not_awaited()
    finally:
        for task in restored_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*restored_tasks, return_exceptions=True)


async def test_startup_away_failure_does_not_stop_background_tracking(monkeypatch, caplog):
    from app.core import remote_task, remote_task_approvals

    monkeypatch.setattr(remote_task, "asyncio", SimpleNamespace(
        sleep=AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()]), CancelledError=asyncio.CancelledError))
    reattach = AsyncMock()
    restore = AsyncMock(return_value=remote_task_approvals.ProxyRestoration(0, True))
    release = AsyncMock(side_effect=[RuntimeError("DB unavailable"), None])
    monkeypatch.setattr(remote_task, "reattach_all", reattach)
    monkeypatch.setattr(remote_task_approvals, "restore_proxies", restore)
    monkeypatch.setattr(global_away_mode, "release_if_enabled", release)
    stops, targets = AsyncMock(), AsyncMock()
    monkeypatch.setattr(remote_task, "deliver_stops", stops)
    monkeypatch.setattr(remote_task, "check_targets", targets)
    with pytest.raises(asyncio.CancelledError):
        await remote_task.tracking_loop()
    reattach.assert_awaited_once()
    assert restore.await_count == release.await_count == 2
    assert stops.await_count == targets.await_count == 3
    assert "Could not release global Away prompts at startup" in caplog.text


async def run_startup_passes(monkeypatch, passes, after_pass):
    """Drive real tracking passes, inspecting durable state between each one."""
    from app.core import remote_task

    completed = 0
    stops, targets, reattach = AsyncMock(), AsyncMock(), AsyncMock()

    async def sleep(_seconds):
        nonlocal completed
        if stops.await_count:
            completed += 1
            assert stops.await_count == targets.await_count == completed
            after_pass(completed)
            if completed == passes:
                raise asyncio.CancelledError()

    monkeypatch.setattr(remote_task, "asyncio", SimpleNamespace(
        sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(remote_task, "reattach_all", reattach)
    monkeypatch.setattr(remote_task, "deliver_stops", stops)
    monkeypatch.setattr(remote_task, "check_targets", targets)
    with pytest.raises(asyncio.CancelledError):
        await remote_task.tracking_loop()
    assert completed == passes
    reattach.assert_awaited_once()


@pytest.mark.parametrize("failure", ["setting", "pending", "chat"])
async def test_startup_retries_failed_sweep_until_clean_then_stops(monkeypatch, failure):
    from app.core import remote_task_approvals as approvals

    chat("owner")
    row = interactions.create_interaction("owner", "question", {
        "request_id": "retry-question", "questions": QUESTIONS,
    }, 300)
    # A broken chat must not hold up the rest of this pass's local sweep.
    chat("z-other")
    other = interactions.create_interaction("z-other", "question", {
        "request_id": "other-question", "questions": QUESTIONS,
    }, 300)
    database.set_system_setting("global_away_mode", "1")
    module, name = {
        "setting": (away_mode, "global_away_enabled"),
        "pending": (global_away_mode, "_pending_sessions"),
        "chat": (away_mode, "release_pending_questions"),
    }[failure]
    original = getattr(module, name)
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("temporary read failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, fail_once)
    restore = AsyncMock(wraps=approvals.restore_proxies)
    sweep = AsyncMock(wraps=global_away_mode.release_if_enabled)
    monkeypatch.setattr(approvals, "restore_proxies", restore)
    monkeypatch.setattr(global_away_mode, "release_if_enabled", sweep)

    def check_pass(number):
        assert interactions.get_interaction(row["id"])["state"] == (
            "pending" if number == 1 else "answered")
        if number == 1 and failure == "chat":
            assert interactions.get_interaction(other["id"])["state"] == "answered"

    await run_startup_passes(monkeypatch, 3, check_pass)
    assert failed
    assert restore.await_count == sweep.await_count == 2
    assert interactions.get_interaction(row["id"])["answer"] == {
        "Track?": "Nightly (Recommended)"}


@pytest.mark.parametrize("failure", ["dispatches", "proxy_rows", "spawn"])
async def test_incomplete_relay_restore_keeps_proxy_pending_but_releases_local_questions(monkeypatch, failure):
    from collections import OrderedDict
    from app.core import remote_task_approvals as approvals
    from app.db import remote_tasks

    chat("owner")
    question = interactions.create_interaction("owner", "question", {
        "request_id": "local-question", "questions": QUESTIONS,
    }, 300)
    proxy = interactions.create_interaction("owner", "permission", {
        "request_id": "retry-proxy", "tool_name": "Bash",
        "provider_agent": {"worker_session_id": "remote-child", "remote_approval_id": "retry-approval"},
    }, 300)
    database.set_system_setting("global_away_mode", "1")
    monkeypatch.setattr(approvals, "_SEEN", OrderedDict())
    dispatches = [{"root_session_id": "owner", "worker_session_id": "remote-child", "target_device_id": "peer"}]
    monkeypatch.setattr(remote_tasks, "tracked_dispatches", lambda: dispatches)
    tasks = []
    monkeypatch.setattr(approvals, "_spawn", lambda coro, name: tasks.append(asyncio.create_task(coro)))
    module, name = {"dispatches": (remote_tasks, "tracked_dispatches"),
                    "proxy_rows": (approvals, "_proxy_rows"), "spawn": (approvals, "_spawn")}[failure]
    original = getattr(module, name)
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("temporary relay failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, fail_once)
    send = AsyncMock()
    monkeypatch.setattr(approvals, "_send_answer", send)
    restore = AsyncMock(wraps=approvals.restore_proxies)
    sweep = AsyncMock(wraps=global_away_mode.release_if_enabled)
    monkeypatch.setattr(approvals, "restore_proxies", restore)
    monkeypatch.setattr(global_away_mode, "release_if_enabled", sweep)

    def check_pass(number):
        assert interactions.get_interaction(question["id"])["state"] == "answered"
        current = interactions.get_interaction(proxy["id"])
        assert current["state"] == ("pending" if number == 1 else "answered")
        if number == 1:
            assert current["answer"] is None
            assert not approvals.has_live_relay("retry-approval")
            send.assert_not_awaited()
        else:
            assert current["answer"]["message"] == away_mode.DEFERRED_MESSAGE

    try:
        with patch.object(away_mode, "record_decision", AsyncMock()):
            await run_startup_passes(monkeypatch, 3, check_pass)
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert failed
        assert restore.await_count == sweep.await_count == 2
        send.assert_awaited_once()
        assert send.await_args.args[0].worker_session_id == "remote-child"
        assert send.await_args.args[1:] == ("deny", approvals.AWAY_MESSAGE)
        # Existing count-only callers keep the original contract.
        assert await approvals.restore_proxies() == 0
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_untracked_remote_proxy_is_skipped_without_retrying_recovery_forever(monkeypatch):
    from collections import OrderedDict
    from app.core import remote_task_approvals as approvals
    from app.db import remote_tasks

    chat("owner")
    row = interactions.create_interaction("owner", "permission", {
        "request_id": "orphan-proxy", "tool_name": "Bash",
        "provider_agent": {"worker_session_id": "untracked", "remote_approval_id": "orphan"},
    }, 300)
    monkeypatch.setattr(approvals, "_SEEN", OrderedDict())
    monkeypatch.setattr(remote_tasks, "tracked_dispatches", lambda: [])
    # A toggle can arrive before startup has even attempted relay restoration.
    assert await global_away_mode.apply(True) == {
        "away": True, "released_questions": 0, "deferred_approvals": 0}
    restore = AsyncMock(wraps=approvals.restore_proxies)
    sweep = AsyncMock(wraps=global_away_mode.release_if_enabled)
    monkeypatch.setattr(approvals, "restore_proxies", restore)
    monkeypatch.setattr(global_away_mode, "release_if_enabled", sweep)

    def check_pass(_number):
        current = interactions.get_interaction(row["id"])
        assert current["state"] == "pending"
        assert current["answer"] is None

    await run_startup_passes(monkeypatch, 3, check_pass)
    restore.assert_awaited_once_with(report_status=True)
    sweep.assert_awaited_once_with(strict=True)


@pytest.mark.parametrize("takeover", ["adopt_session", "adopt_session_follow", "unpin_session", "pin_session_placement"])
async def test_legacy_takeover_recovers_away_questions_after_commit(monkeypatch, takeover):
    from app.core.config import settings
    from app.core.detached_tasks import _DETACHED_TASKS
    from app.db import sessions

    chat("incoming", owner_device_id=None, instance_id="old-install")
    row = interactions.create_interaction("incoming", "question", {
        "request_id": "legacy-question", "questions": QUESTIONS,
    }, 300)
    database.set_system_setting("global_away_mode", "1")
    assert (await global_away_mode.release_local_pending())["released_questions"] == 0
    completed = asyncio.Event()
    original = global_away_mode.release_session_pending

    async def release(sid):
        assert database.get_session(sid)["instance_id"] == settings.effective_instance_id
        await original(sid)
        completed.set()

    monkeypatch.setattr(global_away_mode, "release_session_pending", release)
    with patch.object(away_mode, "record_decision", AsyncMock()):
        getattr(sessions, takeover)("incoming", settings.effective_instance_id)
        await asyncio.wait_for(completed.wait(), 3)
        tasks = [t for t in _DETACHED_TASKS if t.get_name() == "global-away-incoming"]
        await asyncio.gather(*tasks)
    assert interactions.get_interaction(row["id"])["state"] == "answered"


@pytest.fixture
def api_client():
    from app.api import system

    app = FastAPI()
    app.include_router(system.router)
    return TestClient(app)


def test_api_owner_contract_strict_body_and_notifications(api_client):
    with patch("app.api.auth.owner_auth.local_access", return_value=True), \
            patch("app.api.auth.owner_auth.same_origin"), \
            patch("app.core.global_events.global_events.resources_changed") as emit:
        assert api_client.get("/api/v1/away-mode").json() == {"away": False}
        for value in ("true", 1, None):
            assert api_client.put("/api/v1/away-mode", json={"away": value}).status_code == 422
        assert api_client.put("/api/v1/away-mode", json={"away": True, "extra": True}).status_code == 422
        result = api_client.put("/api/v1/away-mode", json={"away": True})
        assert result.status_code == 200
        assert result.json() == {"away": True, "released_questions": 0, "deferred_approvals": 0}
        assert api_client.get("/api/v1/away-mode").json() == {"away": True}
        away_events = [c for c in emit.call_args_list if c.args == ("away_mode",)]
        assert {c.kwargs["user_type"] for c in away_events} == {"admin", "api_user", "account", "paired"}


@pytest.mark.parametrize("method", ["get", "put"])
def test_api_refuses_api_key_users(api_client, method):
    with patch("app.api.auth.owner_auth.local_access", return_value=False), \
            patch("app.api.auth.owner_auth.owner_session", return_value=None), \
            patch("app.api.auth.get_paired_client_from_token", return_value=None):
        kwargs = {"json": {"away": True}} if method == "put" else {}
        result = getattr(api_client, method)("/api/v1/away-mode", headers={"X-API-Key": "api-key"}, **kwargs)
    assert result.status_code == 403
    assert not away_mode.global_away_enabled()


def test_preview_global_toggle_is_disposable_only():
    from starlette.requests import Request
    from app.api.auth import _preview_may_change_dev_data
    from app.core.preview_access import mutation_allowed

    request = Request({"type": "http", "method": "PUT", "path": "/api/v1/away-mode", "headers": []})
    assert mutation_allowed("/api/v1/away-mode", "PUT")
    for disposable in (True, False):
        with patch("app.preview_run.disposable_preview_data", return_value=disposable):
            assert _preview_may_change_dev_data(request) == disposable


@pytest.mark.parametrize("on", [True, False])
async def test_synced_setting_invalidates_cache_notifies_and_only_releases_on(on):
    from app.core.global_events import global_events
    from app.network import startup

    # Cache old state, then mimic a raw sync apply that bypassed the local setter.
    database.set_system_setting("global_away_mode", "0" if on else "1")
    assert away_mode.global_away_enabled() == (not on)
    with get_db() as conn:
        conn.cursor().execute("UPDATE system_settings SET value=%s WHERE key='global_away_mode'",
                              ("1" if on else "0",))
    scheduled = []
    with patch.object(global_events, "_loop", asyncio.get_running_loop()), \
            patch("app.network.startup.asyncio.run_coroutine_threadsafe",
                  side_effect=lambda coro, loop: scheduled.append(coro)), \
            patch.object(startup, "_emit_resource") as emit, \
            patch.object(global_away_mode, "release_local_pending", AsyncMock()) as release:
        startup._on_applied([("system_settings", "global_away_mode", "upsert")])
        assert away_mode.global_away_enabled() == on
        emit.assert_any_call("away_mode")
        assert len(scheduled) == 1
        await scheduled[0]
        assert release.await_count == int(on)
