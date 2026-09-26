"""Online, leased checkout and transcript handoff between authenticated workers."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from app.core import checkout_transfer, project_resolver, checkout_manager
from app.core.config import settings
from app.core.worktree_manager import WorktreeError
from app.db import database, runs, checkouts
from app.db.base import get_db
from app.db.async_database import run_sync
from app.network import control, crypto, engine

TTL = 1800  # Covers commit/push, fetch and context sync; abandoned reservations expire.
# Explicit columns only: no credentials, arbitrary tables, paths or executable jobs.
TABLES = {
    "session_messages": ("role", "content", "tool_name", "tool_input", "metadata", "jsonl_uuid", "created_at"),
    "session_events": ("id", "seq", "event_type", "payload", "command_id", "created_at"),
    "fluid_sessions": ("head_node_id", "created_at", "cli_session_id", "project_root"),
    "fluid_session_nodes": ("node_id", "parent_id", "ts", "message"),
    "fluid_session_events": ("seq", "event"),
    "session_store_entries": ("project_key", "subpath", "entry", "created_at"),
}
JSON_COLUMNS = {"tool_input", "metadata", "message", "event", "entry", "payload"}
APP_TABLES = {"session_messages", "session_events"}


def _aad(sid, target):
    return ("shuffle-session-handoff-v1:" + sid + ":" + target).encode()


def _idle(cursor, sid):
    cursor.execute("SELECT 1 FROM background_tasks WHERE session_id=%s AND status IN ('running','pending') LIMIT 1", (sid,))
    if cursor.fetchone():
        raise ValueError("Wait for this session's background work to finish before resuming here.")
    cursor.execute("SELECT 1 FROM turns WHERE session_id=%s AND state IN ('queued','running','interrupting','draining') LIMIT 1", (sid,))
    if cursor.fetchone():
        raise ValueError("Wait for this session's queued or active turn to finish before resuming here.")


def _history(session):
    rows = {}
    with get_db() as conn:
        cur = conn.cursor()
        for table, columns in TABLES.items():
            sid = session["id"] if table in APP_TABLES else session.get("sdk_session_id")
            order = " ORDER BY id" if table in ("session_messages", "session_store_entries") else ""
            cur.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE session_id=%s" + order, (sid,))
            rows[table] = [dict(row) for row in cur.fetchall()]
    return rows


def _lease(cur, sid, target, token):
    cur.execute("SELECT * FROM runs WHERE id=%s AND session_id=%s AND runner_id=%s "
                "AND state='active' AND lease_expires_at >= NOW() FOR UPDATE",
                (token, sid, "handoff:" + target))
    row = cur.fetchone()
    if not row:
        raise ValueError("The resume reservation expired. Retry from the owning worker.")
    return dict(row)


def begin(sid, target, remote=None):
    control._require_owned(sid)
    session = database.get_session(sid)
    project = project_resolver.resolve_project_v2(session)
    branch = project_resolver._effective_branch(session)
    # Project-less (plain chat) sessions move their history only.
    cwd = None
    if project:
        if not remote:
            raise ValueError("Update both workers to use Git-based resume.")
        cwd = project_resolver.resolve_existing_cwd(session)
        if not cwd:
            raise ValueError("The owning worker has no existing branch checkout to transfer.")
        if not branch:
            branch = checkout_transfer.git(cwd, "symbolic-ref", "--short", "HEAD").decode().strip()
    with get_db() as conn:
        _idle(conn.cursor(), sid)
    run = runs.claim_run(sid, "handoff:" + target, TTL)
    if not run:
        raise ValueError("The owning worker is busy. Wait for its turn to finish before resuming here.")
    try:
        state = checkout_transfer.export_checkout(cwd, branch, remote) if project else None
        data = dict(checkout=state, history=_history(session), sdk_session_id=session.get("sdk_session_id"),
                    project_id=project["id"] if project else None, token=run["id"])
        raw = json.dumps(data, default=str).encode()
        ctx = engine.require_context()
        return {"payload": crypto.encrypt_payload(ctx.network_key, raw, _aad(sid, target)), "key_version": ctx.key_version}
    except BaseException:
        runs.release_run(run["id"], run["fence_token"])
        raise


def finish(sid, target, payload):
    token = payload.get("token", "")
    # Cancellation never changes ownership and cannot release someone else's run.
    if payload.get("phase") == "cancel":
        with get_db() as conn:
            cur = conn.cursor()
            run = _lease(cur, sid, target, token)
            cur.execute("UPDATE runs SET state='released' WHERE id=%s", (run["id"],))
        return {"cancelled": True}
    session = database.get_session(sid)
    if session.get("owner_device_id") == target:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM runs WHERE id=%s AND session_id=%s AND runner_id=%s AND state='transferred'",
                        (token, sid, "handoff:" + target))
            if cur.fetchone():
                return {"committed": True}
    control._require_owned(sid)
    project = project_resolver.resolve_project_v2(session)
    if project:
        cwd = project_resolver.resolve_existing_cwd(session)
        branch = project_resolver._effective_branch(session)
        if (not cwd or (branch and branch != payload.get("branch"))
                or checkout_transfer.fingerprint(cwd) != payload.get("fingerprint")):
            raise ValueError("The owning worker's files changed during resume; ownership was not moved.")
        checkout_transfer._branch(cwd, payload["branch"])
    elif payload.get("fingerprint") is not None:
        raise ValueError("The owning worker's project changed during resume.")
    session_branch = payload.get("session_branch", payload.get("branch"))
    if session_branch not in (None, payload.get("branch")):
        raise ValueError("The receiving worker selected a different session branch.")
    with get_db() as conn:
        cur = conn.cursor()
        _lease(cur, sid, target, token)
        _idle(cur, sid)
        cur.execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s, placement=%s, branch=%s, updated_at=NOW() WHERE id=%s",
                    (target, target, json.dumps({"where": "runner:" + target}), session_branch, sid))
        if session_branch is None:
            cur.execute("UPDATE sessions SET worktree_id=NULL, worktree_branch=NULL, worktree_path=NULL WHERE id=%s", (sid,))
        cur.execute("SELECT * FROM sessions WHERE id=%s", (sid,))
        engine.record(cur, "sessions", sid, dict(cur.fetchone()), force=True)
        cur.execute("UPDATE runs SET state='transferred' WHERE id=%s", (token,))
    engine.kick()
    return {"committed": True}


async def handle(payload, peer_device_id):
    sid = str(payload.get("session_id") or "")
    member = engine.require_context().member(peer_device_id)
    if not member or member.kind != "worker" or member.status != "active":
        raise PermissionError("A handoff requires an authenticated receiving worker.")
    if payload.get("phase") == "begin":
        return await run_sync(begin, sid, peer_device_id, payload.get("remote"))
    if payload.get("phase") in ("commit", "cancel"):
        return await run_sync(finish, sid, peer_device_id, payload)
    raise ValueError("Unknown handoff phase")


def install_history(sid, data, cwd):
    from psycopg2.extras import Json
    from app.core.cli_sessions import project_key_for_directory
    old_sdk = data.get("sdk_session_id")
    # New store identity prevents replacing another local session's native tree
    # and prevents a warm provider cache from reusing stale nodes.
    sdk = ("ses_" if str(old_sdk).startswith("ses_") else "") + str(uuid.uuid4()) if old_sdk else None
    with get_db() as conn:
        cur = conn.cursor()
        for table in APP_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE session_id=%s", (sid,))
        for table, columns in TABLES.items():
            destination_id = sid if table in APP_TABLES else sdk
            for row in data["history"].get(table, []):
                values = []
                for column in columns:
                    value = row.get(column)
                    if column == "cli_session_id":
                        value = None
                    if column == "project_root":
                        value = str(Path(cwd).resolve())
                    if column == "project_key":
                        value = project_key_for_directory(cwd)
                    if column in JSON_COLUMNS and value is not None:
                        value = json.loads(value) if isinstance(value, str) else value
                        if column == "entry" and isinstance(value, dict):
                            value = {**value, "sessionId": sdk}
                            if "cwd" in value:
                                value["cwd"] = cwd
                        value = Json(value)
                    values.append(value)
                names = ("session_id", *columns)
                cur.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})",
                            (destination_id, *values))
        cur.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM session_events WHERE session_id=%s", (sid,))
        ceiling = cur.fetchone()["seq"]
        cur.execute("INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,%s) "
                    "ON CONFLICT(session_id) DO UPDATE SET next_seq=EXCLUDED.next_seq", (sid, ceiling))
        cur.execute("UPDATE sessions SET sdk_session_id=%s WHERE id=%s", (sdk, sid))
    return sdk


def _accept_owner(sid, device_id, branch=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET owner_device_id=%s, branch=%s WHERE id=%s", (device_id, branch, sid))
        if branch is None:
            cur.execute("UPDATE sessions SET worktree_id=NULL, worktree_branch=NULL, worktree_path=NULL WHERE id=%s", (sid,))


async def receive(session):
    """Publish the owner branch, fetch locally, then sync context and ownership.

    Project-less sessions skip the Git steps and transfer history only.
    """
    sid = session["id"]
    owner = control.owner_of(session)
    ctx = engine.context()
    if owner is None:
        if (not ctx or session.get("owner_device_id") != ctx.device_id) and session.get("instance_id") not in (None, "", settings.effective_instance_id):
            raise ValueError("The owning worker must be online on this network to resume here.")
        return None
    if ctx is None:
        raise ValueError("The owning worker must be online on this network to resume here.")
    project = await run_sync(project_resolver.resolve_project_v2, session)
    base = remote = None
    if project:
        if not await run_sync(project_resolver.has_local_base_checkout, session):
            raise ValueError("Clone this project on this worker before resuming here.")
        base_row = await run_sync(project_resolver._local_checkout, project["id"], settings.effective_instance_id)
        base = checkout_manager._resolve_checkout_path(base_row["path"])
        remote = await run_sync(checkout_transfer.remote_identity, base)
    reply = await control.call(owner, "handoff", {"session_id": sid, "phase": "begin", "remote": remote}, timeout=600)
    blob = reply.get("payload", "")
    raw = crypto.decrypt_payload(ctx.keyring[int(reply["key_version"])], blob, _aad(sid, ctx.device_id))
    data = json.loads(raw)
    token = data["token"]
    committed = False
    try:
        state = data["checkout"]
        if data["project_id"] != (project["id"] if project else None):
            raise ValueError("The owning worker's project changed; refresh before resuming.")
        if not project:
            # Plain chat: no checkout to move, only the conversation history.
            if state is not None:
                raise ValueError("Unexpected checkout for a project-less session.")
            cwd = str(settings.effective_workspace_dir)
            await run_sync(install_history, sid, data, cwd)
            result = await control.call(owner, "handoff", {"session_id": sid,
                "phase": "commit", "token": token,
                "fingerprint": None, "branch": None, "session_branch": None,
            }, timeout=180)
            if not result.get("committed"):
                raise ValueError("The owning worker did not confirm the transfer.")
            committed = True
            await run_sync(_accept_owner, sid, ctx.device_id, None)
            return cwd
        expected_branch = project_resolver._effective_branch(session)
        if not state or (expected_branch and state["branch"] != expected_branch):
            raise ValueError("The owning worker's branch changed; refresh before resuming.")
        try:
            dest = checkout_manager._branch_checkout_path(base, state["branch"])
        except WorktreeError as e:
            raise ValueError(str(e)) from e
        cwd = await run_sync(checkout_transfer.import_checkout, base, dest, state)
        # Keep placement on the original owner while preparing local context.
        await run_sync(install_history, sid, data, cwd)
        await run_sync(checkout_transfer._branch, cwd, state["branch"])
        if await run_sync(checkout_transfer.fingerprint, cwd) != state["head"]:
            raise ValueError("The receiving checkout changed during context sync; ownership was not moved.")
        # A base checkout must retain its branch-NULL role in the registry.
        # Relabeling it as a worktree would make later project resolution and
        # subsequent resumes report that this worker has no base clone.
        session_branch = None if Path(cwd).resolve() == Path(base).resolve() else state["branch"]
        await run_sync(checkouts.upsert_checkout, project["id"], settings.effective_instance_id, cwd,
                       branch=session_branch or "", head_sha=state["head"], dirty=False, status="active")
        result = await control.call(owner, "handoff", {"session_id": sid,
            "phase": "commit", "token": token,
            "fingerprint": state["fingerprint"],
            "branch": state["branch"],
            "session_branch": session_branch,
        }, timeout=180)
        if not result.get("committed"):
            raise ValueError("The owning worker did not confirm the transfer.")
        committed = True
        await run_sync(_accept_owner, sid, ctx.device_id, session_branch)
        return cwd
    finally:
        if not committed:
            # Best effort. A lost response/worker restart leaves a bounded lease,
            # not a permanent block; never force ownership after a timeout.
            try:
                await control.call(owner, "handoff", {"session_id": sid,"phase": "cancel", "token": token})
            except (control.RunnerUnreachable, control.ControlRefused, ValueError):
                pass
