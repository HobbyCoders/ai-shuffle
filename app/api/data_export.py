"""Export everything: one ZIP of every chat, agent and project (#2158).

The ZIP is assembled while it downloads, one chat at a time, so a long history
never sits in memory and the connection keeps moving through a remote tunnel.
Each chat goes in twice, in the formats the single-chat export already
produces: the private ``.jsonl`` backup and the Markdown rendering. Agents use
the ``ai-shuffle-agent`` file their own export writes, so each one imports
back through Agents > Import.

Only those renderings and the project listing go in. Nothing is read from
the credential store, provider records, settings, sign-in files, the install
key or ``.env``. Agent environment variables are dropped, since they commonly
hold API keys. Chats stay raw private backups, like the single-chat download:
anything typed or printed in a chat is in it.
"""

import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.auth import require_admin
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/data-export", tags=["Data export"])

FORMAT_VERSION = "1.0"

README = """AI Shuffle export
=================

Everything this install holds about your work, as of {exported_at}.

chats/<date>-<title>-<id>/chat-private.jsonl
    The chat's full transcript, exactly as saved. The same file as
    Download > Private backup (.jsonl) on a single chat.
chats/<date>-<title>-<id>/chat-private.md
    The same chat as readable Markdown.
agents/<agent>-agent.json
    Each agent's settings. Import one with Agents > Import.
projects.json
    Your projects: name, description, folder and settings.
manifest.json
    Every chat's title, agent, project, dates, cost and file names, and
    any chat that could not be exported, with the reason.

Private: these chats are raw backups. Anything typed into a chat or printed
by a tool is in them, including passwords or keys that appeared there.
Keep this file safe and do not share it.

Never included: AI provider sign-ins and API keys, tokens, saved
credentials, app settings, the install key, .env files, and agents'
environment variables. Project files stay in their folders; back up the
install to keep them.
"""


class _ChunkSink:
    """A write-only, unseekable file for ``zipfile`` to stream into.

    Without ``tell``/``seek``, ``zipfile`` writes data descriptors after each
    member instead of seeking back, so every byte can be sent as written.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _member_stem(session: dict, used: set[str]) -> str:
    """A unique, portable folder name for one chat inside the ZIP."""
    from app.api.sessions_export import _safe_filename_title

    created = session.get("created_at")
    date = created.strftime("%Y%m%d") if hasattr(created, "strftime") else str(created or "")[:10].replace("-", "")
    title = _safe_filename_title(session.get("title") or "") or "untitled"
    stem = "-".join(part for part in (date, title, str(session.get("id") or "")[:8].strip("-")) if part)
    return _unique(stem, used)


def _unique(stem: str, used: set[str]) -> str:
    unique, n = stem, 2
    while unique in used:
        unique, n = f"{stem}-{n}", n + 1
    used.add(unique)
    return unique


def _agent_stem(profile_id: str, used: set[str]) -> str:
    """A portable file stem for an agent; ids from imports are not validated."""
    import re

    return _unique(re.sub(r"[^A-Za-z0-9_-]", "", str(profile_id))[:60] or "agent", used)


def _load_sessions() -> list:
    """Every chat on this install, hidden and delegated ones included.

    One read rather than pages: the list is ordered by ``updated_at`` and a
    chat that runs during the export would move between pages and be missed.
    """
    return database.get_sessions(limit=10_000_000, include_hidden=True)


def _load_projects() -> list:
    """The projects in the shape ``GET /api/v1/projects`` returns."""
    from app.api.projects import _load_projects_for_listing
    from app.core.models import Project
    from app.core.project_utils import get_project_working_dir, is_absolute_project_path

    projects, session_counts = _load_projects_for_listing(None)
    out = []
    for project in projects:
        project = dict(project)
        project["session_count"] = session_counts.get(project["id"], 0)
        if not is_absolute_project_path(project["path"]):
            project["path"] = get_project_working_dir(project)
        out.append(Project.model_validate(project).model_dump(mode="json"))
    return out


def _load_agents() -> list:
    """Each agent's export file, without its environment variables."""
    from app.api.agents import build_agent_export

    exports = []
    for profile in database.get_all_profiles():
        export = build_agent_export(profile)
        # Agent env commonly carries API keys; an export-all never holds one.
        export.agent.env = None
        exports.append((profile["id"], export.model_dump(mode="json")))
    return exports


async def _chat_files(session: dict) -> tuple[Optional[str], str, list[str]]:
    """The private JSONL and the Markdown of one chat, plus any problems."""
    from app.api.sessions_export import (
        _load_export_messages, _load_jsonl_content, _render_markdown,
    )
    from app.core.message_resolver import resolve_working_dir

    problems: list[str] = []
    working_dir = await run_sync(resolve_working_dir, dict(session))
    jsonl: Optional[str] = None
    try:
        jsonl = await _load_jsonl_content(session, working_dir)
    except HTTPException as exc:
        problems.append(str(exc.detail))
    except Exception:
        logger.exception("Export all: JSONL failed for session %s", session.get("id"))
        problems.append("The transcript could not be read")
    try:
        messages = await _load_export_messages(session, working_dir)
    except Exception:
        logger.exception("Export all: messages failed for session %s", session.get("id"))
        messages = []
        problems.append("The messages could not be read")
    return jsonl, _render_markdown(session, messages), problems


async def _export_zip(root: str, exported_at: str) -> AsyncIterator[bytes]:
    import zipfile

    from app import __version__
    from app.api.sessions_export import _session_export_metadata

    sink = _ChunkSink()
    zf = zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_DEFLATED)

    async def put(name: str, text: str) -> bytes:
        # Compressing a large transcript is CPU work; keep it off the loop.
        await run_sync(zf.writestr, f"{root}/{name}", text.encode("utf-8"))
        return sink.drain()

    try:
        yield await put("README.txt", README.format(exported_at=exported_at))

        sessions = await run_sync(_load_sessions)
        used: set[str] = set()
        chats = []
        for session in sessions:
            stem = _member_stem(session, used)
            entry = _session_export_metadata(session)
            entry["hidden"] = bool(session.get("hidden"))
            entry["is_worker"] = bool(session.get("is_worker"))
            entry["parent_session_id"] = session.get("parent_session_id")
            entry["files"] = {}
            try:
                jsonl, markdown, problems = await _chat_files(session)
            except Exception:
                logger.exception("Export all: session %s failed", session.get("id"))
                jsonl, markdown, problems = None, None, ["The chat could not be exported"]
            if jsonl is not None:
                entry["files"]["jsonl"] = f"chats/{stem}/chat-private.jsonl"
                yield await put(entry["files"]["jsonl"], jsonl)
            if markdown is not None:
                entry["files"]["markdown"] = f"chats/{stem}/chat-private.md"
                yield await put(entry["files"]["markdown"], markdown)
            if problems:
                entry["problems"] = problems
            chats.append(entry)

        agents = await run_sync(_load_agents)
        agent_files = []
        agent_stems: set[str] = set()
        for profile_id, export in agents:
            name = f"agents/{_agent_stem(profile_id, agent_stems)}-agent.json"
            agent_files.append({"id": profile_id, "name": export["agent"]["name"], "file": name})
            yield await put(name, json.dumps(export, indent=2, default=str))

        projects = await run_sync(_load_projects)
        yield await put("projects.json", json.dumps(projects, indent=2, default=str))

        manifest = {
            "type": "ai-shuffle-export",
            "format_version": FORMAT_VERSION,
            "exported_at": exported_at,
            "app_version": __version__,
            "counts": {"chats": len(chats), "agents": len(agent_files), "projects": len(projects)},
            "chats": chats,
            "agents": agent_files,
        }
        yield await put("manifest.json", json.dumps(manifest, indent=2, default=str))
        zf.close()
        yield sink.drain()
    finally:
        if zf.fp is not None:
            zf.close()


@router.get("/all")
async def export_all(token: str = Depends(require_admin)):
    """Stream a ZIP of every chat (private JSONL and Markdown), agent and project.

    Owner only. Never includes provider sign-ins, API keys, tokens, settings,
    the install key, ``.env`` content or agent environment variables. Chats are
    private raw backups, as in the single-chat export.
    """
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    root = f"ai-shuffle-export-{stamp}"
    exported_at = now.isoformat().replace("+00:00", "Z")
    return StreamingResponse(
        _export_zip(root, exported_at),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{root}-private.zip"',
            "X-Transcript-Privacy": "private-raw",
            "Cache-Control": "no-store",
        },
    )
