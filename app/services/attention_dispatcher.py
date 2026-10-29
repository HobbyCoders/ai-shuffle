"""Durable, owner-worker-only Needs-you state and latest-command outbox.

Inputs are *pending* interactions, private-access waits and the existing prose
question projection, never lifecycle events or bell severity. State travels in
the owner-ordered session catalog. Android also persists version/episode receipts
so retries, reordering and an FCM acceptance followed by a crash cannot re-alert.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from app.db.base import get_db, parse_jsonb
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


def reduce_attention(previous, needs, user_position, *, active=True, explicit_id=None, title="Chat", now=None):
    """Pure state machine. Each identity is observed once, even after replay."""
    now = time.time() if now is None else now
    state = dict(previous or {})
    seen = set(state.get("seen", []))
    calls = set(state.get("calls", []))
    if explicit_id in calls:
        explicit_id = None
    explicit = state.get("explicit")
    if explicit and explicit != user_position:
        explicit = None
    if explicit_id:
        calls.add(explicit_id)
        explicit = user_position
    needs = set(needs) if active else set()
    if needs or not active:
        # A real card/question takes over the same episode. An explicit request
        # must not keep it alive after the owner answers/dismisses that card.
        explicit = None
    # A standalone explicit request waits for an owner reply or settlement.
    if explicit is not None:
        needs.add("explicit:" + str(explicit))
    old_needs = set(state.get("needs", []))
    if needs == old_needs and not explicit_id and title == state.get("title", title):
        return state, False
    was_pending, pending = bool(old_needs), bool(needs)
    new_need = bool(needs - seen)
    action = "alert" if pending and not was_pending and new_need else "update" if pending else "clear"
    seen.update(needs)
    state.update(needs=sorted(needs), seen=sorted(seen), calls=sorted(calls),
                 explicit=explicit, title=title)
    # An idle session's completion, parking, errors or settle cannot enqueue a push.
    if not pending and not was_pending:
        return state, False
    version = int(state.get("version", 0)) + 1
    episode = uuid.uuid4().hex if action == "alert" else state.get("episode", "")
    state.update(version=version, episode=episode, pending=True, enqueued_at=now, action=action)
    return state, True


def latest_notify_tool_id(session_id):
    return latest_tool_id(session_id, "NotifyDevices")


def latest_tool_id(session_id, name=None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT payload FROM session_events WHERE session_id=%s AND event_type='tool.use'
            ORDER BY seq DESC LIMIT 1""", (session_id,))
        row = c.fetchone()
        payload = parse_jsonb(row["payload"]) if row else {}
        if name and str(payload.get("name", "")).split("__")[-1] != name:
            return None
        return payload.get("id") or payload.get("tool_use_id")


def _owned(row, worker_id):
    if not row or not worker_id or row.get("owner_device_id") != worker_id:
        return False
    from app.core.api_user_scope import session_api_user_id
    # Legacy delegated rows inherit the spawning API user's principal.
    return session_api_user_id(row) is None


def _state(row):
    return dict(parse_jsonb(row.get("push_attention_state")) or {})


def _transition(session_id, worker_id, private_needs, explicit_id=None):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM sessions WHERE id=%s FOR NO KEY UPDATE", (session_id,))
        row = c.fetchone()
        if not _owned(row, worker_id):
            return None
        row = dict(row)
        previous = _state(row)
        c.execute("""SELECT id FROM pending_interactions WHERE session_id=%s
            AND kind IN ('question', 'permission') AND state='pending'
            AND (expires_at IS NULL OR expires_at > NOW())""", (session_id,))
        needs = {"interaction:" + str(r["id"]) for r in c.fetchall()}
        needs.update(private_needs)
        seq, epoch = row.get("last_assistant_question_seq"), row.get("last_assistant_question_epoch")
        user_seq, user_epoch = row.get("last_user_message_seq"), row.get("last_user_message_epoch")
        if seq is not None and epoch is not None and (user_seq is None or (user_epoch == epoch and seq > user_seq)):
            needs.add(f"prose:{epoch}:{seq}")
        position = [user_epoch, user_seq]
        # Background/child sessions may start outside Active. Only an actual
        # settlement boundary (not is_active=False alone) dismisses their waits.
        c.execute("""SELECT event_type FROM session_events WHERE session_id=%s AND
            (event_type IN ('turn.started', 'message.user') OR
             (event_type='system.note' AND payload->>'subtype'='session_settled')) ORDER BY seq DESC LIMIT 1""", (session_id,))
        boundary = c.fetchone()
        active = row.get("status") != "archived" and not (boundary and boundary["event_type"] == "system.note")
        state, changed = reduce_attention(previous, needs, position, active=active,
            explicit_id=explicit_id, title=(row.get("title") or "Chat")[:120])
        if state != previous:
            c.execute("UPDATE sessions SET push_attention_state=%s WHERE id=%s", (json.dumps(state), session_id))
    if state != previous:
        from app.db.session_events import notify_session_write
        notify_session_write(session_id, "sessions")
    return state


def _candidates(worker_id):
    # Expiries and crashed/restarted dispatches do not depend on a websocket or hook.
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT s.id FROM sessions s WHERE s.owner_device_id=%s AND s.api_user_id IS NULL
            AND (s.last_assistant_question_seq IS NOT NULL OR s.push_attention_state->>'pending' = 'true'
                 OR s.push_attention_state->'needs' <> '[]'::jsonb
                 OR EXISTS (SELECT 1 FROM pending_interactions i WHERE i.session_id=s.id AND i.state='pending'))""", (worker_id,))
        return [r["id"] for r in c.fetchall()]


def current_command(session_id, worker_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
        row = c.fetchone()
        if not _owned(row, worker_id):
            return None
        state = _state(row)
        if not state.get("version"):
            return None
        return {"type": "attention", "worker_id": worker_id, "session_id": session_id,
                "version": str(state["version"]), "episode": state.get("episode", ""),
                "action": state["action"], "title": "AI Shuffle",
                "body": "A chat needs you: " + state.get("title", "Chat"),
                "expires_at": str(int(state.get("enqueued_at", 0) + 600))}


def _ack(session_id, worker_id, version):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM sessions WHERE id=%s FOR NO KEY UPDATE", (session_id,))
        row = c.fetchone()
        if not _owned(row, worker_id):
            return
        state = _state(row)
        if state.get("version") != version:
            return
        state["pending"] = False
        c.execute("UPDATE sessions SET push_attention_state=%s WHERE id=%s", (json.dumps(state), session_id))
    from app.db.session_events import notify_session_write
    notify_session_write(session_id, "sessions")


class AttentionDispatcher:
    def __init__(self, sender=None):
        self.sender = sender
        self.loop = None
        self.dirty = set()
        self.wakeup = None
        self.lock = asyncio.Lock()

    @staticmethod
    def worker_id():
        from app.network import engine
        ctx = engine.context()
        return ctx.device_id if ctx else None

    def wake(self, session_id):
        loop, event = self.loop, self.wakeup
        if loop and event:
            def add():
                if self.wakeup is event:
                    self.dirty.add(session_id)
                    event.set()
            try:
                loop.call_soon_threadsafe(add)
            except RuntimeError:
                # The DB commit must remain successful if shutdown closed the
                # loop after this thread captured it. Startup scans durable state.
                pass

    @staticmethod
    def private_needs(session_id):
        from app.core.chat_access import broker
        now = time.time()
        # Push belongs to the executing session, even when the UI surfaces a
        # child's private-access card in its parent panel on another worker.
        return {"access:" + item.attention_id for item in list(broker.requests.values()) + list(broker.actions.values())
                if item.attention_id and item.session_id == session_id and item.expires_at > now
                and not (item.result if hasattr(item, "result") else item.decision).done()}

    async def reconcile(self, session_id, explicit_id=None):
        async with self.lock:
            worker_id = await run_sync(self.worker_id)
            if not worker_id:
                return {"sent": False, "reason": "worker identity unavailable"}
            state = await run_sync(_transition, session_id, worker_id, self.private_needs(session_id), explicit_id)
            if state is None:
                return {"sent": False, "reason": "only the chat's owner worker may send"}
            if not state.get("pending"):
                return {"sent": False, "deduplicated": True}
            # Recheck ownership/latest state immediately before transport. Never send stale work.
            command = await run_sync(current_command, session_id, worker_id)
            if command is None or int(command["version"]) != state["version"]:
                return {"sent": False, "reason": "attention changed"}
            from app.services.push_notification import push_notification_service, delivered_count
            if time.time() > state["enqueued_at"] + 600:
                results = []
            else:
                sender = self.sender or push_notification_service
                results = await sender.send_to_all_devices("AI Shuffle", command["body"], data=command, raise_lookup_errors=True)
            # Failed real sends remain in the outbox. No credential/recipient is off, not a backlog.
            if not results or all(r.success or r.simulated or r.invalid_token for r in results):
                await run_sync(_ack, session_id, worker_id, state["version"])
            return {"sent": delivered_count(results) > 0, "accepted": delivered_count(results),
                    "note": "FCM acceptance is not proof of display or that the owner has seen it."}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.wakeup = asyncio.Event()
        try:
            while True:
                try:
                    worker = await run_sync(self.worker_id)
                    ids = set(await run_sync(_candidates, worker)) if worker else set()
                except Exception:
                    logger.warning("Attention scan unavailable; retrying")
                    ids = set()
                ids.update(self.dirty)
                self.dirty.clear()
                self.wakeup.clear()
                for sid in ids:
                    try:
                        await self.reconcile(sid)
                    except Exception:
                        logger.warning("Attention reconciliation failed", exc_info=True)
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.loop = None
            self.wakeup = None


dispatcher = AttentionDispatcher()


def wake(session_id):
    dispatcher.wake(session_id)
