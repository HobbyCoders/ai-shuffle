"""The phone UI, served at /mobile/ to the Android and iOS apps.

`mobile/` builds a static SvelteKit app with `paths.base = '/mobile'`; the
frontend build copies it to build/mobile (frontend/scripts/build-mobile-ui.mjs),
so it lands in app/static/mobile beside the desktop UI on every install. The
apps load `https://<paired worker>/mobile/` and hand the page its paired
session through a document-start script, so a UI change reaches phones with the
worker update instead of a new app release. Only the apps' native code (pairing,
phone tools, permissions, the bridge version) still ships through the stores.
See docs/mobile-desktop-ui-split.md.

The page itself is public, like the desktop UI's index.html: every API call it
makes is authenticated. Hashed assets come from `/mobile/_app`; every other
path that is not a real file in the build gets the SPA entry page, except
missing file-like paths, which stay 404 instead of turning into HTML.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

PATH = "/mobile"


def resolve(directory: Path, path: str) -> Path | None:
    """The file to answer `/mobile/<path>` with, or None for a 404."""
    root = directory.resolve()
    if path:
        candidate = (root / path).resolve()
        if candidate != root and candidate.is_relative_to(root) and candidate.is_file():
            return candidate
        if "." in PurePosixPath(path).name:
            return None
    return root / "index.html"


def install_mobile_ui(app, directory: Path, *, headers: dict | None = None, dependencies: Sequence = (),
                      files: Callable[[Path], object] | None = None) -> bool:
    """Serve the phone UI build in `directory` at /mobile/. False when there is no build.

    `files` builds the ASGI app for `/mobile/_app` (branch preview wraps its
    owner check around StaticFiles); `dependencies` guard the page routes.
    """
    if not (directory / "index.html").is_file():
        return False
    assets = directory / "_app"
    if assets.is_dir():
        # Before the page routes, so a missing hashed asset is a 404, not index.html.
        app.mount(PATH + "/_app", files(assets) if files else StaticFiles(directory=str(assets)), name="mobile_assets")
    router = APIRouter(dependencies=list(dependencies))

    @router.get(PATH, include_in_schema=False)
    async def mobile_root(request: Request):
        # The page's asset URLs are relative to /mobile/; without the slash they would resolve to /.
        query = request.url.query
        return RedirectResponse(PATH + "/" + (f"?{query}" if query else ""), status_code=308)

    @router.get(PATH + "/{path:path}", include_in_schema=False)
    async def mobile_page(path: str):
        target = resolve(directory, path)
        if target is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(target, headers=headers)

    app.include_router(router)
    return True
