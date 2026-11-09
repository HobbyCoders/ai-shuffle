"""The shared owner switch and its idempotent, local-only pending-prompt sweep."""
from __future__ import annotations

import logging

from app.core import away_mode
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


def _pending_sessions() -> list[str]:
    """Start with pending tables/live private cards, never scan all chats."""
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT session_id FROM pending_interactions "
            "WHERE state='pending' AND kind IN ('permission', 'question') "
            "AND (expires_at IS NULL OR expires_at >= NOW())"
        )
        ids = {row["session_id"] for row in cursor.fetchall()}
    # Read these event-loop-owned registries on the app loop, below.
    return list(ids)


def _covered_local(session_id: str) -> bool:
    from app.core.config import settings
    from app.db import sessions
    from app.network.runners import self_device_id

    row = sessions.get_session(session_id)
    if not row:
        return False
    owner = row.get("owner_device_id")
    local = (owner == self_device_id() if owner else
             row.get("instance_id") == settings.effective_instance_id)
    return local and away_mode.globally_covered(session_id)


async def release_local_pending() -> dict:
    """Release only covered chats executed here; repeated/overlapping sweeps are safe.

    Permission cards share one durable state machine, including starter-chat,
    remote-task and owner_confirm cards. Private-access cards have local futures.
    Guarded DB answers and done futures make every successful release one-shot.
    """
    from app.core.chat_access import broker

    ids = set(await run_sync(_pending_sessions))
    ids.update(r.session_id for r in list(broker.requests.values()) if not r.result.done())
    ids.update(a.session_id for a in list(broker.actions.values()) if not a.decision.done())
    counts = {"released_questions": 0, "deferred_approvals": 0}
    for session_id in sorted(ids):
        try:
            if not await run_sync(away_mode.global_away_enabled):
                break
            if not await run_sync(_covered_local, session_id):
                continue
            counts["released_questions"] += await away_mode.release_pending_questions(session_id)
            counts["deferred_approvals"] += await away_mode.release_pending_approvals(session_id)
            counts["deferred_approvals"] += await broker.defer_pending_away(session_id)
        except Exception:  # noqa: BLE001 — one broken prompt must not strand other chats
            logger.warning("Could not release global Away prompts for %s", session_id, exc_info=True)
    return counts


async def release_if_enabled() -> None:
    """Sync callback: read after cache invalidation, off-thread and outside locks."""
    if await run_sync(away_mode.global_away_enabled):
        await release_local_pending()


def notify_changed() -> None:
    from app.core.global_events import global_events

    for kind in ("admin", "api_user", "account", "paired"):
        global_events.resources_changed("away_mode", user_type=kind)


async def apply(away: bool) -> dict:
    from app.db import database

    await run_sync(database.set_system_setting, away_mode.GLOBAL_SETTING, "1" if away else "0")
    notify_changed()
    counts = await release_local_pending() if away else {
        "released_questions": 0, "deferred_approvals": 0,
    }
    return {"away": away, **counts}
