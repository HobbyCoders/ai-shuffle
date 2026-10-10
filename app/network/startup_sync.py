"""A bounded desktop launch barrier; the listener stays up for peer handshakes."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.db import network as store
from app.db.async_database import run_sync
from app.network import blobs, engine, enrollment, hub, transport

logger = logging.getLogger(__name__)
STARTUP_TIMEOUT = 60.0
ACCOUNT_TIMEOUT = 15.0
PEER_TIMEOUT = 30.0
# A peer that does not accept a connection within this long is treated as
# offline for the launch; each dead address used to cost a full request
# timeout. The background sync keeps its own timeouts.
CONNECT_TIMEOUT = 3.0
# Per-peer launch outcomes.
SYNCED, SYNCING, UNREACHED = "synced", "syncing", "unreached"
_task: asyncio.Task | None = None
_last_result: dict | None = None


def status() -> dict | None:
    if _task is not None and not _task.done():
        return {"status": "syncing"}
    if _last_result and _last_result.get("first_copy"):
        ctx = engine.context()
        if ctx is not None and ctx.bootstrap_state != engine.BOOTSTRAP_READY:
            # The copy outlives the launch: report where it is now.
            return {**_last_result, "reason": first_copy_reason()}
    return _last_result


def first_copy_reason() -> str:
    """One line on a joining worker's first copy, for the launch screen and Devices."""
    copy = transport.state_copy_status()
    if not copy:
        return "Waiting for another worker to copy your data to this one."
    peer = copy.get("peer_name") or "another worker"
    phase = copy.get("phase")
    if phase == "downloading" and copy.get("pages_total"):
        return (f"Copying your data from {peer}: {copy['pages_done']:,} of {copy['pages_total']:,} pages. "
                "The copy continues in the background.")
    if phase in ("applying", "done"):
        return f"Saving the data copied from {peer}. This continues in the background."
    if phase == "failed":
        return f"Copying your data from {peer} stopped and will retry: {copy.get('error') or 'unknown error'}"
    return f"Waiting for {peer} to prepare a copy of your data."


async def synchronize() -> dict:
    """Concurrent launch requests share work; a later desktop launch checks again."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_bounded_sync(), name="desktop-startup-sync")
    return await asyncio.shield(_task)


async def _bounded_sync() -> dict:
    global _last_result
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            result = await _sync()
    except TimeoutError:
        result = {"status": "partial", "reason": "Startup sync timed out. Sync will continue in the background."}
    except Exception:
        logger.exception("Desktop startup sync failed")
        result = {"status": "partial", "reason": "Startup sync could not finish. Sync will retry in the background."}
    _last_result = result
    logger.info("Desktop startup sync: %s", result["status"])
    return result


async def _sync() -> dict:
    account_incomplete = False
    started = time.monotonic()
    # Account admission may switch a standalone worker to the account's network.
    # Do this before looking at peers, but do not let an account outage skip LAN.
    try:
        async with asyncio.timeout(ACCOUNT_TIMEOUT):
            hub._state.pop("enrollment_poll", None)
            hub._directory_poll_at.clear()
            while True:
                busy = await hub._loop_body()
                ctx = engine.context()
                linked = await run_sync(hub._binding)
                if not linked or hub._state.get("last_error"):
                    break
                if ctx is not None and ctx.bootstrap_state == engine.BOOTSTRAP_READY and not busy:
                    break
                # Account approval can arrive just after registration. Keep the
                # launch screen up instead of waiting four minutes in the UI.
                hub._state.pop("enrollment_poll", None)
                await asyncio.sleep(1)
            account_incomplete = bool(hub._state.get("last_error"))
    except TimeoutError:
        account_incomplete = True
    logger.info("Startup sync: account phase took %.1fs%s", time.monotonic() - started,
                " (incomplete)" if account_incomplete else "")
    enrollment.resume_enrollment()
    if enrollment._task is not None and not enrollment._task.done():
        # The durable enrollment task owns its resume record; never cancel it.
        await asyncio.shield(enrollment._task)
    ctx = engine.context()
    if ctx is not None and ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        # Enrolled, still receiving the first copy. It goes on in the
        # background and the app shows its progress.
        return {"status": "partial", "first_copy": True, "reason": first_copy_reason()}
    if ctx is None:
        linked = await run_sync(hub._binding)
        return {"status": "partial" if linked else "not_enrolled",
                "reason": "Waiting for network enrollment." if linked else None}
    await run_sync(engine.scan_all, strict=True)
    await transport._refresh_links(ctx)
    links = list(transport._links.values())
    if not links:
        return {"status": "partial" if account_incomplete else "synced", "peers": 0}
    async with httpx.AsyncClient(follow_redirects=False) as client:
        async def catch_up(link) -> str:
            """``SYNCED``, ``SYNCING`` (answered, still delivering at the limit) or ``UNREACHED``."""
            peer_started = time.monotonic()
            outcome = "failed"
            batches = link.batches
            try:
                if not link.endpoints or link.backoff_until > time.monotonic():
                    # The background sync just failed to reach it and is
                    # backing off: do not hold the launch for it again.
                    outcome = "skipped (backing off)"
                    return UNREACHED
                async with asyncio.timeout(PEER_TIMEOUT):
                    await link.sync_once(client, ctx, catch_up=True)
                    transport.note_contact(link.device_id)
                    await run_sync(store.upsert_peer, link.device_id, base_url=link.active_url, contacted=True, error=None)
                outcome = "synced"
                return SYNCED
            except TimeoutError:
                if link.batches > batches:
                    # It answered and was still moving batches when the launch
                    # limit came: a large backlog, not an outage. The
                    # background sync carries on from what was applied.
                    outcome = f"still syncing in the background ({link.batches - batches} batches so far)"
                    return SYNCING
                outcome = "unreachable (TimeoutError)"
                return UNREACHED
            except (httpx.HTTPError, transport.PeerError) as exc:
                outcome = f"unreachable ({type(exc).__name__})"
                return UNREACHED
            except Exception:
                logger.exception("Startup sync with peer %s failed", link.device_id)
                return UNREACHED
            finally:
                logger.info("Startup sync: peer %s %s in %.1fs", link.device_id, outcome,
                            time.monotonic() - peer_started)
        peers_started = time.monotonic()
        # The context variable is copied into each gathered task only.
        token = transport.connect_timeout.set(CONNECT_TIMEOUT)
        try:
            results = await asyncio.gather(*(catch_up(link) for link in links))
        finally:
            transport.connect_timeout.reset(token)
    reached = results.count(SYNCED)
    syncing = results.count(SYNCING)
    logger.info("Startup sync: peer phase took %.1fs (%d of %d reached, %d still syncing)",
                time.monotonic() - peers_started, reached, len(links), syncing)
    # Records can reference note attachments and other managed files. Do not
    # declare the first render current while their bytes are still missing.
    blobs_started = time.monotonic()
    while await run_sync(store.missing_blobs, 1):
        if not await blobs.fetch_missing(limit=16):
            logger.info("Startup sync: file phase gave up after %.1fs", time.monotonic() - blobs_started)
            return {"status": "partial", "peers": reached,
                    "reason": "Some files are not available yet. Sync will retry in the background."}
    logger.info("Startup sync: file phase took %.1fs", time.monotonic() - blobs_started)
    if reached == len(links) and not account_incomplete:
        return {"status": "synced", "peers": reached}
    if syncing:
        # "partial", not a new status word: installed desktop launchers refuse
        # to open the app on a status they do not know (desktop worker.ts).
        reason = ("Still syncing with another worker in the background. Showing saved data; "
                  "it updates as the sync finishes.")
        if reached + syncing < len(links):
            reason += " Some workers could not be reached; sync will retry."
        return {"status": "partial", "peers": reached, "syncing": syncing, "reason": reason}
    return {"status": "partial" if reached else "offline", "peers": reached,
            "reason": "Some workers could not be synced. Showing saved data; sync will retry in the background."}


async def stop() -> None:
    if _task is not None and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
