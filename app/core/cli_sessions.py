"""Local Claude Code transcript operations owned by the app.

Reads/writes only JSONL transcripts below the configured projects directory.
It never imports a provider client, starts a model, or opens CLI credentials.
Metadata records are append-only, using the CLI's existing JSONL vocabulary.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, NotRequired


class SessionKey(TypedDict):
    project_key: str
    session_id: str
    subpath: NotRequired[str]


SessionListSubkeysKey = SessionKey
SessionStoreEntry = dict[str, Any]
SessionStoreListEntry = dict[str, Any]
SessionSummaryEntry = dict[str, Any]


@dataclass
class SessionInfo:
    session_id: str
    summary: str
    last_modified: int
    file_size: int | None = None
    custom_title: str | None = None
    first_prompt: str | None = None
    git_branch: str | None = None
    cwd: str | None = None
    tag: str | None = None
    created_at: int | None = None


@dataclass
class SessionMessage:
    type: str
    uuid: str
    session_id: str
    message: dict[str, Any]
    parent_tool_use_id: str | None = None


def project_key_for_directory(directory: str | None = None) -> str:
    """Match CLI path keys, including NFC and its long-path collision suffix."""
    path = unicodedata.normalize("NFC", str(Path(directory or Path.cwd()).resolve()))
    key = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(key) <= 200:
        return key
    # The transcript filename convention uses a signed 32-bit polynomial
    # string hash, encoded as the absolute value in base 36. This is a path
    # discriminator, not a cryptographic hash or an identity/auth decision.
    value = 0
    for character in path:
        value = (31 * value + ord(character)) & 0xFFFFFFFF
    value = abs(value if value < 0x80000000 else value - 0x100000000)
    suffix = ""
    while value:
        value, digit = divmod(value, 36)
        suffix = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + suffix
    return key[:200] + "-" + (suffix or "0")


def _projects() -> Path:
    from app.core.config import settings
    if settings.claude_projects_dir:
        return Path(settings.claude_projects_dir)
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"


def _safe(path: Path) -> bool:
    """No symlinks out of the transcript root, including intermediate parents."""
    root = _projects().resolve()
    return path.resolve().is_relative_to(root) and not path.is_symlink()


def _session_path(session_id: str, directory: str | None = None) -> Path | None:
    uuid.UUID(session_id)  # refuse traversal and non-session filenames
    root = _projects()
    if directory:
        path = root / project_key_for_directory(directory) / f"{session_id}.jsonl"
        if _safe(path) and path.is_file():
            return path
    for path in sorted(root.glob(f"*/{session_id}.jsonl")):
        if _safe(path) and path.is_file():
            return path
    return None


def _parse_entry(line: bytes) -> dict[str, Any] | None:
    try:
        entry = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None  # one damaged record must not hide the durable prefix
    return entry if isinstance(entry, dict) else None


def _entries(path: Path):
    # Decode inside the per-record guard. A text iterator can raise while
    # filling its buffer, before yielding even the valid preceding lines.
    with path.open("rb") as stream:
        for line in stream:
            entry = _parse_entry(line)
            if entry is not None:
                yield entry


_METADATA_WINDOW_BYTES = 64 * 1024


def _metadata_entries(path: Path):
    """Read at most two 64 KiB windows plus one boundary byte for discovery.

    The CLI writes initial prompt/cwd metadata at the head and current
    title/tag/summary metadata at the tail. Like the former bounded discovery
    path, records wholly in the middle (or larger than a window) aren't used
    for listing metadata. Full conversation reads still stream every record.
    """
    with path.open("rb") as stream:
        head = stream.read(_METADATA_WINDOW_BYTES)
        stream.seek(0, 2)
        size = stream.tell()
        if size <= 2 * _METADATA_WINDOW_BYTES:
            stream.seek(len(head))
            chunks = (head + stream.read(_METADATA_WINDOW_BYTES),)
        else:
            # Drop partial JSON/UTF-8 at both window boundaries. The extra
            # byte keeps a complete tail record when it begins on a newline.
            head = head[:head.rfind(b"\n") + 1]
            stream.seek(size - _METADATA_WINDOW_BYTES - 1)
            tail = stream.read(_METADATA_WINDOW_BYTES + 1).partition(b"\n")[2]
            chunks = (head, tail)
    for chunk in chunks:
        for line in chunk.split(b"\n"):
            entry = _parse_entry(line)
            if entry is not None:
                yield entry


def fold_session_summary(previous: SessionSummaryEntry | None, key: SessionKey,
                         entries: list[SessionStoreEntry]) -> SessionSummaryEntry:
    """Incrementally fold CLI metadata without changing the persisted sidecar shape."""
    data = dict((previous or {}).get("data") or {})
    for entry in entries:
        data.setdefault("is_sidechain", bool(entry.get("isSidechain", False)))
        timestamp = entry.get("timestamp")
        if timestamp and "created_at" not in data:
            try:
                data["created_at"] = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)
            except (ValueError, TypeError, AttributeError):
                pass
        if entry.get("cwd") and not data.get("cwd"):
            data["cwd"] = entry["cwd"]
        if entry.get("gitBranch"):
            data["git_branch"] = entry["gitBranch"]
        for kind, wire, field in (("summary", "summary", "summary_hint"),
                                  ("custom-title", "customTitle", "custom_title"),
                                  ("tag", "tag", "tag")):
            if entry.get("type") == kind and isinstance(entry.get(wire), str):
                data[field] = entry[wire]
        if entry.get("type") != "user" or entry.get("isMeta") or data.get("first_prompt_locked"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            content = next((b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"), "")
        if isinstance(content, str) and content.strip():
            command = re.search(r"<command-name>(.*?)</command-name>", content)
            if command:
                data.setdefault("command_fallback", command.group(1))
                continue
            prompt = " ".join(content.split())
            data["first_prompt"] = prompt[:200] + ("…" if len(prompt) > 200 else "")
            data["first_prompt_locked"] = True
    return {"session_id": key["session_id"], "mtime": (previous or {}).get("mtime", 0), "data": data}


def _info(path: Path) -> SessionInfo | None:
    stat = path.stat()
    key = {"project_key": path.parent.name, "session_id": path.stem}
    summary = None
    for entry in _metadata_entries(path):
        summary = fold_session_summary(summary, key, [entry])
    data = (summary or {}).get("data", {})
    if not data or data.get("is_sidechain"):
        return None
    return SessionInfo(session_id=path.stem,
                       summary=data.get("custom_title") or data.get("summary_hint") or data.get("first_prompt") or data.get("command_fallback") or "",
                       last_modified=int(stat.st_mtime * 1000), file_size=stat.st_size,
                       **{key: data.get(key) for key in ("custom_title", "first_prompt", "git_branch", "cwd", "tag", "created_at")})


def get_session_info(session_id: str, directory: str | None = None) -> SessionInfo | None:
    path = _session_path(session_id, directory)
    return _info(path) if path else None


def list_sessions(directory: str | None = None, limit: int | None = None,
                  offset: int = 0, include_worktrees: bool = True) -> list[SessionInfo]:
    root = _projects()
    directories = {directory} if directory else set()
    if directory and include_worktrees:
        try:
            result = subprocess.run(["git", "-C", directory, "worktree", "list", "--porcelain"],
                                    capture_output=True, text=True, timeout=5, check=True)
            directories.update(line[len("worktree "):] for line in result.stdout.splitlines()
                               if line.startswith("worktree "))
        except (OSError, subprocess.SubprocessError):
            pass  # a non-git project still has CLI sessions
    folders = [root / project_key_for_directory(d) for d in directories] if directory else root.glob("*")
    found = {}
    for folder in folders:
        for path in folder.glob("*.jsonl"):
            if not _safe(path):
                continue
            try:
                uuid.UUID(path.stem)
                info = _info(path)
            except (OSError, ValueError):
                continue  # concurrent delete or non-session JSONL
            if info:
                found[info.session_id] = info
    values = sorted(found.values(), key=lambda info: info.last_modified, reverse=True)
    return values[offset:None if limit is None or limit <= 0 else offset + limit]


def _messages(path: Path, session_id: str, limit: int | None, offset: int,
              *, include_sidechain: bool = False) -> list[SessionMessage]:
    records = [e for e in _entries(path) if include_sidechain or not e.get("isSidechain")]
    entries = [e for e in records if e.get("type") in ("user", "assistant")
               and isinstance(e.get("message"), dict)]
    # Rewound/forked transcripts can contain abandoned siblings. Walk through
    # system/progress parents too, but return only conversational messages.
    if entries and any(e.get("parentUuid") for e in entries):
        by_id = {e.get("uuid"): e for e in records if e.get("uuid")}
        chain, seen = [], set()
        entry = entries[-1]
        while entry and entry.get("uuid") not in seen:
            chain.append(entry)
            seen.add(entry.get("uuid"))
            entry = by_id.get(entry.get("parentUuid"))
        entries = list(reversed(chain))
    messages = [SessionMessage(type=e["type"], uuid=e.get("uuid", ""), session_id=e.get("sessionId", ""),
                               message=e["message"], parent_tool_use_id=e.get("parent_tool_use_id"))
                for e in entries if not e.get("isMeta")
                and e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict)]
    return messages[offset:None if limit is None or limit <= 0 else offset + limit]


def get_session_messages(session_id: str, directory: str | None = None,
                         limit: int | None = None, offset: int = 0) -> list[SessionMessage]:
    path = _session_path(session_id, directory)
    return _messages(path, session_id, limit, offset) if path else []


def _append_metadata(session_id: str, entry: dict, directory: str | None) -> None:
    path = _session_path(session_id, directory)
    if path is None:
        raise FileNotFoundError(f"Session not found: {session_id}")
    with path.open("rb") as stream:
        stream.seek(0, 2)
        if stream.tell():
            stream.seek(-1, 2)
            separator = "" if stream.read(1) == b"\n" else "\n"
        else:
            separator = ""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(separator + json.dumps({**entry, "sessionId": session_id},
                                           ensure_ascii=False, separators=(",", ":")) + "\n")


def rename_session(session_id: str, title: str, directory: str | None = None) -> None:
    if not title.strip():
        raise ValueError("Title must not be empty")
    _append_metadata(session_id, {"type": "custom-title", "customTitle": title.strip()}, directory)


def tag_session(session_id: str, tag: str | None, directory: str | None = None) -> None:
    _append_metadata(session_id, {"type": "tag", "tag": tag.strip() if tag else ""}, directory)


def list_subagents(session_id: str, directory: str | None = None) -> list[str]:
    path = _session_path(session_id, directory)
    if not path:
        return []
    return sorted(p.stem.removeprefix("agent-") for p in (path.with_suffix("") / "subagents").glob("agent-*.jsonl") if _safe(p))


def get_subagent_messages(session_id: str, agent_id: str, directory: str | None = None,
                          limit: int | None = None, offset: int = 0) -> list[SessionMessage]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise ValueError("Invalid agent id")
    path = _session_path(session_id, directory)
    if not path:
        return []
    sub = path.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    return _messages(sub, session_id, limit, offset, include_sidechain=True) if _safe(sub) and sub.is_file() else []


def delete_session(session_id: str, directory: str | None = None) -> None:
    path = _session_path(session_id, directory)
    if not path:
        raise FileNotFoundError(f"Session not found: {session_id}")
    artifacts = path.with_suffix("")
    if _safe(artifacts) and artifacts.is_dir():
        shutil.rmtree(artifacts)
    path.unlink()
