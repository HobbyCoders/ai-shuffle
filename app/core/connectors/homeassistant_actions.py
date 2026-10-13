"""Home Assistant action catalog + handlers for the connector action gateway.

Home Assistant is a non-messaging connector, so its gateway pair
(``homeassistant_actions`` / ``homeassistant_invoke``) is registered globally
for every session while a Home Assistant connector is running (there is no
session/channel binding to scope to). The install's ``entity_filter`` config
still applies to state listings, and ``enabled_action_categories`` can
restrict what the AI may do.

Legacy action ids (``call_service``, ``toggle_entity``, ``get_states``) are
preserved so existing automations keep working unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.connectors.base import ActionDef

logger = logging.getLogger(__name__)


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_ENTITY = {"type": "string", "description": "Home Assistant entity ID, e.g. 'light.kitchen'."}


HOMEASSISTANT_ACTIONS: List[ActionDef] = [
    # -- states ------------------------------------------------------------
    ActionDef(
        id="get_states",
        name="Get States",
        description="List entities (respecting the install's entity filter) with their current states.",
        input_schema=_schema({
            "domain": {"type": "string", "description": "Optional domain filter, e.g. 'light'."},
        }),
        category="states", risk="safe",
    ),
    ActionDef(
        id="get_state",
        name="Get Entity State",
        description="Full state + attributes for one entity.",
        input_schema=_schema({"entity_id": _ENTITY}, ["entity_id"]),
        category="states", risk="safe",
    ),
    ActionDef(
        id="get_history",
        name="Get Entity History",
        description="Recent state history for an entity.",
        input_schema=_schema({
            "entity_id": _ENTITY,
            "hours": {"type": "number", "minimum": 1, "maximum": 168},
        }, ["entity_id"]),
        category="states", risk="safe",
    ),
    # -- control -----------------------------------------------------------
    ActionDef(
        id="turn_on",
        name="Turn On",
        description="Turn an entity on.",
        input_schema=_schema({"entity_id": _ENTITY}, ["entity_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="turn_off",
        name="Turn Off",
        description="Turn an entity off.",
        input_schema=_schema({"entity_id": _ENTITY}, ["entity_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="toggle_entity",
        name="Toggle Entity",
        description="Toggle an entity on/off.",
        input_schema=_schema({"entity_id": _ENTITY}, ["entity_id"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="set_value",
        name="Set Value",
        description=(
            "Set an entity to a value — light brightness, climate temperature, "
            "cover position, fan percentage, input_* values, etc. The right "
            "service is chosen from the entity's domain."
        ),
        input_schema=_schema({
            "entity_id": _ENTITY,
            "value": {"type": "string", "description": "Target value (numbers auto-converted)."},
        }, ["entity_id", "value"]),
        category="control", risk="write",
    ),
    ActionDef(
        id="call_service",
        name="Call Service",
        description=(
            "Call any Home Assistant service — the escape hatch for everything "
            "not covered by a dedicated action. Discover services with "
            "list_services."
        ),
        input_schema=_schema({
            "domain": {"type": "string"},
            "service": {"type": "string"},
            "entity_id": {"type": "string"},
            "data": {"type": "object", "description": "Extra service data fields."},
        }, ["domain", "service"]),
        category="control", risk="write",
    ),
    # -- discovery ---------------------------------------------------------
    ActionDef(
        id="list_services",
        name="List Services",
        description="List available services, optionally for one domain.",
        input_schema=_schema({
            "domain": {"type": "string", "description": "Optional domain filter."},
        }),
        category="discovery", risk="safe",
    ),
    ActionDef(
        id="get_config",
        name="Get Instance Config",
        description="Home Assistant version, location, units, and timezone.",
        input_schema=_schema({}),
        category="discovery", risk="safe",
    ),
    ActionDef(
        id="render_template",
        name="Render Template",
        description="Render a Home Assistant Jinja2 template (e.g. \"{{ states('sun.sun') }}\").",
        input_schema=_schema({"template": {"type": "string"}}, ["template"]),
        category="discovery", risk="safe",
    ),
    # -- events / notifications -------------------------------------------
    ActionDef(
        id="fire_event",
        name="Fire Event",
        description="Fire a custom event on the Home Assistant event bus.",
        input_schema=_schema({
            "event_type": {"type": "string"},
            "data": {"type": "object"},
        }, ["event_type"]),
        category="events", risk="write",
    ),
    ActionDef(
        id="create_notification",
        name="Create Notification",
        description="Create a persistent notification in the Home Assistant UI.",
        input_schema=_schema({
            "title": {"type": "string"},
            "message": {"type": "string"},
        }, ["message"]),
        category="events", risk="write",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in HOMEASSISTANT_ACTIONS}


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _http_error(resp: Any) -> Dict[str, Any]:
    return _err(f"HA returned {resp.status_code}: {resp.text[:200]}")


# Domain -> (service_domain, service, data_key) for set_value
_SET_VALUE_MAP: Dict[str, tuple] = {
    "light": ("light", "turn_on", "brightness"),
    "climate": ("climate", "set_temperature", "temperature"),
    "cover": ("cover", "set_cover_position", "position"),
    "fan": ("fan", "set_percentage", "percentage"),
    "input_number": ("input_number", "set_value", "value"),
    "input_text": ("input_text", "set_value", "value"),
    "input_select": ("input_select", "select_option", "option"),
    "number": ("number", "set_value", "value"),
    "media_player": ("media_player", "volume_set", "volume_level"),
}


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


def _entity_payload(entity: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    attrs = entity.get("attributes", {})
    out = {
        "entity_id": entity.get("entity_id", ""),
        "state": entity.get("state", ""),
        "friendly_name": attrs.get("friendly_name", entity.get("entity_id", "")),
    }
    if full:
        out["attributes"] = attrs
        out["last_changed"] = entity.get("last_changed")
    return out


# -- states -----------------------------------------------------------------

@_handler("get_states")
async def _get_states(conn, params, ctx):
    resp = await conn._api_get("/api/states")
    if resp.status_code != 200:
        return _http_error(resp)
    domain = str(params.get("domain") or "").strip().lower()
    entities = []
    for entity in resp.json():
        eid = entity.get("entity_id", "")
        if not conn._matches_filter(eid):
            continue
        if domain and not eid.startswith(f"{domain}."):
            continue
        entities.append(_entity_payload(entity))
    return _ok(entities=entities, count=len(entities))


@_handler("get_state")
async def _get_state(conn, params, ctx):
    entity_id = str(params.get("entity_id") or "").strip()
    resp = await conn._api_get(f"/api/states/{entity_id}")
    if resp.status_code != 200:
        return _http_error(resp)
    return _ok(entity=_entity_payload(resp.json(), full=True))


@_handler("get_history")
async def _get_history(conn, params, ctx):
    entity_id = str(params.get("entity_id") or "").strip()
    try:
        hours = max(1.0, min(168.0, float(params.get("hours") or 24)))
    except (TypeError, ValueError):
        hours = 24.0
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    resp = await conn._api_get(
        f"/api/history/period/{start}?filter_entity_id={entity_id}&minimal_response"
    )
    if resp.status_code != 200:
        return _http_error(resp)
    series = resp.json() or []
    points = [
        {"state": p.get("state"), "last_changed": p.get("last_changed")}
        for p in (series[0] if series else [])
    ]
    return _ok(entity_id=entity_id, hours=hours, points=points, count=len(points))


# -- control ----------------------------------------------------------------

async def _simple_service(conn, service: str, entity_id: str) -> Dict[str, Any]:
    if not entity_id:
        return _err("entity_id is required.")
    resp = await conn._api_post(
        f"/api/services/homeassistant/{service}", data={"entity_id": entity_id},
    )
    if 200 <= resp.status_code < 300:
        return _ok(entity_id=entity_id, action=service)
    return _http_error(resp)


@_handler("turn_on")
async def _turn_on(conn, params, ctx):
    return await _simple_service(conn, "turn_on", str(params.get("entity_id") or "").strip())


@_handler("turn_off")
async def _turn_off(conn, params, ctx):
    return await _simple_service(conn, "turn_off", str(params.get("entity_id") or "").strip())


@_handler("toggle_entity")
async def _toggle_entity(conn, params, ctx):
    return await _simple_service(conn, "toggle", str(params.get("entity_id") or "").strip())


@_handler("set_value")
async def _set_value(conn, params, ctx):
    entity_id = str(params.get("entity_id") or "").strip()
    value = str(params.get("value") if params.get("value") is not None else "").strip()
    if not entity_id or not value:
        return _err("entity_id and value are required.")
    domain = entity_id.split(".")[0]
    mapping = _SET_VALUE_MAP.get(domain)
    if mapping is None:
        return _err(
            f"set_value does not support domain '{domain}' — use call_service instead."
        )
    svc_domain, svc_name, data_key = mapping
    try:
        parsed: Any = float(value)
        if parsed == int(parsed):
            parsed = int(parsed)
    except ValueError:
        parsed = value
    resp = await conn._api_post(
        f"/api/services/{svc_domain}/{svc_name}",
        data={"entity_id": entity_id, data_key: parsed},
    )
    if 200 <= resp.status_code < 300:
        return _ok(entity_id=entity_id, **{data_key: parsed})
    return _http_error(resp)


@_handler("call_service")
async def _call_service(conn, params, ctx):
    domain = str(params.get("domain") or "").strip()
    service = str(params.get("service") or "").strip()
    if not domain or not service:
        return _err("domain and service are required.")
    payload: Dict[str, Any] = {}
    if params.get("entity_id"):
        payload["entity_id"] = params["entity_id"]
    if isinstance(params.get("data"), dict):
        payload.update(params["data"])
    resp = await conn._api_post(f"/api/services/{domain}/{service}", data=payload)
    if 200 <= resp.status_code < 300:
        return _ok(domain=domain, service=service)
    return _http_error(resp)


# -- discovery --------------------------------------------------------------

@_handler("list_services")
async def _list_services(conn, params, ctx):
    resp = await conn._api_get("/api/services")
    if resp.status_code != 200:
        return _http_error(resp)
    domain_filter = str(params.get("domain") or "").strip().lower()
    out = []
    for entry in resp.json():
        domain = entry.get("domain", "")
        if domain_filter and domain != domain_filter:
            continue
        out.append({"domain": domain, "services": sorted((entry.get("services") or {}).keys())})
    return _ok(domains=out, count=len(out))


@_handler("get_config")
async def _get_config(conn, params, ctx):
    resp = await conn._api_get("/api/config")
    if resp.status_code != 200:
        return _http_error(resp)
    data = resp.json()
    return _ok(config={
        "version": data.get("version"),
        "location_name": data.get("location_name"),
        "time_zone": data.get("time_zone"),
        "unit_system": data.get("unit_system"),
        "components_count": len(data.get("components") or []),
    })


@_handler("render_template")
async def _render_template(conn, params, ctx):
    template = str(params.get("template") or "")
    if not template.strip():
        return _err("template is required.")
    resp = await conn._api_post("/api/template", data={"template": template})
    if resp.status_code != 200:
        return _http_error(resp)
    return _ok(result=resp.text[:4000])


# -- events / notifications -------------------------------------------------

@_handler("fire_event")
async def _fire_event(conn, params, ctx):
    event_type = str(params.get("event_type") or "").strip()
    if not event_type:
        return _err("event_type is required.")
    data = params.get("data") if isinstance(params.get("data"), dict) else {}
    resp = await conn._api_post(f"/api/events/{event_type}", data=data)
    if 200 <= resp.status_code < 300:
        return _ok(event_type=event_type)
    return _http_error(resp)


@_handler("create_notification")
async def _create_notification(conn, params, ctx):
    message = str(params.get("message") or "")
    if not message.strip():
        return _err("message is required.")
    resp = await conn._api_post(
        "/api/services/persistent_notification/create",
        data={
            "title": str(params.get("title") or "AI Shuffle"),
            "message": message,
        },
    )
    if 200 <= resp.status_code < 300:
        return _ok(created=True)
    return _http_error(resp)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live HomeAssistantConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    if getattr(conn, "_http_client", None) is None:
        return _err("Home Assistant connector is not connected.")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("Home Assistant action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in HOMEASSISTANT_ACTIONS if a.id not in _HANDLERS]
