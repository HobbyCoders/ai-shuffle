"""Peer delivery over the worker API, and the loops that drive it.

The sync loop keeps one ``PeerLink`` per active member with a known
endpoint. Each pass, for every reachable peer:

    1. get (or reuse) a device token with a signed challenge
    2. ``exchange``: send our watermarks, receive what we lack, learn theirs
    3. push what the peer lacks from our log (any origin, forwarded verbatim)

A pass runs immediately after a local write (``engine.kick``), so two workers
on a LAN converge within a couple of seconds. When nothing happens the wait
grows from ``SYNC_IDLE_SECONDS`` to ``SYNC_IDLE_MAX_SECONDS``. The scanner publishes rows domain code changed
without calling ``engine.record`` itself.

Restore-from-backup detection: when a peer's watermark for our current
incarnation is ahead of our own log, our sequence went backwards. The device
moves to a new incarnation and starts a fresh stream.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.core.config import settings
from app.db import network as store
from app.network import catalog, engine, identity as identity_mod, lan, membership, policy, tokens, transcripts
from app.network.crypto import CryptoError
from app.network.endpoints import dial_endpoints, known_endpoints, peer_endpoints, validate_endpoints
from app.network.envelope import Envelope

logger = logging.getLogger(__name__)

SYNC_IDLE_SECONDS = 10.0
# Each quiet pass doubles the wait up to this; any activity or wake resets it.
# It stays well inside ONLINE_WINDOW_SECONDS so connected peers still read as online.
SYNC_IDLE_MAX_SECONDS = 30.0
SYNC_ACTIVE_SECONDS = 1.0
# A peer that does not answer is retried after an exponentially growing,
# jittered wait (half fixed, half random) of at most this long. A success,
# a change of its addresses or of the membership ends the wait.
PEER_BACKOFF_BASE_SECONDS = 2.0
PEER_BACKOFF_MAX_SECONDS = 900.0
SCAN_INTERVAL_SECONDS = 2.0
# While a transcript backlog remains, the scanner rests as long as the pass
# took (at least this long, at most SCAN_INTERVAL_SECONDS): about half its
# time publishing instead of a third, and never back to back (#1978).
SCAN_BACKLOG_REST_SECONDS = 0.5
SCAN_IDLE_MAX_SECONDS = 60.0
# Past this many dirty sessions in one burst, a catalog wake reconciles the
# whole catalog instead of listing ids.
SCAN_DIRTY_SESSIONS_MAX = 128
PRESENCE_INTERVAL_SECONDS = 300.0
ONLINE_WINDOW_SECONDS = 90
BATCH_LIMIT = 500
REQUEST_TIMEOUT = 15.0
STATE_REQUEST_TIMEOUT = 120.0  # Older inviters still capture inside the request.
STATE_PREPARE_SECONDS = 30 * 60.0
STATE_POLL_SECONDS = 1.0
STATE_POLL_MAX_SECONDS = 10.0
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
_links_generation: Optional[int] = None
_last_contact: Dict[str, float] = {}
# Authenticated peer frontiers, retained across failed/retried pulls. A failed
# request does not make a known backlog disappear. Only committed watermarks do.
_peer_watermarks: Dict[str, dict] = {}
_snapshot_peers: set[str] = set()
_peer_catalog_backlog: Dict[str, bool] = {}
# Progress of the latest full state copy this worker received (a new worker's
# first copy, or a re-copy a peer asked for). Status and the launch screen
# read it; a first copy can run for many minutes with nothing else to show.
_state_copy: Optional[dict] = None
# A state copy's apply, by publisher, until a caller has taken its result.
_applying: Dict[str, asyncio.Future] = {}


def note_watermarks(device_id: str, watermarks: dict, *, catalog_backlog: bool = False) -> None:
    _peer_watermarks[device_id] = {origin: dict(streams) for origin, streams in watermarks.items()}
    _peer_catalog_backlog[device_id] = catalog_backlog


def state_copy_status() -> Optional[dict]:
    """The latest state copy's progress, or None when none ran in this process.

    ``phase`` is preparing (the peer is capturing), downloading, applying,
    done or failed (``error`` says why; the caller retries).
    """
    return dict(_state_copy) if _state_copy else None


def _copy_begin(device_id: str, ctx: Any) -> None:
    global _state_copy
    try:
        member = ctx.member(device_id)
    except Exception:  # noqa: BLE001
        member = None
    _state_copy = {"peer": device_id, "peer_name": getattr(member, "name", None) or None,
                   "phase": "preparing", "pages_done": 0, "pages_total": 0, "bytes_done": 0,
                   "bytes_total": 0, "resumed_pages": 0, "error": None, "started_at": time.time(),
                   "updated_at": time.time()}


def _apply_received(manifest: dict, publisher: str) -> tuple[dict, int]:
    from app.network import peer_snapshot

    def applied(done: int, total: int) -> None:
        _copy_progress(apply_done=done, apply_total=total)

    return manifest, peer_snapshot.apply_received(manifest, publisher, on_progress=applied)


def _copy_progress(**fields) -> None:
    global _state_copy
    current = dict(_state_copy or {})
    current.update(fields, updated_at=time.time())
    _state_copy = current


def owner_sync_status(owner_device_id: str | None) -> dict:
    """Conservative owner-wide freshness, not a claim about individual events."""
    ctx = engine.context()
    if not ctx or not owner_device_id or owner_device_id == ctx.device_id:
        return {"state": "current", "pending_updates": 0}
    heads: dict[str, int] = {}
    for watermarks in list(_peer_watermarks.values()):
        for incarnation, seq in watermarks.get(owner_device_id, {}).items():
            heads[str(incarnation)] = max(heads.get(str(incarnation), 0), int(seq))
    local = store.local_watermarks().get(owner_device_id, {})
    pending = sum(max(0, seq - int(local.get(inc, 0))) for inc, seq in heads.items())
    snapshot = owner_device_id in _snapshot_peers or any(
        peer in _snapshot_peers and owner_device_id in marks
        for peer, marks in list(_peer_watermarks.items()))
    if pending or snapshot or _peer_catalog_backlog.get(owner_device_id):
        state = "syncing"
    elif heads:
        state = "current"
    else:
        state = "checking" if owner_device_id in online_devices() else "offline"
    return {"state": state, "pending_updates": pending}

# Peers our own sync pass has tried (reached or not) since the startup outbox
# hold began. A peer's inbound view opens the hold only once every other peer
# we can dial is in here (``release_hold_for_peer_view``).
_tried_since_hold: set[str] = set()
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


class PeerResponseTooLarge(PeerError):
    """A peer's answer exceeded the size the caller accepts."""


class PeerStateUnavailable(PeerError):
    """The peer cannot hand out a state copy now: it is still receiving its own."""


def _peer_error(status: int, detail: str) -> PeerError:
    message = f"HTTP {status} {detail}".strip()
    if status == 409 and "still receiving" in detail:
        return PeerStateUnavailable(message)
    return PeerError(message)


class _TransferGone(PeerError):
    """The peer no longer serves a state transfer (it expired or was deleted)."""


# Longest detail kept from a peer's error response. Refusals pass through a
# second worker before reaching the UI, so this must fit a full sentence.
ERROR_DETAIL_LIMIT = 300


class PeerLink:
    """One peer: endpoint candidates, a sealed LAN session or a cached token.

    ``endpoints`` is what ``dial_endpoints`` selects from the addresses the
    peer is known at for device-token requests: its https tunnel alone when
    it advertises one, its plain-http address only when it does not.
    ``lan_endpoints`` are its private LAN addresses, dialed first through a
    sealed session (``lan``) whenever the peer supports one; every failure
    there falls back to ``endpoints`` at once (``open``, ``call``).
    """

    def __init__(self, device_id: str, endpoints: list[str]):
        self.device_id = device_id
        # Member-advertised addresses are dialed: usable http(s) URLs, https only when present.
        self.endpoints = dial_endpoints(endpoints)
        self.lan_endpoints = lan.lan_candidates(endpoints)
        self.lan: lan.ClientSession | None = None
        # Monotonic time before which the LAN is not tried again after it failed.
        self.lan_retry_at = 0.0
        self.lan_last_error: str | None = None
        self.base_url: str | None = None
        self.token: str | None = None
        self.token_expires = 0.0
        self.last_error: str | None = None
        self.backoff_until = 0.0
        self.failures = 0
        # Set once an inbound contact has cut a failure streak's backoff short;
        # cleared by the next successful sync. One early redial per streak.
        self.contact_kick_used = False
        # Batches this link moved in either direction (pulled with ops, or
        # pushed). The launch barrier compares it across its time limit to
        # tell a peer that is still delivering from one that never answered.
        self.batches = 0
        self.syncing = False
        self._sync_lock = asyncio.Lock()
        self._announced = tuple(self.endpoints)

    def reset_backoff(self) -> None:
        self.failures = 0
        self.backoff_until = 0.0
        self.contact_kick_used = False

    def note_failure(self) -> None:
        """Back off before the next attempt; the first failure of a streak also
        asks the account directory for the peer's current address."""
        self.failures += 1
        self.backoff_until = time.monotonic() + peer_backoff_seconds(self.failures)
        if self.failures == 1:
            try:
                from app.network import hub
                hub.request_directory_refresh()
            except Exception:  # the regular directory poll follows
                logger.debug("Could not request a directory refresh", exc_info=True)

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
        announced = tuple(dial_endpoints(endpoints))
        if announced != self._announced:
            # New addresses may answer where the old ones did not.
            self._announced = announced
            self.reset_backoff()
        candidates = ([self.base_url] if self.base_url else []) + list(endpoints)
        self.endpoints = dial_endpoints(candidates)
        if self.base_url is not None and self.base_url not in self.endpoints:
            self.base_url = None
            self.token = None
        local = lan.lan_candidates(endpoints)
        if local != self.lan_endpoints:
            # A changed LAN address is worth trying at once; a withdrawn one
            # is not dialed again, even through a session already open.
            self.lan_endpoints = local
            self.lan_retry_at = 0.0
            if self.lan is not None and self.lan.base not in local:
                self.lan = None

    @property
    def active_url(self) -> str | None:
        """The address sync traffic goes to right now."""
        return self.lan.base if self.lan is not None else self.base_url

    @property
    def route(self) -> str | None:
        """``lan`` (sealed, direct), ``tunnel`` (https), ``direct`` (plain-http token), or None."""
        if self.lan is not None:
            return "lan"
        if self.base_url is None:
            return None
        return "tunnel" if self.base_url.startswith("https://") else "direct"

    async def open(self, client: httpx.AsyncClient, ctx: engine.NetworkContext) -> None:
        """Make a route ready: a sealed LAN session when one works, else a device token.

        The LAN is tried first when the peer advertises a private address,
        LAN-direct is on here and it did not fail in the last
        ``lan.LAN_REPROBE_SECONDS``. Any failure there is silent and the
        device token path (the tunnel) is used straight away.
        """
        now = time.monotonic()
        if self.lan is not None:
            if self.lan.usable(now) and self.lan.base in self.lan_endpoints:
                return
            self.lan = None
        if self.lan_endpoints and now >= self.lan_retry_at and await self._open_lan(client, ctx):
            return
        await self.authenticate(client, ctx)

    async def _open_lan(self, client: httpx.AsyncClient, ctx: engine.NetworkContext) -> bool:
        if not await asyncio.to_thread(lan.lan_direct_enabled):
            return False
        last: BaseException | None = None
        for base in self.lan_endpoints[:lan.LAN_PROBE_ADDRESSES]:
            try:
                self.lan = await lan.handshake(client, ctx, self.device_id, base,
                                               connect_limit=connect_timeout.get())
            except lan.LanIdentityMismatch as exc:
                logger.warning("LAN address %s of peer %s refused: %s", base, self.device_id, exc)
                last = exc
                continue
            except (lan.LanError, httpx.HTTPError, CryptoError) as exc:
                last = exc
                continue
            self.lan_retry_at = 0.0
            self.lan_last_error = None
            logger.debug("Peer %s reached directly on the LAN at %s", self.device_id, base)
            return True
        self._lan_failed(last)
        return False

    def _lan_failed(self, exc: BaseException | None) -> None:
        """Leave the LAN for the tunnel until the next re-probe (or an address change)."""
        if self.lan is not None:
            logger.info("LAN path to peer %s failed (%s); using the tunnel", self.device_id, exc)
        self.lan = None
        self.lan_retry_at = time.monotonic() + lan.LAN_REPROBE_SECONDS
        self.lan_last_error = f"{type(exc).__name__}: {exc}"[:200] if exc is not None else "unreachable"

    async def _lan_exchange(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, method: str, path: str,
                            *, json_body=None, params=None, timeout: float = REQUEST_TIMEOUT,
                            max_bytes: int = lan.REPLY_MAX_BYTES) -> tuple[int, str, bytes] | None:
        """One call through the LAN session, or None when the tunnel must take it.

        A session the peer no longer knows (it restarted, the membership
        changed) is reopened once on the LAN. Every request sent this way is
        safe to repeat over the tunnel: reads, idempotent op batches, the
        exchange and the state preparation.
        """
        for attempt in range(2):
            if self.lan is None:
                return None
            try:
                return await lan.sealed_call(client, self.lan, method, path, json_body=json_body, params=params,
                                             timeout=timeout, max_bytes=max_bytes,
                                             connect_limit=connect_timeout.get())
            except lan.LanSessionRejected as exc:
                self.lan = None
                if attempt or not await self._open_lan(client, ctx):
                    if self.lan_last_error is None:
                        self._lan_failed(exc)
                    return None
            except lan.LanError as exc:
                self._lan_failed(exc)
                return None
        return None

    async def call(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, method: str, path: str, *,
                   json_body=None, params=None, timeout: float = REQUEST_TIMEOUT) -> Any:
        """A sync request to the peer: through the LAN session when open, else with a device token."""
        if self.lan is not None:
            answer = await self._lan_exchange(client, ctx, method, path, json_body=json_body, params=params,
                                              timeout=timeout)
            if answer is not None:
                status, _type, raw = answer
                return _decode_answer(status, raw)
        await self.authenticate(client, ctx)
        return await self._request(client, method, path, json_body=json_body, params=params, timeout=timeout)

    async def fetch_bytes(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, path: str, *,
                          limit: int, timeout: float = 60.0) -> tuple[int, bytes]:
        """GET a byte body (a snapshot page, a file part) of at most ``limit`` bytes: ``(status, body)``."""
        if self.lan is not None:
            answer = await self._lan_exchange(client, ctx, "GET", path, timeout=timeout, max_bytes=limit)
            if answer is not None:
                status, _type, raw = answer
                if len(raw) > limit:
                    raise PeerResponseTooLarge("Peer answer exceeds the transfer limit")
                return status, (raw if status == 200 else b"")
        await self.authenticate(client, ctx)
        raw = bytearray()
        async with client.stream("GET", self.base_url + path, headers={"Authorization": "Bearer " + self.token},
                                 timeout=timeout) as response:
            if response.status_code != 200:
                return response.status_code, b""
            async for piece in response.aiter_bytes():
                raw.extend(piece)
                if len(raw) > limit:
                    raise PeerResponseTooLarge("Peer answer exceeds the transfer limit")
        return 200, bytes(raw)

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
        seconds = timeout
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
            raise _peer_error(response.status_code, detail)
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
            self.syncing = catch_up
            try:
                return await self._sync_once(client, ctx, catch_up=catch_up)
            finally:
                self.syncing = False

    async def _sync_once(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *, catch_up: bool) -> dict:
        """Full exchange with this peer. Returns a small summary."""
        await self.open(client, ctx)
        summary: dict[str, Any] = {"received": 0, "sent": 0}
        target = None
        marks = await asyncio.to_thread(store.local_watermarks)
        pull_deadline = time.monotonic() + SYNC_PULL_SECONDS
        while True:
            exchange = await self.call(client, ctx, "POST", "/api/v1/network/peer/exchange", json_body={
                "watermarks": marks, "limit": BATCH_LIMIT, "incarnation": ctx.identity.incarnation,
                "flush": catch_up and target is None,
                "catalog_backlog": await asyncio.to_thread(catalog.has_events_backlog, ctx=ctx),
                **(await asyncio.to_thread(_description)), "version": settings.version,
                "platform": identity_mod.platform_name(),
            })
            theirs = exchange.get("watermarks") or {}
            note_watermarks(self.device_id, theirs, catalog_backlog=bool(exchange.get("catalog_backlog")))
            if not exchange.get("snapshot_required"):
                _snapshot_peers.discard(self.device_id)
            if catch_up and target is None and not exchange.get("flushed"):
                raise PeerError("Update this worker to support startup sync")
            if target is None:
                # Catch up to a fixed frontier, not a continuously moving writer.
                target = {origin: dict(streams) for origin, streams in theirs.items() if origin != ctx.device_id}
            if exchange.get("snapshot_required"):
                summary["snapshot_required"] = True
                try:
                    summary["received"] += await self.receive_state(client, ctx)
                except PeerStateUnavailable as exc:
                    # The peer is still filling its own copy (its transcripts,
                    # say). Keep exchanging operations; ask again next pass.
                    logger.info("Peer %s cannot hand out a state copy yet: %s", self.device_id, exc)
                    summary["snapshot_deferred"] = True
                ctx = engine.require_context()
            ops = exchange.get("ops") or []
            if ops:
                self.syncing = True
                result = await asyncio.to_thread(engine.apply_batch, ops)
                summary["received"] += result.applied
                summary["apply"] = result.as_dict()
                self.batches += 1
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
        # Do not wait for the scanner's next wake or another network pass.
        # Existing catalog cursors survive the hold; publish only their tail.
        if engine.outbox_held():
            released = await asyncio.to_thread(release_hold_for_peer_view, self.device_id,
                                               theirs.get(ctx.device_id) or {})
            if released:
                self.syncing = True
                await asyncio.to_thread(catalog.publish_owned)
        # Stored addresses follow the same rule as dialed ones (``endpoints``).
        their_endpoints = exchange.get("endpoints")
        their_endpoints = validate_endpoints(their_endpoints) if isinstance(their_endpoints, list) else []
        capabilities = exchange.get("capabilities")
        await asyncio.to_thread(store.touch_member, self.device_id,
                                capabilities=capabilities if isinstance(capabilities, dict) else None,
                                endpoints=their_endpoints or None, version=exchange.get("version"),
                                incarnation=exchange.get("incarnation"))
        if their_endpoints:
            # When the peer's tunnel appeared, the plain-http token is dropped
            # and the push below authenticates over the tunnel (``call``).
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
            self.syncing = True
            pushed = await self.call(client, ctx, "POST", "/api/v1/network/peer/ops", json_body={"ops": outbound})
            summary["sent"] += len(outbound)
            self.batches += 1
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

    async def receive_state(self, client: httpx.AsyncClient, ctx: engine.NetworkContext,
                            *, on_wait=None) -> int:
        """Copy the peer's full state and apply it; progress shows in ``state_copy_status``."""
        _copy_begin(self.device_id, ctx)
        try:
            applied = await self._receive_state(client, ctx, on_wait=on_wait)
        except Exception as exc:
            reason = f"{exc}".strip() or type(exc).__name__
            _copy_progress(phase="failed", error=reason[:ERROR_DETAIL_LIMIT])
            raise
        _copy_progress(phase="done", error=None)
        return applied

    async def _receive_state(self, client: httpx.AsyncClient, ctx: engine.NetworkContext,
                             *, on_wait=None) -> int:
        from app.network import peer_snapshot
        _snapshot_peers.add(self.device_id)
        running = _applying.get(self.device_id)
        if running is not None:
            # An earlier attempt was cancelled (the launch barrier's time
            # limit) while its copy was being applied in a thread. Take that
            # result instead of copying again.
            _copy_progress(phase="applying")
            try:
                manifest, applied = await asyncio.shield(running)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the apply failed; copy again below
                logger.info("Earlier state copy from %s failed to apply; copying again", self.device_id)
            else:
                return self._received_state(manifest, applied)
            finally:
                if running.done() and _applying.get(self.device_id) is running:
                    del _applying[self.device_id]
        saved = await asyncio.to_thread(peer_snapshot.saved_transfer, self.device_id)
        if saved is not None:
            # Resume the saved transfer: the peer serves it until its signed
            # expiry, and a new request would usually start a new capture.
            try:
                return await self._download_state(client, ctx, saved)
            except _TransferGone:
                await asyncio.to_thread(peer_snapshot.discard_received, saved["transfer"])
                logger.info("Peer %s no longer serves the saved state copy; requesting a new one", self.device_id)
                _copy_progress(phase="preparing", pages_done=0, pages_total=0, bytes_done=0, bytes_total=0,
                               resumed_pages=0)
        manifest = await self._request_state(client, ctx, on_wait=on_wait)
        await asyncio.to_thread(peer_snapshot.verify_manifest, manifest, self.device_id)
        try:
            return await self._download_state(client, ctx, manifest)
        except _TransferGone as exc:
            await asyncio.to_thread(peer_snapshot.discard_received, manifest["transfer"])
            raise PeerError("Peer transfer interrupted; retrying from a verified state") from exc

    async def _request_state(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, *, on_wait=None) -> dict:
        """Ask the peer for a state manifest, polling while it captures one."""
        from app.network import peer_snapshot
        deadline = time.monotonic() + STATE_PREPARE_SECONDS
        delay = STATE_POLL_SECONDS
        try:
            # Bound authentication as well as polling, and reject even a ready
            # manifest if its request outlasts the preparation budget. Downloads
            # have their own per-page limits outside this preparation timeout.
            async with asyncio.timeout(STATE_PREPARE_SECONDS):
                while True:
                    await self.open(client, ctx)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PeerError("Timed out waiting for peer state capture")
                    # Older inviters ignore the query parameters and return a
                    # format 1 manifest directly. Keep their 120s request
                    # allowance. Format 2 leaves transcripts for later.
                    manifest = await self.call(
                        client, ctx, "POST", "/api/v1/network/peer/state", json_body={},
                        params={"prepare": "true", "format": str(peer_snapshot.FORMAT_DEFERRED)},
                        timeout=min(STATE_REQUEST_TIMEOUT, remaining))
                    if time.monotonic() >= deadline:
                        raise PeerError("Timed out waiting for peer state capture")
                    if manifest.get("status") != "preparing":
                        return manifest
                    if on_wait:
                        on_wait(f"Waiting for peer {self.device_id} to prepare your settings copy")
                    await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
                    delay = min(delay * 2, STATE_POLL_MAX_SECONDS)
        except TimeoutError as exc:
            raise PeerError("Timed out waiting for peer state capture") from exc

    async def _download_state(self, client: httpx.AsyncClient, ctx: engine.NetworkContext, manifest: dict) -> int:
        """Download the pages missing on disk, then apply the whole copy."""
        from app.network import peer_snapshot
        pages = manifest["pages"]
        directory = await asyncio.to_thread(peer_snapshot.begin_received, manifest)
        _copy_progress(phase="downloading", pages_total=len(pages), pages_done=0, bytes_done=0,
                       bytes_total=sum(int(page["bytes"]) for page in pages), resumed_pages=0)
        done_bytes = kept = 0
        for index, expected in enumerate(pages):
            if await asyncio.to_thread(peer_snapshot.has_page, directory, index, expected):
                kept += 1
            else:
                await self.open(client, ctx)
                try:
                    status, raw = await self.fetch_bytes(
                        client, ctx, f"/api/v1/network/peer/state/{manifest['transfer']}/{index}",
                        limit=peer_snapshot.MAX_PAGE, timeout=60)
                except PeerResponseTooLarge as exc:
                    raise PeerError("Peer state page exceeds transfer limit") from exc
                if status in (404, 409, 410):
                    # Expired or deleted on the peer: these pages can never complete.
                    raise _TransferGone("Peer transfer expired")
                if status != 200:
                    raise PeerError("Peer transfer interrupted; retrying from a verified state")
                if len(raw) != expected["bytes"]:
                    raise PeerError("Peer state page was truncated")
                try:
                    await asyncio.to_thread(peer_snapshot.save_page, directory, index, raw, expected)
                except ValueError as exc:
                    raise PeerError("Peer state page did not verify") from exc
            done_bytes += int(expected["bytes"])
            _copy_progress(pages_done=index + 1, bytes_done=done_bytes, resumed_pages=kept)
        _copy_progress(phase="applying")
        # The apply runs on in its thread when this task is cancelled; a
        # later attempt then takes its result (see ``_receive_state``).
        job = asyncio.ensure_future(asyncio.to_thread(_apply_received, manifest, self.device_id))
        job.add_done_callback(lambda done: done.cancelled() or done.exception())
        _applying[self.device_id] = job
        try:
            _, applied = await asyncio.shield(job)
        finally:
            if job.done() and _applying.get(self.device_id) is job:
                del _applying[self.device_id]
        return self._received_state(manifest, applied)

    def _received_state(self, manifest: dict, applied: int) -> int:
        # A reused snapshot can predate the frontier just advertised by
        # exchange. Adopt only its committed frontier locally, but do not
        # hide a known backlog in owner_sync_status by lowering the peer's
        # observed head. The following exchange tails from local_watermarks;
        # _check_rollback still uses the live exchange's own-stream proof.
        observed = {origin: dict(streams) for origin, streams in manifest["watermarks"].items()}
        for origin, streams in _peer_watermarks.get(self.device_id, {}).items():
            bucket = observed.setdefault(origin, {})
            for incarnation, seq in streams.items():
                bucket[incarnation] = max(int(bucket.get(incarnation, 0)), int(seq))
        note_watermarks(self.device_id, observed,
                        catalog_backlog=_peer_catalog_backlog.get(self.device_id, False))
        _snapshot_peers.discard(self.device_id)
        return applied

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
            answer = await self.call(client, ctx, "GET",
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


def _decode_answer(status: int, raw: bytes) -> Any:
    """A sealed LAN answer, read like ``PeerLink._request`` reads an HTTP one."""
    if status >= 400:
        detail = ""
        try:
            detail = str(json.loads(raw).get("detail", ""))[:ERROR_DETAIL_LIMIT]
        except Exception:  # noqa: BLE001
            pass
        raise _peer_error(status, detail)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PeerError("Peer answer is not JSON") from exc


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

def peer_backoff_seconds(failures: int) -> float:
    """The wait after ``failures`` consecutive failures: 4 s, 8 s, ... up to 15 min.

    Equal jitter: half the exponential step is fixed, the other half random,
    so peers that went offline together do not retry in lockstep.
    """
    step = min(PEER_BACKOFF_MAX_SECONDS, PEER_BACKOFF_BASE_SECONDS * 2 ** min(max(failures, 1), 20))
    return step / 2 + random.random() * step / 2


async def _refresh_links(ctx: engine.NetworkContext) -> None:
    def _read():
        return engine.all_presence(), store.list_members(include_revoked=False)

    presence, members = await asyncio.to_thread(_read)
    # Only the database reads leave the loop; links/locks remain loop-owned.
    for row in members:
        device_id = row["device_id"]
        if device_id == ctx.device_id:
            continue
        endpoints = known_endpoints(row, presence.get(device_id))
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
        out.append({"device_id": link.device_id, "base_url": link.active_url, "endpoints": link.endpoints,
                    "lan_endpoints": link.lan_endpoints, "route": link.route,
                    "lan_error": link.lan_last_error if link.lan is None and link.lan_endpoints else None,
                    "last_error": link.last_error, "seconds_since_contact": age,
                    "online": age is not None and age <= ONLINE_WINDOW_SECONDS, "syncing": link.syncing})
    return out


def route_of(device_id: str) -> Optional[str]:
    """How sync reaches ``device_id`` right now (``PeerLink.route``), None before the first contact."""
    link = _links.get(device_id)
    return link.route if link is not None else None


def drop_lan_sessions() -> None:
    """Stop using LAN sessions to peers now (the owner turned LAN-direct off)."""
    for link in _links.values():
        link.lan = None


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
    from app.network.handoff import note_source_contact
    note_source_contact(device_id)
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
    global _links_generation
    if ctx.generation != _links_generation:
        # A member joined or left: every peer is worth trying again.
        _links_generation = ctx.generation
        for link in _links.values():
            link.failures, link.backoff_until, link.contact_kick_used = 0, 0.0, False
    results: Dict[str, Any] = {}
    now = time.monotonic()
    for link in list(_links.values()):
        if not link.endpoints or link.backoff_until > now:
            continue
        try:
            results[link.device_id] = await link.sync_once(client, ctx)
            # Both outbound sync and inbound/control traffic must reset the
            # persisted recovery clock, not just the online-presence cache.
            await asyncio.to_thread(note_contact, link.device_id)
            await asyncio.to_thread(store.upsert_peer, link.device_id, base_url=link.active_url, contacted=True,
                                    error=None)
        except (httpx.HTTPError, PeerError) as exc:
            link.last_error = f"{type(exc).__name__}: {exc}"[:200]
            link.note_failure()
            await asyncio.to_thread(store.upsert_peer, link.device_id, error=link.last_error)
            logger.debug("Peer %s unreachable: %s", link.device_id, link.last_error)
        except Exception:  # noqa: BLE001
            link.note_failure()
            logger.warning("Sync with %s failed", link.device_id, exc_info=True)
        _tried_since_hold.add(link.device_id)
    if engine.outbox_held() and not any(link.endpoints for link in _links.values()):
        # With no peer to compare, local writes may be published. Reachable
        # peers release via their checked view above; a successful HTTP exchange
        # alone must not release a hold when rollback proof was unavailable.
        engine.hold_outbox(False)
        request_scan()
    return results


def _untried_peer(ctx: engine.NetworkContext, peer_device_id: str) -> bool:
    """Another active member we can dial that our pass has not tried yet.

    Read from the database, not ``_links``: before our first pass the links
    do not exist yet. Such a peer may hold the rollback proof one peer's
    view cannot rule out, and our pass reaches it shortly.
    """
    presence = engine.all_presence()
    for row in store.list_members(include_revoked=False):
        device_id = row["device_id"]
        if device_id in (ctx.device_id, peer_device_id) or device_id in _tried_since_hold:
            continue
        member = ctx.member(device_id)
        if member is None or member.status != membership.STATUS_ACTIVE:
            continue
        if dial_endpoints(peer_endpoints(row, presence.get(device_id))):
            return True
    return False


def release_hold_for_peer_view(peer_device_id: str, view: Any) -> bool:
    """Open the outbox on what an authenticated peer that reached us holds of our stream.

    ``view`` is the peer's ``{incarnation: last_seq}`` for this device, the
    same claim ``PeerLink._check_rollback`` reads from an exchange we start.
    With one-way reachability (the peer reaches us, we cannot dial it) our
    own pass never completes, and the hold would otherwise last until we can
    (#1621). A peer that holds nothing of our current or a newer incarnation
    past our own log is evidence that we were not restored from a backup
    behind it. A claim past our log keeps the hold: only an exchange we start
    can fetch the signed op that proves a rollback. Until our own pass has
    tried every other peer we can dial, or while it is comparing streams
    with one, that comparison decides. Runs in a worker thread. Returns True
    when this call opened the outbox.
    """
    if not engine.outbox_held() or not isinstance(view, dict):
        return False
    if any(device_id != peer_device_id and link._sync_lock.locked() for device_id, link in list(_links.items())):
        return False
    ctx = engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return False
    if _untried_peer(ctx, peer_device_id):
        return False
    current = ctx.identity.incarnation
    claims: Dict[int, int] = {}
    for incarnation, seq in view.items():
        try:
            incarnation, seq = int(incarnation), int(seq)
        except (TypeError, ValueError):
            return False
        if incarnation >= current and seq > 0:
            claims[incarnation] = seq
    if any(incarnation > current for incarnation in claims):
        return False  # a newer incarnation of ours exists somewhere
    if claims:
        with store.get_db() as conn:
            held = store.own_watermark(conn.cursor(), ctx.device_id, current)
        if claims[current] > held:
            return False
    if not engine.outbox_held():
        return False
    engine.hold_outbox(False)
    logger.info("Publishing resumed: peer %s holds none of this worker's stream past its own log",
                peer_device_id)
    request_scan()
    return True


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
    idle = SYNC_IDLE_SECONDS
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
            moved = any(r.get("sent") for r in results.values())
            try:
                from app.network import blobs
                busy = bool(await blobs.fetch_missing(limit=4)) or busy
            except Exception:  # noqa: BLE001
                logger.debug("Peer file catch-up will retry", exc_info=True)
            if busy or moved:
                idle = SYNC_IDLE_SECONDS
            timeout = SYNC_ACTIVE_SECONDS if busy else idle
            try:
                await asyncio.wait_for(_wake.wait(), timeout=timeout)
                idle = SYNC_IDLE_SECONDS  # woken by a write or a peer
            except asyncio.TimeoutError:
                if not busy:
                    idle = min(idle * 2, SYNC_IDLE_MAX_SECONDS)
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
        started = time.monotonic()
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
        from app.network import catalog
        if catalog.take_events_backlog():
            rest = min(SCAN_INTERVAL_SECONDS,
                       max(SCAN_BACKLOG_REST_SECONDS, time.monotonic() - started))
            await asyncio.sleep(rest)
            continue
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
    _tried_since_hold.clear()
    _tasks[:] = [
        asyncio.create_task(sync_loop(), name="network-sync"),
        asyncio.create_task(scan_loop(), name="network-scan"),
        asyncio.create_task(presence_loop(), name="network-presence"),
        asyncio.create_task(liveness_loop(), name="network-liveness"),
        asyncio.create_task(transcripts.loop(), name="network-transcripts"),
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
    global _links_generation, _state_copy
    _state_copy = None
    _applying.clear()
    _links.clear()
    _links_generation = None
    _last_contact.clear()
    _peer_watermarks.clear()
    _snapshot_peers.clear()
    _peer_catalog_backlog.clear()
    _tried_since_hold.clear()


def reset_for_tests() -> None:
    global _full_scan_requested, _dirty_sessions, _advertised, _owner_candidates
    forget_peers()
    lan.reset_for_tests()
    _advertised = None
    with _scan_requests_lock:
        _dirty_collections.clear()
        _dirty_sessions = set()
        _full_scan_requested = False
        _owner_candidates = set()
        _owner_collections.clear()
