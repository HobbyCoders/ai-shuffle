"""Transcripts that arrive after a worker's first copy.

A worker joining a network used to copy every transcript event before it
was usable: on a large network that is millions of events and gigabytes,
an hour of downloading and applying with zero sessions to show. A first
copy in format 2 (``peer_snapshot``) now leaves ``session_events`` out. The
worker is ready as soon as everything else is applied, and this module
copies the transcripts in the background, newest sessions first.

Why no event is lost or applied twice
    The first copy adopts its publisher's watermarks ``W``: the sync loop
    tails every stream from there, so operations after ``W`` arrive as
    usual. The events those watermarks claim (operations up to ``W``) come
    from here instead. A source serves the transcript records it holds now,
    together with its own watermarks, and the reply is applied only when
    those cover ``W`` (the "floor" stored with the backfill): its records
    then include the effect of every operation up to ``W``, and possibly
    later ones. Records go through the snapshot rules (last writer wins by
    clock), so a record this worker already has from the live tail is kept
    when it is newer and an older one is never applied over it; the stream
    then delivers every later operation in order. A source that is still
    filling its own transcripts refuses to serve them, and refuses to serve
    a state copy, so a gap is never passed on.

Deletions
    A request names the highest seq this worker holds for the session. The
    source answers with the deleted records among the seqs it has no row for
    up to there (a cleared transcript's retracted tail, compacted rows), so a
    worker that re-copies after being cut off drops them too.

Re-copies
    A later copy registered while one is still running raises the floor and
    starts every session over under a new ``generation``. A reply fetched
    for the old generation is not applied: each session's transaction checks
    the generation under a shared lock on the state row, which a
    registration takes for update.

When no source can serve
    If no source covering the floor has answered for ``GIVE_UP_SECONDS``
    (the publisher left, say), the floor is relaxed and the best source
    available fills in what it holds, so a worker never waits forever.

Resuming
    Progress is stored per session (``network_transcript_backfill``) in the
    same transaction as the events, so a restart continues after the last
    event that committed. Opening a session that has not arrived yet moves
    it to the front (``prioritise``).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.db import network as store
from app.db.base import get_db
from app.network import crypto, engine, envelope as envelope_mod, membership, policy

logger = logging.getLogger(__name__)

#: Collections a format-2 first copy leaves out, copied here afterwards.
DEFERRED_COLLECTIONS = ("session_events",)
PATH = "/api/v1/network/peer/transcripts"
#: Sessions asked for in one request.
REQUEST_SESSIONS = 32
#: Event bytes (before compression) a source puts in one reply, by default and at most.
REPLY_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 8 * 1024 * 1024
#: Events read per session per query on the serving side.
SESSION_ROWS = 2000
#: Seqs a source looks up for deleted records in one reply part.
PROBE_LIMIT = 5000
#: How long no source covering the floor may answer before any ready source is accepted.
GIVE_UP_SECONDS = 6 * 3600
REQUEST_TIMEOUT = 120.0
IDLE_SECONDS = 30.0
RETRY_MAX_SECONDS = 120.0

_lock = threading.Lock()
# Whether this worker has a backfill to do, by data dir (one worker per
# process, several in tests); absent until read from the database.
_active: Dict[str, bool] = {}
# Sessions someone opened before their transcript arrived, most recent first.
_wanted: List[str] = []
_wake: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
# The latest progress of this process's backfill, for status.
_progress: Dict[str, Any] = {}


class SourceBehind(RuntimeError):
    """The source holds less than the copy's watermarks: it cannot fill the gap yet."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _key() -> str:
    from app.core.config import settings
    return str(settings.data_dir)


def pending() -> bool:
    """Whether transcripts from a first copy are still on their way."""
    key = _key()
    with _lock:
        known = _active.get(key)
    if known is None:
        try:
            known = store.backfill_state() is not None
        except Exception:  # noqa: BLE001 — no network tables yet
            return False
        with _lock:
            _active[key] = known
    return known


def _set_active(value: Optional[bool]) -> None:
    key = _key()
    with _lock:
        if value is None:
            _active.pop(key, None)
        else:
            _active[key] = value


def forget() -> None:
    """The worker left its network: nothing more to copy (the tables are cleared by the caller)."""
    _set_active(None)
    with _lock:
        _wanted.clear()
        _progress.clear()


def reset_for_tests() -> None:
    global _wake, _loop
    forget()
    with _lock:
        _active.clear()
    _wake = None
    _loop = None


def register(cursor, *, publisher: str, floor: Dict[str, Dict[str, int]], ctx: engine.NetworkContext) -> int:
    """Record the transcripts a first copy left out (in the copy's last transaction).

    Every session this worker does not run itself is copied, newest first.
    Sessions created after the copy, and later events of these, arrive with
    the live tail. Returns how many sessions are queued.
    """
    from app.network import catalog
    # Not the sessions this worker runs (the catalog publisher's rule): it holds those itself.
    cursor.execute(
        "SELECT id FROM sessions WHERE COALESCE(owner_device_id, '') <> %s "
        "AND (owner_device_id IS NOT NULL OR COALESCE(instance_id, '') <> %s) "
        "ORDER BY (updated_at IS NULL), updated_at DESC, id",
        (ctx.device_id, catalog._my_instance_id()))
    session_ids = [row["id"] for row in cursor.fetchall()]
    floor = {origin: streams for origin, streams in (floor or {}).items() if origin != ctx.device_id}
    if not session_ids:
        return 0
    store.start_backfill(cursor, publisher=publisher, floor=floor, session_ids=session_ids)
    # Read again after the transaction commits (it may still roll back). The
    # cache is only a hint: serving and capturing check the table itself.
    _set_active(None)
    return len(session_ids)


def after_commit() -> None:
    """A registration committed: start copying."""
    _set_active(None)
    with _lock:
        _progress.clear()
    kick()


def status() -> Optional[dict]:
    """Progress for ``/network/status`` while transcripts are still being copied, else None."""
    if not pending():
        return None
    try:
        state = store.backfill_state()
        done, total = store.backfill_counts()
    except Exception:  # noqa: BLE001
        return None
    if state is None:
        _set_active(False)
        return None
    with _lock:
        progress = dict(_progress)
    peer = progress.get("peer") or state["publisher"]
    ctx = engine.context()
    member = ctx.member(peer) if ctx is not None else None
    return {"phase": "transcripts", "peer": peer, "peer_name": getattr(member, "name", None) or None,
            "sessions_done": done, "sessions_total": total, "events_copied": int(progress.get("events") or 0),
            "error": progress.get("error"), "updated_at": progress.get("updated_at")}


def session_pending(session_id: str) -> bool:
    """Whether this session's transcript has not arrived yet."""
    if not session_id or not pending():
        return False
    try:
        return store.backfill_session_pending(session_id)
    except Exception:  # noqa: BLE001
        return False


def prioritise(session_id: str) -> bool:
    """Copy this session next (someone opened it). True when it is still on its way."""
    if not session_pending(session_id):
        return False
    with _lock:
        if session_id in _wanted:
            _wanted.remove(session_id)
        _wanted.insert(0, session_id)
        del _wanted[REQUEST_SESSIONS:]
    kick()
    return True


def kick() -> None:
    """Wake the backfill loop (from any thread)."""
    wake, loop = _wake, _loop
    if wake is None or loop is None:
        return
    try:
        loop.call_soon_threadsafe(wake.set)
    except RuntimeError:  # the loop closed
        pass


# ---------------------------------------------------------------------------
# Serving side
# ---------------------------------------------------------------------------

def serve(recipient: str, sessions: Any, max_bytes: int = REPLY_BYTES) -> dict:
    """Transcript records of the named sessions after the given seqs, signed by this worker.

    ``sessions`` is ``[{"id": session_id, "after": seq}]``. Each part of the
    reply is one session's events in seq order, sealed as a snapshot page
    (compressed, then encrypted); ``more`` says the session has events past
    ``next``. The reply carries this worker's watermarks, read in the same
    transaction as the events.
    """
    ctx = engine.require_context()
    if ctx.bootstrap_state != engine.BOOTSTRAP_READY or pending():
        raise ValueError("This worker is still receiving its transcripts")
    member = ctx.member(recipient)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise ValueError("Peer is no longer a member")
    if not isinstance(sessions, list) or not sessions or len(sessions) > REQUEST_SESSIONS:
        raise ValueError("Ask for 1 to %d sessions" % REQUEST_SESSIONS)
    wanted = []
    for entry in sessions:
        sid = entry.get("id") if isinstance(entry, dict) else None
        after = entry.get("after", 0) if isinstance(entry, dict) else None
        held = entry.get("held", 0) if isinstance(entry, dict) else None
        if (not isinstance(sid, str) or not sid or len(sid) > 200 or type(after) is not int or after < 0
                or type(held) is not int or held < 0):
            raise ValueError("Invalid transcript request")
        wanted.append((sid, after, held))
    budget = max(1, min(int(max_bytes or REPLY_BYTES), MAX_REPLY_BYTES))
    coll = policy.collection("session_events")
    parts = []
    with get_db() as conn:
        cursor = conn.cursor()
        # The watermarks must describe exactly the rows read below.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        # The table, not the cache: a copy registered a moment ago may still
        # be committing while the cache says nothing is owed.
        if has_backfill(cursor):
            raise ValueError("This worker is still receiving its transcripts")
        watermarks = store.local_watermarks(conn)
        for session_id, after, held in wanted:
            if budget <= 0:
                break
            items, last, more, used = _session_items(cursor, coll, session_id, after, budget, held=held)
            parts.append({"session_id": session_id, "after": after, "next": last, "more": more,
                          "chunk": engine.seal_snapshot_chunk(coll.name, after, items, more, ctx=ctx,
                                                              encoding="zlib")})
            budget -= used
    reply = {"format": 1, "network_id": ctx.network_id, "publisher": ctx.device_id, "recipient": recipient,
             "watermarks": watermarks, "parts": parts}
    reply["signature"] = crypto.b64u_encode(crypto.sign(ctx.identity.signing_private, crypto.canonical_json(reply)))
    return reply


def has_backfill(cursor) -> bool:
    """Whether transcripts are still owed, read in the caller's transaction."""
    cursor.execute("SELECT 1 FROM network_transcript_backfill_state WHERE id = 1")
    return cursor.fetchone() is not None


def _session_items(cursor, coll: policy.Collection, session_id: str, after: int, budget: int, *, held: int = 0):
    """One session's published events past ``after``: ``(items, last seq, more, bytes)``.

    Only events with a live sync record travel: an event the owner has not
    published yet reaches the network as an operation later. Up to ``held``
    (the highest seq the recipient holds), seqs without a row here are looked
    up for deleted records, which travel as deletions.
    """
    items: List[dict] = []
    probes: List[int] = []
    used = 0
    last = after

    def missing(low: int, high: int) -> range:
        """Seqs in (low, high] the recipient may hold."""
        return range(low + 1, min(high, held) + 1)

    def finish(more: bool):
        ids = [f"{session_id}|{seq}" for seq in probes]
        for start in range(0, len(ids), engine.SNAPSHOT_SQL_BATCH):
            found = store.records_for_ids(cursor, coll.name, ids[start:start + engine.SNAPSHOT_SQL_BATCH])
            for record_id in ids[start:start + engine.SNAPSHOT_SQL_BATCH]:
                version = found.get(record_id)
                if version is not None and version["deleted"]:
                    items.append(engine.snapshot_item(cursor, coll, version))
        return items, last, more, used

    while True:
        cursor.execute("SELECT * FROM session_events WHERE session_id = %s AND seq > %s ORDER BY seq LIMIT %s",
                       (session_id, last, SESSION_ROWS + 1))
        rows = [dict(row) for row in cursor.fetchall()]
        more_rows = len(rows) > SESSION_ROWS
        rows = rows[:SESSION_ROWS]
        ids = [coll.record_id(row) for row in rows]
        versions: Dict[str, dict] = {}
        for start in range(0, len(ids), engine.SNAPSHOT_SQL_BATCH):
            versions.update(store.records_for_ids(cursor, coll.name, ids[start:start + engine.SNAPSHOT_SQL_BATCH]))
        for row, record_id in zip(rows, ids):
            if used >= budget and (items or probes):
                return finish(True)
            seq = int(row["seq"])
            gap = missing(last, seq - 1)
            room = PROBE_LIMIT - len(probes)
            if len(gap) > room:
                probes.extend(gap[:room])
                last = probes[-1] if probes else last
                return finish(True)
            probes.extend(gap)
            last = seq
            version = versions.get(record_id)
            if version is None or version["deleted"]:
                continue
            item = engine.snapshot_item(cursor, coll, version, row=row, row_known=True)
            if item["deleted"]:
                continue
            used += len(envelope_mod.record_bytes(item["record"]))
            items.append(item)
        if more_rows:
            continue
        # Past the last row here: the recipient may still hold a retracted tail.
        tail = missing(last, held)
        room = PROBE_LIMIT - len(probes)
        if len(tail) > room:
            probes.extend(tail[:room])
            last = probes[-1] if probes else last
            return finish(True)
        probes.extend(tail)
        return finish(False)


# ---------------------------------------------------------------------------
# Receiving side
# ---------------------------------------------------------------------------

def _verify_reply(reply: Any, source: str, ctx: engine.NetworkContext, floor: dict, *,
                  relaxed: bool = False) -> list:
    if not isinstance(reply, dict) or not isinstance(reply.get("parts"), list):
        raise ValueError("Malformed transcript reply")
    member = ctx.member(source)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise ValueError("Transcript source is not an active member")
    unsigned = {k: v for k, v in reply.items() if k != "signature"}
    try:
        signed = crypto.verify(member.signing_public, crypto.canonical_json(unsigned),
                               crypto.b64u_decode(reply.get("signature") or ""))
    except Exception:  # noqa: BLE001 — anything that does not verify is refused
        signed = False
    if (not signed or reply.get("format") != 1 or reply.get("network_id") != ctx.network_id
            or reply.get("publisher") != source or reply.get("recipient") != ctx.device_id):
        raise ValueError("Transcript reply did not verify")
    held = reply.get("watermarks") if isinstance(reply.get("watermarks"), dict) else {}
    for origin, streams in floor.items():
        if origin == ctx.device_id:
            continue
        for incarnation, seq in (streams or {}).items():
            have = (held.get(origin) or {}).get(str(incarnation), 0)
            if int(have or 0) < int(seq):
                if not relaxed:
                    raise SourceBehind(f"{source} holds less than this worker's copy; it cannot fill the "
                                       "transcripts yet")
                logger.warning("No worker covering this copy answered for %d s; taking the transcripts %s holds "
                               "(it lacks operations of %s up to %s)", GIVE_UP_SECONDS, source, origin, seq)
                return reply["parts"]
    return reply["parts"]


def _runs_here(session: Any, ctx: engine.NetworkContext) -> bool:
    """The catalog publisher's rule: this worker runs the session (it is the owner)."""
    from app.network import catalog
    owner = session["owner_device_id"]
    return owner == ctx.device_id or (not owner and (session["instance_id"] or "") == catalog._my_instance_id())


def apply_reply(reply: Any, source: str, asked: Dict[str, int], *, generation: int,
                ctx: Optional[engine.NetworkContext] = None, relaxed: bool = False) -> dict:
    """Apply a source's reply, one transaction per session part. Returns counts.

    ``generation`` is the backfill the request was made for. A part is
    applied only while that generation is current: ``restarted`` in the
    result says a later copy replaced it and the rest was left alone.
    """
    ctx = ctx or engine.require_context()
    state = store.backfill_state()
    if state is None or state["generation"] != generation:
        return {"sessions": 0, "events": 0, "moved": 0, "restarted": True}
    parts = _verify_reply(reply, source, ctx, state["floor"], relaxed=relaxed)
    coll = policy.collection("session_events")
    sessions = events = moved = 0
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Malformed transcript reply")
        session_id, after, last, more = part.get("session_id"), part.get("after"), part.get("next"), part.get("more")
        if (not isinstance(session_id, str) or asked.get(session_id) != after or type(last) is not int
                or last < after or not isinstance(more, bool) or (more and last == after)):
            raise ValueError("Transcript reply does not answer the request")
        body = engine.open_snapshot_chunk(part.get("chunk") or {}, ctx=ctx)
        prefix = session_id + "|"
        if (body.get("collection") != coll.name or body.get("device_id") != source or body.get("offset") != after
                or body.get("more") != more
                or any(not str(item.get("record_id", "")).startswith(prefix) for item in body["items"])):
            raise ValueError("Transcript reply does not answer the request")
        touched: list = []
        with get_db() as conn:
            cursor = conn.cursor()
            # Lock order: network_state, the backfill state, then the session's row
            # (as a registration in a copy's last transaction takes them).
            store.lock_state(cursor)
            current = store.lock_backfill(cursor)
            if current is None or current["generation"] != generation:
                return {"sessions": sessions, "events": events, "moved": moved, "restarted": True}
            position = store.backfill_position(cursor, session_id)
            if position is None or position["done"] or int(position["after_seq"]) != after:
                continue  # Another pass already moved this session on.
            moved += 1
            cursor.execute("SELECT owner_device_id, instance_id FROM sessions WHERE id = %s", (session_id,))
            local = cursor.fetchone()
            if local is not None and not _runs_here(local, ctx):
                applied, touched = engine.apply_snapshot_items(cursor, coll, body["items"], ctx=ctx)
                events += applied
                store.backfill_advance(cursor, session_id, after_seq=last, done=not more)
            else:
                # Deleted since the copy (its events would be orphans), or
                # handed over to this worker, which now holds the transcript.
                store.backfill_advance(cursor, session_id, after_seq=last, done=True)
        if not more:
            sessions += 1
        if touched:
            engine._notify(touched)
    return {"sessions": sessions, "events": events, "moved": moved, "restarted": False}


def _next_requests() -> Tuple[Optional[int], List[dict]]:
    """``(generation, sessions to ask for)``, each with the highest seq held here."""
    with _lock:
        first = list(_wanted)
    with get_db() as conn:
        cursor = conn.cursor()
        state = store.backfill_state(conn)
        if state is None:
            return None, []
        picked = store.backfill_next(cursor, limit=REQUEST_SESSIONS, first=first)
        if picked:
            ids = [p["id"] for p in picked]
            cursor.execute("SELECT session_id, MAX(seq) AS s FROM session_events WHERE session_id IN "
                           f"({', '.join(['%s'] * len(ids))}) GROUP BY session_id", tuple(ids))
            held = {row["session_id"]: int(row["s"] or 0) for row in cursor.fetchall()}
            for p in picked:
                p["held"] = held.get(p["id"], 0)
    chosen = {p["id"] for p in picked}
    with _lock:
        # Requested now; forget the ones that are done (no longer pending).
        _wanted[:] = [sid for sid in _wanted if sid in chosen]
    return state["generation"], picked


def _note(**fields) -> None:
    with _lock:
        _progress.update(fields, updated_at=time.time())


def _sources(ctx: engine.NetworkContext) -> List[str]:
    """The copy's publisher first, then every other active member."""
    state = store.backfill_state()
    publisher = state["publisher"] if state else None
    others = [m.device_id for m in ctx.active_members() if m.device_id not in (ctx.device_id, publisher)]
    return ([publisher] if publisher and publisher != ctx.device_id else []) + others


def _link(device_id: str):
    from app.network import transport
    from app.network.endpoints import known_endpoints
    existing = transport._links.get(device_id)
    if existing is not None:
        endpoints = list(existing.endpoints) + [u for u in existing.lan_endpoints if u not in existing.endpoints]
    else:
        endpoints = known_endpoints(store.get_member(device_id), engine.presence_of(device_id))
    # A link of its own: the sync loop's link keeps its LAN session and token.
    return transport.PeerLink(device_id, endpoints)


async def run_once(client: httpx.AsyncClient) -> Optional[bool]:
    """Copy until done or no source can serve.

    True when every transcript arrived, False when no source could serve,
    None while the first copy itself is still being applied.
    """
    from app.network import transport
    ctx = engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return None
    if not await asyncio.to_thread(pending):
        return True
    state = await asyncio.to_thread(store.backfill_state)
    if state is None:
        _set_active(False)
        return True
    since = state.get("stalled_since")
    relaxed = since is not None and time.time() - int(since) >= GIVE_UP_SECONDS
    errors = []
    progressed = False
    for device_id in await asyncio.to_thread(_sources, ctx):
        link = await asyncio.to_thread(_link, device_id)
        if not link.endpoints and not link.lan_endpoints:
            continue
        _note(peer=device_id)
        try:
            while True:
                generation, requests = await asyncio.to_thread(_next_requests)
                if generation is None:
                    _set_active(False)
                    return True
                if not requests:
                    if await asyncio.to_thread(store.finish_backfill, generation):
                        _set_active(False)
                        logger.info("All transcripts from the first copy have arrived")
                        _note(error=None)
                        return True
                    continue  # A new copy registered meanwhile: its sessions are next.
                await link.open(client, ctx)
                reply = await link.call(client, ctx, "POST", PATH, json_body={
                    "sessions": requests, "max_bytes": REPLY_BYTES}, timeout=REQUEST_TIMEOUT)
                asked = {r["id"]: r["after"] for r in requests}
                counts = await asyncio.to_thread(apply_reply, reply, device_id, asked, generation=generation,
                                                 ctx=ctx, relaxed=relaxed)
                if counts["restarted"]:
                    continue
                if not counts["moved"]:
                    raise ValueError("The transcript copy made no progress")
                if not progressed:
                    progressed = True
                    await asyncio.to_thread(store.backfill_stalled, generation, stalled=False, now=int(time.time()))
                with _lock:
                    _progress["events"] = int(_progress.get("events") or 0) + counts["events"]
                _note(error=None)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, transport.PeerError, SourceBehind, ValueError, engine.ApplyError) as exc:
            reason = f"{exc}".strip() or type(exc).__name__
            errors.append(reason)
            logger.info("Transcript copy from %s stopped: %s", device_id, reason)
            continue
    if not progressed:
        # Nobody could serve this round: start (or keep) the give-up clock.
        await asyncio.to_thread(store.backfill_stalled, state["generation"], stalled=True, now=int(time.time()))
    if errors:
        _note(error=errors[0][:300])
    return False


async def loop() -> None:
    """Copy pending transcripts in the background; wake on a new copy or an opened session."""
    global _wake, _loop
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    failures = 0
    async with httpx.AsyncClient(follow_redirects=False) as client:
        while True:
            delay = IDLE_SECONDS
            try:
                if await asyncio.to_thread(pending):
                    outcome = await run_once(client)
                    if outcome:
                        failures = 0
                    elif outcome is False:
                        failures += 1
                        delay = min(RETRY_MAX_SECONDS, 2.0 ** min(failures, 7))
                    # None: not ready yet; ``finish_bootstrap`` wakes the loop.
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                failures += 1
                delay = min(RETRY_MAX_SECONDS, 2.0 ** min(failures, 7))
                logger.warning("Transcript copy failed", exc_info=True)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            _wake.clear()
