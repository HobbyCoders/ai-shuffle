"""ListSessions and MessageSession across workers (#2011): peer chats on another
worker are listed and messaged through the owning worker's control channel."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from fluid.tools.builtins.session_orchestration import SPECS
from fluid.tools.registry import ToolCtx

from app.core import session_orchestration as orch
from app.core.task_registry import task_registry
from app.db import database, turns
from app.db.base import get_db
from app.network import control, engine, transport
from tests.core.network import test_start_session_remote as remote_start
from tests.core.network.harness import converge

# The remote-start harness: owner chats on two workers, "ServerWorker" and "Windows".
agent_turns, network = remote_start.agent_turns, remote_start.network
install_local_principal, kicks = remote_start.install_local_principal, remote_start.kicks


def _chat(worker, session_id, title="ServerWorker chat", **fields):
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


def _serve_reverse(sender, receiver, served):
    """Route the real authenticated HTTP relay in the opposite direction."""
    from app.db import network as store
    served.server = receiver
    with sender.active():
        store.touch_member(receiver.device_id, endpoints=[served.base_url])
    transport._links.pop(receiver.device_id, None)


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
    assert remote["target"]["owner_device_id"] == a.device_id and remote["target"]["worker"] == "ServerWorker"
    assert remote["target"]["title"] == "ServerWorker chat"
    assert "MessageSession relays to them" in result["note"]
    assert ("POST", "/api/v1/network/peer/control/orchestration-peers") in served.requests


async def test_discovery_and_message_before_catalog_replication(workers, monkeypatch, kicks):
    a, b, served = network(workers, monkeypatch)
    _chat(a, "target")  # Published, but deliberately not converged to b.
    with b.active():
        assert database.get_session("target") is None
        listing = await _tool("ListSessions", {"state": "active", "scope": "all"})
        assert not listing.is_error, listing.text
        entries = json.loads(listing.text)["sessions"]
        assert [(e["id"], e["owner_device_id"]) for e in entries] == [("target", a.device_id)]
        result = await _tool("MessageSession", MESSAGE)
        assert not result.is_error, result.text
        assert database.get_session("target") is None  # No fabricated local execution row.
        again = await _tool("MessageSession", MESSAGE)
        assert json.loads(again.text)["turn_id"] == json.loads(result.text)["turn_id"]
    with a.active():
        assert len(agent_turns("target")) == 1
    assert kicks == ["target"]


@pytest.mark.parametrize("fields", [
    {"is_active": False}, {"hidden": True}, {"is_worker": True},
    {"status": "archived"}, {"mode": "voice"}, {"parent_session_id": "caller"},
])
async def test_missing_mirror_discovery_keeps_destination_fences(workers, monkeypatch, kicks, fields):
    a, b, _ = network(workers, monkeypatch)
    _chat(a, "target")
    _update(a, "target", publish=False, **fields)
    with b.active():
        assert database.get_session("target") is None
        listed = await _tool("ListSessions", {"scope": "all"})
        assert json.loads(listed.text)["sessions"] == []
        refused = await _tool("MessageSession", MESSAGE)
        assert refused.is_error
        assert _receipts() == []
    with a.active():
        assert agent_turns("target") == []
    assert kicks == []


async def test_explicit_local_owner_survives_legacy_instance_change(workers, monkeypatch, kicks):
    a, b, _ = _two_workers(workers, monkeypatch)
    _chat(a, "local-caller")
    _update(a, "target", publish=False, instance_id="previous-install")
    _update(a, "local-caller", publish=False, instance_id="previous-install")
    with a.active():
        listing = await _tool("ListSessions", {"scope": "all"}, sender="local-caller")
        assert not listing.is_error, listing.text
        assert "target" in {e["id"] for e in json.loads(listing.text)["sessions"]}
        sent = await _tool("MessageSession", MESSAGE, sender="local-caller")
        assert not sent.is_error, sent.text
    with b.active():
        # Authoritative remote eligibility uses the same explicit owner rule.
        listed = await _tool("ListSessions", {"scope": "all"})
        assert "target" in {e["id"] for e in json.loads(listed.text)["sessions"]}


async def test_remote_reply_credit_is_consumed_once_and_cannot_ping_pong(workers, monkeypatch, kicks):
    a, b, served = _two_workers(workers, monkeypatch)
    with b.active():
        first = await _tool("MessageSession", MESSAGE)
        assert not first.is_error, first.text
    monkeypatch.setattr(orch, "MAX_ACTIONS", 0)
    _serve_reverse(a, b, served)
    args = {"session_id": "caller", "message": "Build completed", "request_id": "reply"}
    with a.active():
        request = agent_turns("target")[0]
        sent = await _tool("MessageSession", args, sender="target")
        assert not sent.is_error, sent.text
        reply_id = json.loads(sent.text)["turn_id"]
        retry = await _tool("MessageSession", args, sender="target")
        assert json.loads(retry.text)["turn_id"] == reply_id
        [receipt] = _receipts()
        assert receipt["payload"]["orchestration_reply_to"] == request["id"]
        extra = await _tool("MessageSession", {**args, "request_id": "extra"}, sender="target")
        assert extra.is_error and "budget" in extra.text
    with b.active():
        [reply] = agent_turns("caller")
        assert reply["id"] == reply_id
        assert reply["payload"]["orchestration_reply_to"] == request["id"]
        ack = await _tool("MessageSession", {**MESSAGE, "request_id": "ack"})
        assert ack.is_error and "budget" in ack.text
    assert kicks == ["target", "caller"]
    assert served.requests.count(("POST", "/api/v1/network/peer/control/orchestration-message")) == 3


async def test_offline_worker_is_named_in_the_listing(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch, online=False)
    call = AsyncMock(side_effect=control.RunnerUnreachable("offline"))
    monkeypatch.setattr(control, "call", call)
    with b.active():
        result = json.loads((await _tool("ListSessions", {})).text)
    assert [entry["id"] for entry in result["sessions"]] == []
    assert "Not listed: chats on ServerWorker (could not be reached)." in result["note"]
    call.assert_awaited_once()


def test_in_process_discovery_does_not_use_wall_clock_budgets(workers, monkeypatch):
    # Eligibility tests exercise signed control + SQL even with stale presence;
    # executor/portal scheduling is not peer liveness. Dedicated deadline tests
    # below re-enable budgets and assert cancellation and receipt semantics.
    network(workers, monkeypatch)
    assert orch.PEERS_TIMEOUT is None
    assert orch.OFFLINE_PEERS_TIMEOUT is None


@pytest.mark.parametrize("stale_fields", [
    {"is_active": False}, {"status": "archived"}, {"hidden": True},
    {"is_worker": True},
])
async def test_owner_eligibility_beats_stale_mirror_and_presence(workers, monkeypatch, kicks, stale_fields):
    a, b, _ = _two_workers(workers, monkeypatch, online=False)
    _update(b, "target", publish=False, **stale_fields)
    with b.active():
        listing = await _tool("ListSessions", {"scope": "all"})
        assert not listing.is_error, listing.text
        assert [e["id"] for e in json.loads(listing.text)["sessions"]] == ["target"]
        transport._last_contact.pop(a.device_id, None)
        sent = await _tool("MessageSession", MESSAGE)
        assert not sent.is_error, sent.text
    with a.active():
        assert len(agent_turns("target")) == 1
    assert kicks == ["target"]


@pytest.mark.parametrize("mirrored", [False, True])
async def test_old_destination_cannot_bypass_carried_owner_fence(workers, monkeypatch, kicks, mirrored):
    from app.core import preference_identity
    a, b, served = network(workers, monkeypatch)
    _chat(a, "target")
    _chat(a, "stranger")
    _update(a, "stranger", provenance_owner="account:other-owner")
    if mirrored:
        converge([a, b])
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "our-owner")
    owner, listing = orch._owner, orch.accept_peer_listing

    def old_owner(row):
        if engine.context().device_id == a.device_id:
            principal = orch._principal(row["id"]) or {}
            return orch._as_owner_chat((row.get("api_user_id"), principal.get("owner", "install-admin")))
        return owner(row)

    def old_listing(body, from_device):
        result = listing(body, from_device)
        result.pop("enforces_owner_labels", None)
        return result

    monkeypatch.setattr(orch, "_owner", old_owner)
    monkeypatch.setattr(orch, "accept_peer_listing", old_listing)
    with b.active():
        result = await _tool("ListSessions", {"scope": "all"})
        assert not result.is_error, result.text
        assert {e["id"] for e in json.loads(result.text)["sessions"]} == ({"target"} if mirrored else set())
        refused = await _tool("MessageSession", {**MESSAGE, "session_id": "stranger"})
        assert refused.is_error and "eligible peer" in refused.text
        assert _receipts() == []
        assert ("POST", "/api/v1/network/peer/control/orchestration-message") not in served.requests
        if mirrored:
            accepted = await _tool("MessageSession", MESSAGE)
            assert not accepted.is_error, accepted.text
    with a.active():
        assert agent_turns("stranger") == []


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("action", ["ListSessions", "MessageSession"])
async def test_slow_unreachable_peer_has_overall_deadline(workers, monkeypatch, kicks, online, action):
    _a, b, _ = _two_workers(workers, monkeypatch, online=online)
    monkeypatch.setattr(orch, "PEERS_TIMEOUT", 0.05)
    monkeypatch.setattr(orch, "OFFLINE_PEERS_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def slow_routes(*args, **kwargs):
        try:
            await asyncio.sleep(2)  # Models multiple routes ignoring per-call timeout.
            raise control.RunnerUnreachable("offline")
        finally:
            cancelled.set()

    monkeypatch.setattr(control, "call", slow_routes)
    with b.active():
        args = {"scope": "all"} if action == "ListSessions" else {**MESSAGE, "session_id": "not-mirrored"}
        result = await asyncio.wait_for(_tool(action, args), timeout=0.5)
        assert cancelled.is_set()
        if action == "ListSessions":
            assert not result.is_error and "timed out" in json.loads(result.text)["note"]
        else:
            assert result.is_error and _receipts() == []


async def test_complete_discovery_including_local_reads_is_bounded(monkeypatch):
    async def slow_lookup(*args):
        await asyncio.sleep(2)
    monkeypatch.setattr(orch, "_discover_peer_from_workers", slow_lookup)
    monkeypatch.setattr(orch, "PEERS_TIMEOUT", 0.01)
    with pytest.raises(ValueError, match="discovery timed out"):
        await asyncio.wait_for(orch._discover_remote_peer("caller", "unknown"), timeout=0.5)


@pytest.mark.parametrize("failure", [control.ControlRefused, control.RunnerUnreachable])
async def test_definite_remote_reply_failure_returns_credit(workers, monkeypatch, kicks, failure):
    a, b, served = _two_workers(workers, monkeypatch)
    with b.active():
        first = await _tool("MessageSession", MESSAGE)
        assert not first.is_error, first.text
    _serve_reverse(a, b, served)
    monkeypatch.setattr(orch, "MAX_ACTIONS", 0)
    args = {"session_id": "caller", "message": "Finished", "request_id": "reply"}
    real_call = control.call
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=failure("not delivered")))
    with a.active():
        refused = await _tool("MessageSession", args, sender="target")
        assert refused.is_error and _receipts() == []
        monkeypatch.setattr(control, "call", real_call)
        sent = await _tool("MessageSession", args, sender="target")
        assert not sent.is_error, sent.text  # Still has its one credit despite MAX_ACTIONS=0.
        assert len(_receipts()) == 1
    with b.active():
        assert len(agent_turns("caller")) == 1


async def test_remote_message_deadline_retains_uncertain_receipt(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch)
    monkeypatch.setattr(orch, "REMOTE_TIMEOUT", 0.01)
    async def slow_send(*args, **kwargs):
        await asyncio.sleep(2)
    monkeypatch.setattr(control, "call", slow_send)
    with b.active():
        result = await asyncio.wait_for(_tool("MessageSession", MESSAGE), timeout=0.5)
        assert result.is_error and "delivery is uncertain" in result.text
        assert len(_receipts()) == 1


async def test_old_sender_without_reply_marker_remains_compatible_and_bounded(workers, monkeypatch, kicks):
    a, b, served = _two_workers(workers, monkeypatch)
    with b.active():
        first = await _tool("MessageSession", MESSAGE)
        assert not first.is_error, first.text
    _serve_reverse(a, b, served)
    credit = orch._reply_credit
    # Model the old sender: all messages charged, no reply marker on the wire.
    def old_credit(cursor, sender_id, target_id):
        return None if engine.context().device_id == a.device_id else credit(cursor, sender_id, target_id)
    monkeypatch.setattr(orch, "_reply_credit", old_credit)
    monkeypatch.setattr(orch, "MAX_ACTIONS", 1)
    args = {"session_id": "caller", "message": "Old peer answer", "request_id": "old-answer"}
    with a.active():
        sent = await _tool("MessageSession", args, sender="target")
        assert not sent.is_error, sent.text
        extra = await _tool("MessageSession", {**args, "request_id": "extra"}, sender="target")
        assert extra.is_error and "budget" in extra.text
    with b.active():
        [answer] = agent_turns("caller")
        assert "orchestration_reply_to" not in answer["payload"]
    served.server = a
    monkeypatch.setattr(orch, "MAX_ACTIONS", 0)
    with b.active():
        response = await _tool("MessageSession", {**MESSAGE, "request_id": "new-answer"})
        assert not response.is_error, response.text
        extra = await _tool("MessageSession", {**MESSAGE, "request_id": "new-extra"})
        assert extra.is_error and "budget" in extra.text


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
        assert result["worker"] == {"device_id": a.device_id, "name": "ServerWorker"}
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
        assert "host-verified same-owner session delegation" in turn["prompt"] and MESSAGE["message"] in turn["prompt"]
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
    call = AsyncMock(side_effect=control.RunnerUnreachable("offline"))
    monkeypatch.setattr(control, "call", call)
    with b.active():
        outcome = await _tool("MessageSession", MESSAGE)
        assert outcome.is_error and "ServerWorker could not be reached" in outcome.text
        assert "not an eligible peer" not in outcome.text
        assert _receipts() == []  # No route reached the peer: no budget consumed.
    call.assert_awaited_once()


async def test_uncertain_delivery_keeps_the_receipt_for_a_retry(workers, monkeypatch, kicks):
    _a, b, _ = _two_workers(workers, monkeypatch)
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.MaybeApplied("response lost")))
    with b.active():
        outcome = await _tool("MessageSession", MESSAGE)
        assert outcome.is_error and "delivery is uncertain" in outcome.text
        assert len(_receipts()) == 1
        # A later no-route retry cannot erase the earlier uncertain send.
        monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.RunnerUnreachable("offline")))
        retry = await _tool("MessageSession", MESSAGE)
        assert retry.is_error and len(_receipts()) == 1


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
            assert outcome.is_error and "ServerWorker refused the message" in outcome.text, outcome.text
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


@pytest.mark.parametrize("invalidate", [None, "membership", "receiver_stop", "receiver_archive",
                                        "receiver_owner", "forwarding_provenance"])
async def test_remote_message_steers_a_busy_receiver_with_current_remote_scope(
    workers, monkeypatch, kicks, invalidate,
):
    from types import SimpleNamespace
    from app.core import session_runtime
    from dataclasses import replace
    from app.network import membership

    a, b, _ = _two_workers(workers, monkeypatch)
    with a.active():
        live = turns.enqueue_turn("target", "user", prompt="working", profile_id="default")
        assert turns.start_turn(live["id"])
        runtime = session_runtime.SessionRuntime(live)
        adapter = SimpleNamespace(steer_peer=AsyncMock(), steer=AsyncMock())
        monkeypatch.setattr(runtime, "_live_adapter", lambda: adapter)
    with b.active():
        result = await _tool("MessageSession", MESSAGE)
        assert not result.is_error, result.text
    with a.active():
        (row,) = agent_turns("target")
        assert row["payload"]["forwarded_by_device"] == b.device_id
        # A replicated caller row is not local wake authority on this worker.
        with pytest.raises(ValueError, match="local"):
            orch._caller("caller")
        if invalidate == "membership":
            ctx = engine.context()
            ctx.members[b.device_id] = replace(ctx.member(b.device_id), status=membership.STATUS_REVOKED)
        elif invalidate == "receiver_stop":
            task_registry.mark_user_stopped("target")
        elif invalidate == "receiver_archive":
            _update(a, "target", publish=False, status="archived")
        elif invalidate == "receiver_owner":
            _api_user(a, user_id="foreign")
            _update(a, "target", publish=False, api_user_id="foreign")
        elif invalidate == "forwarding_provenance":
            payload = {**row["payload"], "forwarded_by_device": "different-worker"}
            with get_db() as conn:
                conn.cursor().execute("UPDATE turns SET payload=%s WHERE id=%s", (json.dumps(payload), row["id"]))
        submitted = await runtime.steer_pending()
        if invalidate:
            assert submitted == 0
            adapter.steer_peer.assert_not_awaited()
        else:
            assert submitted == 1
            adapter.steer_peer.assert_awaited_once_with(
                MESSAGE["message"], sender="caller", id=row["id"], provenance="same-owner",
            )
            assert await runtime.steer_pending() == 0
        adapter.steer.assert_not_awaited()
        assert turns.get_turn(row["id"])["state"] == "queued"  # only a receipt retires it
