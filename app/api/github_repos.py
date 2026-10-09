"""
The signed-in GitHub account's repositories, for the clone picker.

GET /api/v1/github/repos uses the existing GitHub connection (the ``gh`` CLI
signed in from Settings); there is no separate token or OAuth flow. The full
list is fetched once and cached by ``github_service``; search and paging run
on that cached list, so typing in the picker does not call GitHub.

Owner-only: the list includes private repositories of every organization the
account belongs to. Nothing in the response, or in what this module logs,
contains the account's token: gh reads it from its own config, and gh error
text is passed through credential redaction before it is returned.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.auth import require_admin
from app.core.export_redaction import redact_export_text
from app.core.github_service import GitHubCliError, github_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["GitHub"])

MAX_PER_PAGE = 100


class GitHubRepo(BaseModel):
    full_name: str
    name: str
    owner: str
    private: bool
    fork: bool = False
    archived: bool = False
    description: str | None = None
    clone_url: str
    html_url: str
    default_branch: str | None = None
    updated_at: str | None = None


class GitHubReposResponse(BaseModel):
    connected: bool
    items: list[GitHubRepo]
    total: int
    page: int
    per_page: int
    has_more: bool
    # True when the account has more repositories than the picker fetches;
    # the oldest are left out (any of them can still be cloned by URL).
    truncated: bool = False


def _matches(repo: dict, terms: list[str]) -> bool:
    haystack = f"{repo['full_name']} {repo.get('description') or ''}".lower()
    return all(term in haystack for term in terms)


@router.get("/repos", response_model=GitHubReposResponse)
async def list_github_repos(
    q: str = Query(default="", max_length=200, description="Filter by name, owner or description"),
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=30, ge=1, le=MAX_PER_PAGE),
    refresh: bool = Query(default=False, description="Refetch instead of using the cached list"),
    token: str = Depends(require_admin),
):
    """List repositories the connected GitHub account can clone.

    Returns ``connected: false`` (not an error) when GitHub is not connected
    on this worker, so the picker can show where to connect it.
    """
    if not await asyncio.to_thread(github_service.is_authenticated, refresh=refresh):
        return GitHubReposResponse(
            connected=False, items=[], total=0, page=page, per_page=per_page, has_more=False,
        )

    try:
        repos, truncated = await asyncio.to_thread(github_service.list_user_repos, refresh=refresh)
    except GitHubCliError as exc:
        detail = redact_export_text(exc.stderr or str(exc))
        logger.warning("Could not list GitHub repositories: %s", detail)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub could not list your repositories: {detail}",
        )

    terms = [t for t in q.lower().split() if t]
    matched = [r for r in repos if _matches(r, terms)] if terms else repos
    start = (page - 1) * per_page
    items = matched[start:start + per_page]
    return GitHubReposResponse(
        connected=True,
        items=[GitHubRepo(**r) for r in items],
        total=len(matched),
        page=page,
        per_page=per_page,
        has_more=start + per_page < len(matched),
        truncated=truncated,
    )
