"""Spotify action catalog + handlers for the connector action gateway.

Spotify is a non-messaging connector, so its gateway pair (``spotify_actions``
/ ``spotify_invoke``) is registered globally for every session while a
Spotify connector is running (there is no session/channel binding to scope
to). Playback control requires an active Spotify Connect device — the model
should call ``list_devices`` first when playback actions fail.

Legacy action ids (``play``, ``pause``, ``play_track``, ``skip_next``,
``skip_previous``, ``set_volume``, ``get_now_playing``, ``search_tracks``)
and the legacy ``spotify_playback`` / ``spotify_search`` tool ids are
preserved so existing automations keep working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.connectors.base import ActionDef

logger = logging.getLogger(__name__)


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_QUERY = {"type": "string", "description": "Track name, artist, or Spotify URI."}


SPOTIFY_ACTIONS: List[ActionDef] = [
    # -- playback ------------------------------------------------------------
    ActionDef(
        id="play",
        name="Play",
        description="Resume playback, or search for and play a track/URI if a query is given.",
        input_schema=_schema({
            "query": {"type": "string", "description": "Optional track name or Spotify URI to play instead of resuming."},
        }),
        category="playback", risk="write",
    ),
    ActionDef(
        id="pause",
        name="Pause",
        description="Pause playback.",
        input_schema=_schema({}),
        category="playback", risk="write",
    ),
    ActionDef(
        id="play_track",
        name="Play Track",
        description="Search for a track (or play a Spotify URI directly) and start playback.",
        input_schema=_schema({"query": _QUERY}, ["query"]),
        category="playback", risk="write",
    ),
    ActionDef(
        id="skip_next",
        name="Skip Next",
        description="Skip to the next track.",
        input_schema=_schema({}),
        category="playback", risk="write",
    ),
    ActionDef(
        id="skip_previous",
        name="Skip Previous",
        description="Skip to the previous track.",
        input_schema=_schema({}),
        category="playback", risk="write",
    ),
    ActionDef(
        id="set_volume",
        name="Set Volume",
        description="Set playback volume.",
        input_schema=_schema({
            "volume_percent": {"type": "integer", "minimum": 0, "maximum": 100},
        }, ["volume_percent"]),
        category="playback", risk="write",
    ),
    ActionDef(
        id="seek",
        name="Seek",
        description="Seek to a position (in milliseconds) in the current track.",
        input_schema=_schema({"position_ms": {"type": "integer", "minimum": 0}}, ["position_ms"]),
        category="playback", risk="write",
    ),
    ActionDef(
        id="set_shuffle",
        name="Set Shuffle",
        description="Turn shuffle mode on or off.",
        input_schema=_schema({"state": {"type": "boolean"}}, ["state"]),
        category="playback", risk="write",
    ),
    ActionDef(
        id="set_repeat",
        name="Set Repeat",
        description="Set repeat mode: off, track (repeat one), or context (repeat playlist/album).",
        input_schema=_schema({
            "mode": {"type": "string", "enum": ["off", "track", "context"]},
        }, ["mode"]),
        category="playback", risk="write",
    ),
    # -- read ------------------------------------------------------------------
    ActionDef(
        id="get_now_playing",
        name="Get Now Playing",
        description="Get currently playing track info and progress.",
        input_schema=_schema({}),
        category="read", risk="safe",
    ),
    # -- devices -----------------------------------------------------------
    ActionDef(
        id="list_devices",
        name="List Devices",
        description="List available Spotify Connect playback devices.",
        input_schema=_schema({}),
        category="devices", risk="safe",
    ),
    ActionDef(
        id="transfer_playback",
        name="Transfer Playback",
        description="Move playback to a different Spotify Connect device.",
        input_schema=_schema({
            "device_id": {"type": "string"},
            "play": {"type": "boolean", "description": "Start playing immediately on the new device (default true)."},
        }, ["device_id"]),
        category="devices", risk="write",
    ),
    # -- search --------------------------------------------------------------
    ActionDef(
        id="search_tracks",
        name="Search Tracks",
        description="Search for tracks.",
        input_schema=_schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
        category="search", risk="safe",
    ),
    ActionDef(
        id="search",
        name="Search",
        description="Search Spotify for tracks, artists, albums, or playlists.",
        input_schema=_schema({
            "query": {"type": "string"},
            "type": {"type": "string", "enum": ["track", "artist", "album", "playlist"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
        category="search", risk="safe",
    ),
    # -- queue -----------------------------------------------------------------
    ActionDef(
        id="add_to_queue",
        name="Add To Queue",
        description="Add a track to the playback queue.",
        input_schema=_schema({"uri": {"type": "string", "description": "Spotify track URI."}}, ["uri"]),
        category="queue", risk="write",
    ),
    ActionDef(
        id="get_queue",
        name="Get Queue",
        description="Get the currently playing track and the upcoming queue.",
        input_schema=_schema({}),
        category="queue", risk="safe",
    ),
    # -- playlists ---------------------------------------------------------
    ActionDef(
        id="list_playlists",
        name="List Playlists",
        description="List the current user's playlists.",
        input_schema=_schema({}),
        category="playlists", risk="safe",
    ),
    ActionDef(
        id="get_playlist_tracks",
        name="Get Playlist Tracks",
        description="List the tracks in a playlist.",
        input_schema=_schema({"playlist_id": {"type": "string"}}, ["playlist_id"]),
        category="playlists", risk="safe",
    ),
    # -- library -----------------------------------------------------------
    ActionDef(
        id="save_tracks",
        name="Save Tracks",
        description="Save tracks to the user's Liked Songs library.",
        input_schema=_schema({
            "track_ids": {"type": "array", "description": "Spotify track IDs (max 50)."},
        }, ["track_ids"]),
        category="library", risk="write",
    ),
    ActionDef(
        id="get_top_tracks",
        name="Get Top Tracks",
        description="The user's top tracks.",
        input_schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        category="library", risk="safe",
    ),
    ActionDef(
        id="get_top_artists",
        name="Get Top Artists",
        description="The user's top artists.",
        input_schema=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        category="library", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in SPOTIFY_ACTIONS}


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


def _artists(item: Dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists", []) or [])


# -- playback -----------------------------------------------------------------

@_handler("play")
async def _play(conn, params, ctx):
    query = params.get("query")
    if query:
        return await conn._play_track_by_query(str(query).strip())
    await conn._api_put("/me/player/play")
    return _ok(message="Playback resumed")


@_handler("pause")
async def _pause(conn, params, ctx):
    await conn._api_put("/me/player/pause")
    return _ok(message="Playback paused")


@_handler("play_track")
async def _play_track(conn, params, ctx):
    query = str(params.get("query") or "").strip()
    if not query:
        return _err("query is required.")
    return await conn._play_track_by_query(query)


@_handler("skip_next")
async def _skip_next(conn, params, ctx):
    await conn._api_post("/me/player/next")
    return _ok(message="Skipped to next track")


@_handler("skip_previous")
async def _skip_previous(conn, params, ctx):
    await conn._api_post("/me/player/previous")
    return _ok(message="Skipped to previous track")


@_handler("set_volume")
async def _set_volume(conn, params, ctx):
    volume = params.get("volume_percent")
    if volume is None:
        return _err("volume_percent is required.")
    volume_percent = _int(volume, 50, 0, 100)
    await conn._api_put(f"/me/player/volume?volume_percent={volume_percent}")
    return _ok(volume_percent=volume_percent, message=f"Volume set to {volume_percent}%")


@_handler("seek")
async def _seek(conn, params, ctx):
    if params.get("position_ms") is None:
        return _err("position_ms is required.")
    position_ms = max(0, _int(params.get("position_ms"), 0, 0, 10_000_000))
    await conn._api_put(f"/me/player/seek?position_ms={position_ms}")
    return _ok(position_ms=position_ms)


@_handler("set_shuffle")
async def _set_shuffle(conn, params, ctx):
    if "state" not in params:
        return _err("state is required.")
    state = bool(params.get("state"))
    await conn._api_put(f"/me/player/shuffle?state={'true' if state else 'false'}")
    return _ok(shuffle=state)


@_handler("set_repeat")
async def _set_repeat(conn, params, ctx):
    mode = str(params.get("mode") or "").strip().lower()
    if mode not in ("off", "track", "context"):
        return _err("mode must be one of: off, track, context.")
    await conn._api_put(f"/me/player/repeat?state={mode}")
    return _ok(repeat=mode)


# -- read -----------------------------------------------------------------

@_handler("get_now_playing")
async def _get_now_playing(conn, params, ctx):
    return await conn._get_now_playing_info()


# -- devices ----------------------------------------------------------------

@_handler("list_devices")
async def _list_devices(conn, params, ctx):
    resp = await conn._api_get("/me/player/devices")
    devices = resp.json().get("devices", []) or []
    return _ok(devices=[{
        "id": d.get("id"),
        "name": d.get("name"),
        "type": d.get("type"),
        "is_active": d.get("is_active", False),
        "volume_percent": d.get("volume_percent"),
    } for d in devices], count=len(devices))


@_handler("transfer_playback")
async def _transfer_playback(conn, params, ctx):
    device_id = str(params.get("device_id") or "").strip()
    if not device_id:
        return _err("device_id is required.")
    play = bool(params.get("play", True))
    await conn._api_put("/me/player", json={"device_ids": [device_id], "play": play})
    return _ok(device_id=device_id, play=play)


# -- search -------------------------------------------------------------------

@_handler("search_tracks")
async def _search_tracks(conn, params, ctx):
    query = str(params.get("query") or "").strip()
    if not query:
        return _err("query is required.")
    limit = _int(params.get("limit"), 5, 1, 50)
    return await conn._search_tracks_api(query, limit=limit)


@_handler("search")
async def _search(conn, params, ctx):
    query = str(params.get("query") or "").strip()
    if not query:
        return _err("query is required.")
    search_type = str(params.get("type") or "track").strip().lower()
    if search_type not in ("track", "artist", "album", "playlist"):
        return _err("type must be one of: track, artist, album, playlist.")
    limit = _int(params.get("limit"), 10, 1, 50)
    resp = await conn._api_get(f"/search?q={query}&type={search_type}&limit={limit}")
    data = resp.json()
    items = data.get(f"{search_type}s", {}).get("items", []) or []
    results: List[Dict[str, Any]] = []
    for item in items:
        entry: Dict[str, Any] = {"id": item.get("id"), "name": item.get("name"), "uri": item.get("uri")}
        if search_type in ("track", "album"):
            entry["artist"] = _artists(item)
        results.append(entry)
    return _ok(results=results, count=len(results), type=search_type)


# -- queue ------------------------------------------------------------------

@_handler("add_to_queue")
async def _add_to_queue(conn, params, ctx):
    uri = str(params.get("uri") or "").strip()
    if not uri:
        return _err("uri is required.")
    await conn._api_post(f"/me/player/queue?uri={uri}")
    return _ok(queued=uri)


@_handler("get_queue")
async def _get_queue(conn, params, ctx):
    resp = await conn._api_get("/me/player/queue")
    data = resp.json()
    currently_playing = data.get("currently_playing") or {}
    queue = data.get("queue", []) or []
    return _ok(
        currently_playing=currently_playing.get("name"),
        queue=[{"name": t.get("name"), "artist": _artists(t), "uri": t.get("uri")} for t in queue[:20]],
        count=len(queue),
    )


# -- playlists ---------------------------------------------------------------

@_handler("list_playlists")
async def _list_playlists(conn, params, ctx):
    resp = await conn._api_get("/me/playlists?limit=50")
    items = resp.json().get("items", []) or []
    return _ok(playlists=[{
        "id": p.get("id"),
        "name": p.get("name"),
        "tracks_total": (p.get("tracks") or {}).get("total"),
        "owner": (p.get("owner") or {}).get("display_name"),
    } for p in items], count=len(items))


@_handler("get_playlist_tracks")
async def _get_playlist_tracks(conn, params, ctx):
    playlist_id = str(params.get("playlist_id") or "").strip()
    if not playlist_id:
        return _err("playlist_id is required.")
    resp = await conn._api_get(f"/playlists/{playlist_id}/tracks?limit=50")
    items = resp.json().get("items", []) or []
    tracks = []
    for it in items:
        track = it.get("track") or {}
        tracks.append({"name": track.get("name"), "artist": _artists(track), "uri": track.get("uri")})
    return _ok(tracks=tracks, count=len(tracks))


# -- library ------------------------------------------------------------------

@_handler("save_tracks")
async def _save_tracks(conn, params, ctx):
    track_ids = params.get("track_ids")
    if not isinstance(track_ids, list) or not track_ids:
        return _err("track_ids (non-empty array) is required.")
    ids = ",".join(str(t) for t in track_ids[:50])
    await conn._api_put(f"/me/tracks?ids={ids}")
    return _ok(saved=track_ids[:50])


@_handler("get_top_tracks")
async def _get_top_tracks(conn, params, ctx):
    limit = _int(params.get("limit"), 10, 1, 50)
    resp = await conn._api_get(f"/me/top/tracks?limit={limit}")
    items = resp.json().get("items", []) or []
    return _ok(tracks=[{"name": t.get("name"), "artist": _artists(t), "uri": t.get("uri")} for t in items], count=len(items))


@_handler("get_top_artists")
async def _get_top_artists(conn, params, ctx):
    limit = _int(params.get("limit"), 10, 1, 50)
    resp = await conn._api_get(f"/me/top/artists?limit={limit}")
    items = resp.json().get("items", []) or []
    return _ok(artists=[{"name": a.get("name"), "genres": (a.get("genres") or [])[:3]} for a in items], count=len(items))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live SpotifyConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    if getattr(conn, "_http_client", None) is None:
        return _err("Spotify connector is not connected.")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("Spotify action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in SPOTIFY_ACTIONS if a.id not in _HANDLERS]
