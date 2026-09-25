"""SpotifyConnector against a fake Spotify Web API (httpx MockTransport).

Unlike tests/mcp/test_connector_gateway_wave3_c.py, which stubs the
connector's _api_* helpers, these run the real token refresh and request
helpers against canned accounts.spotify.com / api.spotify.com responses:
token caching and expiry, auth headers, 4xx/5xx/429 and timeouts surfacing
through raise_for_status, the text-command parser, the playback poller,
notifications, the legacy tool shim and test_connection. No network.
"""

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.connectors import spotify as spotify_module
from app.core.connectors.base import ConnectorStatus, OutgoingMessage
from app.core.connectors.spotify import SpotifyConnector
from tests.fakes.http import mock_transport_client

CONFIG = {"client_id": "cid", "client_secret": "csecret", "refresh_token": "rtok"}


def track(tid, name, artists, album="Album", uri=None, duration_ms=215000):
    return {
        "id": tid, "name": name, "uri": uri or f"spotify:track:{tid}",
        "artists": [{"name": a} for a in artists], "album": {"name": album},
        "duration_ms": duration_ms,
    }


SONG = track("4uLU6hMCjMI75M1A2tKUQC", "Never Gonna Give You Up", ["Rick Astley"], "Whenever You Need Somebody")


class FakeSpotify:
    """Records requests; routes keyed by (method, path) -> (status, json|None) | Exception."""

    def __init__(self, routes=None, token_expires_in=3600):
        self.token_calls = 0
        self.token_expires_in = token_expires_in
        self.routes = {
            ("GET", "/v1/me"): (200, {"display_name": "Rick", "id": "rick"}),
            ("GET", "/v1/search"): (200, {"tracks": {"items": [SONG], "total": 1}}),
            ("GET", "/v1/me/player/currently-playing"): (200, {"is_playing": True, "progress_ms": 61000, "item": SONG}),
            ("GET", "/v1/me/player/devices"): (200, {"devices": [
                {"id": "d1", "name": "Kitchen Echo", "type": "Speaker", "is_active": True},
                {"id": "d2", "name": "MacBook", "type": "Computer", "is_active": False},
            ]}),
            ("GET", "/v1/me/top/tracks"): (200, {"items": [SONG, track("t2", "Song 2", ["Blur"])]}),
            ("GET", "/v1/me/top/artists"): (200, {"items": [
                {"name": "Rick Astley", "genres": ["dance pop", "new wave pop", "pop", "soft rock"]},
                {"name": "Unknown Band", "genres": []},
            ]}),
        }
        self.routes.update(routes or {})
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            self.token_calls += 1
            route = self.routes.get(("POST", "token"))
            if isinstance(route, Exception):
                raise route
            if route is not None:
                return httpx.Response(route[0], json=route[1])
            expected = base64.b64encode(b"cid:csecret").decode()
            assert request.headers["authorization"] == f"Basic {expected}"
            form = parse_qs(request.content.decode())
            assert form == {"grant_type": ["refresh_token"], "refresh_token": ["rtok"]}
            return httpx.Response(200, json={
                "access_token": f"at-{self.token_calls}", "token_type": "Bearer",
                "expires_in": self.token_expires_in, "scope": "user-read-playback-state",
            })

        assert request.url.host == "api.spotify.com"
        assert request.headers["authorization"].startswith("Bearer at-")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body))
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            if request.method in ("PUT", "POST"):
                return httpx.Response(204)
            return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})
        if isinstance(route, Exception):
            raise route
        status, payload = route[0], route[1]
        headers = route[2] if len(route) > 2 else None
        if payload is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    def calls(self, method=None):
        return [(m, p, q, b) for m, p, q, b in self.requests if method is None or m == method]


def make_spotify(api=None, *, config=None, on_trigger=None):
    conn = SpotifyConnector(
        connector_id="sp-1", connector_type="spotify",
        config={**CONFIG, **(config or {})},
        on_incoming_message=AsyncMock(), on_permission_response=AsyncMock(),
        on_trigger=on_trigger,
    )
    if api is not None:
        conn._http_client = mock_transport_client(api)
    return conn


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def patch_async_client(monkeypatch, api):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(api))

    monkeypatch.setattr(spotify_module.httpx, "AsyncClient", factory)


def replies(conn):
    return [c.args[0].text for c in conn._on_incoming_message.await_args_list]


async def say(conn, text):
    return await conn.send_message(OutgoingMessage(platform_chat_id="chat-1", text=text))


# ---------------------------------------------------------------------------
# Token handling and request helpers
# ---------------------------------------------------------------------------

class TestTokens:
    async def test_token_is_fetched_once_and_reused(self):
        api = FakeSpotify()
        conn = make_spotify(api)
        await conn._api_get("/me")
        await conn._api_put("/me/player/pause")
        await conn._api_post("/me/player/next")
        assert api.token_calls == 1
        assert conn._access_token == "at-1"
        assert conn._token_expires_at > time.time() + 3000

    async def test_token_near_expiry_is_refreshed(self):
        api = FakeSpotify()
        conn = make_spotify(api)
        conn._access_token = "stale"
        conn._token_expires_at = time.time() + 30  # inside the 60s safety window
        await conn._api_get("/me")
        assert api.token_calls == 1 and conn._access_token == "at-1"

    async def test_refresh_rejected_raises(self):
        api = FakeSpotify({("POST", "token"): (400, {"error": "invalid_grant", "error_description": "Refresh token revoked"})})
        conn = make_spotify(api)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await conn._api_get("/me")
        assert exc.value.response.status_code == 400
        assert api.requests == []  # no API call made without a token

    @pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502])
    async def test_api_errors_raise_http_status_error(self, status):
        api = FakeSpotify({("GET", "/v1/me"): (status, {"error": {"status": status, "message": "nope"}}, {"Retry-After": "3"})})
        conn = make_spotify(api)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await conn._api_get("/me")
        assert exc.value.response.status_code == status
        # No retry/backoff: exactly one attempt reaches the API.
        assert len(api.calls("GET")) == 1

    async def test_timeout_propagates(self):
        api = FakeSpotify({("PUT", "/v1/me/player/play"): httpx.ReadTimeout("slow")})
        conn = make_spotify(api)
        with pytest.raises(httpx.ReadTimeout):
            await conn._api_put("/me/player/play")


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

class TestConnect:
    async def test_connect_authenticates_and_polls(self, monkeypatch):
        api = FakeSpotify({("GET", "/v1/me/player"): (204, None)})
        patch_async_client(monkeypatch, api)
        conn = make_spotify(config={"poll_interval_seconds": "3600"})
        assert await conn.connect() is True
        assert conn.status == ConnectorStatus.CONNECTED
        assert conn._access_token == "at-1"
        assert conn._poll_task is not None
        await conn.disconnect()
        assert conn._http_client is None and conn.status == ConnectorStatus.DISCONNECTED

    async def test_connect_with_bad_refresh_token_fails(self, monkeypatch):
        patch_async_client(monkeypatch, FakeSpotify({("POST", "token"): (400, {"error": "invalid_grant"})}))
        conn = make_spotify()
        assert await conn.connect() is False
        assert conn.status == ConnectorStatus.ERROR
        assert "400" in conn.status_message

    async def test_connect_without_httpx(self, monkeypatch):
        monkeypatch.setattr(SpotifyConnector, "_SDK_AVAILABLE", False)
        conn = make_spotify()
        assert await conn.connect() is False
        assert conn.status_message == "httpx is not installed"


# ---------------------------------------------------------------------------
# Text commands
# ---------------------------------------------------------------------------

class TestCommands:
    @pytest.mark.parametrize("text,method,path,params", [
        ("play", "PUT", "/v1/me/player/play", {}),
        ("Resume", "PUT", "/v1/me/player/play", {}),
        ("pause", "PUT", "/v1/me/player/pause", {}),
        ("skip", "POST", "/v1/me/player/next", {}),
        ("prev", "POST", "/v1/me/player/previous", {}),
        ("volume 150", "PUT", "/v1/me/player/volume", {"volume_percent": "100"}),
        ("volume -5", "PUT", "/v1/me/player/volume", {"volume_percent": "0"}),
        ("shuffle on", "PUT", "/v1/me/player/shuffle", {"state": "true"}),
        ("shuffle maybe", "PUT", "/v1/me/player/shuffle", {"state": "false"}),
    ])
    async def test_playback_controls(self, text, method, path, params):
        api = FakeSpotify()
        conn = make_spotify(api)
        assert await say(conn, text) is True
        assert [(m, p, q) for m, p, q, _ in api.requests] == [(method, path, params)]

    async def test_volume_not_a_number(self):
        api = FakeSpotify()
        assert await say(make_spotify(api), "volume loud") is False
        assert api.requests == []

    async def test_play_query_searches_then_plays_first_hit(self):
        api = FakeSpotify()
        conn = make_spotify(api)
        assert await say(conn, "play Never Gonna Give You Up") is True
        (_, _, search_q, _), (method, path, _, body) = api.requests
        assert search_q["q"] == "Never Gonna Give You Up" and search_q["limit"] == "1"
        assert (method, path, body) == ("PUT", "/v1/me/player/play", {"uris": [SONG["uri"]]})

    async def test_play_query_no_results(self):
        api = FakeSpotify({("GET", "/v1/search"): (200, {"tracks": {"items": []}})})
        conn = make_spotify(api)
        assert await say(conn, "play zzzz") is True
        assert replies(conn) == ["No tracks found for: zzzz"]
        assert api.calls("PUT") == []

    async def test_no_active_device_returns_false(self):
        # Spotify answers 404 NO_ACTIVE_DEVICE when nothing is open.
        api = FakeSpotify({("PUT", "/v1/me/player/play"): (404, {"error": {
            "status": 404, "message": "Player command failed: No active device found", "reason": "NO_ACTIVE_DEVICE",
        }})})
        assert await say(make_spotify(api), "play") is False

    async def test_rate_limited_returns_false(self):
        api = FakeSpotify({("POST", "/v1/me/player/next"): (429, {"error": {"status": 429}}, {"Retry-After": "2"})})
        assert await say(make_spotify(api), "next") is False

    async def test_devices(self):
        conn = make_spotify(FakeSpotify())
        assert await say(conn, "devices") is True
        assert replies(conn) == ["Devices:\n  - Kitchen Echo (Speaker) (active)\n  - MacBook (Computer)"]
        conn = make_spotify(FakeSpotify({("GET", "/v1/me/player/devices"): (200, {"devices": []})}))
        assert await say(conn, "devices") is True
        assert replies(conn) == ["No active devices found."]

    async def test_now_playing(self):
        conn = make_spotify(FakeSpotify())
        assert await say(conn, "now playing") is True
        assert replies(conn) == [
            "Now playing: Never Gonna Give You Up\nArtist: Rick Astley\n"
            "Album: Whenever You Need Somebody\nProgress: 1:01 / 3:35"
        ]

    @pytest.mark.parametrize("route", [(204, None), (200, {"is_playing": False, "item": None})])
    async def test_now_playing_nothing(self, route):
        conn = make_spotify(FakeSpotify({("GET", "/v1/me/player/currently-playing"): route}))
        assert await say(conn, "now") is True
        assert replies(conn) == ["Nothing is currently playing."]

    async def test_top_tracks_and_artists(self):
        conn = make_spotify(FakeSpotify())
        assert await say(conn, "top tracks") is True
        assert await say(conn, "top artists") is True
        tracks, artists = replies(conn)
        assert tracks.splitlines() == ["Your Top Tracks:", "  1. Never Gonna Give You Up - Rick Astley", "  2. Song 2 - Blur"]
        assert artists.splitlines() == ["Your Top Artists:", "  1. Rick Astley (dance pop, new wave pop, pop)", "  2. Unknown Band"]

    async def test_top_lists_empty(self):
        api = FakeSpotify({("GET", "/v1/me/top/tracks"): (200, {"items": []}),
                           ("GET", "/v1/me/top/artists"): (200, {"items": []})})
        conn = make_spotify(api)
        await say(conn, "top tracks")
        await say(conn, "top artists")
        assert replies(conn) == ["No top tracks found.", "No top artists found."]

    async def test_search(self):
        api = FakeSpotify()
        conn = make_spotify(api)
        assert await say(conn, "search rick astley") is True
        assert api.requests[0][2]["limit"] == "5"
        assert replies(conn)[0] == (
            'Search results for "rick astley":\n'
            "  1. Never Gonna Give You Up - Rick Astley (Whenever You Need Somebody)"
        )
        conn = make_spotify(FakeSpotify({("GET", "/v1/search"): (200, {"tracks": {"items": []}})}))
        assert await say(conn, "search nothing") is True
        assert replies(conn) == ["No results found for: nothing"]

    async def test_unrecognised_and_no_client(self):
        assert await say(make_spotify(FakeSpotify()), "dance") is False
        assert await say(make_spotify(), "play") is False


# ---------------------------------------------------------------------------
# Structured helpers used by spotify_actions
# ---------------------------------------------------------------------------

class TestHelpers:
    async def test_now_playing_info(self):
        info = await make_spotify(FakeSpotify())._get_now_playing_info()
        assert info == {
            "success": True, "playing": True, "track_name": SONG["name"], "artist": "Rick Astley",
            "album": "Whenever You Need Somebody", "track_id": SONG["id"], "track_uri": SONG["uri"],
            "progress_ms": 61000, "duration_ms": 215000,
        }
        idle = await make_spotify(FakeSpotify({("GET", "/v1/me/player/currently-playing"): (204, None)}))._get_now_playing_info()
        assert idle["playing"] is False

    async def test_search_tracks_api_limit_and_empty(self):
        api = FakeSpotify()
        res = await make_spotify(api)._search_tracks_api("rick", limit=3)
        assert api.requests[0][2] == {"q": "rick", "type": "track", "limit": "3"}
        assert res["results"][0]["artist"] == "Rick Astley"
        empty = await make_spotify(FakeSpotify({("GET", "/v1/search"): (200, {"tracks": {"items": []}})}))._search_tracks_api("x")
        assert empty == {"success": True, "results": [], "message": "No results found for: x"}

    async def test_play_track_by_uri_skips_search(self):
        api = FakeSpotify()
        res = await make_spotify(api)._play_track_by_query("spotify:track:abc")
        assert res == {"success": True, "message": "Playing URI: spotify:track:abc"}
        assert [(m, p) for m, p, _, _ in api.requests] == [("PUT", "/v1/me/player/play")]

    async def test_play_track_by_query_not_found(self):
        api = FakeSpotify({("GET", "/v1/search"): (200, {"tracks": {"items": []}})})
        res = await make_spotify(api)._play_track_by_query("zzz")
        assert res == {"success": False, "error": "No tracks found for: zzz"}

    @pytest.mark.xfail(strict=True, reason=(
        "bug: search queries are interpolated into the URL unencoded, so '&' splits the "
        "query and '#' truncates it (and drops type/limit) — Spotify then returns 400"
    ))
    async def test_search_query_is_url_encoded(self):
        api = FakeSpotify()
        await make_spotify(api)._search_tracks_api("Simon & Garfunkel #1", limit=5)
        assert api.requests[0][2] == {"q": "Simon & Garfunkel #1", "type": "track", "limit": "5"}


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

class PollSpotify(FakeSpotify):
    """Serves successive /me/player payloads, then cancels the poll loop."""

    def __init__(self, sequence):
        super().__init__()
        self.sequence = list(sequence)

    def __call__(self, request):
        if request.url.host == "accounts.spotify.com":
            return super().__call__(request)
        if not self.sequence:
            raise asyncio.CancelledError()
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)


class TestPolling:
    async def test_track_change_and_play_pause_triggers(self):
        on_trigger = AsyncMock()
        other = track("t2", "Song 2", ["Blur"], "Blur")
        seq = [
            (200, {"is_playing": False, "item": SONG}),   # first observation: no transition
            (200, {"is_playing": True, "item": SONG}),    # started
            (200, {"is_playing": True, "item": other}),   # track changed
            (204, None),                                  # nothing playing: ignored
            (200, {"is_playing": False, "item": other}),  # paused
        ]
        conn = make_spotify(PollSpotify(seq), config={"poll_interval_seconds": "0"}, on_trigger=on_trigger)
        await conn._poll_playback()

        fired = [(c.args[1], c.args[2]) for c in on_trigger.await_args_list]
        names = [f[0] for f in fired]
        assert names == ["track_changed", "playback_started", "track_changed", "playback_paused"]
        assert fired[1][1] == {"track_name": SONG["name"], "artist": "Rick Astley", "album": "Whenever You Need Somebody"}
        assert fired[2][1]["track_uri"] == "spotify:track:t2"
        assert replies(conn) == [
            "Now playing: Never Gonna Give You Up by Rick Astley (Whenever You Need Somebody)",
            "Now playing: Song 2 by Blur (Blur)",
        ]
        assert conn._on_incoming_message.await_args.args[0].platform_message_id == "t2"

    async def test_repeated_failures_escalate(self, monkeypatch):
        from app.db import database

        update = MagicMock()
        monkeypatch.setattr(database, "update_connector_status", update)
        seq = [(500, {}), (502, {}), httpx.ReadTimeout("t"), (429, {}), (503, {"error": "down"})]
        conn = make_spotify(PollSpotify(seq), config={"poll_interval_seconds": "0"})
        await conn._poll_playback()
        assert conn.status == ConnectorStatus.ERROR
        assert conn.status_message.startswith("Repeated poll failures:")
        assert "503" in conn.status_message
        update.assert_called_once()
        assert update.call_args.args[:2] == ("sp-1", "error")


# ---------------------------------------------------------------------------
# Notifications / tool shim / test_connection
# ---------------------------------------------------------------------------

class TestMisc:
    async def test_notification_and_prompt(self):
        conn = make_spotify()
        assert await conn.send_notification("chat-9", "session.complete", {"cost": 1}) is True
        msg = conn._on_incoming_message.await_args.args[0]
        assert msg.platform_chat_id == "chat-9"
        assert msg.text == "[session.complete] {'cost': 1}"
        assert await conn.send_permission_prompt("x", MagicMock()) is False

    async def test_notification_callback_failure_returns_false(self):
        conn = make_spotify()
        conn._on_incoming_message.side_effect = RuntimeError("bus down")
        assert await conn.send_notification("chat-9", "e", {}) is False

    async def test_execute_tool_legacy_shim(self):
        conn = make_spotify()
        conn.execute_action = AsyncMock(return_value={"success": True})
        assert (await conn.execute_tool("spotify_playback", {}))["error"] == "Missing required parameter: command"
        await conn.execute_tool("spotify_playback", {"command": "set_volume", "volume": 20})
        conn.execute_action.assert_awaited_with("set_volume", {"volume": 20})
        await conn.execute_tool("spotify_search", {"query": "x"})
        conn.execute_action.assert_awaited_with("search_tracks", {"query": "x"})
        assert (await conn.execute_tool("nope", {}))["error"] == "Unknown tool: nope"

    async def test_test_connection(self, monkeypatch):
        patch_async_client(monkeypatch, FakeSpotify())
        conn = make_spotify()
        sentinel = object()
        conn._http_client = sentinel
        assert await conn.test_connection() == {"success": True, "message": "Authenticated as Rick"}
        assert conn._http_client is sentinel  # the live client is restored

    async def test_test_connection_failures(self, monkeypatch):
        patch_async_client(monkeypatch, FakeSpotify({("GET", "/v1/me"): (401, {"error": "bad token"})}))
        res = await make_spotify().test_connection()
        assert res["success"] is False and "401" in res["message"]
        patch_async_client(monkeypatch, FakeSpotify({("POST", "token"): httpx.ConnectTimeout("dns")}))
        assert await make_spotify().test_connection() == {"success": False, "message": "dns"}
        monkeypatch.setattr(spotify_module, "HTTPX_AVAILABLE", False)
        assert await make_spotify().test_connection() == {"success": False, "message": "httpx is not installed"}
