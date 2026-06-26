"""Cross-install fan-out of session streaming state.

Streaming state (which sessions are mid-turn) lives in each install's in-memory
``sync_engine`` and drives the per-device streaming indicators — the spinner on
the sessions board and side panel. Because WebSocket connections are per-install,
a session streaming on install A is invisible to a device connected to install B,
so a user roaming between devices/installs sees no indicator for remote sessions.

This module closes that gap. When a session starts or stops streaming locally,
the owning install fire-and-forget POSTs the transition to every other online
peer in the registry (``POST /api/v1/cluster/streaming``, cluster-secret auth).
Each peer applies it to its OWN ``sync_engine`` and re-broadcasts to its
connected devices, so remote sessions stream exactly like local ones. The
receive path (``sync_engine.apply_remote_streaming_change``) never re-fans-out,
so a transition never loops between installs.

Everything here is best-effort: a slow or offline peer is logged at debug and
skipped, and never affects the local stream.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

import httpx

from app.core import cluster
from app.core.config import settings
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# Peer notifications are control-plane pings; keep the timeout short so a hung
# peer can never stall the fan-out.
FANOUT_TIMEOUT = 5.0

# Hold strong references to in-flight fan-out tasks. asyncio keeps only weak
# references to bare tasks, so without this a task could be garbage-collected
# before it finishes.
_inflight: Set["asyncio.Task"] = set()


def _peer_targets() -> list[str]:
    """Online, controllable peer base URLs to notify.

    Excludes this install, offline installs, and view-only installs (those with
    no advertised ``base_url``). Runs a single registry query — cheap, and in the
    common single-install case it returns an empty list so fan-out is a no-op.
    """
    me = settings.effective_instance_id
    targets: list[str] = []
    for inst in database.get_installs():
        if inst.get("id") == me:
            continue
        if not inst.get("online"):
            continue
        base = (inst.get("base_url") or "").rstrip("/")
        if base:
            targets.append(base)
    return targets


async def _post_streaming(base_url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=FANOUT_TIMEOUT) as client:
            await client.post(
                f"{base_url}/api/v1/cluster/streaming",
                json=payload,
                headers=cluster.cluster_headers(),
            )
    except Exception:  # noqa: BLE001 — an offline/slow peer must not affect us
        logger.debug("Streaming fan-out to %s failed", base_url, exc_info=True)


async def _fanout(payload: dict) -> None:
    try:
        targets = await run_sync(_peer_targets)
    except Exception:  # noqa: BLE001
        logger.debug("Streaming fan-out target lookup failed", exc_info=True)
        return
    if not targets:
        return
    await asyncio.gather(*(_post_streaming(b, payload) for b in targets))


def schedule_fanout(
    *,
    user_id: str,
    session_id: str,
    is_streaming: bool,
    project_id: Optional[str],
    worktree_id: Optional[str],
) -> None:
    """Fire-and-forget: notify peer installs of a local streaming transition.

    Safe to call from inside the ``sync_engine`` broadcast path — it never blocks
    the local broadcast and swallows all peer errors. No-op when there is no
    running event loop (e.g. a sync test context).
    """
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "is_streaming": is_streaming,
        "instance_id": settings.effective_instance_id,
        "project_id": project_id,
        "worktree_id": worktree_id,
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop (e.g. a sync test context) — nothing to schedule.
        return
    task = loop.create_task(_fanout(payload))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
