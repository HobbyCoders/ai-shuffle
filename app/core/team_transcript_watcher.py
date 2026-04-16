"""
Team Transcript Watcher

Teammate Claude CLI subprocesses write their work to JSONL transcripts at
~/.claude/projects/<encoded-cwd>/<session_id>.jsonl. These files are not part
of the parent session's SDK stream, so without a tailer the user cannot see
what teammates are doing.

For each known teammate the watcher:

  1. Opens the JSONL file and seeks to the last known byte offset.
  2. Parses new lines (one JSON event per line).
  3. Maps assistant content blocks (text / tool_use / tool_result) into
     `team_member` updates on the parent session's LiveSession.
  4. Tracks usage (tokens) from the last assistant message.
  5. Detects completion when the JSONL file stops growing for a quiet window
     *and* the teammate PID is no longer running.

Polling is used instead of inotify/watchdog to avoid extra deps and to keep
behavior consistent across Linux containers and macOS dev machines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from app.core.live_session_store import live_session_store
from app.core.sync_engine import sync_engine
from app.core.team_registry import TeammateRecord, team_registry

logger = logging.getLogger(__name__)

# Poll cadence — 500ms gives good responsiveness without punishing the disk
# when dozens of teammates are running in parallel.
POLL_INTERVAL_S = 0.5

# If no new bytes appear within this window AND the PID is dead, the teammate
# is considered finished.
IDLE_COMPLETE_S = 3.0


class TeamTranscriptWatcher:
    """Coordinates per-teammate tailers.

    Each teammate gets its own asyncio task keyed by
    (parent_session_id, team_name, teammate_name). Calling `ensure_watching`
    repeatedly for the same teammate is a no-op once a task is running.
    """

    def __init__(self) -> None:
        self._tasks: Dict[Tuple[str, str, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def ensure_watching(
        self,
        parent_session_id: str,
        rec: TeammateRecord,
    ) -> None:
        if not rec.jsonl_path:
            return
        key = (parent_session_id, rec.team_name, rec.teammate_name)
        async with self._lock:
            existing = self._tasks.get(key)
            if existing and not existing.done():
                return
            task = asyncio.create_task(
                self._tail(parent_session_id, rec),
                name=f"team-tail:{rec.team_name}:{rec.teammate_name}",
            )
            self._tasks[key] = task

    async def stop_session(self, parent_session_id: str) -> None:
        async with self._lock:
            keys = [k for k in self._tasks if k[0] == parent_session_id]
            for k in keys:
                t = self._tasks.pop(k, None)
                if t and not t.done():
                    t.cancel()

    async def stop_all(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for t in tasks:
            if not t.done():
                t.cancel()

    # ------------------------------------------------------------------
    # Internal tailer loop
    # ------------------------------------------------------------------

    async def _tail(self, parent_session_id: str, rec: TeammateRecord) -> None:
        path: Path = rec.jsonl_path  # type: ignore[assignment]
        teammate_name = rec.teammate_name
        team_name = rec.team_name

        logger.info(
            f"team-watch start parent={parent_session_id} team={team_name} "
            f"teammate={teammate_name} jsonl={path}"
        )

        # Seed the live_session_store with an initial team_member message
        # so the UI renders the teammate immediately, before any transcript
        # lines are available.
        await live_session_store.start_team_member(
            session_id=parent_session_id,
            team_name=team_name,
            teammate_name=teammate_name,
            teammate_session_id=rec.teammate_session_id,
            pid=rec.pid,
            prompt=rec.last_prompt,
        )

        # Tool-use id index so tool_result lines can be matched back to the
        # originating tool_use when they appear on separate transcript lines.
        seen_tool_ids: Set[str] = set()
        last_growth_ts = time.monotonic()

        try:
            while True:
                grew = await self._consume_new_lines(
                    parent_session_id, rec, seen_tool_ids
                )
                if grew:
                    last_growth_ts = time.monotonic()

                if _is_completed(rec, last_growth_ts):
                    await self._finalize(parent_session_id, rec)
                    return

                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # Mark teammate killed if cancellation came from stop_all.
            await live_session_store.update_team_member_status(
                session_id=parent_session_id,
                team_name=team_name,
                teammate_name=teammate_name,
                status="killed",
            )
            raise
        except Exception:
            logger.exception(
                f"team-watch crashed parent={parent_session_id} "
                f"team={team_name} teammate={teammate_name}"
            )
            await live_session_store.update_team_member_status(
                session_id=parent_session_id,
                team_name=team_name,
                teammate_name=teammate_name,
                status="error",
            )

    async def _consume_new_lines(
        self,
        parent_session_id: str,
        rec: TeammateRecord,
        seen_tool_ids: Set[str],
    ) -> bool:
        """Read any new JSONL lines since the last poll. Returns True if grew."""
        path: Path = rec.jsonl_path  # type: ignore[assignment]
        if not path.exists():
            return False

        try:
            size = path.stat().st_size
        except OSError:
            return False

        if size <= rec.jsonl_offset:
            return False

        try:
            with path.open("rb") as f:
                f.seek(rec.jsonl_offset)
                data = f.read(size - rec.jsonl_offset)
                rec.jsonl_offset = f.tell()
        except OSError:
            return False

        text = data.decode("utf-8", errors="replace")
        # Partial trailing line — keep for next iteration.
        if not text.endswith("\n"):
            last_nl = text.rfind("\n")
            if last_nl == -1:
                # Nothing complete yet; roll back offset.
                rec.jsonl_offset -= len(data)
                return False
            # Only consume up to the last complete line.
            consumed = text[: last_nl + 1]
            rec.jsonl_offset -= len(data) - len(consumed.encode("utf-8"))
            text = consumed

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            await self._dispatch_event(parent_session_id, rec, ev, seen_tool_ids)

        # Tick the parent session so polling delivers updates immediately.
        await sync_engine.broadcast_stream_chunk(
            session_id=parent_session_id,
            message_id=f"team:{rec.team_name}:{rec.teammate_name}",
            chunk_type="team_member_update",
            chunk_data={
                "team_name": rec.team_name,
                "teammate_name": rec.teammate_name,
            },
            source_device_id=None,
        )
        return True

    async def _dispatch_event(
        self,
        parent_session_id: str,
        rec: TeammateRecord,
        ev: Dict[str, Any],
        seen_tool_ids: Set[str],
    ) -> None:
        etype = ev.get("type")
        if etype not in ("assistant", "user"):
            return

        msg = ev.get("message") or {}
        content = msg.get("content")

        # Capture usage from assistant messages when present.
        if etype == "assistant":
            usage = msg.get("usage") or {}
            if usage:
                rec.usage = {
                    "total_tokens": (
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                    ),
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
                    "cache_read_tokens": usage.get("cache_read_input_tokens"),
                }
                await live_session_store.update_team_member_status(
                    session_id=parent_session_id,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    usage=rec.usage,
                )

        # `content` can be a plain string (user) or a list of blocks (assistant
        # or tool_result-bearing user).
        if isinstance(content, str):
            if etype == "assistant" and content.strip():
                await live_session_store.append_team_member_text(
                    session_id=parent_session_id,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    content=content,
                )
            return

        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "text":
                txt = block.get("text") or ""
                if txt:
                    await live_session_store.append_team_member_text(
                        session_id=parent_session_id,
                        team_name=rec.team_name,
                        teammate_name=rec.teammate_name,
                        content=txt,
                    )

            elif btype == "tool_use":
                tool_id = block.get("id")
                if not tool_id or tool_id in seen_tool_ids:
                    continue
                seen_tool_ids.add(tool_id)
                await live_session_store.add_team_member_tool_use(
                    session_id=parent_session_id,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    tool_id=tool_id,
                    tool_name=block.get("name") or "unknown",
                    tool_input=block.get("input") or {},
                )

            elif btype == "tool_result":
                tool_id = block.get("tool_use_id") or block.get("id")
                if not tool_id:
                    continue
                result_content = block.get("content")
                if isinstance(result_content, list):
                    # content list — concatenate text blocks for display.
                    parts = []
                    for c in result_content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text") or "")
                        elif isinstance(c, str):
                            parts.append(c)
                    result_text = "\n".join(p for p in parts if p)
                else:
                    result_text = str(result_content or "")

                is_error = bool(block.get("is_error"))
                await live_session_store.add_team_member_tool_result(
                    session_id=parent_session_id,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    tool_id=tool_id,
                    result=result_text,
                    is_error=is_error,
                )

    async def _finalize(self, parent_session_id: str, rec: TeammateRecord) -> None:
        logger.info(
            f"team-watch done parent={parent_session_id} team={rec.team_name} "
            f"teammate={rec.teammate_name}"
        )
        status = rec.status if rec.status != "running" else "completed"
        await team_registry.update_teammate(
            parent_session_id,
            rec.team_name,
            rec.teammate_name,
            status=status,
        )
        await live_session_store.update_team_member_status(
            session_id=parent_session_id,
            team_name=rec.team_name,
            teammate_name=rec.teammate_name,
            status=status,
            usage=rec.usage,
        )
        await _persist_team_member(parent_session_id, rec.team_name, rec.teammate_name)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _persist_team_member(
    parent_session_id: str,
    team_name: str,
    teammate_name: str,
) -> None:
    """Flush the current team_member LiveMessage to the session_messages table."""
    from app.db import database
    from app.db.async_database import run_sync

    session = live_session_store.get_session(parent_session_id)
    if not session:
        return
    msg = None
    for m in session.messages:
        if (
            m.type == "team_member"
            and m.team_name == team_name
            and m.teammate_name == teammate_name
        ):
            msg = m
            break
    if msg is None:
        return

    # Serialize children using the same shape db_message_loader understands
    # for subagents, plus the teammate-specific fields.
    children = [c.to_dict() for c in (msg.agent_children or [])]
    metadata = {
        "team_name": team_name,
        "teammate_name": teammate_name,
        "teammate_session_id": msg.teammate_session_id,
        "teammate_pid": msg.teammate_pid,
        "teammate_status": msg.teammate_status,
        "agent_prompt": msg.agent_prompt,
        "agent_children": children,
        "agent_usage": msg.agent_usage,
    }
    try:
        await run_sync(
            database.upsert_team_member_message,
            session_id=parent_session_id,
            team_name=team_name,
            teammate_name=teammate_name,
            content=msg.content or "",
            metadata=metadata,
        )
    except Exception:
        logger.exception("persist team_member failed")


def _pid_running(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but owned by another user — treat as alive.
    except OSError:
        return False
    return True


def _is_completed(rec: TeammateRecord, last_growth_ts: float) -> bool:
    """Teammate is done when its transcript has been idle AND its PID is gone.

    If we never learned the PID we fall back to a longer idle timeout so slow
    but still-running teammates aren't finalized prematurely.
    """
    idle_for = time.monotonic() - last_growth_ts
    if rec.pid:
        return idle_for >= IDLE_COMPLETE_S and not _pid_running(rec.pid)
    # Without a PID, require a much longer idle period — roughly the skill's
    # 5-second synthesis cadence plus slack.
    return idle_for >= (IDLE_COMPLETE_S * 10)


# Singleton
team_transcript_watcher = TeamTranscriptWatcher()
