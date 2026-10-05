"""
Database setup and operations — PostgreSQL backend.

This module is a **facade**: it re-exports every public function from the
domain-specific modules in ``app.db.*`` so that existing callers like
``from app.db.database import get_session`` continue to work.

Only schema initialisation and maintenance helpers live here directly.
"""

import hashlib
import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple

import psycopg2

from app.core.config import settings

# Import shared database utilities from base module.
# Re-exported here so existing ``from app.db.database import get_db`` still works.
from app.db.base import (  # noqa: F401
    get_connection,
    get_db,
    _validate_identifier,
    WorktreeStatus,
    column_exists,
    table_exists,
    column_is_not_null,
    row_to_dict,
    rows_to_list,
)

from app.db.compat import (  # noqa: F401
    ph,
)

# Database error type for catch blocks
_DB_ERRORS: tuple = (psycopg2.Error,)

logger = logging.getLogger(__name__)


def _safe_json(value):
    """Parse JSON if string, return as-is if already parsed (Postgres JSONB returns dicts/lists)."""
    if isinstance(value, str):
        return json.loads(value)
    return value


@contextmanager
def _savepoint(cursor, name: str):
    """Fence a swallow-and-continue block inside the boot transaction.

    The whole migration pass runs in ONE transaction. On real PostgreSQL the
    first error aborts that transaction outright: every later statement fails
    with ``InFailedSqlTransaction`` and the entire pass rolls back, so a boot
    that swallows one harmless error loses all of its work and loops. (The
    SQLite test shim keeps going after an error, which is why the bare
    ``except ...: pass`` blocks survived this long.)

    A savepoint makes the swallow real: on failure only the statements inside
    the block are undone and the transaction stays usable.

    Usage mirrors the hand-written fences in ``schema_postgres`` — the caller
    still owns the ``except``, this only manages the savepoint::

        try:
            with _savepoint(cursor, "sp_thing"):
                cursor.execute(...)
        except _DB_ERRORS:
            pass
    """
    _validate_identifier(name, "savepoint name")
    cursor.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        cursor.execute(f"ROLLBACK TO SAVEPOINT {name}")
        cursor.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        cursor.execute(f"RELEASE SAVEPOINT {name}")


# ============================================================================
# Schema version for migrations
# ============================================================================

# v22: Add credential_policies and api_user_credentials tables
# v23: Worktree-session decoupling - add worktree_id to sessions, remove UNIQUE from worktrees.session_id
# v24: Add unique index on active worktrees to prevent race conditions, fix N+1 queries
# v25: Add api_user_profiles junction table for multi-profile support
# v26: Add built-in subagent support with default values storage and protection
# v27: Remove agent execution engine (agent_runs, agent_tasks, agent_logs tables)
# v28: Add notes table for project-scoped notes feature
# v29: Add project_hooks table for per-project Claude Code event hooks
# v30: Add profile_id to session_messages for per-query profile tracking
# v31: Make notes global (remove project_id FK), remove linked_sessions
# v32: Add mobile device tables (mobile_devices, pending_actions, trusted_actions)
# v33: Add file_path to notes for physical markdown files
# v34: Add connectors tables (connectors, connector_event_log, connector_sessions)
# v35: Add fal_model_catalog table for dynamic model discovery
# v36: Add action_steps column to automations for multi-step connector actions
# v37: Add triggers JSON column for multi-trigger support
# v38: Add message_template to project_hooks, make project_id nullable
# v39: Add queued_messages table for server-side message queueing
# v40: Add instance_id column to sessions for per-instance isolation
# v43: Add session_store_entries + session_store_summaries (SDK 0.1.65 SessionStore)
# v44: Add error_log (app-wide error capture readable by the GetAppErrors MCP tool)
# v45: Add cloud_remote_url + cloud_remote_session_id to sessions (cloud offload mode)
# v46: Add session_id-only partial indexes on session_store_entries for the
#      postgres_only read cutover (idx_sse_session, idx_sse_session_sub)
# v48: Drop cloud_remote_url + cloud_remote_session_id from sessions — the legacy
#      per-session offload proxy is replaced by the cluster intake/relay model
# v49: Add notifications table (unified banner/notification history for the bell)
# v50: Add automation_fires table (atomic cross-install claim per scheduled fire)
# v51: Add sessions.parked_task_kind (durable parked marker, cleared by boot reconcile)
# v52: Add claude_accounts table (multiple Anthropic OAuth accounts + in-app switching)
# v53: Add usage_snapshots table (per-install Claude usage-limits snapshot on a
#      shared DB — the 5-hour window is per-account, so each install persists
#      its own snapshot and the usage endpoint serves them all)
# v54: Add sse_entry_text(entry jsonb) IMMUTABLE function + idx_sse_fts GIN
#      expression index over session_store_entries so full conversation history
#      (assistant/tool content lives in the JSONB store, not session_messages,
#      under postgres_only) is full-text searchable.
# v55: Add background_tasks table (durable per-task lifecycle snapshots,
#      WRITE-ONLY in Phase 1 of the lifecycle unification: task_registry
#      mirrors start/update/complete/unpark transitions through
#      app.core.task_persistence; no reader depends on it yet — boot reconcile
#      marks this install's stale non-terminal rows killed)
# 56: v2 core primitives (session_events, session_event_counters, runs, pending_interactions, artifacts, projects_v2, checkouts)
# 57: sessions.v2_stream — per-session opt-in flag for the V2 streaming path (Phase 1 cutover)
# 58: force v2_stream TRUE on every session + flip the column default TRUE (V2 Phase 1 deletion; the per-session opt-in is retired)
# 59: turns table (V2 Phase 2 — durable per-session turn queue + live-turn state
#     machine, one live turn per session via partial unique index) + one-time
#     migration seeding queued turns from the frozen queued_messages table
# 60: V2 Phase 3 (projects) — additive columns (sessions.project_v2_id,
#     sessions.branch, checkouts.branch) + one-time origin-collapse migration
#     seeding projects_v2 + checkouts from the frozen legacy `projects` rows and
#     rebinding sessions onto their project_v2 (deterministic ids, ON CONFLICT DO
#     NOTHING; legacy `projects`/`link_group_id`/`sessions.project_id` untouched)
# 61: V2 Phase 4 (placement) — additive columns (sessions.placement JSONB,
#     sessions.working_set_ref, artifacts.runner_id) + one-time placement
#     backfill pinning every session with a non-empty instance_id to its owning
#     runner (deterministic/boot-order-independent; stickiness per OD-6); legacy
#     instance_id/cloud_branch/worktree_* NEVER written (sessions with no
#     instance_id stay NULL = local-default)
# 62: Add sessions.context_usage_json JSONB — the last-captured SDK
#     context-window usage breakdown (wrapped as {"captured_at": iso, "data":
#     <raw sdk response>}), written at turn end so the context-usage endpoint
#     can serve a persisted snapshot after the SDK process is evicted from the
#     local pool. Additive/nullable; NULL = never captured.
# 63 (2026-07-16): rename sessions.is_favorite -> is_active (favorites reworked
#     into Active group); index idx_sessions_favorite -> idx_sessions_active.
# 64 (2026-07-17): add model_providers table (admin-configured non-Anthropic
#     model gateways) + idx_model_providers_enabled.
# 65 (2026-07-18): add Codex/ChatGPT-subscription OAuth columns to
#     model_providers (one account per provider — plan amendment A1, no pool
#     table): auth_mode, encrypted write-only codex_access_token /
#     codex_refresh_token, codex_account_id, codex_id_token_expires_at,
#     codex_last_refresh, needs_reauth. All additive/nullable.
# 66 (2026-07-18): add sessions.provider_id TEXT — per-session provider
#     binding. Stamped at session
#     creation from the profile's configured provider; every subsequent
#     turn/spawn resolves the provider from THIS binding, not the live profile
#     (editing a profile never reroutes an existing session). NULL = no
#     provider binding; existing pre-migration sessions stay unbound.
#     One nullable column only (one account per provider, amendment A1 — no
#     provider_account_id). Additive/nullable, no index.
# 67 (2026-07-19): add provider_usage_snapshots table — single row per
#     non-Claude provider (Codex header capture / OpenRouter key-endpoint poll,
#     PROVIDER_USAGE_CONTEXT_PLAN.md U1) holding plan-usage windows, credits and
#     an allowlisted raw payload. Distinct from usage_snapshots (per-install/
#     per-account Claude path). Purely additive.
# 68 (2026-08-03): add pending_wakeups table — durable per-session wakeups
#     (#598). ScheduleWakeup and the idle-at-edge watchdog persist their pending
#     wake here (fire_at as unix epoch), so an app restart rehydrates and re-arms
#     them instead of dropping the bare in-process asyncio timer. Purely additive.
# 69 (2026-08-12): add omni_sessions / omni_session_nodes / omni_session_events
#     (renamed to fluid_* in v77) — PG-backed harness SessionStore (branching
#     node trees + durable event log; see app/core/provider/fluid_pg_store.py).
#     Purely additive; used whenever the app has a database (the fluid pg store
#     is derived, not flagged).
# 70 (2026-08-12): add omni_sessions.cli_session_id TEXT (table renamed in
#     v77) — the delegated engine's ``claude --resume`` handle (a CLI-issued
#     uuid, NOT the fluid session id). It was previously in-process only, so
#     after a restart the adapter resumed the CLI with the fluid session id and
#     every turn died instantly. Additive/nullable, no index.
# v71: agents unification. ``profiles`` is RENAMED in place to
#     ``agents`` (all seven FK consumers follow the rename automatically);
#     ``subagents`` rows are FOLDED into ``agents`` (prompt -> custom
#     system_prompt, tools -> allowed_tools list, memory -> config.memory,
#     inert prov:/subagent_provider fields dropped) and the table is dropped.
#     Child FK COLUMN names (sessions.profile_id etc.) are unchanged — they
#     now reference agents(id). See _migrate_profiles_to_agents().
# 72 (2026-08-13): add pending_wakeups.model TEXT — the scheduling turn's
#     effective model, replayed as the wake query's model override (wakes used to
#     fire on the profile default and re-prime the prompt cache).
# 73 (2026-08-14): add model_providers.api_dialect TEXT — the wire format a
#     ``custom`` row's endpoint speaks: 'anthropic' (default when NULL;
#     Messages at {base_url}/v1/messages, the historic custom-row contract) or
#     'openai' (chat/completions — xAI-direct / Groq / vLLM). Read by the
#     harness adapter's leg decision; lands via the always-run guarded ADD
#     COLUMN in _run_column_migrations.
# 74 (2026-08-14): add sessions.mode TEXT — the session's interaction mode,
#     NULL is the default code mode (every pre-existing session); 'chat' and
#     'voice' (an OpenAI Realtime voice call) are stored explicitly.
#     Additive/nullable with no index, same discipline as sessions.provider_id;
#     lands via the always-run guarded ADD COLUMN in _run_column_migrations.
# 75 (2026-08-15): add sessions.hidden BOOLEAN — list-visibility flag. TRUE
#     keeps a session out of the sessions-list queries (wake-word "command"
#     voice sessions are born hidden — quick one-shot tasks that would litter
#     "recent sessions"); NULL/FALSE (every pre-existing row) stays visible.
#     Hidden rows remain fully fetchable by id. Additive/nullable with no
#     index, same discipline as sessions.mode; lands via the always-run
#     guarded ADD COLUMN in _run_column_migrations.
#   v76 git_ops + upload_blobs — the durable cross-install git-operation queue
#     (D5) and the DB-mediated upload delivery blobs (D8). Both are NEW tables
#     created idempotently by the guarded blocks in _run_column_migrations, so
#     an upgrade from any older version lands them without needing the bootstrap
#     path to fire.
# 77 (2026-08-16): harness rename omni -> fluid. omni_sessions /
#     omni_session_nodes / omni_session_events are RENAMED in place to
#     fluid_sessions / fluid_session_nodes / fluid_session_events (data and
#     constraints carry across ALTER TABLE ... RENAME). Runs before the
#     bootstrap DDL so the idempotent CREATE TABLE IF NOT EXISTS fluid_* can
#     never create an empty table alongside the old one. See
#     _migrate_omni_tables_to_fluid().
# 78 (2026-08-20): one-time device pairing grants, scoped paired-controller
#     sessions, enforced mobile credential expiry/revocation, and runner
#     approval state. Additive and idempotently guarded for rolling upgrades.
# 79 (2026-08-20): add sessions.model / sessions.effort / sessions.permission_mode
#     — the run binding the user picks when the session is created, instead of
#     the one an agent used to pin. ``model`` holds the raw composer selection
#     (a bare id/alias, or a ``prov:<providerId>:<modelId>`` composite whose
#     provider wins over the row's provider_id, exactly like a query-level
#     model override). NULL on every pre-existing row, where resolution falls
#     back to launched_model and then the agent's legacy config — so sessions
#     created before this migration keep running on exactly what they ran on.
#     Additive/nullable with no index; lands via the always-run guarded ADD
#     COLUMN in _run_column_migrations.
# 80 (2026-08-21): add sessions.context_window — the user-selected input-window
#     limit that accompanies the model/effort/permission run binding. NULL keeps
#     the model/provider default for pre-existing sessions.
# 81 (2026-08-21): add sessions.fast — the per-session "fast mode" toggle that
#     rides alongside the model/effort/permission/context_window run binding.
#     TRUE asks the leg for its expedited service (the Claude CLI's
#     ``fastMode`` setting on the delegated lane; ``service_tier: priority`` on
#     the OpenAI Responses lane). NULL/FALSE — every pre-existing row — runs
#     standard, so nothing changes speed or price on upgrade. Additive/nullable
#     with no index; lands via the always-run guarded ADD COLUMN in
#     _run_column_migrations.
# 83 (2026-08-27): add fluid_sessions.project_root — the project the session
#     ran in (fluid's ``project_root_for(cwd)``), recorded at create_session so
#     SessionSearch can scope cross-session recall to one project. NULL on
#     every pre-existing row = "unknown project", which recall never excludes.
#     Additive/nullable, guarded ADD COLUMN alongside cli_session_id.
# 84 (2026-08-28): performance schema pass (docs/PERFORMANCE_REVIEW_2026-08.md
#     findings B3/B4/B7/B11). Three additive, idempotent changes:
#       * ``idx_usage_log_session`` on ``usage_log(session_id)`` — every session
#         open ran ``get_all_session_usage`` (and every delete ran the
#         ``session_id -> NULL`` unlink) as a sequential scan of the ledger (B3).
#       * ``sessions.is_worker BOOLEAN`` — materializes the delegated-worker
#         flag the sessions-list queries used to derive with a correlated
#         ``NOT EXISTS (SELECT 1 FROM turns ...)`` per listed row AND per
#         COUNT(*) row (B7). NULL/FALSE = a normal session. Backfilled once
#         from ``turns.source = 'provider_agent'`` (marker-gated, idempotent).
#       * ``session_store_summaries.folded_through_id BIGINT NOT NULL DEFAULT 0``
#         — watermark for the coalesced summary fold (B4): the fold no longer
#         runs on every ~100ms streaming append, so it must know which entries
#         it has already folded. 0 on every pre-existing row means "fold
#         everything from the start", which is exactly right for a summary that
#         is a pure function of the entries.
#     Also ``idx_projects_instance`` for boot's orphan-adoption scan (B11).
# 85 (2026-09-02): persist pull-request lifecycle on sessions so the sidepanel
#     can show PR Opened / PR Merged without a live GitHub poll. Additive
#     nullable pr_number / pr_url / pr_state; NULL on every pre-existing row.
# 86: per-chat additional workspace projects, stored as project IDs.
# 87 (2026-09-08): worker network. Local network tables (network_state,
#     network_members, sync_ops, sync_records, sync_watermarks, sync_pending,
#     sync_conflicts, sync_peers, network_joins, ...) and nullable
#     ``owner_device_id`` on sessions / automations / connectors / artifacts,
#     ``blob_hash`` on note_attachments. All additive.
# 89 (2026-09-24): retire the in-app Claude sign-in's storage. AI Shuffle no
#     longer keeps Claude credentials: drop ``claude_accounts``, delete the
#     ``claude_active_account:*`` pointers, purge ``claude_accounts`` envelopes
#     from sync_ops / sync_pending / sync_records / sync_conflicts, and drop
#     ``usage_snapshots.account_id/account_email/account_name``. When the old
#     sign-in left rows or pointers, the one-time CLI re-login notice is
#     recorded first (``_retire_claude_accounts``).
# 90 (2026-09-25): add usage_log.usage_estimated BOOLEAN DEFAULT FALSE — TRUE
#     when any token count booked for that turn was estimated by the harness
#     (chars/4, because the provider reported no usage or the reply was
#     interrupted). Budgets and caps keep counting the estimated amounts; the
#     flag only marks them as estimates in the usage panel and per-turn cost
#     footers. Every pre-existing row reads FALSE. Additive, no index; lands
#     via the always-run guarded ADD COLUMN in _run_column_migrations.
#     Also retires message hooks (``hook_type = 'message'``), a type that was
#     never delivered: their ``project_hooks`` rows are deleted, along with
#     their sync envelopes, and ``project_hooks.message_template`` is dropped
#     (``_retire_message_hooks``).
#     Also retires storage no route reaches any more
#     (``_retire_webhooks_and_legacy_push``): the standalone outbound
#     ``webhooks`` table and its sync envelopes (the management API had no
#     client), ``mobile_devices.unifiedpush_endpoint`` (the Android app has no
#     UnifiedPush receiver; the FCM token of a row that carried an endpoint is
#     cleared first, so such a row stays unable to receive push), and the
#     ``rate_limits`` / ``request_log`` tables the removed rate-limit feature
#     left behind.
# 92 (2026-10-03): automations carry their own run model. Nullable
#     ``automations.model`` (the picker value: a native Claude id or a
#     ``prov:<providerId>:<modelId>`` composite) and ``automations.provider_id``.
#     NULL on every pre-existing row, which keeps those automations on the
#     fallback they already used (the agent's saved model, then the app
#     default). Additive; lands via the always-run guarded ADD COLUMN in
#     _run_column_migrations.
SCHEMA_VERSION = 92


def _migrate_omni_tables_to_fluid(cursor):
    """v77 pre-schema migration: rename the harness store tables
    ``omni_sessions`` / ``omni_session_nodes`` / ``omni_session_events`` ->
    ``fluid_sessions`` / ``fluid_session_nodes`` / ``fluid_session_events``
    (the harness itself was renamed omni -> fluid).

    MUST run before ``create_postgres_schema`` — otherwise the idempotent
    ``CREATE TABLE IF NOT EXISTS fluid_*`` would create a fresh empty table
    alongside the still-named ``omni_*`` one and strand every session row
    (same ordering rule as _migrate_profiles_to_agents).

    Data, primary keys and indexes carry across ``ALTER TABLE ... RENAME``;
    session ids themselves (``ses_<hex12>``) never contained the harness name,
    so no row content changes.
    """
    for old, new in (
        ("omni_sessions", "fluid_sessions"),
        ("omni_session_nodes", "fluid_session_nodes"),
        ("omni_session_events", "fluid_session_events"),
    ):
        if table_exists(cursor, old) and not table_exists(cursor, new):
            cursor.execute(f"ALTER TABLE {old} RENAME TO {new}")
            logger.info("v77: renamed %s -> %s", old, new)


def _migrate_profiles_to_agents(cursor):
    """v71 pre-schema migration: rename ``profiles`` -> ``agents`` and fold
    ``subagents`` rows in.

    MUST run before ``create_postgres_schema`` — otherwise the idempotent
    ``CREATE TABLE IF NOT EXISTS agents`` would create a fresh empty table
    alongside the still-named ``profiles`` and orphan every FK row.

    Postgres carries FK constraints across ``ALTER TABLE ... RENAME``, so the
    seven referencing tables (sessions, session_messages, usage_log,
    templates, api_users, api_user_profiles, permission_rules, automations)
    need no changes.
    """
    import json as _json

    if table_exists(cursor, "profiles") and not table_exists(cursor, "agents"):
        cursor.execute("ALTER TABLE profiles RENAME TO agents")
        logger.info("v71: renamed profiles -> agents")

    if not table_exists(cursor, "subagents") or not table_exists(cursor, "agents"):
        return

    cursor.execute("SELECT * FROM subagents")
    rows = [dict(r) for r in (cursor.fetchall() or [])]
    folded = 0
    for row in rows:
        agent_id = row["id"]
        cursor.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
        if cursor.fetchone():
            agent_id = f"{agent_id}-agent"
            cursor.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
            if cursor.fetchone():
                logger.warning("v71 fold: skipping subagent %s (id collision)", row["id"])
                continue

        config = subagent_row_to_agent_config(row)
        cursor.execute(
            """INSERT INTO agents (id, name, description, is_builtin, config, mcp_tools,
                                   created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                agent_id,
                row.get("name") or agent_id,
                row.get("description"),
                bool(row.get("is_builtin")),
                _json.dumps(config),
                _json.dumps([]),
                row.get("created_at"),
                row.get("updated_at"),
            ),
        )
        folded += 1
    cursor.execute("DROP TABLE subagents")
    logger.info("v71: folded %d subagent row(s) into agents; subagents dropped", folded)


def subagent_row_to_agent_config(row: dict) -> dict:
    """Map an old ``subagents`` row onto an agent config blob (v71 fold shape).

    Shared by the in-place v71 migration and the sqlite->postgres import tool.
    ``prov:`` model composites (inert) are dropped — the agent
    inherits the session model instead.
    """
    import json as _json

    def _load(v):
        if v is None or isinstance(v, (list, dict)):
            return v
        try:
            return _json.loads(v)
        except (ValueError, TypeError):
            return None

    tools = _load(row.get("tools"))
    model = row.get("model")
    if model and str(model).startswith("prov:"):
        model = None
    config = {
        "system_prompt": {"type": "custom", "content": row.get("prompt") or ""},
    }
    if row.get("memory"):
        config["memory"] = row["memory"]
    if model:
        config["model"] = model
    if isinstance(tools, list) and tools:
        config["allowed_tools"] = tools
    skills = _load(row.get("skills"))
    if skills:
        config["skills"] = skills
    mcp_servers = _load(row.get("mcp_servers"))
    if mcp_servers:
        config["mcp_servers"] = mcp_servers
    return config


def _migrate_agent_tool_access(cursor, migrations_applied=None):
    """v82: stamp ``tool_selection_mode`` onto every agent
    config and fold the retired ``ai_tools`` / ``app_config_tools`` switches
    into the tool lists.

    Before T2 the runtime inferred intent from list emptiness, so
    ``allowed_tools: []`` ("Disable all tools" in the editor) read as
    *unrestricted* everywhere. Stamping the mode the editor was actually
    showing preserves each agent's visible intent while letting the runtime
    drop the inference entirely.

    Idempotent: an agent whose config already carries one of the four valid
    modes is left alone, so this can run on every startup (which it does — it
    sits in the always-run column-migration pass, like every other guarded
    block here).
    """
    import json as _json

    if not table_exists(cursor, "agents"):
        return

    from app.core.tool_access import TOOL_SELECTION_MODES, fold_legacy_tool_switches

    cursor.execute("SELECT id, config FROM agents")
    rows = cursor.fetchall() or []
    stamped = 0
    for row in rows:
        raw = row["config"]
        if isinstance(raw, str):
            try:
                config = _json.loads(raw)
            except (TypeError, ValueError):
                continue
        elif isinstance(raw, dict):
            config = raw
        else:
            continue
        if config.get("tool_selection_mode") in TOOL_SELECTION_MODES:
            continue
        try:
            updated = fold_legacy_tool_switches(config)
        except Exception:
            logger.warning("v82: could not fold tool access for agent %s", row["id"], exc_info=True)
            continue
        cursor.execute(
            "UPDATE agents SET config = %s WHERE id = %s",
            (_json.dumps(updated), row["id"]),
        )
        stamped += 1

    if stamped and migrations_applied is not None:
        migrations_applied.append(f"agents.tool_selection_mode({stamped})")
    if stamped:
        logger.info("v82: stamped tool_selection_mode onto %d agent(s)", stamped)


def _migrate_preference_revisions(cursor):
    """Backfill opaque preference revisions without changing values/timestamps."""
    from uuid import uuid4

    if not table_exists(cursor, "user_preferences"):
        return
    if not column_exists(cursor, "user_preferences", "revision"):
        cursor.execute("ALTER TABLE user_preferences ADD COLUMN revision TEXT")
    cursor.execute("SELECT user_type, user_id, key FROM user_preferences WHERE revision IS NULL")
    for row in cursor.fetchall():
        cursor.execute(
            "UPDATE user_preferences SET revision = %s WHERE user_type = %s AND user_id = %s AND key = %s AND revision IS NULL",
            (str(uuid4()), row["user_type"], row["user_id"], row["key"]),
        )


def _migrate_provider_revisions(cursor):
    """Opaque binding generations must not depend on install clock resolution."""
    from uuid import uuid4

    for table, key in (("system_settings", "key"), ("model_providers", "id")):
        if not table_exists(cursor, table):
            continue
        if not column_exists(cursor, table, "revision"):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN revision TEXT")
        cursor.execute(f"SELECT {key} FROM {table} WHERE revision IS NULL")
        for row in cursor.fetchall():
            cursor.execute(f"UPDATE {table} SET revision = %s WHERE {key} = %s AND revision IS NULL",
                           (str(uuid4()), row[key]))


def _run_column_migrations(cursor):
    """Run idempotent column migrations.

    These migrations run on EVERY startup to ensure columns exist,
    regardless of schema version. This handles cases where:
    - Schema version was bumped but migration didn't run
    - Database was restored from backup
    - Docker volume has stale schema version
    """
    migrations_applied = []
    _migrate_preference_revisions(cursor)
    from app.db.assistant import create_schema as create_assistant_schema
    create_assistant_schema(cursor)
    from app.db.assistant_limits import create_schema as create_assistant_limits_schema
    create_assistant_limits_schema(cursor)

    # Session messages profile tracking (v30)
    if table_exists(cursor, "session_messages"):
        if not column_exists(cursor, "session_messages", "profile_id"):
            cursor.execute(
                "ALTER TABLE session_messages ADD COLUMN profile_id TEXT"
            )
            migrations_applied.append("session_messages.profile_id")

    # (v71) subagents column migrations removed — the table is folded into
    # ``agents`` by _migrate_profiles_to_agents() and no longer exists.

    # Drop unique index that prevented multiple worktrees on same branch
    # (Users should be able to have multiple worktrees on the same branch)
    # ``IF EXISTS`` already covers the only failure the old try/except caught,
    # and a bare swallow here would abort the whole boot transaction on real
    # Postgres for anything else.
    cursor.execute("DROP INDEX IF EXISTS idx_worktrees_active_branch")
    migrations_applied.append("dropped idx_worktrees_active_branch")

    # Migrate notes table to global (remove project_id and linked_sessions) (v31)
    if table_exists(cursor, "notes") and column_exists(cursor, "notes", "project_id"):
        cursor.execute("ALTER TABLE notes DROP COLUMN IF EXISTS project_id")
        cursor.execute("ALTER TABLE notes DROP COLUMN IF EXISTS linked_sessions")
        cursor.execute("DROP INDEX IF EXISTS idx_notes_project")
        # Recreate indexes (project index no longer needed)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(is_pinned DESC, updated_at DESC)")
        migrations_applied.append("notes_table_global")

    # Migrate sessions.profile_id to be nullable (for profile deletion preserving sessions)
    # Older databases may have profile_id as NOT NULL, but we need it nullable so
    # sessions can persist after their associated profile is deleted
    if table_exists(cursor, "sessions") and column_is_not_null(cursor, "sessions", "profile_id"):
        logger.info("Migrating sessions table to make profile_id nullable...")
        cursor.execute("ALTER TABLE sessions ALTER COLUMN profile_id DROP NOT NULL")
        # Recreate indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id)")
        # NOTE: keep the legacy is_favorite index name here. This branch runs
        # early (while an ancient DB may still have profile_id NOT NULL and the
        # is_favorite column), long before the v63 block below renames the
        # column to is_active — referencing is_active here would raise
        # undefined-column and abort the whole migration pass. The v63 block
        # renames this index to idx_sessions_active once the column is renamed.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_favorite ON sessions(is_favorite)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_worktree ON sessions(worktree_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_sdk_session_id ON sessions(sdk_session_id)")
        migrations_applied.append("sessions.profile_id_nullable")

    # Add file_path column to notes for physical markdown files (v33)
    if table_exists(cursor, "notes") and not column_exists(cursor, "notes", "file_path"):
        cursor.execute("ALTER TABLE notes ADD COLUMN file_path TEXT")
        migrations_applied.append("notes.file_path")

    # Note attachments — uploaded files referenceable via @ inside a note.
    # Created idempotently here so existing databases pick it up without a
    # schema-version bump.
    if table_exists(cursor, "notes") and not table_exists(cursor, "note_attachments"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS note_attachments (
                id TEXT PRIMARY KEY,
                note_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER DEFAULT 0,
                data BYTEA,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_note_attachments_note ON note_attachments(note_id)"
        )
        migrations_applied.append("note_attachments")

    # Attachment bytes live in the shared database so installs that share it can
    # serve attachments uploaded on another machine. Older databases have the
    # table without the column; add it idempotently.
    if table_exists(cursor, "note_attachments") and not column_exists(cursor, "note_attachments", "data"):
        cursor.execute("ALTER TABLE note_attachments ADD COLUMN data BYTEA")
        migrations_applied.append("note_attachments.data")

    # Add worktree_branch and worktree_path columns to sessions for persistent worktree info
    # These store worktree info directly on the session so it survives worktree deletion
    if table_exists(cursor, "sessions"):
        if not column_exists(cursor, "sessions", "worktree_branch"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN worktree_branch TEXT")
            migrations_applied.append("sessions.worktree_branch")
        if not column_exists(cursor, "sessions", "worktree_path"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN worktree_path TEXT")
            migrations_applied.append("sessions.worktree_path")
        # Cloud (no-worktree) offload branch: the branch the worker checks out on
        # its main checkout for a cloud session. Persisted by the primary at intake
        # so the branch a cloud session runs on survives reloads and shows in the
        # UI (cloud+worktree sessions already carry it via worktree_branch).
        if not column_exists(cursor, "sessions", "cloud_branch"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN cloud_branch TEXT")
            migrations_applied.append("sessions.cloud_branch")
        # Automation provenance: which automation created this session (NULL =
        # user/API session). Lets the sessions list badge automation runs —
        # they execute headless, so they read differently from live chats.
        # No FK: automations are deletable and the badge should survive.
        if not column_exists(cursor, "sessions", "automation_id"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN automation_id TEXT")
            migrations_applied.append("sessions.automation_id")
            # Backfill from run history for sessions created before the column.
            # Pre-checked rather than try/except: "automation_runs may not exist
            # yet" was the only thing the old swallow covered, and a swallowed
            # error inside this single boot transaction poisons every statement
            # after it on real Postgres.
            if table_exists(cursor, "automation_runs"):
                cursor.execute("""
                    UPDATE sessions SET automation_id = (
                        SELECT ar.automation_id FROM automation_runs ar
                        WHERE ar.session_id = sessions.id
                        ORDER BY ar.created_at DESC LIMIT 1
                    )
                    WHERE automation_id IS NULL
                      AND id IN (SELECT session_id FROM automation_runs
                                 WHERE session_id IS NOT NULL)
                """)
                if cursor.rowcount > 0:
                    logger.info(
                        f"Backfilled automation_id for {cursor.rowcount} sessions"
                    )
        # Durable parked marker (v51): non-NULL means the parent turn ended
        # while background work (a sub-agent, or an SDK-backgrounded shell) was
        # still running — the session is "parked" awaiting an unpark follow-up.
        # Written at turn end by turn_finalize.persist_turn_end_state, cleared
        # at the next turn start, and force-cleared at boot by
        # app.core.startup_reconcile (no background work survives a restart).
        if not column_exists(cursor, "sessions", "parked_task_kind"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN parked_task_kind TEXT")
            migrations_applied.append("sessions.parked_task_kind")
        # Effective model the CLI/SDK session was launched (or switched) with,
        # written at spawn/link time so the Unraid host-op guard
        # (scripts/unraid-lab/host_op_guard.py) can read the session's model
        # authoritatively without waiting for turn-end session_messages.metadata['model'].
        if not column_exists(cursor, "sessions", "launched_model"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN launched_model TEXT")
            migrations_applied.append("sessions.launched_model")
        # ``launched_model`` is overwritten by every mid-session model switch,
        # so it answers "what is this session on NOW", never "what did it start
        # on". A session that ran turns 1-6 on grok-4.5 and 7-16 on grok-4.6
        # read as grok-4.6 only, and nothing could attribute the early turns.
        # Written once and never overwritten (see sessions.update_session);
        # pre-existing rows stay NULL rather than being back-stamped with a
        # model that may not be the one they started on.
        if not column_exists(cursor, "sessions", "voice_config"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN voice_config JSONB")
            migrations_applied.append("sessions.voice_config")
        if not column_exists(cursor, "sessions", "first_model"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN first_model TEXT")
            migrations_applied.append("sessions.first_model")
        # True token accounting. `total_tokens_in` holds FRESH input
        # only — the provider normalizes usage Anthropic-style, so cache reads are
        # excluded — while the per-turn cache numbers only ever landed in the
        # OVERWRITE snapshot columns (cache_read_tokens / cache_creation_tokens).
        # On a cache-heavy session that hid ~90% of the prompt tokens ever spent.
        # These three columns accumulate what the snapshots throw away.
        if not column_exists(cursor, "sessions", "total_cache_read_tokens"):
            cursor.execute(
                "ALTER TABLE sessions ADD COLUMN total_cache_read_tokens BIGINT DEFAULT 0"
            )
            migrations_applied.append("sessions.total_cache_read_tokens")
        if not column_exists(cursor, "sessions", "total_cache_creation_tokens"):
            cursor.execute(
                "ALTER TABLE sessions ADD COLUMN total_cache_creation_tokens BIGINT DEFAULT 0"
            )
            migrations_applied.append("sessions.total_cache_creation_tokens")
        # `turn_count` was fed the fluid path's num_turns, which is the MODEL-CALL
        # count (harness_adapter sets num_turns=event.num_model_calls) — a 1-turn
        # child read as 16 turns. turn_count now counts turns; the model calls get
        # their own column.
        if not column_exists(cursor, "sessions", "total_model_calls"):
            cursor.execute(
                "ALTER TABLE sessions ADD COLUMN total_model_calls INTEGER DEFAULT 0"
            )
            migrations_applied.append("sessions.total_model_calls")
        # INTEGER caps at 2.1B, which a long-lived session's cumulative prompt
        # tokens now genuinely reach. ALTER ... TYPE rewrites the table, so it is
        # wrapped in an EXISTS guard rather than re-run every boot. A DO block
        # (Postgres-only, and skipped wholesale by the SQLite test shim, whose
        # INTEGER is already 64-bit). The guard reads pg_attribute rather than
        # information_schema deliberately: the shim rewrites any statement
        # mentioning information_schema.columns into a two-parameter pragma
        # query before it ever gets to skip it.
        try:
            with _savepoint(cursor, "sp_tokens_bigint"):
                cursor.execute("""
                    DO $$ BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_attribute a
                            WHERE a.attrelid = 'sessions'::regclass
                              AND a.attname IN ('total_tokens_in', 'total_tokens_out')
                              AND a.atttypid <> 'bigint'::regtype
                        ) THEN
                            ALTER TABLE sessions
                                ALTER COLUMN total_tokens_in TYPE BIGINT,
                                ALTER COLUMN total_tokens_out TYPE BIGINT;
                        END IF;
                    END $$
                """)
        except _DB_ERRORS:
            pass  # a backend without DO blocks — the columns stay as they are
        # Legacy cloud offload removal (v48): the per-session proxy model
        # (cloud_remote_url / cloud_remote_session_id) is superseded by the
        # cluster intake/relay, which shares one session id across installs via
        # the instance_id owner field. Drop the now-dead columns idempotently.
        if column_exists(cursor, "sessions", "cloud_remote_url"):
            cursor.execute("ALTER TABLE sessions DROP COLUMN cloud_remote_url")
            migrations_applied.append("drop sessions.cloud_remote_url")
        if column_exists(cursor, "sessions", "cloud_remote_session_id"):
            cursor.execute("ALTER TABLE sessions DROP COLUMN cloud_remote_session_id")
            migrations_applied.append("drop sessions.cloud_remote_session_id")
        # Backfill for existing sessions with active worktrees. Pre-checked
        # instead of swallowed: "worktrees may not exist yet" is the whole of
        # what the old except covered, and a swallow inside the boot
        # transaction takes every later statement down with it on Postgres.
        if "sessions.worktree_branch" in migrations_applied and table_exists(
            cursor, "worktrees"
        ):
            cursor.execute("""
                UPDATE sessions SET
                    worktree_branch = (SELECT w.branch_name FROM worktrees w WHERE w.id = sessions.worktree_id),
                    worktree_path = (SELECT w.worktree_path FROM worktrees w WHERE w.id = sessions.worktree_id)
                WHERE worktree_id IS NOT NULL AND worktree_branch IS NULL
            """)
            backfilled = cursor.rowcount
            if backfilled > 0:
                logger.info(f"Backfilled worktree_branch/worktree_path for {backfilled} sessions")

    # Per-turn cache usage on the usage log. Without these a usage row
    # records only the fresh input tokens, so replaying the log can never
    # reconstruct what a cache-heavy turn actually sent to the provider.
    if table_exists(cursor, "usage_log"):
        if not column_exists(cursor, "usage_log", "settlement_key"):
            cursor.execute("ALTER TABLE usage_log ADD COLUMN settlement_key TEXT")
            migrations_applied.append("usage_log.settlement_key")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_log_settlement_key "
            "ON usage_log(settlement_key)"
        )
        if not column_exists(cursor, "usage_log", "cache_read_tokens"):
            cursor.execute(
                "ALTER TABLE usage_log ADD COLUMN cache_read_tokens BIGINT DEFAULT 0"
            )
            migrations_applied.append("usage_log.cache_read_tokens")
        if not column_exists(cursor, "usage_log", "cache_creation_tokens"):
            cursor.execute(
                "ALTER TABLE usage_log ADD COLUMN cache_creation_tokens BIGINT DEFAULT 0"
            )
            migrations_applied.append("usage_log.cache_creation_tokens")
        # (v90) usage estimates: turns whose token counts the harness guessed.
        if not column_exists(cursor, "usage_log", "usage_estimated"):
            cursor.execute(
                "ALTER TABLE usage_log ADD COLUMN usage_estimated BOOLEAN DEFAULT FALSE"
            )
            migrations_applied.append("usage_log.usage_estimated")
        # Per-provider attribution for the Stats breakdown. Older rows stay
        # NULL; the breakdown attributes them from the session's binding.
        for column in ("provider_id", "provider_type", "billing"):
            if not column_exists(cursor, "usage_log", column):
                cursor.execute(f"ALTER TABLE usage_log ADD COLUMN {column} TEXT")
                migrations_applied.append(f"usage_log.{column}")

    # Add action_steps column to automations for multi-step connector actions (v36)
    if table_exists(cursor, "automations") and not column_exists(cursor, "automations", "action_steps"):
        cursor.execute("ALTER TABLE automations ADD COLUMN action_steps TEXT DEFAULT NULL")
        migrations_applied.append("automations.action_steps")

    # (v92) Per-automation run model choice. NULL = fall back as before.
    if table_exists(cursor, "automations"):
        for column in ("model", "provider_id"):
            if not column_exists(cursor, "automations", column):
                cursor.execute(f"ALTER TABLE automations ADD COLUMN {column} TEXT")
                migrations_applied.append(f"automations.{column}")

    # Add triggers JSON column for multi-trigger support (v37)
    if table_exists(cursor, "automations") and not column_exists(cursor, "automations", "triggers"):
        cursor.execute("ALTER TABLE automations ADD COLUMN triggers TEXT DEFAULT NULL")
        migrations_applied.append("automations.triggers")
        # Backfill: convert existing trigger_type/trigger_config into triggers array
        cursor.execute("SELECT id, trigger_type, trigger_config FROM automations")
        rows = cursor.fetchall()
        for row in rows:
            auto_id = row[0] if isinstance(row, (list, tuple)) else row["id"]
            t_type = row[1] if isinstance(row, (list, tuple)) else row["trigger_type"]
            t_config = row[2] if isinstance(row, (list, tuple)) else row["trigger_config"]
            if t_config and isinstance(t_config, str):
                try:
                    t_config_parsed = json.loads(t_config)
                except (ValueError, TypeError):
                    t_config_parsed = {}
            else:
                t_config_parsed = t_config if isinstance(t_config, dict) else {}
            triggers_arr = json.dumps([{"type": t_type, "config": t_config_parsed}])
            cursor.execute("UPDATE automations SET triggers = %s WHERE id = %s", (triggers_arr, auto_id))
        migrations_applied.append("automations.triggers_backfill")

    # Make project_hooks.project_id nullable (v38)
    if table_exists(cursor, "project_hooks") and column_is_not_null(cursor, "project_hooks", "project_id"):
        logger.info("Migrating project_hooks table to make project_id nullable...")
        cursor.execute("ALTER TABLE project_hooks ALTER COLUMN project_id DROP NOT NULL")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_project_id ON project_hooks(project_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_event ON project_hooks(event)")
        migrations_applied.append("project_hooks.project_id_nullable")

    # Add instance_id to sessions for per-instance isolation across shared DBs.
    # Mirrors the projects.instance_id column added in v33-era multi-instance work.
    if table_exists(cursor, "sessions"):
        if not column_exists(cursor, "sessions", "instance_id"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN instance_id TEXT")
            # Backfill existing sessions from their project's instance_id so that
            # legacy sessions stay visible on the instance that owns the project.
            # Pre-checked (projects may not exist yet in pathological cases)
            # rather than swallowed inside the boot transaction.
            if table_exists(cursor, "projects") and column_exists(
                cursor, "projects", "instance_id"
            ):
                cursor.execute("""
                    UPDATE sessions SET instance_id = (
                        SELECT p.instance_id FROM projects p WHERE p.id = sessions.project_id
                    )
                    WHERE instance_id IS NULL AND project_id IS NOT NULL
                """)
            migrations_applied.append("sessions.instance_id")
        # Always ensure the index exists (idempotent for fresh installs + upgrades)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_instance ON sessions(instance_id)")
        # Backfill any remaining sessions with NULL instance_id (e.g. ungrouped
        # sessions without a project_id that the original migration missed).
        # Assigns them to the current instance so they stop leaking across installs.
        try:
            with _savepoint(cursor, "sp_instance_backfill"):
                from app.core.config import settings
                _ph = ph()
                cursor.execute(
                    f"UPDATE sessions SET instance_id = {_ph} WHERE instance_id IS NULL",
                    (settings.effective_instance_id,)
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    migrations_applied.append(f"sessions.instance_id_backfill({cursor.rowcount} rows)")
        except _DB_ERRORS:
            pass

    # Persist per-session context-window usage so the context bar can
    # render correctly after loading a session from history (no JSONL scan
    # required). Populated by _persist_turn_results on every turn.
    if table_exists(cursor, "sessions"):
        for col in ("last_input_tokens", "cache_creation_tokens", "cache_read_tokens"):
            if not column_exists(cursor, "sessions", col):
                _validate_identifier(col, "column name")
                cursor.execute(f"ALTER TABLE sessions ADD COLUMN {col} INTEGER DEFAULT 0")
                migrations_applied.append(f"sessions.{col}")

    # Rewind DB<->JSONL sync: store the Claude SDK JSONL uuid on each message
    # row so a rewind can truncate both stores consistently. A dedicated column
    # (rather than nesting inside metadata JSON) keeps rewind's lookup indexed
    # and portable across SQLite/Postgres. See fix/rewind-db-history-sync.
    if table_exists(cursor, "session_messages"):
        if not column_exists(cursor, "session_messages", "jsonl_uuid"):
            cursor.execute("ALTER TABLE session_messages ADD COLUMN jsonl_uuid TEXT")
            migrations_applied.append("session_messages.jsonl_uuid")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_messages_session_uuid "
            "ON session_messages(session_id, jsonl_uuid)"
        )

    # Mobile companion app: columns added after the table was first created.
    # (The v88-era ``unifiedpush_endpoint`` column is dropped by
    # ``_retire_webhooks_and_legacy_push`` below.)
    if table_exists(cursor, "mobile_devices"):
        for column, definition in (
            ("paired_client_id", "TEXT"),
            ("api_key_expires_at", "TIMESTAMP WITH TIME ZONE"),
            (
                "scopes",
                "JSONB NOT NULL DEFAULT '[\"controller\", \"mobile:notifications\", "
                "\"mobile:actions\", \"web:session\"]'",
            ),
            ("paired_at", "TIMESTAMP WITH TIME ZONE"),
            ("revoked_at", "TIMESTAMP WITH TIME ZONE"),
        ):
            if not column_exists(cursor, "mobile_devices", column):
                _validate_identifier(column, "column name")
                cursor.execute(f"ALTER TABLE mobile_devices ADD COLUMN {column} {definition}")
                migrations_applied.append(f"mobile_devices.{column}")

    # Additive account-hub migration, included in this module's fingerprint.
    from app.db.mobile_hub import create_schema as create_mobile_hub_schema
    create_mobile_hub_schema(cursor)

    # v78 controller pairing. These blocks run on every boot so an interrupted
    # rolling upgrade cannot leave the new version without its security tables.
    if not table_exists(cursor, "paired_clients"):
        cursor.execute("""
            CREATE TABLE paired_clients (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL UNIQUE,
                device_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                client_type TEXT NOT NULL DEFAULT 'browser',
                scopes JSONB NOT NULL DEFAULT '["controller"]',
                session_token_hash TEXT UNIQUE,
                session_expires_at TIMESTAMP WITH TIME ZONE,
                paired_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_seen TIMESTAMP WITH TIME ZONE,
                revoked_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("paired_clients")
    if not table_exists(cursor, "pairing_grants"):
        cursor.execute("""
            CREATE TABLE pairing_grants (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                purpose TEXT NOT NULL DEFAULT 'controller',
                scopes JSONB NOT NULL DEFAULT '["controller"]',
                target_install_id TEXT,
                endpoint_url TEXT,
                device_label TEXT,
                created_by TEXT NOT NULL DEFAULT 'admin',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                consumed_at TIMESTAMP WITH TIME ZONE,
                revoked_at TIMESTAMP WITH TIME ZONE,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 10
            )
        """)
        migrations_applied.append("pairing_grants")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pairing_grants_token ON pairing_grants(token_hash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pairing_grants_active ON pairing_grants(expires_at) WHERE consumed_at IS NULL AND revoked_at IS NULL")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paired_clients_session ON paired_clients(session_token_hash) WHERE session_token_hash IS NOT NULL")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_paired_clients_active ON paired_clients(revoked_at, paired_at DESC)")
    # A native voice transport must not replace the WebView's session. One
    # bounded auxiliary slot per device; parent revocation gates both slots.
    cursor.execute("""CREATE TABLE IF NOT EXISTS paired_client_native_sessions (
        client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS paired_client_web_reservations (
        client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
        mobile_record_id TEXT NOT NULL, api_key_hash TEXT NOT NULL,
        reservation_hash TEXT NOT NULL UNIQUE,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL
    )""")

    # Existing installs predate approval state and are trusted in place. New
    # rows created after this migration choose their approval explicitly.
    if table_exists(cursor, "installs"):
        added_approval = False
        if not column_exists(cursor, "installs", "approved_at"):
            cursor.execute("ALTER TABLE installs ADD COLUMN approved_at TIMESTAMP WITH TIME ZONE")
            migrations_applied.append("installs.approved_at")
            added_approval = True
        if not column_exists(cursor, "installs", "revoked_at"):
            cursor.execute("ALTER TABLE installs ADD COLUMN revoked_at TIMESTAMP WITH TIME ZONE")
            migrations_applied.append("installs.revoked_at")
        if added_approval:
            cursor.execute(
                "UPDATE installs SET approved_at = COALESCE(created_at, NOW()) "
                "WHERE approved_at IS NULL"
            )

    # Ensure performance-critical indexes exist (idempotent, no version gate).
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)")

    # SDK 0.1.65 SessionStore tables (v43). Idempotent so upgrades from any
    # older version land the tables without requiring a SCHEMA_VERSION bump
    # to fire the schema bootstrap path (same defensive pattern as the
    # other column-migrations above).
    if not table_exists(cursor, "session_store_entries"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_store_entries (
                id BIGSERIAL PRIMARY KEY,
                project_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                subpath TEXT,
                entry JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        migrations_applied.append("session_store_entries")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_main "
        "ON session_store_entries(project_key, session_id, id) "
        "WHERE subpath IS NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_sub "
        "ON session_store_entries(project_key, session_id, subpath, id) "
        "WHERE subpath IS NOT NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_project "
        "ON session_store_entries(project_key) "
        "WHERE subpath IS NULL"
    )
    # PR C.1 read cutover (v46): session_id-only partial indexes for the app's
    # detail/resume read path, which matches on session_id ALONE (the
    # SDK-derived project_key diverges from the stored folder name). Idempotent
    # CREATE INDEX IF NOT EXISTS so existing/live DBs pick them up on startup.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_session "
        "ON session_store_entries (session_id, id) "
        "WHERE subpath IS NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_session_sub "
        "ON session_store_entries (session_id, subpath, id) "
        "WHERE subpath IS NOT NULL"
    )
    if not table_exists(cursor, "session_store_summaries"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_store_summaries (
                project_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                data JSONB NOT NULL,
                mtime_ms BIGINT NOT NULL,
                PRIMARY KEY (project_key, session_id)
            )
        """)
        migrations_applied.append("session_store_summaries")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sss_project "
        "ON session_store_summaries(project_key)"
    )

    # store-backed FTS (v54). Under postgres_only the assistant/tool history is
    # NOT in session_messages — the full transcript lives in the JSONB
    # session_store_entries.entry. sse_entry_text() extracts searchable text
    # from a user/assistant entry (skipping meta/sidechain rows), and the GIN
    # expression index over its tsvector makes the store searchable. The
    # function MUST be IMMUTABLE so it can back an expression index. Idempotent
    # CREATE OR REPLACE / CREATE INDEX IF NOT EXISTS so upgrades from any older
    # version pick it up on startup without needing the SCHEMA_VERSION bump to
    # fire the bootstrap path (same defensive pattern as the SessionStore block
    # above). The function must be created before the index that references it.
    cursor.execute("""
        CREATE OR REPLACE FUNCTION sse_entry_text(entry jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        AS $fn$
            SELECT CASE
                WHEN COALESCE(entry->>'type', '') NOT IN ('user', 'assistant') THEN NULL
                WHEN COALESCE((entry->>'isMeta')::boolean, false) THEN NULL
                WHEN COALESCE((entry->>'isSidechain')::boolean, false) THEN NULL
                WHEN jsonb_typeof(entry #> '{message,content}') = 'string'
                    THEN entry #>> '{message,content}'
                WHEN jsonb_typeof(entry #> '{message,content}') = 'array'
                    THEN (
                        SELECT string_agg(block ->> 'text', ' ')
                        FROM jsonb_array_elements(entry #> '{message,content}') AS block
                        WHERE block ->> 'type' = 'text'
                    )
                ELSE NULL
            END
        $fn$
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_fts "
        "ON session_store_entries USING gin (to_tsvector('english', sse_entry_text(entry))) "
        "WHERE subpath IS NULL"
    )

    # error_log (v44). Idempotent so upgrades from any older version land the
    # table without needing the SCHEMA_VERSION bump to fire the bootstrap path
    # (same defensive pattern as the SessionStore tables above).
    if not table_exists(cursor, "error_log"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_log (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                level TEXT DEFAULT 'ERROR',
                message TEXT NOT NULL,
                detail TEXT,
                logger_name TEXT,
                url TEXT,
                status_code INTEGER,
                session_id TEXT,
                user_id TEXT,
                context JSONB,
                fingerprint TEXT,
                occurrence_count INTEGER DEFAULT 1,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("error_log")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_source ON error_log(source)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_session ON error_log(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_fingerprint ON error_log(fingerprint)")

    # installs (v47). Cross-install registry: each install self-registers a row
    # keyed by its instance_id so peers can see every install and relay control
    # to the owning device. Idempotent so upgrades from any older version land
    # the table without needing the SCHEMA_VERSION bump to fire the bootstrap
    # path (same defensive pattern as the SessionStore / error_log blocks above).
    #
    # approved_at / revoked_at are declared HERE as well as in the ADD COLUMN
    # block further up. That block runs earlier in the pass and is guarded on
    # ``table_exists(installs)``, so on a database whose version is current but
    # whose ``installs`` table is missing (restore, hand edit) it is skipped and
    # this CREATE is the only thing that lands the table — it used to land it
    # without the approval columns, and nothing else ever added them.
    if not table_exists(cursor, "installs"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS installs (
                id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                base_url TEXT,
                platform TEXT,
                version TEXT,
                capabilities JSONB DEFAULT '{}',
                approved_at TIMESTAMP WITH TIME ZONE,
                revoked_at TIMESTAMP WITH TIME ZONE,
                last_seen TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("installs")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_installs_last_seen ON installs(last_seen DESC)")

    # claude_accounts (v52) is no longer created; v89 drops it (see
    # _retire_claude_accounts below).

    # model_providers (v64). Admin-configured non-Anthropic model gateways
    # (OpenRouter / custom Anthropic-compatible endpoints). Idempotent so
    # upgrades from any older version land the table without needing the
    # SCHEMA_VERSION bump to fire the bootstrap path (same defensive pattern as
    # the SessionStore / error_log / installs blocks above).
    # The index is bootstrapped unconditionally (CREATE INDEX IF NOT EXISTS) so
    # it exists on both fresh installs and upgrades.
    if not table_exists(cursor, "model_providers"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS model_providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                base_url TEXT NOT NULL,
                auth_token TEXT,
                models_json TEXT DEFAULT '[]',
                default_model TEXT,
                small_fast_model TEXT,
                extra_env_json TEXT DEFAULT '{}',
                context_window INTEGER,
                max_output_tokens INTEGER,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("model_providers")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_providers_enabled ON model_providers(enabled)"
    )

    # model_providers Codex/ChatGPT-subscription OAuth columns (v65). One
    # account per provider (plan amendment A1 — no separate pool table): the
    # Codex OAuth credential lives 1:1 on the provider's single-credential row.
    # Additive/idempotent: on an EXISTING v64 model_providers table
    # create_postgres_schema's CREATE TABLE IF NOT EXISTS is a no-op, so these
    # guarded ADD COLUMNs are the sole path that lands them on an upgrade (same
    # discipline as the column migrations above). ``codex_access_token`` /
    # ``codex_refresh_token`` are encrypted (``enc:``) write-only blobs, never
    # exposed on public reads (mirrors ``auth_token``). No index is added on any
    # of these, so there is no schema-pass-before-column ordering trap here.
    #
    # 2026-08-16: cross-instance refresh lease. ``codex_refresh_lease_owner`` /
    # ``codex_refresh_lease_expires_at`` let the periodic refresh worker claim a
    # provider row via a conditional UPDATE on the DB clock before POSTing — the
    # Codex refresh token rotates, so two installs sharing this database
    # refreshing the same row kills the credential. Still no index on any of
    # these columns, so the "no ordering trap" property above is preserved.
    if table_exists(cursor, "model_providers"):
        _codex_provider_columns = (
            ("auth_mode", "TEXT"),
            ("codex_access_token", "TEXT"),
            ("codex_refresh_token", "TEXT"),
            ("codex_account_id", "TEXT"),
            ("codex_id_token_expires_at", "TIMESTAMP WITH TIME ZONE"),
            ("codex_last_refresh", "TIMESTAMP WITH TIME ZONE"),
            ("needs_reauth", "BOOLEAN DEFAULT FALSE"),
            ("codex_refresh_lease_owner", "TEXT"),
            ("codex_refresh_lease_expires_at", "TIMESTAMP WITH TIME ZONE"),
            ("openai_admin_key", "TEXT"),
            ("openai_billing_org_id", "TEXT"),
            ("openai_billing_instance_id", "TEXT"),
        )
        for _col, _decl in _codex_provider_columns:
            if not column_exists(cursor, "model_providers", _col):
                cursor.execute(
                    f"ALTER TABLE model_providers ADD COLUMN {_col} {_decl}"
                )
                migrations_applied.append(f"model_providers.{_col}")

        # model_providers.api_dialect (2026-08-14): which wire format a
        # ``custom`` row's endpoint speaks — 'anthropic' (default when NULL;
        # Anthropic Messages at {base_url}/v1/messages, the historic custom-row
        # contract) or 'openai' (OpenAI chat/completions, for xAI-direct /
        # Groq / vLLM style endpoints). Read by the harness adapter's leg
        # decision; meaningless on non-custom rows.
        if not column_exists(cursor, "model_providers", "api_dialect"):
            cursor.execute("ALTER TABLE model_providers ADD COLUMN api_dialect TEXT")
            migrations_applied.append("model_providers.api_dialect")

    # projects.link_group_id (Phase D). Groups same-repo project rows across
    # installs into one logical project on the shared-DB sessions board. The id
    # is derived deterministically from the git remote (identical on every
    # install), so existing clones can be backfilled immediately.
    if table_exists(cursor, "projects") and not column_exists(cursor, "projects", "link_group_id"):
        cursor.execute("ALTER TABLE projects ADD COLUMN link_group_id TEXT")
        migrations_applied.append("projects.link_group_id")
        # Backfill from each project's git remote. Compute in Python to reuse the
        # canonical normalizer; best-effort so a startup never fails on it.
        if table_exists(cursor, "git_repositories"):
            try:
                with _savepoint(cursor, "sp_link_group_backfill"):
                    from app.core.project_links import auto_link_group_id
                    cursor.execute(
                        """
                        SELECT p.id AS project_id, r.remote_url AS remote_url
                        FROM projects p
                        JOIN git_repositories r ON r.project_id = p.id
                        WHERE r.remote_url IS NOT NULL AND r.remote_url <> ''
                        """
                    )
                    backfill = [(row["project_id"], row["remote_url"]) for row in cursor.fetchall()]
                    for pid, remote_url in backfill:
                        gid = auto_link_group_id(remote_url)
                        if gid:
                            cursor.execute(
                                "UPDATE projects SET link_group_id = %s WHERE id = %s AND link_group_id IS NULL",
                                (gid, pid),
                            )
            except Exception:  # noqa: BLE001 — backfill is best-effort
                logger.warning("link_group_id backfill skipped", exc_info=True)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_link_group ON projects(link_group_id)")

    # notifications (v49). Unified banner/notification history backing the bell.
    # Idempotent so upgrades from any older version land the table without needing
    # the SCHEMA_VERSION bump to fire the bootstrap path (same defensive pattern as
    # the SessionStore / error_log / installs blocks above). Kept separate from
    # error_log (debug observability) — this is the user-facing message layer.
    if not table_exists(cursor, "notifications"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                severity TEXT NOT NULL DEFAULT 'info',
                surface TEXT NOT NULL DEFAULT 'notification',
                category TEXT,
                source TEXT NOT NULL DEFAULT 'server',
                title TEXT,
                message TEXT NOT NULL,
                action_kind TEXT,
                action_payload JSONB,
                session_id TEXT,
                read BOOLEAN DEFAULT FALSE,
                dismissed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("notifications")
    # Preserve existing rows, but ensure omitted surface values on upgraded
    # installations cannot recreate the retired ``toast`` wire value.
    cursor.execute(
        "ALTER TABLE notifications ALTER COLUMN surface SET DEFAULT 'notification'"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read)")

    # automation_fires (v50). Cross-install scheduled-fire claim table: the
    # primary-key insert elects exactly one install per scheduled tick, closing
    # the double-fire window left by the fail-open runner election. Idempotent
    # so upgrades from any older version land the table without needing the
    # SCHEMA_VERSION bump to fire the bootstrap path (same defensive pattern as
    # the notifications block above).
    if not table_exists(cursor, "automation_fires"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS automation_fires (
                automation_id TEXT NOT NULL,
                fire_bucket TEXT NOT NULL,
                claimed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (automation_id, fire_bucket)
            )
        """)
        migrations_applied.append("automation_fires")

    # usage_snapshots (v53). Per-install Claude usage-limits snapshot keyed by
    # instance_id: the windows that install's CLI reported, so the usage
    # endpoint can serve every install's. The account_* columns it carried
    # until v89 are dropped by _retire_claude_accounts. Idempotent so upgrades
    # from any older version land the table without needing the SCHEMA_VERSION
    # bump to fire the bootstrap path (same defensive pattern as the
    # automation_fires block above).
    if not table_exists(cursor, "usage_snapshots"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage_snapshots (
                instance_id TEXT PRIMARY KEY,
                windows JSONB NOT NULL DEFAULT '[]',
                available BOOLEAN DEFAULT FALSE,
                generation BIGINT,
                fetched_at BIGINT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("usage_snapshots")

    # provider_usage_snapshots (v67). Single row per non-Claude provider
    # (ChatGPT/Codex via the loopback gateway, OpenRouter on the direct base-URL
    # path) capturing plan-usage/limit windows, credits and the allowlisted raw
    # source payload. Distinct from usage_snapshots (per-install/per-account for
    # the Claude path). Idempotent so upgrades from any older version land the
    # table without needing the SCHEMA_VERSION bump to fire the bootstrap path
    # (same defensive pattern as the usage_snapshots block above).
    if not table_exists(cursor, "provider_usage_snapshots"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS provider_usage_snapshots (
                provider_id TEXT PRIMARY KEY,
                provider_type TEXT,
                plan_type TEXT,
                windows JSONB NOT NULL DEFAULT '[]',
                credits JSONB,
                raw JSONB,
                fetched_at BIGINT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("provider_usage_snapshots")

    # Durable upstream request budgets. Network peers coordinate with the founder;
    # replicating these rows asynchronously would allow duplicate claims.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_poll_state (
            identity TEXT PRIMARY KEY,
            next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            paused BOOLEAN NOT NULL DEFAULT FALSE,
            lease_token TEXT
        )
    """)
    if not column_exists(cursor, "usage_poll_state", "lease_token"):
        cursor.execute("ALTER TABLE usage_poll_state ADD COLUMN lease_token TEXT")

    # pending_wakeups (v68). Durable per-session wakeups (#598): ScheduleWakeup
    # and the idle-at-edge watchdog persist their pending wake here so an app
    # restart rehydrates and re-arms them instead of dropping the bare in-process
    # asyncio timer. ``fire_at`` is a unix epoch (DOUBLE PRECISION) so the
    # rehydrate math is timezone/shim-agnostic. Idempotent so upgrades from any
    # older version land the table without needing the SCHEMA_VERSION bump to fire
    # the bootstrap path (same defensive pattern as the usage_snapshots block).
    if not table_exists(cursor, "pending_wakeups"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_wakeups (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                profile_id TEXT,
                project_id TEXT,
                prompt TEXT NOT NULL,
                reason TEXT,
                model TEXT,
                source TEXT NOT NULL DEFAULT 'schedule_wakeup',
                instance_id TEXT,
                fire_at DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("pending_wakeups")

    # pending_wakeups.model (v72). The scheduling turn's effective model,
    # replayed as the wake query's model override — without it a wake ran on the
    # profile default and re-primed the prompt cache on the wrong model.
    if not column_exists(cursor, "pending_wakeups", "model"):
        cursor.execute("ALTER TABLE pending_wakeups ADD COLUMN model TEXT")
        migrations_applied.append("pending_wakeups.model")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_wakeups_instance "
        "ON pending_wakeups(instance_id, fire_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_wakeups_session "
        "ON pending_wakeups(session_id)"
    )

    # fluid_sessions.cli_session_id (v70). The delegated engine's
    # ``claude --resume`` handle, persisted so a restart reconnects to the same
    # CLI conversation instead of resuming with the fluid session id. Guarded on
    # table_exists because fluid_sessions is only created by the bootstrap path
    # (v69, as omni_sessions until the v77 rename); nothing here should create
    # it on an older database.
    if table_exists(cursor, "fluid_sessions") and not column_exists(
        cursor, "fluid_sessions", "cli_session_id"
    ):
        cursor.execute("ALTER TABLE fluid_sessions ADD COLUMN cli_session_id TEXT")
        migrations_applied.append("fluid_sessions.cli_session_id")

    # fluid_sessions.project_root (v83). The session's project identity
    # (fluid's project_root_for over the session cwd), recorded at creation so
    # SessionSearch can scope recall to one project. Same table_exists guard as
    # cli_session_id above.
    if table_exists(cursor, "fluid_sessions") and not column_exists(
        cursor, "fluid_sessions", "project_root"
    ):
        cursor.execute("ALTER TABLE fluid_sessions ADD COLUMN project_root TEXT")
        migrations_applied.append("fluid_sessions.project_root")

    # Cross-session recall trigram indexes. SessionSearch's SQL prefilter is a
    # substring ILIKE over the raw JSON of both transcript stores; without an
    # index that is a sequential scan of every message row in the install, per
    # call. pg_trgm GIN expression indexes serve exactly that ILIKE shape.
    # Fenced by a savepoint because CREATE EXTENSION needs privileges some
    # managed databases refuse — recall then simply stays unindexed (correct,
    # just slower) instead of aborting the whole migration transaction.
    # Idempotent, no version gate (same defensive pattern as the FTS block).
    if table_exists(cursor, "fluid_session_nodes"):
        cursor.execute("SAVEPOINT recall_trgm")
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_fluid_nodes_trgm "
                "ON fluid_session_nodes USING gin ((message::text) gin_trgm_ops)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sse_entry_trgm "
                "ON session_store_entries USING gin ((entry::text) gin_trgm_ops) "
                "WHERE subpath IS NULL"
            )
            cursor.execute("RELEASE SAVEPOINT recall_trgm")
        except Exception:  # noqa: BLE001 — degraded recall beats a dead boot
            cursor.execute("ROLLBACK TO SAVEPOINT recall_trgm")
            logger.info(
                "pg_trgm unavailable — SessionSearch's prefilter stays unindexed"
            )

    # background_tasks (v55). Durable per-task lifecycle snapshots — WRITE-ONLY
    # in Phase 1 (no reader flips): task_registry mirrors its transitions here
    # via app.core.task_persistence so a restart/crash leaves a durable trail,
    # and boot reconcile marks this install's stale non-terminal rows killed.
    # Idempotent so upgrades from any older version land the table without
    # needing the SCHEMA_VERSION bump to fire the bootstrap path (same
    # defensive pattern as the usage_snapshots block above).
    if not table_exists(cursor, "background_tasks"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS background_tasks (
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                instance_id TEXT,
                kind TEXT,
                is_sdk_background BOOLEAN DEFAULT FALSE,
                tool_use_id TEXT,
                title TEXT,
                status TEXT,
                terminal_notified BOOLEAN DEFAULT FALSE,
                summary TEXT,
                reason TEXT,
                report_path TEXT,
                report_chars BIGINT,
                agent TEXT,
                roster_name TEXT,
                model TEXT,
                remote_device_id TEXT,
                started_at TIMESTAMP WITH TIME ZONE,
                updated_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (task_id, session_id)
            )
        """)
        migrations_applied.append("background_tasks")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_background_tasks_instance_status "
        "ON background_tasks(instance_id, status)"
    )
    # Preserve bounded terminal evidence across restart, not just its status.
    # agent/roster_name/model keep a Task worker addressable by the name its
    # parent knew it by after a restart rebuilds the registry from these rows.
    # remote_device_id marks a Task child running on another worker: a restart
    # here does not stop it, so its running row survives the boot reconcile.
    for name, sql_type in (
        ("summary", "TEXT"), ("reason", "TEXT"),
        ("report_path", "TEXT"), ("report_chars", "BIGINT"),
        ("agent", "TEXT"), ("roster_name", "TEXT"), ("model", "TEXT"),
        ("remote_device_id", "TEXT"),
    ):
        if not column_exists(cursor, "background_tasks", name):
            cursor.execute(f"ALTER TABLE background_tasks ADD COLUMN {name} {sql_type}")
            migrations_applied.append(f"background_tasks_{name}")

    # Retire background_tasks.unparked. No SCHEMA_VERSION bump: editing this
    # file changes migrations_fingerprint(), which re-runs the column pass once
    # on the next boot, which is all a drop needs. The unpark latch it mirrored was
    # replaced by the in-memory one-shot in task_registry.mark_woken and the
    # idle reader's terminal handoff (see idle_reader's module docstring); no
    # writer has set the column since, so every row reads FALSE and audits kept
    # mistaking that for "the wake never fired". Nothing reads it either, so the
    # drop is safe.
    if table_exists(cursor, "background_tasks") and column_exists(
        cursor, "background_tasks", "unparked"
    ):
        cursor.execute("ALTER TABLE background_tasks DROP COLUMN IF EXISTS unparked")
        migrations_applied.append("background_tasks_drop_unparked")

    # v2 core primitives (v56). session_events (append-only log) + its
    # session_event_counters companion, fenced/leased runs, pending_interactions,
    # content-addressed artifacts, and projects_v2/checkouts. Idempotent so
    # upgrades from any older version land the tables without needing the
    # SCHEMA_VERSION bump to fire the bootstrap path (same defensive pattern as
    # the background_tasks block above).
    if not table_exists(cursor, "session_events"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_events (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq BIGINT NOT NULL,
                run_id TEXT,
                turn_id TEXT,
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}',
                command_id TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE (session_id, seq)
            )
        """)
        migrations_applied.append("session_events")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_events_session_seq "
        "ON session_events(session_id, seq)"
    )

    if not table_exists(cursor, "session_event_counters"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_event_counters (
                session_id TEXT PRIMARY KEY,
                next_seq BIGINT NOT NULL DEFAULT 0
            )
        """)
        migrations_applied.append("session_event_counters")

    if not table_exists(cursor, "runs"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                runner_id TEXT NOT NULL,
                fence_token BIGINT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("runs")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_session "
        "ON runs(session_id) WHERE state = 'active'"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id)"
    )

    if not table_exists(cursor, "pending_interactions"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_interactions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                run_id TEXT,
                kind TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'pending',
                answer JSONB,
                answered_by_device TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE,
                answered_at TIMESTAMP WITH TIME ZONE
            )
        """)
        migrations_applied.append("pending_interactions")
    # Nonblocking questions remain answerable until the person resolves them.
    cursor.execute("ALTER TABLE pending_interactions ALTER COLUMN expires_at DROP NOT NULL")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_interactions_session_state "
        "ON pending_interactions(session_id, state)"
    )

    if not table_exists(cursor, "artifacts"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS artifacts (
                hash TEXT PRIMARY KEY,
                size BIGINT NOT NULL,
                mime TEXT,
                filename TEXT,
                created_by_session TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("artifacts")

    if not table_exists(cursor, "projects_v2"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects_v2 (
                id TEXT PRIMARY KEY,
                origin TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                settings JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("projects_v2")

    if not table_exists(cursor, "checkouts"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS checkouts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects_v2(id) ON DELETE CASCADE,
                runner_id TEXT NOT NULL,
                path TEXT NOT NULL,
                head_sha TEXT,
                dirty BOOLEAN DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE (project_id, runner_id, path)
            )
        """)
        migrations_applied.append("checkouts")

    # ------------------------------------------------------------------
    # V2 Phase 3 (v60) — origin collapse.
    #
    # Additive columns + a one-time, idempotent, concurrent-boot-safe seed that
    # folds the legacy per-install `projects` rows into origin-keyed `projects_v2`
    # rows with one `checkouts` row per legacy project (its per-install
    # materialization), then rebinds sessions onto their project_v2. Modeled on
    # the v-D link_group_id backfill and the v59 turns<-queued_messages seed:
    # deterministic ids computed in Python (no md5/sha in SQL, no DISTINCT ON) +
    # ON CONFLICT DO NOTHING make it safe to re-run every boot and safe when two
    # installs boot Phase-3 code against the shared DB simultaneously.
    #
    # Purely additive — the legacy `projects`, `link_group_id`,
    # `sessions.project_id`, and `sessions.worktree_*` columns are NEVER updated
    # or dropped, so an un-upgraded install sees exactly the DB it saw before plus
    # some new columns/rows it ignores. Runs UNCONDITIONALLY (no flag gate): the
    # seeded rows are dormant until later Phase-3 commits read them.

    # Step A: additive columns (idempotent; guarded so the ALTER never re-runs).
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "project_v2_id"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS project_v2_id TEXT")
        migrations_applied.append("sessions.project_v2_id")
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "branch"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS branch TEXT")
        migrations_applied.append("sessions.branch")
    if table_exists(cursor, "checkouts") and not column_exists(cursor, "checkouts", "branch"):
        cursor.execute("ALTER TABLE checkouts ADD COLUMN IF NOT EXISTS branch TEXT")
        migrations_applied.append("checkouts.branch")

    # Steps B–D: the collapse. Guarded so it is a no-op when the legacy or v2
    # tables are absent, and safe when `projects` is empty.
    if (
        table_exists(cursor, "projects")
        and table_exists(cursor, "projects_v2")
        and table_exists(cursor, "checkouts")
    ):
        import hashlib

        def _origin_for(legacy_row) -> str:
            # origin = the existing link_group_id grouping token
            # (git:… / manual:…), else a deterministic 'local:<legacy id>' synth
            # for remote-less rows (project ids are PKs → collision-free).
            return legacy_row["link_group_id"] or ("local:" + legacy_row["id"])

        def _pv2_id(origin: str) -> str:
            # Deterministic across installs & re-runs — the same origin always
            # yields the same projects_v2 id (computed in Python, not SQL).
            return "pv2-" + hashlib.sha256(origin.encode()).hexdigest()[:24]

        cursor.execute(
            "SELECT id, name, path, instance_id, link_group_id FROM projects"
        )
        legacy_projects = cursor.fetchall()

        # Step B: one projects_v2 row per DISTINCT origin (name = a
        # representative legacy project name for that origin).
        representative_name = {}
        for row in legacy_projects:
            origin = _origin_for(row)
            representative_name.setdefault(origin, row["name"])
        seeded_pv2 = 0
        for origin, name in representative_name.items():
            cursor.execute(
                "INSERT INTO projects_v2 (id, origin, name, settings) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (origin) DO NOTHING",
                (_pv2_id(origin), origin, name or origin, "{}"),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                seeded_pv2 += 1
        if seeded_pv2:
            migrations_applied.append(f"projects_v2<-projects({seeded_pv2})")

        # Step C: one checkout per legacy projects row (its per-install
        # materialization). runner_id = COALESCE(instance_id,''); branch NULL
        # (the base checkout, D7). Deterministic id 'ck-<legacy id>'. Bare ON
        # CONFLICT DO NOTHING (NOT `(id)`-scoped) so BOTH arbiters are honored:
        # the `id` PK AND the UNIQUE (project_id, runner_id, path). Two legacy
        # projects rows that share an origin+instance_id+path (duplicate-path
        # legacy rows exist in the wild) yield distinct `ck-<id>` ids but collide
        # on the (project_id, runner_id, path) unique — `(id)`-scoped conflict
        # would raise UniqueViolation and abort EVERY boot; bare skips the dup.
        seeded_ck = 0
        for row in legacy_projects:
            origin = _origin_for(row)
            cursor.execute(
                "INSERT INTO checkouts "
                "(id, project_id, runner_id, path, branch, status) "
                "VALUES (%s, %s, %s, %s, NULL, 'active') "
                "ON CONFLICT DO NOTHING",
                ("ck-" + row["id"], _pv2_id(origin), row["instance_id"] or "", row["path"]),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                seeded_ck += 1
        if seeded_ck:
            migrations_applied.append(f"checkouts<-projects({seeded_ck})")

        # Step D: rebind sessions onto project_v2 (only rows not already bound;
        # NEVER clears the legacy project_id — old code still reads it).
        if column_exists(cursor, "sessions", "project_v2_id"):
            rebound = 0
            for row in legacy_projects:
                origin = _origin_for(row)
                cursor.execute(
                    "UPDATE sessions SET project_v2_id = %s "
                    "WHERE project_id = %s AND project_v2_id IS NULL",
                    (_pv2_id(origin), row["id"]),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    rebound += cursor.rowcount
            if rebound:
                migrations_applied.append(f"sessions.project_v2_id({rebound})")

        # Steps E–F: seed BRANCH checkouts from the legacy `worktrees` rows and
        # backfill `sessions.branch`. Steps B–D seed only BASE checkouts, so a
        # legacy worktree-bound session (worktree_id set, sessions.branch NULL)
        # would resolve to the base path under the v2 resolver — a cwd
        # regression. These two steps close that gap.
        #
        # A legacy worktree joins to its owning project via
        # git_repositories.project_id (worktrees.repository_id ->
        # git_repositories.id -> git_repositories.project_id -> projects.id).
        # Same guarantees as B–D: table_exists guards, deterministic ids + ON
        # CONFLICT DO NOTHING (re-run = zero writes), safe on empty tables,
        # concurrent-boot safe, never touches a legacy column.
        if table_exists(cursor, "worktrees") and table_exists(cursor, "git_repositories"):
            # Map legacy project id -> (origin, runner_id) from the rows already
            # fetched above (reuse, no re-query).
            project_by_id = {row["id"]: row for row in legacy_projects}

            cursor.execute(
                "SELECT w.id AS wt_id, w.branch_name AS branch_name, "
                "w.worktree_path AS worktree_path, gr.project_id AS project_id "
                "FROM worktrees w "
                "JOIN git_repositories gr ON gr.id = w.repository_id"
            )
            worktree_rows = cursor.fetchall()

            # Step E: one branch checkout per legacy worktree row. STATUS-BLIND —
            # seed regardless of worktrees.status (the per-device sync sweep flips
            # it spuriously; checkout_manager stats the dir at use time, the real
            # check). Deterministic id 'ck-wt-<worktree id>'. Bare ON CONFLICT DO
            # NOTHING (NOT `(id)`-scoped) so BOTH arbiters are honored: the `id`
            # PK AND the UNIQUE (project_id, runner_id, path). Two worktree rows
            # sharing a path+branch under one project yield distinct
            # `ck-wt-<id>` ids but collide on the (project_id, runner_id, path)
            # unique — `(id)`-scoped conflict would raise UniqueViolation and
            # abort EVERY boot; bare skips the dup.
            seeded_wt = 0
            for wt in worktree_rows:
                legacy = project_by_id.get(wt["project_id"])
                if legacy is None:
                    # Worktree points at a repo whose project row is gone —
                    # nothing to key the origin off of; skip defensively.
                    continue
                if not wt["worktree_path"] or not wt["branch_name"]:
                    continue
                origin = _origin_for(legacy)
                cursor.execute(
                    "INSERT INTO checkouts "
                    "(id, project_id, runner_id, path, branch, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'active') "
                    "ON CONFLICT DO NOTHING",
                    (
                        "ck-wt-" + wt["wt_id"],
                        _pv2_id(origin),
                        legacy["instance_id"] or "",
                        wt["worktree_path"],
                        wt["branch_name"],
                    ),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    seeded_wt += 1
            if seeded_wt:
                migrations_applied.append(f"checkouts<-worktrees({seeded_wt})")

            # Step F: backfill sessions.branch per worktree row (only where NULL;
            # NEVER overwrite a non-NULL branch, NEVER touch worktree_id/
            # worktree_branch/worktree_path — old code still reads those).
            if column_exists(cursor, "sessions", "branch"):
                branch_backfilled = 0
                for wt in worktree_rows:
                    if not wt["branch_name"]:
                        continue
                    cursor.execute(
                        "UPDATE sessions SET branch = %s "
                        "WHERE worktree_id = %s AND branch IS NULL",
                        (wt["branch_name"], wt["wt_id"]),
                    )
                    if cursor.rowcount and cursor.rowcount > 0:
                        branch_backfilled += cursor.rowcount
                if branch_backfilled:
                    migrations_applied.append(f"sessions.branch({branch_backfilled})")

        # Step G: consolidate duplicate projects_v2 rows and purge orphans.
        # Two failure modes accumulated in the wild, both rooted in the same
        # gap: origin used to derive from a lazily-populated link_group_id, so
        # a project seeded as `local:<id>` pre-poll re-seeded under its
        # `git:<hash>` once the remote was read — one logical repo, two v2
        # rows. And rows whose legacy `projects` row is gone everywhere were
        # never cleaned up. Rebind sessions + checkouts onto the surviving
        # row, then delete the twin/orphan. Deterministic ids + guarded WHERE
        # clauses keep it idempotent (re-run = zero writes) and concurrent-boot
        # safe; legacy columns are never touched.
        if table_exists(cursor, "projects_v2"):
            cursor.execute("SELECT id, name, origin FROM projects_v2")
            pv2_rows = cursor.fetchall()
            merged, purged = 0, 0

            # Pass 1 — twin merge: a local:-keyed row whose name also has a
            # git:/manual:-keyed sibling is the pre-backfill duplicate; the
            # sibling wins (it is what every post-poll derivation resolves to).
            for row in pv2_rows:
                origin = row["origin"]
                if not origin.startswith("local:"):
                    continue

                survivor = next(
                    (r for r in pv2_rows if r["id"] != row["id"] and r["name"] == row["name"]
                     and r["origin"].startswith(("git:", "manual:"))),
                    None,
                )
                if survivor is None:
                    continue
                cursor.execute(
                    "UPDATE sessions SET project_v2_id = %s WHERE project_v2_id = %s",
                    (survivor["id"], row["id"]),
                )
                moved_sessions = cursor.rowcount or 0
                # A twin checkout colliding with an existing survivor checkout
                # (same runner + path, e.g. both seeded 'ck-<legacy id>') cannot
                # be moved onto the survivor's UNIQUE (project_id, runner_id,
                # path) — drop it as the duplicate it is instead of stranding a
                # reference to the row about to be deleted.
                cursor.execute(
                    "DELETE FROM checkouts WHERE project_id = %s AND EXISTS ("
                    "SELECT 1 FROM checkouts c2 WHERE c2.project_id = %s "
                    "AND c2.id <> checkouts.id "
                    "AND c2.runner_id = checkouts.runner_id "
                    "AND c2.path IS NOT DISTINCT FROM checkouts.path)",
                    (row["id"], survivor["id"]),
                )
                cursor.execute(
                    "UPDATE checkouts SET project_id = %s WHERE project_id = %s",
                    (survivor["id"], row["id"]),
                )
                moved_checkouts = cursor.rowcount or 0
                cursor.execute("DELETE FROM projects_v2 WHERE id = %s", (row["id"],))
                deleted = cursor.rowcount or 0
                if deleted:
                    merged += 1
                    migrations_applied.append(
                        f"projects_v2.merge({row['name']}:local->"
                        f"{survivor['origin'][:12]}..,{moved_sessions}s,{moved_checkouts}c)"
                    )

            # Orphan purge (second pass — needs post-merge state): a local:-keyed
            # row whose legacy projects id exists NOWHERE is a leftover registry
            # entry (deleted project, dropped E2E fixture). Purge it only when
            # sessions AND checkouts are both empty so an in-use row can never
            # vanish; rows with references keep surfacing their history through
            # the removed-project handling instead of stranding it.
            cursor.execute("SELECT id, name, origin FROM projects_v2")
            pv2_rows = cursor.fetchall()
            cursor.execute("SELECT id FROM projects")
            all_legacy_ids = {r["id"] for r in cursor.fetchall()}
            for row in pv2_rows:
                origin = row["origin"]
                if not origin.startswith("local:"):
                    continue
                legacy_id = origin[len("local:"):]
                if legacy_id in all_legacy_ids:
                    continue
                cursor.execute("SELECT 1 FROM sessions WHERE project_v2_id = %s LIMIT 1", (row["id"],))
                has_sessions = cursor.fetchone() is not None
                cursor.execute("SELECT 1 FROM checkouts WHERE project_id = %s LIMIT 1", (row["id"],))
                has_checkouts = cursor.fetchone() is not None
                if has_sessions or has_checkouts:
                    continue
                cursor.execute("DELETE FROM projects_v2 WHERE id = %s", (row["id"],))
                if cursor.rowcount and cursor.rowcount > 0:
                    purged += 1
            if merged:
                migrations_applied.append(f"projects_v2.twins-merged({merged})")
            if purged:
                migrations_applied.append(f"projects_v2.orphans-purged({purged})")

    # turns — durable per-session turn queue + live-turn state machine (v59,
    # V2 Phase 2). Idempotent so upgrades from any older version land the table
    # (same defensive pattern as the v2-core block above). The partial unique
    # index enforces one live turn (running|interrupting|draining) per session.
    if not table_exists(cursor, "turns"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',
                source TEXT NOT NULL,
                prompt TEXT,
                profile_id TEXT,
                project_id TEXT,
                worktree_id TEXT,
                overrides JSONB NOT NULL DEFAULT '{}',
                payload JSONB NOT NULL DEFAULT '{}',
                command_id TEXT,
                parent_turn_id TEXT,
                run_id TEXT,
                interrupt_mode TEXT,
                stop_background BOOLEAN NOT NULL DEFAULT FALSE,
                position DOUBLE PRECISION,
                instance_id TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                started_at TIMESTAMP WITH TIME ZONE,
                ended_at TIMESTAMP WITH TIME ZONE,
                stop_reason TEXT,
                error TEXT
            )
        """)
        migrations_applied.append("turns")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_one_live_per_session "
        "ON turns(session_id) WHERE state IN ('running','interrupting','draining')"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_session_state ON turns(session_id, state)"
    )

    # Latest generated composer hint; retained even with no watching browser.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS composer_suggestions (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
            suggestion TEXT NOT NULL
        )
    """)

    # One-time seed: carry every existing queued_messages row into turns as a
    # source='queue' queued turn, preserving order via position. queued_messages
    # is left in place, frozen (dropped in a later deletion PR). Deterministic
    # turn ids ('turn-qm-<qm.id>') + ON CONFLICT DO NOTHING make this safe to
    # re-run every boot — an already-seeded (or since-settled) row is never
    # duplicated.
    if table_exists(cursor, "turns") and table_exists(cursor, "queued_messages"):
        cursor.execute("""
            INSERT INTO turns
                (id, session_id, state, source, prompt, profile_id, project_id,
                 position, created_at)
            SELECT 'turn-qm-' || id, session_id, 'queued', 'queue', content,
                   profile_id, project_id, position, created_at
            FROM queued_messages
            WHERE true
            ON CONFLICT (id) DO NOTHING
        """)
        if cursor.rowcount and cursor.rowcount > 0:
            migrations_applied.append(f"turns<-queued_messages({cursor.rowcount})")

    # Per-session V2 streaming column (v57). Originally a per-session opt-in;
    # the v58 block below forces it TRUE on every session and flips the default.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "v2_stream"):
        cursor.execute(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS v2_stream BOOLEAN NOT NULL DEFAULT FALSE"
        )
        migrations_applied.append("sessions.v2_stream")

    # Force every session onto the V2 streaming path (v58). The per-session
    # opt-in is retired: flip the column default TRUE so new rows land on V2 and
    # backfill every existing row. Idempotent — the UPDATE only touches rows not
    # already TRUE, and re-issuing SET DEFAULT is a harmless no-op.
    if table_exists(cursor, "sessions") and column_exists(cursor, "sessions", "v2_stream"):
        cursor.execute("ALTER TABLE sessions ALTER COLUMN v2_stream SET DEFAULT TRUE")
        cursor.execute("UPDATE sessions SET v2_stream = TRUE WHERE v2_stream = FALSE")
        if cursor.rowcount and cursor.rowcount > 0:
            migrations_applied.append("sessions.v2_stream=TRUE")

    # ------------------------------------------------------------------
    # V2 Phase 4 (v61) — placement replaces cloud.
    #
    # Additive columns + a one-time, idempotent, concurrent-boot-safe placement
    # backfill so no in-flight session is orphaned at cutover (recon hazard 1).
    # Purely additive: the legacy `instance_id`, `cloud_branch`, and
    # `worktree_*` columns are NEVER updated or dropped, so an un-upgraded
    # install sees exactly the DB it saw before plus new columns it ignores.
    # Runs UNCONDITIONALLY every boot (guarded by column_exists + WHERE
    # placement IS NULL for idempotency). Modeled on the v60 collapse backfill:
    # Python-loop UPDATEs (no DISTINCT ON, no md5/sha in SQL) so the SQLite test
    # shim executes it verbatim, and re-running writes zero rows.

    # Step A: additive columns (idempotent; guarded so the ALTER never re-runs).
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "placement"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS placement JSONB")
        migrations_applied.append("sessions.placement")
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "working_set_ref"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS working_set_ref TEXT")
        migrations_applied.append("sessions.working_set_ref")
    if table_exists(cursor, "artifacts") and not column_exists(cursor, "artifacts", "runner_id"):
        cursor.execute("ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS runner_id TEXT")
        migrations_applied.append("artifacts.runner_id")

    # Connector install affinity: which install owns/runs each connector so two
    # installs sharing one Postgres don't both connect the same bot token and
    # double-handle inbound messages. Additive/nullable TEXT, guarded so the
    # ALTER never re-runs. Existing rows stay NULL — claim-on-start (see
    # ConnectorManager.start_all) heals them by atomically claiming NULL owners.
    if table_exists(cursor, "connectors") and not column_exists(cursor, "connectors", "runner_id"):
        cursor.execute("ALTER TABLE connectors ADD COLUMN IF NOT EXISTS runner_id TEXT")
        migrations_applied.append("connectors.runner_id")

    # Step B: placement backfill. Single
    # deterministic arm — idempotent via WHERE placement IS NULL:
    #   * Every session with a non-empty `instance_id` (this install's own
    #     included) is pinned to its owner: {"where": "runner:<instance_id>"}.
    #     The rule depends ONLY on row data, so it is boot-order INDEPENDENT —
    #     whichever install migrates first produces the identical result
    #     (§2.2's determinism requirement; the earlier `instance_id <> me`
    #     self-exclusion violated it because the first migrator left its own
    #     rows unpinned). Pinning is also required for stickiness (OD-6): an
    #     existing session's Claude CLI conversation state already lives on the
    #     owner's local disk, so a fresh claim by another runner would lose
    #     context — "anywhere" is only valid for NEW sessions before their
    #     first claim (Q3 pins on first claim), never for existing ones, so
    #     there is deliberately no "anywhere" arm here.
    #   * Sessions with no/empty `instance_id` stay NULL — a NULL placement IS
    #     the local-default (the resolver runs it on the local runner).
    # Composition with v40: on any DB at schema >=v40 every session row already
    # carries instance_id (orphan NULL rows were adopted locally by the v40
    # backfill earlier in this function), so Step B honors
    # instance_id as-found and stays a pure function of row data — that local
    # adoption is v40's long-established semantic, not nondeterminism Phase 4
    # introduces.
    # `instance_id` / `cloud_branch` / `worktree_*` are NEVER written here.
    if table_exists(cursor, "sessions") and column_exists(cursor, "sessions", "placement"):
        import json as _json_placement

        cursor.execute(
            "SELECT id, instance_id FROM sessions "
            "WHERE placement IS NULL "
            "AND instance_id IS NOT NULL AND instance_id <> ''"
        )
        pinned = 0
        for row in cursor.fetchall():
            placement_json = _json_placement.dumps({"where": "runner:" + row["instance_id"]})
            cursor.execute(
                "UPDATE sessions SET placement = %s WHERE id = %s AND placement IS NULL",
                (placement_json, row["id"]),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                pinned += cursor.rowcount
        if pinned:
            migrations_applied.append(f"sessions.placement=runner({pinned})")

    # Persisted SDK context-usage snapshot (v62). Additive/nullable JSONB,
    # guarded so the ALTER never re-runs. Written at turn end so the
    # context-usage endpoint can serve the last breakdown after the SDK process
    # is evicted from the local pool.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "context_usage_json"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS context_usage_json JSONB")
        migrations_applied.append("sessions.context_usage_json")

    # Per-session provider binding (v66). One nullable TEXT column: the
    # model_providers id this session is bound to, stamped at creation from the
    # profile's configured provider. NULL = no provider binding. Additive/nullable, no index — same
    # discipline as cloud_branch / context_usage_json above; guarded so the
    # ALTER never re-runs.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "provider_id"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN provider_id TEXT")
        migrations_applied.append("sessions.provider_id")

    # Session interaction mode (v74). One nullable TEXT
    # column: NULL is the default code mode (so every existing row keeps
    # working untouched); 'chat' and 'voice' are stored explicitly.
    # Additive/nullable with NO index, exactly like provider_id above, so it is
    # immune to the schema-pass-before-column-migrations ordering trap; guarded
    # so the ALTER never re-runs.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "mode"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN mode TEXT")
        migrations_applied.append("sessions.mode")

    # Session list-visibility flag (v75). One nullable BOOLEAN column: TRUE
    # keeps the row out of the sessions-list queries (wake-word "command"
    # voice sessions are born hidden), NULL/FALSE — every pre-existing row —
    # stays visible. Hidden rows are still fetchable by id; this is
    # list-visibility only. Additive/nullable with NO index, exactly like
    # mode above; guarded so the ALTER never re-runs.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "hidden"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN hidden BOOLEAN")
        migrations_applied.append("sessions.hidden")

    # Per-session run binding (v79): the model / effort / permission mode the
    # user picked for THIS session. Agents no longer pin a model, so these are
    # the authoritative selection every turn resolves from — a query-level
    # override still wins for that one turn and is written back here, so the
    # composer's mid-session switch sticks server-side (and therefore on every
    # device, and for server-initiated turns like wakeups and automations).
    # NULL on pre-existing rows: resolution falls back to launched_model, then
    # the agent's legacy config, so nothing reroutes on upgrade.
    for _col in ("model", "effort", "permission_mode"):
        if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", _col):
            cursor.execute(f"ALTER TABLE sessions ADD COLUMN {_col} TEXT")
            migrations_applied.append(f"sessions.{_col}")

    # User-selected input-window binding (v80). NULL means use the selected
    # model/provider's registered default, preserving every pre-v80 session.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "context_window"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN context_window INTEGER")
        migrations_applied.append("sessions.context_window")

    # Per-session fast-mode binding (v81). One nullable BOOLEAN, exactly like
    # ``hidden`` above: TRUE asks the leg for its expedited service, NULL/FALSE
    # runs standard. Pre-existing rows are NULL and therefore unchanged.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "fast"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN fast BOOLEAN")
        migrations_applied.append("sessions.fast")

    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "workspace_project_ids"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN workspace_project_ids JSONB")
        migrations_applied.append("sessions.workspace_project_ids")

    # Persisted PR lifecycle for the sidepanel (v85). NULL on every pre-existing
    # row = "no PR known"; the sidepanel then falls back to the open-PR poll.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "pr_number"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN pr_number INTEGER")
        migrations_applied.append("sessions.pr_number")
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "pr_url"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN pr_url TEXT")
        migrations_applied.append("sessions.pr_url")
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "pr_state"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN pr_state TEXT")
        migrations_applied.append("sessions.pr_state")

    # Working time for the sidepanel. ``work_ms`` banks every finished turn's
    # live interval (app/db/turns.py), ``work_started_at`` is the live turn's
    # start. Both ride the session row so peer mirrors show them too. No
    # SCHEMA_VERSION bump: the migrations fingerprint re-runs this pass.
    if table_exists(cursor, "sessions"):
        if not column_exists(cursor, "sessions", "work_ms"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN work_ms BIGINT DEFAULT 0")
            migrations_applied.append("sessions.work_ms")
        if not column_exists(cursor, "sessions", "work_started_at"):
            cursor.execute(
                "ALTER TABLE sessions ADD COLUMN work_started_at TIMESTAMP WITH TIME ZONE"
            )
            migrations_applied.append("sessions.work_started_at")
        backfilled = _backfill_sessions_work_ms(cursor)
        if backfilled is not None:
            migrations_applied.append(f"sessions.work_ms_backfill({backfilled} rows)")

    # Per-session "# Workspace facts" snapshot (#1237). The card used to be
    # re-probed on every connect, so a session that ran `npm ci` and was then
    # idle-evicted came back with a different system prompt and a cold
    # provider cache. Additive/nullable JSONB, no index, no SCHEMA_VERSION
    # bump needed (the migrations fingerprint covers this block).
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "workspace_facts"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN workspace_facts JSONB")
        migrations_applied.append("sessions.workspace_facts")

    # Per-session Away mode (app/core/away_mode.py). One nullable BOOLEAN like
    # ``fast``: NULL/FALSE on every pre-existing row means the owner is present.
    # Additive, no index, no SCHEMA_VERSION bump (the migrations fingerprint
    # covers this block).
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "away_mode"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN away_mode BOOLEAN")
        migrations_applied.append("sessions.away_mode")

    # API-user run configuration. ``model`` / ``effort`` are the preconfigured
    # selection every connector/API session created FOR this user binds at
    # create_session (and re-binds on each connector turn, since ``connector``
    # is a _BINDING_SOURCES member). NULL keeps the pre-existing fallback
    # chain (profile legacy config, then the global default). Additive/nullable
    # with no index; guarded so the ALTER never re-runs.
    for _col in ("model", "effort"):
        if table_exists(cursor, "api_users") and not column_exists(cursor, "api_users", _col):
            cursor.execute(f"ALTER TABLE api_users ADD COLUMN {_col} TEXT")
            migrations_applied.append(f"api_users.{_col}")

    # Agent colour for the Agents page (#1827): one of the frontend's accent
    # token names (``app.core.models.AGENT_AVATAR_COLORS``), NULL = the page's
    # own choice. Additive/nullable, no index, no SCHEMA_VERSION bump (the
    # migrations fingerprint covers this block).
    if table_exists(cursor, "agents") and not column_exists(cursor, "agents", "avatar_color"):
        cursor.execute("ALTER TABLE agents ADD COLUMN avatar_color TEXT")
        migrations_applied.append("agents.avatar_color")

    # Rename sessions.is_favorite -> is_active (v63). Favorites were reworked
    # into an "Active group": the flag now marks the session the user is
    # currently working in. Guarded so the rename runs exactly once (only when
    # the legacy column exists and the new one does not). The index is renamed
    # via guarded DROP+CREATE rather than ALTER INDEX: SQLite (the test pg_shim)
    # has no ALTER INDEX ... RENAME, and DROP/CREATE IF [NOT] EXISTS is
    # equivalent and idempotent on both backends.
    # git_ops (v76) — the durable cross-install git-operation queue. Each
    # install's claim loop polls for rows targeted at itself and flips
    # queued -> running with a single conditional UPDATE. Idempotent so upgrades
    # from any older version land the table without needing the SCHEMA_VERSION
    # bump to fire the bootstrap path (same defensive pattern as the
    # background_tasks / v2-core blocks above).
    if not table_exists(cursor, "git_ops"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS git_ops (
                id TEXT PRIMARY KEY,
                runner_id TEXT NOT NULL,
                project_v2_id TEXT,
                op TEXT NOT NULL,
                params JSONB NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                result JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("git_ops")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_git_ops_runner_status "
        "ON git_ops(runner_id, status)"
    )

    # upload_blobs (v76) — DB-mediated cross-install upload delivery,
    # modeled on note_attachments (bytes in the shared DB are the source of
    # truth; local disk is a cache). Same idempotent guard as git_ops above.
    if not table_exists(cursor, "upload_blobs"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS upload_blobs (
                id TEXT PRIMARY KEY,
                project_v2_id TEXT,
                session_id TEXT,
                source_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                size BIGINT NOT NULL DEFAULT 0,
                data BYTEA,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                materialized_at TIMESTAMP WITH TIME ZONE
            )
        """)
        migrations_applied.append("upload_blobs")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_upload_blobs_session "
        "ON upload_blobs(session_id)"
    )

    # Tool access (v82). Stamp ``tool_selection_mode`` into
    # every agent config so the runtime rule has no legacy branch, and fold the
    # two retired switches (``ai_tools`` / ``app_config_tools``) into the tool
    # lists. Runs once — an agent that already carries a valid mode is skipped,
    # which is also what makes this idempotent across restarts.
    _migrate_agent_tool_access(cursor, migrations_applied)

    if (
        table_exists(cursor, "sessions")
        and column_exists(cursor, "sessions", "is_favorite")
        and not column_exists(cursor, "sessions", "is_active")
    ):
        cursor.execute("ALTER TABLE sessions RENAME COLUMN is_favorite TO is_active")
        cursor.execute("DROP INDEX IF EXISTS idx_sessions_favorite")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_active)")
        migrations_applied.append("sessions.is_favorite->is_active")

    # ------------------------------------------------------------------
    # v84 performance pass (docs/PERFORMANCE_REVIEW_2026-08.md B3/B4/B7/B11).
    # All three are additive and idempotent, so an upgrade from any older
    # version lands them without needing the bootstrap path to fire (same
    # defensive pattern as every block above).
    # ------------------------------------------------------------------

    # B3: per-session index on the usage ledger. Session open
    # (get_all_session_usage) and session delete (the session_id -> NULL
    # unlink) both filter on session_id and had only idx_usage_log_created.
    if table_exists(cursor, "usage_log"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_log_session "
            "ON usage_log(session_id)"
        )

    # B11: boot's _adopt_orphaned_instance_data scans DISTINCT instance_id over
    # projects (sessions already has idx_sessions_instance).
    if table_exists(cursor, "projects") and column_exists(cursor, "projects", "instance_id"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_instance "
            "ON projects(instance_id)"
        )

    # B4: fold watermark for the coalesced session-store summary fold.
    if table_exists(cursor, "session_store_summaries") and not column_exists(
        cursor, "session_store_summaries", "folded_through_id"
    ):
        cursor.execute(
            "ALTER TABLE session_store_summaries "
            "ADD COLUMN folded_through_id BIGINT NOT NULL DEFAULT 0"
        )
        migrations_applied.append("session_store_summaries.folded_through_id")

    # B7: materialize the delegated-worker flag the sessions-list queries used
    # to derive per row with a correlated NOT EXISTS over ``turns``.
    if table_exists(cursor, "sessions"):
        if not column_exists(cursor, "sessions", "is_worker"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN is_worker BOOLEAN")
            migrations_applied.append("sessions.is_worker")
        backfilled = _backfill_sessions_is_worker(cursor)
        if backfilled is not None:
            migrations_applied.append(f"sessions.is_worker_backfill({backfilled} rows)")

    # Scrub media endpoints out of every provider's curated chat roster.
    #
    # Filtering the fetch route alone would not help an existing install: the
    # provider panel deliberately re-surfaces stored ids that the catalog no
    # longer offers (so a save cannot silently drop them), so a hidden model
    # would come back as the FIRST row, checked — the opposite of the intent.
    # And model_registry keeps serving models_json to the chat model picker
    # regardless. So the data itself has to be corrected, once.
    if table_exists(cursor, "model_providers"):
        scrubbed = _backfill_provider_media_models(cursor)
        if scrubbed:
            migrations_applied.append(
                f"model_providers.media_scrub({scrubbed[0]} ids, {scrubbed[1]} rows)"
            )

    _migrate_provider_revisions(cursor)

    # Worker network (v87): private-database peers replace the shared-database
    # cluster. Additive, nullable columns naming the owning device on the
    # tables whose rows execute somewhere, plus the local network tables.
    # The startup backfill (app.network.startup.backfill_owner_device_ids) stamps this worker's device
    # id onto rows that still only carry instance_id.
    from app.db.network import create_schema as _create_network_schema
    _create_network_schema(cursor)
    for _table in ("sessions", "automations", "connectors", "artifacts"):
        if table_exists(cursor, _table) and not column_exists(cursor, _table, "owner_device_id"):
            cursor.execute(f"ALTER TABLE {_table} ADD COLUMN owner_device_id TEXT")
            migrations_applied.append(f"{_table}.owner_device_id")
    if table_exists(cursor, "sessions") and column_exists(cursor, "sessions", "owner_device_id"):
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner_device ON sessions(owner_device_id)")
    if table_exists(cursor, "note_attachments") and not column_exists(cursor, "note_attachments", "blob_hash"):
        cursor.execute("ALTER TABLE note_attachments ADD COLUMN blob_hash TEXT")
        migrations_applied.append("note_attachments.blob_hash")
    # notes.api_user_id: the API key that wrote the note. NULL (every existing
    # row) is the owner's; an API key reads and changes only its own notes.
    # Local-only: the network registry does not sync it, so a mirrored note is
    # the owner's on every peer.
    if table_exists(cursor, "notes") and not column_exists(cursor, "notes", "api_user_id"):
        cursor.execute("ALTER TABLE notes ADD COLUMN api_user_id TEXT")
        migrations_applied.append("notes.api_user_id")

    migrations_applied.extend(_retire_claude_accounts(cursor))
    migrations_applied.extend(_retire_message_hooks(cursor))
    migrations_applied.extend(_retire_webhooks_and_legacy_push(cursor))
    migrations_applied.extend(_retire_fal_billing_key(cursor))

    if migrations_applied:
        logger.info(f"Applied column migrations: {', '.join(migrations_applied)}")


# ---------------------------------------------------------------------------
# v84 one-time data migrations + the migration gate
# ---------------------------------------------------------------------------

#: system_settings key marking that the one-time ``sessions.is_worker`` backfill
#: has run against THIS database (not this install — the database can be shared).
IS_WORKER_BACKFILL_MARKER = "migration:sessions_is_worker_backfilled"

#: system_settings key holding the fingerprint of the migration code that has
#: successfully run against this database (see :func:`migrations_fingerprint`).
MIGRATIONS_FINGERPRINT_KEY = "schema_migrations_fingerprint"


def _get_marker(cursor, key: str) -> Optional[str]:
    """Read a system_settings marker, tolerating a database old enough not to
    have the table yet (returns None)."""
    if not table_exists(cursor, "system_settings"):
        return None
    cursor.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
    row = cursor.fetchone()
    if not row:
        return None
    return row["value"] if isinstance(row, dict) else row[0]


def _set_marker(cursor, key: str, value: str) -> None:
    """Upsert a system_settings marker (no-op when the table does not exist).

    Every settings row carries an opaque ``revision`` (see
    ``_migrate_provider_revisions``). A marker gets one when it is written and
    keeps it while its value is unchanged, so re-running the migration pass
    leaves the row as it was instead of a NULL the next pass backfills.
    """
    from uuid import uuid4

    if not table_exists(cursor, "system_settings"):
        return
    now = datetime.now(timezone.utc).isoformat()
    if not column_exists(cursor, "system_settings", "revision"):
        # Older database: _migrate_provider_revisions adds and backfills the
        # column later in this same pass.
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (key, value, now),
        )
        return
    cursor.execute(
        """INSERT INTO system_settings (key, value, updated_at, revision)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = excluded.updated_at,
               revision = CASE WHEN system_settings.value = excluded.value
                               THEN COALESCE(system_settings.revision, excluded.revision)
                               ELSE excluded.revision END""",
        (key, value, now, str(uuid4())),
    )


#: The retired in-app Claude sign-in's network collection and pointer prefix.
RETIRED_CLAUDE_COLLECTION = "claude_accounts"
RETIRED_CLAUDE_POINTER_PREFIX = "claude_active_account:"
#: Per-worker one-time notice read by ``app.core.claude_relogin_notice``.
CLAUDE_RELOGIN_NOTICE_PREFIX = "claude_cli_relogin_notice:"
CLAUDE_RELOGIN_PENDING = "pending"
#: Database-wide flag: the old sign-in's storage was found and removed here.
#: A worker whose pointer was not in this database (a second install sharing
#: it) records its own pending notice from this flag at startup.
CLAUDE_RELOGIN_NEEDED_MARKER = "migration:claude_cli_relogin_needed"


def _retire_claude_accounts(cursor) -> List[str]:
    """v89: remove every trace of the in-app Claude sign-in. Idempotent.

    AI Shuffle no longer stores Claude credentials; the Claude Code CLI owns
    its own login. Before deleting anything, a database the old sign-in used
    (account rows or active-account pointers) records the one-time
    ``claude auth logout`` / ``claude auth login`` notice for
    every worker that had a pointer and for this one, plus a database-wide
    flag other workers sharing it read at startup. An existing notice
    (pending or dismissed) is never overwritten.

    Then: drop ``claude_accounts``; delete the pointers; purge the
    collection's sealed envelopes and records from the sync tables (a peer
    behind the purged ops gets a snapshot copy, as after ``prune_ops``); drop
    the ``usage_snapshots.account_*`` columns. Returns what was applied.
    """
    applied: List[str] = []
    has_settings = table_exists(cursor, "system_settings")
    pointer_ids: List[str] = []
    if has_settings:
        cursor.execute(
            "SELECT key FROM system_settings WHERE key LIKE %s",
            (RETIRED_CLAUDE_POINTER_PREFIX + "%",),
        )
        for row in cursor.fetchall():
            key = row["key"] if isinstance(row, dict) else row[0]
            suffix = key[len(RETIRED_CLAUDE_POINTER_PREFIX):]
            if suffix:
                pointer_ids.append(suffix)
    has_table = table_exists(cursor, RETIRED_CLAUDE_COLLECTION)
    rows = 0
    if has_table:
        cursor.execute(f"SELECT COUNT(*) AS n FROM {RETIRED_CLAUDE_COLLECTION}")
        row = cursor.fetchone()
        rows = int((row["n"] if isinstance(row, dict) else row[0]) or 0)

    if has_settings and (rows or pointer_ids):
        now = datetime.now(timezone.utc).isoformat()
        for instance_id in dict.fromkeys([*pointer_ids, settings.effective_instance_id]):
            cursor.execute(
                """INSERT INTO system_settings (key, value, updated_at) VALUES (%s, %s, %s)
                   ON CONFLICT(key) DO NOTHING""",
                (CLAUDE_RELOGIN_NOTICE_PREFIX + instance_id, CLAUDE_RELOGIN_PENDING, now),
            )
        _set_marker(cursor, CLAUDE_RELOGIN_NEEDED_MARKER, "1")
        applied.append("claude_cli_relogin_notice")

    if pointer_ids:
        cursor.execute(
            "DELETE FROM system_settings WHERE key LIKE %s",
            (RETIRED_CLAUDE_POINTER_PREFIX + "%",),
        )
        applied.append(f"claude_active_account({len(pointer_ids)} pointers)")
    if has_table:
        cursor.execute(f"DROP TABLE IF EXISTS {RETIRED_CLAUDE_COLLECTION}")
        applied.append(f"drop claude_accounts({rows} rows)")

    for table in ("sync_ops", "sync_records", "sync_conflicts"):
        if table_exists(cursor, table):
            cursor.execute(f"DELETE FROM {table} WHERE collection = %s", (RETIRED_CLAUDE_COLLECTION,))
            if cursor.rowcount:
                applied.append(f"{table}.claude_accounts({cursor.rowcount})")
    if table_exists(cursor, "sync_pending"):
        # Held out-of-order envelopes carry the collection only inside the JSON.
        cursor.execute("SELECT origin_device_id, incarnation, seq, envelope FROM sync_pending")
        stale = []
        for row in cursor.fetchall():
            try:
                envelope = _safe_json(row["envelope"])
            except ValueError:
                continue
            if isinstance(envelope, dict) and envelope.get("collection") == RETIRED_CLAUDE_COLLECTION:
                stale.append((row["origin_device_id"], row["incarnation"], row["seq"]))
        for key in stale:
            cursor.execute(
                "DELETE FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s AND seq = %s",
                key,
            )
        if stale:
            applied.append(f"sync_pending.claude_accounts({len(stale)})")

    if table_exists(cursor, "usage_snapshots"):
        for column in ("account_id", "account_email", "account_name"):
            if column_exists(cursor, "usage_snapshots", column):
                cursor.execute(f"ALTER TABLE usage_snapshots DROP COLUMN {column}")
                applied.append(f"usage_snapshots.drop_{column}")
    return applied


#: The retired hook type: it never had a delivery path into a session.
RETIRED_HOOK_TYPE = "message"


def _retire_message_hooks(cursor) -> List[str]:
    """v90: remove message hooks and their column. Idempotent.

    A ``message`` hook was meant to post a templated message into the chat,
    but nothing ever delivered it; the type is gone from the API, the MCP
    tools and the forms. Existing rows would only show as an unknown type and
    fail every run, so they are deleted, with their ``project_hooks`` sync
    envelopes (the collection stays; only these records leave it, so a peer
    that has not upgraded keeps its own copy until its own migration runs).
    Then ``project_hooks.message_template`` is dropped. Returns what was
    applied.
    """
    applied: List[str] = []
    if not table_exists(cursor, "project_hooks"):
        return applied
    cursor.execute("SELECT id FROM project_hooks WHERE hook_type = %s", (RETIRED_HOOK_TYPE,))
    hook_ids = [row["id"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()]
    if hook_ids:
        cursor.execute("DELETE FROM project_hooks WHERE hook_type = %s", (RETIRED_HOOK_TYPE,))
        applied.append(f"project_hooks.message({len(hook_ids)} rows)")
        marks = ", ".join(["%s"] * len(hook_ids))
        for table in ("sync_ops", "sync_records", "sync_conflicts"):
            if table_exists(cursor, table):
                cursor.execute(
                    f"DELETE FROM {table} WHERE collection = %s AND record_id IN ({marks})",
                    ("project_hooks", *hook_ids),
                )
                if cursor.rowcount:
                    applied.append(f"{table}.message_hooks({cursor.rowcount})")
        if table_exists(cursor, "sync_pending"):
            # Held out-of-order envelopes carry collection and record only
            # inside the JSON.
            cursor.execute("SELECT origin_device_id, incarnation, seq, envelope FROM sync_pending")
            stale = []
            wanted = set(hook_ids)
            for row in cursor.fetchall():
                try:
                    envelope = _safe_json(row["envelope"])
                except ValueError:
                    continue
                if (isinstance(envelope, dict) and envelope.get("collection") == "project_hooks"
                        and envelope.get("record_id") in wanted):
                    stale.append((row["origin_device_id"], row["incarnation"], row["seq"]))
            for key in stale:
                cursor.execute(
                    "DELETE FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s AND seq = %s",
                    key,
                )
            if stale:
                applied.append(f"sync_pending.message_hooks({len(stale)})")
    if column_exists(cursor, "project_hooks", "message_template"):
        cursor.execute("ALTER TABLE project_hooks DROP COLUMN message_template")
        applied.append("project_hooks.drop_message_template")
    return applied


#: The retired standalone outbound-webhook table, also its network collection.
RETIRED_WEBHOOKS_TABLE = "webhooks"
#: Tables an older schema created for the removed rate-limit feature.
RETIRED_RATE_LIMIT_TABLES = ("rate_limits", "request_log")


def _purge_collection_envelopes(cursor, collection: str) -> List[str]:
    """Delete every sync envelope of ``collection`` (a retired collection)."""
    applied: List[str] = []
    for table in ("sync_ops", "sync_records", "sync_conflicts"):
        if table_exists(cursor, table):
            cursor.execute(f"DELETE FROM {table} WHERE collection = %s", (collection,))
            if cursor.rowcount:
                applied.append(f"{table}.{collection}({cursor.rowcount})")
    if table_exists(cursor, "sync_pending"):
        # Held out-of-order envelopes carry the collection only inside the JSON.
        cursor.execute("SELECT origin_device_id, incarnation, seq, envelope FROM sync_pending")
        stale = []
        for row in cursor.fetchall():
            try:
                envelope = _safe_json(row["envelope"])
            except ValueError:
                continue
            if isinstance(envelope, dict) and envelope.get("collection") == collection:
                stale.append((row["origin_device_id"], row["incarnation"], row["seq"]))
        for key in stale:
            cursor.execute(
                "DELETE FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s AND seq = %s",
                key,
            )
        if stale:
            applied.append(f"sync_pending.{collection}({len(stale)})")
    return applied


def _retire_webhooks_and_legacy_push(cursor) -> List[str]:
    """v90: drop storage that no route reaches any more. Idempotent.

    * ``webhooks``, the standalone outbound webhooks: their management API
      had no client, so nobody could create or see one, and the connector
      manager no longer fires them. The table goes with its sync envelopes
      (the ``webhooks`` collection is no longer registered). Connector event
      subscriptions and ``webhook`` hooks are separate and stay.
    * ``mobile_devices.unifiedpush_endpoint``: the Android app dropped its
      UnifiedPush receiver and the registration routes are gone. A row that
      carried an endpoint was already treated as unable to receive push
      (``device_receives_push``); its FCM token is cleared before the marker
      column is dropped, so that stays true.
    * ``rate_limits`` / ``request_log``: created by older releases for the
      removed rate-limit feature and unused since.

    Returns what was applied.
    """
    applied: List[str] = []
    if (table_exists(cursor, "mobile_devices")
            and column_exists(cursor, "mobile_devices", "unifiedpush_endpoint")):
        cursor.execute(
            "UPDATE mobile_devices SET fcm_token = NULL "
            "WHERE unifiedpush_endpoint IS NOT NULL AND fcm_token IS NOT NULL"
        )
        if cursor.rowcount:
            applied.append(f"mobile_devices.fcm_token_cleared({cursor.rowcount})")
        cursor.execute("ALTER TABLE mobile_devices DROP COLUMN unifiedpush_endpoint")
        applied.append("mobile_devices.drop_unifiedpush_endpoint")
    if table_exists(cursor, RETIRED_WEBHOOKS_TABLE):
        cursor.execute(f"SELECT COUNT(*) AS n FROM {RETIRED_WEBHOOKS_TABLE}")
        row = cursor.fetchone()
        rows = int((row["n"] if isinstance(row, dict) else row[0]) or 0)
        cursor.execute(f"DROP TABLE IF EXISTS {RETIRED_WEBHOOKS_TABLE}")
        applied.append(f"drop webhooks({rows} rows)")
    applied.extend(_purge_collection_envelopes(cursor, RETIRED_WEBHOOKS_TABLE))
    for table in RETIRED_RATE_LIMIT_TABLES:
        if table_exists(cursor, table):
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
            applied.append(f"drop {table}")
    return applied


def _retire_fal_billing_key(cursor) -> List[str]:
    """Drop the separate fal.ai Admin billing key. Idempotent.

    The fal.ai balance is read with the ordinary fal.ai API key again, so the
    ``fal_billing_api_key`` setting (stored on this worker only, never synced)
    has no reader or route. The cached ``fal-ai`` balance it produced goes with
    it; the next usage refresh reads the balance with the ordinary key.
    No ``SCHEMA_VERSION`` bump: editing this file changes
    ``migrations_fingerprint()``, which runs the column pass once.
    """
    if not table_exists(cursor, "system_settings"):
        return []
    cursor.execute("DELETE FROM system_settings WHERE key = %s", ("fal_billing_api_key",))
    if not cursor.rowcount:
        return []
    if table_exists(cursor, "provider_usage_snapshots"):
        cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", ("fal-ai",))
    return ["system_settings.fal_billing_api_key"]


def _backfill_sessions_is_worker(cursor) -> Optional[int]:
    """One-time backfill of ``sessions.is_worker`` from ``turns.source`` (v84).

    Delegated ProviderAgent workers have been created ``hidden=True`` since
    v75, so this only has to catch the rows created BEFORE that — exactly the
    population the correlated ``NOT EXISTS (SELECT 1 FROM turns ...)`` guard in
    the list queries existed for. Running it once lets that per-row subquery be
    replaced by a plain column predicate.

    Gated on a ``system_settings`` marker rather than on "the column was just
    added" so it runs exactly once per DATABASE and is correct for both paths
    that can create the column (the fresh-install schema pass and the upgrade
    ALTER above) — and so a re-run after a partially applied migration still
    completes it. Returns the number of rows updated, or ``None`` when the
    backfill was skipped because it had already run.
    """
    if _get_marker(cursor, IS_WORKER_BACKFILL_MARKER):
        return None
    if not column_exists(cursor, "sessions", "is_worker"):
        return None
    if table_exists(cursor, "turns"):
        cursor.execute(
            "UPDATE sessions SET is_worker = TRUE "
            "WHERE COALESCE(is_worker, FALSE) = FALSE "
            "AND EXISTS (SELECT 1 FROM turns t "
            "            WHERE t.session_id = sessions.id "
            "            AND t.source = 'provider_agent')"
        )
        updated = cursor.rowcount or 0
    else:
        updated = 0
    _set_marker(cursor, IS_WORKER_BACKFILL_MARKER, "1")
    return updated


#: system_settings key marking that the one-time ``sessions.work_ms`` backfill
#: has run against this database.
WORK_MS_BACKFILL_MARKER = "migration:sessions_work_ms_backfilled"


def _backfill_sessions_work_ms(cursor) -> Optional[int]:
    """One-time backfill of ``sessions.work_ms`` from the ``turns`` history.

    Sums each session's turn intervals (``started_at`` -> ``ended_at``),
    merging any that overlap so an ad-hoc run beside a scheduled turn is not
    counted twice. A parked turn settled later (``stop_reason='parked_settled'``)
    had its ``ended_at`` rewritten to the settlement time, which would count
    the whole background wait; its last recorded event is used as the end
    instead, and the turn is skipped when it recorded none. Only rows still at
    0 are written, so a session that already banked a turn under the new code
    keeps its value. Marker-gated like ``_backfill_sessions_is_worker``.
    Returns the number of rows updated, or ``None`` when the backfill had
    already run.
    """
    from app.db.turns import _to_utc, interval_ms

    if _get_marker(cursor, WORK_MS_BACKFILL_MARKER):
        return None
    if not column_exists(cursor, "sessions", "work_ms"):
        return None
    updated = 0
    if table_exists(cursor, "turns"):
        last_event: dict = {}
        if table_exists(cursor, "session_events"):
            cursor.execute(
                "SELECT t.id AS turn_id, MAX(e.created_at) AS last_at "
                "FROM turns t JOIN session_events e "
                "  ON e.session_id = t.session_id AND e.turn_id = t.id "
                "WHERE t.stop_reason = 'parked_settled' "
                "GROUP BY t.id"
            )
            last_event = {r["turn_id"]: r["last_at"] for r in cursor.fetchall()}
        # A turn reaped after its runner died (a worker down for an hour reaps
        # at boot) was stamped ended_at at the reap; its run's lease expiry
        # bounds when it last worked.
        if table_exists(cursor, "runs"):
            cursor.execute(
                "SELECT t.id, t.session_id, t.started_at, t.ended_at, t.stop_reason, "
                "r.lease_expires_at FROM turns t LEFT JOIN runs r ON r.id = t.run_id "
                "WHERE t.started_at IS NOT NULL AND t.ended_at IS NOT NULL"
            )
        else:
            cursor.execute(
                "SELECT id, session_id, started_at, ended_at, stop_reason, "
                "NULL AS lease_expires_at FROM turns "
                "WHERE started_at IS NOT NULL AND ended_at IS NOT NULL"
            )
        spans: dict = {}
        for row in cursor.fetchall():
            start, end = _to_utc(row["started_at"]), _to_utc(row["ended_at"])
            lease_end = _to_utc(row["lease_expires_at"])
            if end is not None and lease_end is not None and lease_end < end:
                end = lease_end
            if row["stop_reason"] == "parked_settled":
                event_end = _to_utc(last_event.get(row["id"]))
                end = min(end, event_end) if (end and event_end) else None
            if start is None or end is None or end <= start:
                continue
            spans.setdefault(row["session_id"], []).append((start, end))
        for session_id, intervals in spans.items():
            total = 0
            cur_start, cur_end = None, None
            for start, end in sorted(intervals):
                if cur_end is None or start > cur_end:
                    if cur_end is not None:
                        total += interval_ms(cur_start, cur_end)
                    cur_start, cur_end = start, end
                elif end > cur_end:
                    cur_end = end
            if cur_end is not None:
                total += interval_ms(cur_start, cur_end)
            if total <= 0:
                continue
            cursor.execute(
                "UPDATE sessions SET work_ms = %s "
                "WHERE id = %s AND COALESCE(work_ms, 0) = 0",
                (total, session_id),
            )
            updated += cursor.rowcount or 0
    _set_marker(cursor, WORK_MS_BACKFILL_MARKER, "1")
    return updated


def _backfill_provider_media_models(cursor) -> Optional[Tuple[int, int]]:
    """Drop media endpoints from every provider's curated model roster.

    Returns ``(ids_removed, rows_touched)``, or ``None`` when nothing changed.
    Idempotent: a second run finds nothing to remove and writes nothing.

    Also clears ``default_model`` / ``small_fast_model`` when they point at a
    removed id. Leaving them dangling would be worse than the original problem —
    ``provider_agent.default_model_for_provider`` returns ``None`` once the
    default is no longer among the curated ids, so the row would silently
    resolve to a different model than it claims.

    Best-effort: startup must never fail on a cosmetic scrub.
    """
    from app.core import model_modality

    try:
        with _savepoint(cursor, "sp_media_scrub_read"):
            cursor.execute(
                "SELECT id, models_json, default_model, small_fast_model FROM model_providers"
            )
            rows = cursor.fetchall()
    except Exception:  # noqa: BLE001 — table shape varies across old installs
        logger.warning("provider media scrub skipped", exc_info=True)
        return None

    removed_ids = 0
    touched_rows = 0

    for row in rows:
        try:
            # One savepoint per row: without it the first bad row aborts the
            # boot transaction and "one bad row must not stop the rest" becomes
            # "one bad row kills the whole migration pass" on real Postgres.
            with _savepoint(cursor, "sp_media_scrub_row"):
                raw = row["models_json"]
                if isinstance(raw, str):
                    models = json.loads(raw) if raw.strip() else []
                else:
                    models = raw or []
                if not isinstance(models, list) or not models:
                    continue

                text, media = model_modality.split_media_models(models)
                if not media:
                    continue

                media_ids = {
                    (m.get("id") if isinstance(m, dict) else m) for m in media
                }
                updates = ["models_json = %s"]
                params: list = [json.dumps(text)]

                for column in ("default_model", "small_fast_model"):
                    if row[column] and row[column] in media_ids:
                        updates.append(f"{column} = NULL")

                params.append(row["id"])
                cursor.execute(
                    f"UPDATE model_providers SET {', '.join(updates)} WHERE id = %s",
                    tuple(params),
                )
                removed_ids += len(media)
                touched_rows += 1
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            logger.warning(
                "provider media scrub skipped for one row", exc_info=True
            )

    if not touched_rows:
        return None
    logger.info(
        "Removed %d media model ids from %d provider rows",
        removed_ids, touched_rows,
    )
    return removed_ids, touched_rows


def migrations_fingerprint() -> str:
    """Fingerprint of the migration code that must have run against a database.

    The DDL/migration pass is ~1,900 lines of ``CREATE ... IF NOT EXISTS`` plus
    dozens of ``information_schema`` probes, and it used to run in full on every
    single boot (finding B11) — hundreds of synchronous round trips before the
    server accepts traffic. Skipping it requires proving the database is already
    up to date with THIS build, which ``SCHEMA_VERSION`` alone cannot do: the
    codebase deliberately adds guarded migration blocks WITHOUT bumping the
    version (see the "no SCHEMA_VERSION bump needed" comments throughout
    :func:`_run_column_migrations`).

    Hashing the SOURCE of the two modules that own the schema closes that gap:
    any edit to a migration — version bump or not — changes the fingerprint, so
    the pass runs exactly once more and then records the new value. Unrelated
    edits to these files also trigger one extra (idempotent, cheap) pass, which
    is the safe direction to err in.

    Returns ``""`` when the sources cannot be read (frozen / zipped
    deployment); an empty fingerprint never matches a stored value, so the gate
    degrades to the old always-run behaviour.
    """
    import hashlib

    from app.db import schema_postgres, mobile_hub, network, assistant
    from app.core import owner_auth

    h = hashlib.sha256()
    h.update(f"v{SCHEMA_VERSION}\n".encode())
    # The hub and owner helpers also define tables used during encryption init.
    # Include their sources and the network schema so upgrades cannot skip new DDL.
    for module in (sys.modules[__name__], schema_postgres, mobile_hub, owner_auth, network, assistant):
        path = getattr(module, "__file__", None)
        if not path:
            return ""
        try:
            h.update(Path(path).read_bytes())
        except OSError:
            return ""
    return h.hexdigest()


#: ``pg_advisory_xact_lock`` key for :func:`init_database` (a fixed, namespaced
#: signed bigint: the leading 8 bytes of sha256("shuffle-schema-init")).
SCHEMA_INIT_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"shuffle-schema-init").digest()[:8], "big", signed=True
)

#: ``pg_advisory_xact_lock`` key for :func:`boot_init_lock`. Deliberately not
#: ``SCHEMA_INIT_LOCK_KEY``: the boot lock is held on its own connection while
#: ``init_database`` takes that key on another one, and a process would wait
#: on itself.
BOOT_INIT_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"shuffle-boot-init").digest()[:8], "big", signed=True
)


@contextmanager
def boot_init_lock():
    """Serialize a worker's whole database start-up against other workers.

    ``init_database``'s lock ends with its transaction, but start-up goes on
    to seed: ``run_migrations``, the mobile-tool tables (``CREATE TABLE IF
    NOT EXISTS`` races on PostgreSQL), the built-in agents and templates
    (check, then insert). Two workers booting together on one database raced
    through those after the schema lock was released, and one of them could
    fail to start on a duplicate key. The block under this lock runs one
    worker at a time; the next one starts after the first committed
    everything and finds the rows present.

    A transaction-scoped lock on a dedicated connection, so any exit (an
    exception, or the process dying) releases it.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (BOOT_INIT_LOCK_KEY,))
        # This connection idles in its transaction for the whole start-up; a
        # server-wide idle_in_transaction_session_timeout would end it (and
        # the lock) half way.
        try:
            with _savepoint(cursor, "sp_boot_lock_idle"):
                cursor.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
        except Exception:  # noqa: BLE001 - best effort; the lock is what matters
            pass
        yield


def init_database():
    """Initialize the PostgreSQL database with schema.

    Delegates to schema_postgres.create_postgres_schema() for initial DDL,
    then runs idempotent column migrations.

    Both passes are gated (finding B11). The DDL pass has always been gated on
    ``schema_version``; the column-migration pass — ~1,600 lines of guarded DDL
    and ``information_schema`` probes — used to run in full on EVERY boot,
    synchronously, before the server accepted traffic. It is now skipped when
    the database already carries the fingerprint of THIS build's migration code
    (see :func:`migrations_fingerprint`), which is recorded only after the pass
    completes successfully. Consequences, all deliberate:

    * a partially migrated database never records the fingerprint (the whole
      init runs in one transaction that rolls back on failure), so the next
      boot runs the pass again;
    * an older install stamps an older fingerprint, so upgrading always runs
      the pass once more;
    * editing ANY migration function — with or without a ``SCHEMA_VERSION``
      bump, which this codebase deliberately often skips — changes the
      fingerprint and re-runs the pass exactly once;
    * ``FORCE_DB_MIGRATIONS=1`` in the environment forces the pass (operator
      escape hatch for a hand-edited schema).
    """
    import os

    logger.info("Initializing PostgreSQL database")

    with get_db() as conn:
        cursor = conn.cursor()

        # Serialize schema initialisation across processes. Workers sharing
        # one database can boot (or roll out an upgrade) at the same moment;
        # without this both see the same missing table/column, both issue the
        # DDL, and the loser's whole init fails on a duplicate relation or
        # column once the winner commits. Transaction-scoped, so it is released
        # by the commit/rollback that ends this block, and everything below
        # (version, fingerprint) is read only after the other boot finished.
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK_KEY,))

        # Create schema version table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)

        # Check current version
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        current_version = row["version"] if row else 0

        if current_version < SCHEMA_VERSION:
            logger.info(f"Migrating database from version {current_version} to {SCHEMA_VERSION}")
            # v71 rename/fold and the v77 omni->fluid table rename MUST precede
            # the idempotent CREATE TABLEs (see the two migration docstrings).
            _migrate_profiles_to_agents(cursor)
            _migrate_omni_tables_to_fluid(cursor)
            from app.db.schema_postgres import create_postgres_schema
            create_postgres_schema(cursor)
            cursor.execute("DELETE FROM schema_version")
            cursor.execute("INSERT INTO schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))
            # The schema pass rewrote the version row; any fingerprint recorded
            # for the OLD schema is meaningless now, so force the column pass.
            current_version = SCHEMA_VERSION
            stored_fingerprint = None
        else:
            stored_fingerprint = _get_marker(cursor, MIGRATIONS_FINGERPRINT_KEY)

        fingerprint = migrations_fingerprint()
        forced = os.environ.get("FORCE_DB_MIGRATIONS", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        up_to_date = (
            not forced
            and bool(fingerprint)
            and stored_fingerprint == fingerprint
            and current_version == SCHEMA_VERSION
        )

        if up_to_date:
            logger.debug(
                "Schema already at version %d with matching migration "
                "fingerprint — skipping the column-migration pass",
                SCHEMA_VERSION,
            )
        else:
            # Column migrations handle every edge case where the schema version
            # is current but a guarded block has not run yet.
            _run_column_migrations(cursor)
            if fingerprint:
                # Recorded LAST, inside the same transaction as the pass itself:
                # a failure anywhere above rolls this back with it, so the
                # fingerprint can only ever describe a COMPLETED migration.
                _set_marker(cursor, MIGRATIONS_FINGERPRINT_KEY, fingerprint)

    logger.info("Database initialized successfully")


#: Days of ``usage_log`` the maintenance pass keeps. The Stats page's longest
#: range is 90 days; owner decision 2026-10-01: keep 400, delete older.
USAGE_LOG_RETENTION_DAYS = 400


def run_database_maintenance(skip_vacuum: bool = False) -> dict:
    """Run periodic database maintenance to prevent progressive slowdown.

    Should be called every few hours. Handles:
    - PostgreSQL: ANALYZE, VACUUM ANALYZE
    - Cleanup of unbounded log tables (older than 7 days)
    - Cleanup of the usage ledger (older than USAGE_LOG_RETENTION_DAYS)

    Args:
        skip_vacuum: If True, skip the VACUUM step. Used when sessions are
            actively streaming to avoid holding an exclusive lock that would
            block all reads and writes for seconds.

    Returns:
        dict with maintenance results.
    """
    from datetime import timedelta
    results = {}
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cutoff_usage = (
        datetime.now(timezone.utc) - timedelta(days=USAGE_LOG_RETENTION_DAYS)
    ).isoformat()
    # Notifications are user-facing history (the bell), not debug logs — keep them
    # longer than the 7-day log cutoff, but still bound growth (errors auto-persist).
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    NOTIFICATIONS_PER_USER_CAP = 500

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Update query planner statistics.
        try:
            with _savepoint(cursor, "sp_maint_analyze"):
                cursor.execute("ANALYZE")
            results["analyze"] = "ok"
        except Exception as e:
            results["analyze"] = f"error: {e}"

        # 2. Cleanup unbounded log tables (older than 7 days).
        # ``usage_log`` is NOT one of them: it is the usage ledger behind the
        # Stats page's 30/90-day ranges and the per-turn cost footers, and
        # pruning it at 7 days made every longer range silently report one
        # week of usage. It has its own, much longer cutoff just below.
        log_tables = {
            "audit_log": "created_at",
            "sync_log": "created_at",
            "connector_event_log": "created_at",
        }
        for table, ts_col in log_tables.items():
            try:
                with _savepoint(cursor, "sp_maint_logs"):
                    cursor.execute(f"DELETE FROM {table} WHERE {ts_col} < %s", (cutoff_7d,))
                    cleaned = cursor.rowcount
                results[f"cleaned_{table}"] = cleaned
            except Exception as e:
                results[f"cleaned_{table}"] = f"error: {e}"

        # Usage ledger retention: bounded, but well past the longest Stats
        # range, so a year-over-year look still has data.
        try:
            with _savepoint(cursor, "sp_maint_usage_log"):
                cursor.execute(
                    "DELETE FROM usage_log WHERE created_at < %s", (cutoff_usage,)
                )
                cleaned = cursor.rowcount
            results["cleaned_usage_log"] = cleaned
        except Exception as e:
            results["cleaned_usage_log"] = f"error: {e}"

        # 2a. Cross-install carrier tables (D5/D8). Both are transport, not
        # history: a terminal git_op has already been executed and its result
        # read, and an upload blob is materialized (data NULLed) at spawn. The
        # 7-day cutoff bounds growth without touching in-flight work —
        # queued/running ops are never swept, only done/error rows.
        try:
            with _savepoint(cursor, "sp_maint_git_ops"):
                cursor.execute(
                    "DELETE FROM git_ops WHERE status IN ('done', 'error') "
                    "AND updated_at < %s",
                    (cutoff_7d,),
                )
                cleaned = cursor.rowcount
            results["cleaned_git_ops"] = cleaned
        except Exception as e:
            results["cleaned_git_ops"] = f"error: {e}"
        try:
            with _savepoint(cursor, "sp_maint_upload_blobs"):
                cursor.execute("DELETE FROM upload_blobs WHERE created_at < %s", (cutoff_7d,))
                cleaned = cursor.rowcount
            results["cleaned_upload_blobs"] = cleaned
        except Exception as e:
            results["cleaned_upload_blobs"] = f"error: {e}"

        # 2c. fluid event-log retention. Streaming deltas dominate that table by
        # volume and are reconstructible from the completed ToolUse rows and the
        # node tree; everything else in the log is evidence and stays, Usage
        # especially (the accounting backfill has no other source). Bounded to
        # sessions idle past the window, so live replay is untouched.
        try:
            with _savepoint(cursor, "sp_maint_fluid_deltas"):
                from app.core.config import settings as _settings
                from app.db.fluid_events import prune_fluid_delta_events

                _retention_days = int(
                    getattr(_settings, "fluid_event_retention_days", 7) or 0
                )
                if _retention_days > 0:
                    pruned = prune_fluid_delta_events(
                        _retention_days, cursor=cursor
                    )
                else:
                    pruned = "disabled"
            results["cleaned_fluid_delta_events"] = pruned
        except Exception as e:
            results["cleaned_fluid_delta_events"] = f"error: {e}"

        # 2d. session_events delta compaction (finding B6). Streaming deltas are
        # the bulk of that table and are already coalesced by the replay reader;
        # for sessions idle past the window this merges each run's consecutive
        # delta rows into the exact row that coalescer builds, so replay output
        # is byte-identical and nothing renderable is lost. The window is short
        # (DELTA_COMPACTION_IDLE_HOURS) so recently used sessions stop paying a
        # row per streaming flush on every open; active runs and the retained
        # tail are still skipped. Round-robin cursor inside, so a large install
        # is covered across passes rather than in one long transaction. Shares
        # this maintenance transaction's cursor.
        try:
            with _savepoint(cursor, "sp_maint_compact_deltas"):
                from app.db.session_events import (
                    DELTA_COMPACTION_IDLE_HOURS,
                    compact_idle_session_deltas,
                )

                compacted = compact_idle_session_deltas(
                    older_than_hours=DELTA_COMPACTION_IDLE_HOURS, cursor=cursor
                )["rows"]
            results["compacted_session_event_deltas"] = compacted
        except Exception as e:
            results["compacted_session_event_deltas"] = f"error: {e}"

        # 2b. Notifications retention: drop anything older than 30 days, then cap
        # each user to the most recent N rows so the table can't grow unbounded.
        try:
            with _savepoint(cursor, "sp_maint_notif_age"):
                cursor.execute("DELETE FROM notifications WHERE created_at < %s", (cutoff_30d,))
                cleaned = cursor.rowcount
            results["cleaned_notifications_age"] = cleaned
        except Exception as e:
            results["cleaned_notifications_age"] = f"error: {e}"
        try:
            with _savepoint(cursor, "sp_maint_notif_cap"):
                cursor.execute(
                    """
                    DELETE FROM notifications
                    WHERE id IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY user_id ORDER BY created_at DESC
                            ) AS rn
                            FROM notifications
                        ) ranked
                        WHERE rn > %s
                    )
                    """,
                    (NOTIFICATIONS_PER_USER_CAP,),
                )
                cleaned = cursor.rowcount
            results["cleaned_notifications_cap"] = cleaned
        except Exception as e:
            results["cleaned_notifications_cap"] = f"error: {e}"

    # 3. VACUUM — reclaims disk space. Must run outside a transaction.
    # Skipped when sessions are actively streaming to avoid an exclusive lock
    # that would block all reads and writes for potentially several seconds.
    if skip_vacuum:
        results["vacuum"] = "skipped (active streaming)"
        logger.info("VACUUM skipped — active streaming sessions detected")
    else:
        try:
            conn = get_connection()
            try:
                # Postgres VACUUM cannot run inside a transaction block
                conn.autocommit = True
                cursor = conn.cursor()
                # Parallel index vacuum allocates dynamic shared memory; on a
                # container with a small /dev/shm it fails with "could not
                # resize shared memory segment". A serial vacuum needs none.
                cursor.execute("SET max_parallel_maintenance_workers = 0")
                cursor.execute("VACUUM ANALYZE")
                results["vacuum"] = "ok"
            finally:
                # VACUUM uses a session setting; do not leak it to the next borrower.
                try:
                    conn.cursor().execute("RESET max_parallel_maintenance_workers")
                except Exception:
                    # A connection we cannot reset must not return to service.
                    conn.close()
                # Always reset autocommit before returning to pool,
                # even if VACUUM raises — otherwise the pooled connection
                # is left in autocommit mode, breaking transaction semantics
                # for all subsequent callers.
                try:
                    conn.autocommit = False
                except Exception:
                    pass
                from app.db.base import _close_connection
                _close_connection(conn)
        except Exception as e:
            results["vacuum"] = f"error: {e}"
            logger.warning(f"VACUUM failed: {e}")

    logger.info(f"Database maintenance completed: {results}")
    return results


# ============================================================================
# Re-exports from domain modules
# ============================================================================
# Every public function that was previously defined in this file is now
# implemented in its own domain module.  The wildcard imports below make
# ``from app.db.database import <function>`` and
# ``database.<function>()`` continue to work unchanged.

from app.db.auth import (  # noqa: F401
    is_setup_required,
    get_admin,
    create_admin,
    create_audit_log,
    get_audit_logs,
    get_audit_log_count,
    create_auth_session,
    get_auth_session,
    delete_auth_session,
    clear_all_sessions,
    cleanup_expired_sessions,
)

from app.db.profiles import (  # noqa: F401
    get_profile,
    get_all_profiles,
    create_profile,
    update_profile,
    delete_profile,
    get_profile_session_count,
    get_all_profile_session_counts,
    set_profile_builtin,
)

from app.db.projects import (  # noqa: F401
    get_project,
    get_projects_by_ids,
    get_all_projects,
    create_project,
    update_project,
    set_project_link_group,
    get_projects_in_link_group,
    get_project_session_count,
    get_all_project_session_counts,
    delete_project,
)

from app.db.sessions import (  # noqa: F401
    get_session,
    get_session_by_sdk_id,
    get_sessions,
    get_sessions_paginated,
    get_sessions_cursor,
    create_session,
    create_session_with_stats,
    update_session,
    set_session_context_usage,
    set_session_run_binding,
    patch_voice_session_context,
    get_session_context_usage_snapshot,
    get_session_workspace_facts,
    set_session_workspace_facts,
    delete_session,
    set_session_active,
    archive_status_sql,
    set_session_hidden,
    set_session_parked_task_kind,
    adopt_session,
    adopt_session_follow,
    unpin_session,
    set_session_project,
    set_session_branch,
    resync_session_branch,
    update_session_pr_state,
    list_sessions_by_pr_number,
    list_sessions_for_branch,
    list_sessions_for_v2_branch,
    session_has_forks,
    get_session_messages,
    add_session_message,
    add_session_messages_batch,
    delete_session_message,
    delete_orphan_user_turn,
    discard_orphan_first_turn,
    clear_session_conversation,
    delete_session_messages_after_jsonl_uuid,
    set_message_jsonl_uuid,
    search_sessions,
    search_sessions_paginated,
    add_sync_log,
    get_sync_logs,
    get_latest_sync_id,
)

from app.db.analytics import (  # noqa: F401
    log_usage,
    settle_turn_usage,
    get_all_session_usage,
    get_usage_stats,
    get_analytics_usage_stats,
    get_analytics_cost_breakdown,
    get_analytics_usage_trends,
    get_analytics_top_sessions,
    get_analytics_provider_breakdown,
)

from app.db.api_users import (  # noqa: F401
    create_api_user,
    delete_api_user,
    get_api_user,
    get_api_user_by_key_hash,
    update_api_user_last_used,
    get_all_api_users,
    update_api_user,
    get_all_credential_policies,
    get_credential_policy,
    update_credential_policy,
    get_user_credential_policy,
    get_all_user_credential_policies,
    set_user_credential_policy,
    delete_user_credential_policy,
    get_effective_credential_policy,
    get_user_credential,
    get_all_user_credentials,
    set_user_credential,
    delete_user_credential,
)

from app.core.resource_sync import install_database_mutations as _install_resource_sync
from app.db import settings as _settings_db

_install_resource_sync(
    vars(_settings_db), "settings",
    "set_system_setting delete_system_setting get_or_create_system_setting",
)

from app.db.settings import (  # noqa: F401
    get_user_preference,
    set_user_preference,
    delete_user_preference,
    get_all_user_preferences,
    get_system_setting,
    get_system_setting_revision,
    get_system_setting_cached,
    invalidate_system_setting_memo,
    set_system_setting,
    delete_system_setting,
    get_or_create_system_setting,
    get_all_system_settings,
)

from app.db.subagents import (  # noqa: F401
    get_subagent,
    get_all_subagents,
    create_subagent,
    update_subagent,
    delete_subagent,
    set_subagent_builtin,
    revert_subagent_to_defaults,
    builtin_subagent_is_modified,
    add_permission_rule,
    get_permission_rule,
    get_permission_rules,
    delete_permission_rule,
    delete_profile_permission_rules,
)

from app.db.tags import (  # noqa: F401
    get_tag,
    get_all_tags,
    create_tag,
    update_tag,
    delete_tag,
    get_session_tags,
    add_session_tag,
    remove_session_tag,
    set_session_tags,
)

from app.db.templates import (  # noqa: F401
    get_template,
    get_all_templates,
    create_template,
    update_template,
    delete_template,
    get_template_categories,
    set_template_builtin,
)

from app.db.connectors import (  # noqa: F401
    create_connector,
    claim_connector_runner,
    get_connector,
    get_all_connectors,
    get_active_connectors,
    get_connectors_for_event,
    update_connector,
    update_connector_status,
    delete_connector,
    update_connector_event_dispatched,
    create_connector_event_log,
    get_connector_event_log,
    create_connector_session,
    get_connector_session,
    get_connector_sessions_for_ai_session,
    update_connector_session_ai_session,
)


from app.db.model_providers import (  # noqa: F401
    create_model_provider,
    get_model_provider,
    get_all_model_providers,
    get_enabled_model_providers,
    count_configured_model_providers,
    update_model_provider,
    delete_model_provider,
    set_model_provider_enabled,
    set_codex_token_bundle,
    get_codex_tokens,
    try_claim_codex_refresh,
    release_codex_refresh_claim,
    set_openai_billing_binding,
    clear_openai_billing_binding,
)

from app.db.usage_snapshots import (  # noqa: F401
    upsert_usage_snapshot,
    touch_usage_snapshot_observed,
    get_all_usage_snapshots,
    get_usage_snapshot_publishers,
    delete_usage_snapshot,
)

from app.db.provider_usage_snapshots import (  # noqa: F401
    record_provider_usage_failure,
    upsert_provider_usage_snapshot,
    get_all_provider_usage_snapshots,
    get_provider_usage_raw,
    delete_provider_usage_snapshot,
)

from app.db.git import (  # noqa: F401
    get_git_repository,
    get_git_repository_by_project,
    get_all_git_repositories,
    get_remote_urls_for_projects,
    create_git_repository,
    update_git_repository,
    update_git_repository_synced,
    get_worktree,
    get_worktree_by_session,
    get_sessions_for_worktree,
    get_worktrees_with_sessions,
    get_worktrees_for_repository,
    get_active_worktrees_for_repository,
    create_worktree,
    update_worktree,
    delete_worktree,
)

from app.db.notes import (  # noqa: F401
    get_note,
    get_all_notes,
    create_note,
    update_note,
    delete_note,
    get_all_note_tags,
    get_notes_autocomplete,
    get_all_note_filenames,
    get_notes_without_file_path,
    bulk_update_note_file_paths,
    create_note_attachment,
    get_note_attachment,
    get_note_attachments,
    get_note_attachment_data,
    set_note_attachment_data,
    get_attachments_missing_data,
    delete_note_attachment,
)

from app.db.hooks import (  # noqa: F401
    get_hook,
    get_hooks_for_project,
    get_app_hooks,
    get_all_hooks_for_project,
    get_hooks_for_project_and_event,
    get_app_hooks_for_event,
    create_hook,
    update_hook,
    delete_hook,
    update_hook_triggered,
)

from app.db.mobile import (  # noqa: F401
    create_mobile_device,
    get_mobile_device,
    get_mobile_device_by_api_key_hash,
    get_all_mobile_devices,
    update_mobile_device_last_seen,
    delete_mobile_device,
    get_mobile_devices,
)

from app.db.pairing import (  # noqa: F401
    create_pairing_grant,
    list_pairing_grants,
    revoke_pairing_grant,
    exchange_grant_for_client,
    exchange_grant_for_mobile,
    get_paired_client_by_session_hash as _get_paired_client_by_session_hash,
    touch_paired_client,
    list_paired_clients,
    get_paired_client,
    revoke_paired_client,
    rename_paired_client,
    ensure_paired_client_for_mobile,
    set_paired_client_session,
    clear_paired_client_session,
    consume_grant_for_runner,
)

def get_paired_client_by_session_hash(token_hash):
    """All derived-session consumers must check the originating authorization."""
    client = _get_paired_client_by_session_hash(token_hash)
    if client:
        from app.core.account_hub import check_paired_client
        from fastapi import HTTPException
        try:
            # Derived only after live local + hub authorization, never persisted
            # in paired_clients or accepted from a request/scopes payload.
            client['account_owner_admin'] = check_paired_client(client) is True
        except HTTPException as exc:
            if exc.status_code >= 500:
                raise  # Temporary hub outages must not invalidate saved login.
            return None
    return client


from app.db.installs import (  # noqa: F401
    register_install,
    touch_install_heartbeat,
    set_install_name,
    set_install_approval,
    is_install_approved,
    get_install,
    get_installs,
    get_installs_map,
    delete_install,
)

from app.db.automations import (  # noqa: F401
    create_automation,
    get_automation,
    get_all_automations,
    get_automations_for_profile,
    get_enabled_automations,
    get_automations_for_event,
    update_automation,
    delete_automation,
    create_automation_run,
    get_automation_run,
    get_automation_runs,
    update_automation_run,
    get_automation_daily_stats,
    get_automation_aggregate_stats,
    create_automation_approval,
    get_automation_approval,
    get_pending_automation_approvals,
    update_automation_approval,
    claim_automation_fire,
    prune_automation_fires,
    record_automation_run_success,
)
