"""Online, leased checkout and transcript handoff between authenticated workers."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from pathlib import Path
from time import monotonic
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
# Only settled model receipts and relay-free question cards move. A question
# answer queues a fresh turn; permission callbacks and private grants stay local.
_RECEIPTS_SQL = (" AND ((kind IN ('model_switch_approval', 'model_switch_named') AND state='answered')"
                 " OR (kind='question' AND state='pending' AND payload->'nonblocking'='true'))")
_LAUNCH_SQL = (" AND command_id LIKE 'orchestration:start:%%' AND source='agent_message' "
               "AND state NOT IN ('queued','running','interrupting','draining')")
_TRANSFER_FILTERS = {"pending_interactions": _RECEIPTS_SQL, "turns": _LAUNCH_SQL}


def _aad(sid, target):
    return ("shuffle-session-handoff-v1:" + sid + ":" + target).encode()


_progress_loop = contextvars.ContextVar("handoff_progress_loop", default=None)
_PROGRESS_LOG_S = 60.0
_progress_state = contextvars.ContextVar("handoff_progress_state", default=None)
_import_timing = contextvars.ContextVar("handoff_import_timing", default=None)
_operation_progress = {}
_remote_progress = {}


def progress(sid):
    """Latest receiver progress, for the owner's session-move status card."""
    return _remote_progress.get(sid)


class _Timing:
    """Step timings, including the current step while a move is still pending.

    The export runs in a thread; its copied control-request context supplies
    the event loop. Timer callbacks are diagnostics only, not background work
    that could itself hold the session busy. No extra threads or task launches.
    """

    def __init__(self, role, sid, *, loop=None):
        self.role, self.sid = role, sid
        self.start = self.last = monotonic()
        self.steps = []
        self.current = ("admission", self.start, None)
        self.finished = False
        self.loop = loop or _progress_loop.get()
        self.progress_handle = None
        state = _progress_state.get()
        if state is not None:
            state["timing"] = self
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._arm_progress)

    def _arm_progress(self):
        if not self.finished:
            self.progress_handle = self.loop.call_later(_PROGRESS_LOG_S, self._log_progress)

    def _log_progress(self):
        if self.finished:
            return
        name, since, _ = self.current
        now = monotonic()
        logger.info("Hand-off %s for session %s waiting on %s for %.1fs (total %.1fs): %s",
                    self.role, self.sid, name, now - since, now - self.start,
                    ", ".join(self.steps) or "no completed steps")
        self._arm_progress()

    def snapshot(self):
        name, since, rows = self.current
        now = monotonic()
        return {"step": name, "elapsed_seconds": round(now - self.start, 1),
                "step_elapsed_seconds": round(now - since, 1),
                **({"rows_completed": rows[0], "rows_total": rows[1]} if rows is not None else {})}

    def waiting(self, name):
        self.current = (name, monotonic(), None)

    def rows(self, completed, total):
        # One immutable snapshot crosses the import-thread/status-poll boundary.
        # Counts describe this step only, and advance after a chunk commits.
        name, since, _ = self.current
        self.current = (name, since, (completed, total))

    def step(self, name, **facts):
        now = monotonic()
        detail = "".join(f" {key}={value}" for key, value in facts.items())
        self.steps.append(f"{name}={now - self.last:.1f}s{detail}")
        self.last = now
        self.current = ("next step", now, None)

    def log(self, outcome, *, quiet=False):
        self.finished = True
        if self.progress_handle is not None:
            self.loop.call_soon_threadsafe(self.progress_handle.cancel)
        if quiet:
            return
        logger.info("Hand-off %s for session %s %s after %.1fs: %s", self.role, self.sid, outcome,
                    monotonic() - self.start, ", ".join(self.steps) or "no steps")


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


def open_prompt(cursor, sid, *, allow_questions=False):
    """A local-only prompt. Queue admission may also allow blocking questions;
    the transfer itself allows only durable questions with no live callback.
    """
    cursor.execute("SELECT kind, payload FROM pending_interactions WHERE session_id=%s AND state='pending' "
                   "AND (expires_at IS NULL OR expires_at >= NOW())", (sid,))
    from app.db.base import parse_jsonb

    for row in cursor.fetchall():
        payload = parse_jsonb(row["payload"]) or {}
        if (row["kind"] == "question" and payload.get("questions")
                and (allow_questions or payload.get("nonblocking") is True)):
            continue
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


def question_carry(target):
    """True for a question-capable receiver, False for older, None until known."""
    from app.network import runners

    capabilities = (runners.find(target) or {}).get("capabilities") or {}
    if not capabilities:
        return None
    return int(capabilities.get("move_questions") or 0) >= 1


def plain_folder_carry(target: str) -> bool | None:
    """Whether ``target`` moves a project folder without Git (#639); None until known."""
    from app.network import runners

    capabilities = (runners.find(target) or {}).get("capabilities") or {}
    if not capabilities:
        return None
    return int(capabilities.get("handoff_plain_folder") or 0) >= 1


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


def target_resume(target):
    """Receiver owns the durable, idempotent agent continuation after commit."""
    from app.network import runners

    return ((runners.find(target) or {}).get("capabilities") or {}).get("handoff_target_resume") == 1


def _resume_wake(sid, queued, target):
    if not queued or queued.get("requested_by") != "agent" or not target_resume(target):
        return None
    from app.core import session_handoff

    pending = session_handoff._moving.get(sid)
    if (pending is None or pending.by != "agent" or pending.target_id != target
            or queued.get("target_id") != target or pending.handoff_id != queued.get("handoff_id")):
        return None
    if session_handoff.resume_plan(sid, pending, target) != "send":
        return None
    resume = session_handoff._resume_turn(sid, pending, pending.label)
    return {"id": "handoff-resume-" + pending.handoff_id,
            "prompt": resume["prompt"], "fire_at": time.time(),
            "profile_id": resume["profile_id"], "project_id": resume["project_id"],
            "source": "handoff_resume"}


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
    return {"wake_ids": [str(w.get("id")) for w in data.get("wakes") or []
                         if w.get("id") and w.get("id") != data.get("resume_wake_id")]}


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
        # Superseding old imports also retires their wakes. Lock this session's
        # wake rows in one order before the session row, including on retries.
        cur.execute("SELECT * FROM handoff_wakes WHERE session_id=%s ORDER BY token FOR UPDATE", (sid,))
        row = next((r for r in cur.fetchall() if r["token"] == token and r["source_id"] == source), None)
        if not row:
            raise ValueError("Unknown moved wake reservation")
        wakes = parse_jsonb(row["wakes"])
        device = engine.require_context().device_id
        _lock_history_headers(cur, sid)
        # Already-published wake release is short and still shares the owner
        # lock with admission. Only cold publication needs an optimistic read.
        owner_lock = " FOR UPDATE" if row["finalized"] else ""
        cur.execute("SELECT owner_device_id,placement FROM sessions WHERE id=%s" + owner_lock, (sid,))
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
            metadata = _publish_history(cur, sid, token)
            _lock_published_owner(cur, sid, device, token, session, metadata)
            _accept_owner_row(cur, sid, device, row["branch"], token)
            # This authenticated committed transfer also supersedes older
            # abandoned imports whose cancellation reply never arrived.
            cur.execute("UPDATE handoff_wakes SET finalized=TRUE,wakes='[]' "
                        "WHERE session_id=%s AND token<>%s AND finalized=FALSE", (sid, token))
            superseded = row["superseded"] or payload.get("superseded") is True
            if superseded:
                cur.execute("UPDATE handoff_wakes SET superseded=TRUE WHERE token=%s", (token,))
            if payload.get("release") is not True:
                return []  # Confirm ownership, but keep wakes dormant during delivery.
            from app.core.wakeup_service import HELD_PEER_SOURCE

            for wake in wakes:
                # A held peer row is the only copy of those messages. It is
                # released after the human messages, not cancelled with a timer.
                if discard_all:
                    cur.execute("DELETE FROM pending_wakeups WHERE id=%s AND session_id=%s", (wake["id"], sid))
                elif (not superseded or (row["keep_peer"] and wake.get("source") == "handoff_resume")
                      or (row["keep_peer"] and wake.get("source") == HELD_PEER_SOURCE)):
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
# A project folder without Git moves its history only (#639). Both workers must
# see a plain folder; one Git checkout and one plain folder cannot be reconciled.
PLAIN_MISMATCH = ("This project's folder is a Git repository on one worker but not on the "
                  "other. Use the same kind of folder on both workers, then move again; "
                  "ownership has not moved.")
PLAIN_OWNER_UPDATE = ("Update the owning worker to move a session whose project folder is "
                      "not a Git repository; ownership has not moved.")
PLAIN_UNKNOWN = ("Cannot tell whether this project's folder is a Git repository; "
                 "ownership has not moved.")


def _plain_folder(path: str) -> bool:
    """True only when ``path`` is positively not in a Git repository (#639).

    Uses the Git toolbar's classifier: a failed probe (dubious ownership,
    unreadable or corrupt metadata, Git missing or timing out) never reads as
    plain, so it cannot authorize a history-only move. Raises ``ValueError``.
    """
    import subprocess

    from app.api.git_owner import _is_plain_project_folder

    try:
        return bool(_is_plain_project_folder(path))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(PLAIN_UNKNOWN) from exc


def _known_plain(path: str) -> bool:
    """Like :func:`_plain_folder`, but a doubtful folder is simply not plain."""
    try:
        return _plain_folder(path)
    except ValueError:
        return False


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

    A long-used project has a thousand or more sessions, nearly all bound to
    other branch folders. Validating each one's checkout runs Git, and a move
    scans at least twice on each worker, so compare the registered folder
    first and validate only a session whose folder is this one (#2305).
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
        if target is None:
            continue
        if not row.get("worktree_id"):
            folder = project_resolver.existing_checkout_path(row)
            if not folder or Path(folder).resolve() != target:
                continue
        cwd = _session_cwd(row)
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
        logger.warning("Cannot verify checkout for handoff of session %s: %s", session["id"], exc)
        raise ValueError(f"{UNVERIFIED_CHECKOUT} Reason: {exc}") from exc
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
    from app.db.base import parse_jsonb

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rows FROM handoff_history_chunks WHERE session_id=%s "
                       "AND table_name='' AND batch_no=0", (sid,))
        if any((parse_jsonb(row["rows"]) or {}).get("commit_sent") for row in cursor.fetchall()):
            return True  # An uncertain commit outlives its ordinary run lease.
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


def begin(sid, target, remote=None, note=None, wake_barrier=False, plain: bool = False):
    """Owner: export the chat for ``target``. ``note`` travels with its history.

    ``plain`` is the receiver's request to move a project whose folder is not a
    Git repository: history only, after this worker confirms its own folder is
    not one either (#639).
    """
    timing = _Timing("export", sid)
    try:
        reply = _begin(sid, target, remote, note, wake_barrier, timing, plain)
    except BaseException:
        # Admission refusals are already logged by the control boundary. Still
        # cancel the progress timer, but don't add a second "failed: no steps".
        timing.log("failed", quiet=not timing.steps and timing.current[0] == "admission")
        raise
    timing.log("exported")
    return reply


def _begin(sid, target, remote, note, wake_barrier, timing, plain: bool = False):
    control._require_owned(sid)
    session = database.get_session(sid)
    project = project_resolver.resolve_project_v2(session)
    branch = project_resolver._effective_branch(session)
    # Project-less (plain chat) sessions and project folders without Git move
    # their history only.
    cwd = None
    plain = bool(project and plain)
    if plain:
        cwd = project_resolver.resolve_existing_cwd(session)
        if not cwd:
            raise ValueError("The owning worker no longer has this project's folder; "
                             "ownership has not moved.")
        if not _plain_folder(cwd):
            raise ValueError(PLAIN_MISMATCH)
        branch = None
    elif project:
        if not remote:
            raise ValueError("Update both workers to use Git-based resume.")
        cwd = project_resolver.resolve_existing_cwd(session)
        if not cwd:
            raise ValueError("The owning worker has no existing branch checkout to transfer.")
        if not branch:
            try:
                head = checkout_transfer.git(cwd, "symbolic-ref", "--short", "HEAD")
                branch = head.decode().strip()
            except ValueError:
                # The receiver has a Git checkout of a folder that has none here.
                if _known_plain(cwd):
                    raise ValueError(PLAIN_MISMATCH) from None
                raise
    with get_db() as conn:
        _idle(conn.cursor(), sid)
    from app.db import interactions

    if any(r["kind"] == "question" for r in interactions.get_pending_interactions(sid)):
        carry = question_carry(target)
        if carry is None:
            raise ValueError("Wait for the target worker to report its capabilities before moving an unanswered question.")
        if not carry:
            raise ValueError("Update the target worker before moving an unanswered question.")
    run = runs.claim_run(sid, HANDOFF + target, TTL)
    if not run:
        raise ValueError("The owning worker is busy. Wait for its turn to finish before resuming here.")
    held = {}
    try:
        if project and not plain:
            timing.waiting("checkout_guard")
            # Publication commits and pushes the shared checkout. Exclude other
            # sessions working there; they keep their files and work.
            _hold_checkout(session, project, cwd, branch, HOLD + run["id"], held)
            timing.step("checkout_guard", holds=len(held))
        timing.waiting("git_publish")
        git_backed = project and not plain
        state = checkout_transfer.export_checkout(cwd, branch, remote) if git_backed else None
        if git_backed:
            timing.step("git_publish")
        if note is not None:
            timing.waiting("wake_note")
            note()
        from app.db import session_moves

        timing.waiting("history")
        wakes = _session_wakes(sid)
        queued = session_moves.get(sid)
        resume_wake = _resume_wake(sid, queued, target) if wake_barrier else None
        if resume_wake:
            wakes.append(resume_wake)
        if queued and wakes and not wake_barrier:
            raise ValueError("Update both workers before moving a chat with pending wake-ups.")
        barrier = bool(queued and wakes and wake_barrier)
        if barrier:
            session_moves.set_wake_token(sid, run["id"])
        data = dict(checkout=state, history=_history(session), sdk_session_id=session.get("sdk_session_id"),
                    project_id=project["id"] if project else None, token=run["id"],
                    wakes=wakes, wake_barrier=barrier, handoff_receipt=1,
                    resume_wake_id=resume_wake["id"] if resume_wake else None,
                    **({"plain_folder": True} if plain else {}))
        raw = json.dumps(data, default=str).encode()
        timing.step("history", rows=sum(len(rows) for rows in data["history"].values()),
                    mb=f"{len(raw) / 1e6:.1f}")
        timing.waiting("encrypt")
        ctx = engine.require_context()
        reply = {"payload": crypto.encrypt_payload(ctx.network_key, raw, _aad(sid, target)), "key_version": ctx.key_version}
        timing.step("encrypt")
        if queued and queued.get("target_id") == target:
            session_moves.set_resume_exported(sid, queued["handoff_id"], target, bool(resume_wake))
        return reply
    except BaseException:
        timing.waiting("release_failed_export")
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
        from app.core import streaming_status

        streaming_status.notify(sid)
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


def _announce_owner(sid, target, session):
    # begin exported the transcript before commit, so a terminal source row
    # cannot travel in that snapshot. This local frame settles the source UI
    # even if the destination's outer receive reply is lost.
    from app.core.global_events import global_events

    owner = session.get("api_user_id")
    global_events.lifecycle(owner, {
        "type": "user_session_updated", "session_id": sid,
        "updates": {"owner_device_id": target, "instance_id": target,
                    "placement": {"where": "runner:" + target}},
    }, user_type="admin" if owner is None else "api_user")


def _commit(sid, target, payload, token):
    session = database.get_session(sid)
    if session.get("owner_device_id") == target:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM runs WHERE id=%s AND session_id=%s AND runner_id=%s AND state='transferred'",
                        (token, sid, HANDOFF + target))
            if cur.fetchone():
                _release_holds(cur, HOLD + token)
                _announce_owner(sid, target, session)
                from app.db.base import parse_jsonb
                receipt = (parse_jsonb(session.get("placement")) or {}).get("handoff") or {}
                held_messages = receipt.get("held_messages", 0)
                return {"committed": True, **({"held_messages": held_messages} if held_messages else {})}
    control._require_owned(sid)
    project = project_resolver.resolve_project_v2(session)
    if project and payload.get("plain_folder") is True:
        # History-only move of a project folder without Git (#639).
        cwd = project_resolver.resolve_existing_cwd(session)
        if (not cwd or not _plain_folder(cwd)
                or payload.get("fingerprint") is not None or payload.get("branch") is not None):
            raise ValueError("The owning worker's project folder changed during the move; "
                             "ownership was not moved.")
    elif project:
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
    from app.core import session_handoff

    with session_handoff.commit_holding(sid, target), get_db() as conn:
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
        from app.db.base import parse_jsonb

        cur.execute("SELECT held FROM session_moves WHERE session_id=%s AND target_id=%s FOR UPDATE", (sid, target))
        queued = cur.fetchone()
        held_messages = len(parse_jsonb(queued["held"]) or []) if queued else 0
        # Sync this receipt with the ownership flip. A restarted receiver must
        # know whether a lost commit reply was holding human messages back.
        placement = {"where": "runner:" + target,
                     "handoff": {"token": token, "held_messages": held_messages}}
        cur.execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s, placement=%s, branch=%s, updated_at=NOW() WHERE id=%s",
                    (target, target, json.dumps(placement), session_branch, sid))
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
    # The hand-off lease may have lit the source's busy indicator. Its SQL
    # transition bypasses TurnRecorder.release, so explicitly reconcile it.
    from app.core import streaming_status

    streaming_status.notify(sid)
    # record() feeds peers, not this worker's open clients. Announce the owner
    # change locally even when the outer receive reply times out or is lost.
    from app.core.global_events import global_events

    global_events.resources_changed("sessions")
    _announce_owner(sid, target, session)
    engine.kick()
    return {"committed": True, **({"held_messages": held_messages} if held_messages else {})}


# Long Git/history operations must not occupy a single HTTP response: tunnel
# proxies cut it off at 120s even when the caller's timeout is 900s. Results
# stay scoped to the authenticated requester and session; status never replays
# a mutation. Restart loses the receipt, not the protocol's durable leases.
_operations: dict[tuple[str, str, str], asyncio.Task] = {}
_OPERATION_LIMIT = 32
_POLL_WAIT_S = 10.0  # Long-poll below every proxy deadline, return as soon as ready.


async def _call(device, payload, *, timeout):
    from app.network import runners

    runner = await run_sync(runners.find, device) or {}
    deferred = (runner.get("capabilities") or {}).get("handoff_poll") == 1
    started = asyncio.get_running_loop().time()
    result = await control.call(device, "handoff", {**payload, **({"deferred": True} if deferred else {})}, timeout=timeout)
    # Older workers ignore the extra field and return the ordinary result.
    operation = result.get("operation")
    if not operation:
        return result
    loop = asyncio.get_running_loop()
    deadline = started + min(timeout, TTL)
    lease_deadline = started + TTL
    while operation:
        if loop.time() >= deadline:
            raise control.MaybeApplied("Hand-off is still unconfirmed. Check the owning worker before retrying.")
        try:
            result = await control.call(device, "handoff", {
                "session_id": payload["session_id"], "phase": "status", "operation": operation,
            })
        except control.RunnerUnreachable:
            # A status read is safe to retry; the original mutation never is.
            await asyncio.sleep(1)
            continue
        if result.get("error"):
            raise ValueError(result["error"])
        if not result.get("operation"):
            return result
        # A confirmed pending receipt is not an unconfirmed failure at 900s.
        # Never extend beyond the lease, even if a peer keeps saying pending.
        deadline = lease_deadline
        if result.get("progress") and payload.get("phase") == "receive":
            _remote_progress[payload["session_id"]] = result["progress"]
    return result


async def handle(payload, peer_device_id):
    sid = str(payload.get("session_id") or "")
    member = engine.require_context().member(peer_device_id)
    if not member or member.kind != "worker" or member.status != "active":
        raise PermissionError("A handoff requires an authenticated receiving worker.")
    if payload.get("phase") == "status":
        key = (peer_device_id, sid, str(payload.get("operation") or ""))
        task = _operations.get(key)
        if task is None:
            raise ValueError("Hand-off receipt expired or worker restarted. Check ownership before retrying.")
        if not task.done():
            # A cancelled HTTP waiter must not cancel the leased transfer.
            await asyncio.wait({task}, timeout=_POLL_WAIT_S)
        if not task.done():
            timing = _operation_progress.get(key, {}).get("timing")
            return {"operation": key[2], **({"progress": timing.snapshot()} if timing else {})}
        if task.cancelled():
            return {"error": "Hand-off was interrupted. Check ownership before retrying."}
        error = task.exception()
        return {"error": str(error)} if error is not None else task.result()
    if payload.get("deferred") is True and payload.get("phase") in ("receive", "begin"):
        if len(_operations) >= _OPERATION_LIMIT:
            raise ValueError("Too many hand-offs are pending. Try again later.")
        key = (peer_device_id, sid, str(uuid.uuid4()))
        state = {}
        context_token = _progress_state.set(state)
        try:
            task = asyncio.create_task(_handle(payload, peer_device_id))
        finally:
            _progress_state.reset(context_token)
        _operations[key] = task
        _operation_progress[key] = state
        loop = asyncio.get_running_loop()

        def completed(done):
            if not done.cancelled():
                done.exception()  # Retrieve errors even if the requester went away.
            loop.call_later(TTL, _operations.pop, key, None)
            loop.call_later(TTL, _operation_progress.pop, key, None)

        task.add_done_callback(completed)
        return {"operation": key[2]}
    return await _handle(payload, peer_device_id)


async def _handle(payload, peer_device_id):
    # Deferred failures travel in a 200 status receipt, bypassing peer_control's
    # HTTP-refusal logger. Log at execution, not on every replayed status read.
    token = _progress_loop.set(asyncio.get_running_loop())
    try:
        return await _handle_phase(payload, peer_device_id)
    except (ValueError, PermissionError, LookupError) as exc:
        if payload.get("deferred") is True or payload.get("phase") == "commit":
            logger.warning("Refused hand-off %s for session %s: %s",
                           payload.get("phase"), payload.get("session_id"), exc)
        raise
    except Exception:
        if payload.get("deferred") is True:
            logger.exception("Hand-off %s failed for session %s",
                             payload.get("phase"), payload.get("session_id"))
        raise
    finally:
        _progress_loop.reset(token)


async def _handle_phase(payload, peer_device_id):
    sid = str(payload.get("session_id") or "")
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
                                  payload.get("wake_barrier") is True,
                                  payload.get("plain_folder") is True)
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


def _history_conflict(table: str, columns: tuple[str, ...], deferred: bool = False) -> str:
    key = {"session_events": ("session_id", "seq"), "pending_interactions": ("id",),
           "turns": ("id",), "fluid_sessions": ("session_id",),
           "fluid_session_nodes": ("session_id", "node_id"),
           "fluid_session_events": ("session_id", "seq")}.get(table)
    if not key:
        return ""
    changed = [c for c in columns if c not in key and c != "session_id"]
    same_session = f"{table}.session_id=EXCLUDED.session_id AND "
    assignments = [f"{c}=EXCLUDED.{c}" for c in changed]
    if deferred:
        assignments.append("history_index_state=EXCLUDED.history_index_state")
    return ("ON CONFLICT (" + ", ".join(key) + ") DO UPDATE SET " +
            ", ".join(assignments) + " WHERE " + same_session + "(" +
            ", ".join(f"{table}.{c}" for c in changed) + ") IS DISTINCT FROM (" +
            ", ".join(f"EXCLUDED.{c}" for c in changed) + ")")


def _message_signature(values):
    """Portable legacy-message identity; local BIGSERIAL ids are not exported."""
    from datetime import datetime, timezone

    normalized = []
    for column, value in zip(TABLES["session_messages"], values):
        if column in JSON_COLUMNS and isinstance(value, str):
            value = json.loads(value)
        if column == "created_at" and value is not None:
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
        normalized.append(value)
    return json.dumps(normalized, sort_keys=True, default=str, separators=(",", ":"))


def _reconcile_app_rows(cur, sid, table, snapshot):
    """Prune rewinds/divergent compaction first; retain unchanged local rows.

    One indexed session scan plus bounded deletes/inserts, not one anti-join per
    message. Matching legacy rows is a multiset so identical messages survive.
    Only portable receipts/ancestry markers are authoritative in this snapshot.
    """
    from collections import Counter

    columns = TABLES[table]
    selected = ("id", *columns) if table == "session_messages" else (("id", "seq") if table == "session_events" else ("id",))
    cur.execute(f"SELECT {', '.join(selected)} FROM {table} WHERE session_id=%s" +
                _TRANSFER_FILTERS.get(table, ""), (sid,))
    existing = cur.fetchall()
    stale = []
    if table == "session_messages":
        available = Counter(_message_signature(values) for values in snapshot)
        kept = Counter()
        for row in existing:
            signature = _message_signature([row[c] for c in columns])
            if kept[signature] < available[signature]:
                kept[signature] += 1
            else:
                stale.append(row["id"])
        missing = []
        for values in snapshot:
            signature = _message_signature(values)
            if kept[signature]:
                kept[signature] -= 1
            else:
                missing.append(values)
        snapshot = missing
    else:
        keys = {(values[0], values[1]) if table == "session_events" else values[0] for values in snapshot}
        stale = [row["id"] for row in existing
                 if ((row["id"], row["seq"]) if table == "session_events" else row["id"]) not in keys]
    for start in range(0, len(stale), 200):
        part = stale[start:start + 200]
        cur.execute(f"DELETE FROM {table} WHERE session_id=%s AND id IN (" +
                    ", ".join(["%s"] * len(part)) + ")", (sid, *part))
    return snapshot


def _publish_history(cur, sid, token):
    """Atomic reconciliation/publication, retryable after an uncertain commit."""
    from psycopg2.extras import Json
    from app.db.base import parse_jsonb
    from app.db.network import _insert_rows

    cur.execute("SELECT * FROM handoff_history_chunks WHERE token=%s AND session_id=%s "
                "ORDER BY table_name,batch_no FOR UPDATE", (token, sid))
    chunks = cur.fetchall()
    if not chunks:
        return
    timing = _import_timing.get()
    grouped = {table: [] for table in APP_TABLES}
    for chunk in chunks:
        if chunk["table_name"] in grouped:
            grouped[chunk["table_name"]].extend(parse_jsonb(chunk["rows"]))
    for table, snapshot in grouped.items():
        started = monotonic()
        if timing:
            timing.waiting("publish_history." + table)
        count = len(snapshot)
        snapshot = _reconcile_app_rows(cur, sid, table, snapshot)
        columns = ("session_id", *TABLES[table])
        rows = [(sid, *[Json(v) if c in JSON_COLUMNS and v is not None else v
                        for c, v in zip(TABLES[table], values)]) for values in snapshot]
        from app.db.history_indexing import importing
        with importing(cur, table) as deferred:
            _insert_rows(cur, table, columns, rows,
                         _history_conflict(table, columns, deferred), batch=200)
        if timing:
            timing.steps.append(f"publish_history.{table}={monotonic() - started:.3f}s rows={count}")
    cur.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM session_events WHERE session_id=%s", (sid,))
    ceiling = cur.fetchone()["seq"]
    cur.execute("INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,%s) "
                "ON CONFLICT(session_id) DO UPDATE SET next_seq=CASE "
                "WHEN session_event_counters.next_seq > EXCLUDED.next_seq THEN session_event_counters.next_seq "
                "ELSE EXCLUDED.next_seq END", (sid, ceiling))
    cur.execute("UPDATE sessions SET sdk_session_id=%s WHERE id=%s", (chunks[0]["sdk_session_id"], sid))
    cur.execute("DELETE FROM handoff_history_chunks WHERE token=%s AND session_id=%s", (token, sid))
    header = chunks[0] if chunks[0]["table_name"] == "" else None
    return (parse_jsonb(header["rows"]) or {}) if header else {}


def _mark_commit_sent(sid, token, source, receipt_required=True):
    """Persist uncertainty before sending a commit that may lose its reply."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE handoff_history_chunks SET rows=%s WHERE token=%s AND session_id=%s "
                    "AND table_name='' AND batch_no=0",
                    (json.dumps({"commit_sent": True, "source_id": source,
                                 "receipt_required": receipt_required}), token, sid))
        if not cur.rowcount:
            raise ValueError("The staged hand-off expired before commit; ownership was not moved.")


def _history_receipt_matches(session, token, metadata, device):
    from app.db.base import parse_jsonb

    if not session or session["owner_device_id"] != device:
        return False
    receipt = (parse_jsonb(session["placement"]) or {}).get("handoff") or {}
    if receipt.get("token"):
        return receipt["token"] == token
    # Released sources write no receipt. Only an actually-sent commit may use
    # this fallback; sources advertising receipts must always supply the token.
    return bool(metadata.get("commit_sent") and not metadata.get("receipt_required"))


def _warn_retained_history(cur, sid, token, created_at, metadata):
    age = _staging_age(created_at)
    warned = metadata.get("retention_warning", 0)
    level = warned
    if age >= TTL and warned < 1:
        logger.warning("Retaining uncertain hand-off for session %s from source %s beyond its lease; "
                       "waiting for ownership or cancellation evidence", sid, metadata.get("source_id"))
        level = 1
    if age >= 7 * 24 * 60 * 60 and warned < 2:
        logger.warning("Retaining uncertain hand-off for session %s from source %s after seven days; "
                       "manual recovery may be needed", sid, metadata.get("source_id"))
        level = 2
    if level != warned:
        cur.execute("UPDATE handoff_history_chunks SET rows=%s WHERE token=%s AND session_id=%s "
                    "AND table_name='' AND batch_no=0",
                    (json.dumps({**metadata, "retention_warning": level}), token, sid))


def _delete_history(cur, sid, token, sdk):
    cur.execute("SELECT 1 FROM sessions WHERE sdk_session_id=%s", (sdk,))
    if not cur.fetchone():
        for table in TABLES.keys() - APP_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE session_id=%s", (sdk,))
    cur.execute("DELETE FROM handoff_history_chunks WHERE token=%s AND session_id=%s", (token, sid))
    _release_holds(cur, HOLD + token)


def _lock_history_headers(cur, sid):
    # Supersession cleans older headers at the final flip. Lock them before
    # the session row too, so a concurrent expiry sweep cannot invert the order.
    cur.execute("SELECT token FROM handoff_history_chunks WHERE session_id=%s "
                "AND table_name='' AND batch_no=0 ORDER BY token FOR UPDATE", (sid,))
    cur.fetchall()


def _discard_superseded_history(cur, sid, token):
    from app.db.base import parse_jsonb

    cur.execute("SELECT token,sdk_session_id,rows FROM handoff_history_chunks "
                "WHERE session_id=%s AND token<>%s AND table_name='' AND batch_no=0 FOR UPDATE", (sid, token))
    for row in cur.fetchall():
        if (parse_jsonb(row["rows"]) or {}).get("commit_sent"):
            _delete_history(cur, sid, row["token"], row["sdk_session_id"])
            cur.execute("UPDATE handoff_wakes SET finalized=TRUE,wakes='[]' WHERE session_id=%s AND token=%s",
                        (sid, row["token"]))


def _discard_history(sid, token, *, expired_only=False):
    from app.db.base import parse_jsonb

    with get_db() as conn:
        cur = conn.cursor()
        # Publication locks private staging before taking the session row at
        # its final pointer flip. Use that order here too, never block sync
        # while waiting for a slow publisher to release its private header.
        cur.execute("SELECT sdk_session_id,created_at,rows FROM handoff_history_chunks "
                    "WHERE token=%s AND session_id=%s AND table_name='' AND batch_no=0 FOR UPDATE", (token, sid))
        row = cur.fetchone()
        if not row:
            return
        cur.execute("SELECT owner_device_id,placement FROM sessions WHERE id=%s FOR UPDATE", (sid,))
        session = cur.fetchone()
        if expired_only:
            receipt = (parse_jsonb(session["placement"]) or {}).get("handoff") or {} if session else {}
            owner = session["owner_device_id"] if session else None
            device = engine.require_context().device_id
            metadata = parse_jsonb(row["rows"]) or {}
            if _history_receipt_matches(session, token, metadata, device):
                return  # An ownership sync won the race; recover, never discard.
            if metadata.get("commit_sent"):
                if owner in (None, "", metadata.get("source_id")) or (owner == device and not receipt.get("token")):
                    # No age alone proves cancellation. Persist warning levels
                    # so restarts and repeated sweeps don't flood the logs.
                    _warn_retained_history(cur, sid, token, row["created_at"], metadata)
                    return
            elif _staging_age(row["created_at"]) < TTL:
                return
        _delete_history(cur, sid, token, row["sdk_session_id"])


def _staging_age(created_at):
    from datetime import datetime, timezone

    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return time.time() - created_at.replace(tzinfo=created_at.tzinfo or timezone.utc).timestamp()


def recover_history_staging():
    """Boot/maintenance: finish committed imports, collect expired private ones.

    The ownership record carries the commit token, so an older abandoned export
    cannot overwrite the snapshot of a later successful move. Wakes recovered
    here remain durable; the async boot/maintenance caller rehydrates them.
    """
    from app.db.base import parse_jsonb

    ctx = engine.context()
    if ctx is None:
        return 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT h.token,h.session_id,h.created_at,h.rows,s.owner_device_id,s.placement,s.branch "
                    "FROM handoff_history_chunks h JOIN sessions s ON s.id=h.session_id "
                    "WHERE h.table_name='' AND h.batch_no=0 ORDER BY h.created_at DESC")
        staged = cur.fetchall()
    published = 0
    recovered = set()
    for row in staged:
        if row["session_id"] in recovered:
            continue  # Acceptance retired the older marked tokens atomically.
        metadata = parse_jsonb(row["rows"]) or {}
        try:
            if _history_receipt_matches(row, row["token"], metadata, ctx.device_id):
                _accept_owner(row["session_id"], ctx.device_id, row["branch"], row["token"], recovering=True)
                recovered.add(row["session_id"])
                published += 1
            else:
                _discard_history(row["session_id"], row["token"], expired_only=True)
        except Exception:  # noqa: BLE001 — retry one token without stopping boot
            logger.warning("Could not recover hand-off history for session %s", row["session_id"], exc_info=True)
    return published


async def history_recovery_loop():
    """Also collect attempts that were still inside their lease during boot."""
    from app.core import wakeup_service

    while True:
        await asyncio.sleep(60)
        try:
            if await run_sync(recover_history_staging):
                await wakeup_service.rehydrate_pending()
                _wake_queued()
        except Exception:  # noqa: BLE001 — transient DB outages must not stop maintenance
            logger.warning("Could not sweep hand-off history staging", exc_info=True)


class _HistoryCursor:
    """Measure real execute calls, including driver binding/JSON serialization.

    Python row/placeholder construction outside execute is excluded. The
    shared metrics span all cursors used by one table's import chunks.
    """

    def __init__(self, cursor, metrics):
        self.cursor, self.metrics = cursor, metrics

    def execute(self, *args, **kwargs):
        started = monotonic()
        try:
            return self.cursor.execute(*args, **kwargs)
        finally:
            seconds = monotonic() - started
            self.metrics["statements"] += 1
            self.metrics["execute_seconds"] += seconds
            self.metrics["max_statement_seconds"] = max(self.metrics["max_statement_seconds"], seconds)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


def install_history(sid, data, cwd):
    """Stage history with per-table timings (rounded seconds).

    connection_seconds includes get_db entry: pool wait, health checks and
    transaction setup. execute_seconds times driver execute calls only (with
    binding/serialization), not Python row adaptation. commit_seconds covers
    get_db exit: commit, pool return, slow-hold reporting and post-commit
    callbacks. seconds also includes the table's Python preparation; the
    staging header and final publication are outside these table timings.
    """
    from psycopg2.extras import Json
    from app.core.cli_sessions import project_key_for_directory
    from app.db.network import _insert_rows

    old_sdk = data.get("sdk_session_id")
    # New store identity prevents replacing another local session's native tree
    # and prevents a warm provider cache from reusing stale nodes. Native trees
    # therefore stage under a private SDK id; only app rows use mirrored keys.
    sdk = ("ses_" if str(old_sdk).startswith("ses_") else "") + str(uuid.uuid4()) if old_sdk else None
    token = data.get("token") or str(uuid.uuid4())
    timing = data.get("_timing")
    facts = {}
    try:
        with get_db() as conn:
            conn.cursor().execute("INSERT INTO handoff_history_chunks "
                                  "(token,session_id,sdk_session_id,table_name,batch_no,rows) "
                                  "VALUES (%s,%s,%s,'',0,'[]')", (token, sid, sdk))
        for table, columns in TABLES.items():
            started = monotonic()
            if timing:
                timing.waiting("install_history." + table)
            destination_id = sid if table in APP_TABLES else sdk
            rows = []
            for row in data["history"].get(table, []):
                if table == "pending_interactions":
                    from app.core.model_override import RECEIPT_KINDS
                    payload = row.get("payload") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    question = (row.get("kind") == "question" and row.get("state") == "pending"
                                and payload.get("nonblocking") is True and payload.get("questions"))
                    if not question and (row.get("kind") not in RECEIPT_KINDS or row.get("state") != "answered"):
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
                    values.append(value)
                rows.append((destination_id, *values))
            metrics = {"rows": len(rows), "statements": 0, "execute_seconds": 0.0,
                       "max_statement_seconds": 0.0, "commit_seconds": 0.0, "connection_seconds": 0.0}
            if timing:
                timing.rows(0, len(rows))
            for start in range(0, len(rows), 200):
                part = rows[start:start + 200]
                # get_db commits each bounded chunk. Staging never changes the
                # visible app mirror or its SDK pointer, including on failure.
                connection_started = monotonic()
                with get_db() as conn:
                    metrics["connection_seconds"] += monotonic() - connection_started
                    cur = _HistoryCursor(conn.cursor(), metrics)
                    if table in APP_TABLES:
                        cur.execute("INSERT INTO handoff_history_chunks "
                                    "(token,session_id,sdk_session_id,table_name,batch_no,rows) "
                                    "VALUES (%s,%s,%s,%s,%s,%s)",
                                    (token, sid, sdk, table, start // 200,
                                     Json([r[1:] for r in part], dumps=lambda value: json.dumps(value, default=str))))
                    else:
                        adapted = [(r[0], *[Json(v) if c in JSON_COLUMNS and v is not None else v
                                             for c, v in zip(columns, r[1:])]) for r in part]
                        from app.db.history_indexing import importing
                        with importing(cur, table) as deferred:
                            conflict = _history_conflict(table, ("session_id", *columns), deferred)
                            _insert_rows(cur, table, ("session_id", *columns), adapted,
                                         conflict, batch=200)
                    written = monotonic()
                metrics["commit_seconds"] += monotonic() - written
                if timing:
                    timing.rows(start + len(part), len(rows))
            metrics["seconds"] = monotonic() - started
            facts[table] = {key: round(value, 3) if key.endswith("seconds") else value
                            for key, value in metrics.items()}
            # Log each nonempty completed table immediately, not only when
            # ownership finally commits. Never log parameters/transcript text.
            if rows:
                logger.info("Hand-off import for session %s install_history.%s: rows=%d statements=%d "
                            "total=%.3fs connection=%.3fs execute=%.3fs max_statement=%.3fs commit_release=%.3fs",
                            sid, table, len(rows), metrics["statements"], metrics["seconds"],
                            metrics["connection_seconds"], metrics["execute_seconds"],
                            metrics["max_statement_seconds"], metrics["commit_seconds"])
        if not data.get("_prepare_history"):
            with get_db() as conn:
                _publish_history(conn.cursor(), sid, token)
    except BaseException:
        _discard_history(sid, token)
        raise
    finally:
        data["_history_metrics"] = facts
    return sdk


def _accept_owner_row(cur, sid, device_id, branch, token=None):
    # instance_id moves too (as adopt/unpin do): left on the previous
    # worker's id, the session listed as remote with an unknown state here.
    placement = {"where": "runner:" + device_id}
    if token:
        placement["handoff"] = {"token": token}
    cur.execute("UPDATE sessions SET owner_device_id=%s, instance_id=%s, placement=%s, branch=%s, updated_at=NOW() WHERE id=%s",
                (device_id, settings.effective_instance_id, json.dumps(placement), branch, sid))
    if branch is None:
        cur.execute("UPDATE sessions SET worktree_id=NULL, worktree_branch=NULL, worktree_path=NULL WHERE id=%s", (sid,))
    if token:
        _release_holds(cur, HOLD + token)
        _discard_superseded_history(cur, sid, token)


def _lock_published_owner(cur, sid, device_id, token, previous, metadata):
    """Revalidate after the expensive delta, with only a short session lock.

    A matching commit sync is allowed during publication; a later move is not.
    Raising rolls the delta and SDK pointer back with the caller's transaction.
    """
    from app.db.base import parse_jsonb

    cur.execute("SELECT owner_device_id,placement FROM sessions WHERE id=%s FOR UPDATE", (sid,))
    current = cur.fetchone()
    def identity(row):
        receipt = (parse_jsonb(row["placement"]) or {}).get("handoff") or {}
        return row["owner_device_id"], receipt.get("token")
    if not current or (identity(current) != identity(previous)
                       and not _history_receipt_matches(current, token, metadata or {}, device_id)):
        raise ValueError("Session ownership changed during hand-off publication; context was not replaced.")
    return current


def _accept_owner(sid, device_id, branch=None, token=None, held_messages=None, *, recovering=False):
    from app.db.base import parse_jsonb

    rows = []
    with get_db() as conn:
        cur = conn.cursor()
        # Same lock order as finalize-wakes, which may race the commit reply.
        cur.execute("SELECT token,wakes,superseded,keep_peer,resume_wait,finalized FROM handoff_wakes "
                    "WHERE session_id=%s ORDER BY token FOR UPDATE", (sid,))
        staged_rows = [row for row in cur.fetchall() if row["token"] == token and not row["finalized"]]
        _lock_history_headers(cur, sid)
        # The durable import admission fence prevents target turns throughout
        # publication. Let ownership sync/session metadata writes proceed; lock
        # and revalidate the owner only at the final context/ownership flip.
        cur.execute("SELECT owner_device_id,placement FROM sessions WHERE id=%s", (sid,))
        session = cur.fetchone()
        receipt = (parse_jsonb(session["placement"]) or {}).get("handoff") or {}
        if recovering:
            cur.execute("SELECT rows FROM handoff_history_chunks WHERE session_id=%s AND token=%s "
                        "AND table_name='' AND batch_no=0", (sid, token))
            header = cur.fetchone()
            metadata = parse_jsonb(header["rows"]) or {} if header else {}
            if not _history_receipt_matches(session, token, metadata, device_id):
                return []  # A newer move won while the sweep was acquiring locks.
        metadata = _publish_history(cur, sid, token) if token else {}
        session = _lock_published_owner(cur, sid, device_id, token, session, metadata)
        receipt = (parse_jsonb(session["placement"]) or {}).get("handoff") or {}
        if held_messages is None:
            held_messages = receipt.get("held_messages", 0) if receipt.get("token") == token else 0
        _accept_owner_row(cur, sid, device_id, branch, token)
        for staged in staged_rows:
            wakes = parse_jsonb(staged["wakes"]) or []
            kept = [w for w in wakes if w.get("source") != "handoff_resume"]
            if held_messages:
                staged["resume_wait"] = True
                cur.execute("UPDATE handoff_wakes SET resume_wait=TRUE WHERE token=%s", (staged["token"],))
            for wake in wakes:
                if wake.get("source") != "handoff_resume":
                    continue
                if staged["superseded"] or staged["resume_wait"]:
                    if staged["keep_peer"]:
                        kept.append(wake)  # Release after the held humans, never before.
                    continue  # An explicit Stop/delete permanently retires it.
                # Removing it from staging is the durable receipt: a source
                # finalize/retry cannot recreate this wake after it has fired.
                cur.execute("INSERT INTO pending_wakeups "
                            "(id,session_id,profile_id,project_id,prompt,source,instance_id,fire_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                            (wake["id"], sid, wake.get("profile_id"), wake.get("project_id"),
                             wake["prompt"], "handoff_resume", settings.effective_instance_id, wake["fire_at"]))
                cur.execute("SELECT * FROM pending_wakeups WHERE id=%s", (wake["id"],))
                row = cur.fetchone()
                if row:
                    rows.append(dict(row))
            cur.execute("UPDATE handoff_wakes SET wakes=%s WHERE token=%s",
                        (json.dumps(kept), staged["token"]))
    return rows


async def _accept_and_arm(sid, device_id, branch, token, held_messages=None):
    from app.core import wakeup_service

    for row in await run_sync(_accept_owner, sid, device_id, branch, token, held_messages):
        wakeup_service.arm_persisted(row)


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
        try:
            result = await _call(device, payload, timeout=900)
        finally:
            _remote_progress.pop(session["id"], None)
    if not isinstance(result.get("moved"), bool):
        raise ValueError("The destination did not confirm the session transfer.")
    return result["moved"]


async def receive(session):
    """Publish the owner branch, fetch locally, then sync context and ownership.

    Project-less sessions, and projects whose folder is not a Git repository
    on either worker (#639), skip the Git steps and transfer history only.
    """
    timing = _Timing("import", session["id"], loop=asyncio.get_running_loop())
    context_token = _import_timing.set(timing)
    try:
        cwd = await _receive(session, timing)
    except BaseException:
        timing.log("failed")
        raise
    finally:
        _import_timing.reset(context_token)
    timing.log("committed")
    return cwd


async def _receive(session, timing):
    owner = control.owner_of(session)
    ctx = engine.context()
    if owner is None:
        if (not ctx or session.get("owner_device_id") != ctx.device_id) and session.get("instance_id") not in (None, "", settings.effective_instance_id):
            raise ValueError("The owning worker must be online on this network to resume here.")
        return None
    if ctx is None:
        raise ValueError("The owning worker must be online on this network to resume here.")
    timing.waiting("resolve_checkout")
    project = await run_sync(project_resolver.resolve_project_v2, session)
    base = remote = None
    plain = False
    if project:
        if not await run_sync(project_resolver.has_local_base_checkout, session):
            raise ValueError("Clone this project on this worker before resuming here.")
        base_row = await run_sync(project_resolver._local_checkout, project["id"], settings.effective_instance_id)
        base = checkout_manager._resolve_checkout_path(base_row["path"])
        try:
            remote = await run_sync(checkout_transfer.remote_identity, base)
        except ValueError:
            # Only a folder proven not to be a repository asks for history
            # only; a failed probe keeps the original Git error.
            if not await run_sync(_known_plain, str(base)):
                raise
            # A folder without Git here: ask the owner for its history only.
            if await run_sync(plain_folder_carry, owner) is False:
                raise ValueError(PLAIN_OWNER_UPDATE) from None
            plain = True
    held = {}
    try:
        return await _pull_steps(session, owner, ctx, project, base, remote, held,
                                 HOLD + str(uuid.uuid4()), timing, plain)
    finally:
        if held:
            timing.waiting("release_checkout_holds")
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
    previous = str((active or {}).get("runner_id") or "")
    if previous.startswith(HOLD):
        old_token = previous[len(HOLD):]
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM handoff_history_chunks WHERE token=%s AND session_id=%s LIMIT 1", (old_token, sid))
            abandoned = cur.fetchone()
        if abandoned:
            # This function is reached only after a fresh authenticated begin.
            # Its new source lease proves the previous attempt did not retain
            # ownership, even if that attempt's cancellation reply was lost.
            _discard_history(sid, old_token)
            with get_db() as conn:
                conn.cursor().execute("UPDATE handoff_wakes SET finalized=TRUE,wakes='[]' "
                                      "WHERE token=%s AND session_id=%s", (old_token, sid))
            run = runs.claim_run(sid, holder, TTL)
            if run:
                held[sid] = run
                return
    if not active or not previous.startswith(HANDOFF):
        raise ValueError("Stop this session's active run before moving it.")


def _open_export(ctx, reply, sid):
    blob = reply.get("payload", "")
    raw = crypto.decrypt_payload(ctx.keyring[int(reply["key_version"])], blob, _aad(sid, ctx.device_id))
    return json.loads(raw), len(blob)


async def _owner_commit(owner, payload):
    for attempt in range(3):
        try:
            result = await control.call(owner, "handoff", payload, timeout=180)
            break
        except (control.ControlRefused, ValueError) as exc:
            reason = str(exc).lower()
            if not attempt or not ("reservation expired" in reason or "already transferred" in reason):
                logger.warning("Owner refused hand-off commit for session %s: %s", payload["session_id"], exc)
                if not attempt and payload.get("token"):
                    # An explicit first refusal proves no commit was applied.
                    # A refused cancel after lease expiry cannot make it uncertain.
                    await run_sync(_discard_history, payload["session_id"], payload["token"])
                raise
            # A retry may have waited behind the first commit's lease lock.
            # Its refusal does not establish that the first commit failed.
            if attempt == 2:
                raise control.MaybeApplied(str(exc)) from exc
        except (control.RunnerUnreachable, TimeoutError):
            # Commit is idempotent. Never turn an uncertain applied commit into
            # a cancellation; obtain its receipt again, or leave durable staging
            # for recovery after the ownership sync arrives.
            if attempt == 2:
                raise
        await asyncio.sleep(1.5)
    if not result.get("committed"):
        reason = result.get("error") or result.get("reason") or "The owning worker did not confirm the transfer."
        logger.warning("Owner refused hand-off commit for session %s: %s", payload["session_id"], reason)
        if attempt:
            raise control.MaybeApplied(reason)
        if payload.get("token"):
            await run_sync(_discard_history, payload["session_id"], payload["token"])
        raise ValueError(reason)
    return result


async def _pull_steps(session, owner, ctx, project, base, remote, held, holder, timing,
                      plain: bool = False):
    sid = session["id"]
    expected_branch = project_resolver._effective_branch(session) if project and not plain else None
    if expected_branch:
        timing.waiting("early_checkout_guard")
        # Refuse a busy receiving checkout before the owner commits or pushes.
        _, path = await run_sync(_receiving_path, base, expected_branch)
        await run_sync(_hold_checkout, session, project, str(path), expected_branch, holder, held)
        timing.step("early_checkout_guard", holds=len(held))
    timing.waiting("owner_export")
    begin_body = {"session_id": sid, "phase": "begin", "remote": remote, "wake_barrier": True}
    if plain:
        begin_body["plain_folder"] = True
    reply = await _call(owner, begin_body, timeout=600)
    timing.step("owner_export")
    # A long chat's export is tens of megabytes; never decode it on the event loop.
    timing.waiting("decode")
    data, size = await run_sync(_open_export, ctx, reply, sid)
    timing.step("decode", mb=f"{size / 1e6:.1f}")
    token = data["token"]
    data["_prepare_history"] = True
    data["_timing"] = timing
    committed = False
    published = False
    try:
        if data.get("wakes") and "wake_barrier" not in data:
            raise ValueError("Update both workers before moving a chat with pending wake-ups.")
        state = data["checkout"]
        if data["project_id"] != (project["id"] if project else None):
            raise ValueError("The owning worker's project changed; refresh before resuming.")
        # Keep run admission closed through atomic context publication, including
        # plain chats and recovery after a lost commit response.
        await run_sync(_reserve_mirror, sid, HOLD + token, held)
        if not project or plain:
            # Plain chat, or a project folder without Git on both workers: no
            # checkout to move, only the conversation history. A chat runs in
            # this worker's workspace folder; a project in its folder here.
            if state is not None:
                raise ValueError("Unexpected checkout for a project-less session." if not project
                                 else PLAIN_MISMATCH)
            if plain and data.get("plain_folder") is not True:
                raise ValueError(PLAIN_OWNER_UPDATE)
            cwd = str(base) if plain else str(settings.effective_workspace_dir)
            timing.waiting("install_history")
            await run_sync(install_history, sid, data, cwd)
            if data.get("wake_barrier"):
                await run_sync(_stage_wakes, sid, owner, data, None)
            timing.step("install_history", tables=json.dumps(data.get("_history_metrics", {}), separators=(",", ":")))
            timing.waiting("owner_commit")
            await run_sync(_mark_commit_sent, sid, token, owner, data.get("handoff_receipt") == 1)
            result = await _owner_commit(owner, {"session_id": sid,
                "phase": "commit", "token": token,
                "fingerprint": None, "branch": None, "session_branch": None,
                **({"plain_folder": True} if plain else {}),
                **_moving_wakes(data),
            })
            timing.step("owner_commit")
            if not result.get("committed"):
                raise ValueError("The owning worker did not confirm the transfer.")
            committed = True
            timing.waiting("accept_owner")
            await _accept_and_arm(sid, ctx.device_id, None, token, result.get("held_messages"))
            published = True
            if not data.get("wake_barrier"):
                timing.waiting("arm_wakes")
                await _arm_moved_wakes(sid, data.get("wakes"))
            return cwd
        if not state or (expected_branch and state["branch"] != expected_branch):
            raise ValueError("The owning worker's branch changed; refresh before resuming.")
        timing.waiting("checkout_guard")
        dest, path = await run_sync(_receiving_path, base, state["branch"])
        # Exclude other sessions working in the checkout this import changes.
        await run_sync(_hold_checkout, session, project, str(path), state["branch"], holder, held)
        timing.step("checkout_guard", holds=len(held))
        timing.waiting("git_fetch")
        cwd = await run_sync(checkout_transfer.import_checkout, base, dest, state)
        timing.step("git_fetch")
        # Keep placement on the original owner while preparing local context.
        timing.waiting("install_history")
        await run_sync(install_history, sid, data, cwd)
        timing.step("install_history", tables=json.dumps(data.get("_history_metrics", {}), separators=(",", ":")))
        timing.waiting("verify_checkout")
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
        timing.waiting("owner_commit")
        await run_sync(_mark_commit_sent, sid, token, owner, data.get("handoff_receipt") == 1)
        result = await _owner_commit(owner, {"session_id": sid,
            "phase": "commit", "token": token,
            "fingerprint": state["fingerprint"],
            "branch": state["branch"],
            "session_branch": session_branch,
            **_moving_wakes(data),
        })
        timing.step("owner_commit")
        if not result.get("committed"):
            raise ValueError("The owning worker did not confirm the transfer.")
        committed = True
        timing.waiting("accept_owner")
        await _accept_and_arm(sid, ctx.device_id, session_branch, token, result.get("held_messages"))
        published = True
        if not data.get("wake_barrier"):
            timing.waiting("arm_wakes")
            await _arm_moved_wakes(sid, data.get("wakes"))
        return cwd
    finally:
        if not committed:
            cancelled = {}
            # Best effort. A lost response/worker restart leaves a bounded lease,
            # not a permanent block; never force ownership after a timeout.
            try:
                timing.waiting("cancel")
                cancelled = await control.call(owner, "handoff", {"session_id": sid,"phase": "cancel", "token": token})
                if cancelled.get("cancelled"):
                    await run_sync(_discard_history, sid, token)
                    if data.get("wake_barrier"):
                        await run_sync(_discard_staged_wakes, sid, owner, token)
            except (control.RunnerUnreachable, control.ControlRefused, ValueError):
                pass
            if not cancelled.get("cancelled"):
                # The commit marker fences admission beyond lease expiry until
                # publication or proven cancellation/supersession, never age alone.
                held.pop(sid, None)
        elif not published:
            held.pop(sid, None)  # A committed import still needs its recovery guard.
