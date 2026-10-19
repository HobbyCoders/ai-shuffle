"""Which worker may renew a synced subscription login (ChatGPT, SuperGrok).

A ChatGPT or SuperGrok sign-in is stored on its ``model_providers`` row, and
that row syncs to every worker in the network, refresh token included. These
refresh tokens are single use: a renewal returns a new one and spends the old
one. When two workers renew the same login before the first renewal has
synced, the second sends a refresh token that is already spent, the provider
answers ``refresh_token_reused``, and the login is dead on every worker.

The row's refresh lease only serializes installs that share one database, so
it cannot prevent that. Across a network exactly one worker renews: the
founder, the same fixed coordinator that usage polling uses. Every other
worker keeps using the access token it has and receives the renewed bundle
through sync. When the founder is no longer an active worker, the active
worker with the lowest device id takes over, so a login cannot be stranded by
a founder that left.

An install outside a network renews its own logins, as before.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.network import engine
from app.network.membership import KIND_WORKER, STATUS_ACTIVE

logger = logging.getLogger(__name__)


def _active_worker(member) -> bool:
    return bool(member and member.kind == KIND_WORKER and member.status == STATUS_ACTIVE)


def authority_device_id(ctx: engine.NetworkContext) -> Optional[str]:
    """The device that renews synced subscription logins in this network."""
    founder = ctx.member(ctx.founder_device_id)
    if _active_worker(founder):
        return founder.device_id
    active = sorted(device_id for device_id, member in ctx.members.items() if _active_worker(member))
    return active[0] if active else None


def renewal_pending_message(service: str, *, needs_reauth: bool = False) -> Optional[str]:
    """User-facing reason a 401 could not be fixed here, or None if this worker renews.

    ``service`` names the login ("ChatGPT", "Grok"). Called after a worker
    that does not renew synced logins waited for a renewal that never came.
    ``needs_reauth`` is the stored login's flag: when the renewing worker has
    already found the login dead, waiting for it will not help, so the user is
    asked to sign in again instead.
    """
    try:
        ctx = engine.load_context(publish=False)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read network membership for a %s renewal", service, exc_info=True)
        return (
            f"Your {service} login has expired on this worker, and this worker could not "
            "read its network membership to find the worker that renews it. Try again shortly."
        )
    if ctx is None:
        return None
    authority = authority_device_id(ctx)
    if authority == ctx.device_id:
        return None
    if needs_reauth:
        return (
            f"Your {service} login has expired and could not be renewed. "
            "Sign in again from Settings > Providers."
        )
    member = ctx.member(authority) if authority else None
    if member is None:
        return (
            f"Your {service} login has expired on this worker and no active worker in "
            "this network can renew it. Sign in again from Settings > Providers."
        )
    return (
        f"Your {service} login is renewed by the worker '{member.name or member.device_id}', and its "
        "renewed token has not reached this worker. Make sure that worker is running "
        "and connected to this one, then try again."
    )


def may_refresh_here() -> bool:
    """True when this install may send a refresh for a synced subscription login.

    Reads membership from this worker's database rather than the cached
    context, so a leave or revocation takes effect immediately. If the
    network state cannot be read, the answer is False: a skipped renewal only
    delays, while a duplicate one ends the login on every worker.
    """
    try:
        ctx = engine.load_context(publish=False)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read network membership; not renewing synced logins here", exc_info=True)
        return False
    if ctx is None:
        return True
    return authority_device_id(ctx) == ctx.device_id
