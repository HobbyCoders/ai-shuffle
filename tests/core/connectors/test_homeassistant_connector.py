"""HomeAssistantConnector against a fake Home Assistant REST API.

httpx MockTransport serves realistic /api, /api/states and /api/services
responses, so connect-time validation, the entity filter, the text-command
parser, the state poller, notifications and test_connection all run without
a network. The gateway action handlers live in homeassistant_actions.py and
are covered by tests/mcp/test_connector_gateway_wave3_*.py.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.connectors import homeassistant as ha_module
from app.core.connectors.base import ConnectorStatus, OutgoingMessage
from app.core.connectors.homeassistant import HomeAssistantConnector
from tests.fakes.http import mock_transport_client

BASE = "http://ha.local:8123"
TOKEN = "llat-123"


def entity(eid, state, **attrs):
    return {
        "entity_id": eid, "state": state, "attributes": attrs,
        "last_changed": "2026-09-25T08:00:00+00:00",
        "last_updated": "2026-09-25T08:00:00+00:00",
    }


STATES = [
    entity("light.kitchen", "on", friendly_name="Kitchen Light", brightness=200),
    entity("switch.coffee_maker", "off", friendly_name="Coffee Maker"),
    entity("climate.hallway", "heat", friendly_name="Hallway", temperature=21),
    entity("sensor.outdoor_temp", "14.2", friendly_name="Outdoor", unit_of_measurement="°C"),
    entity("input_select.house_mode", "Home", friendly_name="House Mode", options=["Home", "Away"]),
    entity("lock.front_door", "locked", friendly_name="Front Door"),
]


class FakeHA:
    """Records requests; routes keyed by (method, path) -> (status, json) | Exception."""

    def __init__(self, routes=None, states=None):
        self.states = states if states is not None else STATES
        self.routes = {
            ("GET", "/api/"): (200, {"message": "API running."}),
            ("GET", "/api/states"): (200, self.states),
            ("GET", "/api/config"): (200, {"version": "2026.9.1", "location_name": "Home"}),
        }
        for e in self.states:
            self.routes[("GET", f"/api/states/{e['entity_id']}")] = (200, e)
        self.routes.update(routes or {})
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        route = self.routes.get((request.method, path))
        if route is None:
            if request.method == "POST" and path.startswith("/api/services/"):
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={"message": "Entity not found."})
        if isinstance(route, Exception):
            raise route
        status, payload = route
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    def posts(self):
        return [(p, b) for m, p, b in self.requests if m == "POST"]


def make_ha(api=None, *, config=None, on_trigger=None, preload=True):
    conn = HomeAssistantConnector(
        connector_id="ha-1",
        connector_type="homeassistant",
        config={"url": BASE + "/", "access_token": TOKEN, **(config or {})},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
        on_trigger=on_trigger,
    )
    if api is not None:
        conn._http_client = mock_transport_client(api)
        if preload:
            conn._entity_states = {
                e["entity_id"]: e["state"] for e in api.states if conn._matches_filter(e["entity_id"])
            }
    return conn


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def patch_async_client(monkeypatch, api):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(api))

    monkeypatch.setattr(ha_module.httpx, "AsyncClient", factory)


def replies(conn):
    return [c.args[0].text for c in conn._on_incoming_message.await_args_list]


async def say(conn, text):
    return await conn.send_message(OutgoingMessage(platform_chat_id="ha-chat", text=text))


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

class TestConnect:
    async def test_connect_loads_filtered_states(self, monkeypatch):
        patch_async_client(monkeypatch, FakeHA())
        conn = make_ha(config={"entity_filter": "light., switch. ,", "poll_interval_seconds": "3600"})
        assert conn._entity_filter == ["light.", "switch."]
        assert await conn.connect() is True
        assert conn._entity_states == {"light.kitchen": "on", "switch.coffee_maker": "off"}
        assert conn._poll_task is not None
        await conn.disconnect()
        assert conn._entity_states == {} and conn._http_client is None
        assert conn.status == ConnectorStatus.DISCONNECTED

    @pytest.mark.parametrize("routes,needle", [
        ({("GET", "/api/"): (401, "401: Unauthorized")}, "HA API returned 401"),
        ({("GET", "/api/"): (200, {"message": "Something else"})}, "Unexpected API response"),
        ({("GET", "/api/states"): (500, {})}, "Failed to fetch states: 500"),
        ({("GET", "/api/"): httpx.ConnectTimeout("timed out")}, "timed out"),
    ])
    async def test_connect_failures(self, monkeypatch, routes, needle):
        patch_async_client(monkeypatch, FakeHA(routes))
        conn = make_ha()
        assert await conn.connect() is False
        assert conn.status == ConnectorStatus.ERROR
        assert needle in conn.status_message
        assert conn._http_client is None and conn._poll_task is None

    async def test_api_helpers_require_client(self):
        conn = make_ha()
        with pytest.raises(RuntimeError):
            await conn._api_get("/api/states")
        with pytest.raises(RuntimeError):
            await conn._api_post("/api/services/x/y")
        assert await say(conn, "status") is False
        assert await conn.send_notification("x", "session.started", {}) is False


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

class TestResolveEntity:
    @pytest.mark.parametrize("ref,expected", [
        ("light.kitchen", "light.kitchen"),
        ("  light.kitchen  ", "light.kitchen"),
        ("coffee maker", "switch.coffee_maker"),
        ("hallway", "climate.hallway"),
        ("outdoor", "sensor.outdoor_temp"),
        ("garage", None),
    ])
    def test_resolution(self, ref, expected):
        conn = make_ha(FakeHA())
        assert conn._resolve_entity(ref) == expected

    def test_resolution_respects_filter(self):
        conn = make_ha(FakeHA(), config={"entity_filter": "light."})
        assert conn._resolve_entity("front door") is None


# ---------------------------------------------------------------------------
# Text commands
# ---------------------------------------------------------------------------

class TestCommands:
    async def test_list_entities_formats_attributes_and_filters(self):
        conn = make_ha(FakeHA(), config={"entity_filter": "light.,sensor."})
        assert await say(conn, "list entities") is True
        text = replies(conn)[0]
        assert text.startswith("Entities (2):")
        assert "light.kitchen (Kitchen Light): on [brightness=200]" in text
        assert "sensor.outdoor_temp (Outdoor): 14.2 [unit_of_measurement=°C]" in text
        assert "switch." not in text

    async def test_list_entities_http_error_and_empty(self):
        conn = make_ha(FakeHA({("GET", "/api/states"): (502, {})}))
        assert await say(conn, "entities") is True
        assert replies(conn) == ["Failed to fetch entities (HTTP 502)"]
        conn = make_ha(FakeHA(states=[]))
        assert await say(conn, "status") is True
        assert replies(conn) == ["No matching entities found."]

    async def test_list_entities_long_output_is_chunked(self):
        many = [entity(f"sensor.probe_{i:03d}", "1", friendly_name="P" * 60) for i in range(120)]
        conn = make_ha(FakeHA(states=many))
        assert await say(conn, "entities") is True
        chunks = replies(conn)
        assert len(chunks) > 1 and all(len(c) <= 4000 for c in chunks)

    @pytest.mark.parametrize("text,service,eid", [
        ("turn on kitchen", "turn_on", "light.kitchen"),
        ("Turn Off coffee maker", "turn_off", "switch.coffee_maker"),
        ("toggle light.kitchen", "toggle", "light.kitchen"),
    ])
    async def test_service_commands(self, text, service, eid):
        api = FakeHA()
        conn = make_ha(api)
        assert await say(conn, text) is True
        assert api.posts() == [(f"/api/services/homeassistant/{service}", {"entity_id": eid})]
        assert replies(conn) == [f"OK: {service.replace('_', ' ')} {eid}"]

    async def test_service_unknown_entity_is_not_called(self):
        api = FakeHA()
        conn = make_ha(api)
        assert await say(conn, "turn on garage door") is True
        assert replies(conn) == ["Entity not found: garage door"]
        assert api.posts() == []

    async def test_service_upstream_error_is_reported(self):
        api = FakeHA({("POST", "/api/services/homeassistant/turn_on"): (500, "Internal Server Error")})
        conn = make_ha(api)
        assert await say(conn, "turn on kitchen") is True
        assert replies(conn) == ["Failed (500): Internal Server Error"]

    async def test_timeout_returns_false(self):
        conn = make_ha(FakeHA({("POST", "/api/services/homeassistant/turn_on"): httpx.ReadTimeout("slow")}))
        assert await say(conn, "turn on kitchen") is False

    @pytest.mark.parametrize("text,path,payload", [
        ("set kitchen to 128", "/api/services/light/turn_on", {"entity_id": "light.kitchen", "brightness": 128}),
        ("set hallway to 21.5", "/api/services/climate/set_temperature", {"entity_id": "climate.hallway", "temperature": 21.5}),
        ("set hallway to 20.0", "/api/services/climate/set_temperature", {"entity_id": "climate.hallway", "temperature": 20}),
    ])
    async def test_set_maps_domain_to_service(self, text, path, payload):
        api = FakeHA()
        conn = make_ha(api)
        assert await say(conn, text) is True
        assert api.posts() == [(path, payload)]
        assert replies(conn)[0].startswith("OK: set ")

    async def test_set_unknown_entity_and_upstream_error(self):
        api = FakeHA({("POST", "/api/services/light/turn_on"): (400, "bad brightness")})
        conn = make_ha(api)
        assert await say(conn, "set attic to 3") is True
        assert await say(conn, "set kitchen to 999") is True
        assert replies(conn) == ["Entity not found: attic", "Failed (400): bad brightness"]

    @pytest.mark.xfail(strict=True, reason=(
        "bug: send_message lower-cases the whole command before parsing, so values are "
        "lower-cased too; input_select options are case-sensitive in Home Assistant"
    ))
    async def test_set_preserves_value_case(self):
        api = FakeHA()
        conn = make_ha(api)
        await say(conn, "set house mode to Away")
        assert api.posts() == [("/api/services/input_select/select_option",
                                {"entity_id": "input_select.house_mode", "option": "Away"})]

    @pytest.mark.xfail(strict=True, reason=(
        "bug: _cmd_set falls back to homeassistant.turn_on for domains outside its "
        "service map, so 'set <switch> to off' turns the switch ON"
    ))
    async def test_set_off_on_unmapped_domain_does_not_turn_on(self):
        api = FakeHA()
        conn = make_ha(api)
        await say(conn, "set coffee maker to off")
        assert ("/api/services/homeassistant/turn_on", {"entity_id": "switch.coffee_maker"}) not in api.posts()

    async def test_get_state_lists_attributes(self):
        conn = make_ha(FakeHA())
        assert await say(conn, "get hallway") is True
        text = replies(conn)[0]
        assert text.splitlines()[:3] == [
            "Hallway (climate.hallway)", "State: heat", "Last changed: 2026-09-25T08:00:00+00:00",
        ]
        assert "  temperature: 21" in text

    async def test_get_state_errors(self):
        api = FakeHA({("GET", "/api/states/light.kitchen"): (404, {"message": "Entity not found."})})
        conn = make_ha(api)
        assert await say(conn, "state kitchen") is True
        assert await say(conn, "state attic") is True
        assert replies(conn)[0].startswith("Failed to get state (404)")
        assert replies(conn)[1] == "Entity not found: attic"

    async def test_call_service_with_json(self):
        api = FakeHA()
        conn = make_ha(api)
        assert await say(conn, 'call service light.turn_on light.kitchen {"brightness": 50, "transition": 2}') is True
        assert api.posts() == [("/api/services/light/turn_on",
                                {"entity_id": "light.kitchen", "brightness": 50, "transition": 2})]
        assert replies(conn) == ["OK: called light.turn_on on light.kitchen"]

    async def test_call_service_invalid_json_and_non_dict(self):
        api = FakeHA()
        conn = make_ha(api)
        assert await say(conn, "call service light.turn_on light.kitchen {oops") is True
        assert replies(conn) == ["Invalid JSON data: {oops"]
        assert api.posts() == []
        assert await say(conn, "call service light.turn_on light.kitchen [1, 2]") is True
        assert api.posts() == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"})]

    async def test_call_service_upstream_error(self):
        api = FakeHA({("POST", "/api/services/lock/unlock"): (401, "Unauthorized")})
        conn = make_ha(api)
        assert await say(conn, "call service lock.unlock lock.front_door") is True
        assert replies(conn) == ["Failed (401): Unauthorized"]

    async def test_unrecognised(self):
        assert await say(make_ha(FakeHA()), "open the pod bay doors") is False

    async def test_reply_goes_to_originating_chat(self):
        conn = make_ha(FakeHA())
        await say(conn, "status")
        msg = conn._on_incoming_message.await_args.args[0]
        assert msg.platform_chat_id == "ha-chat"
        assert msg.metadata == {"synthetic": True}


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

class PollHA(FakeHA):
    """Serves successive /api/states payloads, then cancels the poll loop."""

    def __init__(self, sequence):
        super().__init__()
        self.sequence = list(sequence)

    def __call__(self, request):
        if not self.sequence:
            raise asyncio.CancelledError()
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return httpx.Response(status, json=payload)


class TestPolling:
    async def test_change_emits_message_and_trigger(self):
        on_trigger = AsyncMock()
        changed = [dict(e) for e in STATES]
        changed[1] = entity("switch.coffee_maker", "on", friendly_name="Coffee Maker")
        changed.append(entity("light.new_bulb", "on"))
        conn = make_ha(PollHA([(200, changed)]), config={"poll_interval_seconds": "0"}, on_trigger=on_trigger)
        conn._entity_states = {e["entity_id"]: e["state"] for e in STATES}
        await conn._poll_states()

        assert replies(conn) == ["Coffee Maker (switch.coffee_maker) changed from off to on"]
        msg = conn._on_incoming_message.await_args.args[0]
        assert msg.metadata == {"entity_id": "switch.coffee_maker", "old_state": "off", "new_state": "on"}
        on_trigger.assert_awaited_once()
        assert on_trigger.await_args.args[1] == "state_changed"
        # Newly seen entities are recorded without firing a change event.
        assert conn._entity_states["light.new_bulb"] == "on"

    async def test_filtered_entities_are_ignored(self):
        changed = [entity("lock.front_door", "unlocked")]
        conn = make_ha(PollHA([(200, changed)]), config={"poll_interval_seconds": "0", "entity_filter": "light."})
        conn._entity_states = {"lock.front_door": "locked"}
        await conn._poll_states()
        conn._on_incoming_message.assert_not_awaited()

    async def test_repeated_failures_escalate(self, monkeypatch):
        from app.db import database

        update = MagicMock()
        monkeypatch.setattr(database, "update_connector_status", update)
        seq = [(500, {}), httpx.ReadTimeout("t"), (502, {}), httpx.ConnectError("x"), (503, {})]
        conn = make_ha(PollHA(seq), config={"poll_interval_seconds": "0"})
        await conn._poll_states()
        assert conn.status == ConnectorStatus.ERROR
        update.assert_called_once_with("ha-1", "error", error="Repeated poll failures (5)")


# ---------------------------------------------------------------------------
# Notifications / tool shim / test_connection
# ---------------------------------------------------------------------------

class TestNotifications:
    async def test_creates_persistent_notification(self):
        api = FakeHA()
        conn = make_ha(api)
        ok = await conn.send_notification("x", "session.complete", {"duration_seconds": 3, "total_cost": 0.01})
        assert ok is True
        path, body = api.posts()[0]
        assert path == "/api/services/persistent_notification/create"
        assert body["title"] == "AI Shuffle: session.complete"
        assert body["message"] == "[Complete] Session Complete\n\nDuration: 3.0s\nCost: $0.0100"
        assert body["notification_id"].startswith("ai_shuffle_")

    async def test_error_and_generic_events(self):
        api = FakeHA()
        conn = make_ha(api)
        await conn.send_notification("x", "session.error", {"error_message": "boom"})
        await conn.send_notification("x", "custom", {"a": 1})
        assert api.posts()[0][1]["message"] == "[Error] Session Error\n\nboom"
        assert api.posts()[1][1]["message"].startswith("[custom] custom")

    async def test_upstream_failure_and_timeout(self):
        route = ("POST", "/api/services/persistent_notification/create")
        assert await make_ha(FakeHA({route: (500, "x")})).send_notification("x", "e", {}) is False
        assert await make_ha(FakeHA({route: httpx.ReadTimeout("t")})).send_notification("x", "e", {}) is False


class TestToolAndTest:
    async def test_execute_tool_mapping(self):
        conn = make_ha()
        conn.execute_action = AsyncMock(return_value={"success": True})
        assert (await conn.execute_tool("x", {}))["error"] == "Unknown tool: x"
        assert (await conn.execute_tool("homeassistant_control", {}))["error"] == "command is required"
        assert (await conn.execute_tool("homeassistant_control", {"command": "dance"}))["error"] == "Unknown command: dance"
        await conn.execute_tool("homeassistant_control", {
            "command": "call_service", "domain": "light", "service": "turn_on", "entity_id": "light.a", "data": {"x": 1},
        })
        conn.execute_action.assert_awaited_with("call_service", {
            "domain": "light", "service": "turn_on", "entity_id": "light.a", "data": {"x": 1},
        })
        await conn.execute_tool("homeassistant_control", {"command": "toggle", "entity_id": "light.a"})
        conn.execute_action.assert_awaited_with("toggle_entity", {"entity_id": "light.a"})
        await conn.execute_tool("homeassistant_control", {"command": "get_states"})
        conn.execute_action.assert_awaited_with("get_states", {})

    @pytest.mark.parametrize("route,expected", [
        ((200, {"version": "2026.9.1", "location_name": "Home"}),
         {"success": True, "message": "Connected to Home Assistant 2026.9.1 (location: Home)"}),
        ((401, {}), {"success": False, "message": "Authentication failed -- check access_token"}),
        ((500, "boom"), {"success": False, "message": "API returned 500: boom"}),
    ])
    async def test_test_connection(self, monkeypatch, route, expected):
        patch_async_client(monkeypatch, FakeHA({("GET", "/api/config"): route}))
        assert await make_ha().test_connection() == expected

    async def test_test_connection_timeout(self, monkeypatch):
        patch_async_client(monkeypatch, FakeHA({("GET", "/api/config"): httpx.ConnectTimeout("no route to host")}))
        res = await make_ha().test_connection()
        assert res == {"success": False, "message": "no route to host"}

    def test_permission_prompt_and_instructions(self):
        conn = make_ha()
        assert "homeassistant_invoke" in conn.get_context_instructions("x")
