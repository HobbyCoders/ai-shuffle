"""Decode-only compatibility for retired archive checkpoints and tool contexts.

Archive is not a lifecycle, execution guard or Stop action anymore. Keep import
names and the historical reason readable, but never gate, replay, stop work or
clear the explicit user Stop latch. All new membership changes use settlement.
"""
from __future__ import annotations
from typing import Any, Mapping, Optional

ARCHIVED_STATUS = "archived"
ARCHIVED_STOP_REASON = "archived"  # Historical records only; never newly written.


class SessionArchived(ValueError):
    """Legacy exception type, retained so old stored tool results can decode."""
    def __init__(self, session_id: str, message: Optional[str] = None) -> None:
        self.session_id = session_id
        super().__init__(message or "Archive is retired; this chat is ordinary Settled history.")


def is_archived_row(row: Optional[Mapping[str, Any]]) -> bool:
    return False


def is_archived(session_id: Optional[str]) -> bool:
    return False


async def is_archived_async(session_id: Optional[str]) -> bool:
    return False


def refusal(label: str, action: str) -> str:
    return f"Archive is retired. {label} uses ordinary session admission to {action}."


def log_dropped(session_id: str, source: Optional[str], detail: str = "") -> None:
    return None


def drop_queued_turns(session_id: str, turn_id: Optional[str] = None) -> int:
    return 0


def release_after_restore(session_id: str) -> None:
    return None


async def stop_work(session_id: str) -> None:
    return None
