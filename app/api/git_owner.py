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
_MUTATING_ACTIONS = {
    'init_repository', 'clone_repository', 'create_branch', 'delete_branch',
    'checkout_branch', 'fetch_from_remote', 'stage_files', 'unstage_files',
    'discard_changes', 'create_commit', 'push_commits', 'pull_from_remote',
    'create_pull_request', 'finish', 'ship',
}

async def _guard_mutation(action_name, values, endpoint=None):
    if action_name not in _MUTATING_ACTIONS:
        return
    from app.api.common import check_project_access
    from app.api.git_helpers import get_working_dir_async
    from app.core.git_activity import require_idle_git_target
    project_id = values['project_id']
    # Use the endpoint's existing ACL/cwd implementation (the registered
    # server function, never a peer-selected module or caller-supplied path).
    namespace = getattr(endpoint, '__globals__', {})
    access = namespace.get('check_project_access', check_project_access)
    resolve = namespace.get('get_working_dir_async', get_working_dir_async)
    access(values['request'], project_id)
    body = values.get('body')
    sid = values.get('session_id') or getattr(body, 'session_id', None)
    wid = values.get('worktree_id') or getattr(body, 'worktree_id', None)
    if action_name == 'checkout_branch' and wid:
        raise HTTPException(400, 'Cannot checkout branches in a worktree. Worktrees are bound to their branch.')
    try:
        cwd = await resolve(project_id, wid, sid)
    except HTTPException as exc:
        # GitHub-only projects need not have a local clone. Branch activity is
        # still checked against the authenticated project's canonical aliases.
        if action_name != 'create_pull_request' or exc.status_code not in {404, 409}:
            raise
        cwd = None
    branches = [values.get('branch_name')]
    branches.extend(getattr(body, key, None) for key in ('branch_name', 'branch', 'ref', 'head', 'name', 'base_branch'))
    await run_sync(require_idle_git_target, project_id, cwd, branches)



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
        worker_id = request.query_params.get("worker_id") if request else None
        if worker_id:
            if session_id or values.get("worktree_id"):
                raise HTTPException(400, "Worker Git scope cannot include a session or legacy worktree")
            return await _route_project_git(endpoint, values, worker_id, request.query_params.get("checkout_branch"))
        if not session_id:
            await _guard_mutation(endpoint.__name__, values, endpoint)
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
            await _guard_mutation(endpoint.__name__, bound.arguments, endpoint)
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
        await _guard_mutation(body["action"], values, endpoint)
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

# Only checkout-scoped actions are supported without a persisted session.
# Session lifecycle, init/clone and GitHub orchestration must not run on a
# caller's local project merely because the composer selected another worker.
_PROJECT_ACTIONS = {
    "get_git_status", "list_branches", "get_commit_graph", "get_diff_stats",
    "get_changed_files", "get_file_diff", "get_pr_state", "fetch_from_remote",
    "pull_from_remote", "push_commits", "create_commit", "stage_files", "unstage_files",
    "discard_changes", "create_branch", "checkout_branch", "delete_branch",
    "generate_commit_message",
}


async def _route_project_git(endpoint, values, worker_id, branch):
    from app.api.common import require_origin_access
    from app.db.projects_v2 import get_project_v2
    from app.network import engine, git as network_git

    project_id = values["project_id"]
    identity = await run_sync(require_origin_access, request=values["request"], **(
        {"project_v2_id": project_id} if await run_sync(get_project_v2, project_id)
        else {"project_id": project_id}
    ))
    if endpoint.__name__ not in _PROJECT_ACTIONS:
        raise HTTPException(409, "This Git action requires a session on the selected worker")
    ctx = engine.context()
    from app.core.config import settings
    local = ctx.device_id if ctx else settings.effective_instance_id
    try:
        target = local if worker_id == "local" else await run_sync(network_git.device_id, worker_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    params = jsonable_encoder({k: v for k, v in values.items() if k not in {"request", "token"}})
    params["project_id"] = identity["project_v2"]["id"]
    payload = {"action": endpoint.__name__, "project_id": params["project_id"], "branch": branch, "params": params}
    try:
        result = (await handle_project_git(payload, local) if target == local else
                  await control.call(target, "project-git", payload,
                                     timeout=20.0 if values["request"].method == "GET" else 300.0))
    except control.RunnerUnreachable as exc:
        raise HTTPException(503, "Selected worker is unreachable. No local Git action was run.") from exc
    except control.ControlRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    if result["status"] >= 400:
        raise HTTPException(result["status"], result["detail"])
    return result["result"]


async def handle_project_git(body, from_device):
    from app.api import git  # noqa: F401
    from app.api.git_helpers import _worker_checkout_scope
    from app.core.checkout_manager import _dir_is_worktree, _resolve_checkout_path
    from app.core.config import settings
    from app.core.git_service import git_service
    from app.db import checkouts, projects_v2

    name = body.get("action")
    if name not in _PROJECT_ACTIONS or name not in _actions:
        raise ValueError("Unsupported project Git action")
    project_id = body.get("project_id")
    if not await run_sync(projects_v2.get_project_v2, project_id):
        return {"status": 404, "detail": "Project not found on selected worker"}
    endpoint, signature, hints = _actions[name]
    params = body.get("params")
    if not isinstance(params, dict) or params.get("project_id") != project_id or set(params) - (set(signature.parameters) - {"request", "token"}):
        raise ValueError("Invalid project Git parameters")
    if params.get("session_id") or params.get("worktree_id"):
        raise ValueError("Mixed project and session Git scope")
    branch = body.get("branch") or None
    if branch is not None and (not isinstance(branch, str) or not control._BRANCH_RE.fullmatch(branch)):
        raise ValueError("Invalid checkout branch")
    rows = await run_sync(checkouts.list_checkouts, project_id)
    candidates = [r for r in rows if r["runner_id"] == settings.effective_instance_id
                  and (r.get("status") or "active") == "active"
                  and (not r.get("branch") or r.get("branch") == branch)]
    # A named branch must match the repository too, not only stale DB metadata.
    paths = []
    for row in candidates:
        path = _resolve_checkout_path(row["path"])
        if not await run_sync(_dir_is_worktree, path):
            continue
        actual = await run_sync(git_service.get_current_branch, path)
        if branch is None and row.get("branch"):
            continue
        if branch is not None and actual != branch:
            continue
        paths.append((path, row))
    if len(paths) != 1:
        return {"status": 409, "detail": "Selected checkout is missing or ambiguous on this worker"}
    path, checkout = paths[0]
    if name == "checkout_branch" and checkout.get("branch"):
        return {"status": 400, "detail": "Cannot checkout branches in a worktree. Worktrees are bound to their branch."}
    try:
        values = {}
        for name, parameter in signature.parameters.items():
            if name in {"request", "token"}:
                continue
            if name in params:
                values[name] = TypeAdapter(hints[name]).validate_python(params[name])
            elif parameter.default is inspect.Parameter.empty:
                raise ValueError(f"Missing Git parameter: {name}")
            else:
                values[name] = parameter.default
        payload = values.get("body")
        if getattr(payload, "session_id", None) or getattr(payload, "worktree_id", None):
            raise ValueError("Mixed project and session Git scope")
        request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "state": {}})
        request.state.api_user = None  # Authenticated peer membership authorizes this catalog.
        values.update(request=request, token="worker-control")
        scope = _worker_checkout_scope.set((project_id, path))
        try:
            await _guard_mutation(body["action"], values, endpoint)
            result = await endpoint(**values)
        finally:
            _worker_checkout_scope.reset(scope)
        return {"status": 200, "result": jsonable_encoder(result)}
    except ValidationError as exc:
        return {"status": 422, "detail": jsonable_encoder(exc.errors(), custom_encoder={ValueError: str})}
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}
