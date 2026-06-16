"""Emergency-stop switch for computer-use actions.

A single process-global flag the agent's input actions check *before* every
operation. The user can flip it instantly from the UI ("Stop controlling my
computer" → ``POST /api/computer-use/stop``) or it can be tripped by session
cancellation. While stopped, every computer-use action refuses to run.

It is deliberately a module-global (not per-session): the panic button must halt
*all* synthetic input at once, regardless of which session issued it.

A plain ``bool`` is used rather than ``asyncio.Event`` because the flag is read
from worker threads (``asyncio.to_thread``) and from multiple event loops (the
HTTP request loop vs. the query loop); a bool guarded by the GIL is the simplest
thing that is correct across all of them.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# True => computer-use is paused and all actions refuse to run.
_STOPPED: bool = False


def request_stop() -> None:
    """Engage the emergency stop. All computer-use actions will refuse to run."""
    global _STOPPED
    if not _STOPPED:
        logger.warning("computer-use EMERGENCY STOP engaged — refusing all actions")
    _STOPPED = True


def resume() -> None:
    """Clear the emergency stop, re-enabling computer-use actions."""
    global _STOPPED
    if _STOPPED:
        logger.info("computer-use emergency stop cleared — actions re-enabled")
    _STOPPED = False


def is_stopped() -> bool:
    """Return True if computer-use is currently halted by the emergency stop."""
    return _STOPPED
