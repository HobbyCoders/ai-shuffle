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
    from app.core.upload_transfer import resolve_upload_target, stage_admitted_session_upload
    from app.core.query_options import WorktreeUnavailableError

    try:
        await run_sync(resolve_upload_target, session)
    except WorktreeUnavailableError as exc:
        raise HTTPException(409, {"message": str(exc), "reason": "worktree_unavailable"}) from exc

    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "File too large.")
    filename = Path(file.filename or "upload").name
    if not filename or filename.startswith("."):
        raise HTTPException(400, "Invalid filename")
    content = await _read_upload_within_limit(file)
    try:
        return await run_sync(stage_admitted_session_upload, session, filename, content)
    except WorktreeUnavailableError as exc:
        raise HTTPException(409, {"message": str(exc), "reason": "worktree_unavailable"}) from exc
    except ValueError:
        raise HTTPException(400, "Invalid upload path")
