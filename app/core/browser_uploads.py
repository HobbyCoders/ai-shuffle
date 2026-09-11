"""Bounded, authenticated device uploads into the session-owned browser."""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core import browser_control
from app.core.config import settings

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
MAX_BODY = MAX_BYTES + 65536
_inflight: set[tuple[str, str]] = set()


def upload_root() -> Path:
    return (settings.effective_workspace_dir / ".browser-uploads").resolve()


async def upload(request: Request, session_id: str, viewer_id: str,
                 runtime_id: str, tab_id: str, field: str) -> dict:
    broker = browser_control.get(session_id)
    identity = {"runtime_id": runtime_id, "tab_id": tab_id}

    def authorize():
        for value in (viewer_id, runtime_id, tab_id, field):
            browser_control.identifier(value)
        broker.identity(identity)
        control = broker.state["control"]
        if viewer_id not in broker.viewers or control["owner"] != "human" or control["viewer_id"] != viewer_id:
            raise browser_control.ControlError("This viewer does not control the browser")
        if not any(f["id"] == field and f["type"] == "file" for f in broker.state.get("editors", [])):
            raise browser_control.ControlError("File field changed. Choose the files again.")

    try:
        authorize()
    except browser_control.ControlError as exc:
        raise HTTPException(409, str(exc)) from None
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(400, "Expected multipart upload files")
    key = (session_id, viewer_id)
    if key in _inflight:
        raise HTTPException(409, "An upload is already in progress")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        raise HTTPException(413, "Choose up to 10 files, 10 MB total")
    _inflight.add(key)
    too_large = False

    async def bounded_body():
        nonlocal too_large
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                too_large = True
                # MultiPartParser closes already-spooled files on this exception.
                raise MultiPartException("Upload too large")
            yield chunk

    try:
        try:
            form = await MultiPartParser(request.headers, bounded_body(), max_files=MAX_FILES, max_fields=0).parse()
        except MultiPartException:
            raise HTTPException(413 if too_large else 400, "Choose up to 10 files, 10 MB total") from None
        try:
            files = [value for name, value in form.multi_items() if name == "files" and isinstance(value, UploadFile)]
            if not files or len(files) != len(form.multi_items()):
                raise HTTPException(400, "No upload files provided")
            if sum(f.size or 0 for f in files) > MAX_BYTES:
                raise HTTPException(413, "Choose up to 10 files, 10 MB total")
            authorize()
            base = upload_root()
            await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="picker-", dir=base) as folder:
                paths = []
                for index, file in enumerate(files):
                    name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                    if not name or name in (".", "..") or len(name) > 255 or any(ord(c) < 32 for c in name):
                        raise HTTPException(400, "Invalid filename")
                    path = Path(folder) / str(index) / name
                    await asyncio.to_thread(path.parent.mkdir)
                    content = await file.read(MAX_BYTES + 1)
                    if len(content) > MAX_BYTES:
                        raise HTTPException(413, "Choose up to 10 files, 10 MB total")
                    await asyncio.to_thread(path.write_bytes, content)
                    paths.append(str(path))
                authorize()
                result = await broker.submit_and_wait(viewer_id, {
                    "id": f"upload-{uuid.uuid4()}", **identity, "op": "upload",
                    "args": {"field": field, "paths": paths},
                })
                if not result["ok"]:
                    raise HTTPException(409, result.get("error", "Upload failed"))
                return {"ok": True}
        finally:
            await form.close()
    except browser_control.ControlError as exc:
        raise HTTPException(409, str(exc)) from None
    except asyncio.TimeoutError:
        raise HTTPException(504, "Upload outcome unknown. Check the page before retrying.") from None
    finally:
        _inflight.discard(key)
