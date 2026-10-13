"""Session-scoped upload staging for worker sessions without a local checkout."""
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.db.async_database import run_sync

router = APIRouter()


@router.post("/{session_id}/upload")
async def upload_session_file(
    request: Request, session_id: str, file: UploadFile = File(...),
    token: str = Depends(require_auth),
):
    # Access is checked before reading bytes or creating directories. Never
    # accept a caller-supplied root/path or use another session's staging area.
    session = await require_session(request, session_id)
    if not session.get("project_v2_id") or session.get("mode") == "chat":
        raise HTTPException(400, "Attachments need a code or voice project session.")
    from app.api.projects import MAX_UPLOAD_SIZE, _read_upload_within_limit
    from app.core.config import settings
    from app.core.confined_files import upload
    from app.core.upload_transfer import session_upload_dir

    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "File too large.")
    filename = Path(file.filename or "upload").name
    if not filename or filename.startswith("."):
        raise HTTPException(400, "Invalid filename")
    content = await _read_upload_within_limit(file)
    workspace = settings.effective_workspace_dir
    stage = session_upload_dir(session_id).relative_to(workspace).as_posix()
    try:
        destination = await run_sync(upload, workspace, stage, filename, content)
    except ValueError:
        raise HTTPException(400, "Invalid upload path")
    return {
        "filename": destination.name,
        "path": destination.relative_to(workspace).as_posix(),
        "full_path": destination.as_posix(),
        "size": len(content),
    }
