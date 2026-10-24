"""Owner identity, detected introduction and cross-worker spending integration.

Historical assistant names remain for wire/API compatibility. Email research is
retired; watches own all background observation and its permission provenance.
"""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from datetime import datetime, timezone

from app.core import assistant_introduction as introductions
from app.core.config import settings
from app.db import assistant as store, database
from app.db.base import get_db, row_to_dict

logger = logging.getLogger(__name__)

AVATAR_PRESETS = store.AVATAR_PRESETS
AGENT_ID = store.AGENT_ID
# The built-in agent row research runs on. No system prompt and no model.
# Every worker writes this exact definition (ensure_agent), so it stays
# unchanged while older peers still restore it.
AGENT_NAME = "Assistant"
AGENT_DESCRIPTION = "Runs your personal assistant. Built into AI Shuffle."
AGENT_CONFIG = {"tool_selection_mode": "all", "agent_invoke_mode": "all"}
# Marks the agent older Assistant setups created; agent identities report it
# as ``dedicated``.
DEDICATED_KEY = "assistant_dedicated"
_guard_version = 0


def assistant_id():
    """This worker's runner row id (its install id)."""
    return settings.effective_instance_id


def register_proactive_guard(version=1):
    global _guard_version
    _guard_version = version


def proactive_guard_available():
    return _guard_version == 1


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


def _publish():
    """Send the account row to the other workers (no-op when standalone)."""
    try:
        from app.network import engine
        engine.record_after_commit("assistant", store.ACCOUNT_ID)
    except Exception:  # noqa: BLE001 - the scanner publishes it on its next pass
        logger.warning("Could not publish the assistant account row", exc_info=True)


def _seed(cursor, local):
    return dict(agent_id=local.get("agent_id"), introduction=local.get("introduction") or {})


def _project(conn, account):
    """Write the account view this worker runs under into its runner row."""
    cursor = conn.cursor()
    agent_id = account.get("agent_id")
    if agent_id:
        cursor.execute("SELECT id FROM agents WHERE id=%s", (agent_id,))
        if not cursor.fetchone():
            agent_id = None  # Not synced here yet; research waits for it.
    values = dict(agent_id=agent_id, enabled=True, paused=False,
                  introduction=account.get("introduction") or {})
    store.project(conn, assistant_id(), values, account.get("sources") or {})


def reconcile():
    """Project the synced account onto this worker's runner row.

    Only seeding (and moving an older account onto the built-in agent) writes
    the account. Research state is runner-local.
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
            logger.info("Assistant: shared this worker's assistant configuration with the account")
        _project(conn, account)
    if publish:
        _publish()
    return account


def on_network_applied(touched):
    """A peer's change landed: re-run reconcile when it concerns the assistant."""
    if any(collection == "assistant" for collection, _record, _op in touched):
        reconcile()


def _config(agent):
    config = (agent or {}).get("config") or {}
    return json.loads(config) if isinstance(config, str) else config


def _is_dedicated(agent):
    return bool(agent and not agent.get("is_builtin") and _config(agent).get(DEDICATED_KEY) is True)


def agent_identities():
    # ProfileConfig's normal typed serializer deliberately does not contain
    # assistant presentation metadata. This owner-only projection uses the
    # SAME unified agent records/IDs, never a parallel agent registry.
    result = []
    for agent in database.get_all_profiles():
        avatar = (agent.get("config") or {}).get("avatar_id", "robot")
        result.append({"id": agent["id"], "name": agent["name"],
                       "avatar_id": avatar if avatar in AVATAR_PRESETS else "robot",
                       "dedicated": _is_dedicated(agent)})
    return result


def update_agent_identity(agent_id, name=None, avatar_id=None, _conn=None):
    if avatar_id is not None and avatar_id not in AVATAR_PRESETS:
        raise ValueError("Unknown avatar preset")
    if name is not None and (not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name)):
        raise ValueError("Agent name must be 1-80 printable characters")
    # Lock/read/merge to preserve custom prompts, models, tools, skills and
    # unknown future config keys; a stale client must not replace the config.
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents WHERE id=%s FOR UPDATE", (agent_id,))
        agent = row_to_dict(cursor.fetchone())
        if not agent:
            raise ValueError("Agent does not exist")
        if agent_id == AGENT_ID:
            raise ValueError("The built-in assistant agent cannot be edited")
        if agent["is_builtin"]:
            raise ValueError("Copy a built-in agent before customizing its identity")
        config = agent.get("config") or {}
        if isinstance(config, str):
            config = json.loads(config)
        if avatar_id is not None:
            config["avatar_id"] = avatar_id
        cursor.execute("UPDATE agents SET name=%s,config=%s,updated_at=NOW() WHERE id=%s", (name.strip() if name is not None else agent["name"], json.dumps(config), agent_id))
    return {"id": agent_id, "name": name.strip() if name is not None else agent["name"], "avatar_id": config.get("avatar_id", "robot")}


def session_stopped(session_id):
    """Honor both the live Stop latch and durable last-turn Stop after reboot."""
    from app.core.task_registry import task_registry
    if not session_id or task_registry.is_user_stopped(session_id):
        return True
    session = database.get_session(session_id)
    if not session or session.get("status") in ("stopped", "archived"):
        return True
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s ORDER BY position DESC,created_at DESC LIMIT 1", (session_id,))
        turn = row_to_dict(cursor.fetchone())
        if not turn:
            return False
        payload = turn.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return (turn.get("interrupt_mode") == "stop" and payload.get("interrupt_source") != "assistant") or turn.get("stop_reason") in ("user_stopped", "user_stopped_all", "agent_stopped")


def assert_proactive_allowed(metadata, session_id=None):
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
        if not account or not account.get("agent_id"):
            return False
        return True


def apply_detection(detected, unknown=()):
    """Store a detection pass (see assistant_introduction); True when it wrote.

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
        if updated == stored:
            return False
        _project(conn, store.write_account(conn, {"introduction": updated}))
    _publish()
    return True

