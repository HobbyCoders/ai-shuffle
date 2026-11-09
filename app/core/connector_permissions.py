"""Permission requests in a connector chat, which has no prompt to press.

Tool approvals for connector-linked sessions belong to the OWNER, not to
whoever is talking to the bot. The owner can answer in the app, as for a web
session, or in plain text from one of their own accounts on that connector
(``connector_service.sender_is_owner``): a connector has no permission UI, so
a firm yes or no is the control there. Nobody else in the chat can answer; for
them the chat shows one line saying the agent is waiting, and their "yes" is
an ordinary message. Platforms can deliver a message twice, so each reply counts
once, by its platform message ID. A reply with no ID cannot be told from a
repeat, so it never approves anything and stays an ordinary message.

Those counted IDs stay on the worker that counted them, so a connector that moves
to another worker can be sent an already counted reply again. A reply therefore
answers only a request whose message the platform timed before it, both times
from the platform's clock (``BaseConnector.send_timed``), so the workers' own
clocks play no part. A request stays on the worker that opened it, and so does
its time. A platform that does not report that time gets no invitation to reply.

The platform Approve/Deny buttons are not sent, and
``ConnectorManager.handle_permission_response`` ignores any left in old chat
history: a button press there carries no sender to verify.

Delivery is best-effort from the permission request path: ANY failure
(connector not running, platform send error, lookup miss) must never break the
permission flow itself. An unanswered request still ends with its TTL deny.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: What :func:`_request_to_answer` returns for a reply already delivered once.
_REPEAT_DELIVERY: Tuple[str, str, Dict[str, Any]] = ("", "", {})
_NOT_AFTER_REQUEST: Tuple[str, str, Dict[str, Any]] = ("", "", {})


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
                text = await _ask_in_chat(instance, interaction_id, connector_id, platform_chat_id, action)
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


async def _ask_in_chat(
    instance: Any, interaction_id: Optional[str], connector_id: str, platform_chat_id: str, action: str,
) -> str:
    """Post the action, keep the platform's time for it on the request, and
    return the line to follow it. The invitation to reply is only written once
    that time is stored: a channel that gives none must not offer a reply it
    cannot honour."""
    from app.core.connectors.base import OutgoingMessage, sent_at_seconds
    from app.db import interactions

    posted_at = sent_at_seconds(await instance.send_timed(
        OutgoingMessage(platform_chat_id=platform_chat_id, text=f"The agent wants to {action}.")
    ))
    if posted_at is None or not interaction_id or not await asyncio.to_thread(
            interactions.record_chat_prompt, interaction_id, _chat_key(connector_id, platform_chat_id), posted_at):
        return "Answer it in the app."
    return "Reply yes or no to answer."


def _chat_key(connector_id: Any, platform_chat_id: Any) -> str:
    return f"{connector_id}:{platform_chat_id}"


def _request_to_answer(message: Any) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """``(session id, request id, payload)`` of the oldest request this sender
    was asked in this chat, else None. Any doubt is None. A repeat of a reply is
    :data:`_REPEAT_DELIVERY`; one the platform did not time after the request's
    message, :data:`_NOT_AFTER_REQUEST`. Blocking (DB)."""
    from app.core import owner_confirm
    from app.core.connector_service import sender_is_owner
    from app.core.permission_handler import PERMISSION_KIND
    from app.db import connectors as connectors_db
    from app.db import database, interactions
    from app.network import ownership

    if not sender_is_owner(message.connector_id, message.platform_user_id):
        return None
    if ownership.enrolled() and not ownership.owns(database.get_connector(message.connector_id)):
        return None
    message_id = str(message.platform_message_id or "").strip()
    if not message_id or message.sent_at is None:
        return None
    # Claimed before anything is looked up: a yes first delivered with nothing
    # open was an ordinary message, and its repeat must not answer a later request.
    if not connectors_db.claim_answer_message(message.connector_id, message.platform_chat_id, message_id):
        return _REPEAT_DELIVERY
    linked = database.get_connector_session(message.connector_id, message.platform_chat_id)
    session_id = (linked or {}).get("session_id")
    if not session_id or not (linked or {}).get("is_active"):
        return None
    chat = _chat_key(message.connector_id, message.platform_chat_id)
    for row in interactions.get_pending_interactions(session_id):
        payload = row.get("payload") or {}
        if row.get("kind") != PERMISSION_KIND or not payload.get("request_id"):
            continue
        asked_at = (payload.get(interactions.CHAT_PROMPTS_KEY) or {}).get(chat)
        if asked_at is None:
            continue  # never asked in this chat
        if owner_confirm.is_owner_approval(payload.get("tool_name"), payload.get("tool_input")):
            # Strictly later: platforms that report whole seconds cannot order
            # two messages in one second, so such a reply is asked for again.
            if message.sent_at <= asked_at:
                return _NOT_AFTER_REQUEST
            return session_id, payload["request_id"], payload
    return None


async def answer_from_chat(message: Any) -> bool:
    """Answer the chat's oldest open request from the owner's plain-text reply.

    True when ``message`` was that answer, a repeat delivery of a reply already
    counted, or not timed after the request (the owner is told), and needs no
    agent turn. False leaves it an ordinary message: another sender, nothing
    asked in this chat, not a firm yes or no, or no platform message ID or send
    time. Runs ahead of the per-chat message lock, because that lock is held by
    the very turn waiting for this answer. Never raises.
    """
    from app.core import owner_confirm

    try:
        verdict = owner_confirm.classify(message.text)
        if verdict == owner_confirm.UNCLEAR or message.attachments:
            return False
        found = await asyncio.to_thread(_request_to_answer, message)
        if found is None:
            return False
        if found is _REPEAT_DELIVERY:
            return True
        if found is _NOT_AFTER_REQUEST:
            await _tell_chat(message, "Couldn't tell that reply came after this request, so it did not "
                                      "answer it. Reply yes or no again to answer.")
            return True
        session_id, request_id, payload = found
        from app.core.permission_handler import permission_handler

        allow = verdict == owner_confirm.YES
        result = await permission_handler.respond(request_id, session_id, "allow" if allow else "deny")
        if not result.get("resolved"):
            return False
    except Exception:  # noqa: BLE001 — unanswered is safe; the TTL denies it
        logger.warning("connector_permissions: text answer failed", exc_info=True)
        return False
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
