"""Permission requests in a connector chat, which has no prompt to press.

Tool approvals for connector-linked sessions belong to the OWNER, not to
whoever is talking to the bot. The owner can answer in the app, as for a web
session, or in a text reply from one of their own accounts on that connector
(``connector_service.sender_is_owner``). Nobody else can answer; for them the
chat shows one line saying the agent is waiting, and what they type is an
ordinary message.

A request asked in a chat carries a four-digit code, and a reply answers only
the request it names: "yes 4821" or "no 4821". Platforms repeat messages, also
to another worker after a connector moves, and nothing about a bare yes says
which request it meant, so a bare yes answers nothing and the chat shows the
reply to send. A code that is not open gets "no longer open".

A reply with a platform message ID counts once on a worker. One with no ID
cannot be told from a repeat, so it answers nothing. A code is not drawn again
in its chat for 30 days, so a reply that arrives late meets "no longer open"
rather than a newer request.

Only a request this worker can act on is answered: the call waiting for it in
this process, or a call already let go (#523) on a chat this worker runs. Any
other row, such as a read-only copy synced from another worker or one whose
waiter died in a restart, is left alone and the chat is told to use the app.

Workers do not share codes, so the one way a reply can answer the wrong request
is a platform delivering it to a second worker that happens to hold the same
code open in that chat.

Connectors whose senders cannot be verified (LINE, Microsoft Teams,
BlueBubbles: ``connector_service.OWNER_SENDER_TYPES``) have no owner account, so
their chats are told to answer in the app.

The platform Approve/Deny buttons are not sent, and
``ConnectorManager.handle_permission_response`` ignores any left in old chat
history: a button press there carries no sender to verify.

Delivery is best-effort from the permission request path: ANY failure
(connector not running, platform send error, lookup miss) must never break the
permission flow itself. An unanswered request still ends with its TTL deny.
"""

import asyncio
import logging
import re
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

_IN_THE_APP = "Answer it in the app."
_NOT_CONFIRMED = "Couldn't confirm that answer. Send a new reply or answer it in the app."
#: "yes 4821" in any case, with any spacing, and nothing else.
_CODED_REPLY = re.compile(r"\s*(yes|no)\s*([0-9]{4})\s*", re.ASCII | re.IGNORECASE)


async def deliver_permission_prompt(
    session_id: str,
    request_id: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    description: str,
    interaction_id: Optional[str] = None,
) -> None:
    """Post the request to every chat linked to ``session_id``.

    Looks up the connector sessions bound to the AI session, resolves each
    RUNNING connector instance, and sends a plain one-line message to the
    platform chat that owns the conversation. No buttons: approval happens in
    the owner's app or in plain text (:func:`answer_from_chat`). Fire-and-forget
    from the caller's perspective: errors are logged, never raised.
    """
    try:
        from app.core.connectors.base import OutgoingMessage
        from app.core.connectors.manager import connector_manager
    except Exception:  # noqa: BLE001 — imports must never break the flow
        logger.debug("connector_permissions: imports unavailable", exc_info=True)
        return

    try:
        connector_sessions = _lookup_connector_sessions(session_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "connector_permissions: connector-session lookup failed for %s",
            session_id, exc_info=True,
        )
        return

    if not connector_sessions:
        return

    for cs in connector_sessions:
        connector_id = cs.get("connector_id")
        platform_chat_id = cs.get("platform_chat_id")
        if not connector_id or not platform_chat_id:
            continue
        try:
            action = await asyncio.to_thread(_action_to_offer, cs, tool_name, tool_input)
            instance = connector_manager.get_running_instance(connector_id)
            if instance is None:
                logger.debug(
                    "connector_permissions: connector %s not running — "
                    "skipping prompt delivery", connector_id,
                )
                continue
            text = waiting_for_owner_text(tool_name)
            if action is not None:
                # A command prefix makes the adapter drop "yes 4821" before it gets here.
                codeable = None if instance.config.get("command_prefix") else interaction_id
                text = await asyncio.to_thread(
                    _question_text, codeable, _chat_key(connector_id, platform_chat_id), action)
            elif await asyncio.to_thread(_owner_cannot_be_verified, connector_id):
                text = f"{text} {_IN_THE_APP}"
            sent = await instance.send_message(
                OutgoingMessage(platform_chat_id=platform_chat_id, text=text)
            )
            if not sent:
                logger.warning(
                    "connector_permissions: %s failed to post the waiting "
                    "notice for %s to chat %s",
                    connector_id, request_id, platform_chat_id,
                )
        except Exception:  # noqa: BLE001 — one platform's failure must not
            # stop the others (a session can be linked to several connectors).
            logger.warning(
                "connector_permissions: prompt delivery raised for connector "
                "%s chat %s", connector_id, platform_chat_id, exc_info=True,
            )


def waiting_for_owner_text(tool_name: str) -> str:
    """The one line a chat sees while its agent waits for the owner."""
    return f"Waiting for the owner to approve {tool_name}."


def _action_to_offer(connector_session: Dict[str, Any], tool_name: str, tool_input: Any) -> Optional[str]:
    """What the agent wants to do, for a chat started from the owner's own
    account. None for any other chat, which only hears that the agent is
    waiting. Blocking (DB read)."""
    from app.core import owner_confirm
    from app.core.connector_service import sender_is_owner

    if owner_confirm.is_guardrail(tool_name, tool_input) or not sender_is_owner(
            connector_session.get("connector_id"), connector_session.get("platform_user_id")):
        return None
    return owner_confirm.describe(tool_name, tool_input)


def _owner_cannot_be_verified(connector_id: Any) -> bool:
    """True for a connector whose chats can never show the owner a code.
    Blocking (DB read)."""
    from app.core.connector_service import OWNER_SENDER_TYPES
    from app.db import database

    row = database.get_connector(connector_id)
    return bool(row) and row.get("type") not in OWNER_SENDER_TYPES


def _question_text(interaction_id: Optional[str], chat: str, action: str) -> str:
    """The question, with the code that answers it once that code is stored on
    the request. Blocking (DB)."""
    from app.db import interactions

    code = interactions.assign_chat_code(interaction_id, chat) if interaction_id else None
    if code is None:
        return f"The agent wants to {action}. {_IN_THE_APP}"
    return f"The agent wants to {action}. Reply yes {code} or no {code}."


def _chat_key(connector_id: Any, platform_chat_id: Any) -> str:
    return f"{connector_id}:{platform_chat_id}"


def _code_hint(codes: list) -> str:
    """What a bare yes or no is told: the reply that would answer."""
    first = codes[0]
    if len(codes) == 1:
        return f"To answer, reply yes {first} or no {first}."
    return f"Open requests: {', '.join(codes)}. To answer one, reply yes or no with its code, like yes {first}."


def _closed_text(code: str) -> str:
    return f"Request {code} is no longer open."


def _can_act_on(row: Dict[str, Any]) -> bool:
    """Whether answering ``row`` reaches something that will act on it. Blocking (DB)."""
    from app.core import interaction_wait, owner_confirm
    from app.db import database
    from app.network import control

    if interaction_wait.is_awaited(row["id"]):
        return True
    # A call already let go has no waiter; answering tells the agent (#523),
    # but only on the worker that runs the chat. Elsewhere the row is a copy.
    if not (owner_confirm.approval_of(row.get("payload")) and owner_confirm.is_released(row)):
        return False
    session = database.get_session(row["session_id"])
    return session is not None and control.owner_of(session) is None


def _settle_text(code: str, row: Optional[Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    """The open request to answer, or what to tell the chat instead. Blocking (DB)."""
    if row is None:
        return _closed_text(code)
    return row if _can_act_on(row) else f"Request {code} can't be answered from this chat. {_IN_THE_APP}"


def _read_reply(message: Any, code: Optional[str]) -> Union[Dict[str, Any], str, None]:
    """What the owner's reply does: the open request it names, the line to tell
    the chat when it answers nothing ("" for a repeat, which is told nothing),
    or None when it is an ordinary message. Blocking (DB).

    Raises only before the claim, when the reply is not yet consumed. After the
    claim a failure comes back as a line for the chat.
    """
    from app.core.connector_service import sender_is_owner
    from app.db import connectors as connectors_db
    from app.db import database, interactions
    from app.network import ownership

    if not sender_is_owner(message.connector_id, message.platform_user_id):
        return None
    if ownership.enrolled() and not ownership.owns(database.get_connector(message.connector_id)):
        return None
    chat = _chat_key(message.connector_id, message.platform_chat_id)
    if code is None:
        codes = list(interactions.open_chat_codes(chat))
        return _code_hint(codes) if codes else None
    message_id = str(message.platform_message_id or "").strip()
    if not message_id:
        return f"This connection did not identify that reply, so it answered nothing. {_IN_THE_APP}"
    # One insert decides, so of concurrent deliveries exactly one goes on.
    if not connectors_db.claim_answer_message(message.connector_id, message.platform_chat_id, message_id):
        return ""
    try:
        return _settle_text(code, interactions.open_chat_codes(chat).get(code))
    except Exception:  # noqa: BLE001 — consumed now, so never an agent prompt
        logger.warning("connector_permissions: claimed answer could not be read", exc_info=True)
        return _NOT_CONFIRMED


async def answer_from_chat(message: Any) -> bool:
    """Answer the open request the owner's reply names by its code.

    True when ``message`` was for this path and needs no agent turn: an answer,
    a repeat of one, a bare yes or no while a request is open (told the reply to
    send), or a code that is not open (told so). False leaves it an ordinary
    message: another sender, anything else they said, or a bare yes or no with
    nothing open. Runs ahead of the per-chat message lock, because that lock is
    held by the very turn waiting for this answer. Never raises.
    """
    from app.core import owner_confirm

    try:
        coded = _CODED_REPLY.fullmatch(message.text or "")
        if message.attachments or (coded is None and owner_confirm.classify(message.text) == owner_confirm.UNCLEAR):
            return False
        code = coded[2] if coded else None
        found = await asyncio.to_thread(_read_reply, message, code)
    except Exception:  # noqa: BLE001 — not claimed, so the reply can come again
        logger.warning("connector_permissions: text answer failed", exc_info=True)
        return False
    if found is None:
        return False
    if isinstance(found, str):
        if found:
            await _tell_chat(message, found)
        return True
    from app.core.permission_handler import permission_handler

    payload = found["payload"]
    allow = coded[1].lower() == "yes"
    try:
        result = await permission_handler.respond(payload["request_id"], found["session_id"], "allow" if allow else "deny")
    except Exception:  # noqa: BLE001 — claimed, so this reply is spent
        logger.warning("connector_permissions: claimed answer failed", exc_info=True)
        await _tell_chat(message, _NOT_CONFIRMED)
        return True
    if not result.get("resolved"):
        # Another path answered it first, or it expired since the lookup.
        await _tell_chat(message, _closed_text(code))
        return True
    done = "Approved" if allow else "Declined"
    action = owner_confirm.describe(payload.get("tool_name") or "", payload.get("tool_input"))
    await _tell_chat(message, f"{done}: {action}.")
    return True


async def _tell_chat(message: Any, text: str) -> None:
    """Best-effort reply in the chat ``message`` came from."""
    from app.core.connectors.base import OutgoingMessage
    from app.core.connectors.manager import connector_manager

    try:
        instance = connector_manager.get_running_instance(message.connector_id)
        if instance is not None:
            await instance.send_message(OutgoingMessage(platform_chat_id=message.platform_chat_id, text=text))
    except Exception:  # noqa: BLE001 — what it reports already stands
        logger.debug("connector_permissions: chat reply failed", exc_info=True)


def _lookup_connector_sessions(session_id):
    """Resolve the connector-session rows for an AI session (sync DB read)."""
    from app.db import database

    return database.get_connector_sessions_for_ai_session(session_id)
