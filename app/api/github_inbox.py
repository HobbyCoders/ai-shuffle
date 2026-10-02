"""
Cross-project pull request inbox.

One read across every project the caller can see: each project's GitHub
origin is resolved locally, projects sharing a repository are folded
together, and ``gh pr list`` runs once per repository with review and check
state. Filtering (state, review, checks, project) is done by the client on
the returned list.

A project without a GitHub origin, or one ``gh`` cannot read, is reported
in ``skipped`` instead of failing the whole inbox.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.auth import get_api_user_from_request, require_auth
from app.api.github import check_github_auth
from app.core.github_service import GitHubCliError, github_service
from app.core.project_utils import get_project_working_dir
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/github", tags=["GitHub"])

# gh calls in flight at once; each one is a subprocess and an API request.
_MAX_CONCURRENT_REPOS = 4


class InboxProject(BaseModel):
    id: str
    name: str


class InboxPullRequest(BaseModel):
    repo: str
    number: int
    title: str
    url: str
    author: str
    head_branch: str
    base_branch: str
    state: str
    review: str
    checks: str
    updated_at: str
    projects: List[InboxProject]


class InboxSkipped(BaseModel):
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    repo: Optional[str] = None
    reason: str


class InboxResponse(BaseModel):
    items: List[InboxPullRequest]
    skipped: List[InboxSkipped]
    repos: List[str]


def _visible_projects(api_user_project_id: Optional[str]) -> List[Dict[str, Any]]:
    """Projects the caller can see (an API user only sees its own project)."""
    from app.api.projects import _load_projects_for_listing

    projects, _counts = _load_projects_for_listing(api_user_project_id)
    return [p for p in projects if p]


def _repos_by_project(projects: List[Dict[str, Any]]) -> tuple:
    """Blocking: map each project to its GitHub repository.

    Returns ``(repos, skipped)`` where ``repos`` is
    ``{"owner/repo": [InboxProject, ...]}`` in first-seen order.
    """
    repos: Dict[str, List[InboxProject]] = {}
    skipped: List[InboxSkipped] = []
    for project in projects:
        ref = InboxProject(id=str(project.get("id")), name=str(project.get("name") or project.get("id")))
        try:
            path = get_project_working_dir(project)
        except Exception:  # noqa: BLE001 — one bad row must not sink the inbox
            path = None
        if not path or not os.path.isdir(path):
            skipped.append(InboxSkipped(project_id=ref.id, project_name=ref.name, reason="Project folder is not on this machine"))
            continue
        repo = github_service.get_repo_from_remote(path)
        if not repo:
            skipped.append(InboxSkipped(project_id=ref.id, project_name=ref.name, reason="No GitHub remote"))
            continue
        repos.setdefault(repo, []).append(ref)
    return repos, skipped


@router.get("/inbox", response_model=InboxResponse)
async def pull_request_inbox(
    request: Request,
    state: str = Query("open", pattern="^(open|closed|merged|all)$"),
    limit: int = Query(30, ge=1, le=100, description="Pull requests per repository"),
    token: str = Depends(require_auth),
) -> InboxResponse:
    """Pull requests across every project's repository, newest activity first."""
    await check_github_auth()

    api_user = get_api_user_from_request(request)
    api_user_project_id = api_user.get("project_id") if api_user else None
    projects = await run_sync(_visible_projects, api_user_project_id)
    repos, skipped = await asyncio.to_thread(_repos_by_project, projects)

    gate = asyncio.Semaphore(_MAX_CONCURRENT_REPOS)

    async def fetch(repo: str) -> tuple:
        async with gate:
            try:
                return repo, await asyncio.to_thread(github_service.list_inbox_pulls, repo, state, limit), None
            except GitHubCliError as exc:
                logger.warning("PR inbox: gh could not list %s: %s", repo, exc)
                return repo, [], exc.stderr or str(exc)

    results = await asyncio.gather(*(fetch(repo) for repo in repos))

    items: List[InboxPullRequest] = []
    for repo, pulls, error in results:
        if error is not None:
            skipped.append(InboxSkipped(repo=repo, reason=f"GitHub CLI could not list pull requests: {error}"))
            continue
        for pr in pulls:
            items.append(InboxPullRequest(repo=repo, projects=repos[repo], **pr))

    items.sort(key=lambda pr: pr.updated_at, reverse=True)
    return InboxResponse(items=items, skipped=skipped, repos=list(repos))
