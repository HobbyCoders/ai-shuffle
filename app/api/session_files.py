"""@-mention lists for a chat that runs on another worker.

The list has to come from the worker running the chat: those are the paths its
agent can open, and its checkout can differ from this worker's copy (branch,
worktree, untracked files). The project routes forward here over the worker
control channel; the running worker checks it owns the session and resolves
the session's own directory. A peer never supplies a directory, only a path
that is checked against that one.
"""
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.api.common import format_file_size, get_file_extension, is_path_safe
from app.db.async_database import run_sync
from app.network import control, transport

#: A person is waiting on a menu: fail fast on an address that never answers.
CONNECT_SECONDS = 3.0
FORWARD_SECONDS = 10.0

WORKER_UNREACHABLE = "The worker running this chat is offline or unreachable."
WORKER_OUTDATED = "The worker running this chat needs an update before it can list its files here."
NO_CHECKOUT = "This chat's folder is not available on the worker running it."
_UNKNOWN_ACTION = "Unknown control action"
_MAX_HISTORY = 100


def list_session_directory(base_path: Path, path: str, show_hidden: bool) -> List[Dict[str, Any]]:
    """The entries of ``path`` inside a session's directory, folders first."""
    full_path = base_path / path if path else base_path
    if not is_path_safe(full_path, base_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes session checkout"
        )

    if not full_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Directory not found"
        )
    if not full_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory"
        )

    files = []
    for item in full_path.iterdir():
        if item.name.startswith('.') and not show_hidden:
            continue
        try:
            stat_info = item.stat()
            is_dir = item.is_dir()
            files.append({
                "name": item.name,
                "type": "directory" if is_dir else "file",
                "size": stat_info.st_size if not is_dir else None,
                "sizeFormatted": format_file_size(stat_info.st_size) if not is_dir else None,
                "path": str(item.relative_to(base_path)),
                "extension": get_file_extension(item.name) if not is_dir else None,
                "modifiedAt": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                "createdAt": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            })
        except (OSError, PermissionError):
            continue
    files.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
    return files


async def forward(owner: str, session_id: str, action: str, params: Dict[str, Any]) -> Any:
    """Ask the worker running the chat; its refusals reach the menu as written."""
    capped = transport.connect_timeout.set(CONNECT_SECONDS)
    try:
        result = await control.call(owner, "session-files", {
            "action": action, "session_id": session_id, "params": params,
        }, timeout=FORWARD_SECONDS)
    except control.RunnerUnreachable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, WORKER_UNREACHABLE) from exc
    except control.ControlRefused as exc:
        detail = WORKER_OUTDATED if _UNKNOWN_ACTION in str(exc) else str(exc)
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    finally:
        transport.connect_timeout.reset(capped)
    if result["status"] >= 400:
        raise HTTPException(result["status"], result["detail"])
    return result["result"]


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("Invalid session file parameter")
    return value


def _run(session_id: str, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    from app.core.jsonl_parser import list_chat_history_sessions
    from app.core.project_resolver import resolve_existing_cwd

    # LookupError / PermissionError: /peer/control answers 404 / 409.
    session = control._require_owned(session_id)
    cwd: Optional[str] = resolve_existing_cwd(session)
    if not cwd:
        return {"status": status.HTTP_409_CONFLICT, "detail": NO_CHECKOUT}
    if action == "list":
        path = _text(params.get("path"))
        files = list_session_directory(Path(cwd), path, params.get("show_hidden") is True)
        return {"status": 200, "result": {"files": files, "path": path}}
    limit = params.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_HISTORY:
        raise ValueError("Invalid session file parameter")
    sessions = list_chat_history_sessions(working_dir=cwd, search=_text(params.get("search")), limit=limit)
    return {"status": 200, "result": {"sessions": sessions}}


async def handle_session_files(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """Owner side of ``session-files``: read-only, for a session this worker runs."""
    action = body.get("action")
    if action not in ("list", "chat-history"):
        raise ValueError("Unsupported session file action")
    params = body.get("params")
    if not isinstance(params, dict):
        raise ValueError("Invalid session file parameters")
    try:
        return await run_sync(_run, str(body.get("session_id") or ""), action, params)
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}
