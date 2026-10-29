"""
Notification emit funnel — the single server-side entry point for user-facing
notifications.

Every server-originated notification / banner goes through ``emit_notification``:

1. **Persist** the notification to the ``notifications`` table (history / the
   bell), so a message the user missed isn't lost and survives a reload.
2. **Broadcast** it live over the user-scoped ``notification`` WS event
   (``SyncEngine.broadcast_notification``) so it reaches *every* device the user
   has open — not just one watching the originating session.
Phone push is deliberately separate: only the attention dispatcher can send.
Errors, completions, parking, watches and expiry notices stay in the bell.

This replaces the ad-hoc bell emit points (``api_error`` and
``session_complete``) with one persist-and-broadcast funnel. Mobile push is
not a delivery adapter for this funnel.

Defensive throughout: a notification failing to persist or broadcast must never
break the streaming / request path it was emitted from.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ``notification`` is the persistent bell surface. ``banner`` is an explicit
# live surface and is not added to bell history unless the caller opts in.
# Persistence is surface-based, not severity-based; every bell severity persists.
_PERSIST_BY_DEFAULT = {"notification"}


def _normalize_surface(surface: Optional[str]) -> str:
    """Collapse legacy wire values into the two supported surfaces."""
    return "banner" if surface == "banner" else "notification"

def _should_push(user_id: str, category: Optional[str], severity: str, explicit: Optional[bool]) -> bool:
    """Bell events never push, even for legacy callers passing push=True."""
    return False


async def emit_notification(
    user_id: str,
    *,
    severity: str = "info",
    message: str,
    title: Optional[str] = None,
    category: Optional[str] = None,
    surface: str = "notification",
    session_id: Optional[str] = None,
    persist: Optional[bool] = None,
    action: Optional[Dict[str, Any]] = None,
    push: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Emit a user-facing notification through the unified funnel.

    Args:
        user_id: Recipient (``api_user["id"]`` or ``"admin"`` — same key the WS
            broadcast path uses).
        severity: ``error`` | ``warning`` | ``info`` | ``success``.
        message: The body text.
        title: Optional heading.
        category: Stable tag (e.g. ``api_error``, ``session_complete``) the
            frontend keys interactive actions on (e.g. "Fix with AI").
        surface: ``notification`` (persistent bell item) or ``banner``
            (explicit live banner). Legacy ``toast`` / ``inline`` values normalize
            to ``notification``.
        session_id: Originating session, if any.
        persist: Override the surface-based history default. Banners are live-only
            unless this is explicitly true.
        action: Optional ``{"kind": str, "payload": dict}`` action descriptor,
            stored so the frontend can rebuild the button on hydration.
        push: Retired compatibility argument; ignored. Bell events never push.

    Returns the persisted notification (wire shape) or ``None`` on failure.
    Never raises — emitting a notification must not break its caller.
    """
    surface = _normalize_surface(surface)
    should_persist = persist if persist is not None else (surface in _PERSIST_BY_DEFAULT)
    action_kind = action.get("kind") if action else None
    action_payload = action.get("payload") if action else None

    record: Optional[Dict[str, Any]] = None

    # 1. Persist (history / the bell). Best-effort — a failed insert still lets
    #    the live broadcast through so the user at least sees it now.
    if should_persist:
        try:
            from app.db.async_database import run_sync
            from app.db import notifications as notifications_db

            record = await run_sync(
                lambda: notifications_db.create_notification(
                    user_id,
                    severity=severity,
                    message=message,
                    surface=surface,
                    category=category,
                    source="server",
                    title=title,
                    action_kind=action_kind,
                    action_payload=action_payload,
                    session_id=session_id,
                )
            )
        except Exception:
            logger.debug("emit_notification: persist failed", exc_info=True)
            record = None

    # Build the wire payload the frontend store ingests. Reuse the persisted
    # row when available (carries the real id + created_at); otherwise synthesize
    # a transient, non-persisted payload so live delivery still works.
    if record is None:
        record = {
            "id": f"s{int(time.time() * 1000)}",
            "user_id": user_id,
            "severity": severity,
            "surface": surface,
            "category": category,
            "source": "server",
            "title": title,
            "message": message,
            "session_id": session_id,
            "read": False,
            "dismissed": False,
            "persist": bool(should_persist),
            "created_at": int(time.time() * 1000),
            "action": ({"kind": action_kind, "payload": action_payload} if action_kind else None),
        }

    # 2. Broadcast live to all of the user's devices (user-scoped, never
    #    session/device-scoped — so it reaches devices not watching this session).
    try:
        from app.core.sync_engine import sync_engine

        await sync_engine.broadcast_notification(user_id, record)
    except Exception:
        logger.debug("emit_notification: broadcast failed", exc_info=True)

    return record


def _default_push_title(severity: str) -> str:
    """A reasonable push heading when the caller didn't supply a title."""
    return {
        "error": "Error",
        "warning": "Warning",
        "success": "Done",
        "info": "AI Shuffle",
    }.get(severity, "AI Shuffle")
