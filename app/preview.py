"""Opt-in branch UI on the existing authenticated listener, never a second worker.

After building this checkout's frontend: ``python -m app.preview stamp``.
Start the normal application with AI_SHUFFLE_PREVIEW=1 only on an isolated
instance or after stopping that instance's existing worker. Configuration and
credentials remain inherited; this module neither starts services nor proxies.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fluid.tools.child_env import scrub_child_env

MANIFEST = ".shuffle-preview.json"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], timeout=10, text=True,
        env=scrub_child_env(),
    ).strip()


def _source_identity(root: Path) -> dict:
    files = set(_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "frontend").split("\0"))
    digest = hashlib.sha256()
    for name in sorted(files - {""}):
        path = root / name
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"<deleted>")
    return {
        "branch": _git(root, "branch", "--show-current"),
        "commit": _git(root, "rev-parse", "HEAD"),
        "worktree_id": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
        "source_sha256": digest.hexdigest(),
    }


def _build_digest(directory: Path) -> str:
    if not (directory / "index.html").is_file():
        raise ValueError("Build frontend/build/index.html before stamping the preview")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Preview build must not link to files outside its directory")
        if path.is_file() and path.name != MANIFEST:
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _build_dir(root: Path) -> Path:
    directory = root / "frontend" / "build"
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("Preview requires this worktree's own frontend/build, not a shared build")
    return directory


def stamp_build(root: Path | None = None) -> dict:
    """Record the source checkout and exact output after a successful UI build."""
    root = root or Path(__file__).resolve().parent.parent
    directory = _build_dir(root)
    identity = {**_source_identity(root), "build_sha256": _build_digest(directory)}
    (directory / MANIFEST).write_text(json.dumps(identity), encoding="utf-8")
    return identity


def _require_preview_owner(request: Request) -> None:
    from app.core import preview_access

    # No forwarded-header exception or hostname allow-list. This is the same
    # direct-listener ownership boundary as the normal app, plus the agent
    # browser's session-scoped preview cookie (app.core.preview_access), which
    # is held to the same listener and peer rules and is read-only here.
    from app.core.owner_auth import local_access, owner_session
    if (not local_access(request) and preview_access.grant(request) is None
            and not owner_session(request, request.cookies.get("session"))):
        raise HTTPException(403, "Branch preview requires direct local-owner access")


class _PreviewFiles(StaticFiles):
    def __init__(self, directory: Path, headers: dict):
        super().__init__(directory=directory)
        self.preview_headers = headers

    async def __call__(self, scope, receive, send):
        try:
            _require_preview_owner(Request(scope))
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(self.preview_headers)
        return response


def install_preview(app, root: Path | None = None) -> bool:
    """Install before normal SPA mounts. False leaves normal serving untouched."""
    if os.environ.get("AI_SHUFFLE_PREVIEW") != "1":
        return False
    root = root or Path(__file__).resolve().parent.parent
    directory = _build_dir(root)
    try:
        identity = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Build the frontend, then run python -m app.preview stamp") from exc
    expected = {**_source_identity(root), "build_sha256": _build_digest(directory)}
    if identity != expected:
        raise ValueError("Preview source or build changed; rebuild and run python -m app.preview stamp")
    headers = {"Cache-Control": "no-store", "X-AI-Shuffle-Preview": identity["build_sha256"]}
    router = APIRouter(dependencies=[Depends(_require_preview_owner)])

    @router.get("/api/preview")
    async def preview_identity():
        return JSONResponse(identity, headers=headers)

    async def index():
        return FileResponse(directory / "index.html", headers=headers)

    for route in ("/", "/login", "/setup", "/pair", "/plaid-link", "/chat", "/chat/{path:path}", "/settings", "/workspace"):
        router.add_api_route(route, index, methods=["GET", "HEAD"], include_in_schema=False)

    @router.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(directory / "sw.js", headers=headers, media_type="application/javascript")

    def file_handler(name):
        async def serve_file():
            return FileResponse(directory / name, headers=headers)
        return serve_file

    for name in ("favicon.svg", "manifest.json"):
        router.add_api_route("/" + name, file_handler(name), methods=["GET"], include_in_schema=False)
    app.include_router(router)
    # The phone UI the frontend build emits beside the desktop UI, under the same owner check.
    from app.mobile_ui import install_mobile_ui
    install_mobile_ui(app, directory / "mobile", headers=headers, dependencies=[Depends(_require_preview_owner)],
                      files=lambda folder: _PreviewFiles(folder, headers))
    for route, folder in (("/_app", directory / "_app"), ("/icons", directory / "icons"), ("/static", directory)):
        if folder.is_dir():
            app.mount(route, _PreviewFiles(folder, headers))
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stamp"])
    parser.parse_args()
    print(json.dumps(stamp_build()))
