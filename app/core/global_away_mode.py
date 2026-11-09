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


async def release_session_pending(session_id: str, *, strict: bool = False) -> dict:
    """Best-effort recovery; startup can request failure reporting for retry."""
    from app.core.chat_access import broker

    counts = {"released_questions": 0, "deferred_approvals": 0}
    try:
        if (await run_sync(away_mode.global_away_enabled)
                and await run_sync(_covered_local, session_id)):
            counts["released_questions"] += await away_mode.release_pending_questions(session_id)
            counts["deferred_approvals"] += await away_mode.release_pending_approvals(session_id)
            counts["deferred_approvals"] += await broker.defer_pending_away(session_id)
    except Exception:  # noqa: BLE001 — a broken prompt must never fail a handoff
        logger.warning("Could not release global Away prompts for %s", session_id, exc_info=True)
        if strict:
            raise
    return counts


def release_session_after_commit(conn, session_id: str) -> None:
    """Schedule on the app loop only after durable ownership/history publication.

    All acceptance paths, including synchronous transfer recovery, use this
    boundary. Before the app loop exists the startup sweep handles recovery.
    """
    import asyncio
    from app.core.detached_tasks import spawn_detached
    from app.core.global_events import global_events
    from app.db.base import after_commit

    def schedule():
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = global_events._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(
                    lambda: spawn_detached(release_session_pending(session_id),
                                           name=f"global-away-{session_id}"))
        except Exception:  # noqa: BLE001 — never fail ownership acceptance
            logger.warning("Could not schedule global Away recovery for %s", session_id, exc_info=True)

    after_commit(conn, schedule)


async def release_local_pending(*, strict: bool = False) -> dict:
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
    failed = False
    for session_id in sorted(ids):
        try:
            released = (await release_session_pending(session_id, strict=True) if strict
                        else await release_session_pending(session_id))
            for key in counts:
                counts[key] += released[key]
        except Exception:  # noqa: BLE001 — still release the other chats on this pass
            failed = True
    if failed:
        raise RuntimeError("Global Away sweep did not release every chat")
    return counts


async def release_if_enabled(*, strict: bool = False) -> None:
    """Sync callback; startup requires a clean sweep before it stops retrying."""
    if await run_sync(away_mode.global_away_enabled):
        await release_local_pending(strict=strict)


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
