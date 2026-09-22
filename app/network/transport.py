"""Peer delivery over the worker API, and the loops that drive it.

The sync loop keeps one ``PeerLink`` per active member with a known
endpoint. Each pass, for every reachable peer:

    1. get (or reuse) a device token with a signed challenge
    2. ``exchange``: send our watermarks, receive what we lack, learn theirs
    3. push what the peer lacks from our log (any origin, forwarded verbatim)

A pass runs every ``SYNC_IDLE_SECONDS`` when nothing happens and immediately
after a local write (``engine.kick``), so two workers on a LAN converge
within a couple of seconds. The scanner publishes rows domain code changed
without calling ``engine.record`` itself.

Restore-from-backup detection: when a peer's watermark for our current
incarnation is ahead of our own log, our sequence went backwards. The device
moves to a new incarnation and starts a fresh stream.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.core.config import settings
from app.db import network as store
from app.network import engine, identity as identity_mod, membership, policy, tokens
from app.network.endpoints import validate_endpoints
from app.network.envelope import Envelope

logger = logging.getLogger(__name__)

SYNC_IDLE_SECONDS = 10.0
SYNC_ACTIVE_SECONDS = 1.0
SCAN_INTERVAL_SECONDS = 2.0
SCAN_IDLE_MAX_SECONDS = 60.0
# Past this many dirty sessions in one burst, a catalog wake reconciles the
# whole catalog instead of listing ids.
SCAN_DIRTY_SESSIONS_MAX = 128
PRESENCE_INTERVAL_SECONDS = 300.0
ONLINE_WINDOW_SECONDS = 90
BATCH_LIMIT = 500
REQUEST_TIMEOUT = 15.0

_wake: Optional[asyncio.Event] = None
_scan_wake: Optional[asyncio.Event] = None
_scan_requests_lock = threading.Lock()
_dirty_collections: set[str] = set()
# Catalog wakes name their sessions when the hook knows them. ``None`` means
# a catalog wake without ids (or too many): reconcile the whole catalog.
_dirty_sessions: Optional[set[str]] = set()
_full_scan_requested = False
_tasks: List[asyncio.Task] = []
_links: Dict[str, "PeerLink"] = {}
_last_contact: Dict[str, float] = {}


class PeerError(RuntimeError):
    pass


class PeerTokenRejected(PeerError):
    """The peer rejected authentication before dispatching the action."""


class PeerLink:
    """One peer: endpoint candidates, a cached token, and an HTTP client."""

    def __init__(self, device_id: str, endpoints: list[str]):
        self.device_id = device_id
        # Member-advertised addresses are dialed: keep only usable http(s) URLs.
        self.endpoints = validate_endpoints(endpoints)
        self.base_url: str | None = None
        self.token: str | None = None
        self.token_expires = 0.0
        self.last_error: str | None = None
        self.backoff_until = 0.0
        self.failures = 0
        self._sync_lock = asyncio.Lock()

    def update_endpoints(self, endpoints: list[str]) -> None:
        """Learn endpoints without dropping the one that is answering right now."""
        merged: list[str] = [self.base_url] if self.base_url else []
        for url in validate_endpoints(endpoints) + self.endpoints:
            url = (url or "").rstrip("/")
            if url and url not in merged:
                merged.append(url)
        self.endpoints = merged

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, *, json_body=None,
                       params=None, auth: bool = True, timeout: float = REQUEST_TIMEOUT) -> Any:
        if self.base_url is None:
            raise PeerError("no endpoint")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + (self.token or "")
        response = await client.request(method, self.base_url + path, json=json_body, params=params,
                                        headers=headers, timeout=120 if path == "/api/v1/network/peer/state" else timeout)
        if response.status_code == 401 and auth:
            self.token = None
            raise PeerTokenRejected("token rejected")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))[:120]
            except Exception:  # noqa: BLE001
                pass
            raise PeerError(f"HTTP {response.status_code} {detail}".strip())
        return response.json()

    async def authenticate(self, client: httpx.AsyncClient, ctx: engine.NetworkContext) -> None:
        if self.token and self.token_expires - 30 > time.time():
            return
        candidates = [self.base_url] if self.base_url else []
        candidates += [e for e in self.endpoints if e != self.base_url]
        last: str | None = None
        for endpoint in candidates:
            self.base_url = endpoint.rstrip("/")
            try:
                challenge = await self._request(client, "POST", "/api/v1/network/peer/auth/challenge",
                                                json_body={"device_id": ctx.device_id}, auth=False)
                signature = tokens.sign_challenge(ctx.identity.signing_private, challenge["challenge"], self.device_id)
                result = await self._request(client, "POST", "/api/v1/network/peer/auth/token", json_body={
                    "device_id": ctx.device_id,
                    "challenge": challenge["challenge"],
                    "signature": signature,
                    "incarnation": ctx.identity.incarnation,
                    **(await asyncio.to_thread(_description)),
                    "version": settings.version,
                    "platform": identity_mod.platform_name(),
                }, auth=False)
                if result.get("device_id") != self.device_id or result.get("network_id") != ctx.network_id:
                    raise PeerError("endpoint answered for another device")
                self.token = result["token"]
                self.token_expires = float(result.get("expires_at") or (time.time() + tokens.TOKEN_TTL_SECONDS))
                return
            except (httpx.HTTPError, PeerError, KeyError, ValueError, TypeError) as exc:
                last = f"{type(exc).__name__}: {exc}"[:200]
                self.token = None
                continue
        self.base_url = None
        raise PeerError(last or "unreachable")

    async def sync_once(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *, catch_up: bool = False) -> dict:
        async with self._sync_lock:
            return await self._sync_once(client, ctx, catch_up=catch_up)

    async def _sync_once(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *, catch_up: bool) -> dict:
        """Full exchange with this peer. Returns a small summary."""
        await self.authenticate(client, ctx)
        summary: dict[str, Any] = {"received": 0, "sent": 0}
        target = None
        marks = await asyncio.to_thread(store.local_watermarks)
        while True:
            exchange = await self._request(client, "POST", "/api/v1/network/peer/exchange", json_body={
                "watermarks": marks, "limit": BATCH_LIMIT, "incarnation": ctx.identity.incarnation,
                "flush": catch_up and target is None,
                **(await asyncio.to_thread(_description)), "version": settings.version,
            })
            theirs = exchange.get("watermarks") or {}
            if catch_up and target is None and not exchange.get("flushed"):
                raise PeerError("Update this worker to support startup sync")
            if target is None:
                # Catch up to a fixed frontier, not a continuously moving writer.
                target = {origin: dict(streams) for origin, streams in theirs.items() if origin != ctx.device_id}
            if exchange.get("snapshot_required"):
                summary["received"] += await self.receive_state(client, ctx)
                summary["snapshot_required"] = True
                ctx = engine.require_context()
            ops = exchange.get("ops") or []
            if ops:
                result = await asyncio.to_thread(engine.apply_batch, ops)
                summary["received"] += result.applied
                summary["apply"] = result.as_dict()
            if not catch_up:
                break
            current = await asyncio.to_thread(store.local_watermarks)
            caught_up = all(int(current.get(origin, {}).get(str(inc), 0)) >= int(seq)
                            for origin, streams in target.items() for inc, seq in streams.items())
            if caught_up:
                break
            if current == marks:
                raise PeerError("Peer sync could not fill missing updates")
            # This is already the next request's committed frontier. A later
            # local write can safely be advertised on the following exchange.
            marks = current
        if await self._check_rollback(client, ctx, theirs):
            ctx = engine.require_context()
        their_endpoints = exchange.get("endpoints") or []
        await asyncio.to_thread(store.touch_member, self.device_id, capabilities=exchange.get("capabilities"),
                                endpoints=their_endpoints or None, version=exchange.get("version"),
                                incarnation=exchange.get("incarnation"))
        if their_endpoints:
            self.update_endpoints(their_endpoints)
        # Push what the peer lacks, in bounded batches.
        while True:
            outbound, _, snapshot = await asyncio.to_thread(
                store.exchange_batch, theirs, limit=BATCH_LIMIT, exclude_origin=self.device_id)
            if snapshot:
                # The other worker's next pull requests our state. Do not send
                # an incomplete log prefix and pretend it was acknowledged.
                break
            if not outbound:
                break
            pushed = await self._request(client, "POST", "/api/v1/network/peer/ops", json_body={"ops": outbound})
            summary["sent"] += len(outbound)
            if pushed.get("retry") or pushed.get("pending"):
                # The peer kept some ops out of its stream for now; resend soon.
                summary["unsettled"] = True
            for env in outbound:
                bucket = theirs.setdefault(env["origin_device_id"], {})
                key = str(env["origin_incarnation"])
                bucket[key] = max(int(bucket.get(key, 0)), int(env["origin_seq"]))
            if len(outbound) < BATCH_LIMIT:
                break
            if pushed.get("rejected", 0) == len(outbound):
                summary["unsettled"] = True  # a full batch was refused; the rest follows on the next pass
                break
        if exchange.get("snapshot_required"):
            summary["snapshot_required"] = True
        self.failures = 0
        self.last_error = None
        return summary

    async def receive_state(self, client: httpx.AsyncClient, ctx: engine.NetworkContext) -> int:
        from app.network import peer_snapshot
        await self.authenticate(client, ctx)
        manifest = await self._request(client, "POST", "/api/v1/network/peer/state", json_body={})
        await asyncio.to_thread(peer_snapshot.verify_manifest, manifest, self.device_id)
        with tempfile.TemporaryDirectory(prefix="shuffle-peer-") as temporary:
            directory = Path(temporary)
            for index, expected in enumerate(manifest["pages"]):
                await self.authenticate(client, ctx)
                raw = bytearray()
                async with client.stream("GET", self.base_url + f"/api/v1/network/peer/state/{manifest['transfer']}/{index}",
                                         headers={"Authorization": "Bearer " + self.token}, timeout=60) as response:
                    if response.status_code != 200:
                        raise PeerError("Peer transfer interrupted; retrying from a verified state")
                    async for piece in response.aiter_bytes():
                        raw.extend(piece)
                        if len(raw) > peer_snapshot.MAX_PAGE:
                            raise PeerError("Peer state page exceeds transfer limit")
                if len(raw) != expected["bytes"]:
                    raise PeerError("Peer state page was truncated")
                await asyncio.to_thread((directory / f"{index}.json").write_bytes, raw)
            return await asyncio.to_thread(peer_snapshot.apply, manifest, directory, self.device_id)

    async def _check_rollback(self, client: httpx.AsyncClient, ctx: engine.NetworkContext,
                              theirs: Dict[str, Dict[str, int]]) -> bool:
        """A peer claims to hold more of our current stream than we do.

        The claim alone proves nothing (any member could say it and make us
        discard our unsynced writes). Ask for a retained op past our watermark:
        pruning may have removed both the first missing op and the head. An
        envelope that verifies under our own signing key proves that we were
        restored from a backup. Returns True after a bump.
        """
        mine = theirs.get(ctx.device_id) or {}
        remote = int(mine.get(str(ctx.identity.incarnation), 0) or 0)
        if not remote:
            return False

        def _held() -> int:
            with store.get_db() as conn:
                return store.own_watermark(conn.cursor(), ctx.device_id, ctx.identity.incarnation)

        local = await asyncio.to_thread(_held)
        if remote <= local:
            return False
        try:
            answer = await self._request(client, "GET",
                                         f"/api/v1/network/peer/ops/{ctx.device_id}/{ctx.identity.incarnation}/{local + 1}",
                                         params={"at_or_after": "true"})
            env = Envelope.from_dict(answer["op"])
        except (PeerError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Peer %s claims %d ops of our stream (we hold %d) but produced no proof: %s",
                           self.device_id, remote, local, exc)
            return False
        if (env.network_id != ctx.network_id or env.origin_device_id != ctx.device_id
                or env.origin_incarnation != ctx.identity.incarnation or env.origin_seq <= local
                or not env.verify_signature(ctx.identity.signing_public)):
            logger.warning("Peer %s offered an op of our stream that does not verify; claim ignored", self.device_id)
            return False
        recovered = await asyncio.to_thread(
            engine.restored_from_backup, env.origin_seq, env.origin_incarnation,
            evidence_network_id=env.network_id, evidence_device_id=env.origin_device_id,
        )
        if (recovered is None or recovered.network_id != env.network_id
                or recovered.device_id != env.origin_device_id
                or recovered.identity.incarnation <= env.origin_incarnation):
            return False
        await asyncio.to_thread(engine.publish_presence, capabilities=_capabilities(), version=settings.version)
        return True


def _description() -> dict:
    # Both probes can read the database, PATH and the filesystem.
    return {"endpoints": engine._own_endpoints(), "capabilities": _capabilities()}


def _capabilities() -> dict:
    try:
        from app.core.runner_capabilities import detect_capabilities
        return detect_capabilities()
    except Exception:  # noqa: BLE001
        return {}




# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------

async def _refresh_links(ctx: engine.NetworkContext) -> None:
    def _read():
        return engine.all_presence(), store.list_members(include_revoked=False)

    presence, members = await asyncio.to_thread(_read)
    # Only the database reads leave the loop; links/locks remain loop-owned.
    for row in members:
        device_id = row["device_id"]
        if device_id == ctx.device_id:
            continue
        endpoints = list(row.get("endpoints") or [])
        for url in (presence.get(device_id) or {}).get("endpoints") or []:
            if url not in endpoints:
                endpoints.append(url)
        link = _links.get(device_id)
        if link is None:
            _links[device_id] = PeerLink(device_id, endpoints)
        else:
            link.update_endpoints(endpoints)
    for device_id in list(_links):
        member = ctx.member(device_id)
        if member is None or member.status != membership.STATUS_ACTIVE:
            _links.pop(device_id, None)


def online_devices() -> Dict[str, float]:
    """Device id -> seconds since last successful contact, for recently seen peers."""
    now = time.monotonic()
    return {d: now - t for d, t in _last_contact.items() if now - t <= ONLINE_WINDOW_SECONDS}


def link_status() -> List[dict]:
    out = []
    for link in _links.values():
        age = None
        if link.device_id in _last_contact:
            age = round(time.monotonic() - _last_contact[link.device_id], 1)
        out.append({"device_id": link.device_id, "base_url": link.base_url, "endpoints": link.endpoints,
                    "last_error": link.last_error, "seconds_since_contact": age,
                    "online": age is not None and age <= ONLINE_WINDOW_SECONDS})
    return out


def note_contact(device_id: str) -> None:
    """Called when a peer reached us (token, exchange), so liveness is symmetric."""
    _last_contact[device_id] = time.monotonic()


def forward_pushed(sender: str) -> None:
    """Wake the sync loop for ops a peer pushed only if another peer may lack them.

    The sender exchanged frontiers with us just before pushing, so a pass back
    to it moves nothing: in a two-worker network every remote write used to
    cost the receiver one extra exchange.
    """
    if any(device_id != sender and link.endpoints for device_id, link in _links.items()):
        engine.kick()


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

async def sync_pass(client: httpx.AsyncClient) -> dict:
    ctx = engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return {}
    await _refresh_links(ctx)
    results: Dict[str, Any] = {}
    now = time.monotonic()
    for link in list(_links.values()):
        if not link.endpoints or link.backoff_until > now:
            continue
        try:
            results[link.device_id] = await link.sync_once(client, ctx)
            _last_contact[link.device_id] = time.monotonic()
            await asyncio.to_thread(store.upsert_peer, link.device_id, base_url=link.base_url, contacted=True,
                                    error=None)
        except (httpx.HTTPError, PeerError) as exc:
            link.failures += 1
            link.last_error = f"{type(exc).__name__}: {exc}"[:200]
            link.backoff_until = time.monotonic() + min(60.0, 2.0 * (2 ** min(link.failures, 5)))
            await asyncio.to_thread(store.upsert_peer, link.device_id, error=link.last_error)
            logger.debug("Peer %s unreachable: %s", link.device_id, link.last_error)
        except Exception:  # noqa: BLE001
            link.failures += 1
            link.backoff_until = time.monotonic() + 10.0
            logger.warning("Sync with %s failed", link.device_id, exc_info=True)
    if engine.outbox_held() and (results or not any(link.endpoints for link in _links.values())):
        # Our stream was compared with at least one peer (or there is no
        # peer to compare with): local writes may be published now.
        engine.hold_outbox(False)
        request_scan()
    return results


async def liveness_loop() -> None:
    """Invalidate readers on contact expiry even if sync is blocked or idle.

    Presence/runtime records are not contact evidence. This observes only the
    monotonic contact clock used by the runner directory, without DB reads.
    """
    from app.core.global_events import global_events

    previous: set[str] = set()
    while True:
        current = set(online_devices())
        if current != previous:
            for resource in ("network", "sessions"):
                for kind in ("admin", "api_user", "account", "paired"):
                    global_events.resources_changed(resource, user_type=kind)
            previous = current
        await asyncio.sleep(1.0)


async def sync_loop() -> None:
    global _wake
    _wake = asyncio.Event()
    engine.set_kick(_kick_from_thread)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        while True:
            try:
                results = await sync_pass(client)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("Sync pass failed", exc_info=True)
                results = {}
            # A push that landed completely needs no follow-up exchange: the
            # peer already compared frontiers with us before we pushed.
            busy = any((r.get("received") or r.get("unsettled")) for r in results.values())
            try:
                from app.network import blobs
                busy = bool(await blobs.fetch_missing(limit=4)) or busy
            except Exception:  # noqa: BLE001
                logger.debug("Peer file catch-up will retry", exc_info=True)
            timeout = SYNC_ACTIVE_SECONDS if busy else SYNC_IDLE_SECONDS
            try:
                await asyncio.wait_for(_wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            _wake.clear()


def _kick_from_thread() -> None:
    loop = _loop
    if loop is None or _wake is None:
        return
    try:
        loop.call_soon_threadsafe(_wake.set)
    except RuntimeError:
        pass


def _scan_kick_from_thread() -> None:
    loop = _loop
    if loop is None or _scan_wake is None:
        return
    try:
        loop.call_soon_threadsafe(_scan_wake.set)
    except RuntimeError:
        pass


_loop: Optional[asyncio.AbstractEventLoop] = None


async def scan_loop() -> None:
    """Back off quiet full scans; local writes wake us without a scan per write.

    Unhooked changes (including external file edits) still get a full scan:
    the next pass starts within SCAN_IDLE_MAX_SECONDS of the last completion.
    """
    global _scan_wake, _full_scan_requested, _dirty_sessions
    _scan_wake = asyncio.Event()
    delay = SCAN_INTERVAL_SECONDS
    last_full_scan = float("-inf")
    while True:
        # Snapshot before offloading. Writes during the scan remain queued for
        # the next pass, including writes to a collection already visited.
        _scan_wake.clear()
        with _scan_requests_lock:
            dirty = set(_dirty_collections)
            _dirty_collections.clear()
            sessions = None if _dirty_sessions is None else set(_dirty_sessions)
            _dirty_sessions = set()
            full = _full_scan_requested
            _full_scan_requested = False
        full = full or not dirty or time.monotonic() - last_full_scan >= SCAN_IDLE_MAX_SECONDS
        published = 0
        try:
            if full:
                published = await asyncio.to_thread(engine.scan_all)
            else:
                catalog = bool(dirty.intersection(policy.CATALOG_COLLECTIONS))
                narrow = {"session_ids": sorted(sessions)} if catalog and sessions else {}
                published = await asyncio.to_thread(
                    engine.scan_all, collections=dirty, include_catalog=catalog, strict=True, **narrow)
            if published:
                logger.debug("Published %d changed records", published)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Scan pass failed", exc_info=True)
            if not full:
                request_scan()  # Retry through full reconciliation after a failed narrow pass.
        if full:
            last_full_scan = time.monotonic()
        delay = (
            SCAN_INTERVAL_SECONDS if published
            else min(delay * 2, SCAN_IDLE_MAX_SECONDS)
        )
        # Targeted writes must not postpone discovery of external edits/deletes.
        delay = min(delay, max(SCAN_INTERVAL_SECONDS,
                               SCAN_IDLE_MAX_SECONDS - (time.monotonic() - last_full_scan)))
        # Coalesce streaming writes and requests arriving during the scan.
        # Even a slow scan or repeated failure must leave the worker some rest.
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        if not _scan_wake.is_set() and delay > SCAN_INTERVAL_SECONDS:
            try:
                await asyncio.wait_for(
                    _scan_wake.wait(), timeout=delay - SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
        if _scan_wake.is_set():
            delay = SCAN_INTERVAL_SECONDS


PRUNE_INTERVAL_SECONDS = 24 * 3600.0
_last_prune = 0.0


async def presence_loop() -> None:
    global _last_prune
    while True:
        try:
            capabilities = await asyncio.to_thread(_capabilities)
            await asyncio.to_thread(engine.publish_presence, capabilities=capabilities, version=settings.version)
            await asyncio.to_thread(store.expire_conflicts)
            await asyncio.to_thread(store.expire_joins)
            if time.monotonic() - _last_prune >= PRUNE_INTERVAL_SECONDS:
                # The op log is forwarded to peers that lack it; older ops that
                # are not a record's latest are served by snapshots instead.
                _last_prune = time.monotonic()
                pruned = await asyncio.to_thread(store.prune_ops)
                if pruned:
                    logger.info("Pruned %d old sync operations", pruned)
            from app.network import peer_snapshot
            await asyncio.to_thread(peer_snapshot.cleanup)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("Presence publish failed", exc_info=True)
        await asyncio.sleep(PRESENCE_INTERVAL_SECONDS)


def request_scan(collections: Optional[List[str]] = None, *,
                 session_ids: Optional[Iterable[str]] = None) -> None:
    """Coalesce shared/catalog writes; unknown changes reconcile fully.

    ``session_ids`` names the sessions a catalog write touched. Without it,
    the next catalog pass covers every session.
    """
    global _full_scan_requested, _dirty_sessions
    with _scan_requests_lock:
        if not collections or any(
                name not in policy.SCANNED_COLLECTIONS and name not in policy.CATALOG_COLLECTIONS
                for name in collections):
            _full_scan_requested = True
        else:
            _dirty_collections.update(collections)
            if any(name in policy.CATALOG_COLLECTIONS for name in collections) and _dirty_sessions is not None:
                ids = {sid for sid in (session_ids or ()) if isinstance(sid, str) and sid}
                _dirty_sessions.update(ids)
                if not ids or len(_dirty_sessions) > SCAN_DIRTY_SESSIONS_MAX:
                    _dirty_sessions = None
    _scan_kick_from_thread()


def start() -> List[asyncio.Task]:
    global _loop
    _loop = asyncio.get_running_loop()
    # Publish nothing until this device's stream has been compared with a
    # peer or the account feed (or there is nobody to compare with): a
    # worker restored from a backup must move to a new incarnation before it
    # reuses sequence numbers peers already hold under other content.
    engine.hold_outbox(True)
    _tasks[:] = [
        asyncio.create_task(sync_loop(), name="network-sync"),
        asyncio.create_task(scan_loop(), name="network-scan"),
        asyncio.create_task(presence_loop(), name="network-presence"),
        asyncio.create_task(liveness_loop(), name="network-liveness"),
    ]
    return list(_tasks)


async def stop() -> None:
    global _loop
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
    _loop = None
    engine.set_kick(None)
    engine.hold_outbox(False)


def forget_peers() -> None:
    """Drop every peer link and contact record (the worker left its network)."""
    _links.clear()
    _last_contact.clear()


def reset_for_tests() -> None:
    global _full_scan_requested, _dirty_sessions
    forget_peers()
    with _scan_requests_lock:
        _dirty_collections.clear()
        _dirty_sessions = set()
        _full_scan_requested = False
