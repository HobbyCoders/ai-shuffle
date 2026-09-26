"""Tests for the wave-3c action-gateway conversions: Spotify, Hue.

Covers catalog integrity and legacy-id preservation, the gateway's global
registration mode for these non-messaging connectors, and handler sampling
for each platform against faked httpx clients — no network, no MCP
transport, no real Spotify/Hue API calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.connectors import hue_actions, spotify_actions
from app.core.connectors.hue import HueConnector
from app.core.connectors.spotify import SpotifyConnector
from app.mcp.connector_gateway_sdk import build_gateway_tools, get_gateway_tool_names
from tests.mcp.tool_helpers import tool_result_json


def _resp(status=200, body=None, content=b"x"):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body if body is not None else {})
    r.content = content
    return r


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

class TestCatalogs:
    @pytest.mark.parametrize("mod,cls,legacy_ids", [
        (spotify_actions, SpotifyConnector, {
            "play", "pause", "play_track", "skip_next", "skip_previous",
            "set_volume", "get_now_playing", "search_tracks",
        }),
        (hue_actions, HueConnector, {"set_light", "turn_on", "turn_off", "list_lights"}),
    ])
    def test_catalog_integrity(self, mod, cls, legacy_ids):
        catalog = cls.ACTIONS
        ids = [a.id for a in catalog]
        assert mod.missing_handlers() == []
        assert len(ids) == len(set(ids))
        assert legacy_ids <= set(ids)  # automations keep working
        for a in catalog:
            assert a.risk in ("safe", "write", "destructive"), a.id
            assert a.input_schema.get("type") == "object", a.id
        assert cls.SUPPORTS_ACTION_GATEWAY is True
        assert cls.TOOLS == []

    def test_gateway_names_include_both_pairs(self):
        names = set(get_gateway_tool_names())
        for t in ("spotify", "hue"):
            assert f"mcp__builtins__{t}_actions" in names
            assert f"mcp__builtins__{t}_invoke" in names

    def test_non_messaging_platforms_have_no_destructive_actions(self):
        # Neither Spotify nor Hue has an interactive approval surface, so
        # nothing in their catalogs may require one.
        for catalog in (spotify_actions.SPOTIFY_ACTIONS, hue_actions.HUE_ACTIONS):
            assert [a.id for a in catalog if a.risk == "destructive"] == []


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

def make_spotify(config=None):
    conn = SpotifyConnector(
        connector_id="c1",
        connector_type="spotify",
        config=config or {"client_id": "i", "client_secret": "s", "refresh_token": "r"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._http_client = MagicMock()  # truthy — real calls go through mocked helpers
    conn._api_get = AsyncMock(return_value=_resp())
    conn._api_put = AsyncMock(return_value=_resp())
    conn._api_post = AsyncMock(return_value=_resp())
    return conn


class TestSpotifyHandlers:
    @pytest.mark.asyncio
    async def test_get_now_playing_reports_track(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "item": {
                "name": "Song", "id": "t1", "uri": "spotify:track:t1",
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "duration_ms": 200000,
            },
            "progress_ms": 1000,
        }))
        res = await conn.execute_action("get_now_playing", {}, context={})
        assert res["success"] is True
        assert res["playing"] is True
        assert res["track_name"] == "Song"

    @pytest.mark.asyncio
    async def test_get_now_playing_nothing_playing(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(status=204, content=b""))
        res = await conn.execute_action("get_now_playing", {}, context={})
        assert res["success"] is True
        assert res["playing"] is False

    @pytest.mark.asyncio
    async def test_play_without_query_resumes(self):
        conn = make_spotify()
        res = await conn.execute_action("play", {}, context={})
        assert res["success"] is True
        conn._api_put.assert_awaited_once_with("/me/player/play")

    @pytest.mark.asyncio
    async def test_play_with_query_searches_and_plays(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "tracks": {"items": [{
                "uri": "spotify:track:abc", "name": "Found Song",
                "artists": [{"name": "Someone"}],
            }]},
        }))
        res = await conn.execute_action("play", {"query": "some song"}, context={})
        assert res["success"] is True
        assert res["track_uri"] == "spotify:track:abc"
        conn._api_put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_play_track_requires_query(self):
        conn = make_spotify()
        res = await conn.execute_action("play_track", {}, context={})
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_set_volume_clamps_and_validates(self):
        conn = make_spotify()
        res = await conn.execute_action("set_volume", {"volume_percent": 500}, context={})
        assert res["success"] is True
        assert res["volume_percent"] == 100
        conn._api_put.assert_awaited_once_with("/me/player/volume?volume_percent=100")

        conn2 = make_spotify()
        res2 = await conn2.execute_action("set_volume", {}, context={})
        assert res2["success"] is False

    @pytest.mark.asyncio
    async def test_seek_requires_position_ms(self):
        conn = make_spotify()
        res = await conn.execute_action("seek", {}, context={})
        assert res["success"] is False
        res2 = await conn.execute_action("seek", {"position_ms": 5000}, context={})
        assert res2["success"] is True
        conn._api_put.assert_awaited_once_with("/me/player/seek?position_ms=5000")

    @pytest.mark.asyncio
    async def test_set_repeat_validates_mode(self):
        conn = make_spotify()
        res = await conn.execute_action("set_repeat", {"mode": "bogus"}, context={})
        assert res["success"] is False
        res2 = await conn.execute_action("set_repeat", {"mode": "track"}, context={})
        assert res2["success"] is True

    @pytest.mark.asyncio
    async def test_list_devices(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "devices": [{"id": "d1", "name": "Laptop", "type": "Computer", "is_active": True}],
        }))
        res = await conn.execute_action("list_devices", {}, context={})
        assert res["success"] is True
        assert res["count"] == 1
        assert res["devices"][0]["id"] == "d1"

    @pytest.mark.asyncio
    async def test_transfer_playback_requires_device_id(self):
        conn = make_spotify()
        res = await conn.execute_action("transfer_playback", {}, context={})
        assert res["success"] is False
        res2 = await conn.execute_action("transfer_playback", {"device_id": "d1"}, context={})
        assert res2["success"] is True
        conn._api_put.assert_awaited_once_with("/me/player", json={"device_ids": ["d1"], "play": True})

    @pytest.mark.asyncio
    async def test_search_validates_type(self):
        conn = make_spotify()
        res = await conn.execute_action("search", {"query": "x", "type": "invalid"}, context={})
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_search_tracks_type(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "artists": {"items": [{"id": "a1", "name": "Some Artist", "uri": "spotify:artist:a1"}]},
        }))
        res = await conn.execute_action("search", {"query": "x", "type": "artist"}, context={})
        assert res["success"] is True
        assert res["results"][0]["name"] == "Some Artist"

    @pytest.mark.asyncio
    async def test_add_to_queue_requires_uri(self):
        conn = make_spotify()
        res = await conn.execute_action("add_to_queue", {}, context={})
        assert res["success"] is False
        res2 = await conn.execute_action("add_to_queue", {"uri": "spotify:track:z"}, context={})
        assert res2["success"] is True

    @pytest.mark.asyncio
    async def test_get_queue(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "currently_playing": {"name": "Now"},
            "queue": [{"name": "Next", "artists": [{"name": "A"}], "uri": "u1"}],
        }))
        res = await conn.execute_action("get_queue", {}, context={})
        assert res["success"] is True
        assert res["currently_playing"] == "Now"
        assert res["count"] == 1

    @pytest.mark.asyncio
    async def test_save_tracks_requires_list(self):
        conn = make_spotify()
        res = await conn.execute_action("save_tracks", {"track_ids": "not-a-list"}, context={})
        assert res["success"] is False
        res2 = await conn.execute_action("save_tracks", {"track_ids": ["t1", "t2"]}, context={})
        assert res2["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        conn = make_spotify()
        res = await conn.execute_action("does_not_exist", {}, context={})
        assert res["success"] is False
        assert "Unknown action" in res["error"]

    @pytest.mark.asyncio
    async def test_not_connected(self):
        conn = make_spotify()
        conn._http_client = None
        res = await conn.execute_action("get_now_playing", {}, context={})
        assert res["success"] is False
        assert "not connected" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_playback(self):
        conn = make_spotify()
        res = await conn.execute_tool("spotify_playback", {"command": "pause"})
        assert res["success"] is True
        conn._api_put.assert_awaited_once_with("/me/player/pause")

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_search(self):
        conn = make_spotify()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "tracks": {"items": [{"name": "T", "id": "t1", "uri": "u1", "artists": [], "album": {}}]},
        }))
        res = await conn.execute_tool("spotify_search", {"query": "abc", "limit": 3})
        assert res["success"] is True
        assert res["results"][0]["track_name"] == "T"

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_spotify(config={
            "client_id": "i", "client_secret": "s", "refresh_token": "r",
            "enabled_action_categories": ["playback"],
        })
        avail = await conn.get_action_availability()
        assert avail["play"][0] is True
        ok, reason = avail["list_devices"]
        assert ok is False and "disabled" in reason


# ---------------------------------------------------------------------------
# Hue
# ---------------------------------------------------------------------------

def make_hue(config=None):
    conn = HueConnector(
        connector_id="c1",
        connector_type="hue",
        config=config or {"bridge_ip": "10.0.0.5", "api_key": "k"},
        on_incoming_message=AsyncMock(),
        on_permission_response=AsyncMock(),
    )
    conn._http_client = MagicMock()  # truthy — real calls go through mocked helpers
    conn._lights = {"1": "Kitchen", "2": "Bedroom"}
    conn._api_get = AsyncMock(return_value=_resp())
    conn._api_put = AsyncMock(return_value=_resp())
    return conn


class TestHueHandlers:
    @pytest.mark.asyncio
    async def test_set_light_builds_state(self):
        conn = make_hue()
        res = await conn.execute_action(
            "set_light", {"light_id": "1", "on": True, "brightness": 200}, context={},
        )
        assert res["success"] is True
        conn._api_put.assert_awaited_once_with("/lights/1/state", {"on": True, "bri": 200})

    @pytest.mark.asyncio
    async def test_set_light_requires_state(self):
        conn = make_hue()
        res = await conn.execute_action("set_light", {"light_id": "1"}, context={})
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_turn_on_resolves_light_by_name(self):
        conn = make_hue()
        res = await conn.execute_action("turn_on", {"light_id": "kitchen"}, context={})
        assert res["success"] is True
        assert res["light_id"] == "1"
        conn._api_put.assert_awaited_once_with("/lights/1/state", {"on": True})

    @pytest.mark.asyncio
    async def test_turn_off(self):
        conn = make_hue()
        res = await conn.execute_action("turn_off", {"light_id": "2"}, context={})
        assert res["success"] is True
        assert res["on"] is False

    @pytest.mark.asyncio
    async def test_set_color_temp_validates_preset(self):
        conn = make_hue()
        res = await conn.execute_action(
            "set_color_temp", {"light_id": "1", "preset": "bogus"}, context={},
        )
        assert res["success"] is False
        res2 = await conn.execute_action(
            "set_color_temp", {"light_id": "1", "preset": "warm"}, context={},
        )
        assert res2["success"] is True
        assert res2["mirek"] == 400

    @pytest.mark.asyncio
    async def test_set_effect_validates_effect(self):
        conn = make_hue()
        res = await conn.execute_action(
            "set_effect", {"light_id": "1", "effect": "rainbow"}, context={},
        )
        assert res["success"] is False
        res2 = await conn.execute_action(
            "set_effect", {"light_id": "1", "effect": "colorloop"}, context={},
        )
        assert res2["success"] is True

    @pytest.mark.asyncio
    async def test_set_group_state_requires_group_id(self):
        conn = make_hue()
        res = await conn.execute_action("set_group_state", {"on": True}, context={})
        assert res["success"] is False
        res2 = await conn.execute_action(
            "set_group_state", {"group_id": "0", "on": True}, context={},
        )
        assert res2["success"] is True
        conn._api_put.assert_awaited_once_with("/groups/0/action", {"on": True})

    @pytest.mark.asyncio
    async def test_activate_scene(self):
        conn = make_hue()
        res = await conn.execute_action("activate_scene", {"scene_id": "abc123"}, context={})
        assert res["success"] is True
        conn._api_put.assert_awaited_once_with("/groups/0/action", {"scene": "abc123"})

    @pytest.mark.asyncio
    async def test_list_lights(self):
        conn = make_hue()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "1": {"name": "Kitchen", "state": {"on": True, "bri": 200, "reachable": True}},
        }))
        res = await conn.execute_action("list_lights", {}, context={})
        assert res["success"] is True
        assert res["count"] == 1

    @pytest.mark.asyncio
    async def test_list_groups(self):
        conn = make_hue()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "1": {"name": "Living Room", "type": "Room", "lights": ["1", "2"], "state": {"all_on": True, "any_on": True}},
        }))
        res = await conn.execute_action("list_groups", {}, context={})
        assert res["success"] is True
        assert res["groups"][0]["name"] == "Living Room"

    @pytest.mark.asyncio
    async def test_list_scenes(self):
        conn = make_hue()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "s1": {"name": "Movie Night", "group": "1"},
        }))
        res = await conn.execute_action("list_scenes", {}, context={})
        assert res["success"] is True
        assert res["scenes"][0]["name"] == "Movie Night"

    @pytest.mark.asyncio
    async def test_get_bridge_config(self):
        conn = make_hue()
        conn._api_get = AsyncMock(return_value=_resp(body={
            "name": "Bridge", "modelid": "BSB002", "apiversion": "1.50.0", "swversion": "1953188020",
        }))
        res = await conn.execute_action("get_bridge_config", {}, context={})
        assert res["success"] is True
        assert res["config"]["name"] == "Bridge"

    @pytest.mark.asyncio
    async def test_bridge_error_status_surfaces(self):
        conn = make_hue()
        conn._api_put = AsyncMock(return_value=_resp(status=403))
        res = await conn.execute_action("turn_on", {"light_id": "1"}, context={})
        assert res["success"] is False
        assert "403" in res["error"]

    @pytest.mark.asyncio
    async def test_not_connected(self):
        conn = make_hue()
        conn._http_client = None
        res = await conn.execute_action("list_lights", {}, context={})
        assert res["success"] is False
        assert "not connected" in res["error"]

    @pytest.mark.asyncio
    async def test_legacy_execute_tool_hue_control(self):
        conn = make_hue()
        res = await conn.execute_tool(
            "hue_control", {"command": "set_light", "light_id": "1", "on": True},
        )
        assert res["success"] is True
        conn._api_put.assert_awaited_once_with("/lights/1/state", {"on": True})

    @pytest.mark.asyncio
    async def test_category_gating(self):
        conn = make_hue(config={
            "bridge_ip": "10.0.0.5", "api_key": "k",
            "enabled_action_categories": ["discovery"],
        })
        avail = await conn.get_action_availability()
        assert avail["list_lights"][0] is True
        ok, reason = avail["turn_on"]
        assert ok is False and "disabled" in reason


# ---------------------------------------------------------------------------
# Gateway registration — global mode (non-messaging connectors)
# ---------------------------------------------------------------------------

def _patched_db():
    return patch.multiple(
        "app.db.database",
        get_connector_sessions_for_ai_session=MagicMock(return_value=[]),
        get_connector=MagicMock(return_value=None),
    )


class TestRegistration:
    def test_spotify_and_hue_register_globally(self):
        sp = make_spotify()
        hu = make_hue()

        def fake_running(ctype=None):
            pairs = [("sp1", sp), ("hu1", hu)]
            if ctype is None:
                return pairs
            return [(cid, c) for cid, c in pairs if c.connector_type == ctype]

        with _patched_db(), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            tools = build_gateway_tools("ai-1")
        assert {t.name for t in tools} == {
            "spotify_actions", "spotify_invoke",
            "hue_actions", "hue_invoke",
        }

    @pytest.mark.asyncio
    async def test_global_invoke_dispatches_to_first_running_instance(self):
        sp = make_spotify()

        def fake_running(ctype=None):
            return [("sp1", sp)] if ctype in (None, "spotify") else []

        with _patched_db(), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = tool_result_json(await tools["spotify_invoke"]({
                "action": "pause", "params": {},
            }))
        assert res["success"] is True
        sp._api_put.assert_awaited_once_with("/me/player/pause")

    @pytest.mark.asyncio
    async def test_global_actions_lists_hue_catalog(self):
        hu = make_hue()

        def fake_running(ctype=None):
            return [("hu1", hu)] if ctype in (None, "hue") else []

        with _patched_db(), patch(
            "app.core.connectors.manager.connector_manager.get_running_connectors",
            side_effect=fake_running,
        ):
            tools = {t.name: t.handler for t in build_gateway_tools("ai-1")}
            res = tool_result_json(await tools["hue_actions"]({}))
        assert res["success"] is True
        ids = {a["id"] for a in res["actions"]}
        assert {"set_light", "turn_on", "list_lights", "activate_scene"} <= ids
