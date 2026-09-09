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
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.db import network as store
from app.network import engine, identity as identity_mod, membership, tokens

logger = logging.getLogger(__name__)

SYNC_IDLE_SECONDS = 10.0
SYNC_ACTIVE_SECONDS = 1.0
SCAN_INTERVAL_SECONDS = 2.0
PRESENCE_INTERVAL_SECONDS = 300.0
ONLINE_WINDOW_SECONDS = 90
BATCH_LIMIT = 500
REQUEST_TIMEOUT = 15.0

_wake: Optional[asyncio.Event] = None
_scan_wake: Optional[asyncio.Event] = None
_tasks: List[asyncio.Task] = []
_links: Dict[str, "PeerLink"] = {}
_last_contact: Dict[str, float] = {}


class PeerError(RuntimeError):
    pass


class PeerLink:
    """One peer: endpoint candidates, a cached token, and an HTTP client."""

    def __init__(self, device_id: str, endpoints: List[str]):
        self.device_id = device_id
        self.endpoints = list(endpoints)
        self.base_url: Optional[str] = None
        self.token: Optional[str] = None
        self.token_expires = 0.0
        self.last_error: Optional[str] = None
        self.backoff_until = 0.0
        self.failures = 0

    def update_endpoints(self, endpoints: List[str]) -> None:
        """Learn endpoints without dropping the one that is answering right now."""
        merged: List[str] = [self.base_url] if self.base_url else []
        for url in list(endpoints or []) + self.endpoints:
            url = (url or "").rstrip("/")
            if url and url not in merged:
                merged.append(url)
        self.endpoints = merged

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, *, json_body=None,
                       params=None, auth: bool = True) -> Any:
        if self.base_url is None:
            raise PeerError("no endpoint")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + (self.token or "")
        response = await client.request(method, self.base_url + path, json=json_body, params=params,
                                        headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 401 and auth:
            self.token = None
            raise PeerError("token rejected")
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
        last: Optional[str] = None
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
                    "endpoints": engine._own_endpoints(),
                    "capabilities": _capabilities(),
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

    async def sync_once(self, client: httpx.AsyncClient, ctx: engine.NetworkContext) -> dict:
        """Full exchange with this peer. Returns a small summary."""
        await self.authenticate(client, ctx)
        marks = await asyncio.to_thread(store.local_watermarks)
        exchange = await self._request(client, "POST", "/api/v1/network/peer/exchange", json_body={
            "watermarks": marks, "limit": BATCH_LIMIT, "incarnation": ctx.identity.incarnation,
            "endpoints": engine._own_endpoints(), "capabilities": _capabilities(), "version": settings.version,
        })
        summary: Dict[str, Any] = {"received": 0, "sent": 0}
        ops = exchange.get("ops") or []
        if ops:
            result = await asyncio.to_thread(engine.apply_batch, ops)
            summary["received"] = result.applied
            summary["apply"] = result.as_dict()
        theirs = exchange.get("watermarks") or {}
        _detect_rollback(ctx, theirs)
        their_endpoints = exchange.get("endpoints") or []
        await asyncio.to_thread(store.touch_member, self.device_id, capabilities=exchange.get("capabilities"),
                                endpoints=their_endpoints or None, version=exchange.get("version"),
                                incarnation=exchange.get("incarnation"))
        if their_endpoints:
            self.update_endpoints(their_endpoints)
        # Push what the peer lacks, in bounded batches.
        while True:
            outbound = await asyncio.to_thread(store.ops_since, theirs, limit=BATCH_LIMIT,
                                               exclude_origin=self.device_id)
            if not outbound:
                break
            pushed = await self._request(client, "POST", "/api/v1/network/peer/ops", json_body={"ops": outbound})
            summary["sent"] += len(outbound)
            for env in outbound:
                bucket = theirs.setdefault(env["origin_device_id"], {})
                key = str(env["origin_incarnation"])
                bucket[key] = max(int(bucket.get(key, 0)), int(env["origin_seq"]))
            if len(outbound) < BATCH_LIMIT or pushed.get("rejected", 0) == len(outbound):
                break
        if exchange.get("snapshot_required"):
            summary["snapshot_required"] = True
        self.failures = 0
        self.last_error = None
        return summary


def _capabilities() -> dict:
    try:
        from app.core.runner_capabilities import detect_capabilities
        return detect_capabilities()
    except Exception:  # noqa: BLE001
        return {}


def _detect_rollback(ctx: engine.NetworkContext, theirs: Dict[str, Dict[str, int]]) -> None:
    """A peer knows more of our current stream than we do: we were restored from a backup."""
    mine = theirs.get(ctx.device_id) or {}
    remote = int(mine.get(str(ctx.identity.incarnation), 0) or 0)
    if not remote:
        return
    with store.get_db() as conn:
        local = store.own_watermark(conn.cursor(), ctx.device_id, ctx.identity.incarnation)
    if remote > local:
        logger.error("Peers hold %d operations from this device but only %d exist locally; "
                     "this worker was restored from a backup and re-enrolls with a new incarnation.", remote, local)
        identity_mod.bump_incarnation()
        engine.load_context()
        engine.publish_presence(capabilities=_capabilities(), version=settings.version)


# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------

def _refresh_links(ctx: engine.NetworkContext) -> None:
    presence = engine.all_presence()
    for row in store.list_members(include_revoked=False):
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


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

async def sync_pass(client: httpx.AsyncClient) -> dict:
    ctx = engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return {}
    _refresh_links(ctx)
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
    return results


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
            busy = any((r.get("received") or r.get("sent")) for r in results.values())
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
    """Publish rows domain code changed; runs every few seconds and on demand."""
    global _scan_wake
    _scan_wake = asyncio.Event()
    while True:
        try:
            published = await asyncio.to_thread(engine.scan_all)
            if published:
                logger.debug("Published %d changed records", published)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Scan pass failed", exc_info=True)
        try:
            await asyncio.wait_for(_scan_wake.wait(), timeout=SCAN_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _scan_wake.clear()


async def presence_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(engine.publish_presence, capabilities=_capabilities(), version=settings.version)
            await asyncio.to_thread(store.expire_conflicts)
            await asyncio.to_thread(store.expire_joins)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("Presence publish failed", exc_info=True)
        # Blob hydration rides the same cadence, plus a short retry while bytes are missing.
        try:
            from app.network import blobs
            fetched = await blobs.fetch_missing()
            if fetched:
                logger.info("Fetched %d blob(s) from peers", fetched)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("Blob fetch failed", exc_info=True)
        await asyncio.sleep(PRESENCE_INTERVAL_SECONDS)


def request_scan() -> None:
    """Domain code changed something: scan soon, then sync."""
    _scan_kick_from_thread()


def start() -> List[asyncio.Task]:
    global _loop
    _loop = asyncio.get_running_loop()
    _tasks[:] = [
        asyncio.create_task(sync_loop(), name="network-sync"),
        asyncio.create_task(scan_loop(), name="network-scan"),
        asyncio.create_task(presence_loop(), name="network-presence"),
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


def forget_peers() -> None:
    """Drop every peer link and contact record (the worker left its network)."""
    _links.clear()
    _last_contact.clear()


def reset_for_tests() -> None:
    forget_peers()
