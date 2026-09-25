"""ConnectorManager: start/claim rules, start_connector gates, event fan-out
and relay, trigger routing, and the inbound message -> queued turn -> reply path.

tests/test_connector_manager.py covers the accessors and encryption helpers;
this file covers the lifecycle and routing logic around them. The database
layer, network ownership, the turn queue and the automation/webhook sinks are
replaced per test, so nothing here touches a real connector platform.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.connectors import manager as manager_module
from app.core.connectors.base import BaseConnector, ConnectorCapabilities, ConnectorStatus, IncomingMessage
from app.core.connectors.manager import (
    CONNECTOR_NEEDS_API_USER_MESSAGE,
    CONNECTOR_NEEDS_PROFILE_MESSAGE,
    ConnectorManager,
)
from app.db import database
from app.network import ownership

ME = "runner-me"


class FakeConnector(BaseConnector):
    CAPABILITIES = ConnectorCapabilities(typing_indicator=True, reactions=True)
    MAX_MESSAGE_LENGTH = 2000
    connect_result = True
    connect_error = None
    instances = []

    def __init__(self, connector_id, connector_type, config, on_incoming_message,
                 on_permission_response, on_trigger=None):
        super().__init__(connector_id, connector_type, config, on_incoming_message,
                         on_permission_response, on_trigger=on_trigger)
        self.sent = []
        self.notifications = []
        self.notify_ok = True
        self.disconnect = AsyncMock()
        self.send_ack_reaction = AsyncMock()
        self.mark_as_read = AsyncMock()
        self.send_typing_indicator = AsyncMock()
        FakeConnector.instances.append(self)

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self._status = ConnectorStatus.CONNECTED
        return self.connect_result

    async def disconnect(self):  # replaced per instance
        pass

    async def send_message(self, message):
        self.sent.append(message)
        return True

    async def send_permission_prompt(self, platform_chat_id, prompt):
        return False

    async def send_notification(self, platform_chat_id, event_type, data):
        self.notifications.append((platform_chat_id, event_type, data))
        return self.notify_ok

    async def test_connection(self):
        return {"success": True}

    def get_context_instructions(self, platform_chat_id):
        return f"USE THE FAKE TOOLS in {platform_chat_id}"


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeConnector.instances = []
    FakeConnector.connect_result = True
    FakeConnector.connect_error = None
    yield


@pytest.fixture
def mgr():
    return ConnectorManager()


@pytest.fixture
def me(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "effective_instance_id", property(lambda self: ME))
    return ME


@pytest.fixture
def db(monkeypatch):
    """Replace every database call the manager makes with a recording MagicMock."""
    fakes = SimpleNamespace(
        get_active_connectors=MagicMock(return_value=[]),
        claim_connector_runner=MagicMock(return_value=True),
        get_connector=MagicMock(return_value=None),
        update_connector_status=MagicMock(),
        get_connectors_for_event=MagicMock(return_value=[]),
        get_connector_sessions_for_ai_session=MagicMock(return_value=[]),
        create_connector_event_log=MagicMock(),
        update_connector_event_dispatched=MagicMock(),
        get_api_user=MagicMock(return_value=None),
        get_connector_session=MagicMock(return_value=None),
        create_connector_session=MagicMock(),
        create_session=MagicMock(),
        update_connector_session_ai_session=MagicMock(),
    )
    for name, fn in vars(fakes).items():
        monkeypatch.setattr(database, name, fn)
    return fakes


@pytest.fixture
def not_enrolled(monkeypatch):
    monkeypatch.setattr(ownership, "enrolled", lambda: False)
    monkeypatch.setattr(ownership, "decide_connector", lambda row: None)


def connector_row(cid="c1", ctype="fake", config=None, **extra):
    return {"id": cid, "type": ctype, "name": f"{ctype} bot", "config": config or {},
            "is_active": True, "runner_id": ME, "events": ["session.complete"], **extra}


def statuses(db, cid="c1"):
    return [c.args[1] for c in db.update_connector_status.call_args_list if c.args[0] == cid]


# ---------------------------------------------------------------------------
# start_all: ownership / claim rules
# ---------------------------------------------------------------------------

class TestStartAll:
    async def test_legacy_runner_rules(self, mgr, db, me, not_enrolled, monkeypatch):
        db.get_active_connectors.return_value = [
            {"id": "mine", "runner_id": ME},
            {"id": "unowned-win", "runner_id": None},
            {"id": "unowned-lose", "runner_id": None},
            {"id": "theirs", "runner_id": "runner-other"},
        ]
        db.claim_connector_runner.side_effect = lambda cid, runner: cid == "unowned-win"
        start = AsyncMock(return_value=True)
        monkeypatch.setattr(mgr, "start_connector", start)
        await mgr.start_all()
        assert [c.args[0] for c in start.await_args_list] == ["mine", "unowned-win"]
        assert [c.args for c in db.claim_connector_runner.call_args_list] == [("unowned-win", ME), ("unowned-lose", ME)]

    async def test_network_rules(self, mgr, db, me, monkeypatch):
        db.get_active_connectors.return_value = [
            {"id": "start"}, {"id": "claim-win"}, {"id": "claim-lose"}, {"id": "skip"},
        ]
        rule = {"start": "start", "claim-win": "claim", "claim-lose": "claim", "skip": "skip"}
        monkeypatch.setattr(ownership, "decide_connector", lambda row: rule[row["id"]])
        monkeypatch.setattr(ownership, "claim", lambda table, cid: cid == "claim-win")
        monkeypatch.setattr(ownership, "my_device_id", lambda: "dev-1")
        start = AsyncMock(return_value=True)
        monkeypatch.setattr(mgr, "start_connector", start)
        await mgr.start_all()
        assert [c.args[0] for c in start.await_args_list] == ["start", "claim-win"]
        db.claim_connector_runner.assert_not_called()

    async def test_one_failure_does_not_stop_the_rest(self, mgr, db, me, not_enrolled, monkeypatch):
        db.get_active_connectors.return_value = [{"id": "a", "runner_id": ME}, {"id": "b", "runner_id": ME}]
        start = AsyncMock(side_effect=[RuntimeError("boom"), True])
        monkeypatch.setattr(mgr, "start_connector", start)
        await mgr.start_all()
        assert start.await_count == 2


# ---------------------------------------------------------------------------
# start_connector gates
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(manager_module, "get_connector_class", lambda ctype: FakeConnector)


class TestStartConnector:
    async def test_happy_path_registers_instance(self, mgr, db, not_enrolled, fake_registry):
        db.get_connector.return_value = connector_row(config={"token": "t"})
        assert await mgr.start_connector("c1") is True
        inst = mgr.get_running_instance("c1")
        assert isinstance(inst, FakeConnector) and inst.config == {"token": "t"}
        assert statuses(db) == ["connecting"]  # "connected" is stamped by the client itself
        # Callbacks are wired back to the manager.
        assert inst._on_trigger == mgr._handle_trigger
        assert inst._on_permission_response == mgr.handle_permission_response

    async def test_ownership_skip_or_lost_claim(self, mgr, db, fake_registry, monkeypatch):
        db.get_connector.return_value = connector_row()
        monkeypatch.setattr(ownership, "decide_connector", lambda row: "skip")
        assert await mgr.start_connector("c1") is False
        monkeypatch.setattr(ownership, "decide_connector", lambda row: "claim")
        monkeypatch.setattr(ownership, "claim", lambda table, cid: False)
        assert await mgr.start_connector("c1") is False
        assert FakeConnector.instances == []

    async def test_ownership_failure_fails_closed(self, mgr, db, fake_registry, monkeypatch):
        db.get_connector.return_value = connector_row()

        def boom(row):
            raise RuntimeError("network db down")

        monkeypatch.setattr(ownership, "decide_connector", boom)
        assert await mgr.start_connector("c1") is False
        assert FakeConnector.instances == []

    async def test_unknown_type_stamps_error(self, mgr, db, not_enrolled, monkeypatch):
        db.get_connector.return_value = connector_row(ctype="carrier_pigeon")

        def unknown(ctype):
            raise ValueError(f"Unknown connector type: {ctype}")

        monkeypatch.setattr(manager_module, "get_connector_class", unknown)
        assert await mgr.start_connector("c1") is False
        db.update_connector_status.assert_called_once_with("c1", "error", error="Unknown connector type: carrier_pigeon")

    async def test_chat_connector_without_allowlist_does_not_start(self, mgr, db, not_enrolled, fake_registry):
        from app.core.connector_service import ALLOWLIST_REQUIRED_MESSAGE

        db.get_connector.return_value = connector_row(ctype="telegram", config={"bot_token": "x", "allowed_chat_ids": " , "})
        assert await mgr.start_connector("c1") is False
        assert FakeConnector.instances == []
        db.update_connector_status.assert_called_once_with(
            "c1", "error", status_message=ALLOWLIST_REQUIRED_MESSAGE, error=ALLOWLIST_REQUIRED_MESSAGE,
        )

    async def test_chat_connector_with_allowlist_starts(self, mgr, db, not_enrolled, fake_registry):
        db.get_connector.return_value = connector_row(ctype="telegram", config={"allowed_chat_ids": "111, 222"})
        assert await mgr.start_connector("c1") is True

    async def test_connect_raises_or_returns_false(self, mgr, db, not_enrolled, fake_registry):
        db.get_connector.return_value = connector_row()
        FakeConnector.connect_error = TimeoutError("login timed out")
        assert await mgr.start_connector("c1") is False
        assert db.update_connector_status.call_args.args == ("c1", "error")
        assert db.update_connector_status.call_args.kwargs == {"error": "login timed out"}
        FakeConnector.connect_error = None
        FakeConnector.connect_result = False
        assert await mgr.start_connector("c1") is False
        assert db.update_connector_status.call_args.kwargs == {"error": "connect() returned False"}
        assert mgr.get_running_instance("c1") is None

    @pytest.mark.parametrize("change", ["fenced", "owned_elsewhere", "deleted", "ownership_down"])
    async def test_start_rule_is_rechecked_after_connect(self, mgr, db, fake_registry, monkeypatch, change):
        """A fence (or an ownership move / delete) that lands while connect() is
        in flight stops the client once it connects, instead of leaving it to
        the startup sweep, which gives up after FENCE_SETTLE_SECONDS."""
        db.get_connector.return_value = connector_row()
        state = {"rule": "start"}

        def decide(row):
            if state["rule"] == "boom":
                raise RuntimeError("network db down")
            return state["rule"]

        monkeypatch.setattr(ownership, "decide_connector", decide)

        async def connect(self):
            # The fence lands mid-connect.
            if change == "fenced" or change == "owned_elsewhere":
                state["rule"] = "skip"
            elif change == "deleted":
                db.get_connector.return_value = None
            else:
                state["rule"] = "boom"
            self._status = ConnectorStatus.CONNECTED
            return True

        monkeypatch.setattr(FakeConnector, "connect", connect)

        assert await mgr.start_connector("c1") is False
        assert mgr.get_running_instance("c1") is None
        [inst] = FakeConnector.instances
        inst.disconnect.assert_awaited_once()
        assert statuses(db) == ["connecting", "disconnected"]

    async def test_owned_start_survives_the_recheck(self, mgr, db, fake_registry, monkeypatch):
        db.get_connector.return_value = connector_row()
        monkeypatch.setattr(ownership, "decide_connector", lambda row: "start")
        assert await mgr.start_connector("c1") is True
        assert mgr.get_running_instance("c1") is FakeConnector.instances[0]
        FakeConnector.instances[0].disconnect.assert_not_awaited()

    async def test_secrets_are_decrypted_before_instantiation(self, mgr, db, not_enrolled, fake_registry, monkeypatch):
        db.get_connector.return_value = connector_row(config={"token": "enc:abc"})
        monkeypatch.setattr(ConnectorManager, "_decrypt_config",
                            staticmethod(lambda cfg, ctype: {k: v.replace("enc:", "") for k, v in cfg.items()}))
        assert await mgr.start_connector("c1") is True
        assert mgr.get_running_instance("c1").config == {"token": "abc"}

    async def test_starting_twice_does_not_orphan_the_live_instance(self, mgr, db, not_enrolled, fake_registry):
        db.get_connector.return_value = connector_row()
        assert await mgr.start_connector("c1") is True
        first = mgr.get_running_instance("c1")
        await mgr.start_connector("c1")
        assert mgr.get_running_instance("c1") is first or first.disconnect.await_count == 1

    async def test_second_start_of_a_live_connector_is_a_noop(self, mgr, db, not_enrolled, fake_registry):
        """POST /activate twice (or toggleConnector active=true) must not build
        a second client: two live clients would each hand every inbound message
        to the manager, running every turn twice."""
        db.get_connector.return_value = connector_row()
        assert await mgr.start_connector("c1") is True
        first = mgr.get_running_instance("c1")
        for status in (ConnectorStatus.CONNECTED, ConnectorStatus.CONNECTING):
            first._status = status
            assert await mgr.start_connector("c1") is True
        assert FakeConnector.instances == [first]
        assert mgr.get_running_instance("c1") is first
        first.disconnect.assert_not_awaited()
        assert statuses(db) == ["connecting"]

    @pytest.mark.parametrize("stuck", ["handshake_timed_out", "connect_task_ended"])
    async def test_client_stuck_connecting_is_replaced_by_activate(
        self, mgr, db, not_enrolled, fake_registry, stuck,
    ):
        """R6-5: a gateway handshake that never completes left the client
        CONNECTING for good, and /activate treated it as live."""
        db.get_connector.return_value = connector_row()
        assert await mgr.start_connector("c1") is True
        first = mgr.get_running_instance("c1")
        first._status = ConnectorStatus.CONNECTING
        if stuck == "handshake_timed_out":
            mgr._registered_at["c1"] -= manager_module.CONNECTING_STALE_SECONDS + 1
        else:
            first._task = asyncio.get_running_loop().create_future()
            first._task.set_result(None)
        assert await mgr.start_connector("c1") is True
        second = mgr.get_running_instance("c1")
        assert second is not first and FakeConnector.instances == [first, second]
        first.disconnect.assert_awaited_once()

    async def test_client_still_within_its_handshake_is_left_alone(self, mgr, db, not_enrolled, fake_registry):
        db.get_connector.return_value = connector_row()
        assert await mgr.start_connector("c1") is True
        first = mgr.get_running_instance("c1")
        first._status = ConnectorStatus.CONNECTING
        first._task = asyncio.get_running_loop().create_future()  # still running
        assert await mgr.start_connector("c1") is True
        assert mgr.get_running_instance("c1") is first
        first.disconnect.assert_not_awaited()
        first._task.cancel()

    @pytest.mark.parametrize("dead", [ConnectorStatus.ERROR, ConnectorStatus.DISCONNECTED])
    async def test_dead_registered_instance_is_disconnected_and_replaced(
        self, mgr, db, not_enrolled, fake_registry, dead,
    ):
        db.get_connector.return_value = connector_row()
        assert await mgr.start_connector("c1") is True
        first = mgr.get_running_instance("c1")
        first._status = dead
        assert await mgr.start_connector("c1") is True
        second = mgr.get_running_instance("c1")
        assert second is not first and FakeConnector.instances == [first, second]
        first.disconnect.assert_awaited_once()
        second.disconnect.assert_not_awaited()

    async def test_concurrent_starts_leave_one_live_client(self, mgr, db, not_enrolled, fake_registry, monkeypatch):
        """Two starts racing past the registry check both connect; whichever
        registers second must disconnect the one it displaces."""
        db.get_connector.return_value = connector_row()
        both_connecting = asyncio.Event()
        entered = []

        async def slow_connect(self):
            entered.append(self)
            if len(entered) == 2:
                both_connecting.set()
            await both_connecting.wait()
            self._status = ConnectorStatus.CONNECTED
            return True

        monkeypatch.setattr(FakeConnector, "connect", slow_connect)
        results = await asyncio.wait_for(
            asyncio.gather(mgr.start_connector("c1"), mgr.start_connector("c1")), timeout=10,
        )
        assert results == [True, True]
        live = mgr.get_running_instance("c1")
        assert len(FakeConnector.instances) == 2 and live in FakeConnector.instances
        (orphan,) = [i for i in FakeConnector.instances if i is not live]
        orphan.disconnect.assert_awaited_once()
        live.disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# Event dispatch and relay
# ---------------------------------------------------------------------------

@pytest.fixture
def sinks(monkeypatch):
    from app.core import webhook_service
    from app.core.automation_manager import automation_manager
    from app.core.connectors import lifecycle_relay

    s = SimpleNamespace(webhook=AsyncMock(), automations=AsyncMock(), trigger=AsyncMock(), publish=MagicMock())
    monkeypatch.setattr(webhook_service, "dispatch_webhook", s.webhook)
    monkeypatch.setattr(automation_manager, "handle_connector_event", s.automations)
    monkeypatch.setattr(automation_manager, "handle_connector_trigger", s.trigger)
    monkeypatch.setattr(lifecycle_relay, "publish", s.publish)
    return s


def running(mgr, cid="c1"):
    inst = FakeConnector(cid, "fake", {}, AsyncMock(), AsyncMock())
    mgr._connectors[cid] = inst
    return inst


class TestDispatchEvent:
    async def test_local_owner_delivers_to_linked_chats_only(self, mgr, db, me, not_enrolled, sinks):
        inst = running(mgr)
        db.get_connectors_for_event.return_value = [connector_row()]
        db.get_connector_sessions_for_ai_session.return_value = [
            {"connector_id": "c1", "platform_chat_id": "chat-A"},
            {"connector_id": "other", "platform_chat_id": "chat-B"},
            {"connector_id": "c1", "platform_chat_id": None},
        ]
        data = {"session_id": "ai-1", "duration_seconds": 3}
        await mgr.dispatch_event("session.complete", data)
        assert inst.notifications == [("chat-A", "session.complete", data)]
        log = db.create_connector_event_log.call_args.kwargs
        assert (log["status"], log["direction"], log["error"]) == ("delivered", "outbound", None)
        db.update_connector_event_dispatched.assert_called_once_with("c1", success=True)
        sinks.webhook.assert_awaited_once_with("session.complete", data)
        sinks.automations.assert_awaited_once_with("session.complete", data)

    async def test_remote_owner_is_relayed_not_delivered(self, mgr, db, me, not_enrolled, sinks):
        inst = running(mgr)
        db.get_connectors_for_event.return_value = [connector_row(runner_id="runner-2")]
        await mgr.dispatch_event("session.error", {"session_id": "ai-1"})
        sinks.publish.assert_called_once_with("c1", "runner-2", "session.error", {"session_id": "ai-1"})
        assert inst.notifications == []

    async def test_relay_failure_is_swallowed(self, mgr, db, me, not_enrolled, sinks):
        sinks.publish.side_effect = RuntimeError("pg notify failed")
        db.get_connectors_for_event.return_value = [connector_row(runner_id="runner-2")]
        await mgr.dispatch_event("session.error", {})
        sinks.webhook.assert_awaited_once()

    async def test_enrolled_non_owner_skips(self, mgr, db, me, sinks, monkeypatch):
        inst = running(mgr)
        monkeypatch.setattr(ownership, "enrolled", lambda: True)
        monkeypatch.setattr(ownership, "owns", lambda row: False)
        db.get_connectors_for_event.return_value = [connector_row()]
        await mgr.dispatch_event("session.complete", {"session_id": "ai-1"})
        assert inst.notifications == [] and not sinks.publish.called

    async def test_sink_failures_are_isolated(self, mgr, db, me, not_enrolled, sinks):
        sinks.webhook.side_effect = RuntimeError("webhook down")
        await mgr.dispatch_event("session.started", {})
        sinks.automations.assert_awaited_once()

    async def test_all_notifications_failing_logs_failed(self, mgr, db, me, not_enrolled, sinks):
        inst = running(mgr)
        inst.notify_ok = False
        db.get_connectors_for_event.return_value = [connector_row()]
        db.get_connector_sessions_for_ai_session.return_value = [{"connector_id": "c1", "platform_chat_id": "chat-A"}]
        await mgr.dispatch_event("session.complete", {"session_id": "ai-1"})
        log = db.create_connector_event_log.call_args.kwargs
        assert log["status"] == "failed" and log["error"] == "No messages were delivered to platform"
        db.update_connector_event_dispatched.assert_called_once_with("c1", success=False)

    async def test_delivery_exception_logs_failure(self, mgr, db, me, not_enrolled, sinks):
        running(mgr)
        db.get_connectors_for_event.return_value = [connector_row()]
        db.get_connector_sessions_for_ai_session.side_effect = RuntimeError("db gone")
        await mgr.dispatch_event("session.complete", {"session_id": "ai-1"})
        log = db.create_connector_event_log.call_args.kwargs
        assert log["status"] == "failed" and log["error"] == "db gone"

    async def test_not_running_or_no_sessions_is_a_noop(self, mgr, db, me, not_enrolled, sinks):
        db.get_connectors_for_event.return_value = [connector_row()]
        await mgr.dispatch_event("session.complete", {"session_id": "ai-1"})  # not running
        running(mgr)
        await mgr.dispatch_event("session.complete", {"session_id": "ai-1"})  # no linked sessions
        db.create_connector_event_log.assert_not_called()


class TestBroadcastDelivery:
    async def test_without_session_id_broadcasts_to_active_chats(self, mgr, mock_db, monkeypatch):
        from app.db.connectors import create_connector, create_connector_session

        create_connector("c1", "webhook", "hook", {}, ["session.started"])
        create_connector_session("cs-1", "c1", "chat-1")
        create_connector_session("cs-2", "c1", "chat-2")
        log = MagicMock()
        monkeypatch.setattr(database, "create_connector_event_log", log)
        monkeypatch.setattr(database, "update_connector_event_dispatched", MagicMock())
        inst = running(mgr)
        await mgr._deliver_connector_event(connector_row(), "session.started", {"x": 1})
        assert sorted(n[0] for n in inst.notifications) == ["chat-1", "chat-2"]
        assert log.call_args.kwargs["status"] == "delivered"


class TestRelayedEvent:
    @pytest.mark.parametrize("row", [
        None,
        connector_row(is_active=False),
        connector_row(runner_id="runner-2"),
        connector_row(events=["session.error"]),
    ])
    async def test_rechecks_reject(self, mgr, db, me, not_enrolled, row):
        inst = running(mgr)
        db.get_connector.return_value = row
        db.get_connector_sessions_for_ai_session.return_value = [{"connector_id": "c1", "platform_chat_id": "chat"}]
        await mgr._deliver_relayed_event("c1", "session.complete", {"session_id": "ai-1"})
        assert inst.notifications == []

    async def test_accepted_relay_delivers(self, mgr, db, me, not_enrolled):
        inst = running(mgr)
        db.get_connector.return_value = connector_row()
        db.get_connector_sessions_for_ai_session.return_value = [{"connector_id": "c1", "platform_chat_id": "chat"}]
        await mgr._deliver_relayed_event("c1", "session.complete", {"session_id": "ai-1"})
        assert [n[0] for n in inst.notifications] == ["chat"]

    async def test_enrolled_non_owner_rejects(self, mgr, db, me, monkeypatch):
        inst = running(mgr)
        monkeypatch.setattr(ownership, "enrolled", lambda: True)
        monkeypatch.setattr(ownership, "owns", lambda row: False)
        db.get_connector.return_value = connector_row()
        await mgr._deliver_relayed_event("c1", "session.complete", {"session_id": "ai-1"})
        assert inst.notifications == []


class TestTriggersAndRelayLifecycle:
    async def test_trigger_routes_to_automations(self, mgr, sinks):
        await mgr._handle_trigger("c1", "email_received", {"subject": "hi"})
        sinks.trigger.assert_awaited_once_with("c1", "email_received", {"subject": "hi"})
        sinks.trigger.side_effect = RuntimeError("automation db down")
        await mgr._handle_trigger("c1", "email_received", {})  # swallowed

    async def test_event_relay_start_and_shutdown(self, mgr, db, me, monkeypatch):
        from app.core.connectors import lifecycle_relay

        relay = MagicMock(start=MagicMock(), stop=AsyncMock())
        ctor = MagicMock(return_value=relay)
        monkeypatch.setattr(lifecycle_relay, "LifecycleRelay", ctor)
        mgr.start_event_relay()
        mgr.start_event_relay()  # reuses the relay
        ctor.assert_called_once_with(ME, mgr._deliver_relayed_event)
        assert relay.start.call_count == 2
        inst = running(mgr)
        await mgr.shutdown_all()
        relay.stop.assert_awaited_once()
        inst.disconnect.assert_awaited_once()
        assert mgr._event_relay is None and mgr._connectors == {}


# ---------------------------------------------------------------------------
# Inbound message -> queued turn -> reply
# ---------------------------------------------------------------------------

@pytest.fixture
def turns(monkeypatch):
    from app.core import turn_manager, turn_outcome
    from app.core.sync_engine import sync_engine

    t = SimpleNamespace(
        submit=AsyncMock(return_value="turn-1"),
        idle=AsyncMock(return_value=True),
        outcome=MagicMock(return_value={"error": None}),
        text=MagicMock(return_value="Here is your answer."),
        broadcast=AsyncMock(),
    )
    monkeypatch.setattr(turn_manager, "submit_async", t.submit)
    monkeypatch.setattr(turn_manager, "wait_session_idle", t.idle)
    monkeypatch.setattr(turn_outcome, "final_turn_outcome", t.outcome)
    monkeypatch.setattr(turn_outcome, "extract_final_assistant_text", t.text)
    monkeypatch.setattr(sync_engine, "broadcast_user_session_created", t.broadcast)
    return t


def incoming(text="What's the weather?", mid="m-1"):
    return IncomingMessage(connector_id="c1", platform_chat_id="chat-9", platform_user_id="u-1",
                           platform_user_name="Alice", text=text, platform_message_id=mid)


@pytest.fixture
def routed(db):
    db.get_connector.return_value = connector_row(api_user_id="au-1")
    db.get_api_user.return_value = {"id": "au-1", "profile_ids": ["prof-1", "prof-2"], "project_id": "proj-1",
                                    "model": "claude-sonnet", "effort": "high"}
    db.get_connector_session.return_value = None
    db.create_connector_session.return_value = {"id": "cs-1", "session_id": None}
    return db


class TestIncomingMessage:
    async def test_new_chat_mints_session_runs_turn_and_replies(self, mgr, routed, not_enrolled, turns):
        inst = running(mgr)
        await mgr.handle_incoming_message(incoming("x" * 60))

        inst.send_ack_reaction.assert_awaited_once_with("chat-9", "m-1")
        inst.mark_as_read.assert_awaited_once_with("chat-9", "m-1")
        routed.create_connector_session.assert_called_once()
        created = routed.create_session.call_args.kwargs
        assert created["profile_id"] == "prof-1" and created["project_id"] == "proj-1"
        assert created["permission_mode"] == "default" and created["model"] == "claude-sonnet"
        assert created["title"] == "x" * 50 + "..."
        ai_session = created["session_id"]
        routed.update_connector_session_ai_session.assert_called_once_with("cs-1", ai_session)
        turns.broadcast.assert_awaited_once()

        args, kwargs = turns.submit.await_args
        assert args == (ai_session, "connector")
        assert kwargs["prompt"] == "x" * 60 and kwargs["profile_id"] == "prof-1"
        overrides = kwargs["overrides"]
        assert overrides["permission_mode"] == "default"
        assert overrides["model"] == "claude-sonnet" and overrides["effort"] == "high"
        assert "USE THE FAKE TOOLS in chat-9" in overrides["system_prompt_append"]
        assert "Max message length: 2000" in overrides["system_prompt_append"]
        assert "typing indicator" in overrides["system_prompt_append"]
        inst.send_typing_indicator.assert_awaited()

        assert [(m.platform_chat_id, m.text, m.reply_to_message_id) for m in inst.sent] == [
            ("chat-9", "Here is your answer.", "m-1"),
        ]
        log = routed.create_connector_event_log.call_args.kwargs
        assert (log["direction"], log["status"]) == ("inbound", "delivered")
        routed.update_connector_event_dispatched.assert_called_once_with("c1", success=True)

    async def test_existing_linked_session_is_reused(self, mgr, routed, not_enrolled, turns):
        running(mgr)
        routed.get_connector_session.return_value = {"id": "cs-1", "session_id": "ai-existing"}
        await mgr.handle_incoming_message(incoming())
        routed.create_session.assert_not_called()
        routed.update_connector_session_ai_session.assert_not_called()
        assert turns.submit.await_args.args[0] == "ai-existing"

    @pytest.mark.parametrize("api_user,expected", [
        (None, CONNECTOR_NEEDS_API_USER_MESSAGE),
        ({"id": "au-1", "profile_ids": []}, CONNECTOR_NEEDS_PROFILE_MESSAGE),
    ])
    async def test_missing_api_user_or_profile_is_refused(self, mgr, routed, not_enrolled, turns, api_user, expected):
        inst = running(mgr)
        routed.get_api_user.return_value = api_user
        await mgr.handle_incoming_message(incoming())
        assert [m.text for m in inst.sent] == [expected]
        turns.submit.assert_not_awaited()
        routed.create_connector_session.assert_not_called()

    async def test_revoked_ownership_drops_before_ack(self, mgr, routed, turns, monkeypatch):
        inst = running(mgr)
        monkeypatch.setattr(ownership, "enrolled", lambda: True)
        monkeypatch.setattr(ownership, "owns", lambda row: False)
        await mgr.handle_incoming_message(incoming())
        inst.send_ack_reaction.assert_not_awaited()
        assert inst.sent == [] and not turns.submit.await_count

    async def test_turn_not_settling_sends_fallback(self, mgr, routed, not_enrolled, turns):
        inst = running(mgr)
        turns.idle.return_value = False
        await mgr.handle_incoming_message(incoming())
        assert [m.text for m in inst.sent] == ["I couldn't generate a response."]
        turns.outcome.assert_not_called()

    async def test_failed_turn_still_sends_partial_text(self, mgr, routed, not_enrolled, turns):
        inst = running(mgr)
        turns.outcome.return_value = {"error": "rate limited"}
        turns.text.return_value = "Partial answer"
        await mgr.handle_incoming_message(incoming())
        assert [m.text for m in inst.sent] == ["Partial answer"]

    async def test_mint_failure_drops_message(self, mgr, routed, not_enrolled, turns):
        inst = running(mgr)
        routed.create_session.side_effect = RuntimeError("insert failed")
        await mgr.handle_incoming_message(incoming())
        turns.submit.assert_not_awaited()
        assert inst.sent == []

    async def test_reply_send_failure_is_logged_failed(self, mgr, routed, not_enrolled, turns, monkeypatch):
        inst = running(mgr)
        monkeypatch.setattr(inst, "send_message", AsyncMock(return_value=False))
        await mgr.handle_incoming_message(incoming())
        log = routed.create_connector_event_log.call_args.kwargs
        assert log["status"] == "failed" and log["error"] == "Failed to send response"

    async def test_ack_failures_do_not_block_the_turn(self, mgr, routed, not_enrolled, turns):
        inst = running(mgr)
        inst.send_ack_reaction.side_effect = RuntimeError("no reaction perms")
        inst.mark_as_read.side_effect = RuntimeError("nope")
        inst.send_typing_indicator.side_effect = RuntimeError("typing broken")
        await mgr.handle_incoming_message(incoming())
        assert [m.text for m in inst.sent] == ["Here is your answer."]

    async def test_message_without_running_instance_still_runs_turn(self, mgr, routed, not_enrolled, turns):
        await mgr.handle_incoming_message(incoming(mid=None))
        turns.submit.assert_awaited_once()
        assert turns.submit.await_args.kwargs["overrides"].get("system_prompt_append") is None
        log = routed.create_connector_event_log.call_args.kwargs
        assert log["status"] == "failed"

    async def test_unexpected_error_is_contained(self, mgr, routed, not_enrolled, turns):
        running(mgr)
        routed.get_connector.side_effect = RuntimeError("db exploded")
        await mgr.handle_incoming_message(incoming())  # must not raise
        turns.submit.assert_not_awaited()


def test_connector_status_helper(mgr):
    inst = running(mgr)
    inst._status = ConnectorStatus.ERROR
    inst._status_message = "boom"
    assert mgr.get_connector_status("c1") == {"connector_id": "c1", "status": "error", "status_message": "boom"}
