"""Durable ``interaction.*`` lifecycle event emitters (#653 PR-A).

The interaction state machine (``app.db.interactions``) is pure CRUD over
``pending_interactions`` and deliberately never touches the event log. This
module is the "elsewhere" that appends the typed ``interaction.requested`` /
``interaction.answered`` / ``interaction.expired`` contract events to the
append-only ``session_events`` log so a non-runner device (or a watcher on the
runner) can durably settle a permission/question card by ``request_id`` instead
of relying on the runner-local legacy WS acks.

Design (mirrors ``turn_recorder``):

- Every emitter first ``await ensure_backfilled(session_id)``. This is
  load-bearing: a first-writer append on a never-backfilled session seeds
  ``session_event_counters`` and would permanently block the legacy-history
  backfill, dropping the session's prior transcript. Backfill must run first so
  the legacy prefix is preserved.
- Appends are UNFENCED (``run_id=None``): the ANSWERING install holds no run
  lease (it may not even be the runner), so these events must not be gated on a
  fence. All DB work runs off-loop via ``asyncio.to_thread``.
- Every emitter swallows and logs any failure — emission must never fail the
  user's answer/cancel. It runs AFTER the caller's ``notify_changed`` + WS
  broadcast, so a swallowed append leaves the live path fully intact.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.contract import events as contract
from app.db import session_events as session_events_db

logger = logging.getLogger(__name__)

# Envelope fields of EventBase — everything else on a model is its payload.
# Kept in sync with turn_recorder._ENVELOPE_FIELDS.
_ENVELOPE_FIELDS = {
    "id", "session_id", "seq", "run_id", "turn_id", "command_id",
    "created_at", "type",
}


def _build_event(model_cls: type, session_id: str, **fields: Any) -> Dict[str, Any]:
    """Construct a contract event model and reduce it to append_events shape.

    ``id``/``seq``/``created_at`` are server-assigned by the log layer; the
    model is built with placeholders purely to validate the typed payload.
    Mirrors ``turn_recorder._build_event`` (no turn/command binding — these
    events land between/outside turns).
    """
    model = model_cls(
        id="",
        session_id=session_id,
        seq=0,
        created_at=datetime.now(timezone.utc),
        **fields,
    )
    payload = model.model_dump(mode="json", exclude=_ENVELOPE_FIELDS)
    return {
        "type": model.type,
        "payload": payload,
        "turn_id": None,
        "command_id": None,
    }


async def _emit(session_id: str, event: Dict[str, Any]) -> None:
    """Backfill-then-append one event UNFENCED, off-loop. Never raises."""
    from app.core.history_backfill import ensure_backfilled

    # Seed legacy history BEFORE the first append so a first-writer interaction
    # event never seeds session_event_counters and blocks the backfill.
    await ensure_backfilled(session_id)
    await asyncio.to_thread(
        session_events_db.append_events, session_id, [event]
    )


async def emit_interaction_requested(
    session_id: str,
    interaction_id: str,
    request_id: Optional[str],
    kind: str,
    request: Optional[Dict[str, Any]] = None,
) -> None:
    """Append ``interaction.requested`` when a pending interaction is created.

    ``kind`` is ``"permission"`` or ``"question"``; ``request`` is the payload
    dict persisted on the row. Never raises.
    """
    try:
        await _emit(session_id, _build_event(
            contract.InteractionRequested,
            session_id,
            interaction_id=interaction_id,
            request_id=request_id,
            kind=kind,
            request=request or {},
        ))
    except Exception:
        logger.warning(
            "[interaction_events] emit requested failed for %s (interaction %s)",
            session_id, interaction_id, exc_info=True,
        )


async def emit_interaction_answered(
    session_id: str,
    interaction_id: str,
    request_id: Optional[str],
    answer: Optional[Dict[str, Any]] = None,
) -> None:
    """Append ``interaction.answered`` after a guarded answer UPDATE won.

    Callers MUST gate this on ``answer_interaction(...) is True`` so exactly one
    terminal event is ever emitted per interaction. Never raises.
    """
    try:
        await _emit(session_id, _build_event(
            contract.InteractionAnswered,
            session_id,
            interaction_id=interaction_id,
            request_id=request_id,
            answer=answer or {},
        ))
    except Exception:
        logger.warning(
            "[interaction_events] emit answered failed for %s (interaction %s)",
            session_id, interaction_id, exc_info=True,
        )


async def emit_interaction_expired(
    session_id: str,
    interaction_id: str,
    request_id: Optional[str],
    reason: str,
) -> None:
    """Append ``interaction.expired`` after a guarded cancel/expire UPDATE won.

    Callers MUST gate this on the guarded UPDATE (``cancel_interaction(...)`` at
    cancel sites) returning True so exactly one terminal event is ever emitted.
    ``reason`` is ``"cancelled"`` from cancel sites (``"ttl"`` from the sweeper,
    PR-B). Never raises.
    """
    try:
        await _emit(session_id, _build_event(
            contract.InteractionExpired,
            session_id,
            interaction_id=interaction_id,
            request_id=request_id,
            reason=reason,
        ))
    except Exception:
        logger.warning(
            "[interaction_events] emit expired failed for %s (interaction %s)",
            session_id, interaction_id, exc_info=True,
        )
