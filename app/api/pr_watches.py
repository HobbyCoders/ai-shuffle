"""Owner-only PR watches: start, stop and list (app/core/pr_watch.py).

Clicking Watch is the owner's authorization for the watch to wake the PR's
chat within the limits set in Settings > Usage; no further approval card.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_admin
from app.db.async_database import run_sync

router = APIRouter(
    prefix="/api/v1/pr-watches", tags=["PR watches"], dependencies=[Depends(require_admin)]
)

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class StartWatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(..., max_length=200)
    pr_number: int = Field(..., ge=1)
    pr_url: Optional[str] = Field(None, max_length=500)
    pr_title: Optional[str] = Field(None, max_length=500)
    session_id: Optional[str] = Field(None, max_length=200)


@router.get("")
async def list_pr_watches() -> dict[str, Any]:
    """Active watches, plus those that stopped or ended in the last day."""
    from app.core import pr_watch
    from app.db import pr_watches

    rows = await run_sync(pr_watches.list_watches)
    return {"watches": [pr_watch.view(r) for r in rows]}


@router.post("")
async def start_pr_watch(body: StartWatch) -> dict[str, Any]:
    """Watch a pull request. Watching one already watched returns that watch."""
    from app.core import pr_watch

    if not _REPO.match(body.repo):
        raise HTTPException(400, "repo must look like owner/name")
    if body.pr_url and not body.pr_url.startswith("https://"):
        raise HTTPException(400, "pr_url must be an https link")
    try:
        row = await run_sync(lambda: pr_watch.start_watch(
            repo=body.repo, pr_number=body.pr_number, pr_url=body.pr_url,
            pr_title=body.pr_title, session_id=body.session_id,
        ))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    pr_watch.pr_watch_manager.notify()
    return row


@router.delete("/{watch_id}")
async def stop_pr_watch(watch_id: str) -> dict[str, Any]:
    """Stop a watch. Stopping one that already ended returns it unchanged."""
    from app.core import pr_watch

    row = await run_sync(pr_watch.stop_watch, watch_id)
    if row is None:
        raise HTTPException(404, "No such PR watch")
    return row
