"""WhatsApp action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``. WhatsApp (Meta Cloud API) has no persistent
client and no channel/role concept — every action is a stateless POST to the
phone number's ``/messages`` endpoint (or a GET against the phone number
resource), so handlers call the Graph API directly via the connector's
``_api_post`` / ``_api_get`` helpers. There is no discrete OAuth-scope
introspection available, so ``required_permission`` is left unset.

Scoping rules:
    * Actions may only target the session's bound chat (a WhatsApp phone
      number) or the connector's ``allowed_phone_numbers``.
    * ``risk == "destructive"`` actions require in-channel Approve/Deny
      confirmation via WhatsApp's interactive button messages (see
      ``WhatsAppConnector.request_action_approval``). The current catalog
      has no destructive actions (WhatsApp Business exposes no bulk-delete
      or membership-management surface to bots).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.core.connectors.base import ActionDef, chunk_text

logger = logging.getLogger(__name__)


_CHAT_PROP = {
    "chat_id": {
        "type": "string",
        "description": (
            "Optional recipient phone number to target instead of the bound "
            "chat. Must be an allowed number or the call is rejected."
        ),
    },
}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


WHATSAPP_ACTIONS: List[ActionDef] = [
    # -- messages ------------------------------------------------------------
    ActionDef(
        id="send_message",
        name="Send Message",
        description="Send a text message. Long text is chunked automatically.",
        input_schema=_schema({
            "text": {"type": "string"},
            "platform_chat_id": {"type": "string", "description": "Legacy alias for chat_id."},
            **_CHAT_PROP,
        }, ["text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="mark_read",
        name="Mark Message Read",
        description="Mark an inbound message as read (shows blue checkmarks).",
        input_schema=_schema({"message_id": {"type": "string"}}, ["message_id"]),
        category="messages", risk="write",
    ),
    # -- reactions ---------------------------------------------------------
    ActionDef(
        id="send_reaction",
        name="Send Reaction",
        description="React to a message with an emoji.",
        input_schema=_schema({
            "message_id": {"type": "string"},
            "emoji": {"type": "string", "description": "A single emoji character."},
            **_CHAT_PROP,
        }, ["message_id", "emoji"]),
        category="reactions", risk="write",
    ),
    ActionDef(
        id="remove_reaction",
        name="Remove Reaction",
        description="Remove this bot's own reaction from a message.",
        input_schema=_schema({"message_id": {"type": "string"}, **_CHAT_PROP}, ["message_id"]),
        category="reactions", risk="write",
    ),
    # -- templates -----------------------------------------------------------
    ActionDef(
        id="send_template",
        name="Send Template Message",
        description=(
            "Send a pre-approved WhatsApp message template (required to "
            "start a conversation outside the 24-hour customer-service window)."
        ),
        input_schema=_schema({
            "template_name": {"type": "string"},
            "language_code": {"type": "string", "description": "e.g. 'en_US'."},
            "parameters": {"type": "array", "description": "Body parameter strings, in order."},
            **_CHAT_PROP,
        }, ["template_name", "language_code"]),
        category="templates", risk="write",
    ),
    # -- media -----------------------------------------------------------------
    ActionDef(
        id="send_image",
        name="Send Image",
        description="Send an image by HTTP(S) link, with an optional caption.",
        input_schema=_schema({
            "link": {"type": "string", "description": "HTTP(S) URL of the image."},
            "caption": {"type": "string"},
            **_CHAT_PROP,
        }, ["link"]),
        category="media", risk="write",
    ),
    ActionDef(
        id="send_document",
        name="Send Document",
        description="Send a document/file by HTTP(S) link, with an optional caption.",
        input_schema=_schema({
            "link": {"type": "string", "description": "HTTP(S) URL of the file."},
            "caption": {"type": "string"},
            "filename": {"type": "string"},
            **_CHAT_PROP,
        }, ["link"]),
        category="media", risk="write",
    ),
    ActionDef(
        id="send_location",
        name="Send Location",
        description="Send a location pin.",
        input_schema=_schema({
            "latitude": {"type": "number"},
            "longitude": {"type": "number"},
            "name": {"type": "string"},
            "address": {"type": "string"},
            **_CHAT_PROP,
        }, ["latitude", "longitude"]),
        category="media", risk="write",
    ),
    # -- account ----------------------------------------------------------
    ActionDef(
        id="get_phone_number_info",
        name="Get Phone Number Info",
        description="Details for this WhatsApp Business phone number (verified name, quality rating).",
        input_schema=_schema({}),
        category="account", risk="safe",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in WHATSAPP_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _allowed_chat_ids(conn: Any, ctx: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    bound = str(ctx.get("bound_chat_id") or "")
    if bound:
        allowed.add(bound)
    raw = (conn.config or {}).get("allowed_phone_numbers")
    if raw:
        if isinstance(raw, str):
            raw = [raw]
        allowed.update(str(c) for c in raw)
    if not bound:
        return set()  # non-gateway caller (automation) — unrestricted
    return allowed


def _resolve_chat(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    target = str(
        params.get("chat_id") or params.get("platform_chat_id")
        or ctx.get("bound_chat_id") or ""
    ).strip()
    if not target:
        return None, _err("No target chat — pass 'chat_id' or use a bound session.")
    allowed = _allowed_chat_ids(conn, ctx)
    if allowed and target not in allowed:
        return None, _err(
            f"Chat {target} is not permitted for this connector session. "
            "Only the bound chat or the connector's allowed phone numbers may be used."
        )
    return target, None


def _check_status(resp: Any) -> Optional[Dict[str, Any]]:
    if not (200 <= resp.status_code < 300):
        return _err(f"WhatsApp API returned {resp.status_code}: {resp.text[:200]}")
    return None


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


# -- messages -----------------------------------------------------------------

@_handler("send_message")
async def _send_message(conn, params, ctx):
    text = str(params.get("text") or "")
    if not text.strip():
        return _err("text is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    for chunk in chunk_text(text, conn.MAX_MESSAGE_LENGTH or 4096):
        resp = await conn._api_post({
            "messaging_product": "whatsapp", "to": chat_id,
            "type": "text", "text": {"body": chunk},
        })
        bad = _check_status(resp)
        if bad:
            return bad
    return _ok(chat_id=chat_id)


@_handler("mark_read")
async def _mark_read(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return _err("message_id is required.")
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "status": "read", "message_id": message_id,
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(message_id=message_id, read=True)


# -- reactions -------------------------------------------------------------

@_handler("send_reaction")
async def _send_reaction(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    emoji = str(params.get("emoji") or "").strip()
    if not message_id or not emoji:
        return _err("message_id and emoji are required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "to": chat_id, "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, message_id=message_id, emoji=emoji)


@_handler("remove_reaction")
async def _remove_reaction(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return _err("message_id is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    # Meta's API removes a reaction by sending an empty emoji string.
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "to": chat_id, "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": ""},
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, message_id=message_id, removed=True)


# -- templates ---------------------------------------------------------------

@_handler("send_template")
async def _send_template(conn, params, ctx):
    template_name = str(params.get("template_name") or "").strip()
    language_code = str(params.get("language_code") or "").strip()
    if not template_name or not language_code:
        return _err("template_name and language_code are required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    parameters = params.get("parameters") or []
    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in parameters],
        })
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "to": chat_id, "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            **({"components": components} if components else {}),
        },
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id, template_name=template_name)


# -- media --------------------------------------------------------------------

@_handler("send_image")
async def _send_image(conn, params, ctx):
    link = str(params.get("link") or "").strip()
    if not link:
        return _err("link is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "to": chat_id, "type": "image",
        "image": {"link": link, **({"caption": params["caption"]} if params.get("caption") else {})},
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id)


@_handler("send_document")
async def _send_document(conn, params, ctx):
    link = str(params.get("link") or "").strip()
    if not link:
        return _err("link is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    document: Dict[str, Any] = {"link": link}
    if params.get("caption"):
        document["caption"] = params["caption"]
    if params.get("filename"):
        document["filename"] = params["filename"]
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "to": chat_id, "type": "document",
        "document": document,
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id)


@_handler("send_location")
async def _send_location(conn, params, ctx):
    latitude = params.get("latitude")
    longitude = params.get("longitude")
    if latitude is None or longitude is None:
        return _err("latitude and longitude are required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    location: Dict[str, Any] = {"latitude": float(latitude), "longitude": float(longitude)}
    if params.get("name"):
        location["name"] = params["name"]
    if params.get("address"):
        location["address"] = params["address"]
    resp = await conn._api_post({
        "messaging_product": "whatsapp", "to": chat_id, "type": "location",
        "location": location,
    })
    bad = _check_status(resp)
    if bad:
        return bad
    return _ok(chat_id=chat_id)


# -- account --------------------------------------------------------------

@_handler("get_phone_number_info")
async def _get_phone_number_info(conn, params, ctx):
    resp = await conn._api_get_phone_number()
    bad = _check_status(resp)
    if bad:
        return bad
    data = resp.json() or {}
    return _ok(phone_number={
        "id": data.get("id"),
        "verified_name": data.get("verified_name"),
        "display_phone_number": data.get("display_phone_number"),
        "quality_rating": data.get("quality_rating"),
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
    """Dispatch one catalog action against a live WhatsAppConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("WhatsApp action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in WHATSAPP_ACTIONS if a.id not in _HANDLERS]
