"""Online, leased checkout and transcript handoff between authenticated workers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
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
    "pending_interactions": ("id", "kind", "payload", "state", "answer", "created_at", "answered_at"),
    "turns": ("id", "source", "prompt", "payload", "command_id", "position", "state", "created_at"),
}
JSON_COLUMNS = {"tool_input", "metadata", "message", "event", "entry", "payload", "answer"}
APP_TABLES = {"session_messages", "session_events", "pending_interactions", "turns"}
# Only settled model receipts move. Live permission/question/access prompts have
# no move behaviour and stay on the source worker; never replace those here.
_RECEIPTS_SQL = " AND kind IN ('model_switch_approval', 'model_switch_named') AND state='answered'"
_LAUNCH_SQL = (" AND command_id LIKE 'orchestration:start:%%' AND source='agent_message' "
               "AND state NOT IN ('queued','running','interrupting','draining')")
_TRANSFER_FILTERS = {"pending_interactions": _RECEIPTS_SQL, "turns": _LAUNCH_SQL}


def _aad(sid, target):
    return ("shuffle-session-handoff-v1:" + sid + ":" + target).encode()


def _has_live_tasks(cursor, sid):
    """Whether ``sid`` has background work that is still running on this worker.

    A durable running row is live while something still runs it: a Task child
    on another worker reports back to it, and local work is in this process's
    task registry. A local row the registry does not know was stranded by a
    missed settle (#1758); it must not pin the session to this worker.
    """
    from app.core.task_registry import task_registry

    if task_registry.has_active_background_work(sid):
        return True
    cursor.execute("SELECT task_id, remote_device_id FROM background_tasks "
                   "WHERE session_id=%s AND status IN ('running','pending')", (sid,))
    for row in cursor.fetchall():
        if row["remote_device_id"]:
            return True
        record = task_registry.get_task(sid, row["task_id"])
        if record is not None and record.get("status") in ("running", "pending"):
            return True
    return False


OPEN_PROMPT = "Answer or dismiss the open prompt first."


def open_prompt(cursor, sid):
    """Whether ``sid`` shows the owner an open approval, question or access card.

    Those cards live only on this worker and do not move (#1819), so a move
    with one open would leave the agent waiting for an answer nobody can give.
    """
    cursor.execute("SELECT 1 FROM pending_interactions WHERE session_id=%s AND state='pending' "
                   "AND (expires_at IS NULL OR expires_at >= NOW()) LIMIT 1", (sid,))
    if cursor.fetchone():
        return True
    from app.core.chat_access import broker

    return broker.pending_count(sid) > 0


def _check_unfinished_wakes(cursor, sid):
    ctx = engine.context()
    cursor.execute("SELECT 1 FROM handoff_wakes WHERE session_id=%s AND finalized=FALSE "
                   "AND source_id<>%s LIMIT 1", (sid, ctx.device_id if ctx else ""))
    if cursor.fetchone():
        raise ValueError("Wait for this chat's previous move to finish delivering messages before moving it again. "
                         "If the source is unavailable, open Settings, Devices, This worker, Network to abandon it.")


def check_unfinished_wakes(sid):
    with get_db() as conn:
        _check_unfinished_wakes(conn.cursor(), sid)


def _idle(cursor, sid):
    _check_unfinished_wakes(cursor, sid)
    if _has_live_tasks(cursor, sid):
        raise ValueError("Wait for this session's background work to finish before resuming here.")
    cursor.execute("SELECT 1 FROM turns WHERE session_id=%s AND state IN ('queued','running','interrupting','draining') LIMIT 1", (sid,))
    if cursor.fetchone():
        raise ValueError("Wait for this session's queued or active turn to finish before resuming here.")
    if open_prompt(cursor, sid):
        raise ValueError(OPEN_PROMPT)


# Pending wake-ups (ScheduleWakeup, usage-limit resumes, held peer messages)
# move with the chat (#1819): ``begin`` sends them, ``_commit`` deletes them
# here and the receiver re-creates and arms them once it owns the session. A
# receiver whose capabilities lack ``move_wakes`` ignores them, so the chat is
# told which ones will not run (``uncarried_wakes``). ``move_wakes`` version 1
# also means the receiver drops a repeated ``command_id`` on a forwarded turn
# and accepts the ``note`` operation of ``session-move`` (session_handoff).
# Version 2 also takes the turn that resumes a chat the agent handed off
# (``resume_carry``, #2012) and drops it while the chat is stopped there.
WAKE_COLUMNS = ("id", "prompt", "fire_at", "profile_id", "project_id", "reason", "model", "source")


def _session_wakes(sid):
    from app.db import wakeups

    return [{column: row.get(column) for column in WAKE_COLUMNS}
            for row in wakeups.get_session_wakeups(sid)]


def wake_carry(target):
    """True when ``target`` keeps moved wakes, False when it cannot, None when unknown."""
    from app.network import runners

    capabilities = (runners.find(target) or {}).get("capabilities") or {}
    if not capabilities:
        return None
    return int(capabilities.get("move_wakes") or 0) >= 1


def carries_wakes(target):
    return wake_carry(target) is True


def resume_carry(target):
    """Whether ``target`` takes an agent hand-off's resume turn (``move_wakes`` 2+, #2012).

    True when it does, False when it runs an older version, None when its
    capabilities are not known yet. A version 2 receiver drops a resume turn
    while the chat is stopped there; only then may the old owner send one.
    """
    from app.network import runners

    capabilities = (runners.find(target) or {}).get("capabilities") or {}
    if not capabilities:
        return None
    return int(capabilities.get("move_wakes") or 0) >= 2


def wake_label(row):
    """Plain words for one wake: what it does and when, for the chat."""
    from datetime import datetime, timezone
    from app.core import wakeup_service

    when = datetime.fromtimestamp(float(row.get("fire_at") or 0), timezone.utc).strftime("%H:%M UTC")
    source = row.get("source")
    if source == wakeup_service.LIMIT_RESUME_SOURCE:
        return f"the resume after the usage limit at {when}"
    if source == wakeup_service.HELD_PEER_SOURCE:
        return f"a held message from another chat due at {when}"
    return f"the scheduled wake-up at {when}"


def uncarried_wakes(sid, target):
    """The wakes a move of ``sid`` to ``target`` may drop, and whether that is certain.

    ``([], True)`` when the receiver keeps them; ``(wakes, False)`` when its
    capabilities are not known yet, so it may or may not.
    """
    carry = wake_carry(target)
    if carry:
        return [], True
    return _session_wakes(sid), carry is False


def _moving_wakes(data):
    """Commit field naming the wakes this receiver installs (begin's snapshot)."""
    if "wakes" not in data:
        return {}
    return {"wake_ids": [str(w.get("id")) for w in data.get("wakes") or [] if w.get("id")]}


def install_wakes(sid, wakes):
    """Receiver: re-create the moved wakes here. Returns the rows to arm."""
    from app.db import wakeups

    rows = []
    for wake in wakes or []:
        if not isinstance(wake, dict) or not wake.get("id"):
            continue
        try:
            rows.append(wakeups.create_wakeup(
                id=str(wake["id"]), session_id=sid, prompt=str(wake.get("prompt") or ""),
                fire_at=float(wake.get("fire_at") or 0), profile_id=wake.get("profile_id"),
                project_id=wake.get("project_id"), reason=wake.get("reason"), model=wake.get("model"),
                source=str(wake.get("source") or "schedule_wakeup"),
                instance_id=settings.effective_instance_id))
        except Exception:  # noqa: BLE001 — already here (a shared database); keep the rest
            logger.warning("Could not install moved wake %s for %s", wake.get("id"), sid, exc_info=True)
    return rows


def _stage_wakes(sid, owner, data, branch):
    """Persist dormant wakes before commit; boot rehydration cannot see them."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO handoff_wakes (token,session_id,source_id,branch,wakes,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (token) DO NOTHING",
                    (data["token"], sid, owner, branch, json.dumps(data.get("wakes") or []), time.time()))


def note_source_contact(device_id):
    """A successful contact resets recovery even while Devices is closed."""
    if engine.context() is None:
        return
    now = time.time()
    with get_db() as conn:
        conn.cursor().execute("UPDATE handoff_wakes SET offline_since=CASE "
                              "WHEN offline_since IS NULL OR offline_since<%s THEN %s ELSE offline_since END "
                              "WHERE source_id=%s AND finalized=FALSE", (now, now, device_id))


# No timeout runs a possibly superseded wake. After this observed offline
# interval the owner may explicitly discard or resume the remaining wakes.
ABANDON_AFTER_S = 300


def transfer_recovery_status():
    from app.network import membership, transport

    ctx = engine.context()
    if ctx is None:
        return {"pending": [], "history": []}
    online = transport.online_devices()
    now = time.time()
    pending, history = [], []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT h.*,s.title,s.owner_device_id FROM handoff_wakes h "
                    "JOIN sessions s ON s.id=h.session_id WHERE h.source_id<>%s "
                    "AND (h.finalized=FALSE OR h.recovery_reason IS NOT NULL) ORDER BY h.created_at,h.token",
                    (ctx.device_id,))
        for raw in cur.fetchall():
            row = dict(raw)
            if row["finalized"]:
                history.append({"session_id": row["session_id"], "title": row["title"],
                                "reason": row["recovery_reason"]})
                continue
            if row["owner_device_id"] not in (ctx.device_id, row["source_id"]):
                continue
            member = ctx.member(row["source_id"])
            revoked = member is not None and member.status == membership.STATUS_REVOKED
            source_online = row["source_id"] in online
            # Decide against the current locked row, not the SELECT snapshot:
            # neither a stale poll nor an older contact may rewind the clock.
            cur.execute("UPDATE handoff_wakes SET offline_since=CASE "
                        "WHEN offline_since IS NULL OR (%s AND offline_since<%s) "
                        "THEN %s ELSE offline_since END WHERE token=%s AND finalized=FALSE "
                        "RETURNING offline_since", (source_online, now, now, row["token"]))
            current = cur.fetchone()
            if current is None:
                continue  # Finalized after the status snapshot.
            since = current["offline_since"]
            can_abandon = revoked or (not source_online and now - since >= ABANDON_AFTER_S)
            pending.append({"token": row["token"], "session_id": row["session_id"], "title": row["title"],
                            "source": member.name if member else "Unavailable worker",
                            "can_abandon": can_abandon,
                            "reason": "source_revoked" if revoked else "delivering" if source_online else "source_offline"})
    return {"pending": pending, "history": history[-20:]}


def abandon_transfer(token, *, release_wakes=False):
    status = transfer_recovery_status()
    row = next((row for row in status["pending"] if row["token"] == token), None)
    if row is None:
        raise ValueError("This transfer is no longer waiting for recovery.")
    if not row["can_abandon"]:
        raise ValueError("Wait until the source has been offline for five minutes before abandoning this transfer.")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT source_id FROM handoff_wakes WHERE token=%s", (token,))
        source = cur.fetchone()["source_id"]
    reason = "owner_resumed_wakes" if release_wakes else "owner_discarded_wakes"
    rows = _finalize_wakes(row["session_id"], source, {"token": token, "release": True},
                           recovery_reason=reason, discard_all=not release_wakes)
    return row["session_id"], rows


def recover_revoked_transfers():
    """Revocation is explicit owner authority, but never authority to run old wakes."""
    from app.network import membership

    ctx = engine.context()
    if ctx is None:
        return
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM handoff_wakes WHERE finalized=FALSE AND source_id<>%s", (ctx.device_id,))
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        source = ctx.member(row["source_id"])
        if source is None or source.status != membership.STATUS_REVOKED:
            continue
        session = database.get_session(row["session_id"])
        if session and session.get("owner_device_id") in (ctx.device_id, row["source_id"]):
            _finalize_wakes(row["session_id"], row["source_id"], {"token": row["token"], "release": True},
                            recovery_reason="source_revoked", discard_all=True)
        else:
            # A historical reservation must never undo a later ownership change.
            with get_db() as conn:
                conn.cursor().execute("UPDATE handoff_wakes SET finalized=TRUE,recovery_reason='source_revoked' "
                                      "WHERE token=%s", (row["token"],))


def _discard_staged_wakes(sid, source, token):
    # Only a confirmed source cancellation retires a reservation. A lost
    # commit/cancel reply remains recoverable, never guessed to be cancelled.
    with get_db() as conn:
        conn.cursor().execute("UPDATE handoff_wakes SET finalized=TRUE,wakes='[]' "
                              "WHERE token=%s AND session_id=%s AND source_id=%s AND finalized=FALSE",
                              (token, sid, source))


def _finalize_wakes(sid, source, payload, *, recovery_reason=None, discard_all=False):
    """Source has stopped holding: atomically cancel or publish the staged wakes.

    The receipt survives retries, including a lost commit/finalize reply. A
    repeated finalize never recreates a wake already fired or superseded.
    """
    from app.db.base import parse_jsonb

    token = str(payload.get("token") or "")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM handoff_wakes WHERE token=%s AND session_id=%s "
                    "AND source_id=%s FOR UPDATE", (token, sid, source))
        row = cur.fetchone()
        if not row:
            raise ValueError("Unknown moved wake reservation")
        wakes = parse_jsonb(row["wakes"])
        device = engine.require_context().device_id
        cur.execute("SELECT owner_device_id FROM sessions WHERE id=%s FOR UPDATE", (sid,))
        session = cur.fetchone()
        owner = session["owner_device_id"] if session else None
        if row["finalized"] and owner != device:
            return []  # A late receipt after a later move must not re-arm here.
        if not session or owner not in (source, device):
            raise ValueError("This wake reservation belongs to an earlier move; ownership was not changed.")
        if not row["finalized"]:
            if recovery_reason in ("owner_discarded_wakes", "owner_resumed_wakes"):
                from app.network import membership, transport
                member = engine.require_context().member(source)
                revoked = member is not None and member.status == membership.STATUS_REVOKED
                since = row["offline_since"]
                # A contact may arrive after the status snapshot but before
                # this locked decision. Never use the stale eligibility bit.
                if not revoked and (source in transport.online_devices() or since is None
                                    or time.time() - since < ABANDON_AFTER_S):
                    raise ValueError("Wait until the source has been offline for five minutes before abandoning this transfer.")
            # This authenticated message from the source confirms its commit,
            # even when the earlier commit response never reached this worker.
            _accept_owner_row(cur, sid, device, row["branch"])
            # This authenticated committed transfer also supersedes older
            # abandoned imports whose cancellation reply never arrived.
            cur.execute("UPDATE handoff_wakes SET finalized=TRUE,wakes='[]' "
                        "WHERE session_id=%s AND token<>%s AND finalized=FALSE", (sid, token))
            superseded = row["superseded"] or payload.get("superseded") is True
            if payload.get("release") is not True:
                return []  # Confirm ownership, but keep wakes dormant during delivery.
            from app.core.wakeup_service import HELD_PEER_SOURCE

            for wake in wakes:
                # A held peer row is the only copy of those messages. It is
                # released after the human messages, not cancelled with a timer.
                if discard_all:
                    cur.execute("DELETE FROM pending_wakeups WHERE id=%s AND session_id=%s", (wake["id"], sid))
                elif not superseded or (row["keep_peer"] and wake.get("source") == HELD_PEER_SOURCE):
                    cur.execute("INSERT INTO pending_wakeups "
                                "(id,session_id,profile_id,project_id,prompt,reason,model,source,instance_id,fire_at) "
                                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                                (wake["id"], sid, wake.get("profile_id"), wake.get("project_id"),
                                 wake.get("prompt") or "", wake.get("reason"), wake.get("model"),
                                 wake.get("source") or "schedule_wakeup", settings.effective_instance_id,
                                 float(wake.get("fire_at") or 0)))
            cur.execute("UPDATE handoff_wakes SET finalized=TRUE,recovery_reason=%s WHERE token=%s",
                        (recovery_reason, token))
        # Return only surviving rows, including on retries. Re-arming a pending
        # row is safe; after a crash boot also re-arms these published rows.
        rows = []
        for wake in wakes:
            cur.execute("SELECT * FROM pending_wakeups WHERE id=%s", (wake["id"],))
            pending = cur.fetchone()
            if pending:
                rows.append(dict(pending))
    return rows


async def _arm_moved_wakes(sid, wakes):
    from app.core import wakeup_service

    for row in await run_sync(install_wakes, sid, wakes):
        wakeup_service.arm_persisted(row)


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
    cursor.execute("SELECT 1 FROM turns WHERE session_id=%s AND state IN ('running','interrupting','draining') LIMIT 1", (sid,))
    if cursor.fetchone():
        raise ValueError(BUSY_CHECKOUT)
    if _has_live_tasks(cursor, sid):
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
            receipt_filter = _TRANSFER_FILTERS.get(table, "")
            cur.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE session_id=%s" + receipt_filter + order, (sid,))
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


def begin(sid, target, remote=None, note=None, wake_barrier=False):
    """Owner: export the chat for ``target``. ``note`` runs once every check passed,
    before the history is read, so what it records travels with the chat."""
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
        if note is not None:
            note()
        from app.db import session_moves

        wakes = _session_wakes(sid)
        queued = session_moves.get(sid)
        if queued and wakes and not wake_barrier:
            raise ValueError("Update both workers before moving a chat with pending wake-ups.")
        barrier = bool(queued and wakes and wake_barrier)
        if barrier:
            session_moves.set_wake_token(sid, run["id"])
        data = dict(checkout=state, history=_history(session), sdk_session_id=session.get("sdk_session_id"),
                    project_id=project["id"] if project else None, token=run["id"],
                    wakes=wakes, wake_barrier=barrier)
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
        moving = payload.get("wake_ids")
        if moving is not None:
            # The receiver installs only the wakes begin sent. One created or
            # replaced since would be deleted here without running anywhere.
            cur.execute("SELECT id FROM pending_wakeups WHERE session_id=%s", (sid,))
            if {row["id"] for row in cur.fetchall()} != set(moving):
                raise ValueError("A wake-up of this chat changed during the move; ownership was not moved. "
                                 "Try again.")
        cur.execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s, placement=%s, branch=%s, updated_at=NOW() WHERE id=%s",
                    (target, target, json.dumps({"where": "runner:" + target}), session_branch, sid))
        if session_branch is None:
            cur.execute("UPDATE sessions SET worktree_id=NULL, worktree_branch=NULL, worktree_path=NULL WHERE id=%s", (sid,))
        cur.execute("SELECT * FROM sessions WHERE id=%s", (sid,))
        engine.record(cur, "sessions", sid, dict(cur.fetchone()), force=True)
        cur.execute("UPDATE runs SET state='transferred' WHERE id=%s", (token,))
        _release_holds(cur, HOLD + token)
        # The receiver re-creates them (``wakes`` in begin's data); none may
        # also fire here. A receiver that names them gets exactly those
        # deleted; an older one sends no list and keeps the old behaviour.
        if moving is None:
            cur.execute("DELETE FROM pending_wakeups WHERE session_id=%s RETURNING id", (sid,))
            moved_wakes = [row["id"] for row in cur.fetchall()]
        else:
            moved_wakes = list(dict.fromkeys(moving))
            if moved_wakes:
                cur.execute("DELETE FROM pending_wakeups WHERE session_id=%s AND id IN ("
                            + ", ".join(["%s"] * len(moved_wakes)) + ")", (sid, *moved_wakes))
    if moved_wakes:
        from app.core import wakeup_service

        wakeup_service._disarm(moved_wakes)
    engine.kick()
    return {"committed": True}


async def handle(payload, peer_device_id):
    sid = str(payload.get("session_id") or "")
    member = engine.require_context().member(peer_device_id)
    if not member or member.kind != "worker" or member.status != "active":
        raise PermissionError("A handoff requires an authenticated receiving worker.")
    if payload.get("phase") == "finalize-wakes":
        rows = await run_sync(_finalize_wakes, sid, peer_device_id, payload)
        from app.core import wakeup_service

        for row in rows:
            wakeup_service.arm_persisted(row)
        return {"finalized": True} if payload.get("release") is True else {"accepted": True}
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
        loop = asyncio.get_running_loop()

        def note():
            # The wakes note is written only once begin's checks passed (a
            # refused move keeps every wake here) and never blocks the move.
            try:
                asyncio.run_coroutine_threadsafe(_note_uncarried_wakes(sid, peer_device_id), loop).result(30)
            except Exception:  # noqa: BLE001
                logger.warning("Could not note the wakes of %s before a move", sid, exc_info=True)
        try:
            return await run_sync(begin, sid, peer_device_id, payload.get("remote"), note,
                                  payload.get("wake_barrier") is True)
        except BaseException:
            _wake_queued()
            raise
    if payload.get("phase") in ("commit", "cancel"):
        try:
            return await run_sync(finish, sid, peer_device_id, payload)
        finally:
            _wake_queued()
    raise ValueError("Unknown handoff phase")


# (session, wake id, target) already named in the chat: a retried move does
# not repeat the note.
_noted_wakes: set = set()


async def _note_uncarried_wakes(sid, target):
    """Before an older receiver takes the chat: say which wakes will not run there.

    Recorded before ``begin`` reads the history, so the note travels with it.
    """
    try:
        wakes, certain = await run_sync(uncarried_wakes, sid, target)
    except Exception:  # noqa: BLE001 — the note must never block the move
        logger.warning("Could not check the wakes of %s before a move", sid, exc_info=True)
        return
    from app.core import wakeup_service
    from app.core.turn_recorder import record_drained_event
    from app.network import runners

    label = str((runners.find(target) or {}).get("device_name") or "the other worker")
    for wake in wakes:
        key = (sid, str(wake.get("id")), target)
        if key in _noted_wakes:
            continue
        _noted_wakes.add(key)
        if certain:
            message = (f"Moving to {label}, which runs an older version that cannot keep "
                       f"{wake_label(wake)}. It will not run. Send a message there to continue.")
        else:
            message = (f"Moving to {label}. This worker cannot tell whether {label} can keep "
                       f"{wake_label(wake)}; if it runs an older version, it will not run. "
                       "Send a message there to continue.")
        await record_drained_event(sid, {"type": "system", "subtype": wakeup_service.DROPPED_NOTE_SUBTYPE,
                                         "data": {"message": message, "reason": "moved",
                                                  "source": wake.get("source"), "wakeup_id": wake.get("id")}})


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
            receipt_filter = _TRANSFER_FILTERS.get(table, "")
            cur.execute(f"DELETE FROM {table} WHERE session_id=%s" + receipt_filter, (sid,))
        for table, columns in TABLES.items():
            destination_id = sid if table in APP_TABLES else sdk
            for row in data["history"].get(table, []):
                if table == "pending_interactions":
                    from app.core.model_override import RECEIPT_KINDS
                    if row.get("kind") not in RECEIPT_KINDS or row.get("state") != "answered":
                        continue
                if table == "turns" and (
                        row.get("source") != "agent_message"
                        or not str(row.get("command_id") or "").startswith("orchestration:start:")):
                    continue
                values = []
                for column in columns:
                    value = row.get(column)
                    if table == "turns" and column == "state":
                        value = "completed"  # An ancestry marker is never executable work.
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


def _accept_owner_row(cur, sid, device_id, branch):
    # instance_id moves too (as adopt/unpin do): left on the previous
    # worker's id, the session listed as remote with an unknown state here.
    cur.execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s, placement=%s, branch=%s, updated_at=NOW() WHERE id=%s",
                (device_id, settings.effective_instance_id, json.dumps({"where": "runner:" + device_id}), branch, sid))
    if branch is None:
        cur.execute("UPDATE sessions SET worktree_id=NULL, worktree_branch=NULL, worktree_path=NULL WHERE id=%s", (sid,))


def _accept_owner(sid, device_id, branch=None):
    with get_db() as conn:
        _accept_owner_row(conn.cursor(), sid, device_id, branch)


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
    reply = await control.call(owner, "handoff", {"session_id": sid, "phase": "begin", "remote": remote, "wake_barrier": True}, timeout=600)
    blob = reply.get("payload", "")
    raw = crypto.decrypt_payload(ctx.keyring[int(reply["key_version"])], blob, _aad(sid, ctx.device_id))
    data = json.loads(raw)
    token = data["token"]
    committed = False
    try:
        if data.get("wakes") and "wake_barrier" not in data:
            raise ValueError("Update both workers before moving a chat with pending wake-ups.")
        state = data["checkout"]
        if data["project_id"] != (project["id"] if project else None):
            raise ValueError("The owning worker's project changed; refresh before resuming.")
        if not project:
            # Plain chat: no checkout to move, only the conversation history.
            if state is not None:
                raise ValueError("Unexpected checkout for a project-less session.")
            cwd = str(settings.effective_workspace_dir)
            await run_sync(install_history, sid, data, cwd)
            if data.get("wake_barrier"):
                await run_sync(_stage_wakes, sid, owner, data, None)
            result = await control.call(owner, "handoff", {"session_id": sid,
                "phase": "commit", "token": token,
                "fingerprint": None, "branch": None, "session_branch": None,
                **_moving_wakes(data),
            }, timeout=180)
            if not result.get("committed"):
                raise ValueError("The owning worker did not confirm the transfer.")
            committed = True
            await run_sync(_accept_owner, sid, ctx.device_id, None)
            if not data.get("wake_barrier"):
                await _arm_moved_wakes(sid, data.get("wakes"))
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
        if data.get("wake_barrier"):
            await run_sync(_stage_wakes, sid, owner, data, session_branch)
        result = await control.call(owner, "handoff", {"session_id": sid,
            "phase": "commit", "token": token,
            "fingerprint": state["fingerprint"],
            "branch": state["branch"],
            "session_branch": session_branch,
            **_moving_wakes(data),
        }, timeout=180)
        if not result.get("committed"):
            raise ValueError("The owning worker did not confirm the transfer.")
        committed = True
        await run_sync(_accept_owner, sid, ctx.device_id, session_branch)
        if not data.get("wake_barrier"):
            await _arm_moved_wakes(sid, data.get("wakes"))
        return cwd
    finally:
        if not committed:
            # Best effort. A lost response/worker restart leaves a bounded lease,
            # not a permanent block; never force ownership after a timeout.
            try:
                cancelled = await control.call(owner, "handoff", {"session_id": sid,"phase": "cancel", "token": token})
                if cancelled.get("cancelled") and data.get("wake_barrier"):
                    await run_sync(_discard_staged_wakes, sid, owner, token)
            except (control.RunnerUnreachable, control.ControlRefused, ValueError):
                pass
