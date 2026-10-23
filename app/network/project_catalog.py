"""Tell online peers that this worker's project catalog changed (#434).

Peers build ``GET /cluster/projects`` by asking each owner which projects it
can run (``git-catalog``). Adding or deleting a project changes that answer
but no synced record, so a peer's open browsers had nothing telling them to
re-read it and kept a deleted project in their selectors until the next
periodic reconcile. This is the content-free signal: the peer re-reads through
its own authorized endpoints. Best effort; an older peer that does not know
the action still catches up on its reconcile.
"""
from __future__ import annotations

import asyncio
import logging

from app.network import control, engine, transport

logger = logging.getLogger(__name__)

ACTION = "projects-changed"
ANNOUNCE_TIMEOUT = 3.0

_tasks: set[asyncio.Task] = set()


async def announce() -> None:
    """Ask every online peer to refresh its project selectors."""
    ctx = engine.context()
    if ctx is None:
        return
    peers = [device for device in transport.online_devices() if device != ctx.device_id]

    async def one(device_id: str) -> None:
        try:
            async with asyncio.timeout(ANNOUNCE_TIMEOUT):
                await control.call(device_id, ACTION, {}, timeout=ANNOUNCE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - the peer's reconcile still catches up
            logger.debug("Project catalog change not delivered to %s: %s", device_id, type(exc).__name__)

    await asyncio.gather(*(one(device) for device in peers))


def announce_soon() -> None:
    """Fire :func:`announce` without holding up the request that changed it."""
    if engine.context() is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(announce())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def handle_changed(body: dict, from_device: str) -> dict:
    from app.core.global_events import global_events

    for kind in ("admin", "api_user", "account", "paired"):
        global_events.resources_changed("projects", user_type=kind)
    return {"ok": True}
