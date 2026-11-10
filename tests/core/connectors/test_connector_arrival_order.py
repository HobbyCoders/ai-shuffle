"""Quick messages in one chat reach the manager in the order they arrived (#1324).

Each test drives a connector's real inbound handler with two messages for one
chat and makes the step before the hand-off slow for the first one. The real
``ConnectorManager.handle_incoming_message`` fixes a chat's order; only the
work after that (routing, the turn) is replaced by a recorder.
"""

import asyncio
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.connectors.manager import ConnectorManager

pytestmark = pytest.mark.asyncio


@pytest.fixture
def manager(monkeypatch):
    """A real manager whose queue order is recorded as messages are processed."""
    from app.core import connector_permissions

    async def not_an_answer(_message):
        return False

    monkeypatch.setattr(connector_permissions, "answer_from_chat", not_an_answer)
    received = []
    held = {}
    instance = ConnectorManager()

    async def record(message):
        # Runs under the chat's lock, so this is the order turns start in. A
        # text in ``held`` keeps its turn running until that event is set.
        received.append((message.platform_chat_id, message.text, message.platform_user_name))
        if message.text in held:
            await held[message.text].wait()

    monkeypatch.setattr(instance, "_handle_incoming_message", record)
    instance.received = received
    instance.held = held
    return instance


async def settle(predicate, timeout=5.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


# --------------------------------------------------------------------- Slack

@pytest.fixture
def slack(monkeypatch, manager):
    # The socket client's response type is the only SDK piece the handler uses.
    response = types.ModuleType("slack_sdk.socket_mode.response")
    response.SocketModeResponse = lambda envelope_id: SimpleNamespace(envelope_id=envelope_id)
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.response", response)
    from app.core.connectors.slack import SlackConnector
    connector = SlackConnector("slack-id", "slack", {}, manager.handle_incoming_message, AsyncMock())
    gates = {}
    looked_up = []

    async def users_info(user):
        looked_up.append(user)
        gate = gates.get(user)
        if gate is not None:
            await gate.wait()
            if gate.fail:
                raise RuntimeError("users.info timed out")
        return {"user": {"profile": {"display_name": f"name-{user}"}}}

    connector._web_client = SimpleNamespace(users_info=users_info)
    socket = SimpleNamespace(send_socket_mode_response=AsyncMock())
    return SimpleNamespace(connector=connector, gates=gates, looked_up=looked_up, socket=socket)


def slack_event(channel, user, text, ts):
    return SimpleNamespace(type="events_api", envelope_id=f"env-{ts}", payload={
        "event": {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts}})


def slow_gate(fail=False):
    gate = asyncio.Event()
    gate.fail = fail
    return gate


def deliver_slack(slack, *requests):
    # slack_sdk starts one task per socket message, in arrival order.
    return [asyncio.ensure_future(slack.connector._handle_socket_event(slack.socket, request))
            for request in requests]


async def test_slack_slow_name_lookup_does_not_let_a_later_message_overtake(slack, manager):
    slack.gates["U1"] = slow_gate()
    tasks = deliver_slack(slack, slack_event("C1", "U1", "first", "1.1"),
                          slack_event("C1", "U2", "second", "1.2"))
    await settle(lambda: "U2" in slack.looked_up)
    await asyncio.sleep(0.05)
    assert manager.received == []  # the second message waits for the first
    slack.gates["U1"].set()
    await asyncio.gather(*tasks)
    assert manager.received == [("C1", "first", "name-U1"), ("C1", "second", "name-U2")]
    assert slack.connector._arrival_tails == {}


async def test_slack_failed_lookup_keeps_the_message_and_its_place(slack, manager):
    slack.gates["U1"] = slow_gate(fail=True)
    tasks = deliver_slack(slack, slack_event("C1", "U1", "first", "1.1"),
                          slack_event("C1", "U2", "second", "1.2"))
    await settle(lambda: "U2" in slack.looked_up)
    slack.gates["U1"].set()
    await asyncio.gather(*tasks)
    # The name falls back to the user id; nothing is dropped or stalled.
    assert manager.received == [("C1", "first", "U1"), ("C1", "second", "name-U2")]


async def test_slack_other_channels_do_not_wait_for_a_slow_lookup(slack, manager):
    slack.gates["U1"] = slow_gate()
    tasks = deliver_slack(slack, slack_event("C1", "U1", "first", "1.1"),
                          slack_event("C2", "U2", "elsewhere", "1.2"))
    await settle(lambda: manager.received == [("C2", "elsewhere", "name-U2")])
    slack.gates["U1"].set()
    await asyncio.gather(*tasks)
    assert manager.received[-1] == ("C1", "first", "name-U1")


async def test_slack_filtered_message_gives_up_its_place(slack, manager):
    slack.gates["U1"] = slow_gate()
    bot_echo = slack_event("C1", "U9", "bot reply", "1.2")
    bot_echo.payload["event"]["bot_id"] = "B1"
    tasks = deliver_slack(slack, slack_event("C1", "U1", "first", "1.1"), bot_echo,
                          slack_event("C1", "U2", "second", "1.3"))
    await settle(lambda: "U2" in slack.looked_up)
    slack.gates["U1"].set()
    await asyncio.gather(*tasks)
    assert [text for _chat, text, _name in manager.received] == ["first", "second"]
    assert slack.connector._arrival_tails == {}


# ---------------------------------------------------------------------- LINE

@pytest.fixture
def line_connector(monkeypatch, manager):
    """A LINE connector whose first profile lookup waits for ``first_gate``."""
    from app.core.connectors import line
    connector = line.LINEConnector("line-id", "line", {"channel_access_token": "test-token"},
                                   manager.handle_incoming_message, AsyncMock())
    connector.first_gate = first_gate = asyncio.Event()
    connector.calls = calls = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, headers):
            calls.append(url)
            if len(calls) == 1:
                await first_gate.wait()
            return SimpleNamespace(status_code=200, json=lambda: {"displayName": f"name-{len(calls)}"})

    monkeypatch.setattr(line.httpx, "AsyncClient", Client)
    return connector


def line_message(text, chat="U1"):
    return {"type": "message", "replyToken": f"reply-{text}", "source": {"type": "user", "userId": chat},
            "message": {"type": "text", "id": f"id-{text}", "text": text}}


def line_webhook(*events):
    return {"events": list(events)}


def texts(manager):
    return [text for _chat, text, _name in manager.received]


async def test_line_slow_profile_lookup_does_not_let_a_later_webhook_overtake(line_connector, manager):
    connector = line_connector
    # Two webhook deliveries overlap; each is its own request task.
    tasks = [asyncio.ensure_future(connector.handle_webhook_payload(line_webhook(line_message("first")))),
             asyncio.ensure_future(connector.handle_webhook_payload(line_webhook(line_message("second"))))]
    await settle(lambda: len(connector.calls) == 2)
    await asyncio.sleep(0.05)
    assert manager.received == []
    connector.first_gate.set()
    await asyncio.gather(*tasks)
    assert texts(manager) == ["first", "second"]


async def test_line_later_webhook_does_not_slip_between_two_messages_of_one_payload(line_connector, manager):
    connector = line_connector
    manager.held["A"] = asyncio.Event()  # A's turn is still running when C arrives
    first = asyncio.ensure_future(connector.handle_webhook_payload(
        line_webhook(line_message("A"), line_message("B"))))
    await settle(lambda: connector.calls)
    second = asyncio.ensure_future(connector.handle_webhook_payload(line_webhook(line_message("C"))))
    await asyncio.sleep(0.05)
    connector.first_gate.set()
    await settle(lambda: manager.received)
    await asyncio.sleep(0.05)
    manager.held["A"].set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert texts(manager) == ["A", "B", "C"]
    assert connector._arrival_tails == {}


async def test_line_answer_in_a_payload_reaches_the_turn_waiting_for_it(line_connector, manager):
    connector = line_connector
    connector.first_gate.set()
    manager.held["waiting"] = asyncio.Event()  # a turn waiting for the owner's answer
    waiting = asyncio.ensure_future(connector.handle_webhook_payload(line_webhook(line_message("waiting"))))
    await settle(lambda: manager.received)

    async def answered(*_args):
        manager.held["waiting"].set()

    connector._on_permission_response = answered
    # A new message for the same chat comes first, then the answer, in one payload.
    both = asyncio.ensure_future(connector.handle_webhook_payload(line_webhook(
        line_message("next"), {"type": "postback", "postback": {"data": "perm:req-1:approve"}})))
    await asyncio.wait_for(asyncio.gather(waiting, both), 5)
    assert texts(manager) == ["waiting", "next"]


# ------------------------------------------------------------------ WhatsApp

@pytest.fixture
def whatsapp(manager):
    from app.core.connectors.whatsapp import WhatsAppConnector
    return WhatsAppConnector("wa-id", "whatsapp", {"phone_number_id": "1", "access_token": "test-token"},
                             manager.handle_incoming_message, AsyncMock())


def whatsapp_text(text, sender="15550100"):
    return {"type": "text", "from": sender, "id": f"wamid.{text}", "text": {"body": text}}


def whatsapp_webhook(*messages):
    return {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


async def test_whatsapp_later_webhook_does_not_slip_between_two_messages_of_one_payload(whatsapp, manager):
    manager.held["A"] = asyncio.Event()  # A's turn is still running when C arrives
    first = asyncio.ensure_future(whatsapp.handle_webhook_payload(
        whatsapp_webhook(whatsapp_text("A"), whatsapp_text("B"))))
    await settle(lambda: manager.received)
    second = asyncio.ensure_future(whatsapp.handle_webhook_payload(whatsapp_webhook(whatsapp_text("C"))))
    await asyncio.sleep(0.05)
    manager.held["A"].set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert texts(manager) == ["A", "B", "C"]
    assert whatsapp._arrival_tails == {}


async def test_whatsapp_answer_in_a_payload_reaches_the_turn_waiting_for_it(whatsapp, manager):
    manager.held["waiting"] = asyncio.Event()  # a turn waiting for the owner's answer
    waiting = asyncio.ensure_future(whatsapp.handle_webhook_payload(whatsapp_webhook(whatsapp_text("waiting"))))
    await settle(lambda: manager.received)

    async def answered(*_args):
        manager.held["waiting"].set()

    whatsapp._on_permission_response = answered
    answer = {"type": "interactive", "from": "15550100",
              "interactive": {"button_reply": {"id": "perm:req-1:approve"}}}
    # A new message for the same chat comes first, then the answer, in one payload.
    both = asyncio.ensure_future(whatsapp.handle_webhook_payload(whatsapp_webhook(whatsapp_text("next"), answer)))
    await asyncio.wait_for(asyncio.gather(waiting, both), 5)
    assert texts(manager) == ["waiting", "next"]


async def test_whatsapp_failed_message_does_not_stop_the_rest_or_keep_its_place(whatsapp, manager):
    real_find_contact = whatsapp._find_contact

    def find_contact(value, phone):
        if phone == "broken":
            raise RuntimeError("bad contact entry")
        return real_find_contact(value, phone)

    whatsapp._find_contact = find_contact
    await asyncio.wait_for(whatsapp.handle_webhook_payload(whatsapp_webhook(
        whatsapp_text("lost", sender="broken"), whatsapp_text("kept"),
        whatsapp_text("after", sender="broken"))), 5)
    assert texts(manager) == ["kept"]
    assert whatsapp._arrival_tails == {}


@pytest.mark.parametrize("hold_first_turn", [False, True])
async def test_cancelled_delivery_gives_up_every_place(whatsapp, manager, hold_first_turn):
    if hold_first_turn:
        manager.held["A"] = asyncio.Event()
    first = asyncio.ensure_future(whatsapp.handle_webhook_payload(whatsapp_webhook(
        whatsapp_text("A"), whatsapp_text("B", sender="other"))))
    if hold_first_turn:
        await settle(lambda: len(manager.received) == 2)  # cancelled mid-turn
    else:
        # Cancelled after the places are taken but before any handler has run.
        await asyncio.sleep(0)
        assert whatsapp._arrival_tails
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert whatsapp._arrival_tails == {}
    # Neither chat is blocked: new messages still reach the manager.
    await asyncio.wait_for(whatsapp.handle_webhook_payload(whatsapp_webhook(
        whatsapp_text("D", sender="other"), whatsapp_text("E"))), 5)
    assert texts(manager)[-2:] == ["D", "E"]


# --------------------------------------------------------------------- Teams

async def test_teams_slow_reference_save_keeps_arrival_order(tmp_path, monkeypatch, manager):
    """Already ordered on main: the save runs under a FIFO lock, and nothing is
    awaited between releasing it and taking the manager's place."""
    from app.core.connectors.msteams import MSTeamsConnector
    connector = MSTeamsConnector("teams-id", "msteams", {
        "app_id": "test-bot-app", "app_password": "test-secret",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "allowed_user_ids": ["allowed-user"], "_state_path": str(tmp_path / "refs.json"),
    }, manager.handle_incoming_message, AsyncMock())
    release_first = threading.Event()
    saves = []
    real_save = connector._save_references

    def slow_first_save():
        saves.append(1)
        if len(saves) == 1:
            release_first.wait(5)
        real_save()

    monkeypatch.setattr(connector, "_save_references", slow_first_save)

    def activity(text, activity_id):
        return {"type": "message", "id": activity_id, "channelId": "msteams",
                "serviceUrl": "https://smba.trafficmanager.net/teams/", "text": text,
                "conversation": {"id": "chat-1"}, "recipient": {"id": "bot"},
                "from": {"id": "channel-user", "aadObjectId": "allowed-user", "name": "Test User"}}

    tasks = [asyncio.ensure_future(connector.handle_webhook_activity(activity("first", "a1"))),
             asyncio.ensure_future(connector.handle_webhook_activity(activity("second", "a2")))]
    await asyncio.sleep(0.1)
    assert manager.received == []
    release_first.set()
    await asyncio.gather(*tasks)
    assert [text for _chat, text, _name in manager.received] == ["first", "second"]


# ------------------------------------------------- isolated connector worker

FAKE_CONNECTOR = '''
import asyncio, sys, time
from app.core.connectors.base import BaseConnector, IncomingMessage

worker = sys.modules["__main__"]
real_emit = worker.emit
held = []

def slow_first_message(value):
    # The first message's write to the host is slow, as when its thread is
    # scheduled late.
    if value.get("method") == "incoming" and not held:
        held.append(value)
        time.sleep(0.5)
    real_emit(value)

worker.emit = slow_first_message


class FakeConnector(BaseConnector):
    async def disconnect(self):
        pass

    async def send_message(self, message):
        return True

    async def send_permission_prompt(self, platform_chat_id, prompt):
        return True

    async def send_notification(self, platform_chat_id, event_type, data):
        return True

    async def handle_webhook_payload(self, payload):
        # Hands both messages over in order, one task each.
        await asyncio.gather(*(self._on_incoming_message(IncomingMessage(
            connector_id=self.connector_id, platform_chat_id="chat-1", platform_user_id="u",
            platform_user_name="u", text=text)) for text in payload["texts"]))
'''


async def test_worker_hands_the_host_callbacks_in_call_order(tmp_path):
    from app.core.connectors import package_proxy
    connectors = Path(package_proxy.__file__).parent
    package = tmp_path / "package"
    package.mkdir()
    (package / "fake_connector.py").write_text(FAKE_CONNECTOR, encoding="utf-8")
    received = []

    async def incoming(message):
        received.append(message.text)

    cls = package_proxy.proxy_class("webhook", package, {"module": "fake_connector", "class": "FakeConnector"})
    proxy = cls("fake-id", "webhook", {}, incoming, AsyncMock())
    proxy._process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(connectors / "package_worker.py"), str(package),
        "fake_connector", "FakeConnector", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=package_proxy.worker_environment(), limit=2_000_000,
    )
    proxy._reader = asyncio.create_task(proxy._read())
    try:
        await proxy._call("init", connector_id="fake-id", connector_type="webhook", config={})
        await asyncio.wait_for(proxy._call("handle_webhook_payload", {"texts": ["first", "second"]}), 20)
        assert received == ["first", "second"]
    finally:
        await proxy.disconnect()
