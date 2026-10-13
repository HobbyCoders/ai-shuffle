"""Gmail action catalog + handlers for the connector action gateway.

Gmail is a non-messaging connector, so its gateway pair (``gmail_actions`` /
``gmail_invoke``) is registered globally for every session while a Gmail
connector is running. The google-api-python-client is synchronous, so every
API call is pushed onto a worker thread.

Nothing here permanently deletes mail: ``trash_message`` moves to Trash
(auto-purged by Gmail after ~30 days, reversible via ``untrash_message``),
which is why it is risk tier "write" rather than "destructive".

The legacy ``send_email`` action id is preserved for existing automations.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from email.mime.text import MIMEText
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.connectors.base import ActionDef

logger = logging.getLogger(__name__)


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


_MSG_ID = {"type": "string", "description": "Gmail message ID."}


GMAIL_ACTIONS: List[ActionDef] = [
    # -- compose -----------------------------------------------------------
    ActionDef(
        id="send_email",
        name="Send Email",
        description="Send a new email.",
        input_schema=_schema({
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
        }, ["to", "subject", "body"]),
        category="compose", risk="write",
    ),
    ActionDef(
        id="reply_email",
        name="Reply To Email",
        description="Reply to an existing email in its thread.",
        input_schema=_schema({
            "message_id": _MSG_ID,
            "body": {"type": "string"},
        }, ["message_id", "body"]),
        category="compose", risk="write",
    ),
    ActionDef(
        id="create_draft",
        name="Create Draft",
        description="Create a draft email without sending it.",
        input_schema=_schema({
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }, ["to", "subject", "body"]),
        category="compose", risk="write",
    ),
    # -- read --------------------------------------------------------------
    ActionDef(
        id="search_messages",
        name="Search Messages",
        description="Search mail with Gmail query syntax (e.g. 'from:alice is:unread newer_than:7d').",
        input_schema=_schema({
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ["query"]),
        category="read", risk="safe",
    ),
    ActionDef(
        id="list_messages",
        name="List Messages",
        description="List recent messages in a label (default INBOX).",
        input_schema=_schema({
            "label": {"type": "string", "description": "Label ID, e.g. 'INBOX', 'SENT'."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
        }),
        category="read", risk="safe",
    ),
    ActionDef(
        id="read_message",
        name="Read Message",
        description="Fetch one message's headers and plain-text body.",
        input_schema=_schema({"message_id": _MSG_ID}, ["message_id"]),
        category="read", risk="safe",
    ),
    ActionDef(
        id="get_profile",
        name="Get Profile",
        description="The connected mailbox's address and message counts.",
        input_schema=_schema({}),
        category="read", risk="safe",
    ),
    # -- organize ----------------------------------------------------------
    ActionDef(
        id="list_labels",
        name="List Labels",
        description="List the mailbox's labels.",
        input_schema=_schema({}),
        category="organize", risk="safe",
    ),
    ActionDef(
        id="create_label",
        name="Create Label",
        description="Create a new label.",
        input_schema=_schema({"name": {"type": "string"}}, ["name"]),
        category="organize", risk="write",
    ),
    ActionDef(
        id="modify_labels",
        name="Modify Message Labels",
        description="Add and/or remove labels on a message.",
        input_schema=_schema({
            "message_id": _MSG_ID,
            "add_labels": {"type": "array", "description": "Label IDs to add."},
            "remove_labels": {"type": "array", "description": "Label IDs to remove."},
        }, ["message_id"]),
        category="organize", risk="write",
    ),
    ActionDef(
        id="mark_read",
        name="Mark As Read",
        description="Mark a message as read.",
        input_schema=_schema({"message_id": _MSG_ID}, ["message_id"]),
        category="organize", risk="write",
    ),
    ActionDef(
        id="mark_unread",
        name="Mark As Unread",
        description="Mark a message as unread.",
        input_schema=_schema({"message_id": _MSG_ID}, ["message_id"]),
        category="organize", risk="write",
    ),
    ActionDef(
        id="archive_message",
        name="Archive Message",
        description="Archive a message (remove it from the inbox).",
        input_schema=_schema({"message_id": _MSG_ID}, ["message_id"]),
        category="organize", risk="write",
    ),
    ActionDef(
        id="trash_message",
        name="Trash Message",
        description="Move a message to Trash (reversible with untrash_message; Gmail purges Trash after ~30 days).",
        input_schema=_schema({"message_id": _MSG_ID}, ["message_id"]),
        category="organize", risk="write",
    ),
    ActionDef(
        id="untrash_message",
        name="Untrash Message",
        description="Restore a message from Trash.",
        input_schema=_schema({"message_id": _MSG_ID}, ["message_id"]),
        category="organize", risk="write",
    ),
]

ACTIONS_BY_ID: Dict[str, ActionDef] = {a.id: a for a in GMAIL_ACTIONS}


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def _ok(**payload: Any) -> Dict[str, Any]:
    return {"success": True, **payload}


async def _call(request: Any) -> Any:
    """Run a googleapiclient request's blocking .execute() on a worker thread."""
    return await asyncio.to_thread(request.execute)


def _build_mime(conn: Any, to: str, subject: str, body: str,
                cc: Optional[str] = None, bcc: Optional[str] = None) -> str:
    mime_msg = MIMEText(body)
    mime_msg["To"] = to
    mime_msg["From"] = getattr(conn, "_email_address", "") or "me"
    mime_msg["Subject"] = subject
    if cc:
        mime_msg["Cc"] = cc
    if bcc:
        mime_msg["Bcc"] = bcc
    return base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")


def _message_summary(conn: Any, msg_data: Dict[str, Any]) -> Dict[str, Any]:
    headers = msg_data.get("payload", {}).get("headers", [])
    return {
        "id": msg_data.get("id"),
        "thread_id": msg_data.get("threadId"),
        "from": conn._get_header(headers, "From"),
        "to": conn._get_header(headers, "To"),
        "subject": conn._get_header(headers, "Subject"),
        "date": conn._get_header(headers, "Date"),
        "snippet": msg_data.get("snippet", ""),
        "labels": msg_data.get("labelIds", []),
    }


async def _fetch_summaries(conn: Any, stubs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for stub in stubs:
        msg_data = await _call(conn._service.users().messages().get(
            userId="me", id=stub["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ))
        out.append(_message_summary(conn, msg_data))
    return out


Handler = Callable[[Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]
_HANDLERS: Dict[str, Handler] = {}


def _handler(action_id: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[action_id] = fn
        return fn
    return deco


# -- compose ----------------------------------------------------------------

@_handler("send_email")
async def _send_email(conn, params, ctx):
    to = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "")
    body = params.get("body")
    if not to or body is None:
        return _err("to, subject, and body are required.")
    raw = _build_mime(conn, to, subject, str(body),
                      cc=params.get("cc"), bcc=params.get("bcc"))
    result = await _call(conn._service.users().messages().send(
        userId="me", body={"raw": raw},
    ))
    return _ok(message_id=result.get("id"), thread_id=result.get("threadId"))


@_handler("reply_email")
async def _reply_email(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    body = str(params.get("body") or "")
    if not message_id or not body:
        return _err("message_id and body are required.")
    original = await _call(conn._service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID"],
    ))
    headers = original.get("payload", {}).get("headers", [])
    orig_from = conn._get_header(headers, "From")
    orig_subject = conn._get_header(headers, "Subject")
    orig_message_id = conn._get_header(headers, "Message-ID")
    _, to_email = conn._parse_sender(orig_from)
    send_body = conn._build_reply(
        thread_id=original.get("threadId", message_id),
        message_id=orig_message_id,
        to=to_email,
        subject=orig_subject,
        body=body,
    )
    result = await _call(conn._service.users().messages().send(userId="me", body=send_body))
    return _ok(message_id=result.get("id"), thread_id=result.get("threadId"), to=to_email)


@_handler("create_draft")
async def _create_draft(conn, params, ctx):
    to = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "")
    body = params.get("body")
    if not to or body is None:
        return _err("to, subject, and body are required.")
    raw = _build_mime(conn, to, subject, str(body))
    result = await _call(conn._service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}},
    ))
    return _ok(draft_id=result.get("id"))


# -- read -------------------------------------------------------------------

@_handler("search_messages")
async def _search_messages(conn, params, ctx):
    query = str(params.get("query") or "").strip()
    if not query:
        return _err("query is required.")
    try:
        max_results = max(1, min(50, int(params.get("max_results") or 10)))
    except (TypeError, ValueError):
        max_results = 10
    response = await _call(conn._service.users().messages().list(
        userId="me", q=query, maxResults=max_results,
    ))
    stubs = response.get("messages", []) or []
    return _ok(messages=await _fetch_summaries(conn, stubs), count=len(stubs), query=query)


@_handler("list_messages")
async def _list_messages(conn, params, ctx):
    label = str(params.get("label") or "INBOX").strip()
    try:
        max_results = max(1, min(50, int(params.get("max_results") or 10)))
    except (TypeError, ValueError):
        max_results = 10
    response = await _call(conn._service.users().messages().list(
        userId="me", labelIds=[label], maxResults=max_results,
    ))
    stubs = response.get("messages", []) or []
    return _ok(messages=await _fetch_summaries(conn, stubs), count=len(stubs), label=label)


@_handler("read_message")
async def _read_message(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return _err("message_id is required.")
    msg_data = await _call(conn._service.users().messages().get(
        userId="me", id=message_id, format="full",
    ))
    summary = _message_summary(conn, msg_data)
    body = conn._extract_body(msg_data.get("payload", {})) or msg_data.get("snippet", "")
    summary["body"] = body[:10000]
    return _ok(message=summary)


@_handler("get_profile")
async def _get_profile(conn, params, ctx):
    profile = await _call(conn._service.users().getProfile(userId="me"))
    return _ok(profile={
        "email": profile.get("emailAddress"),
        "messages_total": profile.get("messagesTotal"),
        "threads_total": profile.get("threadsTotal"),
    })


# -- organize ---------------------------------------------------------------

@_handler("list_labels")
async def _list_labels(conn, params, ctx):
    response = await _call(conn._service.users().labels().list(userId="me"))
    labels = [{
        "id": lbl.get("id"),
        "name": lbl.get("name"),
        "type": lbl.get("type"),
    } for lbl in response.get("labels", [])]
    return _ok(labels=labels, count=len(labels))


@_handler("create_label")
async def _create_label(conn, params, ctx):
    name = str(params.get("name") or "").strip()
    if not name:
        return _err("name is required.")
    result = await _call(conn._service.users().labels().create(
        userId="me", body={"name": name},
    ))
    return _ok(label_id=result.get("id"), name=result.get("name"))


async def _modify(conn, message_id: str, add: List[str], remove: List[str]) -> Dict[str, Any]:
    if not message_id:
        return _err("message_id is required.")
    body: Dict[str, Any] = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    if not body:
        return _err("Nothing to change — pass add_labels and/or remove_labels.")
    result = await _call(conn._service.users().messages().modify(
        userId="me", id=message_id, body=body,
    ))
    return _ok(message_id=result.get("id"), labels=result.get("labelIds", []))


@_handler("modify_labels")
async def _modify_labels(conn, params, ctx):
    add = [str(x) for x in (params.get("add_labels") or [])]
    remove = [str(x) for x in (params.get("remove_labels") or [])]
    return await _modify(conn, str(params.get("message_id") or "").strip(), add, remove)


@_handler("mark_read")
async def _mark_read(conn, params, ctx):
    return await _modify(conn, str(params.get("message_id") or "").strip(), [], ["UNREAD"])


@_handler("mark_unread")
async def _mark_unread(conn, params, ctx):
    return await _modify(conn, str(params.get("message_id") or "").strip(), ["UNREAD"], [])


@_handler("archive_message")
async def _archive_message(conn, params, ctx):
    return await _modify(conn, str(params.get("message_id") or "").strip(), [], ["INBOX"])


@_handler("trash_message")
async def _trash_message(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return _err("message_id is required.")
    result = await _call(conn._service.users().messages().trash(userId="me", id=message_id))
    return _ok(message_id=result.get("id"), trashed=True)


@_handler("untrash_message")
async def _untrash_message(conn, params, ctx):
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return _err("message_id is required.")
    result = await _call(conn._service.users().messages().untrash(userId="me", id=message_id))
    return _ok(message_id=result.get("id"), trashed=False)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(
    conn: Any,
    action_id: str,
    params: Optional[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one catalog action against a live GmailConnector."""
    handler = _HANDLERS.get(action_id)
    if handler is None:
        return _err(f"Unknown action: {action_id}")
    if getattr(conn, "_service", None) is None:
        return _err("Gmail connector is not connected.")
    try:
        return await handler(conn, params or {}, context or {})
    except Exception as e:
        logger.exception("Gmail action %s failed", action_id)
        return _err(f"Action '{action_id}' failed: {e}")


def missing_handlers() -> List[str]:
    return [a.id for a in GMAIL_ACTIONS if a.id not in _HANDLERS]
