"""Read-only authorization and runtime selection for command discovery."""

from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from app.api.auth import get_api_user_from_request
from app.api.common import check_project_access, require_origin_access
from app.api.sessions_helpers import check_session_access
from app.core.config import settings
from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
from app.db import database


def command_context(
    request: Optional[Request], project_id: Optional[str],
    worktree_id: Optional[str], session_id: Optional[str], profile_id: Optional[str] = None,
) -> tuple[str, Optional[dict]]:
    """Authorize every supplied identity before resolving or scanning any path."""
    user = get_api_user_from_request(request) if request is not None else None
    session = None
    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if request is not None:
            check_session_access(request, session)
        if user and session.get("api_user_id") not in (None, user.get("id")):
            raise HTTPException(403, "Access denied to this session")
        if project_id and project_id != session.get("project_id"):
            raise HTTPException(400, "Project does not match this session")
        if worktree_id and worktree_id != session.get("worktree_id"):
            raise HTTPException(400, "Worktree does not match this session")
        if user and user.get("project_id"):
            if session.get("project_v2_id"):
                require_origin_access(request=request, project_v2_id=session["project_v2_id"])
            elif not session.get("project_id"):
                raise HTTPException(403, "Access denied to this session")
        from app.core.project_resolver import resolve_existing_cwd
        cwd = resolve_existing_cwd(session)
        if not cwd:
            raise HTTPException(404, "Session checkout is not available on this device")
        return str(cwd), session

    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            raise HTTPException(404, "Worktree not found")
        repository = database.get_git_repository(worktree.get("repository_id"))
        owner = (repository or {}).get("project_id")
        if not owner:
            raise HTTPException(404, "Worktree project not found")
        if request is not None:
            check_project_access(request, owner)
        if project_id and project_id != owner:
            raise HTTPException(400, "Worktree does not belong to this project")
        return str(get_worktree_working_dir(worktree)), None

    # A scoped caller never falls through to the host's global workspace.
    project_id = project_id or (user or {}).get("project_id")
    if project_id:
        if request is not None:
            check_project_access(request, project_id)
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        return str(get_project_working_dir(project)), None
    config = _command_profile_config(request, None, profile_id)
    return str(config.get("cwd") or Path(settings.effective_workspace_dir)), None


def command_setting_sources(
    request: Optional[Request], session: Optional[dict], profile_id: Optional[str],
):
    """Resolve the same profile field passed by build_sdk_options to the adapter."""
    from app.core.provider.harness_adapter import _fluid_setting_sources
    from app.core.provider.types import SessionOptions

    config = _command_profile_config(request, session, profile_id)
    return _fluid_setting_sources(SessionOptions(model="", setting_sources=config.get("setting_sources")))


def _command_profile_config(request, session, profile_id):
    user = get_api_user_from_request(request) if request is not None else None
    allowed = (user or {}).get("profile_ids") or []
    profile_id = profile_id or (session or {}).get("profile_id") or (
        allowed[0] if len(allowed) == 1 else None)
    if allowed and profile_id and profile_id not in allowed:
        raise HTTPException(403, "Access denied to this profile")
    profile = database.get_profile(profile_id) if profile_id else None
    return (profile or {}).get("config") or {}


def command_provider_kind(
    request: Optional[Request], session: Optional[dict],
    model: Optional[str], profile_id: Optional[str], mode: Optional[str],
) -> str:
    """Use the adapter's route decision without creating or connecting a model."""
    from app.core.model_registry import DEFAULT_MODEL
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.core.query_options import decode_provider_model, is_native_claude_model
    from app.core.provider_agent import curated_model_ids

    config = _command_profile_config(request, session, profile_id)
    if (session or {}).get("mode", mode) == "voice":
        return "voice"
    provider_id = session.get("provider_id") if session is not None else config.get("provider")
    selected = model or (session or {}).get("model")
    raw_model = selected or (session or {}).get("launched_model") or config.get("model") or DEFAULT_MODEL
    decoded = decode_provider_model(selected)
    if decoded:
        provider_id, raw_model = decoded
    elif selected and provider_id and is_native_claude_model(selected):
        row = database.get_model_provider(provider_id, include_token=False)
        if selected not in curated_model_ids(row):
            provider_id = None
    options = SessionOptions(model=raw_model)
    if provider_id:
        row = database.get_model_provider(provider_id, include_token=False)
        if not row or not row.get("enabled", True):
            return "unavailable"
        options.extra["provider_id"] = provider_id
    return _decide_provider_kind(options)
