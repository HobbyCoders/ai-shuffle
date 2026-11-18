"""Owner identity, detected introduction and cross-worker spending integration.

Historical assistant names remain for wire/API compatibility. Email research is
retired; watches own all background observation and its permission provenance.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any

from app.core import owner_introduction as introductions
from app.core.config import settings
from app.core.owner_compat import OWNER_COLLECTION, SPENDING_INTERRUPT_SOURCE
from app.db import database
from app.db import owner_state as store
from app.db.base import get_db, row_to_dict

logger = logging.getLogger(__name__)

AGENT_ID = store.AGENT_ID
# Mixed-version tombstone only: never seed a built-in agent or hidden home.


def assistant_id():
    """This worker's runner row id (its install id)."""
    return settings.effective_instance_id


# ---------------------------------------------------------------------------
# Account sync
# ---------------------------------------------------------------------------

def _network():
    """The enrolled network context, or None on a standalone worker.

    Cached only: the per-tool recheck calls this, and a standalone worker
    would otherwise read the network state row on every call. Startup loads
    the context before any assistant work runs.
    """
    try:
        from app.network import engine
        return engine.cached_context()
    except Exception:  # noqa: BLE001 - no network layer means standalone
        return None


def _owned_here(session):
    """Whether this worker executes ``session`` for the owner (not an API key)."""
    if not session or session.get("api_user_id") is not None:
        return False
    ctx = _network()
    owner = session.get("owner_device_id")
    if ctx is not None and owner:
        return owner == ctx.device_id
    return session.get("instance_id") == assistant_id()


def _publish() -> None:
    """Send the account row to the other workers (no-op when standalone)."""
    try:
        from app.network import engine
        engine.record_after_commit(OWNER_COLLECTION, store.ACCOUNT_ID)
    except Exception:  # The scanner publishes it on its next pass.
        logger.warning("Could not publish the owner account row", exc_info=True)


def _seed(cursor: Any, local: Any) -> Any:
    return {"agent_id": local.get("agent_id"), "introduction": local.get("introduction") or {}}


def _project(conn: Any, account: dict[str, Any]) -> None:
    """Write the account view this worker runs under into its runner row."""
    cursor = conn.cursor()
    agent_id = account.get("agent_id")
    if agent_id:
        cursor.execute("SELECT id FROM agents WHERE id=%s", (agent_id,))
        if not cursor.fetchone():
            agent_id = None  # The referenced historical identity has not synced yet.
    values = {
        "agent_id": agent_id,
        "enabled": True,
        "paused": False,
        "introduction": account.get("introduction") or {},
    }
    store.project(conn, assistant_id(), values, account.get("sources") or {})


def reconcile() -> Any:
    """Project the synced account onto this worker's runner row.

    Only seeding an older account writes it. Historical wire/table names remain
    for owner introduction and remote accounting, never for a research runner.
    """
    publish = False
    with get_db() as conn:
        cursor = conn.cursor()
        account = store.lock_account(conn)
        cursor.execute("SELECT * FROM assistant_state WHERE id=%s", (assistant_id(),))
        local = store._parse(cursor.fetchone())
        if account is None:
            if not local or not local.get("agent_id"):
                return None
            account = store.write_account(conn, _seed(cursor, local))
            publish = True
            logger.info("Shared this worker's owner configuration with the account")
        _project(conn, account)
    if publish:
        _publish()
    return account


def on_network_applied(touched):
    """A peer's change landed: re-run reconcile when it concerns the assistant."""
    if any(collection == OWNER_COLLECTION for collection, _record, _op in touched):
        reconcile()


def agent_identities() -> list[dict[str, Any]]:
    # Owner-only projection of agent names over the SAME unified agent
    # records/IDs, never a parallel agent registry.
    return [{"id": agent["id"], "name": agent["name"],
             # Old clients read this key; no dedicated agent exists.
             "dedicated": False}
            for agent in database.get_all_profiles()]


def update_agent_identity(
    agent_id: str, name: str | None = None, _conn: Any = None,
) -> dict[str, Any]:
    if name is not None and (not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name)):
        raise ValueError("Agent name must be 1-80 printable characters")
    # Only the name column changes: custom prompts, models, tools, skills and
    # unknown config keys are never rewritten by an identity edit.
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, is_builtin FROM agents WHERE id=%s FOR UPDATE", (agent_id,))
        agent = row_to_dict(cursor.fetchone())
        if not agent:
            raise ValueError("Agent does not exist")
        if name is None:
            # Nothing to change (an older client sending only the retired
            # avatar): answer with the current name and write nothing.
            return {"id": agent_id, "name": agent["name"]}
        if agent["is_builtin"]:
            raise ValueError("Copy a built-in agent before customizing its identity")
        updated_name = name.strip()
        cursor.execute("UPDATE agents SET name=%s,updated_at=NOW() WHERE id=%s", (updated_name, agent_id))
    return {"id": agent_id, "name": updated_name}


def session_stopped(session_id):
    """Honor both the live Stop latch and durable last-turn Stop after reboot."""
    from app.core.task_registry import task_registry
    if not session_id or task_registry.is_user_stopped(session_id):
        return True
    session = database.get_session(session_id)
    if not session or session.get("status") == "stopped":
        return True
    from app.db import turns
    # The same visible history/steered resolution as every latest-turn reader.
    turn = turns.get_latest_turn(session_id)
    if not turn:
        return False
    payload = turn.get("payload") or {}
    return (
        turn.get("interrupt_mode") == "stop"
        and payload.get("interrupt_source") != SPENDING_INTERRUPT_SOURCE
    ) or turn.get("stop_reason") in ("user_stopped", "user_stopped_all", "agent_stopped")


def assert_proactive_allowed(metadata: dict[str, Any], session_id: str | None = None) -> Any:
    """Shared harness seam: legacy research provenance is no longer executable."""
    from app.core.watches import assert_allowed

    return assert_allowed(metadata, session_id)


def link_remote_job(caller_session_id, session_id, device_id, kind, label=None):
    """Count a job started on worker ``device_id`` against the owner's spending
    limit when an owner coordination chat (or one of its jobs) started it.
    Returns the link, or None for any other caller."""
    from app.core.orchestration_limits import role
    if role(caller_session_id) is None:
        return None
    return store.link_remote_job(assistant_id(), session_id, device_id, kind, label,
                                 starter_session_id=caller_session_id)


def detection_wanted():
    """True when this worker should run owner detection at all.

    Every configured worker detects its local owner context and connections.
    """
    with get_db() as conn:
        account = store.get_account(conn)
        return not (not account or not account.get("agent_id"))


def apply_detection(
    detected: dict[str, Any], unknown: tuple[str, ...] | set[str] = (),
) -> bool:
    """Store a detection pass (see owner_introduction); True when it wrote.

    Every worker contributes detected introduction values only when they
    change. Idle passes do not publish. Fields in ``unknown`` keep their last
    detected value.
    """
    with get_db() as conn:
        account = store.lock_account(conn)
        if not account or not account.get("agent_id"):
            return False
        stored = account.get("introduction") or {}
        updated = introductions.with_detected(stored, detected, unknown)
        changed = updated != stored
        if changed:
            _project(conn, store.write_account(conn, {"introduction": updated}))
    if changed:
        _publish()
    # Also run on unchanged accounts: upgrade backfill and failed writes retry
    # without changing introduction/Git-author projection or publishing idle passes.
    introductions.remember_identity(updated)
    return changed
