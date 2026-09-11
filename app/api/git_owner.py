"""Route session Git actions through the authenticated worker control channel.

Only explicitly decorated endpoints are callable by peers. Both devices check
the session/project binding; the owner supplies its own checkout identifiers.
The regular endpoint remains the implementation for local and remote actions.
"""
import inspect
from functools import wraps
from typing import get_type_hints

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import TypeAdapter, ValidationError

from app.db import database
from app.db.async_database import run_sync
from app.network import control

_actions = {}


def _check_project(session, project_id):
    from app.core.project_resolver import resolve_project_v2
    if project_id in (session.get("project_id"), session.get("project_v2_id")):
        return
    project = resolve_project_v2({"project_id": project_id})
    if project and project["id"] == session.get("project_v2_id"):
        return
    raise HTTPException(403, "Project does not belong to this session")


def session_git_action(endpoint):
    """Runs after FastAPI authentication and input validation, before any Git IO."""
    signature = inspect.signature(endpoint)
    hints = get_type_hints(endpoint)
    _actions[endpoint.__name__] = (endpoint, signature, hints)

    @wraps(endpoint)
    async def routed(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        values = bound.arguments
        request = values.get("request")
        body = values.get("body")
        session_id = values.get("session_id") or getattr(body, "session_id", None)
        if not session_id:
            return await endpoint(*args, **kwargs)
        session = await run_sync(database.get_session, session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        from app.api.common import check_project_access
        from app.api.sessions_helpers import check_session_access
        check_session_access(request, session)
        check_project_access(request, values["project_id"])
        await run_sync(_check_project, session, values["project_id"])
        owner = control.owner_of(session)
        if owner is None:
            bound.arguments["project_id"] = await run_sync(_owner_project_id, session, values["project_id"])
            return await endpoint(*bound.args, **bound.kwargs)
        params = jsonable_encoder({k: v for k, v in values.items() if k not in {"request", "token"}})
        try:
            result = await control.call(owner, "session-git", {
                "action": endpoint.__name__, "session_id": session_id, "params": params,
            }, timeout=20.0 if request.method == "GET" else 300.0)
        except control.RunnerUnreachable as exc:
            raise HTTPException(503, "Owning worker is unreachable. Reconnect it to use Git actions.") from exc
        except control.ControlRefused as exc:
            raise HTTPException(409, str(exc)) from exc
        if result["status"] >= 400:
            raise HTTPException(result["status"], result["detail"])
        return result["result"]

    return routed


async def handle_session_git(body, from_device):
    # Import only the explicit action catalog, never a peer-selected module or URL.
    from app.api import git, git_ai, git_operations, github  # noqa: F401

    action = _actions.get(body.get("action"))
    if action is None:
        raise ValueError("Unsupported session Git action")
    session_id = str(body.get("session_id") or "")
    session = await run_sync(control._require_owned, session_id)
    endpoint, signature, hints = action
    params = body.get("params")
    if not isinstance(params, dict) or set(params) - (set(signature.parameters) - {"request", "token"}):
        raise ValueError("Invalid Git action parameters")
    try:
        await run_sync(_check_project, session, params.get("project_id"))
        values = {}
        for name, parameter in signature.parameters.items():
            if name in {"request", "token"}:
                continue
            if name in params:
                values[name] = TypeAdapter(hints[name]).validate_python(params[name])
            elif parameter.default is inspect.Parameter.empty:
                raise ValueError(f"Missing Git action parameter: {name}")
            else:
                values[name] = parameter.default
        payload = values.get("body")
        supplied_session = values.get("session_id") or getattr(payload, "session_id", None)
        if supplied_session != session_id:
            raise ValueError("Git action session does not match owner request")
        # Never trust another device's local worktree ID. The session is the
        # authority even when the same branch/project exists on both devices.
        if "worktree_id" in values:
            values["worktree_id"] = session.get("worktree_id")
        if payload is not None and "worktree_id" in type(payload).model_fields:
            payload.worktree_id = session.get("worktree_id")
        values["project_id"] = await run_sync(_owner_project_id, session, values["project_id"])
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "state": {}})
        request.state.api_user = None  # Peer membership + owned-session check authorize this call.
        values.update(request=request, token="worker-control")
        result = await endpoint(**values)
        return {"status": 200, "result": jsonable_encoder(result)}
    except ValidationError as exc:
        return {"status": 422, "detail": jsonable_encoder(exc.errors(), custom_encoder={ValueError: str})}
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}


def _owner_project_id(session, fallback):
    """Translate a mirrored/v2 project to this worker's legacy Git API key."""
    if session.get("project_id"):
        return session["project_id"]
    from app.core.config import settings
    from app.core.project_resolver import origin_for_legacy_project
    from app.db import projects_v2
    project = projects_v2.get_project_v2(session.get("project_v2_id"))
    if project:
        for local in database.get_all_projects():
            if local.get("instance_id") not in (None, "", settings.effective_instance_id):
                continue
            if origin_for_legacy_project(local) == project["origin"]:
                return local["id"]
    return session.get("project_v2_id") or fallback
