"""Surface a clear rate-limited state when a Claude turn hits a usage limit.

Native-Claude **automatic account failover was RETIRED** (OC-1, "one account per
provider — most compliant"). Hitting a usage limit (``api_error_status == 429``)
no longer selects and switches to another saved account: rotating subscription
credentials across accounts is exactly the pooling pattern that triggers vendor
bans, so it is gone. To use a different account the user signs the current one
out and a new one in — deliberately manual.

What survives:

* limit detection (:func:`is_usage_limit_error`), and
* the user-facing notification (:func:`_notify`): on a limit we surface a clear
  "rate-limited — wait for the window to reset or sign into a different account"
  state (OC-4) with NO auto-rotation.

:func:`attempt_failover` is kept as the single seam every caller already invokes
(the streaming SSE wrapper, ``turn_scheduler``, and
:func:`execute_query_with_failover`); it now only emits that notification and
always returns ``retry=False`` so each caller's retry loop terminates at once.

Cross-install note: an offloaded / cloud turn runs this seam on the WORKER
install (the machine that actually spawns the SDK subprocess).

Out of scope: proactive utilization-based switching; this module reacts only to
a realized 429.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def is_usage_limit_error(metadata: Optional[Dict[str, Any]]) -> bool:
    """True iff this turn's metadata carries a 429 usage-limit API error.

    Handles ``api_error_status`` arriving as an int or a numeric string. Never
    raises — a malformed metadata blob simply yields ``False``.
    """
    if not metadata:
        return False
    try:
        return int(metadata.get("api_error_status")) == 429
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Failover decision + execution
# ---------------------------------------------------------------------------

@dataclass
class FailoverDecision:
    retry: bool
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    switched: bool = False


async def _notify(
    api_user_id: Optional[str],
    session_id: Optional[str],
    *,
    severity: str,
    message: str,
) -> None:
    """Emit a user-facing notification through the unified funnel. Never raises."""
    try:
        from app.services.notification_service import emit_notification
        await emit_notification(
            api_user_id or "admin",
            severity=severity,
            message=message,
            category="account_failover",
            surface="notification",
            session_id=session_id,
            persist=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("account_failover notification emit failed", exc_info=True)


async def attempt_failover(
    session_id: Optional[str],
    api_user_id: Optional[str],
    attempted: Set[str],
) -> FailoverDecision:
    """Native automatic account failover was RETIRED (OC-1).

    On a usage-limit 429 we no longer rotate to another saved account. Surface a
    clear rate-limited state (OC-4) and let the turn end — the user waits for the
    window to reset or signs into a different account. Always returns
    ``retry=False`` so every caller's retry loop terminates immediately.

    ``attempted`` is accepted for call-site compatibility and is ignored (there
    is nothing to fail over to).
    """
    await _notify(
        api_user_id, session_id,
        severity="warning",
        message=(
            "Usage limit reached — wait for the window to reset or sign into a "
            "different account."
        ),
    )
    logger.info(
        "Usage limit hit for session %s — automatic account failover retired; "
        "not rotating accounts (user waits or switches manually).",
        session_id,
    )
    return FailoverDecision(retry=False)


async def execute_query_with_failover(**kwargs) -> Dict[str, Any]:
    """Run ``execute_query`` once; on a usage-limit 429 surface the rate-limited
    state.

    Shared by every non-streaming caller (the query API routes, automations,
    connectors, and the ScheduleWakeup builtin). Native auto-switch was retired
    (OC-1): a 429 no longer rotates accounts or retries. This wrapper remains the
    single seam those callers invoke so the rate-limited notification is emitted
    consistently, then it returns the (failed) result as-is.

    ``execute_query`` is imported lazily to avoid an import cycle
    (query_engine ↔ account_failover).
    """
    from app.core.query_engine import execute_query

    result = await execute_query(**kwargs)
    if is_usage_limit_error(result.get("metadata")):
        await attempt_failover(
            result.get("session_id"), kwargs.get("api_user_id"), set()
        )
    return result
