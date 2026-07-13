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
     `team.member_*` contract events appended (unfenced) to the parent
     session's event log — teammates run outside any turn's run lease.
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
from typing import Any, Dict, List, Optional, Set, Tuple

from app.contract import events as contract
from app.core.team_registry import TeammateRecord, team_registry
from app.core.turn_recorder import _append_unfenced, _build_event

logger = logging.getLogger(__name__)

# Poll cadence — 500ms gives good responsiveness without punishing the disk
# when dozens of teammates are running in parallel.
POLL_INTERVAL_S = 0.5

# If no new bytes appear within this window AND the PID is dead, the teammate
# is considered finished.
IDLE_COMPLETE_S = 3.0


async def _append_team_event(
    session_id: str, model_cls: type, **fields: Any
) -> None:
    """Append one team.member_* contract event to the parent session's log.

    Unfenced (run_id=None) — teammate transcripts are tailed between/across
    turns with no owning run lease. Never raises: a recording bug must never
    break the tailer loop.
    """
    try:
        await _append_unfenced(
            session_id, [_build_event(model_cls, session_id, **fields)]
        )
    except Exception:
        logger.warning(
            "team event append failed for %s (%s)",
            session_id, getattr(model_cls, "__name__", model_cls), exc_info=True,
        )


def _completed_status(status: Optional[str]) -> str:
    """Map a registry/watcher status onto the TeamMemberCompleted enum.

    The contract enum is {completed, failed, killed}; the watcher's internal
    "error" (crash) maps to "failed" — same convention as
    history_backfill._terminal_status.
    """
    s = (status or "").lower()
    if s in ("error", "failed"):
        return "failed"
    if s in ("killed", "cancelled", "canceled"):
        return "killed"
    return "completed"


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

        # Record the teammate immediately so the UI renders the card before
        # any transcript lines are available.
        await _append_team_event(
            parent_session_id,
            contract.TeamMemberStarted,
            team_name=team_name,
            teammate_name=teammate_name,
            description=None,
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
            await _append_team_event(
                parent_session_id,
                contract.TeamMemberCompleted,
                team_name=team_name,
                teammate_name=teammate_name,
                status="killed",
                usage=rec.usage or None,
            )
            raise
        except Exception:
            logger.exception(
                f"team-watch crashed parent={parent_session_id} "
                f"team={team_name} teammate={teammate_name}"
            )
            await _append_team_event(
                parent_session_id,
                contract.TeamMemberCompleted,
                team_name=team_name,
                teammate_name=teammate_name,
                status="failed",
                usage=rec.usage or None,
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
                await _append_team_event(
                    parent_session_id,
                    contract.TeamMemberProgress,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    kind=None,
                    payload={"usage": rec.usage},
                    tokens=rec.usage.get("total_tokens"),
                )

        # `content` can be a plain string (user) or a list of blocks (assistant
        # or tool_result-bearing user).
        if isinstance(content, str):
            if etype == "assistant" and content.strip():
                await _append_team_event(
                    parent_session_id,
                    contract.TeamMemberProgress,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    kind="chunk",
                    payload={"content": content},
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
                    await _append_team_event(
                        parent_session_id,
                        contract.TeamMemberProgress,
                        team_name=rec.team_name,
                        teammate_name=rec.teammate_name,
                        kind="chunk",
                        payload={"content": txt},
                    )

            elif btype == "tool_use":
                tool_id = block.get("id")
                if not tool_id or tool_id in seen_tool_ids:
                    continue
                seen_tool_ids.add(tool_id)
                await _append_team_event(
                    parent_session_id,
                    contract.TeamMemberProgress,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    kind="tool_use",
                    payload={
                        "id": tool_id,
                        "name": block.get("name") or "unknown",
                        "input": block.get("input") or {},
                    },
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
                await _append_team_event(
                    parent_session_id,
                    contract.TeamMemberProgress,
                    team_name=rec.team_name,
                    teammate_name=rec.teammate_name,
                    kind="tool_result",
                    payload={
                        "tool_use_id": tool_id,
                        "output": result_text,
                        "is_error": is_error,
                    },
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
        contract_status = _completed_status(status)
        await _append_team_event(
            parent_session_id,
            contract.TeamMemberCompleted,
            team_name=rec.team_name,
            teammate_name=rec.teammate_name,
            status=contract_status,
            usage=rec.usage or None,
        )
        await _persist_team_member(parent_session_id, rec, status)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _children_from_events(
    team_name: str,
    teammate_name: str,
    progress_payloads: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rebuild the persisted child list from team.member_progress payloads.

    Produces the same camelCase child dict shape SubagentChild.to_dict()
    writes for subagents, so db_message_loader renders team members
    identically: consecutive chunks coalesce into one text child; tool_use
    opens a tool child; tool_result closes the matching tool child.
    """
    prefix = f"{team_name}-{teammate_name}"
    children: List[Dict[str, Any]] = []
    seen_tool_ids: Set[str] = set()
    text_idx = 0

    for payload in progress_payloads:
        kind = payload.get("kind")
        inner = payload.get("payload") or {}
        if not isinstance(inner, dict):
            continue

        if kind == "chunk":
            txt = inner.get("content") or ""
            if not txt:
                continue
            last = children[-1] if children else None
            if last is not None and last.get("type") == "text":
                last["content"] = (last.get("content") or "") + txt
            else:
                text_idx += 1
                children.append({
                    "id": f"{prefix}-text-{text_idx}",
                    "type": "text",
                    "content": txt,
                    "streaming": False,
                })

        elif kind == "tool_use":
            tool_id = inner.get("id") or ""
            if not tool_id or tool_id in seen_tool_ids:
                continue
            seen_tool_ids.add(tool_id)
            children.append({
                "id": f"{prefix}-tool-{tool_id}",
                "type": "tool_use",
                "content": "",
                "streaming": False,
                "toolName": inner.get("name") or "unknown",
                "toolId": tool_id,
                "toolInput": inner.get("input") or {},
                "toolStatus": "running",
            })

        elif kind == "tool_result":
            tool_id = inner.get("tool_use_id") or ""
            if not tool_id:
                continue
            for child in children:
                if child.get("toolId") == tool_id:
                    child["toolResult"] = inner.get("output") or ""
                    child["toolStatus"] = (
                        "error" if inner.get("is_error") else "complete"
                    )
                    break

    return children


async def _persist_team_member(
    parent_session_id: str,
    rec: TeammateRecord,
    status: str,
) -> None:
    """Flush the teammate's transcript to the session_messages table.

    Re-sourced from the session EVENT LOG (the watcher's own team.member_*
    appends) now that the live store no longer holds team state: the
    teammate's progress events are replayed into the same persisted child
    shape the old LiveMessage serialization produced.
    """
    from app.db import database
    from app.db import session_events as session_events_db
    from app.db.async_database import run_sync

    team_name = rec.team_name
    teammate_name = rec.teammate_name

    # Page through the parent session's log collecting this teammate's
    # progress payloads (reads run off-loop via run_sync).
    progress_payloads: List[Dict[str, Any]] = []
    from_seq = 0
    try:
        while True:
            rows = await run_sync(
                session_events_db.get_events, parent_session_id, from_seq, 500
            )
            if not rows:
                break
            for row in rows:
                if row.get("event_type") != "team.member_progress":
                    continue
                payload = row.get("payload") or {}
                if (
                    payload.get("team_name") == team_name
                    and payload.get("teammate_name") == teammate_name
                ):
                    progress_payloads.append(payload)
            from_seq = rows[-1]["seq"]
    except Exception:
        logger.exception("persist team_member: event log read failed")
        return

    children = _children_from_events(team_name, teammate_name, progress_payloads)

    # Serialize using the same shape db_message_loader understands for
    # subagents, plus the teammate-specific fields.
    metadata = {
        "team_name": team_name,
        "teammate_name": teammate_name,
        "teammate_session_id": rec.teammate_session_id,
        "teammate_pid": rec.pid,
        "teammate_status": status,
        "agent_prompt": rec.last_prompt,
        "agent_children": children,
        "agent_usage": rec.usage or None,
    }
    try:
        await run_sync(
            database.upsert_team_member_message,
            session_id=parent_session_id,
            team_name=team_name,
            teammate_name=teammate_name,
            content="",
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
