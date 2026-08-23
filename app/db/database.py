"""
Database setup and operations — PostgreSQL backend.

This module is a **facade**: it re-exports every public function from the
domain-specific modules in ``app.db.*`` so that existing callers like
``from app.db.database import get_session`` continue to work.

Only schema initialisation and maintenance helpers live here directly.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import psycopg2

from app.core.config import settings

# Import shared database utilities from base module.
# Re-exported here so existing ``from app.db.database import get_db`` still works.
from app.db.base import (  # noqa: F401
    get_connection,
    _close_connection,
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
    sql_date,
    sql_strftime,
    sql_ilike,
    sql_current_timestamp,
)

# Database error type for catch blocks
_DB_ERRORS: tuple = (psycopg2.Error,)

logger = logging.getLogger(__name__)


def _safe_json(value):
    """Parse JSON if string, return as-is if already parsed (Postgres JSONB returns dicts/lists)."""
    if isinstance(value, str):
        return json.loads(value)
    return value


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
#     binding (multi-provider phase-2 Chunk E, ruling G2-r). Stamped at session
#     creation from the profile's configured provider; every subsequent
#     turn/spawn resolves the provider from THIS binding, not the live profile
#     (editing a profile never reroutes an existing session). NULL = native
#     Claude (gateway bypass, F1) — existing pre-migration sessions stay native.
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
# v71: agents unification (ruling A4). ``profiles`` is RENAMED in place to
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
#     'chat' (default when NULL — every pre-existing session) or 'voice' (an
#     OpenAI Realtime voice call, ruling VM4 of docs/VOICE_MODE_PLAN.md).
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
SCHEMA_VERSION = 82


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
    ``prov:`` model composites (inert since ruling D4) are dropped — the agent
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
    """v82 (rulings T2 + T5): stamp ``tool_selection_mode`` onto every agent
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


def _run_column_migrations(cursor):
    """Run idempotent column migrations.

    These migrations run on EVERY startup to ensure columns exist,
    regardless of schema version. This handles cases where:
    - Schema version was bumped but migration didn't run
    - Database was restored from backup
    - Docker volume has stale schema version
    """
    migrations_applied = []

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
    try:
        cursor.execute("DROP INDEX IF EXISTS idx_worktrees_active_branch")
        migrations_applied.append("dropped idx_worktrees_active_branch")
    except _DB_ERRORS:
        pass  # Index doesn't exist, that's fine

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
            try:
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
            except _DB_ERRORS:
                pass  # automation_runs table may not exist yet
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
        # Backfill for existing sessions with active worktrees
        if "sessions.worktree_branch" in migrations_applied:
            try:
                cursor.execute("""
                    UPDATE sessions SET
                        worktree_branch = (SELECT w.branch_name FROM worktrees w WHERE w.id = sessions.worktree_id),
                        worktree_path = (SELECT w.worktree_path FROM worktrees w WHERE w.id = sessions.worktree_id)
                    WHERE worktree_id IS NOT NULL AND worktree_branch IS NULL
                """)
                backfilled = cursor.rowcount
                if backfilled > 0:
                    logger.info(f"Backfilled worktree_branch/worktree_path for {backfilled} sessions")
            except _DB_ERRORS:
                pass  # worktrees table may not exist yet

    # Add action_steps column to automations for multi-step connector actions (v36)
    if table_exists(cursor, "automations") and not column_exists(cursor, "automations", "action_steps"):
        cursor.execute("ALTER TABLE automations ADD COLUMN action_steps TEXT DEFAULT NULL")
        migrations_applied.append("automations.action_steps")

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

    # Add message_template column to project_hooks (v38)
    if table_exists(cursor, "project_hooks") and not column_exists(cursor, "project_hooks", "message_template"):
        cursor.execute("ALTER TABLE project_hooks ADD COLUMN message_template TEXT")
        migrations_applied.append("project_hooks.message_template")

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
            try:
                cursor.execute("""
                    UPDATE sessions SET instance_id = (
                        SELECT p.instance_id FROM projects p WHERE p.id = sessions.project_id
                    )
                    WHERE instance_id IS NULL AND project_id IS NOT NULL
                """)
            except _DB_ERRORS:
                pass  # projects table may not exist yet in pathological cases
            migrations_applied.append("sessions.instance_id")
        # Always ensure the index exists (idempotent for fresh installs + upgrades)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_instance ON sessions(instance_id)")
        # Backfill any remaining sessions with NULL instance_id (e.g. ungrouped
        # sessions without a project_id that the original migration missed).
        # Assigns them to the current instance so they stop leaking across installs.
        try:
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

    # Mobile companion app (UnifiedPush): per-device push endpoint URL so a
    # self-hosted server can deliver notifications to the user's own push
    # distributor (e.g. ntfy) without a shared Firebase project. Idempotent
    # column add for databases created before the column existed.
    if table_exists(cursor, "mobile_devices"):
        if not column_exists(cursor, "mobile_devices", "unifiedpush_endpoint"):
            cursor.execute("ALTER TABLE mobile_devices ADD COLUMN unifiedpush_endpoint TEXT")
            migrations_applied.append("mobile_devices.unifiedpush_endpoint")
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
    if not table_exists(cursor, "installs"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS installs (
                id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                base_url TEXT,
                platform TEXT,
                version TEXT,
                capabilities JSONB DEFAULT '{}',
                last_seen TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        migrations_applied.append("installs")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_installs_last_seen ON installs(last_seen DESC)")

    # claude_accounts (v52). Multiple Anthropic OAuth accounts + in-app switching.
    # The account list is shared; the active pointer is per-install and lives in
    # system_settings (key claude_active_account:{instance_id}), so no is_active
    # column here. Idempotent so upgrades from any older version land the table
    # without needing the SCHEMA_VERSION bump to fire the bootstrap path (same
    # defensive pattern as the SessionStore / error_log / installs blocks above).
    if not table_exists(cursor, "claude_accounts"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS claude_accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                subscription_type TEXT,
                credentials TEXT,
                credential_fingerprint TEXT,
                expires_at BIGINT,
                has_refresh_token BOOLEAN DEFAULT TRUE,
                sync_pending BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_used_at TIMESTAMP WITH TIME ZONE
            )
        """)
        migrations_applied.append("claude_accounts")

    # model_providers (v64). Admin-configured non-Anthropic model gateways
    # (OpenRouter / custom Anthropic-compatible endpoints). Idempotent so
    # upgrades from any older version land the table without needing the
    # SCHEMA_VERSION bump to fire the bootstrap path (same defensive pattern as
    # the SessionStore / error_log / installs / claude_accounts blocks above).
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
                surface TEXT NOT NULL DEFAULT 'toast',
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
    # instance_id. The 5-hour Claude usage window is per-account and two installs
    # on a shared DB may run different accounts, so each install persists its own
    # snapshot and the usage endpoint serves them all. Idempotent so upgrades
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
                account_id TEXT,
                account_email TEXT,
                account_name TEXT,
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
                unparked BOOLEAN DEFAULT FALSE,
                terminal_notified BOOLEAN DEFAULT FALSE,
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
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                answered_at TIMESTAMP WITH TIME ZONE
            )
        """)
        migrations_applied.append("pending_interactions")
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

    # Step B: placement backfill (hazard 1; A9 ruling, 07-14). Single
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
    # backfill earlier in this function, ~line 372-385), so Step B honors
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

    # Per-session provider binding (v66, multi-provider phase-2 Chunk E / ruling
    # G2-r). One nullable TEXT column: the model_providers id this session is
    # bound to, stamped at creation from the profile's configured provider. NULL
    # = native Claude (gateway bypass). Additive/nullable, no index — same
    # discipline as cloud_branch / context_usage_json above; guarded so the
    # ALTER never re-runs.
    if table_exists(cursor, "sessions") and not column_exists(cursor, "sessions", "provider_id"):
        cursor.execute("ALTER TABLE sessions ADD COLUMN provider_id TEXT")
        migrations_applied.append("sessions.provider_id")

    # Session interaction mode (v74, voice-mode ruling VM4). One nullable TEXT
    # column: 'chat' (the implicit default — NULL means chat, so every existing
    # row keeps working untouched) or 'voice' (an OpenAI Realtime voice call).
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

    # Rename sessions.is_favorite -> is_active (v63). Favorites were reworked
    # into an "Active group": the flag now marks the session the user is
    # currently working in. Guarded so the rename runs exactly once (only when
    # the legacy column exists and the new one does not). The index is renamed
    # via guarded DROP+CREATE rather than ALTER INDEX: SQLite (the test pg_shim)
    # has no ALTER INDEX ... RENAME, and DROP/CREATE IF [NOT] EXISTS is
    # equivalent and idempotent on both backends.
    # git_ops (v76) — the durable cross-install git-operation queue (D5). Each
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

    # upload_blobs (v76) — DB-mediated cross-install upload delivery (D8),
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

    # Tool access (v82, rulings T2 + T5). Stamp ``tool_selection_mode`` into
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

    if migrations_applied:
        logger.info(f"Applied column migrations: {', '.join(migrations_applied)}")


def init_database():
    """Initialize the PostgreSQL database with schema.

    Delegates to schema_postgres.create_postgres_schema() for initial DDL,
    then runs idempotent column migrations.
    """
    logger.info("Initializing PostgreSQL database")

    with get_db() as conn:
        cursor = conn.cursor()

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

        # Always run column migrations to handle edge cases where
        # schema version is correct but columns are missing
        _run_column_migrations(cursor)

    logger.info("Database initialized successfully")


def run_database_maintenance(skip_vacuum: bool = False) -> dict:
    """Run periodic database maintenance to prevent progressive slowdown.

    Should be called every few hours. Handles:
    - PostgreSQL: ANALYZE, VACUUM ANALYZE
    - Cleanup of unbounded log tables (older than 7 days)

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
    # Notifications are user-facing history (the bell), not debug logs — keep them
    # longer than the 7-day log cutoff, but still bound growth (errors auto-persist).
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    NOTIFICATIONS_PER_USER_CAP = 500

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Update query planner statistics.
        try:
            cursor.execute("ANALYZE")
            results["analyze"] = "ok"
        except Exception as e:
            results["analyze"] = f"error: {e}"

        # 2. Cleanup unbounded log tables (older than 7 days).
        # request_log uses 'timestamp' column; all others use 'created_at'.
        log_tables = {
            "usage_log": "created_at",
            "audit_log": "created_at",
            "request_log": "timestamp",
            "sync_log": "created_at",
            "connector_event_log": "created_at",
        }
        for table, ts_col in log_tables.items():
            try:
                cursor.execute(f"DELETE FROM {table} WHERE {ts_col} < %s", (cutoff_7d,))
                results[f"cleaned_{table}"] = cursor.rowcount
            except Exception as e:
                results[f"cleaned_{table}"] = f"error: {e}"

        # 2a. Cross-install carrier tables (D5/D8). Both are transport, not
        # history: a terminal git_op has already been executed and its result
        # read, and an upload blob is materialized (data NULLed) at spawn. The
        # 7-day cutoff bounds growth without touching in-flight work —
        # queued/running ops are never swept, only done/error rows.
        try:
            cursor.execute(
                "DELETE FROM git_ops WHERE status IN ('done', 'error') "
                "AND updated_at < %s",
                (cutoff_7d,),
            )
            results["cleaned_git_ops"] = cursor.rowcount
        except Exception as e:
            results["cleaned_git_ops"] = f"error: {e}"
        try:
            cursor.execute("DELETE FROM upload_blobs WHERE created_at < %s", (cutoff_7d,))
            results["cleaned_upload_blobs"] = cursor.rowcount
        except Exception as e:
            results["cleaned_upload_blobs"] = f"error: {e}"

        # 2b. Notifications retention: drop anything older than 30 days, then cap
        # each user to the most recent N rows so the table can't grow unbounded.
        try:
            cursor.execute("DELETE FROM notifications WHERE created_at < %s", (cutoff_30d,))
            results["cleaned_notifications_age"] = cursor.rowcount
        except Exception as e:
            results["cleaned_notifications_age"] = f"error: {e}"
        try:
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
            results["cleaned_notifications_cap"] = cursor.rowcount
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
                conn.cursor().execute("VACUUM ANALYZE")
                results["vacuum"] = "ok"
            finally:
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
    update_admin_password,
    create_audit_log,
    get_audit_logs,
    get_audit_log_count,
    create_auth_session,
    get_auth_session,
    delete_auth_session,
    clear_all_sessions,
    cleanup_expired_sessions,
    record_login_attempt,
    get_failed_attempts_count,
    get_failed_attempts_for_username,
    create_lockout,
    is_ip_locked,
    is_username_locked,
    get_api_key_session,
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
    get_session_context_usage_snapshot,
    delete_session,
    set_session_active,
    set_session_hidden,
    set_session_parked_task_kind,
    adopt_session,
    adopt_session_follow,
    unpin_session,
    set_session_project,
    set_session_branch,
    session_has_forks,
    get_session_forks,
    get_session_messages,
    add_session_message,
    add_session_messages_batch,
    upsert_team_member_message,
    delete_session_message,
    delete_orphan_user_turn,
    discard_orphan_first_turn,
    clear_session_conversation,
    delete_session_messages_after,
    delete_session_messages_after_jsonl_uuid,
    set_message_jsonl_uuid,
    search_sessions,
    search_sessions_paginated,
    add_sync_log,
    get_sync_logs,
    get_latest_sync_id,
    update_message_content,
)

from app.db.analytics import (  # noqa: F401
    log_usage,
    get_last_session_usage,
    get_all_session_usage,
    get_usage_stats,
    get_analytics_usage_stats,
    get_analytics_cost_breakdown,
    get_analytics_usage_trends,
    get_analytics_top_sessions,
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
    user_has_credential,
)

from app.db.settings import (  # noqa: F401
    get_user_preference,
    set_user_preference,
    delete_user_preference,
    get_all_user_preferences,
    get_system_setting,
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
    has_subagent_been_modified,
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
    get_sessions_by_tag,
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

from app.db.webhooks import (  # noqa: F401
    get_all_webhooks,
    get_active_webhooks,
    get_webhook,
    create_webhook,
    update_webhook,
    delete_webhook,
    update_webhook_triggered,
    get_webhooks_for_event,
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
    get_connector_session_by_id,
    get_connector_sessions_for_ai_session,
    update_connector_session_ai_session,
)

from app.db.claude_accounts import (  # noqa: F401
    create_claude_account,
    get_claude_account,
    get_all_claude_accounts,
    update_claude_account_credentials,
    update_claude_account_credentials_only,
    rename_claude_account,
    delete_claude_account,
    clear_claude_active_pointers,
    mark_claude_account_sync_pending,
    touch_claude_account_last_used,
    set_claude_account_subscription_type,
    count_claude_accounts,
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
)

from app.db.usage_snapshots import (  # noqa: F401
    upsert_usage_snapshot,
    get_all_usage_snapshots,
    delete_usage_snapshot,
)

from app.db.provider_usage_snapshots import (  # noqa: F401
    upsert_provider_usage_snapshot,
    get_all_provider_usage_snapshots,
    delete_provider_usage_snapshot,
)

from app.db.rate_limits import (  # noqa: F401
    get_rate_limit,
    get_rate_limit_for_user,
    get_all_rate_limits,
    create_rate_limit,
    update_rate_limit,
    delete_rate_limit,
    log_request,
    get_request_count,
    cleanup_old_request_logs,
)

from app.db.git import (  # noqa: F401
    get_git_repository,
    get_git_repository_by_project,
    get_all_git_repositories,
    get_remote_urls_for_projects,
    create_git_repository,
    update_git_repository,
    update_git_repository_synced,
    delete_git_repository,
    get_worktree,
    get_worktree_by_session,
    get_sessions_for_worktree,
    get_worktrees_with_sessions,
    get_worktree_by_branch,
    get_worktrees_for_repository,
    get_active_worktrees_for_repository,
    create_worktree,
    update_worktree,
    delete_worktree,
    delete_worktrees_for_repository,
)

from app.db.notes import (  # noqa: F401
    get_note,
    get_all_notes,
    create_note,
    update_note,
    delete_note,
    get_all_note_tags,
    get_notes_autocomplete,
    count_all_notes,
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
    get_enabled_hooks_for_project,
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
    update_mobile_device_capabilities,
    update_mobile_device_fcm_token,
    update_mobile_device_unifiedpush_endpoint,
    update_mobile_device_last_seen,
    delete_mobile_device,
    create_pending_action,
    get_pending_action,
    get_pending_actions_for_device,
    update_pending_action_status,
    cleanup_expired_actions,
    create_trusted_action,
    get_trusted_action,
    get_trusted_actions_for_device,
    delete_trusted_action,
    get_mobile_schedules,
    get_mobile_schedule,
    save_mobile_schedule,
    delete_mobile_schedule,
    get_mobile_actions,
    save_mobile_action,
    delete_mobile_action,
    get_mobile_devices,
    save_mobile_device,
)

from app.db.pairing import (  # noqa: F401
    create_pairing_grant,
    list_pairing_grants,
    revoke_pairing_grant,
    exchange_grant_for_client,
    exchange_grant_for_mobile,
    get_paired_client_by_session_hash,
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

from app.db.installs import (  # noqa: F401
    register_install,
    touch_install_last_seen,
    touch_install_heartbeat,
    set_install_name,
    set_install_base_url,
    set_install_approval,
    is_install_approved,
    get_install,
    get_installs,
    get_installs_map,
    delete_install,
    ONLINE_WINDOW_SECONDS,
)

from app.db.automations import (  # noqa: F401
    create_automation,
    get_automation,
    get_all_automations,
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

from app.db.background_tasks import (  # noqa: F401
    upsert_background_task,
    mark_stale_background_tasks_killed,
)

# Backward-compatible alias kept for callers that import the private helper.
_parse_subagent_json_fields = None
try:
    from app.db.subagents import _parse_json_fields as _parse_subagent_json_fields  # noqa: F401
except ImportError:
    pass
