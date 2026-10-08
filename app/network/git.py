"""Owner-routed Git metadata and operations for independent worker databases.

Public runner IDs are device IDs. Filesystem/claim records retain the owner's
stable legacy instance ID; neither checkouts nor Git job queues are replicated.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.core.config import settings
from app.db import checkouts, git_ops
from app.db import network as store
from app.db.async_database import run_sync
from app.network import control, engine, membership


def device_id(runner_id: str) -> str:
    ctx = engine.context()
    if ctx is None:
        return runner_id
    target = engine.instance_aliases().get(runner_id, runner_id)
    for member in store.list_members(include_revoked=False):
        if target == member["device_id"]:
            if member["kind"] != membership.KIND_WORKER:
                raise ValueError("Selected device is not an active worker")
            return member["device_id"]
    raise ValueError("Unknown or revoked worker")


def local_state(project_v2_id: str, *, public: bool = False) -> dict[str, Any]:
    runtime_id = settings.effective_instance_id
    ctx = engine.context()
    canonical_id = ctx.device_id if ctx else runtime_id
    fields = ("branch", "head_sha", "dirty", "status", "updated_at")
    rows = [row for row in checkouts.list_checkouts(project_v2_id) if row["runner_id"] == runtime_id]
    summaries = [{key: row.get(key) for key in fields} for row in rows]
    if not public:
        for summary, row in zip(summaries, rows):
            summary["path"] = row.get("path")
    recent = []
    for row in git_ops.list_recent_for_project(project_v2_id, 20):
        if row["runner_id"] != runtime_id:
            continue
        entry = {key: row.get(key) for key in ("id", "op", "status", "created_at", "updated_at")}
        entry["runner_id"] = canonical_id
        if not public:
            entry.update(params=row.get("params"), result=row.get("result"))
        recent.append(entry)
    return {"eligible": checkouts.has_active_base_checkout(project_v2_id, runtime_id),
            "checkouts": summaries, "recent_ops": recent}


async def state(runner_id: str, project_v2_id: str) -> dict[str, Any]:
    ctx = engine.context()
    target = device_id(runner_id)
    if ctx and target != ctx.device_id:
        return await control.call(target, "git-state", {"project_v2_id": project_v2_id})
    return await run_sync(local_state, project_v2_id)


async def catalog(runner_id: str) -> list[str]:
    """Owner-reported eligibility only: no checkout paths or executable jobs."""
    ctx = engine.context()
    target = device_id(runner_id)
    if ctx and target != ctx.device_id:
        return (await control.call(target, "git-catalog", {}))["project_ids"]
    return await run_sync(checkouts.eligible_base_project_ids, settings.effective_instance_id)


async def handle_catalog(body: dict, from_device: str) -> dict:
    return {"project_ids": await run_sync(
        checkouts.eligible_base_project_ids, settings.effective_instance_id)}


async def eligible(runner_id: str, project_v2_id: str) -> bool:
    if engine.context() is None:
        return await run_sync(checkouts.has_active_base_checkout, project_v2_id, runner_id)
    return bool((await state(runner_id, project_v2_id))["eligible"])


async def enqueue(runner_id: str, operation: str, *, project_v2_id: str, params: dict | None = None) -> dict:
    ctx = engine.context()
    target = device_id(runner_id)
    if ctx and target != ctx.device_id:
        return await control.call(target, "git-op", {
            "operation": operation, "project_v2_id": project_v2_id,
            "params": params or {}, "request_id": "gitop-" + uuid.uuid4().hex,
        })
    runtime_id = settings.effective_instance_id if ctx else runner_id
    row = await run_sync(git_ops.insert_op, runtime_id, operation, project_v2_id=project_v2_id, params=params or {})
    from app.core import git_ops_runner
    if runtime_id == settings.effective_instance_id:
        git_ops_runner.kick()
    return {"id": row["id"], "runner_id": target}


async def handle_state(body: dict, from_device: str) -> dict:
    project_id = str(body.get("project_v2_id") or "")
    if not project_id:
        raise ValueError("project_v2_id is required")
    return await run_sync(local_state, project_id, public=True)


async def handle_op(body: dict, from_device: str) -> dict:
    ctx = engine.context()
    if ctx is None:
        raise PermissionError("Worker is not enrolled")
    device_id(ctx.device_id)  # Reject work after local revocation as well.
    operation = body.get("operation")
    if operation not in {"refresh_status", "ensure_worktree"}:
        raise ValueError("Unsupported remote Git operation")
    project_id = str(body.get("project_v2_id") or "")
    if not project_id or not await run_sync(checkouts.has_active_base_checkout, project_id, settings.effective_instance_id):
        raise ValueError("Project has no active base checkout on this worker")
    params = body.get("params") or {}
    allowed = {"branch", "base_branch", "create_new_branch", "session_id"} if operation == "ensure_worktree" else set()
    if not isinstance(params, dict) or set(params) - allowed:
        raise ValueError("Unsupported Git operation parameters")
    session_id = params.get("session_id")
    if session_id is not None and not (isinstance(session_id, str) and 0 < len(session_id) <= 80):
        raise ValueError("Invalid Git operation session ID")
    request_id = str(body.get("request_id") or "")
    if not request_id.startswith("gitop-") or len(request_id) > 80:
        raise ValueError("Invalid Git operation request ID")
    row = await run_sync(git_ops.insert_op, settings.effective_instance_id, operation,
                         project_v2_id=project_id, params=params, op_id=request_id)
    # The regular owner claim loop consumes this durable row; do not launch an
    # untracked task on the request handler's event loop.
    return {"id": row["id"], "runner_id": ctx.device_id}


async def export_uploads(blob_ids: list[str]) -> list[dict]:
    import base64
    from app.db import upload_blobs
    records = await run_sync(upload_blobs.get_blobs, blob_ids)
    result = []
    for row in records:
        data = row.get("data")
        if data is None:
            data = await run_sync(upload_blobs.get_blob_data, row["id"])
        if data is None:
            raise ValueError("Upload bytes are unavailable; upload the file again")
        result.append({key: row.get(key) for key in ("id", "project_v2_id", "source_path", "filename", "size")}
                      | {"data": base64.b64encode(bytes(data)).decode("ascii")})
    if len(result) != len(blob_ids):
        raise ValueError("An upload is unavailable; upload the file again")
    return result


def import_uploads(session_id: str, records: list[dict]) -> None:
    import base64
    from app.api.projects import MAX_UPLOAD_SIZE
    from app.db.base import get_db
    if not isinstance(records, list) or len(records) > 50:
        raise ValueError("Invalid upload transfer")
    with get_db() as conn:
        cursor = conn.cursor()
        for row in records:
            data = base64.b64decode(row["data"], validate=True)
            if len(data) != row["size"] or len(data) > MAX_UPLOAD_SIZE:
                raise ValueError("Invalid upload size")
            cursor.execute("SELECT session_id FROM upload_blobs WHERE id = %s", (row["id"],))
            existing = cursor.fetchone()
            if existing and existing["session_id"] != session_id:
                raise ValueError("Upload belongs to another session")
            cursor.execute(
                "INSERT INTO upload_blobs (id, project_v2_id, session_id, source_path, filename, size, data) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (row["id"], row.get("project_v2_id"), session_id, row["source_path"], row["filename"], len(data), data),
            )
