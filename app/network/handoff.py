"""Online, leased checkout and transcript handoff between authenticated workers."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import uuid

from app.core import checkout_transfer, project_resolver, checkout_manager
from app.core.config import settings
from app.core.worktree_manager import WorktreeError
from app.db import database, runs, checkouts
from app.db.base import get_db
from app.db.async_database import run_sync
from app.network import control, crypto, engine

logger = logging.getLogger(__name__)

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


HANDOFF = "handoff:"
# Runner id prefix for a handoff's reservation of another session's run slot.
HOLD = "handoff-hold:"
BUSY_CHECKOUT = ("Another session is working in this checkout or on this branch. "
                 "Wait for it to finish before moving this session.")
UNVERIFIED_CHECKOUT = ("Cannot verify which sessions use this checkout or branch. "
                       "The session was not moved.")


def _bound_branch(row):
    return row.get("branch") or row.get("worktree_branch") or None


def _session_cwd(row):
    if row.get("worktree_id"):
        from app.core.project_utils import get_worktree_working_dir
        worktree = database.get_worktree(row["worktree_id"])
        return get_worktree_working_dir(worktree) if worktree else None
    return project_resolver.resolve_existing_cwd(row)


def _checkout_users(session, project, path, branch):
    """This worker's other sessions that work in ``path`` or on ``branch``.

    Hidden sessions and legacy project aliases are included. Sessions another
    worker executes run in that worker's checkout, not this one.
    """
    from app.db.sessions import list_sessions_for_cleanup
    target = Path(path).resolve() if path else None
    users = []
    for row in list_sessions_for_cleanup(session.get("project_id") or project["id"], project["id"]):
        sid = row.get("id")
        if not sid or sid == session["id"] or control.owner_of(row) is not None:
            continue
        if branch and _bound_branch(row) == branch:
            users.append(sid)
            continue
        cwd = _session_cwd(row) if target else None
        if cwd and Path(cwd).resolve() == target:
            users.append(sid)
    return users


def _quiet(cursor, sid):
    """Refuse when a held session already has live work that predates the hold."""
    from app.core.task_registry import task_registry
    cursor.execute("SELECT 1 FROM turns WHERE session_id=%s AND state IN ('running','interrupting','draining') LIMIT 1", (sid,))
    if cursor.fetchone():
        raise ValueError(BUSY_CHECKOUT)
    cursor.execute("SELECT 1 FROM background_tasks WHERE session_id=%s AND status IN ('running','pending') LIMIT 1", (sid,))
    if cursor.fetchone() or task_registry.has_active_background_work(sid):
        raise ValueError(BUSY_CHECKOUT)


def _hold_checkout(session, project, path, branch, holder, held):
    """Reserve run admission for every other session using this checkout/branch.

    Turn admission claims a session's run lease before its turn may start, so
    a held session cannot start until the hold is released or expires. The
    caller's own reservation is written first and the scan runs after it, so a
    concurrent start is either held here, refused here, or sees the
    reservation in :func:`admission_held`. The caller releases ``held``.
    """
    from app.core.git_activity import GitActivityUnavailable, busy_git_target_sessions
    try:
        for _ in range(3):
            fresh = [sid for sid in _checkout_users(session, project, path, branch) if sid not in held]
            if not fresh:
                break
            for sid in fresh:
                run = runs.claim_run(sid, holder, TTL)
                if not run:
                    raise ValueError(BUSY_CHECKOUT)
                held[sid] = run
        else:
            raise ValueError(UNVERIFIED_CHECKOUT)
        with get_db() as conn:
            cursor = conn.cursor()
            for sid in held:
                if sid != session["id"]:
                    _quiet(cursor, sid)
        # Also apply the Git toolbar's guard to anything the scan missed, such
        # as a session whose checkout is now on this branch.
        existing = path if path and Path(path).is_dir() else None
        busy = busy_git_target_sessions(project["id"], existing, (branch,), exclude=(session["id"], *held))
    except GitActivityUnavailable as exc:
        raise ValueError(UNVERIFIED_CHECKOUT) from exc
    if busy:
        raise ValueError(BUSY_CHECKOUT)


def _release(held):
    for run in held.values():
        runs.release_run(run["id"], run["fence_token"])
    held.clear()


def _release_holds(cursor, holder):
    cursor.execute("UPDATE runs SET state='released', updated_at=NOW() WHERE runner_id=%s AND state='active'", (holder,))


def _wake_queued():
    """Let turns that waited behind released holds start without a full poll."""
    try:
        from app.core import turn_claim
        turn_claim.kick("turn_claim")
    except Exception:  # The periodic claim pass still runs.
        logger.debug("Could not wake the turn claim pass after a handoff", exc_info=True)


def admission_held(sid):
    """True while a handoff on this worker reserves this session's checkout.

    Run admission calls this after claiming the session's lease. A handoff
    writes its reservation before scanning for checkout users, so either the
    scan sees that claim or this read sees the reservation.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id, runner_id FROM runs WHERE state='active' "
                       "AND lease_expires_at >= NOW() AND session_id <> %s", (sid,))
        reserved = {row["session_id"] for row in cursor.fetchall()
                    if str(row["runner_id"]).startswith((HANDOFF, HOLD))}
    if not reserved:
        return False
    mine = database.get_session(sid)
    project = project_resolver.resolve_project_v2(mine) if mine else None
    if not project:
        return False
    cwd = None
    for other_id in reserved:
        other = database.get_session(other_id)
        other_project = project_resolver.resolve_project_v2(other) if other else None
        if not other_project or other_project["id"] != project["id"]:
            continue
        # Equal branches, or two base-checkout sessions, share a checkout.
        if _bound_branch(other) == _bound_branch(mine):
            return True
        cwd = cwd or _session_cwd(mine)
        other_cwd = _session_cwd(other)
        if cwd and other_cwd and Path(cwd).resolve() == Path(other_cwd).resolve():
            return True
    return False


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
                (token, sid, HANDOFF + target))
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
    run = runs.claim_run(sid, HANDOFF + target, TTL)
    if not run:
        raise ValueError("The owning worker is busy. Wait for its turn to finish before resuming here.")
    held = {}
    try:
        if project:
            # Publication commits and pushes the shared checkout. Exclude other
            # sessions working there; they keep their files and work.
            _hold_checkout(session, project, cwd, branch, HOLD + run["id"], held)
        state = checkout_transfer.export_checkout(cwd, branch, remote) if project else None
        data = dict(checkout=state, history=_history(session), sdk_session_id=session.get("sdk_session_id"),
                    project_id=project["id"] if project else None, token=run["id"])
        raw = json.dumps(data, default=str).encode()
        ctx = engine.require_context()
        return {"payload": crypto.encrypt_payload(ctx.network_key, raw, _aad(sid, target)), "key_version": ctx.key_version}
    except BaseException:
        _release(held)
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
            _release_holds(cur, HOLD + run["id"])
        return {"cancelled": True}
    try:
        return _commit(sid, target, payload, token)
    except BaseException:
        # A refused commit never moves ownership. The receiver cancels the
        # moving session's own lease; other sessions need not wait for that.
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM runs WHERE id=%s AND session_id=%s AND runner_id=%s AND state='active'",
                        (token, sid, HANDOFF + target))
            if cur.fetchone():
                _release_holds(cur, HOLD + token)
        raise


def _commit(sid, target, payload, token):
    session = database.get_session(sid)
    if session.get("owner_device_id") == target:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM runs WHERE id=%s AND session_id=%s AND runner_id=%s AND state='transferred'",
                        (token, sid, HANDOFF + target))
            if cur.fetchone():
                _release_holds(cur, HOLD + token)
                return {"committed": True}
    control._require_owned(sid)
    project = project_resolver.resolve_project_v2(session)
    if project:
        cwd = project_resolver.resolve_existing_cwd(session)
        branch = project_resolver._effective_branch(session)
        if not cwd:
            raise ValueError(
                "The owning worker's checkout is unavailable or on a different branch; "
                "ownership was not moved."
            )
        if ((branch and branch != payload.get("branch"))
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
        _release_holds(cur, HOLD + token)
    engine.kick()
    return {"committed": True}


async def handle(payload, peer_device_id):
    sid = str(payload.get("session_id") or "")
    member = engine.require_context().member(peer_device_id)
    if not member or member.kind != "worker" or member.status != "active":
        raise PermissionError("A handoff requires an authenticated receiving worker.")
    if payload.get("phase") == "receive":
        from app.api.git_helpers import invalidate_working_dir_cache
        from app.core import streaming_status

        session = await run_sync(database.get_session, sid)
        if not session:
            raise LookupError("Unknown session")
        if streaming_status.has_active_run(sid):
            raise ValueError("Stop this session's active run before moving it.")
        try:
            cwd = await receive(session)
        except (control.RunnerUnreachable, control.ControlRefused) as exc:
            # Preserve the owner's refusal/offline reason across the outer
            # control hop instead of turning an expected refusal into HTTP 500.
            raise ValueError(str(exc)) from exc
        invalidate_working_dir_cache(session_id=sid)
        return {"moved": cwd is not None}
    if payload.get("phase") == "begin":
        try:
            return await run_sync(begin, sid, peer_device_id, payload.get("remote"))
        except BaseException:
            _wake_queued()
            raise
    if payload.get("phase") in ("commit", "cancel"):
        try:
            return await run_sync(finish, sid, peer_device_id, payload)
        finally:
            _wake_queued()
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
        # instance_id moves too (as adopt/unpin do): left on the previous
        # worker's id, the session listed as remote with an unknown state here.
        cur.execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s, placement=%s, branch=%s, updated_at=NOW() WHERE id=%s",
                    (device_id, settings.effective_instance_id, json.dumps({"where": "runner:" + device_id}), branch, sid))
        if branch is None:
            cur.execute("UPDATE sessions SET worktree_id=NULL, worktree_branch=NULL, worktree_path=NULL WHERE id=%s", (sid,))


async def move(session, target):
    """Ask an authenticated destination to pull through the canonical protocol.

    No checkout or transcript is supplied by the UI worker. The destination
    reads its mirrored session and obtains Git identifiers and encrypted context
    directly from the current owner, under the same lease as Resume here.
    """
    from app.network import runners

    ctx = engine.context()
    if ctx is None:
        raise ValueError("Both workers must be online on this network to move a session.")
    device = runners.resolve_runner_id(target)
    member = ctx.member(device)
    if not member or member.kind != "worker" or member.status != "active":
        raise ValueError("The destination must be an active worker on this network.")
    payload = {"session_id": session["id"], "phase": "receive"}
    if device == ctx.device_id:
        result = await handle(payload, ctx.device_id)
    else:
        # The destination may spend 600s publishing and 180s confirming with
        # the owner. Never retry a timed-out mutation or fall back to a DB pin.
        result = await control.call(device, "handoff", payload, timeout=900)
    if not isinstance(result.get("moved"), bool):
        raise ValueError("The destination did not confirm the session transfer.")
    return result["moved"]


async def receive(session):
    """Publish the owner branch, fetch locally, then sync context and ownership.

    Project-less sessions skip the Git steps and transfer history only.
    """
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
    held = {}
    try:
        return await _pull(session, owner, ctx, project, base, remote, held, HOLD + str(uuid.uuid4()))
    finally:
        if held:
            await run_sync(_release, held)
            _wake_queued()


def _receiving_path(base, branch):
    try:
        dest = checkout_manager._branch_checkout_path(base, branch)
    except WorktreeError as e:
        raise ValueError(str(e)) from e
    return dest, checkout_transfer.receiving_checkout(base, dest, branch)


def _reserve_mirror(sid, holder, held):
    """Reserve this worker's mirror of the moving session for the import.

    Placed only after the owner has begun, because a database shared with the
    owner already holds the owner's handoff lease, which serves the same role.
    """
    if sid in held:
        return
    run = runs.claim_run(sid, holder, TTL)
    if run:
        held[sid] = run
        return
    active = runs.get_active_run(sid)
    if not active or not str(active.get("runner_id") or "").startswith(HANDOFF):
        raise ValueError("Stop this session's active run before moving it.")


async def _pull(session, owner, ctx, project, base, remote, held, holder):
    sid = session["id"]
    expected_branch = project_resolver._effective_branch(session) if project else None
    if expected_branch:
        # Refuse a busy receiving checkout before the owner commits or pushes.
        _, path = await run_sync(_receiving_path, base, expected_branch)
        await run_sync(_hold_checkout, session, project, str(path), expected_branch, holder, held)
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
        if not state or (expected_branch and state["branch"] != expected_branch):
            raise ValueError("The owning worker's branch changed; refresh before resuming.")
        dest, path = await run_sync(_receiving_path, base, state["branch"])
        # Exclude other sessions working in the checkout this import changes.
        await run_sync(_reserve_mirror, sid, holder, held)
        await run_sync(_hold_checkout, session, project, str(path), state["branch"], holder, held)
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
