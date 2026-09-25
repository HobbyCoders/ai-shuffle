"""
Team Registry - Tracks Claude Code agent teams spawned by a parent session.

When the parent session calls TeamCreate / Task(team_name=...) / SendMessage,
each teammate runs as a separate Claude CLI subprocess with its own session_id
and JSONL transcript. The registry maps:

    parent_session_id -> team_name -> {teammate_name -> TeammateRecord}

Teammate discovery is defensive because Claude's internal on-disk layout
under ~/.claude/teams/{team_name}/ is not part of a stable public contract:

1. Best source: parse tool_use `input` and tool_result `output` from the
   parent's SDK stream — these contain explicit team_name / teammate_name /
   session_id fields.
2. Fallback: scan ~/.claude/teams/{team_name}/ for any JSON files containing
   session_id / pid / name.
3. JSONL path resolved via the existing jsonl_parser helpers (cwd-encoded
   directory under ~/.claude/projects/).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.config import settings
from app.core.jsonl_parser import get_project_dir_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TeammateRecord:
    """One teammate subprocess belonging to a team."""

    team_name: str
    teammate_name: str
    teammate_session_id: Optional[str] = None
    jsonl_path: Optional[Path] = None
    pid: Optional[int] = None
    status: str = "running"   # running | completed | error | killed | unknown
    working_dir: Optional[str] = None
    last_prompt: Optional[str] = None
    # Monotonic byte offset into the JSONL — set by the transcript watcher.
    jsonl_offset: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "team_name": self.team_name,
            "teammate_name": self.teammate_name,
            "teammate_session_id": self.teammate_session_id,
            "pid": self.pid,
            "status": self.status,
            "usage": self.usage,
        }


@dataclass
class TeamRecord:
    """A single team owned by a parent session."""

    parent_session_id: str
    team_name: str
    working_dir: Optional[str] = None
    teammates: Dict[str, TeammateRecord] = field(default_factory=dict)
    created_at: float = 0.0

    def public(self) -> Dict[str, Any]:
        return {
            "team_name": self.team_name,
            "parent_session_id": self.parent_session_id,
            "teammates": [m.to_public_dict() for m in self.teammates.values()],
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Tool names that signal team activity. Kept lower-cased for matching.
TEAM_TOOLS = {"TeamCreate", "Task", "SendMessage", "TeamDelete"}

# Regex that matches a UUID in tool output — teammate session ids.
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


class TeamRegistry:
    """In-memory registry of active teams.

    Thread-safety: all public methods are async and hold `_lock` while
    mutating shared state. Watcher tasks call back into the registry; those
    updates are pushed through `update_teammate()` which re-acquires the lock.
    """

    def __init__(self) -> None:
        # parent_session_id -> team_name -> TeamRecord
        self._teams: Dict[str, Dict[str, TeamRecord]] = {}
        self._lock = asyncio.Lock()
        # Optional callback invoked on every teammate state change so the
        # transcript watcher / live session store can react. Signature:
        #     (parent_session_id, team_name, TeammateRecord) -> Awaitable[None]
        self._on_teammate_update: Optional[
            Callable[[str, str, TeammateRecord], Awaitable[None]]
        ] = None

    def set_update_callback(
        self,
        cb: Callable[[str, str, TeammateRecord], Awaitable[None]],
    ) -> None:
        self._on_teammate_update = cb

    # ------------------------------------------------------------------
    # Public query helpers
    # ------------------------------------------------------------------

    async def list_teams(self, parent_session_id: str) -> List[Dict[str, Any]]:
        async with self._lock:
            teams = self._teams.get(parent_session_id, {})
            return [t.public() for t in teams.values()]

    async def get_teammate(
        self,
        parent_session_id: str,
        team_name: str,
        teammate_name: str,
    ) -> Optional[TeammateRecord]:
        async with self._lock:
            t = self._teams.get(parent_session_id, {}).get(team_name)
            if not t:
                return None
            return t.teammates.get(teammate_name)

    async def all_teammates(self, parent_session_id: str) -> List[TeammateRecord]:
        async with self._lock:
            out: List[TeammateRecord] = []
            for team in self._teams.get(parent_session_id, {}).values():
                out.extend(team.teammates.values())
            return out

    # ------------------------------------------------------------------
    # Event ingestion (team tool_use / tool_result from the parent stream)
    # ------------------------------------------------------------------

    async def handle_team_tool_use(
        self,
        *,
        parent_session_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        working_dir: Optional[str],
    ) -> None:
        """Called when the parent stream emits a `tool_use` for a team tool."""
        if tool_name not in TEAM_TOOLS:
            return

        team_name = _extract_team_name(tool_input)
        if not team_name:
            # Not enough info yet; we'll catch metadata in the tool_result.
            return

        async with self._lock:
            team = self._ensure_team_locked(parent_session_id, team_name, working_dir)

            if tool_name == "Task":
                teammate_name = tool_input.get("teammate") or tool_input.get("to") or tool_input.get("subagent_type")
                prompt = tool_input.get("prompt") or tool_input.get("message") or ""
                if teammate_name:
                    m = team.teammates.get(teammate_name) or TeammateRecord(
                        team_name=team_name,
                        teammate_name=teammate_name,
                        working_dir=working_dir,
                    )
                    m.last_prompt = prompt or m.last_prompt
                    m.status = "running"
                    team.teammates[teammate_name] = m

            elif tool_name == "SendMessage":
                teammate_name = tool_input.get("to") or tool_input.get("teammate")
                if teammate_name:
                    m = team.teammates.get(teammate_name) or TeammateRecord(
                        team_name=team_name,
                        teammate_name=teammate_name,
                        working_dir=working_dir,
                    )
                    team.teammates[teammate_name] = m

            elif tool_name == "TeamDelete":
                # Mark all teammates as stopping. Watcher will finalize.
                for m in team.teammates.values():
                    if m.status == "running":
                        m.status = "killed"

    async def handle_team_tool_result(
        self,
        *,
        parent_session_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_output: Any,
        working_dir: Optional[str],
    ) -> List[TeammateRecord]:
        """Called on tool_result for a team tool.

        Returns the list of teammates that are newly discovered or updated
        so the caller can spin up transcript watchers.
        """
        if tool_name not in TEAM_TOOLS:
            return []

        team_name = _extract_team_name(tool_input) or _extract_team_name_from_output(tool_output)
        if not team_name:
            return []

        changed: List[TeammateRecord] = []
        async with self._lock:
            team = self._ensure_team_locked(parent_session_id, team_name, working_dir)

            # 1. Parse the tool output for teammate/session_id/pid info.
            output_records = _parse_output_for_teammates(tool_output)

            # 2. Filesystem fallback.
            fs_records = _scan_team_dir_for_teammates(team_name)

            # Merge, preferring explicit output records (more authoritative).
            merged: Dict[str, Dict[str, Any]] = {}
            for rec in fs_records + output_records:
                name = rec.get("teammate_name") or rec.get("name")
                if not name:
                    continue
                cur = merged.setdefault(name, {})
                for k, v in rec.items():
                    if v is not None:
                        cur[k] = v

            for name, rec in merged.items():
                existing = team.teammates.get(name)
                if existing is None:
                    existing = TeammateRecord(
                        team_name=team_name,
                        teammate_name=name,
                        working_dir=working_dir,
                    )
                    team.teammates[name] = existing

                sid = rec.get("teammate_session_id") or rec.get("session_id")
                if sid and not existing.teammate_session_id:
                    existing.teammate_session_id = sid

                pid = rec.get("pid")
                # bool is an int: a JSON ``true`` must not become pid 1.
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    existing.pid = pid

                status = rec.get("status")
                if status:
                    existing.status = status

                # Resolve JSONL path once we know the session id + cwd.
                if existing.teammate_session_id and existing.jsonl_path is None:
                    existing.jsonl_path = _locate_jsonl(
                        existing.teammate_session_id,
                        existing.working_dir or working_dir,
                    )

                changed.append(existing)

        # Emit update callbacks outside the lock to avoid deadlocks.
        for m in changed:
            await self._emit_update(parent_session_id, m.team_name, m)

        return changed

    # ------------------------------------------------------------------
    # Updates from transcript watcher
    # ------------------------------------------------------------------

    async def update_teammate(
        self,
        parent_session_id: str,
        team_name: str,
        teammate_name: str,
        **updates: Any,
    ) -> Optional[TeammateRecord]:
        async with self._lock:
            team = self._teams.get(parent_session_id, {}).get(team_name)
            if not team:
                return None
            m = team.teammates.get(teammate_name)
            if not m:
                return None
            for k, v in updates.items():
                if hasattr(m, k) and v is not None:
                    setattr(m, k, v)
            rec = m

        await self._emit_update(parent_session_id, team_name, rec)
        return rec

    # ------------------------------------------------------------------
    # Kill / cleanup
    # ------------------------------------------------------------------

    async def kill_teammate(
        self,
        parent_session_id: str,
        team_name: str,
        teammate_name: str,
    ) -> bool:
        async with self._lock:
            team = self._teams.get(parent_session_id, {}).get(team_name)
            if not team:
                return False
            m = team.teammates.get(teammate_name)
            if not m or not m.pid:
                return False
            pid = m.pid

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            logger.info(f"Teammate pid {pid} already exited")
        except PermissionError:
            logger.warning(f"Not permitted to kill teammate pid {pid}")
            return False
        except OSError as e:
            logger.warning(f"kill_teammate pid {pid} failed: {e}")
            return False

        await self.update_teammate(
            parent_session_id, team_name, teammate_name, status="killed"
        )
        return True

    async def remove_team(self, parent_session_id: str, team_name: str) -> None:
        async with self._lock:
            teams = self._teams.get(parent_session_id, {})
            teams.pop(team_name, None)
            if not teams:
                self._teams.pop(parent_session_id, None)

    async def remove_session(self, parent_session_id: str) -> List[TeamRecord]:
        async with self._lock:
            teams = self._teams.pop(parent_session_id, {})
            return list(teams.values())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_team_locked(
        self,
        parent_session_id: str,
        team_name: str,
        working_dir: Optional[str],
    ) -> TeamRecord:
        """Create-or-get a TeamRecord. Must be called with `_lock` held."""
        import time
        teams = self._teams.setdefault(parent_session_id, {})
        team = teams.get(team_name)
        if team is None:
            team = TeamRecord(
                parent_session_id=parent_session_id,
                team_name=team_name,
                working_dir=working_dir,
                created_at=time.time(),
            )
            teams[team_name] = team
        elif working_dir and not team.working_dir:
            team.working_dir = working_dir
        return team

    async def _emit_update(
        self,
        parent_session_id: str,
        team_name: str,
        rec: TeammateRecord,
    ) -> None:
        cb = self._on_teammate_update
        if cb is None:
            return
        try:
            await cb(parent_session_id, team_name, rec)
        except Exception:
            logger.exception("team_registry update callback failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_team_name(tool_input: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(tool_input, dict):
        return None
    for key in ("team_name", "team", "name"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_team_name_from_output(output: Any) -> Optional[str]:
    if isinstance(output, dict):
        return _extract_team_name(output)
    if isinstance(output, str):
        # Try to parse a JSON block inside a text result.
        try:
            data = json.loads(output)
        except (TypeError, ValueError):
            # Look for "Team 'foo' created"
            m = re.search(r"[Tt]eam\s+['\"]([^'\"]+)['\"]", output)
            return m.group(1) if m else None
        return _extract_team_name(data if isinstance(data, dict) else None)
    return None


def _parse_output_for_teammates(output: Any) -> List[Dict[str, Any]]:
    """Best-effort extraction of teammate metadata from a tool_result.

    Handles:
    - dict output with a `teammates` or `members` list
    - JSON-encoded string output with the same structure
    - Free-form text containing `name: ... session_id: UUID ... pid: N`
    """
    recs: List[Dict[str, Any]] = []

    def _harvest_dict(d: Dict[str, Any]) -> None:
        for key in ("teammates", "members", "agents"):
            items = d.get(key)
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        recs.append({
                            "teammate_name": it.get("name") or it.get("teammate"),
                            "teammate_session_id": (
                                it.get("session_id") or it.get("sessionId") or it.get("id")
                            ),
                            "pid": it.get("pid"),
                            "status": it.get("status"),
                        })
        # Single-member shape
        if "session_id" in d or "sessionId" in d:
            recs.append({
                "teammate_name": d.get("name") or d.get("teammate"),
                "teammate_session_id": d.get("session_id") or d.get("sessionId"),
                "pid": d.get("pid"),
                "status": d.get("status"),
            })

    if isinstance(output, dict):
        _harvest_dict(output)
    elif isinstance(output, str):
        try:
            data = json.loads(output)
            if isinstance(data, dict):
                _harvest_dict(data)
            elif isinstance(data, list):
                for it in data:
                    if isinstance(it, dict):
                        _harvest_dict(it)
        except (TypeError, ValueError):
            # Fall back to regex scraping.
            for sid in _UUID_RE.findall(output):
                recs.append({"teammate_session_id": sid, "teammate_name": None})

    # Drop records that have no useful info at all.
    return [r for r in recs if any(v for v in r.values())]


def _team_dir(team_name: str) -> Optional[Path]:
    """``~/.claude/teams/<team_name>``, or None when the name could leave it.

    The team name comes from model-authored tool input: it must never name a
    directory outside ~/.claude/teams (a pid read from there is later
    signalled by kill_teammate). Refused: separators, NUL, ``.``/``..``, a
    Windows drive prefix (``D:x`` is drive-relative, ``C:..`` climbs) and any
    name whose resolved directory is not directly inside the teams folder.
    """
    if (
        not team_name
        or team_name in (".", "..")
        or any(ch in team_name for ch in ("/", "\\", "\0", ":"))
        or PurePosixPath(team_name).name != team_name
        or PureWindowsPath(team_name).name != team_name
    ):
        return None
    root = Path.home() / ".claude" / "teams"
    base = root / team_name
    try:
        if base.resolve().parent != root.resolve():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return base


def _scan_team_dir_for_teammates(team_name: str) -> List[Dict[str, Any]]:
    """Fallback: read ~/.claude/teams/{team_name}/ for teammate JSON files."""
    base = _team_dir(team_name)
    if base is None or not base.is_dir():
        return []

    out: List[Dict[str, Any]] = []
    try:
        for entry in base.iterdir():
            if not entry.is_file() or entry.suffix.lower() not in (".json", ".yaml", ".yml"):
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                if "teammates" in data or "members" in data:
                    out.extend(_parse_output_for_teammates(data))
                else:
                    # The file itself describes a single teammate.
                    out.append({
                        "teammate_name": data.get("name") or entry.stem,
                        "teammate_session_id": (
                            data.get("session_id") or data.get("sessionId")
                        ),
                        "pid": data.get("pid"),
                        "status": data.get("status"),
                    })
    except OSError:
        logger.exception("Failed scanning %s", base)

    return [r for r in out if any(v for v in r.values())]


def _locate_jsonl(session_id: str, working_dir: Optional[str]) -> Optional[Path]:
    """Find a teammate's JSONL transcript.

    Tries the encoded-cwd directory first, then scans all Claude project
    directories for a matching `<session_id>.jsonl`.
    """
    if not session_id:
        return None

    candidates: List[Path] = []
    projects_dir = settings.get_claude_projects_dir

    if working_dir:
        try:
            candidates.append(projects_dir / get_project_dir_name(working_dir) / f"{session_id}.jsonl")
        except Exception:
            pass

    for p in candidates:
        if p.exists():
            return p

    if projects_dir.exists():
        try:
            for d in projects_dir.iterdir():
                if not d.is_dir():
                    continue
                p = d / f"{session_id}.jsonl"
                if p.exists():
                    return p
        except OSError:
            pass

    return None


# Singleton instance used across the app.
team_registry = TeamRegistry()
