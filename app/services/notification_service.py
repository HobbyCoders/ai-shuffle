"""
Notification emit funnel — the single server-side entry point for user-facing
notifications.

Every server-originated notification / banner goes through ``emit_notification``:

1. **Persist** the notification to the ``notifications`` table (history / the
   bell), so a message the user missed isn't lost and survives a reload.
2. **Broadcast** it live over the user-scoped ``notification`` WS event
   (``SyncEngine.broadcast_notification``) so it reaches *every* device the user
   has open — not just one watching the originating session.
3. **Optionally push** to mobile for high-severity notifications, reusing the
   existing ``fire_and_forget_notify`` adapter (cluster + local fan-out), with a
   per-(user, category) throttle so an error storm (e.g. repeated 429s) can't
   spam the user's phone.

This replaces the ad-hoc emit points (``api_error`` yielded from the stream,
``session_complete`` firing only an OS notification) with one funnel. Mobile
push / webhooks / connectors remain *delivery adapters* downstream — not
rebuilt here.

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

# Severities that warrant a mobile push by default (high signal only).
_PUSH_BY_DEFAULT = {"error"}

# Per-(user, category) mobile-push throttle: at most one push per window so an
# error storm can't spam the phone. Live WS + history persistence are NOT
# throttled — only the outbound push.
_PUSH_THROTTLE_SECONDS = 60.0
_last_push: Dict[str, float] = {}


def _should_push(user_id: str, category: Optional[str], severity: str, explicit: Optional[bool]) -> bool:
    """Decide whether to fire a mobile push, applying the per-category throttle."""
    want = explicit if explicit is not None else (severity in _PUSH_BY_DEFAULT)
    if not want:
        return False
    key = f"{user_id}\x1f{category or severity}"
    now = time.monotonic()
    last = _last_push.get(key)
    if last is not None and (now - last) < _PUSH_THROTTLE_SECONDS:
        logger.debug("Mobile push throttled for %s (category=%s)", user_id, category)
        return False
    _last_push[key] = now
    return True


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
        push: Override the severity-based mobile-push default.

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

    # 3. Optional mobile push for high-severity, throttled per category.
    if (record.get("occurrence_count", 1) <= 1
            and _should_push(user_id, category, severity, push)):
        try:
            from app.services.push_notification import fire_and_forget_notify

            fire_and_forget_notify(
                title=title or _default_push_title(severity),
                body=message,
                data={
                    "type": "notification",
                    "severity": severity,
                    "category": category,
                    "session_id": session_id,
                },
            )
        except Exception:
            logger.debug("emit_notification: push failed", exc_info=True)

    return record


def _default_push_title(severity: str) -> str:
    """A reasonable push heading when the caller didn't supply a title."""
    return {
        "error": "Error",
        "warning": "Warning",
        "success": "Done",
        "info": "AI Shuffle",
    }.get(severity, "AI Shuffle")
