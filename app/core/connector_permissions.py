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
_SENT_BEFORE_REQUEST: Tuple[str, str, Dict[str, Any]] = ("", "", {})


async def deliver_permission_prompt(
    session_id: str,
    request_id: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    description: str,
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
            text = await asyncio.to_thread(_prompt_text, cs, tool_name, tool_input)
            instance = connector_manager.get_running_instance(connector_id)
            if instance is None:
                logger.debug(
                    "connector_permissions: connector %s not running — "
                    "skipping prompt delivery", connector_id,
                )
                continue
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


def _prompt_text(connector_session: Dict[str, Any], tool_name: str, tool_input: Any) -> str:
    """What this chat is told. The action and the invitation to reply go only
    to a chat started from the owner's own account. Blocking (DB read)."""
    from app.core import owner_confirm
    from app.core.connector_service import sender_is_owner

    if owner_confirm.is_guardrail(tool_name, tool_input) or not sender_is_owner(
            connector_session.get("connector_id"), connector_session.get("platform_user_id")):
        return waiting_for_owner_text(tool_name)
    action = owner_confirm.describe(tool_name, tool_input)
    return f"The agent wants to {action}. Reply yes or no to answer."


def _request_to_answer(message: Any) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """``(session id, request id, payload)`` of the oldest request this sender
    may answer in this chat, else None. Any doubt is None. A repeat of a reply
    is :data:`_REPEAT_DELIVERY`; one sent before the request opened,
    :data:`_SENT_BEFORE_REQUEST`. Blocking (DB)."""
    from app.core import owner_confirm
    from app.core.connector_service import sender_is_owner
    from app.core.connectors.base import sent_at_seconds
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
    for row in interactions.get_pending_interactions(session_id):
        payload = row.get("payload") or {}
        if row.get("kind") != PERMISSION_KIND or not payload.get("request_id"):
            continue
        if owner_confirm.is_owner_approval(payload.get("tool_name"), payload.get("tool_input")):
            # A reply cannot answer a question not yet asked. Claims stay on the
            # worker that made them, so this is what stops a reply another worker
            # already counted from answering here after the connector moved.
            opened = sent_at_seconds(row.get("created_at"))
            if opened is None or message.sent_at < int(opened):
                return _SENT_BEFORE_REQUEST
            return session_id, payload["request_id"], payload
    return None


async def answer_from_chat(message: Any) -> bool:
    """Answer the chat's oldest open request from the owner's plain-text reply.

    True when ``message`` was that answer, a repeat delivery of a reply already
    counted, or sent before the request opened (the owner is told), and needs
    no agent turn. False leaves it an ordinary message: another sender, nothing
    open, not a firm yes or no, or no platform message ID or send time. Runs ahead of the per-chat message lock, because that lock is held by
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
        if found is _SENT_BEFORE_REQUEST:
            await _tell_chat(message, "That reply was sent before this request opened, so it did not "
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
