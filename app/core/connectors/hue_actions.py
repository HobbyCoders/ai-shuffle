"""Philips Hue action catalog + handlers for the connector action gateway.

Hue is a non-messaging connector, so its gateway pair (``hue_actions`` /
``hue_invoke``) is registered globally for every session while a Hue
connector is running (there is no session/channel binding to scope to).

Legacy action ids (``set_light``, ``turn_on``, ``turn_off``, ``list_lights``)
and the legacy ``hue_control`` tool id are preserved so existing automations
keep working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.connectors.base import ActionDef

logger = logging.getLogger(__name__)

# Color temperature presets (mirek values). Kept as a local copy — see the
# duplicate in hue.py, which is used by the legacy free-text command parser.
_COLOR_TEMP_PRESETS: Dict[str, int] = {
    "warm": 400,
    "neutral": 300,
    "cool": 200,
}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_LIGHT_STATE_PROPS = {
    "light_id": {"type": "string"},
    "on": {"type": "boolean"},
    "brightness": {"type": "integer", "minimum": 0, "maximum": 254},
    "hue": {"type": "integer", "minimum": 0, "maximum": 65535},
    "saturation": {"type": "integer", "minimum": 0, "maximum": 254},
}


HUE_ACTIONS: List[ActionDef] = [
    # -- control --------------------------------------------------------------
    ActionDef(
        id="set_light",
        name="Set Light",
        description="Set a light's on/off state, brightness, hue, and/or saturation.",
        input_schema=_schema(_LIGHT_STATE_PROPS, ["light_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="turn_on",
        name="Turn On",
        description="Turn a light on.",
        input_schema=_schema({"light_id": {"type": "string"}}, ["light_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="turn_off",
        name="Turn Off",
        description="Turn a light off.",
        input_schema=_schema({"light_id": {"type": "string"}}, ["light_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="set_color_temp",
        name="Set Color Temperature",
        description="Set a light's white color temperature to a named preset.",
        input_schema=_schema({
            "light_id": {"type": "string"},
            "preset": {"type": "string", "enum": ["warm", "neutral", "cool"]},
        }, ["light_id", "preset"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="set_effect",
        name="Set Effect",
        description="Set a light's dynamic effect.",
        input_schema=_schema({
            "light_id": {"type": "string"},
            "effect": {"type": "string", "enum": ["none", "colorloop"]},
        }, ["light_id", "effect"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="set_group_state",
        name="Set Group State",
        description=(
            "Set on/off, brightness, hue, and/or saturation for a whole room/group "
            "at once. Use group_id '0' to control all lights."
        ),
        input_schema=_schema({
            "group_id": {"type": "string"},
            "on": {"type": "boolean"},
            "brightness": {"type": "integer", "minimum": 0, "maximum": 254},
            "hue": {"type": "integer", "minimum": 0, "maximum": 65535},
            "saturation": {"type": "integer", "minimum": 0, "maximum": 254},
        }, ["group_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="activate_scene",
        name="Activate Scene",
        description="Activate a scene by ID (discover IDs with list_scenes).",
        input_schema=_schema({"scene_id": {"type": "string"}}, ["scene_id"]),
        category="control", risk="write",
    ),
    # -- discovery --------------------------------------------------------------
    ActionDef(
        id="list_lights",
        name="List Lights",
        description="List all lights with their current state.",
        input_schema=_schema({}),
        category="discovery", risk="safe",
    ),
    ActionDef(
        id="get_light",
        name="Get Light",
        description="Get the full state of one light.",
        input_schema=_schema({"light_id": {"type": "string"}}, ["light_id"]),
        category="discovery", risk="safe",
    ),
    ActionDef(
        id="list_groups",
        name="List Groups",
        description="List all rooms/groups with their state.",
        input_schema=_schema({}),
        category="discovery", risk="safe",
    ),
    ActionDef(
        id="list_scenes",
        name="List Scenes",
        description="List all scenes.",
        input_schema=_schema({}),
        category="discovery", risk="safe",
    ),
    ActionDef(
        id="get_bridge_config",
        name="Get Bridge Config",
        description="Get the bridge's name, model, and software version.",
        input_schema=_schema({}),
        category="discovery", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in HUE_ACTIONS}


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


def _resolve_light(conn: Any, light_query: str) -> str:
    return conn._find_light(light_query) or light_query


async def _set_light_state(conn: Any, light_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    resp = await conn._api_put(f"/lights/{light_id}/state", state)
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    return _ok(light_id=light_id, state=state)


# -- control ------------------------------------------------------------------

@_handler("set_light")
async def _h_set_light(conn, params, ctx):
    light_query = str(params.get("light_id") or "").strip()
    if not light_query:
        return _err("light_id is required.")
    state: Dict[str, Any] = {}
    if "on" in params:
        state["on"] = bool(params["on"])
    if "brightness" in params:
        state["bri"] = int(params["brightness"])
    if "hue" in params:
        state["hue"] = int(params["hue"])
    if "saturation" in params:
        state["sat"] = int(params["saturation"])
    if not state:
        return _err("No state parameters provided — pass on/brightness/hue/saturation.")
    return await _set_light_state(conn, _resolve_light(conn, light_query), state)


@_handler("turn_on")
async def _h_turn_on(conn, params, ctx):
    light_query = str(params.get("light_id") or "").strip()
    if not light_query:
        return _err("light_id is required.")
    result = await _set_light_state(conn, _resolve_light(conn, light_query), {"on": True})
    if result.get("success"):
        result["on"] = True
    return result


@_handler("turn_off")
async def _h_turn_off(conn, params, ctx):
    light_query = str(params.get("light_id") or "").strip()
    if not light_query:
        return _err("light_id is required.")
    result = await _set_light_state(conn, _resolve_light(conn, light_query), {"on": False})
    if result.get("success"):
        result["on"] = False
    return result


@_handler("set_color_temp")
async def _h_set_color_temp(conn, params, ctx):
    light_query = str(params.get("light_id") or "").strip()
    preset = str(params.get("preset") or "").strip().lower()
    if not light_query:
        return _err("light_id is required.")
    ct_value = _COLOR_TEMP_PRESETS.get(preset)
    if ct_value is None:
        return _err("preset must be one of: warm, neutral, cool.")
    light_id = _resolve_light(conn, light_query)
    result = await _set_light_state(conn, light_id, {"ct": ct_value})
    if result.get("success"):
        result["preset"] = preset
        result["mirek"] = ct_value
    return result


@_handler("set_effect")
async def _h_set_effect(conn, params, ctx):
    light_query = str(params.get("light_id") or "").strip()
    effect = str(params.get("effect") or "").strip().lower()
    if not light_query:
        return _err("light_id is required.")
    if effect not in ("none", "colorloop"):
        return _err("effect must be one of: none, colorloop.")
    light_id = _resolve_light(conn, light_query)
    return await _set_light_state(conn, light_id, {"effect": effect})


@_handler("set_group_state")
async def _h_set_group_state(conn, params, ctx):
    group_id = str(params.get("group_id") or "").strip()
    if not group_id:
        return _err("group_id is required.")
    state: Dict[str, Any] = {}
    if "on" in params:
        state["on"] = bool(params["on"])
    if "brightness" in params:
        state["bri"] = int(params["brightness"])
    if "hue" in params:
        state["hue"] = int(params["hue"])
    if "saturation" in params:
        state["sat"] = int(params["saturation"])
    if not state:
        return _err("No state parameters provided — pass on/brightness/hue/saturation.")
    resp = await conn._api_put(f"/groups/{group_id}/action", state)
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    return _ok(group_id=group_id, state=state)


@_handler("activate_scene")
async def _h_activate_scene(conn, params, ctx):
    scene_id = str(params.get("scene_id") or "").strip()
    if not scene_id:
        return _err("scene_id is required.")
    resp = await conn._api_put("/groups/0/action", {"scene": scene_id})
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    return _ok(scene_id=scene_id, activated=True)


# -- discovery ----------------------------------------------------------------

@_handler("list_lights")
async def _h_list_lights(conn, params, ctx):
    resp = await conn._api_get("/lights")
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    data = resp.json()
    if isinstance(data, list):
        return _err("Bridge returned an error response.")
    lights = []
    for lid, info in data.items():
        state = info.get("state", {})
        lights.append({
            "id": lid,
            "name": info.get("name", f"Light {lid}"),
            "on": state.get("on", False),
            "brightness": state.get("bri", 0),
            "reachable": state.get("reachable", False),
        })
    return _ok(lights=lights, count=len(lights))


@_handler("get_light")
async def _h_get_light(conn, params, ctx):
    light_query = str(params.get("light_id") or "").strip()
    if not light_query:
        return _err("light_id is required.")
    light_id = _resolve_light(conn, light_query)
    resp = await conn._api_get(f"/lights/{light_id}")
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    info = resp.json()
    if not isinstance(info, dict) or "state" not in info:
        return _err(f"Light not found: {light_query}")
    return _ok(light_id=light_id, name=info.get("name"), state=info.get("state", {}))


@_handler("list_groups")
async def _h_list_groups(conn, params, ctx):
    resp = await conn._api_get("/groups")
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    data = resp.json()
    if isinstance(data, list):
        return _err("Bridge returned an error response.")
    groups = []
    for gid, info in data.items():
        state = info.get("state", {})
        groups.append({
            "id": gid,
            "name": info.get("name", f"Group {gid}"),
            "type": info.get("type", ""),
            "light_count": len(info.get("lights", [])),
            "all_on": state.get("all_on", False),
            "any_on": state.get("any_on", False),
        })
    return _ok(groups=groups, count=len(groups))


@_handler("list_scenes")
async def _h_list_scenes(conn, params, ctx):
    resp = await conn._api_get("/scenes")
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    data = resp.json()
    if isinstance(data, list):
        return _err("Bridge returned an error response.")
    scenes = [
        {"id": sid, "name": info.get("name", f"Scene {sid}"), "group": info.get("group", "?")}
        for sid, info in data.items()
    ]
    return _ok(scenes=scenes, count=len(scenes))


@_handler("get_bridge_config")
async def _h_get_bridge_config(conn, params, ctx):
    resp = await conn._api_get("/config")
    if resp.status_code != 200:
        return _err(f"Bridge returned {resp.status_code}")
    data = resp.json()
    return _ok(config={
        "name": data.get("name"),
        "model_id": data.get("modelid"),
        "api_version": data.get("apiversion"),
        "sw_version": data.get("swversion"),
    })


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live HueConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    if getattr(conn, "_http_client", None) is None:
        return _err("Hue connector is not connected.")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("Hue action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in HUE_ACTIONS if a.id not in _HANDLERS]
