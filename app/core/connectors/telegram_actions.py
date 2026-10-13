"""Telegram action catalog + handlers for the connector action gateway.

Mirrors ``slack_actions.py``: every Telegram Bot API capability this
connector exposes is an ``ActionDef`` (id, JSON schema, category, risk tier)
with a matching handler, reached through the ``telegram_actions`` /
``telegram_invoke`` MCP tool pair. Telegram bots have no discrete OAuth-style
permission scopes to introspect (group-admin actions simply fail with a
Bot API error if the bot lacks admin rights in that chat), so
``required_permission`` is left unset and errors are surfaced verbatim.

Scoping rules:
    * Chat-level actions may only target the session's bound chat or the
      connector's ``allowed_chat_ids``.
    * ``risk == "destructive"`` actions require in-channel Approve/Deny
      confirmation (see TelegramConnector.request_action_approval).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from app.core.connectors.base import ActionDef, chunk_text

logger = logging.getLogger(__name__)

try:
    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import InputPollOption, ReactionTypeEmoji

    AIOGRAM_TYPES_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without aiogram
    AIOGRAM_TYPES_AVAILABLE = False


_CHAT_PROP = {
    "chat_id": {
        "type": "string",
        "description": (
            "Optional chat ID (or @channelusername) to target instead of the "
            "bound chat. Must be an allowed chat or the call is rejected."
        ),
    },
}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


TELEGRAM_ACTIONS: List[ActionDef] = [
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
        id="edit_message",
        name="Edit Message",
        description="Edit the text of a message previously sent by this bot.",
        input_schema=_schema({
            "message_id": {"type": "integer"},
            "text": {"type": "string"},
            **_CHAT_PROP,
        }, ["message_id", "text"]),
        category="messages", risk="write",
    ),
    ActionDef(
        id="delete_message",
        name="Delete Message",
        description="Delete a message.",
        input_schema=_schema({"message_id": {"type": "integer"}, **_CHAT_PROP}, ["message_id"]),
        category="messages", risk="destructive",
    ),
    ActionDef(
        id="forward_message",
        name="Forward Message",
        description="Forward a message from another chat into the target chat.",
        input_schema=_schema({
            "from_chat_id": {"type": "string"},
            "message_id": {"type": "integer"},
            **_CHAT_PROP,
        }, ["from_chat_id", "message_id"]),
        category="messages", risk="write",
    ),
    # -- reactions -------------------------------------------------------------
    ActionDef(
        id="add_reaction",
        name="Add Reaction",
        description="React to a message with a single emoji (e.g. a thumbs-up).",
        input_schema=_schema({
            "message_id": {"type": "integer"},
            "emoji": {"type": "string", "description": "A single emoji character."},
            **_CHAT_PROP,
        }, ["message_id", "emoji"]),
        category="reactions", risk="write",
    ),
    # -- pins --------------------------------------------------------------
    ActionDef(
        id="pin_message",
        name="Pin Message",
        description="Pin a message in the chat.",
        input_schema=_schema({"message_id": {"type": "integer"}, **_CHAT_PROP}, ["message_id"]),
        category="pins", risk="write",
    ),
    ActionDef(
        id="unpin_message",
        name="Unpin Message",
        description="Unpin a specific message (omit message_id to unpin the most recent pin).",
        input_schema=_schema({"message_id": {"type": "integer"}, **_CHAT_PROP}),
        category="pins", risk="write",
    ),
    ActionDef(
        id="unpin_all_messages",
        name="Unpin All Messages",
        description="Unpin every pinned message in the chat.",
        input_schema=_schema({**_CHAT_PROP}),
        category="pins", risk="destructive",
    ),
    # -- polls ---------------------------------------------------------------
    ActionDef(
        id="send_poll",
        name="Send Poll",
        description="Send a poll to the chat.",
        input_schema=_schema({
            "question": {"type": "string"},
            "options": {"type": "array", "description": "2-10 answer strings."},
            "is_anonymous": {"type": "boolean"},
            "allows_multiple_answers": {"type": "boolean"},
            **_CHAT_PROP,
        }, ["question", "options"]),
        category="polls", risk="write",
    ),
    ActionDef(
        id="stop_poll",
        name="Stop Poll",
        description="Close (stop accepting votes on) a poll message sent by this bot.",
        input_schema=_schema({"message_id": {"type": "integer"}, **_CHAT_PROP}, ["message_id"]),
        category="polls", risk="write",
    ),
    # -- media ---------------------------------------------------------------
    ActionDef(
        id="send_photo",
        name="Send Photo",
        description="Send a photo by HTTP(S) URL, with an optional caption.",
        input_schema=_schema({
            "photo": {"type": "string", "description": "HTTP(S) URL of the photo."},
            "caption": {"type": "string"},
            **_CHAT_PROP,
        }, ["photo"]),
        category="media", risk="write",
    ),
    ActionDef(
        id="send_document",
        name="Send Document",
        description="Send a document/file by HTTP(S) URL, with an optional caption.",
        input_schema=_schema({
            "document": {"type": "string", "description": "HTTP(S) URL of the file."},
            "caption": {"type": "string"},
            **_CHAT_PROP,
        }, ["document"]),
        category="media", risk="write",
    ),
    # -- chat ------------------------------------------------------------
    ActionDef(
        id="get_chat",
        name="Get Chat Info",
        description="Details for a chat (title, type, description, member count).",
        input_schema=_schema({**_CHAT_PROP}),
        category="chat", risk="safe",
    ),
    ActionDef(
        id="get_chat_member_count",
        name="Get Chat Member Count",
        description="Number of members in the chat.",
        input_schema=_schema({**_CHAT_PROP}),
        category="chat", risk="safe",
    ),
    ActionDef(
        id="set_chat_title",
        name="Set Chat Title",
        description="Rename the chat (bot must be an admin in groups/channels).",
        input_schema=_schema({"title": {"type": "string"}, **_CHAT_PROP}, ["title"]),
        category="chat", risk="write",
    ),
    ActionDef(
        id="set_chat_description",
        name="Set Chat Description",
        description="Set the chat's description (bot must be an admin in groups/channels).",
        input_schema=_schema({"description": {"type": "string"}, **_CHAT_PROP}, ["description"]),
        category="chat", risk="write",
    ),
    # -- moderation ------------------------------------------------------------
    ActionDef(
        id="ban_chat_member",
        name="Ban Chat Member",
        description="Ban a user from the group/supergroup (bot must be an admin).",
        input_schema=_schema({"user_id": {"type": "integer"}, **_CHAT_PROP}, ["user_id"]),
        category="moderation", risk="destructive",
    ),
    ActionDef(
        id="unban_chat_member",
        name="Unban Chat Member",
        description="Unban a previously banned user.",
        input_schema=_schema({"user_id": {"type": "integer"}, **_CHAT_PROP}, ["user_id"]),
        category="moderation", risk="write",
    ),
    ActionDef(
        id="leave_chat",
        name="Leave Chat",
        description="Make the bot leave the chat.",
        input_schema=_schema({**_CHAT_PROP}),
        category="moderation", risk="destructive",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in TELEGRAM_ACTIONS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


def _bot(conn: Any) -> Optional[Any]:
    return getattr(conn, "_bot", None)


def _allowed_chat_ids(conn: Any, ctx: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    bound = str(ctx.get("bound_chat_id") or "")
    if bound:
        allowed.add(bound)
    raw = (conn.config or {}).get("allowed_chat_ids")
    if raw:
        if isinstance(raw, str):
            raw = [raw]
        allowed.update(str(c) for c in raw)
    if not bound:
        return set()  # non-gateway caller (automation) — unrestricted
    return allowed


def _resolve_chat(
    conn: Any, ctx: Dict[str, Any], params: Dict[str, Any]
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve and allow-list-check the target chat id (int if numeric)."""
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
            "Only the bound chat or the connector's allowed chats may be used."
        )
    return _chat_id_value(target), None


def _chat_id_value(target: str) -> Any:
    """aiogram accepts int chat ids or '@username' strings."""
    try:
        return int(target)
    except ValueError:
        return target


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
    message_ids = []
    for chunk in chunk_text(text, conn.MAX_MESSAGE_LENGTH or 4096):
        msg = await _bot(conn).send_message(chat_id=chat_id, text=chunk)
        message_ids.append(msg.message_id)
    return _ok(message_ids=message_ids, chat_id=str(chat_id))


@_handler("edit_message")
async def _edit_message(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    message_id = params.get("message_id")
    if message_id is None:
        return _err("message_id is required.")
    await _bot(conn).edit_message_text(
        chat_id=chat_id, message_id=int(message_id), text=str(params.get("text") or ""),
    )
    return _ok(message_id=int(message_id), chat_id=str(chat_id))


@_handler("delete_message")
async def _delete_message(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    message_id = params.get("message_id")
    if message_id is None:
        return _err("message_id is required.")
    await _bot(conn).delete_message(chat_id=chat_id, message_id=int(message_id))
    return _ok(deleted_message_id=int(message_id), chat_id=str(chat_id))


@_handler("forward_message")
async def _forward_message(conn, params, ctx):
    from_chat_id = params.get("from_chat_id")
    message_id = params.get("message_id")
    if not from_chat_id or message_id is None:
        return _err("from_chat_id and message_id are required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    msg = await _bot(conn).forward_message(
        chat_id=chat_id, from_chat_id=_chat_id_value(str(from_chat_id)), message_id=int(message_id),
    )
    return _ok(message_id=msg.message_id, chat_id=str(chat_id))


# -- reactions ------------------------------------------------------------

@_handler("add_reaction")
async def _add_reaction(conn, params, ctx):
    if not AIOGRAM_TYPES_AVAILABLE:
        return _err("aiogram is not installed.")
    emoji = str(params.get("emoji") or "").strip()
    if not emoji:
        return _err("emoji is required.")
    message_id = params.get("message_id")
    if message_id is None:
        return _err("message_id is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    await _bot(conn).set_message_reaction(
        chat_id=chat_id, message_id=int(message_id), reaction=[ReactionTypeEmoji(emoji=emoji)],
    )
    return _ok(message_id=int(message_id), emoji=emoji)


# -- pins -----------------------------------------------------------------

@_handler("pin_message")
async def _pin_message(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    message_id = params.get("message_id")
    if message_id is None:
        return _err("message_id is required.")
    await _bot(conn).pin_chat_message(chat_id=chat_id, message_id=int(message_id))
    return _ok(message_id=int(message_id), pinned=True)


@_handler("unpin_message")
async def _unpin_message(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    message_id = params.get("message_id")
    kwargs: Dict[str, Any] = {"chat_id": chat_id}
    if message_id is not None:
        kwargs["message_id"] = int(message_id)
    await _bot(conn).unpin_chat_message(**kwargs)
    return _ok(pinned=False)


@_handler("unpin_all_messages")
async def _unpin_all_messages(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    await _bot(conn).unpin_all_chat_messages(chat_id=chat_id)
    return _ok(unpinned_all=True, chat_id=str(chat_id))


# -- polls ------------------------------------------------------------------

@_handler("send_poll")
async def _send_poll(conn, params, ctx):
    if not AIOGRAM_TYPES_AVAILABLE:
        return _err("aiogram is not installed.")
    question = str(params.get("question") or "")
    options = params.get("options")
    if not question.strip():
        return _err("question is required.")
    if not isinstance(options, list) or not (2 <= len(options) <= 10):
        return _err("options must be a list of 2-10 answer strings.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    msg = await _bot(conn).send_poll(
        chat_id=chat_id,
        question=question,
        options=[InputPollOption(text=str(o)) for o in options],
        is_anonymous=params.get("is_anonymous"),
        allows_multiple_answers=bool(params.get("allows_multiple_answers")),
    )
    return _ok(message_id=msg.message_id, chat_id=str(chat_id))


@_handler("stop_poll")
async def _stop_poll(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    message_id = params.get("message_id")
    if message_id is None:
        return _err("message_id is required.")
    poll = await _bot(conn).stop_poll(chat_id=chat_id, message_id=int(message_id))
    return _ok(total_voter_count=getattr(poll, "total_voter_count", None), closed=True)


# -- media --------------------------------------------------------------------

@_handler("send_photo")
async def _send_photo(conn, params, ctx):
    photo = str(params.get("photo") or "")
    if not photo:
        return _err("photo is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    msg = await _bot(conn).send_photo(
        chat_id=chat_id, photo=photo, caption=params.get("caption") or None,
    )
    return _ok(message_id=msg.message_id, chat_id=str(chat_id))


@_handler("send_document")
async def _send_document(conn, params, ctx):
    document = str(params.get("document") or "")
    if not document:
        return _err("document is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    msg = await _bot(conn).send_document(
        chat_id=chat_id, document=document, caption=params.get("caption") or None,
    )
    return _ok(message_id=msg.message_id, chat_id=str(chat_id))


# -- chat ---------------------------------------------------------------------

@_handler("get_chat")
async def _get_chat(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    chat = await _bot(conn).get_chat(chat_id=chat_id)
    return _ok(chat={
        "id": chat.id,
        "type": chat.type,
        "title": getattr(chat, "title", None),
        "username": getattr(chat, "username", None),
        "description": getattr(chat, "description", None),
    })


@_handler("get_chat_member_count")
async def _get_chat_member_count(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    count = await _bot(conn).get_chat_member_count(chat_id=chat_id)
    return _ok(member_count=count, chat_id=str(chat_id))


@_handler("set_chat_title")
async def _set_chat_title(conn, params, ctx):
    title = str(params.get("title") or "").strip()
    if not title:
        return _err("title is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    await _bot(conn).set_chat_title(chat_id=chat_id, title=title)
    return _ok(chat_id=str(chat_id), title=title)


@_handler("set_chat_description")
async def _set_chat_description(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    description = str(params.get("description") or "")
    await _bot(conn).set_chat_description(chat_id=chat_id, description=description)
    return _ok(chat_id=str(chat_id), description=description)


# -- moderation ------------------------------------------------------------

@_handler("ban_chat_member")
async def _ban_chat_member(conn, params, ctx):
    user_id = params.get("user_id")
    if user_id is None:
        return _err("user_id is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    await _bot(conn).ban_chat_member(chat_id=chat_id, user_id=int(user_id))
    return _ok(banned_user_id=int(user_id), chat_id=str(chat_id))


@_handler("unban_chat_member")
async def _unban_chat_member(conn, params, ctx):
    user_id = params.get("user_id")
    if user_id is None:
        return _err("user_id is required.")
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    await _bot(conn).unban_chat_member(chat_id=chat_id, user_id=int(user_id))
    return _ok(unbanned_user_id=int(user_id), chat_id=str(chat_id))


@_handler("leave_chat")
async def _leave_chat(conn, params, ctx):
    chat_id, err = _resolve_chat(conn, ctx, params)
    if err:
        return err
    bound = str(ctx.get("bound_chat_id") or "")
    if bound and str(chat_id) == bound:
        return _err("Refusing to leave the chat this session is bound to.")
    await _bot(conn).leave_chat(chat_id=chat_id)
    return _ok(left_chat_id=str(chat_id))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live TelegramConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    if _bot(conn) is None:
        return _err("Telegram bot is not running.")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        # aiogram raises TelegramAPIError subclasses with the Bot API error
        # description in the message (e.g. "chat not found", "not enough rights").
        logger.exception("Telegram action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in TELEGRAM_ACTIONS if a.id not in _HANDLERS]
