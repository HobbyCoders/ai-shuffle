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
    termination) so per-session permission rules and pending permission and
    question requests do not accumulate over the process lifetime.
    """
    try:
        await permission_handler.cancel_all_requests(session_id)
        permission_handler.clear_session_rules(session_id)
    except Exception:
        logger.debug(f"Permission handler cleanup failed for {session_id}", exc_info=True)
    try:
        await user_question_handler.cancel_all(session_id, include_nonblocking=False)
    except Exception:
        logger.debug(f"Question handler cleanup failed for {session_id}", exc_info=True)


# Per-session teardown happens on pool eviction (resource_monitor._on_pool_eviction).
