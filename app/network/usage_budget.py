"""One durable usage-request budget per credential across a worker network.

The founder is a fixed coordinator, not an election based on reachable peers.
A partition must serve cached usage instead of granting independent budgets.
Only credential/endpoint digests and lease outcomes cross the network; provider
credentials and responses stay on the worker making the upstream request.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from app.core import usage_polling as polling
from app.db.async_database import run_sync
from app.network import engine, ownership, transport
from app.network.browser_panel import peer_link

logger = logging.getLogger(__name__)
PREFIX = "/api/v1/network/peer/usage-budget"


def coordinator():
    # Read membership from this worker's database, including after a leave or
    # revocation. A stale cached context must not authorize a local claim.
    ctx = engine.load_context()
    if ctx is None:
        return None
    me = ctx.member(ctx.device_id)
    owner = ctx.member(ctx.founder_device_id)
    if (not ownership.execution_eligible() or not me or me.kind != "worker"
            or not owner or owner.kind != "worker" or owner.status != "active"):
        raise polling.UsageDeferred("Usage budget authority is unavailable")
    return ctx


async def _remote(ctx, action, key, body=None):
    try:
        current, link = await peer_link(ctx.founder_device_id)
        if current.network_id != ctx.network_id or current.device_id != ctx.device_id:
            raise polling.UsageDeferred("Usage network changed")
        async with asyncio.timeout(20), httpx.AsyncClient(follow_redirects=False) as client:
            for attempt in range(2):
                await link.authenticate(client, ctx)
                response = await client.post(
                    link.base_url + f"{PREFIX}/{action}/{key}", json=body or {},
                    headers={"Authorization": "Bearer " + link.token}, timeout=10,
                )
                if response.status_code == 401 and not attempt:
                    link.token = None
                    # Authentication rejected before dispatch; don't replay any
                    # other failure because a claim may already have committed.
                    continue
                if response.status_code != 200:
                    raise transport.PeerError("Usage budget request refused")
                transport.note_contact(ctx.founder_device_id)
                return response.json()
    except (TimeoutError, httpx.HTTPError, transport.PeerError, HTTPException, ValueError, KeyError, TypeError):
        raise polling.UsageDeferred("Usage budget authority is unavailable") from None


@dataclass(frozen=True)
class Budget:
    key: str
    lease: str
    ctx: engine.NetworkContext | None = None

    async def finish(self, status, retry_after, interval):
        if self.ctx is None:
            await run_sync(polling.finish, self.key, time.time(), status, retry_after, interval, lease=self.lease)
            return
        try:
            await _remote(self.ctx, "finish", self.key, {
                "lease": self.lease, "status": status,
                "retry_after": retry_after, "interval": interval,
            })
        except polling.UsageDeferred:
            # Never turn a successful observation into an error or shorten the
            # outstanding lease. A lost completion retains the ten-minute hold.
            logger.debug("Usage budget completion deferred; coordinator lease remains held")


async def acquire(key, interval=300):
    ctx = await run_sync(coordinator)
    if ctx is None or ctx.device_id == ctx.founder_device_id:
        lease = await run_sync(polling.claim, key, time.time(), interval)
        remote = None
    else:
        result = await _remote(ctx, "claim", key, {"interval": interval})
        lease = result.get("lease") if isinstance(result, dict) else None
        remote = ctx
    if not isinstance(lease, str) or not re.fullmatch(r"[0-9a-f]{32}", lease):
        raise polling.UsageDeferred()
    return Budget(key, lease, remote)


async def resume_authentication(url, headers):
    key = polling.identity(url, headers)
    ctx = await run_sync(coordinator)
    if ctx is None or ctx.device_id == ctx.founder_device_id:
        await run_sync(polling.resume_identity, key)
    else:
        await _remote(ctx, "resume", key)
