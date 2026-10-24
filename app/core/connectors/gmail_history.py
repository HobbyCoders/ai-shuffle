"""Read-only Gmail history polling inside the isolated connector package."""
from __future__ import annotations

from typing import Any

RESYNC_LIMIT = 100


def _message(users: Any, mid: str) -> tuple[str, dict[str, Any]]:
    message = users.messages().get(
        userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"]
    ).execute()
    headers = {
        h["name"].lower(): h.get("value", "")
        for h in message.get("payload", {}).get("headers", [])
    }
    return "email_received:" + mid, dict(
        event_type="email_received", message_id=mid,
        sender=headers.get("from", ""), subject=headers.get("subject", ""),
        snippet=str(message.get("snippet", ""))[:4000],
    )


def _resync(
    users: Any, labels: list[str], expired: str
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    # Anchor BEFORE the snapshot: arrivals during resync replay via history.
    # Exactly one page, including already-read mail; never an unbounded mailbox scan.
    anchor = str(users.getProfile(userId="me").execute()["historyId"])
    page = users.messages().list(
        userId="me", labelIds=labels, maxResults=RESYNC_LIMIT
    ).execute()
    events = [_message(users, str(item["id"])) for item in page.get("messages", [])]
    return events, {"history_id": anchor, "history_gap": {
        "expired_history_id": expired, "resumed_history_id": anchor,
        "max_messages": RESYNC_LIMIT, "truncated": bool(page.get("nextPageToken")),
    }}


def poll(
    instance: Any, cursor: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Return one replayable page; only the host's durable commit advances it."""
    sdk = instance.build_thread_client()
    if sdk is None:
        raise ValueError("Gmail is not connected")
    users = sdk.users()
    labels = [
        s.strip() for s in str(instance.config.get("labels", "INBOX")).split(",") if s.strip()
    ]
    if "legacy_seen_ids" in cursor:
        history_id = cursor.get("history_id") or str(
            users.getProfile(userId="me").execute()["historyId"]
        )
        args: dict[str, Any] = dict(
            userId="me", maxResults=100, labelIds=labels,
            q=("after:" + str(cursor["legacy_after"])) if cursor.get("legacy_after") else "is:unread",
        )
        if cursor.get("legacy_page_token"):
            args["pageToken"] = cursor["legacy_page_token"]
        page = users.messages().list(**args).execute()
        events = [
            _message(users, str(item["id"])) for item in page.get("messages", [])
            if str(item["id"]) not in cursor["legacy_seen_ids"]
        ]
        checkpoint = {"history_id": history_id}
        if page.get("nextPageToken"):
            checkpoint.update(
                legacy_seen_ids=cursor["legacy_seen_ids"], legacy_page_token=page["nextPageToken"]
            )
            if cursor.get("legacy_after"):
                checkpoint["legacy_after"] = cursor["legacy_after"]
        return events, checkpoint
    if not cursor.get("history_id"):
        return [], {"history_id": str(users.getProfile(userId="me").execute()["historyId"])}
    args = dict(
        userId="me", startHistoryId=cursor["history_id"],
        historyTypes=["messageAdded"], maxResults=100,
    )
    if cursor.get("page_token"):
        args["pageToken"] = cursor["page_token"]
    try:
        page = users.history().list(**args).execute()
    except Exception as exc:
        # Only Gmail's expired startHistoryId response permits rebasing. Auth,
        # rate-limit and transport errors leave the durable cursor untouched.
        if getattr(getattr(exc, "resp", None), "status", None) != 404:
            raise
        return _resync(users, labels, str(cursor["history_id"]))
    messages: dict[str, Any] = {}
    for history in page.get("history", []):
        for added in history.get("messagesAdded", []):
            msg = added["message"]
            if not labels or set(labels).intersection(msg.get("labelIds", [])):
                messages[str(msg["id"])] = msg
    events = [_message(users, mid) for mid in messages]
    next_cursor = {"history_id": str(page.get("historyId", cursor["history_id"]))}
    if page.get("nextPageToken"):
        next_cursor = {"history_id": cursor["history_id"], "page_token": page["nextPageToken"]}
    return events, next_cursor
