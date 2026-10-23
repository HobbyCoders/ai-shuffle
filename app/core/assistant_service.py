"""Owner assistant research policy and public integration seams (no provider execution).

The personal assistant's own conversation (its "home") and settings page were
retired in #371 step 5. What is left here serves the parts that stay:

* the background research loop on opted-in email sources
  (``assistant_manager``), until watches (#371 step 3) replace it;
* the "about you" detection that sets the default git author;
* agent identities (names and avatars on the Agents page);
* linking work a coordination chat starts on another worker to the owner's
  spending limit.

Orchestration must register_proactive_guard(1) ONLY after it enforces the
_assistant_proactive read-only ceiling, including MCP/native tools and delegated
work, and rechecks assert_proactive_allowed before starting/resuming a turn.
The marker lives in turn.overrides AND turn.payload; it is never trusted from
source content. Configured bypass permissions do not waive this ceiling.

Research runs on the built-in agent ``store.AGENT_ID``. Owners never pick or
edit it; agent lists leave it out. The synced ``assistant_account`` holds the
introduction and the source opt-ins; research, observations and limits stay
local to the worker and its connected sources.
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
    """Seed shared identity from this worker without sharing its conversation."""
    cursor.execute("SELECT connector_id FROM assistant_sources WHERE assistant_id=%s AND enabled=TRUE", (assistant_id(),))
    return dict(agent_id=local.get("agent_id"), introduction=local.get("introduction") or {},
                sources={row["connector_id"]: True for row in cursor.fetchall()})


def ensure_agent(cursor):
    """Create the built-in assistant agent, or put it back to its definition.

    Each worker writes the same row, so it needs no election. A row a stale
    peer or a manual edit changed is restored on the next pass.
    """
    cursor.execute("SELECT * FROM agents WHERE id=%s", (AGENT_ID,))
    row = row_to_dict(cursor.fetchone())
    if row is None:
        stamp = datetime.now(timezone.utc).isoformat()
        cursor.execute("""INSERT INTO agents (id, name, description, config, is_builtin, mcp_tools, created_at, updated_at)
            VALUES (%s,%s,%s,%s,TRUE,'[]',%s,%s) ON CONFLICT (id) DO NOTHING""",
            (AGENT_ID, AGENT_NAME, AGENT_DESCRIPTION, json.dumps(AGENT_CONFIG), stamp, stamp))
        return
    tools = row.get("mcp_tools")
    if isinstance(tools, str):
        tools = json.loads(tools)
    if (row.get("is_builtin") and row.get("name") == AGENT_NAME and row.get("description") == AGENT_DESCRIPTION
            and _config(row) == AGENT_CONFIG and not tools):
        return
    cursor.execute("UPDATE agents SET name=%s,description=%s,config=%s,is_builtin=TRUE,mcp_tools='[]',updated_at=NOW() WHERE id=%s",
                   (AGENT_NAME, AGENT_DESCRIPTION, json.dumps(AGENT_CONFIG), AGENT_ID))


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
        if account.get("agent_id"):
            if account.get("agent_id") != AGENT_ID:
                account = store.write_account(conn, {"agent_id": AGENT_ID})
                publish = True
                logger.info("Assistant: moved research to the built-in assistant agent")
            ensure_agent(cursor)
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


def source_catalog():
    """Owner connectors with the account's opt-ins.

    ``available`` means usable by research on this worker: the connector runs
    here, is connected and its grant covers the research agent.
    """
    from app.core.connectors.registry import get_connector_class
    selections = {s["connector_id"]: s for s in store.get_sources(assistant_id())}
    account = store.get_account()
    if account is not None:
        opted = {cid for cid, on in (account.get("sources") or {}).items() if on}
    else:
        opted = {cid for cid, selected in selections.items() if selected.get("enabled")}
    # Availability is local even though source opt-ins sync with the identity.
    agent_id = (account or store.get_state(assistant_id()) or {}).get("agent_id")
    result = []
    for connector in database.get_all_connectors():
        with get_db() as conn:
            if not store.owner_connector(conn, connector):
                continue
            grant_available = store.connector_grant_available(conn, connector, agent_id)
        selected = selections.get(connector["id"], {})
        try:
            trigger_ids = [t.id for t in get_connector_class(connector["type"]).TRIGGERS]
        except ValueError:
            trigger_ids = []
        on_runner = connector.get("runner_id") == assistant_id() and connector["status"] == "connected"
        available = bool(grant_available and trigger_ids and connector["is_active"] and on_runner)
        result.append(dict(id=connector["id"], connector_id=connector["id"], name=connector["name"], type=connector["type"],
                           enabled=connector["id"] in opted, available=available,
                           status=connector["status"], trigger_ids=trigger_ids,
                           observation_mode="poll_and_events" if connector["type"] == "gmail" else "events",
                           last_observed_at=selected.get("last_observed_at"), last_error=selected.get("last_error")))
    return result


def set_source(connector_id, enabled):
    source = next((s for s in source_catalog() if s["id"] == connector_id), None)
    if not source:
        raise ValueError("Owner connector does not exist")
    if enabled and not source["available"]:
        raise ValueError("Connector is unavailable on this runner or has no observation triggers")
    reconcile()
    with get_db() as conn:
        account = store.lock_account(conn)
        if account is None:
            raise ValueError("Choose an assistant agent first")
        sources = dict(account.get("sources") or {})
        if enabled:
            sources[connector_id] = True
        else:
            sources.pop(connector_id, None)
        _project(conn, store.write_account(conn, {"sources": sources}))
    _publish()
    return next(s for s in source_catalog() if s["id"] == connector_id)


def record_proactive_event(connector_id, event_type, payload):
    """Real connector callbacks enter here; external text is never authorization."""
    connector = database.get_connector(connector_id)
    if not connector:
        return []
    from app.core.connectors.registry import get_connector_class
    try:
        known = {t.id for t in get_connector_class(connector["type"]).TRIGGERS}
    except ValueError:
        return []
    if event_type not in known:
        return []
    # Serialize a bounded data envelope, not instructions or source-controlled
    # role/permissions/overrides. The ID suppresses redelivery across restarts.
    safe_payload = {"data": json.dumps(payload, ensure_ascii=False, default=str)[:12000]}
    external_id = payload.get("message_id") or payload.get("event_id") or payload.get("id")
    return store.record_observations(assistant_id(), connector_id, [(event_type, external_id, safe_payload)])


def proactive_metadata(run):
    # ``home_session_id`` is kept so metadata stamped on an older run's turn
    # still compares equal; runs admitted now carry None.
    deadline = run["deadline_at"]
    return dict(version=1, assistant_id=run["assistant_id"], run_id=run["id"],
                home_session_id=run["home_session_id"], event_ids=run["event_ids"],
                source_ids=run["source_ids"], read_only=True,
                deadline_at=deadline.isoformat() if isinstance(deadline, datetime) else deadline)


def assert_proactive_allowed(metadata, session_id=None):
    """Fail closed at turn start/tool boundaries, including after pause/revoke.

    Successor's enforced ceiling must reject mutation/delegation tools even
    under bypass. Call this for wakes/retries as well as original admission.
    """
    if not isinstance(metadata, dict) or metadata.get("version") != 1 or metadata.get("read_only") is not True:
        raise ValueError("Invalid proactive provenance")
    run = store.get_run(metadata.get("run_id"))
    if not run or run["assistant_id"] != assistant_id() or run["status"] not in store.ACTIVE:
        raise ValueError("Proactive run is no longer admitted")
    if metadata != proactive_metadata(run) or (session_id is not None and run["session_id"] != session_id):
        raise ValueError("Proactive provenance does not match the admitted run")
    deadline = run["deadline_at"]
    if not isinstance(deadline, datetime):
        deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if deadline <= store.now():
        raise ValueError("Proactive run deadline expired")
    with get_db() as conn:
        # A read-only recheck: never take the install mutex here. Tool hooks
        # call this per tool call and must not queue behind admission writers.
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM assistant_state WHERE id=%s", (assistant_id(),))
        state = store._parse(cursor.fetchone())
        if not store.research_allowed(state):
            raise ValueError("Assistant research does not run on this worker" if state and state.get("agent_id")
                             else "Assistant is missing its agent")
        cursor.execute("SELECT id FROM agents WHERE id=%s", (state["agent_id"],))
        if not cursor.fetchone() or state["agent_id"] != run["agent_id"]:
            raise ValueError("Assistant is missing its agent")
        if state["runner_id"] != assistant_id():
            raise ValueError("Assistant placement changed")
        for source_id in run["source_ids"]:
            if not store.trusted_source(conn, state, source_id):
                raise ValueError("Proactive source grant is no longer available")
    if run["session_id"]:
        if not _owned_here(database.get_session(run["session_id"])):
            raise ValueError("Proactive session owner or runner changed")
        if session_stopped(run["session_id"]):
            raise ValueError("The research session was stopped")
    return run


def assert_proactive_source(metadata, connector_id, session_id=None):
    run = assert_proactive_allowed(metadata, session_id)
    if connector_id not in run["source_ids"]:
        raise ValueError("Connector is not in this proactive run's opted-in sources")
    return run


def link_remote_job(caller_session_id, session_id, device_id, kind, label=None):
    """Count a job started on worker ``device_id`` against the owner's spending
    limit when an owner coordination chat (or one of its jobs) started it.
    Returns the link, or None for any other caller."""
    from app.core.orchestration_limits import role
    if role(caller_session_id) is None:
        return None
    return store.link_remote_job(assistant_id(), session_id, device_id, kind, label,
                                 starter_session_id=caller_session_id)


# A research turn with nothing worth telling the owner answers with exactly this.
NOTHING_TO_REPORT = "NOTHING_TO_REPORT"


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

