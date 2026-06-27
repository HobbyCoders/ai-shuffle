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
SCHEMA_VERSION = 49


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

    # SDK 0.1.49 agent fields: skills, memory, mcp_servers
    subagent_new_cols = {"skills": "JSON", "memory": "TEXT", "mcp_servers": "JSON"}
    if table_exists(cursor, "subagents"):
        for col, col_type in subagent_new_cols.items():
            if not column_exists(cursor, "subagents", col):
                _validate_identifier(col, "column name")
                cursor.execute(f"ALTER TABLE subagents ADD COLUMN {col} {col_type}")
                migrations_applied.append(f"subagents.{col}")

    # Built-in subagent columns (v26)
    subagent_cols = [
        "default_name", "default_description", "default_prompt",
        "default_tools", "default_model"
    ]
    if table_exists(cursor, "subagents"):
        for col in subagent_cols:
            _validate_identifier(col, "column name")  # Defense-in-depth
            col_type = "JSON" if col == "default_tools" else "TEXT"
            if not column_exists(cursor, "subagents", col):
                sql = f"ALTER TABLE subagents ADD COLUMN {col} {col_type}"
                cursor.execute(sql)
                migrations_applied.append(f"subagents.{col}")

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
    delete_session,
    toggle_session_favorite,
    set_session_favorite,
    session_has_forks,
    get_session_forks,
    get_session_messages,
    add_session_message,
    add_session_messages_batch,
    upsert_team_member_message,
    delete_session_message,
    delete_orphan_user_turn,
    discard_orphan_first_turn,
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

from app.db.installs import (  # noqa: F401
    register_install,
    touch_install_last_seen,
    set_install_name,
    set_install_base_url,
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
)

from app.db.queued_messages import (  # noqa: F401
    add_queued_message,
    get_queued_messages,
    pop_next_queued_message,
    remove_queued_message,
    clear_queued_messages,
    requeue_message_at_front,
)

# Backward-compatible alias kept for callers that import the private helper.
_parse_subagent_json_fields = None
try:
    from app.db.subagents import _parse_json_fields as _parse_subagent_json_fields  # noqa: F401
except ImportError:
    pass
