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
import contextvars
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
from app.network.endpoints import dial_endpoints, peer_endpoints, validate_endpoints
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
# A regular pass keeps pulling full batches for this long before it pushes.
SYNC_PULL_SECONDS = 3.0
# Set for the launch barrier's requests only (a context variable, so the
# background sync loop keeps its own timeouts): a dead address fails fast.
connect_timeout: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "peer_connect_timeout", default=None)

_wake: Optional[asyncio.Event] = None
_scan_wake: Optional[asyncio.Event] = None
_scan_requests_lock = threading.Lock()
_dirty_collections: set[str] = set()
# Catalog wakes name their sessions when the hook knows them. ``None`` means
# a catalog wake without ids (or too many): reconcile the whole catalog.
_dirty_sessions: Optional[set[str]] = set()
_full_scan_requested = False
# Sessions named by owner-side write hooks (event appends, prompts, run and
# background-task changes). They may name a mirrored session, so the scanner
# keeps only the ones this device owns before it narrows a catalog pass to
# them; ``None`` means too many to list: reconcile the whole catalog.
_owner_candidates: Optional[set[str]] = set()
_owner_collections: set[str] = set()
_tasks: List[asyncio.Task] = []
_links: Dict[str, "PeerLink"] = {}
_last_contact: Dict[str, float] = {}
# Our own advertised addresses as of the last sync pass; a change (the
# account tunnel appearing or going away) is announced to peers at once
# instead of at the next presence interval.
_advertised: Optional[List[str]] = None


class PeerError(RuntimeError):
    pass


class PeerTokenRejected(PeerError):
    """The peer rejected authentication before dispatching the action."""


# Failures while opening the connection (DNS, TCP connect, TLS handshake).
# No HTTP request was sent, so the peer saw nothing and the request can be
# tried on another address, even a mutation.
CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


class PeerConnectFailed(PeerError):
    """No address accepted a connection; no request reached the peer."""


# Longest detail kept from a peer's error response. Refusals pass through a
# second worker before reaching the UI, so this must fit a full sentence.
ERROR_DETAIL_LIMIT = 300


class PeerLink:
    """One peer: endpoint candidates, a cached token, and an HTTP client.

    ``endpoints`` is what ``dial_endpoints`` selects from the addresses the
    peer is known at: its https tunnel alone when it advertises one, its
    plain-http LAN address only when it does not.
    """

    def __init__(self, device_id: str, endpoints: list[str]):
        self.device_id = device_id
        # Member-advertised addresses are dialed: usable http(s) URLs, https only when present.
        self.endpoints = dial_endpoints(endpoints)
        self.base_url: str | None = None
        self.token: str | None = None
        self.token_expires = 0.0
        self.last_error: str | None = None
        self.backoff_until = 0.0
        self.failures = 0
        # Set once an inbound contact has cut a failure streak's backoff short;
        # cleared by the next successful sync. One early redial per streak.
        self.contact_kick_used = False
        self._sync_lock = asyncio.Lock()

    def update_endpoints(self, endpoints: list[str]) -> None:
        """Rebuild the dial list from the peer's current addresses.

        The address answering right now stays a candidate (a peer does not
        always list the address it was reached at, such as the invite address
        against Docker's guessed bridge IP); the rest is what the peer
        announced, or what its stored row and presence record say now, not
        what this link remembered, so an address the peer withdrew stops being
        dialed. ``dial_endpoints`` then applies the https rule to all of them:
        when the peer's tunnel appears, the plain-http connection is dropped
        and the next request authenticates over the tunnel.
        """
        candidates = ([self.base_url] if self.base_url else []) + list(endpoints)
        self.endpoints = dial_endpoints(candidates)
        if self.base_url is not None and self.base_url not in self.endpoints:
            self.base_url = None
            self.token = None

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, *, json_body=None,
                       params=None, auth: bool = True, timeout: float = REQUEST_TIMEOUT,
                       base: str | None = None) -> Any:
        """One request to the peer, at ``base`` or else the address in use."""
        base = base or self.base_url
        if base is None:
            raise PeerError("no endpoint")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + (self.token or "")
        seconds = 120 if path == "/api/v1/network/peer/state" else timeout
        connect = connect_timeout.get()
        response = await client.request(method, base + path, json=json_body, params=params,
                                        headers=headers,
                                        timeout=httpx.Timeout(seconds, connect=min(seconds, connect))
                                        if connect else seconds)
        if response.status_code == 401 and auth:
            self.token = None
            raise PeerTokenRejected("token rejected")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))[:ERROR_DETAIL_LIMIT]
            except Exception:  # noqa: BLE001
                pass
            raise PeerError(f"HTTP {response.status_code} {detail}".strip())
        return response.json()

    def routes(self) -> list[str]:
        """The dial addresses in the order to try them.

        The address the last successful authentication used comes first: the
        sync loop re-authenticates there every few minutes and exchanges over
        it every pass, so it is the one known to work.
        """
        first = [self.base_url] if self.base_url and self.base_url in self.endpoints else []
        return first + [e for e in self.endpoints if e not in first]

    async def authenticate(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *,
                           at: str | None = None) -> None:
        """Get a device token, trying each address in turn.

        With ``at``, authenticate at that address only (and use it from then
        on); a failure there leaves the address and token in use untouched.
        Raises ``PeerConnectFailed`` when no candidate accepted a connection.
        """
        if self.token and self.token_expires - 30 > time.time() and at in (None, self.base_url):
            return
        if at is not None:
            candidates = [at.rstrip("/")]
        else:
            candidates = [self.base_url] if self.base_url else []
            candidates += [e for e in self.endpoints if e != self.base_url]
        last: BaseException | None = None
        connect_only = True
        for endpoint in candidates:
            endpoint = endpoint.rstrip("/")
            try:
                token, expires = await self._issue_token(client, ctx, endpoint)
            except (httpx.HTTPError, PeerError, KeyError, ValueError, TypeError) as exc:
                last = exc
                connect_only = connect_only and isinstance(exc, CONNECT_ERRORS)
                continue
            self.base_url, self.token, self.token_expires = endpoint, token, expires
            return
        if at is None:
            self.base_url = None
            self.token = None
        message = f"{type(last).__name__}: {last}"[:200] if last is not None else "unreachable"
        if last is not None and connect_only:
            raise PeerConnectFailed(message) from last
        raise PeerError(message) from last

    async def _issue_token(self, client: httpx.AsyncClient, ctx: engine.NetworkContext,
                           endpoint: str) -> tuple[str, float]:
        """Challenge-response at ``endpoint``; checks it answers as this peer."""
        challenge = await self._request(client, "POST", "/api/v1/network/peer/auth/challenge",
                                        json_body={"device_id": ctx.device_id}, auth=False, base=endpoint)
        signature = tokens.sign_challenge(ctx.identity.signing_private, challenge["challenge"], self.device_id)
        result = await self._request(client, "POST", "/api/v1/network/peer/auth/token", json_body={
            "device_id": ctx.device_id,
            "challenge": challenge["challenge"],
            "signature": signature,
            "incarnation": ctx.identity.incarnation,
            **(await asyncio.to_thread(_description)),
            "version": settings.version,
            "platform": identity_mod.platform_name(),
        }, auth=False, base=endpoint)
        if result.get("device_id") != self.device_id or result.get("network_id") != ctx.network_id:
            raise PeerError("endpoint answered for another device")
        return result["token"], float(result.get("expires_at") or (time.time() + tokens.TOKEN_TTL_SECONDS))

    async def sync_once(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *, catch_up: bool = False) -> dict:
        async with self._sync_lock:
            return await self._sync_once(client, ctx, catch_up=catch_up)

    async def _sync_once(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *, catch_up: bool) -> dict:
        """Full exchange with this peer. Returns a small summary."""
        await self.authenticate(client, ctx)
        summary: dict[str, Any] = {"received": 0, "sent": 0}
        target = None
        marks = await asyncio.to_thread(store.local_watermarks)
        pull_deadline = time.monotonic() + SYNC_PULL_SECONDS
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
                # A backlog drains in one pass instead of one batch per
                # SYNC_ACTIVE_SECONDS; the time budget keeps the push below
                # from waiting behind a long pull.
                if (len(ops) < BATCH_LIMIT or exchange.get("snapshot_required")
                        or time.monotonic() >= pull_deadline):
                    break
                current = await asyncio.to_thread(store.local_watermarks)
                if current == marks:
                    break  # nothing applied; the same batch would come back
                marks = current
                continue
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
        # Stored addresses follow the same rule as dialed ones (``endpoints``).
        their_endpoints = exchange.get("endpoints")
        their_endpoints = validate_endpoints(their_endpoints) if isinstance(their_endpoints, list) else []
        capabilities = exchange.get("capabilities")
        await asyncio.to_thread(store.touch_member, self.device_id,
                                capabilities=capabilities if isinstance(capabilities, dict) else None,
                                endpoints=their_endpoints or None, version=exchange.get("version"),
                                incarnation=exchange.get("incarnation"))
        if their_endpoints:
            self.update_endpoints(their_endpoints)
            if self.base_url is None:
                # The peer's tunnel appeared: the plain-http connection is
                # dropped and the push below already goes over the tunnel.
                await self.authenticate(client, ctx)
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
        self.contact_kick_used = False
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
        endpoints = peer_endpoints(row, presence.get(device_id))
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
    """Called when a peer reached us (token, exchange), so liveness is symmetric.

    A peer that reaches us is up: end our backoff towards it (after it
    rebooted, our link would otherwise skip it for up to a minute) and sync
    now. Only a link in backoff kicks, so two healthy peers do not wake each
    other on every exchange. The failure count stays, and the early redial is
    granted once per failure streak: with one-way reachability (the peer can
    dial us, its advertised addresses are dead from here) every inbound call
    would otherwise restart a redial that times out and delays every other
    peer in the serial sync pass.
    """
    now = time.monotonic()
    _last_contact[device_id] = now
    link = _links.get(device_id)
    if link is None or link.contact_kick_used:
        return
    if link.backoff_until > now or link.failures:
        link.backoff_until = 0.0
        link.contact_kick_used = True
        engine.kick()


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

async def _announce_own_endpoints_if_changed() -> None:
    """Publish presence now when this worker's own addresses changed.

    Peers select the tunnel as soon as they learn of it and stop dialing it
    once it is withdrawn; both should not wait for the presence interval.
    The exchange carries the same list on every pass, but only to peers this
    worker reaches; the presence record also reaches peers that reach us.
    """
    global _advertised
    current = await asyncio.to_thread(engine._own_endpoints)
    if _advertised is None:
        _advertised = current
        return
    if current == _advertised:
        return
    _advertised = current
    capabilities = await asyncio.to_thread(_capabilities)
    await asyncio.to_thread(engine.publish_presence, capabilities=capabilities, version=settings.version,
                            endpoints=current)


async def sync_pass(client: httpx.AsyncClient) -> dict:
    ctx = engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return {}
    try:
        await _announce_own_endpoints_if_changed()
    except Exception:  # noqa: BLE001 — the presence loop publishes the change later
        logger.debug("Could not announce changed addresses", exc_info=True)
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
    global _scan_wake, _full_scan_requested, _dirty_sessions, _owner_candidates
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
            candidates = None if _owner_candidates is None else set(_owner_candidates)
            _owner_candidates = set()
            candidate_collections = set(_owner_collections)
            _owner_collections.clear()
        owner_wake = candidates is None or bool(candidates)
        full = full or (not dirty and not owner_wake) or time.monotonic() - last_full_scan >= SCAN_IDLE_MAX_SECONDS
        published = 0
        try:
            if full:
                published = await asyncio.to_thread(engine.scan_all)
            elif owner_wake:
                published = await asyncio.to_thread(
                    _owner_wake_pass, dirty=dirty, sessions=sessions, candidates=candidates,
                    candidate_collections=candidate_collections)
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


def _owner_wake_pass(*, dirty: set, sessions: Optional[set], candidates: Optional[set],
                     candidate_collections: set) -> int:
    """A narrow pass that also covers hooked owner writes (in the scan thread).

    Only sessions this device owns join the narrow catalog pass: a foreign
    id would widen ``catalog.publish_owned`` to the whole catalog.
    """
    from app.network import catalog as catalog_mod
    dirty = set(dirty)
    if candidates is None:
        dirty.update(candidate_collections)
        sessions = None
    else:
        owned = catalog_mod.owned_session_ids(candidates)
        if owned:
            if not dirty.intersection(policy.CATALOG_COLLECTIONS):
                sessions = set()
            dirty.update(candidate_collections)
            if sessions is not None:
                sessions.update(owned)
                if len(sessions) > SCAN_DIRTY_SESSIONS_MAX:
                    sessions = None
    if not dirty:
        return 0  # every named session is a mirror; its owner publishes it
    catalog = bool(dirty.intersection(policy.CATALOG_COLLECTIONS))
    narrow = {"session_ids": sorted(sessions)} if catalog and sessions else {}
    return engine.scan_all(collections=dirty, include_catalog=catalog, strict=True, **narrow)


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
                 session_ids: Optional[Iterable[str]] = None, owned_only: bool = False) -> None:
    """Coalesce shared/catalog writes; unknown changes reconcile fully.

    ``session_ids`` names the sessions a catalog write touched. Without it,
    the next catalog pass covers every session.

    ``owned_only`` is for post-commit hooks that cannot tell a mirrored
    session from an owned one without a database read (they may run on the
    event loop): the scanner drops the sessions this device does not own,
    and a wake that names none of its own sessions publishes nothing.
    """
    global _full_scan_requested, _dirty_sessions, _owner_candidates
    if owned_only:
        names = [name for name in (collections or ()) if name in policy.CATALOG_COLLECTIONS]
        ids = {sid for sid in (session_ids or ()) if isinstance(sid, str) and sid}
        if not names or not ids:
            return
        with _scan_requests_lock:
            _owner_collections.update(names)
            if _owner_candidates is not None:
                _owner_candidates.update(ids)
                if len(_owner_candidates) > SCAN_DIRTY_SESSIONS_MAX:
                    _owner_candidates = None
        _scan_kick_from_thread()
        return
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
    global _full_scan_requested, _dirty_sessions, _advertised, _owner_candidates
    forget_peers()
    _advertised = None
    with _scan_requests_lock:
        _dirty_collections.clear()
        _dirty_sessions = set()
        _full_scan_requested = False
        _owner_candidates = set()
        _owner_collections.clear()
