"""WebSocket session cleanup and lifecycle management."""

import asyncio
import logging

from app.core.permission_handler import permission_handler
from app.core.user_question_handler import user_question_handler

logger = logging.getLogger(__name__)

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}
_active_sessions_lock = asyncio.Lock()

async def cleanup_session_handlers(session_id: str) -> None:
    """Clean up session-keyed data from global handler singletons.

    Called from session teardown paths (clear, eviction, worktree
    termination) to prevent unbounded growth of _pending_requests,
    _session_rules, and _pending_questions dicts over the process lifetime.
    """
    try:
        await permission_handler.cancel_all_requests(session_id)
        permission_handler.clear_session_rules(session_id)
    except Exception:
        logger.debug(f"Permission handler cleanup failed for {session_id}", exc_info=True)
    try:
        await user_question_handler.cancel_all(session_id)
    except Exception:
        logger.debug(f"Question handler cleanup failed for {session_id}", exc_info=True)


# NOTE: Session cleanup is no longer driven by a standalone delayed timer.
# A session's live-store message buffer + task registry are now torn down on
# pool eviction (LRU or idle), wired via SessionPool.set_eviction_callback →
# resource_monitor._on_pool_eviction → _cleanup_evicted_sessions. This binds
# buffer lifetime to pool membership (ONE lifecycle) and inherently respects
# in-flight background sub-agents, because the pool refuses to evict a session
# with active background work (SessionPool._has_background_work). The old 30s
# _delayed_session_cleanup / schedule_delayed_cleanup / cancel_pending_cleanup
# machinery — which raced the drain pump and could tear a parked session down
# out from under a running sub-agent — has been removed.
