"""Which provider leg a booked ``usage_log`` row belongs to.

Every usage writer (turn settlement, the kill-path recovery, one-shot text
generation) stamps the row with the leg that served it, so the Stats page can
break spend down per provider. The leg is the resolved binding the turn ran on
(``options.aih_provider_id``): ``None`` is the native/delegated Claude leg,
anything else is a ``model_providers`` row.

``billing`` says whether that leg is paid per token (``"api"``) or rides a
subscription login (``"subscription"``). A subscription leg's cost is still the
app's API-price estimate, so the Stats page labels it as such instead of
presenting it as money spent. The classification mirrors
``account_failover``'s 429 seam: a ChatGPT/Grok row or a subscription
``auth_mode`` is a subscription; for Claude, the configured Claude Code auth
mode decides.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

logger = logging.getLogger(__name__)

#: ``usage_log.provider_type`` for the native/delegated Claude leg.
CLAUDE_PROVIDER_TYPE = "claude"
#: ``usage_log.provider_type`` when a bound provider row no longer exists.
UNKNOWN_PROVIDER_TYPE = "unknown"

BILLING_SUBSCRIPTION = "subscription"
BILLING_API = "api"


class UsageAttribution(TypedDict):
    provider_id: Optional[str]
    provider_type: str
    billing: Optional[str]


def provider_billing(provider_type: Optional[str], auth_mode: Optional[str]) -> str:
    """``"subscription"`` or ``"api"`` for a ``model_providers`` row's shape."""
    from app.core.provider.fluid_credentials import _SUBSCRIPTION_AUTH_MODES

    ptype = (provider_type or "").strip().lower()
    mode = (auth_mode or "").strip().lower()
    if ptype in ("chatgpt", "grok") or mode in _SUBSCRIPTION_AUTH_MODES:
        return BILLING_SUBSCRIPTION
    return BILLING_API


def claude_billing() -> Optional[str]:
    """Billing of the native Claude leg under the current Claude Code auth mode."""
    try:
        from app.core import claude_auth_mode

        mode = claude_auth_mode.current_mode()
    except Exception:  # noqa: BLE001 — attribution must never break booking
        logger.debug("usage attribution: Claude auth mode unreadable", exc_info=True)
        return None
    return BILLING_API if mode == claude_auth_mode.MODE_API_KEY else BILLING_SUBSCRIPTION


def usage_attribution(provider_id: Optional[str]) -> UsageAttribution:
    """Attribution columns for a row booked on ``provider_id``'s leg.

    Synchronous (one indexed lookup); callers on the event loop wrap it in
    ``run_sync``. Never raises: a failed lookup books the row as
    ``provider_type="unknown"`` with no billing, which the Stats page shows as
    such rather than guessing.
    """
    if not isinstance(provider_id, str) or not provider_id.strip():
        return {
            "provider_id": None,
            "provider_type": CLAUDE_PROVIDER_TYPE,
            "billing": claude_billing(),
        }
    try:
        from app.db.model_providers import get_model_provider

        row = get_model_provider(provider_id, include_token=False)
    except Exception:  # noqa: BLE001
        logger.debug("usage attribution: provider %s lookup failed", provider_id, exc_info=True)
        row = None
    if not row:
        return {
            "provider_id": provider_id,
            "provider_type": UNKNOWN_PROVIDER_TYPE,
            "billing": None,
        }
    ptype = (row.get("provider_type") or "").strip().lower() or UNKNOWN_PROVIDER_TYPE
    return {
        "provider_id": provider_id,
        "provider_type": ptype,
        "billing": provider_billing(row.get("provider_type"), row.get("auth_mode")),
    }


def session_provider_id(session_id: Optional[str]) -> Optional[str]:
    """The provider a session is bound to now (``sessions.provider_id``).

    The fallback for writers that have no options object to read the turn's
    resolved binding from (the kill path). Never raises.
    """
    if not session_id:
        return None
    try:
        from app.db import database

        row = database.get_session(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("usage attribution: session %s lookup failed", session_id, exc_info=True)
        return None
    return (row or {}).get("provider_id") or None
