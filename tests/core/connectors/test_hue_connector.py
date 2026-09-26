"""HueConnector against a fake Hue Bridge (httpx MockTransport, no network).

Covers connect-time validation (HTTP errors, the bridge's 200-with-error-list
shape, timeouts), the text-command parser and its handlers, the polling loop's
change detection / trigger emission / failure escalation, notifications, the
legacy tool shim and test_connection. The action-gateway handlers live in
hue_actions and are covered in tests/mcp/test_connector_gateway_wave3_c.py.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.connectors import hue as hue_module
from app.core.connectors.base import ConnectorStatus, OutgoingMessage
from app.core.connectors.hue import HueConnector
from tests.fakes.http import mock_transport_client

BRIDGE = "10.0.0.2"
KEY = "k3y"

LIGHTS = {
    "1": {"name": "Floor Lamp", "state": {"on": True, "bri": 254, "ct": 300, "reachable": True}},
    "2": {"name": "Lamp", "state": {"on": False, "bri": 127, "reachable": True}},
    "3": {"name": "Desk Strip", "state": {"on": False, "reachable": False}},
}
GROUPS = {
    "1": {"name": "Living Room", "type": "Room", "lights": ["1", "2"], "state": {"all_on": True, "any_on": True}},
    "2": {"name": "Office", "type": "Room", "lights": ["3"], "state": {"all_on": False, "any_on": True}},
    "3": {"name": "Porch", "type": "Zone", "lights": [], "state": {"all_on": False, "any_on": False}},
}
SCENES = {
    "abc": {"name": "Relax", "group": "1"},
    "def": {"name": "Energize Morning", "group": "2"},
}
SUCCESS = [{"success": {"/lights/1/state/on": True}}]


class Bridge:
    """Records requests and serves canned Hue v1 responses keyed by (method, path)."""

    def __init__(self, routes=None):
        self.routes = {
            ("GET", "/lights"): (200, LIGHTS),
            ("GET", "/groups"): (200, GROUPS),
            ("GET", "/scenes"): (200, SCENES),
            ("GET", "/config"): (200, {"name": "Philips hue", "apiversion": "1.65.0"}),
        }
        self.routes.update(routes or {})
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/api/{KEY}"
        assert request.url.host == BRIDGE
        assert request.url.path.startswith(prefix)
        path = request.url.path[len(prefix):]
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        route = self.routes.get((request.method, path))
        if route is None:
            if request.method == "PUT":
                return httpx.Response(200, json=SUCCESS)
            return httpx.Response(404, text="not found")
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(request)
        status, payload = route
        return httpx.Response(status, json=payload)

    def puts(self):
        return [(p, b) for m, p, b in self.requests if m == "PUT"]


def make_hue(bridge=None, *, config=None, on_trigger=None):
    conn = HueConnector(
        connector_id="hue-1",
        connector_type="hue",
        config={"bridge_ip": BRIDGE, "api_key": KEY, **(config or {})},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
        on_trigger=on_trigger,
    )
    if bridge is not None:
        conn._http_client = mock_transport_client(bridge)
        conn._lights = {lid: info["name"] for lid, info in LIGHTS.items()}
    return conn


def replies(conn):
    return [c.args[0].text for c in conn._on_incoming_message.await_args_list]


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def patch_async_client(monkeypatch, bridge):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(bridge))

    monkeypatch.setattr(hue_module.httpx, "AsyncClient", factory)


async def say(conn, text):
    return await conn.send_message(OutgoingMessage(platform_chat_id="hue-bridge", text=text))


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

class TestConnect:
    async def test_connect_caches_lights_and_starts_polling(self, monkeypatch):
        bridge = Bridge()
        patch_async_client(monkeypatch, bridge)
        conn = make_hue(config={"poll_interval_seconds": "3600"})
        assert await conn.connect() is True
        assert conn.status == ConnectorStatus.CONNECTED
        assert conn._lights == {"1": "Floor Lamp", "2": "Lamp", "3": "Desk Strip"}
        assert conn._light_states["2"]["bri"] == 127
        assert conn._poll_task is not None and not conn._poll_task.done()
        await conn.disconnect()
        assert conn._poll_task is None and conn._http_client is None
        assert conn.status == ConnectorStatus.DISCONNECTED

    async def test_poll_interval_zero_disables_polling(self, monkeypatch):
        patch_async_client(monkeypatch, Bridge())
        conn = make_hue(config={"poll_interval_seconds": "0"})
        assert await conn.connect() is True
        assert conn._poll_task is None
        await conn.disconnect()

    async def test_unauthorized_user_error_list_fails_connect(self, monkeypatch):
        bridge = Bridge({("GET", "/lights"): (200, [
            {"error": {"type": 1, "address": "/lights", "description": "unauthorized user"}},
        ])})
        patch_async_client(monkeypatch, bridge)
        conn = make_hue()
        assert await conn.connect() is False
        assert conn.status == ConnectorStatus.ERROR
        assert conn.status_message == "unauthorized user"
        assert conn._http_client is None  # closed on failure

    async def test_http_error_fails_connect(self, monkeypatch):
        patch_async_client(monkeypatch, Bridge({("GET", "/lights"): (503, {"x": 1})}))
        conn = make_hue()
        assert await conn.connect() is False
        assert "Bridge API returned 503" in conn.status_message

    async def test_timeout_fails_connect(self, monkeypatch):
        patch_async_client(monkeypatch, Bridge({("GET", "/lights"): httpx.ConnectTimeout("timed out")}))
        conn = make_hue()
        assert await conn.connect() is False
        assert conn.status == ConnectorStatus.ERROR
        assert conn._http_client is None

    async def test_api_helpers_require_client(self):
        conn = make_hue()
        with pytest.raises(RuntimeError, match="not initialised"):
            await conn._api_get("/lights")
        with pytest.raises(RuntimeError, match="not initialised"):
            await conn._api_put("/lights/1/state", {})
        assert await say(conn, "lights") is False


# ---------------------------------------------------------------------------
# Text commands
# ---------------------------------------------------------------------------

class TestCommands:
    async def test_list_lights(self):
        conn = make_hue(Bridge())
        assert await say(conn, "list lights") is True
        text = replies(conn)[0]
        assert "[1] Floor Lamp — on, 100%" in text
        assert "[2] Lamp — off, 50%" in text
        assert "[3] Desk Strip — off" in text

    async def test_list_rooms_states(self):
        conn = make_hue(Bridge())
        assert await say(conn, "rooms") is True
        text = replies(conn)[0]
        assert "Living Room (Room, 2 lights) — all on" in text
        assert "Office (Room, 1 lights) — some on" in text
        assert "Porch (Zone, 0 lights) — all off" in text

    async def test_list_scenes(self):
        conn = make_hue(Bridge())
        assert await say(conn, "Scenes") is True
        assert "[abc] Relax (group 1)" in replies(conn)[0]

    @pytest.mark.parametrize("text,path", [
        ("list lights", "/lights"), ("list rooms", "/groups"), ("list scenes", "/scenes"),
    ])
    async def test_list_commands_fail_on_http_error_or_error_list(self, text, path):
        conn = make_hue(Bridge({("GET", path): (500, {})}))
        assert await say(conn, text) is False
        conn = make_hue(Bridge({("GET", path): (200, [{"error": {"description": "x"}}])}))
        assert await say(conn, text) is False
        conn._on_incoming_message.assert_not_awaited()

    async def test_turn_on_by_id_and_off_by_name(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "turn on 3") is True
        assert await say(conn, "Turn OFF desk") is True
        assert bridge.puts() == [("/lights/3/state", {"on": True}), ("/lights/3/state", {"on": False})]
        assert replies(conn) == ["Desk Strip turned on", "Desk Strip turned off"]

    async def test_unknown_light_is_reported_not_sent(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "turn on garage") is True
        assert replies(conn) == ["Light not found: garage"]
        assert bridge.puts() == []

    async def test_brightness_both_syntaxes_and_clamp(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "set desk brightness 50") is True
        assert await say(conn, "set desk to 250%") is True
        assert bridge.puts() == [("/lights/3/state", {"bri": 127}), ("/lights/3/state", {"bri": 254})]
        assert replies(conn)[-1] == "Desk Strip brightness set to 100%"

    async def test_color_temp_preset(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "set desk color temp warm") is True
        assert bridge.puts() == [("/lights/3/state", {"ct": 400})]
        assert "(400 mirek)" in replies(conn)[0]

    async def test_color_temp_unknown_preset_direct(self):
        conn = make_hue(Bridge())
        assert await conn._cmd_set_color_temp("desk", "sunset") is True
        assert "Unknown color temperature preset" in replies(conn)[0]

    async def test_activate_scene_partial_match(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "activate scene energize") is True
        assert bridge.puts() == [("/groups/0/action", {"scene": "def"})]
        assert replies(conn) == ["Scene 'Energize Morning' activated"]

    async def test_activate_scene_not_found(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "activate scene party") is True
        assert replies(conn) == ["Scene not found: party"]
        assert bridge.puts() == []

    async def test_all_on_all_off(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        assert await say(conn, "all on") is True
        assert await say(conn, "ALL OFF") is True
        assert bridge.puts() == [("/groups/0/action", {"on": True}), ("/groups/0/action", {"on": False})]

    @pytest.mark.parametrize("text", [
        "turn on desk", "set desk brightness 10", "set desk color temp cool",
        "activate scene relax", "all on",
    ])
    async def test_write_commands_return_false_on_http_error(self, text):
        bridge = Bridge({
            ("PUT", "/lights/3/state"): (500, {"err": 1}),
            ("PUT", "/groups/0/action"): (500, {"err": 1}),
        })
        conn = make_hue(bridge)
        assert await say(conn, text) is False

    async def test_timeout_during_command_returns_false(self):
        conn = make_hue(Bridge({("PUT", "/lights/3/state"): httpx.ReadTimeout("slow bridge")}))
        assert await say(conn, "turn on desk") is False

    async def test_unrecognised_command(self):
        conn = make_hue(Bridge())
        assert await say(conn, "make me a sandwich") is False

    async def test_exact_light_name_beats_partial_match(self):
        bridge = Bridge()
        conn = make_hue(bridge)
        await say(conn, "turn off lamp")
        assert bridge.puts() == [("/lights/2/state", {"on": False})]

    async def test_bridge_error_list_on_write_is_not_reported_as_success(self):
        bridge = Bridge({("PUT", "/lights/3/state"): (200, [{"error": {
            "type": 201, "address": "/lights/3/state/bri",
            "description": "parameter, bri, is not modifiable. Device is set to off.",
        }}])})
        conn = make_hue(bridge)
        await say(conn, "set desk brightness 40")
        assert not any("brightness set to" in r for r in replies(conn))


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def _states(**overrides):
    data = json.loads(json.dumps(LIGHTS))
    for lid, state in overrides.items():
        data[lid]["state"].update(state)
    return data


class SequenceBridge(Bridge):
    """Serves successive /lights payloads, then cancels the poll loop."""

    def __init__(self, sequence):
        super().__init__()
        self.sequence = list(sequence)

    def __call__(self, request):
        self.requests.append((request.method, request.url.path, None))
        if not self.sequence:
            raise asyncio.CancelledError()
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return httpx.Response(status, json=payload)


class TestPolling:
    async def test_state_change_emits_message_and_trigger(self):
        on_trigger = AsyncMock()
        bridge = SequenceBridge([(200, _states(**{"2": {"on": True, "bri": 200}, "3": {"reachable": True}}))])
        conn = make_hue(bridge, on_trigger=on_trigger)
        conn._light_states = {lid: dict(info["state"]) for lid, info in LIGHTS.items()}
        await conn._poll_state(0)

        text = replies(conn)[0]
        assert text.startswith("Light state changes detected:")
        assert "Lamp turned on" in text
        assert "Lamp brightness changed to 79%" in text
        assert "Desk Strip became reachable" in text
        triggered = {c.args[2]["light_id"]: c.args[2] for c in on_trigger.await_args_list}
        assert set(triggered) == {"2", "3"}
        assert triggered["2"] == {"light_id": "2", "light_name": "Lamp", "on": True,
                                  "brightness": 200, "hue": 0, "saturation": 0}
        assert conn._light_states["2"]["bri"] == 200

    async def test_no_change_emits_nothing(self):
        on_trigger = AsyncMock()
        conn = make_hue(SequenceBridge([(200, _states())]), on_trigger=on_trigger)
        conn._light_states = {lid: dict(info["state"]) for lid, info in LIGHTS.items()}
        await conn._poll_state(0)
        conn._on_incoming_message.assert_not_awaited()
        on_trigger.assert_not_awaited()

    async def test_repeated_failures_escalate_to_error(self, monkeypatch):
        from app.db import database

        update = MagicMock()
        monkeypatch.setattr(database, "update_connector_status", update)
        bridge = SequenceBridge([
            (500, {}), (200, [{"error": {"description": "x"}}]), httpx.ReadTimeout("t"),
            (503, {}), httpx.ConnectError("refused"),
        ])
        conn = make_hue(bridge)
        await conn._poll_state(0)
        assert conn.status == ConnectorStatus.ERROR
        assert conn.status_message == "Repeated poll failures (5)"
        update.assert_called_once_with("hue-1", "error", error="Repeated poll failures (5)")

    async def test_success_resets_failure_count(self, monkeypatch):
        from app.db import database

        update = MagicMock()
        monkeypatch.setattr(database, "update_connector_status", update)
        bridge = SequenceBridge([(500, {})] * 4 + [(200, _states())] + [(500, {})] * 4)
        conn = make_hue(bridge)
        conn._light_states = {lid: dict(info["state"]) for lid, info in LIGHTS.items()}
        await conn._poll_state(0)
        update.assert_not_called()
        assert conn.status != ConnectorStatus.ERROR

    async def test_recovery_after_error_restores_connected(self, monkeypatch):
        from app.db import database

        monkeypatch.setattr(database, "update_connector_status", MagicMock())
        bridge = SequenceBridge([(500, {})] * 5 + [(200, _states())])
        conn = make_hue(bridge)
        conn._light_states = {lid: dict(info["state"]) for lid, info in LIGHTS.items()}
        await conn._poll_state(0)
        assert conn.status == ConnectorStatus.CONNECTED


def test_describe_state_diff_color_temperature():
    diffs = HueConnector._describe_state_diff("Lamp", {"ct": 300, "on": True}, {"ct": 450, "on": True})
    assert diffs == ["  - Lamp color temperature changed to 450 mirek"]


# ---------------------------------------------------------------------------
# Notifications, tool shim, test_connection
# ---------------------------------------------------------------------------

class TestMisc:
    async def test_notifications(self):
        conn = make_hue()
        assert await conn.send_notification("x", "session.complete", {"duration_seconds": 1.25, "total_cost": 0.5}) is True
        assert await conn.send_notification("x", "session.error", {"error_message": "boom"}) is True
        assert await conn.send_notification("x", "custom.event", {"k": 1}) is True
        texts = replies(conn)
        assert texts[0] == "[OK] Session Complete\n\nDuration: 1.2s\nCost: $0.5000"
        assert texts[1] == "[ERR] Session Error\n\nboom"
        assert texts[2].startswith("[i] custom.event")
        assert await conn.send_permission_prompt("x", MagicMock()) is False

    async def test_long_reply_is_chunked(self):
        conn = make_hue()
        await conn._emit_reply("x" * 9000)
        assert len(replies(conn)) == 3

    async def test_execute_tool_routes_to_actions(self):
        conn = make_hue()
        conn.execute_action = AsyncMock(return_value={"success": True})
        assert (await conn.execute_tool("nope", {}))["error"] == "Unknown tool: nope"
        assert (await conn.execute_tool("hue_control", {}))["error"] == "command is required"
        await conn.execute_tool("hue_control", {"command": "turn_on", "light_id": "1"})
        conn.execute_action.assert_awaited_once_with("turn_on", {"light_id": "1"})

    async def test_context_instructions_mention_gateway(self):
        assert "hue_invoke" in make_hue().get_context_instructions("x")

    async def test_test_connection(self, monkeypatch):
        patch_async_client(monkeypatch, Bridge())
        res = await make_hue().test_connection()
        assert res == {"success": True, "message": "Connected to Hue Bridge 'Philips hue' (API version 1.65.0)"}

        patch_async_client(monkeypatch, Bridge({("GET", "/config"): (403, {})}))
        assert await make_hue().test_connection() == {"success": False, "message": "Bridge returned 403"}

        patch_async_client(monkeypatch, Bridge({("GET", "/config"): httpx.ConnectTimeout("no route")}))
        res = await make_hue().test_connection()
        assert res["success"] is False and "no route" in res["message"]
