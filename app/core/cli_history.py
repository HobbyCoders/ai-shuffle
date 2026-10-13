"""Discover Claude Code CLI conversations on this worker that AI Shuffle has not seen.

The Claude Code CLI writes every conversation to
``<claude projects dir>/<encoded cwd>/<session id>.jsonl``. Conversations
started from AI Shuffle are registered by their SDK session id; anything else
there was started from the CLI directly (a terminal on the worker, or the
user's own ``claude`` on a desktop install). Onboarding offers those for
import.

Only transcripts are read: never credentials, settings or any other file in
the Claude config directory. Importing registers the transcript in place
(``POST /sessions/import-jsonl``); nothing is copied.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# How far into a transcript to look for the conversation's working directory.
_CWD_SCAN_LINES = 40


def _transcripts(projects_dir: Path) -> Iterable[Path]:
    """Top-level conversation files, skipping subagent logs and hook sidecars."""
    try:
        project_dirs = [d for d in projects_dir.iterdir() if d.is_dir()]
    except OSError:
        return []
    found: List[Path] = []
    for project_dir in project_dirs:
        try:
            for path in project_dir.glob("*.jsonl"):
                if path.name.startswith("agent-") or path.name.endswith(".hooks.jsonl"):
                    continue
                found.append(path)
        except OSError:
            continue
    return found


def transcript_cwd(path: Path) -> Optional[str]:
    """The working directory the CLI recorded for this conversation, if any."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= _CWD_SCAN_LINES:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                cwd = entry.get("cwd") if isinstance(entry, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.realpath(path)).rstrip("/\\")


def find_cli_conversations(
    projects_dir: Path,
    *,
    is_registered: Callable[[str], bool],
    summarize: Callable[[Path], tuple],
    project_dirs: Dict[str, str],
    days: int = 30,
    limit: int = 50,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Recent CLI conversations not yet in AI Shuffle, newest first.

    Args:
        projects_dir: The Claude projects directory.
        is_registered: Whether an SDK session id already belongs to a session.
        summarize: ``path -> (title, preview, message_count)``.
        project_dirs: ``{project working dir: project id}`` for matching a
            conversation to the project it ran in.
        days: Only conversations touched in this many days.
        limit: At most this many results.
    """
    cutoff = (now if now is not None else time.time()) - days * 86_400
    candidates = []
    for path in _transcripts(projects_dir):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)

    by_dir = {_normalize(d): pid for d, pid in project_dirs.items() if d}
    results: List[Dict[str, Any]] = []
    for mtime, path in candidates:
        if len(results) >= limit:
            break
        sdk_session_id = path.stem
        if is_registered(sdk_session_id):
            continue
        try:
            title, _preview, message_count = summarize(path)
        except Exception:  # noqa: BLE001 — one unreadable transcript is skipped
            logger.debug("CLI history: could not read %s", path, exc_info=True)
            continue
        if not title or not message_count:
            continue
        cwd = transcript_cwd(path)
        results.append({
            "jsonl_path": str(path),
            "sdk_session_id": sdk_session_id,
            "title": title,
            "message_count": message_count,
            "modified_at": mtime,
            "cwd": cwd,
            "project_id": by_dir.get(_normalize(cwd)) if cwd else None,
        })
    return results
