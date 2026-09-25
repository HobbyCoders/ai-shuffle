"""
Session export and import endpoints.

Extracted from sessions.py for maintainability. The sub-router is
included in the main sessions router for backward compatibility.
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query, status, Request, UploadFile, File

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin, require_auth, get_api_user_from_request
from app.core.project_utils import get_project_working_dir
from app.api.sessions_helpers import (
    api_user_may_use_profile,
    require_session,
    resolve_api_user_profile,
    SessionImportResponse,
    ImportJSONLRequest,
    ImportJSONLResponse,
)

import logging
logger = logging.getLogger(__name__)

# Sub-router — included by the main sessions router (no prefix needed)
export_router = APIRouter(tags=["Sessions"])


async def _resolve_import_profile_id(requested: Optional[str]) -> str:
    """The agent an imported session is filed under: requested, else the first.

    An import names an agent from wherever it was exported, so the id often
    matches nothing here. ``sessions.profile_id`` has a FK to ``agents``, and
    the app seeds no agents — so both "that agent is gone" and "this install
    has none yet" have to be answered before the INSERT rather than surfacing
    as a foreign-key 500.
    """
    if requested and await run_sync(database.get_profile, requested):
        return requested
    profiles = await run_sync(database.get_all_profiles)
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No agents are configured; create an agent before importing a session.",
        )
    return profiles[0]["id"]


async def _load_legacy_messages(session_id: str) -> list:
    db_messages = await run_sync(database.get_session_messages, session_id)
    messages = []
    for m in db_messages:
        msg = dict(m)
        if msg.get("tool_name"):
            msg["type"] = "tool_use"
            msg["toolName"] = msg.get("tool_name")
            msg["toolInput"] = msg.get("tool_input")
        elif msg.get("role") == "assistant":
            msg["type"] = "text"
        messages.append(msg)
    return messages


async def _load_export_messages(session: dict, working_dir: str) -> list:
    """Load the complete transcript through the same source chain as chat views."""
    session_id = session["id"]
    sdk_session_id = session.get("sdk_session_id")

    if sdk_session_id:
        try:
            from app.core.provider.fluid_transcript import is_fluid_session, fluid_live_messages

            if await run_sync(is_fluid_session, sdk_session_id):
                messages, _usage = await run_sync(
                    fluid_live_messages, sdk_session_id, working_dir
                )
            else:
                from app.api.sessions_helpers import parse_session_history_via_store

                messages, _usage = await run_sync(
                    parse_session_history_via_store, sdk_session_id, working_dir
                )
            if messages:
                return messages
        except Exception:
            logger.exception("Failed to load transcript for export session %s", session_id)

    return await _load_legacy_messages(session_id)


async def _load_jsonl_content(session: dict, working_dir: str) -> str:
    """Return the best authoritative transcript as newline-delimited JSON."""
    import json

    session_id = session["id"]
    sdk_session_id = session.get("sdk_session_id")
    entries = []

    if sdk_session_id:
        try:
            from app.core.provider.fluid_transcript import (
                is_fluid_session,
                fluid_transcript_entries,
            )

            if await run_sync(is_fluid_session, sdk_session_id):
                # The export is a read-only render, so it takes the code-mode
                # inner calls too — a bare run_python line would hide every
                # tool the cell actually ran.
                entries = await run_sync(
                    fluid_transcript_entries, sdk_session_id,
                    include_inner_calls=True,
                )
            else:
                from app.core.jsonl_parser import get_session_jsonl_path

                jsonl_path = await run_sync(
                    get_session_jsonl_path, sdk_session_id, working_dir
                )
                if jsonl_path:
                    try:
                        return jsonl_path.read_text(encoding="utf-8")
                    except Exception as exc:
                        logger.error(
                            "Failed to read JSONL file for session %s: %s",
                            session_id,
                            exc,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to read JSONL history file",
                        ) from exc

                from app.core.pg_session_store import _load_entries_by_session_id_sync

                entries = await run_sync(
                    _load_entries_by_session_id_sync, sdk_session_id
                )
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to load JSONL transcript for session %s", session_id)

    if entries:
        return "".join(json.dumps(entry, default=str) + "\n" for entry in entries)

    messages = await _load_export_messages(session, working_dir)
    if messages:
        return "".join(json.dumps(message, default=str) + "\n" for message in messages)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No chat history found for this session",
    )


@export_router.get("/{session_id}/export")
async def export_session(
    request: Request,
    session_id: str,
    format: str = Query("markdown", description="Export format: markdown, json or jsonl"),
    token: str = Depends(require_auth)
):
    """
    Export a session in the specified format.

    Args:
        session_id: The session ID to export
        format: Export format - 'markdown', 'json' or 'jsonl'

    Returns:
        - For markdown: Plain text Markdown representation
        - For JSON: Full session data including metadata and messages
        - For JSONL: The authoritative transcript as newline-delimited JSON;
          raw Claude history files are returned verbatim when available
    """
    from datetime import datetime
    from fastapi.responses import Response
    import json

    # Validate format
    if format not in ("markdown", "json", "jsonl"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid format. Must be 'markdown', 'json' or 'jsonl'"
        )

    session = await require_session(request, session_id)

    from app.core.message_resolver import resolve_working_dir

    working_dir = await run_sync(resolve_working_dir, dict(session))

    # Generate base filename
    title = session.get("title") or "untitled-session"
    # Sanitize title for filename
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in title)
    safe_title = safe_title.strip().replace(" ", "-")[:50]
    date_str = datetime.now().strftime("%Y%m%d")

    if format == "jsonl":
        content = await _load_jsonl_content(session, working_dir)
        filename = f"{safe_title}-{date_str}.jsonl"
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )

    messages = await _load_export_messages(session, working_dir)

    if format == "json":
        # JSON export - include full session data
        export_data = {
            "session": {
                "id": session.get("id"),
                "title": session.get("title"),
                "status": session.get("status"),
                "profile_id": session.get("profile_id"),
                "project_id": session.get("project_id"),
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "total_cost_usd": session.get("total_cost_usd"),
                "total_tokens_in": session.get("total_tokens_in"),
                "total_tokens_out": session.get("total_tokens_out"),
                "turn_count": session.get("turn_count"),
                # Model attribution: what it started on AND what it ended on.
                # A session that switched mid-flight exported only the latter.
                "first_model": session.get("first_model"),
                "launched_model": session.get("launched_model"),
            },
            "messages": messages,
            "exported_at": datetime.now().isoformat(),
            "format_version": "1.0"
        }

        filename = f"{safe_title}-{date_str}.json"
        content = json.dumps(export_data, indent=2, default=str)

        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )

    else:
        # Markdown export
        md_lines = []

        # Header
        md_lines.append(f"# {session.get('title') or 'Untitled Session'}")
        md_lines.append("")

        # Metadata
        md_lines.append("## Session Info")
        md_lines.append("")
        md_lines.append(f"- **Created:** {session.get('created_at')}")
        md_lines.append(f"- **Updated:** {session.get('updated_at')}")
        if session.get("total_cost_usd"):
            md_lines.append(f"- **Cost:** ${session.get('total_cost_usd'):.4f}")
        if session.get("turn_count"):
            md_lines.append(f"- **Turns:** {session.get('turn_count')}")
        md_lines.append("")

        # Messages
        md_lines.append("## Conversation")
        md_lines.append("")

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            msg_type = msg.get("type")
            timestamp = msg.get("metadata", {}).get("timestamp") if isinstance(msg.get("metadata"), dict) else None

            # Skip empty messages and certain system messages
            if not content and msg_type not in ("tool_use", "subagent"):
                continue

            if role == "user":
                md_lines.append("### Human")
                if timestamp:
                    md_lines.append(f"*{timestamp}*")
                md_lines.append("")
                md_lines.append(content)
                md_lines.append("")

            elif role == "assistant":
                if msg_type == "tool_use":
                    tool_name = msg.get("toolName") or msg.get("tool_name", "Unknown Tool")
                    tool_input = msg.get("toolInput") or msg.get("tool_input", {})
                    tool_result = msg.get("toolResult", "")

                    md_lines.append(f"### Tool: {tool_name}")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")

                    # Format tool input
                    if tool_input:
                        md_lines.append("**Input:**")
                        md_lines.append("```json")
                        md_lines.append(json.dumps(tool_input, indent=2, default=str))
                        md_lines.append("```")
                        md_lines.append("")

                    # Format tool result
                    if tool_result:
                        # Coerce to string (tool_result can be a dict from JSONL)
                        if not isinstance(tool_result, str):
                            tool_result = json.dumps(tool_result, indent=2, default=str)
                        md_lines.append("**Result:**")
                        # Check if result looks like code or file content
                        if "\n" in tool_result or len(tool_result) > 100:
                            md_lines.append("```")
                            md_lines.append(tool_result)
                            md_lines.append("```")
                        else:
                            md_lines.append(tool_result)
                        md_lines.append("")

                elif msg_type == "subagent":
                    agent_type = msg.get("agentType", "Agent")
                    agent_desc = msg.get("agentDescription", "")

                    md_lines.append(f"### Subagent: {agent_type}")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")
                    if agent_desc:
                        md_lines.append(f"**Task:** {agent_desc}")
                        md_lines.append("")
                    if content:
                        md_lines.append(content)
                        md_lines.append("")

                else:
                    # Regular text message
                    md_lines.append("### Assistant")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")
                    md_lines.append(content)
                    md_lines.append("")

            elif role == "system":
                # Include system messages as notes
                subtype = msg.get("subtype", "")
                if content:
                    md_lines.append("---")
                    md_lines.append(f"*System ({subtype}):* {content[:500]}")
                    md_lines.append("---")
                    md_lines.append("")

        # Footer
        md_lines.append("---")
        md_lines.append(f"*Exported from AI Shuffle on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        filename = f"{safe_title}-{date_str}.md"
        content = "\n".join(md_lines)

        return Response(
            content=content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )


_IMPORT_STRING_FIELDS = (
    "role", "type", "subtype", "toolName", "tool_name", "agentType", "agentDescription",
)
_IMPORT_ID_FIELDS = ("id", "toolId", "tool_id")


def _import_message_problem(msg: dict) -> Optional[str]:
    """Why the transcript writer cannot take ``msg``, or None when it can."""
    metadata = msg.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return "'metadata' must be an object"
    if metadata and metadata.get("timestamp") is not None and not isinstance(metadata["timestamp"], str):
        return "'metadata.timestamp' must be a string"
    for field in _IMPORT_STRING_FIELDS:
        value = msg.get(field)
        if value is not None and not isinstance(value, str):
            return f"'{field}' must be a string"
    for field in _IMPORT_ID_FIELDS:
        value = msg.get(field)
        # Legacy database rows export integer ids.
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
            return f"'{field}' must be a string"
    content = msg.get("content")
    if content is not None and not isinstance(content, (str, list)):
        return "'content' must be a string or an array"
    return None


@export_router.post("/import", response_model=SessionImportResponse)
async def import_session(
    request: Request,
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Import a session from a JSON file export.

    Accepts the AI Shuffle JSON export format with session metadata and messages.
    Creates a new session with a new ID to avoid conflicts.

    Args:
        file: JSON file containing exported session data

    Returns:
        Information about the imported session
    """
    import json
    import uuid
    from datetime import datetime, timezone
    from app.core.config import settings

    # Validate file type
    if not file.filename or not file.filename.endswith('.json'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Please upload a JSON file."
        )

    # Read and parse file content (with size limit to prevent memory exhaustion)
    max_size = 50 * 1024 * 1024  # 50MB limit
    try:
        # Check Content-Length header first (fast reject)
        content_length = file.size
        if content_length and content_length > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File too large. Maximum size is 50MB."
            )

        # Stream-read with size cap to avoid unbounded memory use
        chunks = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)  # 1MB chunks
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File too large. Maximum size is 50MB."
                )
            chunks.append(chunk)
        content = b"".join(chunks)

        import_data = json.loads(content.decode('utf-8'))
    except HTTPException:
        # The size caps above: keep their 413 instead of the generic 400 below.
        raise
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON file: {str(e)}"
        )
    except Exception:
        logger.exception("Failed to read session import file")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to read file"
        )

    # Validate import data structure
    if not isinstance(import_data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. The file must hold a JSON object."
        )
    if 'session' not in import_data or 'messages' not in import_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. Missing 'session' or 'messages' field."
        )

    session_data = import_data['session']
    messages_data = import_data['messages']

    if not isinstance(session_data, dict) or not isinstance(messages_data, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. 'session' must be an object and 'messages' must be an array."
        )
    if not all(isinstance(m, dict) for m in messages_data):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. Every message must be an object."
        )
    # The transcript writer below reads these per-message fields. A wrong type
    # there used to fail it after the session row was saved, leaving an empty
    # session behind a 200, so every message is checked before anything is.
    for index, msg in enumerate(messages_data):
        problem = _import_message_problem(msg)
        if problem:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid export format. Message {index + 1}: {problem}."
            )
    # These name the session and its agent and project. A non-string would fail
    # the lookups, or the response after the session row was already created.
    for field in ('title', 'profile_id', 'project_id'):
        value = session_data.get(field)
        if value is not None and not isinstance(value, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid export format. '{field}' must be a string."
            )
    # These feed numeric increments; a string would fail the UPDATE after the
    # session row was already created.
    for field in ('total_cost_usd', 'total_tokens_in', 'total_tokens_out', 'turn_count'):
        value = session_data.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid export format. '{field}' must be a number."
            )

    # Generate new session ID
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    new_sdk_session_id = str(uuid.uuid4())

    # The agent the export names, else this install's first one. The app seeds
    # no agents, so "none configured" is a real state and has to be refused
    # here — sessions.profile_id has a FK to agents.
    # Get API user if applicable. A key imports only into its own scope: a
    # named agent outside its assigned set is refused (403), and a
    # project-limited key files the session under its project — another
    # project named by the export is refused.
    api_user = get_api_user_from_request(request)
    api_user_id = api_user['id'] if api_user else None
    profile_id = await _resolve_import_profile_id(
        resolve_api_user_profile(api_user, session_data.get('profile_id'))
    )
    if not api_user_may_use_profile(api_user, profile_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This API key is not allowed to use agent '{profile_id}'.",
        )
    project_id = session_data.get('project_id') or None
    if api_user and api_user.get('project_id'):
        if project_id and project_id != api_user['project_id']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project",
            )
        project_id = api_user['project_id']
    elif project_id and not await run_sync(database.get_project, project_id):
        # An export from another install names a project this one lacks;
        # sessions.project_id has a FK to projects, so file it projectless.
        project_id = None

    # Create session in database
    title = session_data.get('title') or 'Imported Session'
    _now = datetime.now(timezone.utc).isoformat()  # Available for future metadata use

    # Create session - result used implicitly by subsequent operations
    await run_sync(
        database.create_session,
        session_id=new_session_id,
        profile_id=profile_id,
        project_id=project_id,
        title=title,
        api_user_id=api_user_id
    )

    # Update session with SDK session ID and stats
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id,
        cost_increment=session_data.get('total_cost_usd') or 0,
        tokens_in_increment=session_data.get('total_tokens_in') or 0,
        tokens_out_increment=session_data.get('total_tokens_out') or 0,
        turn_increment=session_data.get('turn_count') or len([m for m in messages_data if m.get('role') == 'user'])
    )

    # Create JSONL file for the session
    jsonl_path = None
    try:
        from app.core.jsonl_parser import get_project_dir_name

        # Get Claude projects directory
        claude_projects_dir = settings.get_claude_projects_dir

        # Determine project directory - use /workspace by default
        working_dir = "/workspace"
        if project_id:
            project = await run_sync(database.get_project, project_id)
            if project:
                working_dir = get_project_working_dir(project)

        # Convert working dir to Claude's project directory format
        project_dir_name = get_project_dir_name(working_dir)
        project_dir = claude_projects_dir / project_dir_name

        # Ensure project directory exists
        project_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = project_dir / f"{new_sdk_session_id}.jsonl"

        # Convert messages to JSONL format
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            for msg in messages_data:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                msg_type = msg.get('type')
                timestamp = (msg.get('metadata') or {}).get('timestamp') or datetime.now(timezone.utc).isoformat()
                msg_uuid = msg.get('id') or str(uuid.uuid4())

                if role == 'user' and msg_type != 'tool_result':
                    # User message
                    entry = {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": content
                        },
                        "uuid": msg_uuid,
                        "timestamp": timestamp
                    }
                    f.write(json.dumps(entry) + '\n')

                elif role == 'assistant':
                    if msg_type == 'tool_use':
                        # Tool use message
                        tool_name = msg.get('toolName') or msg.get('tool_name')
                        tool_input = msg.get('toolInput') or msg.get('tool_input') or {}
                        tool_id = msg.get('toolId') or msg.get('tool_id') or str(uuid.uuid4())

                        content_blocks = []
                        if content:
                            content_blocks.append({"type": "text", "text": content})
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input
                        })

                        entry = {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks
                            },
                            "uuid": msg_uuid,
                            "timestamp": timestamp
                        }
                        f.write(json.dumps(entry) + '\n')

                        # Write tool result if present
                        tool_result = msg.get('toolResult')
                        if tool_result:
                            result_entry = {
                                "type": "user",
                                "message": {
                                    "role": "user",
                                    "content": [{
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": tool_result
                                    }]
                                },
                                "uuid": str(uuid.uuid4()),
                                "timestamp": timestamp
                            }
                            f.write(json.dumps(result_entry) + '\n')

                    elif msg_type == 'subagent':
                        # Subagent message - write as tool use
                        tool_id = msg.get('toolId') or str(uuid.uuid4())
                        agent_type = msg.get('agentType') or msg.get('agentDescription') or 'Agent'
                        description = msg.get('agentDescription', '')

                        content_blocks = [{
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Agent",
                            "input": {
                                "subagent_type": agent_type,
                                "description": description
                            }
                        }]

                        entry = {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks
                            },
                            "uuid": msg_uuid,
                            "timestamp": timestamp
                        }
                        f.write(json.dumps(entry) + '\n')

                    else:
                        # Text message
                        if content:
                            entry = {
                                "type": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": content}]
                                },
                                "uuid": msg_uuid,
                                "timestamp": timestamp
                            }
                            f.write(json.dumps(entry) + '\n')

                elif role == 'system':
                    # System message
                    subtype = msg.get('subtype', '')
                    entry = {
                        "type": "system",
                        "subtype": subtype,
                        "content": content,
                        "uuid": msg_uuid,
                        "timestamp": timestamp
                    }
                    f.write(json.dumps(entry) + '\n')

        logger.debug(f"Created JSONL file for imported session: {jsonl_path}")

    except Exception:
        # A session without its transcript is not an import: remove both, so a
        # failure here never leaves an empty session behind a "success".
        logger.exception("Failed to create JSONL file for imported session")
        if jsonl_path is not None:
            try:
                jsonl_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove the partial transcript %s", jsonl_path)
        try:
            await run_sync(database.delete_session, new_session_id)
        except Exception:
            logger.exception("Could not remove imported session %s", new_session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not write the imported transcript; nothing was imported.",
        )

    logger.debug(f"Imported session {new_session_id} with {len(messages_data)} messages")

    return SessionImportResponse(
        session_id=new_session_id,
        title=title,
        message_count=len(messages_data),
        status="success"
    )


def _confined_jsonl_path(raw_path: str) -> Path:
    """Resolve ``raw_path`` and require it inside a transcript root.

    Roots are the Claude projects directory (where CLI transcripts live) and
    the workspace. Resolution follows symlinks and ``..``, so traversal or a
    link pointing outside a root is rejected with 400 before any existence
    check (no probing of arbitrary server paths).
    """
    from app.core.config import settings

    resolved = Path(raw_path).expanduser().resolve()
    roots = (settings.get_claude_projects_dir, settings.effective_workspace_dir)
    if not any(resolved.is_relative_to(Path(root).resolve()) for root in roots):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="jsonl_path must be inside the Claude projects directory or the workspace",
        )
    return resolved


@export_router.post("/import-jsonl", response_model=ImportJSONLResponse)
async def import_jsonl_session(
    request: Request,
    body: ImportJSONLRequest,
    token: str = Depends(require_admin)
):
    """
    Import an existing Claude Code JSONL session into AI Shuffle - Owner only.

    This allows resuming sessions that were created directly in Claude Code
    without going through AI Shuffle. The session will be registered in the
    AI Shuffle database, allowing it to be resumed and managed like any other session.

    Args:
        body: Request containing:
            - jsonl_path: Full path to the JSONL file, inside the Claude
              projects directory or the workspace (400 otherwise)
            - profile_id: Optional profile to associate (defaults to 'default')
            - project_id: Optional project to associate

    Returns:
        Information about the imported session
    """
    import uuid
    from app.core.jsonl_parser import (
        get_session_cost_from_jsonl,
        _get_session_title_and_preview
    )

    jsonl_path = _confined_jsonl_path(body.jsonl_path)

    # Validate the file exists and is a JSONL file
    if not jsonl_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"JSONL file not found: {body.jsonl_path}"
        )

    if not jsonl_path.suffix == '.jsonl':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be a JSONL file"
        )

    # Skip agent files
    if jsonl_path.name.startswith("agent-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot import agent files (subagent logs)"
        )

    # Get SDK session ID from filename
    sdk_session_id = jsonl_path.stem

    # Check if this session is already imported
    existing_session = await run_sync(database.get_session_by_sdk_id, sdk_session_id)
    if existing_session:
        return ImportJSONLResponse(
            session_id=existing_session["id"],
            sdk_session_id=sdk_session_id,
            title=existing_session.get("title"),
            message_count=existing_session.get("turn_count", 0),
            status="already_exists"
        )

    # Get title and preview from JSONL
    title, preview, message_count = _get_session_title_and_preview(jsonl_path)
    if not title:
        title = f"Imported: {sdk_session_id[:8]}..."

    # Same resolution as the full-export import: an id that names no agent
    # cannot be handed to create_session, and an install may have none.
    profile_id = await _resolve_import_profile_id(body.profile_id)

    # Validate project if specified
    project_id = body.project_id
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

    # Get working dir from project or from the JSONL path
    working_dir = str(jsonl_path.parent.parent)  # Go up from session file to project dir

    # Parse JSONL to get token usage
    try:
        usage_data = get_session_cost_from_jsonl(sdk_session_id, working_dir)
        total_tokens_in = usage_data.get("total_tokens_in", 0) if usage_data else 0
        total_tokens_out = usage_data.get("total_tokens_out", 0) if usage_data else 0
        total_cost = usage_data.get("total_cost", 0.0) if usage_data else 0.0
    except Exception as e:
        logger.warning(f"Could not get token usage from JSONL: {e}")
        total_tokens_in = 0
        total_tokens_out = 0
        total_cost = 0.0

    # Create new session in database
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"

    await run_sync(
        database.create_session,
        session_id=new_session_id,
        profile_id=profile_id,
        project_id=project_id,
        title=title,
        api_user_id=None  # Local import
    )

    # Update session with SDK session ID and stats
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=sdk_session_id,
        cost_increment=total_cost,
        tokens_in_increment=total_tokens_in,
        tokens_out_increment=total_tokens_out,
        turn_increment=message_count
    )

    logger.debug(f"Imported JSONL session {new_session_id} (SDK: {sdk_session_id}) with {message_count} messages")

    return ImportJSONLResponse(
        session_id=new_session_id,
        sdk_session_id=sdk_session_id,
        title=title,
        message_count=message_count,
        status="success"
    )
