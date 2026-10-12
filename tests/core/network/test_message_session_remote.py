"""ListSessions and MessageSession across workers (#2011): peer chats on another
worker are listed and messaged through the owning worker's control channel."""
import json
from unittest.mock import AsyncMock

from fluid.tools.builtins.session_orchestration import SPECS
from fluid.tools.registry import ToolCtx

from app.core import session_orchestration as orch
from app.core.task_registry import task_registry
from app.db import database, turns
from app.db.base import get_db
from app.network import control, engine, transport
from tests.core.network import test_start_session_remote as remote_start
from tests.core.network.harness import converge

# The remote-start harness: owner chats on two workers, "Unraid" and "Windows".
agent_turns, network = remote_start.agent_turns, remote_start.network
install_local_principal, kicks = remote_start.install_local_principal, remote_start.kicks


def _chat(worker, session_id, title="Unraid chat", **fields):
    """An open owner chat on ``worker``, published to the other workers."""
    with worker.active():
        database.create_session(session_id=session_id, profile_id="default", project_id=None,
                                title=title, is_active=True, model="sonnet", **fields)
        turn = turns.enqueue_turn(session_id, "user", prompt="work", profile_id="default")
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
        engine.scan_all()


def _api_user(*workers, user_id):
    for worker in workers:
        with worker.active():
            if database.get_api_user(user_id) is None:
                database.create_api_user(user_id, "Key user", (user_id * 64)[:64])
                engine.scan_all()


def _update(worker, session_id, publish=True, **fields):
    with worker.active():
        sets = ", ".join(f"{key}=%s" for key in fields)
        with get_db() as conn:
            conn.cursor().execute(f"UPDATE sessions SET {sets} WHERE id=%s", (*fields.values(), session_id))
        if publish:
            engine.scan_all()


async def _tool(name, args, sender="caller"):
    _, handler = next((s, h) for s, h in SPECS if s.name == name)
    return await handler(args, ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: sender)}))


def _receipts():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id LIKE %s", (orch.REMOTE_MESSAGE_PREFIX + "%",))
        return [turns._row(r) for r in cursor.fetchall()]


MESSAGE = {"session_id": "target", "message": "Can you rerun the Windows build?", "request_id": "ask-1"}


def _two_workers(workers, monkeypatch, *, online=True):
    a, b, served = network(workers, monkeypatch)
    _chat(a, "target")
    converge([a, b])
    if not online:
        transport._last_contact.pop(a.device_id, None)
    return a, b, served


async def test_lists_active_chats_on_another_online_worker(workers, monkeypatch, kicks):
    a, b, served = network(workers, monkeypatch)
    _chat(a, "target")
    _chat(a, "stopped-chat", title="Stopped there")
    _chat(a, "archived-chat", title="Archived there")
    _chat(a, "settled-chat", title="Settled there")
    _update(a, "archived-chat", status="archived")
    _update(a, "settled-chat", is_active=False)
    converge([a, b])
    # Only the owning worker knows a chat was stopped (an in-memory latch).
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "stopped-chat")
    with b.active():
        outcome = await _tool("ListSessions", {"scope": "all"})
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    remote = {entry["id"]: entry for entry in result["sessions"] if entry.get("worker")}
    assert set(remote) == {"target"}
    assert remote["target"]["owner_device_id"] == a.device_id and remote["target"]["worker"] == "Unraid"
    assert remote["target"]["title"] == "Unraid chat"
    assert "MessageSession relays to them" in result["note"]
    assert ("POST", "/api/v1/network/peer/control/orchestration-peers") in served.requests


async def test_offline_worker_is_named_in_the_listing(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch, online=False)
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=AssertionError("no remote call")))
    with b.active():
        result = json.loads((await _tool("ListSessions", {})).text)
    assert [entry["id"] for entry in result["sessions"]] == []
    assert "Not listed: chats on Unraid (offline)." in result["note"]


async def test_other_states_do_not_ask_other_workers(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch)
    call = AsyncMock(side_effect=AssertionError("no remote call"))
    monkeypatch.setattr(control, "call", call)
    with b.active():
        outcome = await _tool("ListSessions", {"state": "all", "scope": "all"})
    assert not outcome.is_error, outcome.text
    call.assert_not_called()


async def test_message_is_relayed_to_the_owning_worker_once(workers, monkeypatch, kicks):
    a, b, served = _two_workers(workers, monkeypatch)
    with b.active():
        outcome = await _tool("MessageSession", MESSAGE)
        assert not outcome.is_error, outcome.text
        result = json.loads(outcome.text)
        assert result["session_id"] == "target" and result["status"] == "queued"
        assert result["delivery_status"] == "queued" and result["created"] is False
        assert result["worker"] == {"device_id": a.device_id, "name": "Unraid"}
        # Caller-side accounting only: settled at birth, never runnable.
        [receipt] = _receipts()
        assert receipt["state"] == "completed" and receipt["command_id"].startswith("orchestration:message:")
    with a.active():
        [turn] = agent_turns("target")
        assert turn["state"] == "queued" and turn["id"] == result["turn_id"]
        payload = turn["payload"]
        assert payload["peer_message_content"] == MESSAGE["message"]
        assert payload["peer_sender_label"] == "Windows chat (on Windows)"
        assert payload["orchestration_sender"] == "caller"
        assert payload["orchestration_remote_origin"] == b.device_id
        assert payload["orchestration_depth"] == 1 and "orchestration_root" not in payload
        assert "another top-level session" in turn["prompt"] and MESSAGE["message"] in turn["prompt"]
    assert kicks == ["target"]
    assert ("POST", "/api/v1/network/peer/control/orchestration-message") in served.requests

    # A retry reads the same row back and never queues it twice.
    with b.active():
        again = json.loads((await _tool("MessageSession", MESSAGE)).text)
        assert again["turn_id"] == result["turn_id"] and again["status"] == "already_queued"
        assert len(_receipts()) == 1
    with a.active():
        assert len(agent_turns("target")) == 1
    assert kicks == ["target"]
    with b.active():
        changed = await _tool("MessageSession", {**MESSAGE, "message": "Something else"})
    assert changed.is_error and "different arguments" in changed.text


async def test_relay_returns_the_queued_shape(workers, monkeypatch, kicks):
    a, b, _ = _two_workers(workers, monkeypatch)
    call = AsyncMock(return_value={"session_id": "target", "turn_id": "turn-1", "status": "queued",
                                   "delivery_status": "queued", "created": True})
    monkeypatch.setattr(control, "call", call)
    with b.active():
        result = json.loads((await _tool("MessageSession", MESSAGE)).text)
    assert {k: result[k] for k in ("session_id", "turn_id", "status", "delivery_status", "url")} == {
        "session_id": "target", "turn_id": "turn-1", "status": "queued", "delivery_status": "queued",
        "url": "/?session=target"}
    assert "Queued is not delivered" in result["note"]
    device, action, body = call.await_args.args
    assert device == a.device_id and action == orch.MESSAGE_ACTION
    assert body["session_id"] == "target" and body["message"] == MESSAGE["message"]
    assert body["sender_session_id"] == "caller" and body["depth"] == 1
    assert kicks == []  # The owning worker wakes its own chat.


async def test_offline_owner_is_named_in_the_refusal(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch, online=False)
    call = AsyncMock(side_effect=AssertionError("no remote call"))
    monkeypatch.setattr(control, "call", call)
    with b.active():
        outcome = await _tool("MessageSession", MESSAGE)
        assert outcome.is_error and "Session target runs on Unraid, which is offline" in outcome.text
        assert "not an eligible peer" not in outcome.text and _receipts() == []
    call.assert_not_called()


async def test_unreachable_owner_keeps_the_receipt_for_a_retry(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch)
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.RunnerUnreachable("timed out")))
    with b.active():
        outcome = await _tool("MessageSession", MESSAGE)
        assert outcome.is_error and "Unraid could not be reached" in outcome.text
        assert len(_receipts()) == 1


async def test_owning_worker_applies_its_own_peer_checks(workers, monkeypatch, kicks):
    a, b, _ = _two_workers(workers, monkeypatch)
    _api_user(a, user_id="someone-else")
    # Changes the owning worker has not published yet: its own row decides.
    cases = [
        ({"status": "archived"}, "archived"),
        ({"api_user_id": "someone-else"}, "not an eligible peer"),
    ]
    for number, (fields, expected) in enumerate(cases):
        _update(a, "target", publish=False, **fields)
        with b.active():
            outcome = await _tool("MessageSession", {**MESSAGE, "request_id": f"check-{number}"})
            assert outcome.is_error and "Unraid refused the message" in outcome.text, outcome.text
            assert expected in outcome.text, outcome.text
            # Nothing was queued, so the reservation is given back.
            assert _receipts() == []
        _update(a, "target", publish=False, status="active", api_user_id=None)
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "target")
    with b.active():
        outcome = await _tool("MessageSession", {**MESSAGE, "request_id": "stopped"})
    assert outcome.is_error and "stopped by the user" in outcome.text
    with a.active():
        assert agent_turns("target") == []
    assert kicks == []


async def test_chats_started_from_the_owners_account_are_peers(workers, monkeypatch, kicks):
    # The owner starts a chat from a signed-in phone or browser: the worker
    # that runs it labels it with the owner's account and keeps no record of
    # its own. Other workers see only the label (#2218).
    from app.core import preference_identity
    a, b, served = network(workers, monkeypatch)
    _chat(a, "target")
    _chat(a, "stranger", title="Someone else's")
    _update(a, "target", provenance_owner="account:owner-1")
    _update(a, "stranger", provenance_owner="account:other-1")
    converge([a, b])
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "owner-1")
    with b.active():
        result = json.loads((await _tool("ListSessions", {"scope": "all"})).text)
        assert {e["id"] for e in result["sessions"] if e.get("worker")} == {"target"}
        outcome = await _tool("MessageSession", MESSAGE)
        assert not outcome.is_error, outcome.text
        refused = await _tool("MessageSession", {**MESSAGE, "session_id": "stranger", "request_id": "x"})
        assert refused.is_error and "not an eligible peer" in refused.text
    with a.active():
        [turn] = agent_turns("target")
        assert turn["payload"]["peer_message_content"] == MESSAGE["message"]

    # No owner account readable: the label proves nothing, as before.
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: None)
    with b.active():
        outcome = await _tool("MessageSession", {**MESSAGE, "request_id": "ask-2"})
    assert outcome.is_error and "not an eligible peer" in outcome.text


async def test_only_owner_chats_message_another_worker(workers, monkeypatch, kicks):
    a, b, _ = _two_workers(workers, monkeypatch)
    _api_user(a, b, user_id="user-1")
    _update(a, "target", api_user_id="user-1")
    _update(b, "caller", api_user_id="user-1")
    converge([a, b])
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=AssertionError("no remote call")))
    with b.active():
        outcome = await _tool("MessageSession", MESSAGE)
    assert outcome.is_error and "Only the owner's chats can message chats on another worker" in outcome.text
