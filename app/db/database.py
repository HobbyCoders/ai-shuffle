"""
SQLite database setup and operations
"""

import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

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

logger = logging.getLogger(__name__)


# Schema version for migrations
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
SCHEMA_VERSION = 39


def _run_column_migrations(cursor: sqlite3.Cursor):
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
    except sqlite3.OperationalError:
        pass  # Index doesn't exist, that's fine

    # Migrate notes table to global (remove project_id and linked_sessions) (v31)
    # SQLite doesn't support DROP COLUMN directly, so we recreate the table
    if table_exists(cursor, "notes") and column_exists(cursor, "notes", "project_id"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes_new (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT DEFAULT '',
                tags JSON DEFAULT '[]',
                is_pinned BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            INSERT INTO notes_new (id, title, content, tags, is_pinned, created_at, updated_at)
            SELECT id, title, content, tags, is_pinned, created_at, updated_at
            FROM notes
        """)
        cursor.execute("DROP TABLE notes")
        cursor.execute("ALTER TABLE notes_new RENAME TO notes")
        # Recreate indexes (project index no longer needed)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(is_pinned DESC, updated_at DESC)")
        migrations_applied.append("notes_table_global")

    # Migrate sessions.profile_id to be nullable (for profile deletion preserving sessions)
    # Older databases may have profile_id as NOT NULL, but we need it nullable so
    # sessions can persist after their associated profile is deleted
    if table_exists(cursor, "sessions") and column_is_not_null(cursor, "sessions", "profile_id"):
        logger.info("Migrating sessions table to make profile_id nullable...")
        # SQLite doesn't support ALTER COLUMN, so we must recreate the table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions_new (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                profile_id TEXT,
                api_user_id TEXT,
                sdk_session_id TEXT,
                worktree_id TEXT,
                worktree_branch TEXT,
                worktree_path TEXT,
                title TEXT,
                status TEXT DEFAULT 'active',
                is_favorite BOOLEAN DEFAULT FALSE,
                total_cost_usd REAL DEFAULT 0,
                total_tokens_in INTEGER DEFAULT 0,
                total_tokens_out INTEGER DEFAULT 0,
                turn_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                parent_session_id TEXT,
                fork_point_message_index INTEGER,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
                FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL,
                FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
            )
        """)
        # Copy all data from old table to new
        cursor.execute("""
            INSERT INTO sessions_new (
                id, project_id, profile_id, api_user_id, sdk_session_id, worktree_id,
                title, status, is_favorite, total_cost_usd, total_tokens_in, total_tokens_out,
                turn_count, created_at, updated_at, parent_session_id, fork_point_message_index
            )
            SELECT
                id, project_id, profile_id, api_user_id, sdk_session_id, worktree_id,
                title, status, is_favorite, total_cost_usd, total_tokens_in, total_tokens_out,
                turn_count, created_at, updated_at, parent_session_id, fork_point_message_index
            FROM sessions
        """)
        cursor.execute("DROP TABLE sessions")
        cursor.execute("ALTER TABLE sessions_new RENAME TO sessions")
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

    # Add worktree_branch and worktree_path columns to sessions for persistent worktree info
    # These store worktree info directly on the session so it survives worktree deletion
    if table_exists(cursor, "sessions"):
        if not column_exists(cursor, "sessions", "worktree_branch"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN worktree_branch TEXT")
            migrations_applied.append("sessions.worktree_branch")
        if not column_exists(cursor, "sessions", "worktree_path"):
            cursor.execute("ALTER TABLE sessions ADD COLUMN worktree_path TEXT")
            migrations_applied.append("sessions.worktree_path")
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
            except sqlite3.OperationalError:
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
            cursor.execute("UPDATE automations SET triggers = ? WHERE id = ?", (triggers_arr, auto_id))
        migrations_applied.append("automations.triggers_backfill")

    # Add message_template column to project_hooks (v38)
    if table_exists(cursor, "project_hooks") and not column_exists(cursor, "project_hooks", "message_template"):
        cursor.execute("ALTER TABLE project_hooks ADD COLUMN message_template TEXT")
        migrations_applied.append("project_hooks.message_template")

    # Make project_hooks.project_id nullable (v38)
    if table_exists(cursor, "project_hooks") and column_is_not_null(cursor, "project_hooks", "project_id"):
        logger.info("Migrating project_hooks table to make project_id nullable...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_hooks_new (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                name TEXT NOT NULL,
                event TEXT NOT NULL,
                hook_type TEXT NOT NULL,
                command TEXT,
                timeout INTEGER DEFAULT 30000,
                url TEXT,
                secret TEXT,
                enabled BOOLEAN DEFAULT TRUE,
                tool_matcher TEXT,
                message_template TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_triggered_at TIMESTAMP,
                failure_count INTEGER DEFAULT 0,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            INSERT INTO project_hooks_new (
                id, project_id, name, event, hook_type, command, timeout,
                url, secret, enabled, tool_matcher, message_template,
                created_at, updated_at, last_triggered_at, failure_count
            )
            SELECT
                id, project_id, name, event, hook_type, command, timeout,
                url, secret, enabled, tool_matcher, message_template,
                created_at, updated_at, last_triggered_at, failure_count
            FROM project_hooks
        """)
        cursor.execute("DROP TABLE project_hooks")
        cursor.execute("ALTER TABLE project_hooks_new RENAME TO project_hooks")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_project_id ON project_hooks(project_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_event ON project_hooks(event)")
        migrations_applied.append("project_hooks.project_id_nullable")

    # Ensure performance-critical indexes exist (idempotent, no version gate).
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)")

    if migrations_applied:
        logger.info(f"Applied column migrations: {', '.join(migrations_applied)}")


def init_database():
    """Initialize the database with schema"""
    logger.info(f"Initializing database at {settings.db_path}")

    # Ensure data directory exists
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

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
            _create_schema(cursor)
            cursor.execute("DELETE FROM schema_version")
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

        # Always run column migrations to handle edge cases where
        # schema version is correct but columns are missing
        _run_column_migrations(cursor)

    logger.info("Database initialized successfully")


def _create_schema(cursor: sqlite3.Cursor):
    """Create all database tables"""

    # System settings (key-value store for global configuration)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Admin user (single user system)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY DEFAULT 1,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (id = 1)
        )
    """)

    # Agent profiles
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config JSON NOT NULL,
            mcp_tools JSON DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Projects (workspaces)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            path TEXT NOT NULL,
            settings JSON DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Sessions (conversations)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            profile_id TEXT,  -- Nullable: sessions persist even when profile is deleted
            api_user_id TEXT,
            sdk_session_id TEXT,
            worktree_id TEXT,
            worktree_branch TEXT,
            worktree_path TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            is_favorite BOOLEAN DEFAULT FALSE,
            total_cost_usd REAL DEFAULT 0,
            total_tokens_in INTEGER DEFAULT 0,
            total_tokens_out INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        )
    """)

    # Session messages (with sync support)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSON,
            metadata JSON,
            profile_id TEXT,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # Sync log for tracking changes (used for polling fallback)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            data JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # Auth sessions (login tokens)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Usage tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            profile_id TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API users (for programmatic access with isolated workspaces)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE,
            password_hash TEXT,
            api_key_hash TEXT NOT NULL,
            project_id TEXT,
            profile_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            web_login_allowed BOOLEAN DEFAULT TRUE,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # Add columns if they don't exist (migration for existing DBs)
    # Note: SQLite doesn't support ADD COLUMN with UNIQUE constraint, so we add
    # the column without UNIQUE and rely on the CREATE UNIQUE INDEX below
    try:
        cursor.execute("ALTER TABLE api_users ADD COLUMN username TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE api_users ADD COLUMN password_hash TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE api_users ADD COLUMN web_login_allowed BOOLEAN DEFAULT TRUE")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # API user profiles junction table (many-to-many relationship)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_profiles (
            api_user_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (api_user_id, profile_id),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Add is_favorite column to sessions (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN is_favorite BOOLEAN DEFAULT FALSE")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add fork columns to sessions (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN fork_point_message_index INTEGER")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add worktree_id column to sessions (migration for v23 - worktree-session decoupling)
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN worktree_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate existing worktree-session relationships from worktrees.session_id to sessions.worktree_id
    # This preserves backward compatibility while moving to the new model
    # First check if migration is needed (any sessions without worktree_id that should have one)
    # Note: This check and migration only applies to existing databases, not fresh installs
    try:
        cursor.execute("""
            SELECT COUNT(*) as cnt FROM sessions s
            WHERE s.worktree_id IS NULL
            AND EXISTS (SELECT 1 FROM worktrees w WHERE w.session_id = s.id)
        """)
        migration_needed = cursor.fetchone()["cnt"] > 0

        if migration_needed:
            cursor.execute("""
                UPDATE sessions SET worktree_id = (
                    SELECT w.id FROM worktrees w WHERE w.session_id = sessions.id
                )
                WHERE worktree_id IS NULL
                AND EXISTS (SELECT 1 FROM worktrees w WHERE w.session_id = sessions.id)
            """)
            migrated_count = cursor.rowcount
            logger.info(f"Migrated {migrated_count} worktree-session relationships to sessions.worktree_id")
    except sqlite3.OperationalError as e:
        # Handle specific SQLite errors (e.g., table doesn't exist during initial setup)
        if "no such table" in str(e).lower():
            logger.debug("Worktrees table does not exist yet, skipping migration")
        else:
            logger.error(f"Failed to migrate worktree-session relationships: {e}")
            raise  # Re-raise unexpected errors

    # Login attempts tracking for brute force protection
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            username TEXT,
            success BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Account lockout tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_lockouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            username TEXT,
            locked_until TIMESTAMP NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API key web sessions (for API key users logging into web UI)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_key_sessions (
            token TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)


    # User preferences (for persisting open tabs across devices)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value JSON,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_type, user_id, key)
        )
    """)

    # Global subagents (independent of profiles)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tools JSON,
            model TEXT,
            skills JSON,
            memory TEXT,
            mcp_servers JSON,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Permission rules for tool access control
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permission_rules (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            tool_name TEXT NOT NULL,
            tool_pattern TEXT,
            decision TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Tags for organizing sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Junction table for session-tag relationships
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_tags (
            session_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, tag_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    # Session templates for starter prompts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            prompt TEXT NOT NULL,
            profile_id TEXT,
            icon TEXT,
            category TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # Webhooks for external integrations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT,
            events JSON NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0
        )
    """)

    # Audit log for security events
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            user_type TEXT DEFAULT 'admin',
            event_type TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add profile_id column to session_messages (migration for per-query profile tracking)
    try:
        cursor.execute("ALTER TABLE session_messages ADD COLUMN profile_id TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add built-in subagent columns (migration for v26)
    # These store the original default values so admins can revert after editing
    try:
        cursor.execute("ALTER TABLE subagents ADD COLUMN default_name TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE subagents ADD COLUMN default_description TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE subagents ADD COLUMN default_prompt TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE subagents ADD COLUMN default_tools JSON")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE subagents ADD COLUMN default_model TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Drop agent execution engine tables (migration for v27)
    # These tables are no longer used after removing the background agent feature
    # Drop in correct order to respect foreign key constraints
    try:
        cursor.execute("DROP TABLE IF EXISTS agent_logs")
    except sqlite3.OperationalError as e:
        logger.debug(f"Could not drop agent_logs table: {e}")
    try:
        cursor.execute("DROP TABLE IF EXISTS agent_tasks")
    except sqlite3.OperationalError as e:
        logger.debug(f"Could not drop agent_tasks table: {e}")
    try:
        cursor.execute("DROP TABLE IF EXISTS agent_runs")
    except sqlite3.OperationalError as e:
        logger.debug(f"Could not drop agent_runs table: {e}")

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_templates_category ON templates(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_templates_profile ON templates(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_favorite ON sessions(is_favorite)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_created ON usage_log(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_users_active ON api_users(is_active)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_api_users_username ON api_users(username) WHERE username IS NOT NULL")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_profiles_user ON api_user_profiles(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_profiles_profile ON api_user_profiles(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_api_user ON sessions(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_log_session ON sync_log(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_log_created ON sync_log(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_created ON login_attempts(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_lockouts_ip ON account_lockouts(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_lockouts_username ON account_lockouts(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_key_sessions_user ON api_key_sessions(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_user ON user_preferences(user_type, user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subagents_name ON subagents(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_rules_profile ON permission_rules(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_rules_tool ON permission_rules(tool_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_session ON session_tags(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_worktree ON sessions(worktree_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_active ON webhooks(is_active)")

    # Rate limit configurations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            requests_per_minute INTEGER DEFAULT 20,
            requests_per_hour INTEGER DEFAULT 200,
            requests_per_day INTEGER DEFAULT 1000,
            concurrent_requests INTEGER DEFAULT 3,
            priority INTEGER DEFAULT 0,
            is_unlimited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_key_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # Request log for rate limit tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            endpoint TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_ms INTEGER,
            status TEXT DEFAULT 'success'
        )
    """)

    # Create rate limit indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_user ON rate_limits(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_api_key ON rate_limits(api_key_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_log_user ON request_log(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_log_api_key ON request_log(api_key_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_event ON audit_log(event_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)")

    # Git repositories (tracks git state per project)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS git_repositories (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE,
            remote_url TEXT,
            default_branch TEXT DEFAULT 'main',
            github_repo_name TEXT,
            last_synced_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # Worktrees (for parallel session work on branches)
    # Note: session_id is deprecated - relationship is now tracked via sessions.worktree_id
    # Kept for backward compatibility during migration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            session_id TEXT,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_branch TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (repository_id) REFERENCES git_repositories(id) ON DELETE CASCADE
        )
    """)

    # Create git indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_git_repositories_project ON git_repositories(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_repository ON worktrees(repository_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_session ON worktrees(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_branch ON worktrees(branch_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_status ON worktrees(status)")
    # Composite index for efficient active worktree queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_repo_status ON worktrees(repository_id, status)")
    # NOTE: We intentionally do NOT create a unique index on (repository_id, branch_name)
    # because users should be able to have multiple worktrees on the same branch.
    # Each worktree is an independent workspace that can check out any branch.

    # =========================================================================
    # Notes - Global notes for ideas, bugs, planning (not project-scoped)
    # =========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            tags JSON DEFAULT '[]',
            is_pinned BOOLEAN DEFAULT FALSE,
            file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Indexes for efficient queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(is_pinned DESC, updated_at DESC)")

    # =========================================================================
    # Queued Messages — server-side message queue for streaming sessions
    # =========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS queued_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            profile_id TEXT,
            project_id TEXT,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_queued_messages_session ON queued_messages(session_id, position ASC)")

    # =========================================================================
    # Project Hooks - For Claude Code event handling
    # =========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_hooks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            name TEXT NOT NULL,
            event TEXT NOT NULL,
            hook_type TEXT NOT NULL,
            command TEXT,
            timeout INTEGER DEFAULT 30000,
            url TEXT,
            secret TEXT,
            enabled BOOLEAN DEFAULT TRUE,
            tool_matcher TEXT,
            message_template TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # Index for efficient project hooks lookup
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_project_id ON project_hooks(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_event ON project_hooks(event)")

    # =========================================================================
    # API User Credentials & Policies (per-user API keys and GitHub auth)
    # =========================================================================

    # Credential policies - admin controls which keys are admin-provided vs user-provided
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credential_policies (
            id TEXT PRIMARY KEY,
            policy TEXT NOT NULL DEFAULT 'admin_provided',
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Per-user credentials (encrypted API keys, GitHub PAT)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_credentials (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        )
    """)

    # Per-user GitHub configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_github_config (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL UNIQUE,
            github_username TEXT,
            github_avatar_url TEXT,
            default_repo TEXT,
            default_branch TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # Per-user credential policy overrides (admin can set different policies per user)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_credential_policies (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            policy TEXT NOT NULL DEFAULT 'optional',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        )
    """)

    # Create indexes for new tables
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_credentials_user ON api_user_credentials(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_credentials_type ON api_user_credentials(credential_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_github_config_user ON api_user_github_config(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_credential_policies_user ON user_credential_policies(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_credential_policies_type ON user_credential_policies(credential_type)")

    # Initialize default credential policies if not exist
    default_policies = [
        ('openai_api_key', 'optional', 'OpenAI API key for AI tools (TTS, STT, GPT Image, Sora)'),
        ('gemini_api_key', 'optional', 'Google Gemini API key for Nano Banana, Imagen, Veo'),
        ('meshy_api_key', 'optional', 'Meshy API key for 3D model generation'),
        ('github_pat', 'user_provided', 'GitHub Personal Access Token for repository access'),
    ]
    for policy_id, policy, description in default_policies:
        cursor.execute("""
            INSERT OR IGNORE INTO credential_policies (id, policy, description)
            VALUES (?, ?, ?)
        """, (policy_id, policy, description))

    # =========================================================================
    # Mobile Device Management
    # =========================================================================

    # Mobile devices - registered mobile apps for push notifications and actions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mobile_devices (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL UNIQUE,
            device_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            api_key_hash TEXT NOT NULL,
            fcm_token TEXT,
            capabilities JSON DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
    """)

    # Pending actions - actions waiting for mobile confirmation
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            params JSON DEFAULT '{}',
            security_level TEXT DEFAULT 'normal',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        )
    """)

    # Trusted actions - auto-approve patterns for specific devices
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trusted_actions (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            action_pattern TEXT NOT NULL,
            auto_approve BOOLEAN DEFAULT TRUE,
            require_preview BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        )
    """)

    # Mobile schedules - proactive notifications and briefings
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mobile_schedules (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            trigger_time TIMESTAMP,
            hour INTEGER,
            minute INTEGER,
            params JSON DEFAULT '{}',
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        )
    """)

    # Mobile device indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mobile_devices_device_id ON mobile_devices(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_device ON pending_actions(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_expires ON pending_actions(expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trusted_actions_device ON trusted_actions(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mobile_schedules_device ON mobile_schedules(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mobile_schedules_enabled ON mobile_schedules(enabled)")

    # ========================================================================
    # Connectors — external messaging platform integrations
    # ========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS connectors (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            api_user_id TEXT,
            config JSON NOT NULL DEFAULT '{}',
            events JSON NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'disconnected',
            status_message TEXT,
            last_connected_at TIMESTAMP,
            last_error TEXT,
            last_event_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS connector_event_log (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'outbound',
            payload JSON,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS connector_sessions (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            platform_chat_id TEXT NOT NULL,
            platform_user_id TEXT,
            platform_user_name TEXT,
            session_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL,
            UNIQUE(connector_id, platform_chat_id)
        )
    """)

    # Connector indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connector_event_log_connector ON connector_event_log(connector_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connector_sessions_connector ON connector_sessions(connector_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connector_sessions_session ON connector_sessions(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connectors_active ON connectors(is_active)")

    # ========================================================================
    # Automations — scheduled and event-driven AI task execution
    # ========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            trigger_type TEXT NOT NULL,
            trigger_config JSON NOT NULL DEFAULT '{}',
            prompt TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            project_id TEXT,
            output_config JSON NOT NULL DEFAULT '{"type": "session"}',
            max_turns INTEGER DEFAULT 1,
            timeout_seconds INTEGER DEFAULT 300,
            max_cost_per_run REAL,
            max_cost_per_day REAL,
            max_cost_total REAL,
            max_runs_per_day INTEGER,
            total_cost_used REAL DEFAULT 0.0,
            is_enabled BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'idle',
            last_run_at TIMESTAMP,
            last_error TEXT,
            run_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            requires_approval BOOLEAN DEFAULT FALSE,
            action_steps TEXT DEFAULT NULL,
            triggers TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE RESTRICT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automation_runs (
            id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            trigger_data JSON,
            session_id TEXT,
            prompt_rendered TEXT,
            response TEXT,
            error TEXT,
            cost_usd REAL DEFAULT 0.0,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            output_status TEXT DEFAULT 'pending',
            output_error TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automation_approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt_preview TEXT,
            decided_by TEXT,
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES automation_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE
        )
    """)

    # Automation indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_automation ON automation_runs(automation_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_status ON automation_runs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_approvals_status ON automation_approvals(status, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automations_enabled ON automations(is_enabled)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automations_trigger ON automations(trigger_type)")

    # ========================================================================
    # fal.ai Model Catalog — dynamic model discovery cache (v35)
    # ========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fal_model_catalog (
            endpoint_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            thumbnail_url TEXT,
            tags_json TEXT DEFAULT '[]',
            group_key TEXT,
            group_label TEXT,
            tool_type TEXT,
            output_type TEXT,
            updated_at TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fal_catalog_category ON fal_model_catalog(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fal_catalog_tool_type ON fal_model_catalog(tool_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fal_catalog_output_type ON fal_model_catalog(output_type)")


# ============================================================================
# Admin Operations
# ============================================================================

def is_setup_required() -> bool:
    """Check if admin setup is required"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM admin")
        row = cursor.fetchone()
        return row["count"] == 0


def get_admin() -> Optional[Dict[str, Any]]:
    """Get admin user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admin LIMIT 1")
        return row_to_dict(cursor.fetchone())


def create_admin(username: str, password_hash: str) -> Dict[str, Any]:
    """Create admin user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO admin (id, username, password_hash) VALUES (1, ?, ?)",
            (username, password_hash)
        )
        return {"id": 1, "username": username}


def update_admin_password(password_hash: str) -> bool:
    """Update admin password"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE admin SET password_hash = ? WHERE id = 1",
            (password_hash,)
        )
        return cursor.rowcount > 0




# ============================================================================
# Audit Log Operations
# ============================================================================

def create_audit_log(
    event_id: str,
    event_type: str,
    user_id: Optional[str] = None,
    user_type: str = "admin",
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Create an audit log entry"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO audit_log (id, user_id, user_type, event_type, ip_address, user_agent, details)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, user_id, user_type, event_type, ip_address, user_agent, json.dumps(details) if details else None)
        )
        return {
            "id": event_id,
            "user_id": user_id,
            "user_type": user_type,
            "event_type": event_type,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "details": details
        }


def get_audit_logs(
    user_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get audit logs with optional filters"""
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []

    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("details"):
                row["details"] = json.loads(row["details"]) if isinstance(row["details"], str) else row["details"]
        return rows


def get_audit_log_count(user_id: Optional[str] = None, event_type: Optional[str] = None) -> int:
    """Get total count of audit logs"""
    query = "SELECT COUNT(*) as count FROM audit_log WHERE 1=1"
    params = []

    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return row["count"]




# ============================================================================
# Auth Session Operations
# ============================================================================

def create_auth_session(token: str, expires_at: datetime) -> Dict[str, Any]:
    """Create an auth session token"""
    logger.info(
        f"create_auth_session: Creating token ...{token[-4:]} "
        f"(expires_at={expires_at.isoformat()})"
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO auth_sessions (token, expires_at) VALUES (?, ?)",
            (token, expires_at.isoformat())
        )
        return {"token": token, "expires_at": expires_at}


_auth_session_empty_logged = False  # Rate-limit the verbose empty-table diagnostic


def get_auth_session(token: str) -> Optional[Dict[str, Any]]:
    """Get an auth session by token"""
    global _auth_session_empty_logged
    with get_db() as conn:
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "SELECT * FROM auth_sessions WHERE token = ? AND expires_at > ?",
            (token, now_iso)
        )
        result = row_to_dict(cursor.fetchone())

        # Debug logging for the mystery logout issue
        if result is None and token:
            # Check how many tokens exist total
            cursor.execute("SELECT COUNT(*) as cnt FROM auth_sessions")
            total_count = cursor.fetchone()["cnt"]

            # Check if THIS token exists (regardless of expiry)
            cursor.execute(
                "SELECT token, expires_at, created_at FROM auth_sessions WHERE token = ?",
                (token,)
            )
            exact_match = cursor.fetchone()

            # Log concise summary at debug level (avoid spamming on repeated WebSocket retries)
            logger.debug(
                f"get_auth_session FAILED for ...{token[-4:]}: "
                f"total_tokens_in_db={total_count}, "
                f"token_exists={exact_match is not None}, "
                f"db_path={settings.db_path}, "
                f"now={now_iso}"
            )
            if exact_match:
                logger.warning(
                    f"  Token ...{token[-4:]} found but expired/invalid: "
                    f"expires_at={exact_match['expires_at']}, created_at={exact_match['created_at']}"
                )

            # If table is empty, log verbose diagnostics only ONCE to avoid log spam
            # (This is expected before first login since sessions are cleared on startup)
            if total_count == 0 and not _auth_session_empty_logged:
                _auth_session_empty_logged = True
                import traceback
                import os
                logger.warning(
                    f"auth_sessions table is empty (expected before first login).\n"
                    f"Stack trace:\n{''.join(traceback.format_stack())}\n"
                    f"Database file exists: {settings.db_path.exists()}\n"
                    f"Database file size: {settings.db_path.stat().st_size if settings.db_path.exists() else 'N/A'}\n"
                    f"Process ID: {os.getpid()}"
                )
                # Check if other tables have data (to rule out complete DB wipe)
                cursor.execute("SELECT COUNT(*) as cnt FROM sessions")
                sessions_count = cursor.fetchone()["cnt"]
                cursor.execute("SELECT COUNT(*) as cnt FROM profiles")
                profiles_count = cursor.fetchone()["cnt"]
                logger.warning(
                    f"Other table counts - sessions: {sessions_count}, profiles: {profiles_count}"
                )

        # Reset the flag when a session is successfully created/found
        # so the diagnostic fires again if sessions unexpectedly disappear later
        if result is not None:
            _auth_session_empty_logged = False

        return result


def delete_auth_session(token: str):
    """Delete an auth session"""
    import traceback
    # Log deletion with stack trace to help debug unexpected logouts
    logger.warning(
        f"delete_auth_session: Deleting token ...{token[-4:] if token else 'None'}. "
        f"Stack: {''.join(traceback.format_stack()[-5:-1])}"
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


def clear_all_sessions():
    """
    Clear all authentication sessions (admin and API user).
    Called on app startup to force re-login, which is required
    to restore the encryption key to memory.
    """
    import traceback
    # Log the call stack to help diagnose unexpected session clearing
    logger.warning(f"clear_all_sessions() called! Stack trace:\n{''.join(traceback.format_stack())}")

    with get_db() as conn:
        cursor = conn.cursor()
        # Clear admin sessions
        cursor.execute("DELETE FROM auth_sessions")
        admin_count = cursor.rowcount
        # Clear API user web sessions
        cursor.execute("DELETE FROM api_key_sessions")
        api_count = cursor.rowcount
        logger.warning(f"Cleared {admin_count} admin sessions and {api_count} API key sessions")
        return admin_count, api_count


def cleanup_expired_sessions() -> dict:
    """Remove expired auth sessions, API key sessions, login attempts, and lockouts.

    This should be called periodically (e.g. every hour) to prevent unbounded
    growth of the auth-related tables. Each cleanup is idempotent.

    Returns:
        dict with counts of rows deleted from each table.
    """
    from datetime import timedelta
    now_iso = datetime.now(timezone.utc).isoformat()
    results = {}

    with get_db() as conn:
        cursor = conn.cursor()

        # Expired admin sessions
        cursor.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now_iso,))
        results["auth_sessions"] = cursor.rowcount

        # Expired API key sessions
        cursor.execute("DELETE FROM api_key_sessions WHERE expires_at <= ?", (now_iso,))
        results["api_key_sessions"] = cursor.rowcount

        # Expired account lockouts
        cursor.execute("DELETE FROM account_lockouts WHERE locked_until <= ?", (now_iso,))
        results["account_lockouts"] = cursor.rowcount

        # Old login attempts (older than 24 hours — well past the 15-minute window)
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cursor.execute("DELETE FROM login_attempts WHERE created_at < ?", (cutoff_24h,))
        results["login_attempts"] = cursor.rowcount

    total = sum(results.values())
    if total > 0:
        logger.info(f"Session cleanup: removed {results}")
    else:
        logger.debug("Session cleanup: nothing to remove")

    return results


def run_database_maintenance() -> dict:
    """Run periodic SQLite maintenance to prevent progressive slowdown.

    Should be called every few hours. Handles:
    - WAL checkpoint (keeps WAL file small, prevents read amplification)
    - PRAGMA optimize (updates query planner statistics)
    - Cleanup of unbounded log tables (older than 7 days)
    - VACUUM (reclaims disk space from deleted rows)

    Returns:
        dict with maintenance results.
    """
    from datetime import timedelta
    results = {}
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()

        # 1. WAL checkpoint — moves WAL data back to main DB, resets WAL file.
        # TRUNCATE mode is the most thorough: checkpoint + truncate WAL to zero bytes.
        try:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            wal_result = cursor.fetchone()
            results["wal_checkpoint"] = {
                "busy": wal_result[0] if wal_result else -1,
                "log_pages": wal_result[1] if wal_result else -1,
                "checkpointed_pages": wal_result[2] if wal_result else -1,
            }
        except Exception as e:
            results["wal_checkpoint"] = f"error: {e}"
            logger.warning(f"WAL checkpoint failed: {e}")

        # 2. Update query planner statistics.
        # PRAGMA optimize runs ANALYZE on tables whose stats are stale.
        try:
            cursor.execute("PRAGMA optimize")
            results["optimize"] = "ok"
        except Exception as e:
            results["optimize"] = f"error: {e}"

        # 3. Cleanup unbounded log tables (older than 7 days).
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
                cursor.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff_7d,))
                results[f"cleaned_{table}"] = cursor.rowcount
            except Exception as e:
                results[f"cleaned_{table}"] = f"error: {e}"

    # 4. VACUUM — reclaims disk space. Must run outside a transaction.
    try:
        conn = get_connection()
        try:
            conn.execute("VACUUM")
            results["vacuum"] = "ok"
        finally:
            conn.close()
    except Exception as e:
        results["vacuum"] = f"error: {e}"
        logger.warning(f"VACUUM failed: {e}")

    logger.info(f"Database maintenance completed: {results}")
    return results


# ============================================================================
# Profile Operations
# ============================================================================

def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["config"] = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            row["mcp_tools"] = json.loads(row["mcp_tools"]) if isinstance(row["mcp_tools"], str) else row["mcp_tools"]
        return row


def get_all_profiles() -> List[Dict[str, Any]]:
    """Get all profiles"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM profiles ORDER BY is_builtin DESC, name ASC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["config"] = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            row["mcp_tools"] = json.loads(row["mcp_tools"]) if isinstance(row["mcp_tools"], str) else row["mcp_tools"]
        return rows


def create_profile(
    profile_id: str,
    name: str,
    description: Optional[str],
    config: Dict[str, Any],
    is_builtin: bool = False,
    mcp_tools: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Create a new profile"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO profiles (id, name, description, config, is_builtin, mcp_tools, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, name, description, json.dumps(config), is_builtin, json.dumps(mcp_tools or []), now, now)
        )
    return get_profile(profile_id)


def update_profile(
    profile_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    allow_builtin: bool = False
) -> Optional[Dict[str, Any]]:
    """Update a profile

    Args:
        profile_id: The profile ID to update
        name: Optional new name
        description: Optional new description
        config: Optional new config
        allow_builtin: If True, allows updating builtin profiles (for migrations)

    Uses a single database connection for the existence check, update, and
    final read to avoid opening three separate connections.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Check existence within the same connection
        cursor.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        existing = row_to_dict(cursor.fetchone())
        if not existing:
            return None
        if existing.get("is_builtin") and not allow_builtin:
            return None

        updates = []
        values = []
        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if description is not None:
            updates.append("description = ?")
            values.append(description)
        if config is not None:
            updates.append("config = ?")
            values.append(json.dumps(config))

        if updates:
            updates.append("updated_at = ?")
            values.append(datetime.now(timezone.utc).isoformat())
            values.append(profile_id)

            cursor.execute(
                f"UPDATE profiles SET {', '.join(updates)} WHERE id = ?",
                values
            )

        # Re-read the updated row within the same connection
        cursor.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["config"] = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            row["mcp_tools"] = json.loads(row["mcp_tools"]) if isinstance(row["mcp_tools"], str) else row["mcp_tools"]
        return row


def delete_profile(profile_id: str) -> bool:
    """Delete a profile and unlink sessions.

    Sessions are preserved when a profile is deleted - their profile_id is set to NULL.
    This allows users to keep their chat history even after deleting a profile.
    """
    existing = get_profile(profile_id)
    if not existing:
        return False

    with get_db() as conn:
        cursor = conn.cursor()
        # Unlink sessions from this profile (sessions are preserved with NULL profile_id)
        cursor.execute("UPDATE sessions SET profile_id = NULL WHERE profile_id = ?", (profile_id,))
        # Unlink session_messages from this profile
        cursor.execute("UPDATE session_messages SET profile_id = NULL WHERE profile_id = ?", (profile_id,))
        # Unlink usage_log entries from this profile
        cursor.execute("UPDATE usage_log SET profile_id = NULL WHERE profile_id = ?", (profile_id,))
        # Now delete the profile
        cursor.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        return cursor.rowcount > 0


def get_profile_session_count(profile_id: str) -> int:
    """Get the number of sessions associated with a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sessions WHERE profile_id = ?", (profile_id,))
        return cursor.fetchone()[0]


def get_all_profile_session_counts() -> Dict[str, int]:
    """Get session counts for all profiles in a single query"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT profile_id, COUNT(*) as count
            FROM sessions
            GROUP BY profile_id
        """)
        return {row["profile_id"]: row["count"] for row in cursor.fetchall()}


def set_profile_builtin(profile_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE profiles SET is_builtin = ?, updated_at = ? WHERE id = ?",
            (is_builtin, datetime.now(timezone.utc).isoformat(), profile_id)
        )
        return cursor.rowcount > 0


# ============================================================================
# Project Operations
# ============================================================================

def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    """Get a project by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
        return row


def get_all_projects() -> List[Dict[str, Any]]:
    """Get all projects"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects ORDER BY name ASC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
        return rows


def create_project(
    project_id: str,
    name: str,
    description: Optional[str],
    path: str,
    settings_dict: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Create a new project"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO projects (id, name, description, path, settings, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (project_id, name, description, path, json.dumps(settings_dict or {}), now, now)
        )
    return get_project(project_id)


def update_project(
    project_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    path: Optional[str] = None,
    settings_dict: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Update a project.

    Uses a single database connection for the existence check, update, and
    final read to avoid opening three separate connections.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Check existence within the same connection
        cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        existing = row_to_dict(cursor.fetchone())
        if not existing:
            return None

        updates = []
        values = []
        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if description is not None:
            updates.append("description = ?")
            values.append(description)
        if path is not None:
            updates.append("path = ?")
            values.append(path)
        if settings_dict is not None:
            updates.append("settings = ?")
            values.append(json.dumps(settings_dict))

        if updates:
            updates.append("updated_at = ?")
            values.append(datetime.now(timezone.utc).isoformat())
            values.append(project_id)

            cursor.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
                values
            )

        # Re-read the updated row within the same connection
        cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
        return row


def get_project_session_count(project_id: str) -> int:
    """Get the number of sessions associated with a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
        return cursor.fetchone()[0]


def get_all_project_session_counts() -> Dict[str, int]:
    """Get session counts for all projects in a single query"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT project_id, COUNT(*) as count
            FROM sessions
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        """)
        return {row["project_id"]: row["count"] for row in cursor.fetchall()}


def delete_project(project_id: str, hard_delete: bool = False) -> bool:
    """Delete a project and disassociate its sessions.

    By default performs a soft-delete: sessions have their project_id set to
    NULL (preserving chat history) and the project row is deleted.  Pass
    ``hard_delete=True`` to permanently remove the session rows as well.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if hard_delete:
            cursor.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
        else:
            # Soft-delete: unlink sessions so chat history is preserved
            cursor.execute(
                "UPDATE sessions SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
        # Delete the project itself
        cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0


# ============================================================================
# Session Operations
# ============================================================================

def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get a session by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_session_by_sdk_id(sdk_session_id: str) -> Optional[Dict[str, Any]]:
    """Get a session by SDK session ID (the Claude Code session ID)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE sdk_session_id = ?", (sdk_session_id,))
        return row_to_dict(cursor.fetchone())


def get_sessions(
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
    api_user_id: Optional[str] = None,
    api_users_only: bool = False,
    favorites_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get sessions with optional filters"""
    if tag_id:
        # Join with session_tags when filtering by tag
        query = """SELECT DISTINCT s.* FROM sessions s
                   INNER JOIN session_tags st ON s.id = st.session_id
                   WHERE st.tag_id = ?"""
        params = [tag_id]
    else:
        query = "SELECT * FROM sessions WHERE 1=1"
        params = []

    if project_id:
        query += " AND s.project_id = ?" if tag_id else " AND project_id = ?"
        params.append(project_id)
    if profile_id:
        query += " AND s.profile_id = ?" if tag_id else " AND profile_id = ?"
        params.append(profile_id)
    if status:
        query += " AND s.status = ?" if tag_id else " AND status = ?"
        params.append(status)
    if favorites_only:
        query += " AND s.is_favorite = TRUE" if tag_id else " AND is_favorite = TRUE"
    if api_users_only:
        # Filter for sessions that have an API user (exclude admin sessions)
        query += " AND s.api_user_id IS NOT NULL" if tag_id else " AND api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            query += " AND s.api_user_id = ?" if tag_id else " AND api_user_id = ?"
            params.append(api_user_id)
        else:
            query += " AND s.api_user_id IS NULL" if tag_id else " AND api_user_id IS NULL"

    query += " ORDER BY s.updated_at DESC LIMIT ? OFFSET ?" if tag_id else " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def get_sessions_paginated(
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
    api_user_id: Optional[str] = None,
    api_users_only: bool = False,
    favorites_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> tuple[List[Dict[str, Any]], int]:
    """Get sessions with optional filters, returning items and total count.

    Returns:
        Tuple of (sessions list, total count)
    """
    # Build the WHERE clause (shared between count and data queries)
    if tag_id:
        base_query = """FROM sessions s
                       INNER JOIN session_tags st ON s.id = st.session_id
                       WHERE st.tag_id = ?"""
        params: List[Any] = [tag_id]
        col_prefix = "s."
    else:
        base_query = "FROM sessions WHERE 1=1"
        params = []
        col_prefix = ""

    if project_id:
        base_query += f" AND {col_prefix}project_id = ?"
        params.append(project_id)
    if profile_id:
        base_query += f" AND {col_prefix}profile_id = ?"
        params.append(profile_id)
    if status:
        base_query += f" AND {col_prefix}status = ?"
        params.append(status)
    if favorites_only:
        base_query += f" AND {col_prefix}is_favorite = TRUE"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = ?"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"

    with get_db() as conn:
        cursor = conn.cursor()

        # Get total count
        if tag_id:
            count_query = f"SELECT COUNT(DISTINCT s.id) {base_query}"
        else:
            count_query = f"SELECT COUNT(*) {base_query}"
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]

        # Get paginated data
        if tag_id:
            data_query = f"SELECT DISTINCT s.* {base_query} ORDER BY s.updated_at DESC LIMIT ? OFFSET ?"
        else:
            data_query = f"SELECT * {base_query} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        cursor.execute(data_query, params + [limit, offset])
        sessions = rows_to_list(cursor.fetchall())

        return sessions, total


def create_session(
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    title: Optional[str] = None,
    api_user_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    fork_point_message_index: Optional[int] = None,
    worktree_id: Optional[str] = None,
    worktree_branch: Optional[str] = None,
    worktree_path: Optional[str] = None,
    _conn=None
) -> Dict[str, Any]:
    """Create a new session

    Args:
        session_id: Unique session identifier
        profile_id: Agent profile to use
        project_id: Optional project to work in
        title: Optional session title
        api_user_id: Optional API user owner
        parent_session_id: For forked sessions
        fork_point_message_index: Fork point index
        worktree_id: Optional worktree to link this session to
        worktree_branch: Optional branch name (persisted for display after worktree deletion)
        worktree_path: Optional worktree path (persisted for display after worktree deletion)
        _conn: Optional existing DB connection for transaction grouping
    """
    now = datetime.now(timezone.utc).isoformat()
    params = (session_id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, now, now)
    sql = """INSERT INTO sessions (id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql, params)
    else:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
    return get_session(session_id)


def create_session_with_stats(
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    title: Optional[str] = None,
    api_user_id: Optional[str] = None,
    sdk_session_id: Optional[str] = None,
    total_cost_usd: float = 0.0,
    total_tokens_in: int = 0,
    total_tokens_out: int = 0,
    turn_count: int = 0,
    created_at: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new session with all stats in a single atomic transaction.

    This is used for importing sessions where all metadata is known upfront.
    Using a single transaction ensures no orphaned sessions if any part fails.

    Args:
        session_id: Unique session identifier
        profile_id: Agent profile to use
        project_id: Optional project to work in
        title: Optional session title
        api_user_id: Optional API user owner
        sdk_session_id: Optional Claude SDK session ID for imported sessions
        total_cost_usd: Total cost in USD
        total_tokens_in: Total input tokens
        total_tokens_out: Total output tokens
        turn_count: Number of conversation turns
        created_at: Optional creation timestamp (ISO format), defaults to now
    """
    now = datetime.now(timezone.utc).isoformat()
    ts_created = created_at or now
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sessions (
                id, profile_id, project_id, title, api_user_id,
                sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, project_id, title, api_user_id,
             sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
             ts_created, now)
        )
    return get_session(session_id)


def update_session(
    session_id: str,
    sdk_session_id: Optional[str] = None,
    title: Optional[str] = None,
    status: Optional[str] = None,
    cost_increment: float = 0,
    tokens_in_increment: int = 0,
    tokens_out_increment: int = 0,
    turn_increment: int = 0,
    worktree_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a session with usage stats

    Args:
        worktree_id: Set to a worktree ID to link, or empty string "" to unlink
    """
    updates = ["updated_at = ?"]
    values = [datetime.now(timezone.utc).isoformat()]

    if sdk_session_id is not None:
        updates.append("sdk_session_id = ?")
        values.append(sdk_session_id)
    if title is not None:
        updates.append("title = ?")
        values.append(title)
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if cost_increment != 0:
        updates.append("total_cost_usd = total_cost_usd + ?")
        values.append(cost_increment)
    if tokens_in_increment != 0:
        updates.append("total_tokens_in = total_tokens_in + ?")
        values.append(tokens_in_increment)
    if tokens_out_increment != 0:
        updates.append("total_tokens_out = total_tokens_out + ?")
        values.append(tokens_out_increment)
    if turn_increment != 0:
        updates.append("turn_count = turn_count + ?")
        values.append(turn_increment)
    if worktree_id is not None:
        updates.append("worktree_id = ?")
        # Empty string means unlink (set to NULL)
        values.append(worktree_id if worktree_id else None)

    values.append(session_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
            values
        )

    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    """Delete a session and its messages"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0


def toggle_session_favorite(session_id: str) -> Optional[Dict[str, Any]]:
    """Toggle the is_favorite flag for a session and return the updated session"""
    with get_db() as conn:
        cursor = conn.cursor()
        # Toggle the favorite status
        cursor.execute(
            """UPDATE sessions SET is_favorite = NOT is_favorite, updated_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def set_session_favorite(session_id: str, is_favorite: bool) -> Optional[Dict[str, Any]]:
    """Set the is_favorite flag for a session and return the updated session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE sessions SET is_favorite = ?, updated_at = ?
               WHERE id = ?""",
            (is_favorite, datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def session_has_forks(session_id: str) -> bool:
    """Check if a session has any forked children"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM sessions WHERE parent_session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        return row["count"] > 0 if row else False


def get_session_forks(session_id: str) -> List[Dict[str, Any]]:
    """Get all forked children of a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM sessions WHERE parent_session_id = ?
               ORDER BY created_at DESC""",
            (session_id,)
        )
        return rows_to_list(cursor.fetchall())


# ============================================================================
# Session Message Operations
# ============================================================================

def get_session_messages(session_id: str) -> List[Dict[str, Any]]:
    """Get all messages for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("tool_input"):
                row["tool_input"] = json.loads(row["tool_input"]) if isinstance(row["tool_input"], str) else row["tool_input"]
            if row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        return rows


def add_session_message(
    session_id: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_input: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    profile_id: Optional[str] = None,
    _conn=None
) -> Dict[str, Any]:
    """Add a message to a session

    Args:
        session_id: The session ID
        role: Message role (user, assistant, system)
        content: Message content
        tool_name: Optional tool name for tool_use messages
        tool_input: Optional tool input for tool_use messages
        metadata: Optional metadata dict
        profile_id: Optional profile ID that created this message (for per-query tracking)
        _conn: Optional existing DB connection for transaction grouping
    """
    now = datetime.now(timezone.utc).isoformat()
    params = (session_id, role, content, tool_name,
              json.dumps(tool_input) if tool_input else None,
              json.dumps(metadata) if metadata else None,
              profile_id,
              now)
    sql = """INSERT INTO session_messages (session_id, role, content, tool_name, tool_input, metadata, profile_id, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""

    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql, params)
        lastrowid = cursor.lastrowid
    else:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            lastrowid = cursor.lastrowid

    msg = {
        "id": lastrowid,
        "session_id": session_id,
        "role": role,
        "content": content,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "metadata": metadata,
        "profile_id": profile_id,
        "created_at": now
    }

    # Update in-memory profile map cache (avoids DB re-read on next load)
    if role == "user" and profile_id and content:
        from app.core.cost_utils import update_profile_map
        update_profile_map(session_id, content, profile_id)

    return msg


def delete_session_message(session_id: str, message_id: int) -> bool:
    """Delete a specific message from a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND id = ?",
            (session_id, message_id)
        )
        return cursor.rowcount > 0


def delete_session_messages_after(session_id: str, message_id: int) -> int:
    """Delete all messages after a specific message ID (for rewind)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND id > ?",
            (session_id, message_id)
        )
        return cursor.rowcount


def search_sessions(
    query: str,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    admin_only: bool = False,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """
    Search sessions by title and message content.
    Returns sessions with matching snippets showing where the match was found.
    """
    if not query or not query.strip():
        return []

    search_term = f"%{query.strip()}%"

    with get_db() as conn:
        cursor = conn.cursor()

        # Build the base query - search both session titles and message content
        # Use UNION to combine title matches and content matches
        sql = """
        WITH title_matches AS (
            SELECT
                s.id,
                s.project_id,
                s.profile_id,
                s.api_user_id,
                s.title,
                s.status,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.turn_count,
                s.created_at,
                s.updated_at,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE s.title LIKE ? COLLATE NOCASE
            AND s.status != 'archived'
        ),
        content_matches AS (
            SELECT DISTINCT
                s.id,
                s.project_id,
                s.profile_id,
                s.api_user_id,
                s.title,
                s.status,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.turn_count,
                s.created_at,
                s.updated_at,
                'content' as match_type,
                (
                    SELECT SUBSTR(
                        sm2.content,
                        MAX(1, INSTR(LOWER(sm2.content), LOWER(?)) - 40),
                        120
                    )
                    FROM session_messages sm2
                    WHERE sm2.session_id = s.id
                    AND sm2.content LIKE ? COLLATE NOCASE
                    AND sm2.role IN ('user', 'assistant')
                    ORDER BY sm2.created_at DESC
                    LIMIT 1
                ) as match_snippet,
                (
                    SELECT sm3.created_at
                    FROM session_messages sm3
                    WHERE sm3.session_id = s.id
                    AND sm3.content LIKE ? COLLATE NOCASE
                    AND sm3.role IN ('user', 'assistant')
                    ORDER BY sm3.created_at DESC
                    LIMIT 1
                ) as match_time
            FROM sessions s
            INNER JOIN session_messages sm ON sm.session_id = s.id
            WHERE sm.content LIKE ? COLLATE NOCASE
            AND sm.role IN ('user', 'assistant')
            AND s.status != 'archived'
        ),
        all_matches AS (
            SELECT * FROM title_matches
            UNION ALL
            SELECT * FROM content_matches
        )
        SELECT
            id,
            project_id,
            profile_id,
            api_user_id,
            title,
            status,
            total_cost_usd,
            total_tokens_in,
            total_tokens_out,
            turn_count,
            created_at,
            updated_at,
            match_type,
            match_snippet,
            match_time
        FROM all_matches
        WHERE 1=1
        """

        # Base params for the search terms (used multiple times in query)
        params = [search_term, query.strip(), search_term, search_term, search_term]

        # Add filters
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if profile_id:
            sql += " AND profile_id = ?"
            params.append(profile_id)
        if admin_only:
            sql += " AND api_user_id IS NULL"
        elif api_user_id:
            sql += " AND api_user_id = ?"
            params.append(api_user_id)

        # Group by session to avoid duplicates, keeping the best match
        # Order by most recent match
        sql += """
        GROUP BY id
        ORDER BY
            CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
            updated_at DESC
        LIMIT ?
        """
        params.append(limit)

        cursor.execute(sql, params)
        results = rows_to_list(cursor.fetchall())

        # Clean up snippets - add ellipsis if truncated
        for result in results:
            if result.get('match_snippet') and result.get('match_type') == 'content':
                snippet = result['match_snippet']
                # Add ellipsis if we truncated
                if len(snippet) >= 118:
                    if not snippet.startswith(' '):
                        snippet = '...' + snippet
                    snippet = snippet + '...'
                result['match_snippet'] = snippet.strip()

        return results


def search_sessions_paginated(
    query: str,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    admin_only: bool = False,
    limit: int = 20,
    offset: int = 0
) -> tuple[List[Dict[str, Any]], int]:
    """
    Search sessions by title and message content with pagination.
    Returns sessions with matching snippets and total count.

    Returns:
        Tuple of (results list, total count)
    """
    if not query or not query.strip():
        return [], 0

    search_term = f"%{query.strip()}%"

    with get_db() as conn:
        cursor = conn.cursor()

        # Build the base query - search both session titles and message content
        base_cte = """
        WITH title_matches AS (
            SELECT
                s.id,
                s.project_id,
                s.profile_id,
                s.api_user_id,
                s.title,
                s.status,
                s.is_favorite,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.cache_creation_tokens,
                s.cache_read_tokens,
                s.context_tokens,
                s.turn_count,
                s.parent_session_id,
                s.fork_point_message_index,
                s.created_at,
                s.updated_at,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE s.title LIKE ? COLLATE NOCASE
            AND s.status != 'archived'
        ),
        content_matches AS (
            SELECT DISTINCT
                s.id,
                s.project_id,
                s.profile_id,
                s.api_user_id,
                s.title,
                s.status,
                s.is_favorite,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.cache_creation_tokens,
                s.cache_read_tokens,
                s.context_tokens,
                s.turn_count,
                s.parent_session_id,
                s.fork_point_message_index,
                s.created_at,
                s.updated_at,
                'content' as match_type,
                (
                    SELECT SUBSTR(
                        sm2.content,
                        MAX(1, INSTR(LOWER(sm2.content), LOWER(?)) - 40),
                        120
                    )
                    FROM session_messages sm2
                    WHERE sm2.session_id = s.id
                    AND sm2.content LIKE ? COLLATE NOCASE
                    AND sm2.role IN ('user', 'assistant')
                    ORDER BY sm2.created_at DESC
                    LIMIT 1
                ) as match_snippet,
                (
                    SELECT sm3.created_at
                    FROM session_messages sm3
                    WHERE sm3.session_id = s.id
                    AND sm3.content LIKE ? COLLATE NOCASE
                    AND sm3.role IN ('user', 'assistant')
                    ORDER BY sm3.created_at DESC
                    LIMIT 1
                ) as match_time
            FROM sessions s
            INNER JOIN session_messages sm ON sm.session_id = s.id
            WHERE sm.content LIKE ? COLLATE NOCASE
            AND sm.role IN ('user', 'assistant')
            AND s.status != 'archived'
        ),
        all_matches AS (
            SELECT * FROM title_matches
            UNION ALL
            SELECT * FROM content_matches
        ),
        grouped_matches AS (
            SELECT
                id,
                project_id,
                profile_id,
                api_user_id,
                title,
                status,
                is_favorite,
                total_cost_usd,
                total_tokens_in,
                total_tokens_out,
                cache_creation_tokens,
                cache_read_tokens,
                context_tokens,
                turn_count,
                parent_session_id,
                fork_point_message_index,
                created_at,
                updated_at,
                match_type,
                match_snippet,
                match_time
            FROM all_matches
            WHERE 1=1
        """

        # Base params for the search terms (used multiple times in query)
        base_params = [search_term, query.strip(), search_term, search_term, search_term]

        # Build filter conditions
        filter_sql = ""
        filter_params: List[Any] = []

        if project_id:
            filter_sql += " AND project_id = ?"
            filter_params.append(project_id)
        if profile_id:
            filter_sql += " AND profile_id = ?"
            filter_params.append(profile_id)
        if admin_only:
            filter_sql += " AND api_user_id IS NULL"
        elif api_user_id:
            filter_sql += " AND api_user_id = ?"
            filter_params.append(api_user_id)

        # Count query - count unique session IDs
        count_sql = base_cte + filter_sql + """
            GROUP BY id
        )
        SELECT COUNT(*) FROM grouped_matches
        """
        cursor.execute(count_sql, base_params + filter_params)
        total = cursor.fetchone()[0]

        # Data query with pagination
        data_sql = base_cte + filter_sql + """
            GROUP BY id
            ORDER BY
                CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT ? OFFSET ?
        )
        SELECT * FROM grouped_matches
        ORDER BY
            CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
            updated_at DESC
        """
        cursor.execute(data_sql, base_params + filter_params + [limit, offset])
        results = rows_to_list(cursor.fetchall())

        # Clean up snippets - add ellipsis if truncated
        for result in results:
            if result.get('match_snippet') and result.get('match_type') == 'content':
                snippet = result['match_snippet']
                # Add ellipsis if we truncated
                if len(snippet) >= 118:
                    if not snippet.startswith(' '):
                        snippet = '...' + snippet
                    snippet = snippet + '...'
                result['match_snippet'] = snippet.strip()

        return results, total


# ============================================================================
# Usage Log Operations
# ============================================================================

def log_usage(
    session_id: Optional[str],
    profile_id: Optional[str],
    model: Optional[str],
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    duration_ms: int
):
    """Log usage for tracking"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO usage_log (session_id, profile_id, model, tokens_in, tokens_out, cost_usd, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, model, tokens_in, tokens_out, cost_usd, duration_ms)
        )

    # Update in-memory usage cache (avoids DB re-read on next load)
    if session_id:
        from app.core.cost_utils import append_usage_entry
        append_usage_entry(session_id, cost_usd, duration_ms, tokens_in, tokens_out, model)


def get_last_session_usage(session_id: str) -> Optional[Dict[str, Any]]:
    """Get the most recent usage entry for a session.

    Returns cost and duration info for the last query turn in the session.
    This is used to populate metadata for the last assistant message when
    loading chat history (since JSONL files don't store cost/duration).

    Returns:
        Dict with cost_usd, duration_ms, tokens_in, tokens_out, model
        or None if no usage found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cost_usd, duration_ms, tokens_in, tokens_out, model
            FROM usage_log
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (session_id,))
        row = cursor.fetchone()
        if row:
            return {
                "total_cost_usd": row["cost_usd"],
                "duration_ms": row["duration_ms"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "model": row["model"]
            }
        return None


def get_all_session_usage(session_id: str) -> List[Dict[str, Any]]:
    """Get ALL usage entries for a session, ordered by turn.

    Returns list of usage entries, one per conversation turn.
    The Nth entry corresponds to the Nth user->assistant turn.
    Includes both per-turn cost and cumulative cost running total.

    Args:
        session_id: The session to get usage for

    Returns:
        List of dicts with: cost_usd, cumulative_cost_usd, duration_ms,
        tokens_in, tokens_out, model, turn_index
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cost_usd, duration_ms, tokens_in, tokens_out, model
            FROM usage_log
            WHERE session_id = ?
            ORDER BY id ASC
        """, (session_id,))
        rows = cursor.fetchall()

        result = []
        cumulative_cost = 0.0
        for turn_index, row in enumerate(rows):
            cost = row["cost_usd"] or 0.0
            cumulative_cost += cost
            result.append({
                "cost_usd": cost,
                "cumulative_cost_usd": cumulative_cost,
                "duration_ms": row["duration_ms"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "model": row["model"],
                "turn_index": turn_index
            })
        return result


def get_usage_stats() -> Dict[str, Any]:
    """Get aggregate usage statistics"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(*) as total_queries,
                COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd
            FROM usage_log
        """)
        row = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) as count FROM sessions")
        sessions_row = cursor.fetchone()

        return {
            "total_sessions": sessions_row["count"],
            "total_queries": row["total_queries"],
            "total_tokens_in": row["total_tokens_in"],
            "total_tokens_out": row["total_tokens_out"],
            "total_cost_usd": row["total_cost_usd"]
        }


# ============================================================================
# API User Operations
# ============================================================================

def _get_api_user_profile_ids(cursor, user_id: str) -> List[str]:
    """Get profile IDs for an API user from junction table"""
    cursor.execute(
        "SELECT profile_id FROM api_user_profiles WHERE api_user_id = ? ORDER BY created_at",
        (user_id,)
    )
    return [row[0] for row in cursor.fetchall()]


def _enrich_api_user_with_profiles(cursor, user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Add profile_ids to API user dict from junction table"""
    if not user:
        return None
    profile_ids = _get_api_user_profile_ids(cursor, user["id"])
    user["profile_ids"] = profile_ids if profile_ids else None
    return user


def create_api_user(
    user_id: str,
    name: str,
    api_key_hash: str,
    project_id: Optional[str] = None,
    profile_ids: Optional[List[str]] = None,
    web_login_allowed: bool = True,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new API user and optionally link profile IDs."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO api_users
               (id, name, api_key_hash, project_id, is_active, web_login_allowed, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, TRUE, ?, ?, ?, ?)""",
            (user_id, name, api_key_hash, project_id, web_login_allowed, description, now, now),
        )
        if profile_ids:
            for pid in profile_ids:
                cursor.execute(
                    "INSERT INTO api_user_profiles (api_user_id, profile_id, created_at) VALUES (?, ?, ?)",
                    (user_id, pid, now),
                )
        cursor.execute("SELECT * FROM api_users WHERE id = ?", (user_id,))
        user = row_to_dict(cursor.fetchone())
        return _enrich_api_user_with_profiles(cursor, user)


def delete_api_user(user_id: str) -> bool:
    """Delete an API user by ID. Cascade handles junction tables."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_user_profiles WHERE api_user_id = ?", (user_id,))
        cursor.execute("DELETE FROM api_users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


def get_api_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Get an API user by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_users WHERE id = ?", (user_id,))
        user = row_to_dict(cursor.fetchone())
        return _enrich_api_user_with_profiles(cursor, user)


def get_api_user_by_key_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    """Get an API user by API key hash"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_users WHERE api_key_hash = ? AND is_active = TRUE",
            (key_hash,)
        )
        user = row_to_dict(cursor.fetchone())
        return _enrich_api_user_with_profiles(cursor, user)


def update_api_user_last_used(user_id: str):
    """Update the last_used_at timestamp for an API user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_users SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id)
        )


def get_all_api_users(active_only: bool = False) -> List[Dict[str, Any]]:
    """Get all API users, optionally filtered to active-only."""
    with get_db() as conn:
        cursor = conn.cursor()
        if active_only:
            cursor.execute("SELECT * FROM api_users WHERE is_active = TRUE ORDER BY created_at DESC")
        else:
            cursor.execute("SELECT * FROM api_users ORDER BY created_at DESC")
        users = rows_to_list(cursor.fetchall())
        for user in users:
            profile_ids = _get_api_user_profile_ids(cursor, user["id"])
            user["profile_ids"] = profile_ids if profile_ids else None
        return users


def update_api_user(user_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update an API user. Only provided fields are changed.

    Supported kwargs: name, description, project_id, is_active,
    web_login_allowed, api_key_hash, username, password_hash, profile_ids.

    profile_ids replaces the full set in the junction table when provided.
    """
    existing = get_api_user(user_id)
    if not existing:
        return None

    profile_ids = kwargs.pop("profile_ids", None)

    now = datetime.now(timezone.utc).isoformat()
    updates = ["updated_at = ?"]
    values: list = [now]

    allowed = {
        "name", "description", "project_id", "is_active",
        "web_login_allowed", "api_key_hash", "username", "password_hash",
    }
    for key, val in kwargs.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            values.append(val)

    values.append(user_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE api_users SET {', '.join(updates)} WHERE id = ?",
            tuple(values),
        )

        if profile_ids is not None:
            cursor.execute("DELETE FROM api_user_profiles WHERE api_user_id = ?", (user_id,))
            for pid in profile_ids:
                cursor.execute(
                    "INSERT INTO api_user_profiles (api_user_id, profile_id, created_at) VALUES (?, ?, ?)",
                    (user_id, pid, now),
                )

        cursor.execute("SELECT * FROM api_users WHERE id = ?", (user_id,))
        user = row_to_dict(cursor.fetchone())
        return _enrich_api_user_with_profiles(cursor, user)


# ============================================================================
# Credential Policies Operations (admin controls which keys users must provide)
# ============================================================================

def get_all_credential_policies() -> List[Dict[str, Any]]:
    """Get all credential policies"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM credential_policies ORDER BY id")
        return rows_to_list(cursor.fetchall())


def get_credential_policy(policy_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific credential policy"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM credential_policies WHERE id = ?", (policy_id,))
        return row_to_dict(cursor.fetchone())


def update_credential_policy(policy_id: str, policy: str, description: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Update a credential policy.

    policy: 'admin_provided', 'user_provided', or 'optional'
    - admin_provided: All users use admin's key, users cannot set their own
    - user_provided: Each user must provide their own key
    - optional: Users can optionally provide their own key, falls back to admin's
    """
    valid_policies = ['admin_provided', 'user_provided', 'optional']
    if policy not in valid_policies:
        return None

    with get_db() as conn:
        cursor = conn.cursor()
        if description is not None:
            cursor.execute(
                "UPDATE credential_policies SET policy = ?, description = ?, updated_at = ? WHERE id = ?",
                (policy, description, datetime.now(timezone.utc).isoformat(), policy_id)
            )
        else:
            cursor.execute(
                "UPDATE credential_policies SET policy = ?, updated_at = ? WHERE id = ?",
                (policy, datetime.now(timezone.utc).isoformat(), policy_id)
            )
    return get_credential_policy(policy_id)


# ============================================================================
# User Credential Policy Overrides (per-user policy settings)
# ============================================================================

def get_user_credential_policy(api_user_id: str, credential_type: str) -> Optional[Dict[str, Any]]:
    """Get a user's policy override for a specific credential type"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_credential_policies WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return row_to_dict(cursor.fetchone())


def get_all_user_credential_policies(api_user_id: str) -> List[Dict[str, Any]]:
    """Get all policy overrides for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_credential_policies WHERE api_user_id = ? ORDER BY credential_type",
            (api_user_id,)
        )
        return rows_to_list(cursor.fetchall())


def set_user_credential_policy(api_user_id: str, credential_type: str, policy: str) -> Optional[Dict[str, Any]]:
    """
    Set or update a user's policy override for a credential type.

    policy: 'admin_provided', 'user_provided', or 'optional'
    """
    import uuid
    valid_policies = ['admin_provided', 'user_provided', 'optional']
    if policy not in valid_policies:
        return None

    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        # Try to update existing
        cursor.execute(
            """UPDATE user_credential_policies
               SET policy = ?, updated_at = ?
               WHERE api_user_id = ? AND credential_type = ?""",
            (policy, now, api_user_id, credential_type)
        )

        if cursor.rowcount == 0:
            # Insert new
            policy_id = str(uuid.uuid4())
            cursor.execute(
                """INSERT INTO user_credential_policies (id, api_user_id, credential_type, policy, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (policy_id, api_user_id, credential_type, policy, now, now)
            )

    return get_user_credential_policy(api_user_id, credential_type)


def delete_user_credential_policy(api_user_id: str, credential_type: str) -> bool:
    """Delete a user's policy override (revert to global policy)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_credential_policies WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return cursor.rowcount > 0


def get_effective_credential_policy(api_user_id: str, credential_type: str) -> Dict[str, Any]:
    """
    Get the effective policy for a user+credential.

    Policies are per-user only, no global fallback.
    If no policy exists (shouldn't happen for properly created users),
    defaults to 'user_provided'.

    Returns dict with:
    - policy: the effective policy value
    - source: 'user' or 'default'
    - credential_type: the credential type
    """
    user_policy = get_user_credential_policy(api_user_id, credential_type)
    if user_policy:
        return {
            "policy": user_policy["policy"],
            "source": "user",
            "credential_type": credential_type
        }

    # Default to user_provided if no policy exists
    # This shouldn't happen for properly created users
    return {
        "policy": "user_provided",
        "source": "default",
        "credential_type": credential_type
    }


# ============================================================================
# API User Credentials Operations (per-user encrypted API keys)
# ============================================================================

def get_user_credential(api_user_id: str, credential_type: str) -> Optional[Dict[str, Any]]:
    """Get a specific credential for a user (returns encrypted value)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_user_credentials WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return row_to_dict(cursor.fetchone())


def get_all_user_credentials(api_user_id: str) -> List[Dict[str, Any]]:
    """Get all credentials for a user (returns encrypted values)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_user_credentials WHERE api_user_id = ? ORDER BY credential_type",
            (api_user_id,)
        )
        return rows_to_list(cursor.fetchall())


def set_user_credential(api_user_id: str, credential_type: str, encrypted_value: str) -> Dict[str, Any]:
    """Set or update a credential for a user (value should be pre-encrypted)"""
    import uuid
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        # Try to update existing
        cursor.execute(
            """UPDATE api_user_credentials
               SET encrypted_value = ?, updated_at = ?
               WHERE api_user_id = ? AND credential_type = ?""",
            (encrypted_value, now, api_user_id, credential_type)
        )

        if cursor.rowcount == 0:
            # Insert new
            cred_id = str(uuid.uuid4())
            cursor.execute(
                """INSERT INTO api_user_credentials (id, api_user_id, credential_type, encrypted_value, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cred_id, api_user_id, credential_type, encrypted_value, now, now)
            )

    return get_user_credential(api_user_id, credential_type)


def delete_user_credential(api_user_id: str, credential_type: str) -> bool:
    """Delete a user's credential"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM api_user_credentials WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return cursor.rowcount > 0


def user_has_credential(api_user_id: str, credential_type: str) -> bool:
    """Check if a user has a specific credential set"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM api_user_credentials WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return cursor.fetchone() is not None


# ============================================================================
# Sync Log Operations (for cross-device synchronization)
# ============================================================================

def add_sync_log(
    session_id: str,
    event_type: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Add an entry to the sync log for polling fallback"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sync_log (session_id, event_type, entity_type, entity_id, data, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, event_type, entity_type, entity_id,
             json.dumps(data) if data else None, now)
        )
        return {
            "id": cursor.lastrowid,
            "session_id": session_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": data,
            "created_at": now
        }


def get_sync_logs(
    session_id: str,
    since_id: int = 0,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """Get sync log entries for a session since a given ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM sync_log
               WHERE session_id = ? AND id > ?
               ORDER BY id ASC
               LIMIT ?""",
            (session_id, since_id, limit)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("data"):
                row["data"] = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        return rows


def get_latest_sync_id(session_id: str) -> int:
    """Get the latest sync log ID for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(id) as max_id FROM sync_log WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        return row["max_id"] or 0


def update_message_content(
    message_id: int,
    content: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Update message content (used during streaming)"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()

        updates = ["content = ?", "updated_at = ?", "version = version + 1"]
        values = [content, now]

        if metadata is not None:
            updates.append("metadata = ?")
            values.append(json.dumps(metadata))

        values.append(message_id)

        cursor.execute(
            f"UPDATE session_messages SET {', '.join(updates)} WHERE id = ?",
            values
        )

        if cursor.rowcount > 0:
            cursor.execute("SELECT * FROM session_messages WHERE id = ?", (message_id,))
            row = row_to_dict(cursor.fetchone())
            if row and row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            return row

    return None


# ============================================================================
# Login Attempts & Rate Limiting Operations
# ============================================================================

def record_login_attempt(ip_address: str, username: Optional[str], success: bool):
    """Record a login attempt for rate limiting"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO login_attempts (ip_address, username, success, created_at)
               VALUES (?, ?, ?, ?)""",
            (ip_address, username, success, datetime.now(timezone.utc).isoformat())
        )


def get_failed_attempts_count(ip_address: str, window_minutes: int = 15) -> int:
    """Get the number of failed login attempts from an IP in the time window"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) as count FROM login_attempts
               WHERE ip_address = ? AND success = FALSE AND created_at > ?""",
            (ip_address, cutoff)
        )
        row = cursor.fetchone()
        return row["count"]


def get_failed_attempts_for_username(username: str, window_minutes: int = 15) -> int:
    """Get the number of failed login attempts for a username in the time window"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) as count FROM login_attempts
               WHERE username = ? AND success = FALSE AND created_at > ?""",
            (username, cutoff)
        )
        row = cursor.fetchone()
        return row["count"]


# ============================================================================
# Account Lockout Operations
# ============================================================================

def create_lockout(ip_address: Optional[str], username: Optional[str],
                   duration_minutes: int, reason: str):
    """Create a lockout for an IP or username"""
    from datetime import timedelta
    locked_until = (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO account_lockouts (ip_address, username, locked_until, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (ip_address, username, locked_until, reason, datetime.now(timezone.utc).isoformat())
        )


def is_ip_locked(ip_address: str) -> Optional[Dict[str, Any]]:
    """Check if an IP address is currently locked out"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM account_lockouts
               WHERE ip_address = ? AND locked_until > ?
               ORDER BY locked_until DESC LIMIT 1""",
            (ip_address, now)
        )
        return row_to_dict(cursor.fetchone())


def is_username_locked(username: str) -> Optional[Dict[str, Any]]:
    """Check if a username is currently locked out"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM account_lockouts
               WHERE username = ? AND locked_until > ?
               ORDER BY locked_until DESC LIMIT 1""",
            (username, now)
        )
        return row_to_dict(cursor.fetchone())


# ============================================================================
# API Key Session Operations
# ============================================================================

def get_api_key_session(token: str) -> Optional[Dict[str, Any]]:
    """Get an API key session by token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT aks.*, au.name as user_name, au.project_id, au.is_active
               FROM api_key_sessions aks
               JOIN api_users au ON aks.api_user_id = au.id
               WHERE aks.token = ? AND aks.expires_at > ? AND au.is_active = TRUE""",
            (token, datetime.now(timezone.utc).isoformat())
        )
        session = row_to_dict(cursor.fetchone())
        if session:
            # Get profile_ids from junction table
            profile_ids = _get_api_user_profile_ids(cursor, session["api_user_id"])
            session["profile_ids"] = profile_ids if profile_ids else None
        return session


# ============================================================================
# User Preferences Operations
# ============================================================================

def get_user_preference(user_type: str, user_id: str, key: str) -> Optional[Dict[str, Any]]:
    """Get a user preference by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_preferences WHERE user_type = ? AND user_id = ? AND key = ?",
            (user_type, user_id, key)
        )
        row = cursor.fetchone()
        if row:
            result = row_to_dict(row)
            # Parse JSON value
            if result and result.get("value"):
                result["value"] = json.loads(result["value"])
            return result
        return None


def set_user_preference(user_type: str, user_id: str, key: str, value: Any) -> Dict[str, Any]:
    """Set a user preference (upsert)"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value)

        cursor.execute(
            """INSERT INTO user_preferences (user_type, user_id, key, value, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_type, user_id, key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (user_type, user_id, key, value_json, now)
        )

        return {
            "user_type": user_type,
            "user_id": user_id,
            "key": key,
            "value": value,
            "updated_at": now
        }


def delete_user_preference(user_type: str, user_id: str, key: str) -> bool:
    """Delete a user preference"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_preferences WHERE user_type = ? AND user_id = ? AND key = ?",
            (user_type, user_id, key)
        )
        return cursor.rowcount > 0


def get_all_user_preferences(user_type: str, user_id: str) -> List[Dict[str, Any]]:
    """Get all preferences for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_preferences WHERE user_type = ? AND user_id = ?",
            (user_type, user_id)
        )
        rows = rows_to_list(cursor.fetchall())
        # Parse JSON values
        for row in rows:
            if row.get("value"):
                row["value"] = json.loads(row["value"])
        return rows


# ============================================================================
# Subagent Operations
# ============================================================================

def get_subagent(subagent_id: str) -> Optional[Dict[str, Any]]:
    """Get a subagent by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM subagents WHERE id = ?", (subagent_id,))
        row = row_to_dict(cursor.fetchone())
        if row and row.get("tools"):
            row["tools"] = json.loads(row["tools"]) if isinstance(row["tools"], str) else row["tools"]
        return row


def get_all_subagents() -> List[Dict[str, Any]]:
    """Get all subagents"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM subagents ORDER BY name ASC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("tools"):
                row["tools"] = json.loads(row["tools"]) if isinstance(row["tools"], str) else row["tools"]
        return rows


def create_subagent(
    subagent_id: str,
    name: str,
    description: str,
    prompt: str,
    tools: Optional[List[str]] = None,
    model: Optional[str] = None,
    is_builtin: bool = False,
    store_defaults: bool = False
) -> Dict[str, Any]:
    """
    Create a new subagent.

    Args:
        subagent_id: Unique identifier for the subagent
        name: Display name
        description: When to use this subagent
        prompt: System prompt for the subagent
        tools: List of allowed tools (None = inherit all)
        model: Model override (None = inherit from profile)
        is_builtin: Whether this is a built-in subagent (cannot be deleted)
        store_defaults: If True, also store current values as defaults (for built-in subagents)
    """
    now = datetime.now(timezone.utc).isoformat()
    tools_json = json.dumps(tools) if tools else None

    with get_db() as conn:
        cursor = conn.cursor()
        if store_defaults:
            # For built-in subagents, store both current values and defaults
            cursor.execute(
                """INSERT INTO subagents (
                    id, name, description, prompt, tools, model, is_builtin,
                    default_name, default_description, default_prompt, default_tools, default_model,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (subagent_id, name, description, prompt, tools_json, model, is_builtin,
                 name, description, prompt, tools_json, model,
                 now, now)
            )
        else:
            cursor.execute(
                """INSERT INTO subagents (id, name, description, prompt, tools, model, is_builtin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (subagent_id, name, description, prompt, tools_json, model, is_builtin, now, now)
            )
    return get_subagent(subagent_id)


def update_subagent(
    subagent_id: str,
    name: Optional[str] = ...,  # type: ignore[assignment]
    description: Optional[str] = ...,  # type: ignore[assignment]
    prompt: Optional[str] = ...,  # type: ignore[assignment]
    tools: Optional[List[str]] = ...,  # type: ignore[assignment]
    model: Optional[str] = ...,  # type: ignore[assignment]
    skills: Optional[List[str]] = ...,  # type: ignore[assignment]
    memory: Optional[str] = ...,  # type: ignore[assignment]
    mcp_servers: Optional[List[Any]] = ...,  # type: ignore[assignment]
) -> Optional[Dict[str, Any]]:
    """
    Update a subagent.

    Uses Ellipsis (...) as default to distinguish between "not provided" and "explicitly set to None".
    This allows clearing fields by passing None explicitly.
    """
    existing = get_subagent(subagent_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not ...:
        updates.append("name = ?")
        values.append(name)
    if description is not ...:
        updates.append("description = ?")
        values.append(description)
    if prompt is not ...:
        updates.append("prompt = ?")
        values.append(prompt)
    if tools is not ...:
        updates.append("tools = ?")
        values.append(json.dumps(tools) if tools else None)
    if model is not ...:
        updates.append("model = ?")
        values.append(model)
    if skills is not ...:
        updates.append("skills = ?")
        values.append(json.dumps(skills) if skills else None)
    if memory is not ...:
        updates.append("memory = ?")
        values.append(memory)
    if mcp_servers is not ...:
        updates.append("mcp_servers = ?")
        values.append(json.dumps(mcp_servers) if mcp_servers else None)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(subagent_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE subagents SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_subagent(subagent_id)


def delete_subagent(subagent_id: str) -> bool:
    """Delete a subagent"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM subagents WHERE id = ?", (subagent_id,))
        return cursor.rowcount > 0


def set_subagent_builtin(subagent_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a subagent"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subagents SET is_builtin = ?, updated_at = ? WHERE id = ?",
            (is_builtin, datetime.now(timezone.utc).isoformat(), subagent_id)
        )
        return cursor.rowcount > 0


def revert_subagent_to_defaults(subagent_id: str) -> Optional[Dict[str, Any]]:
    """
    Revert a built-in subagent to its original default values.

    Only works for built-in subagents that have stored defaults.
    Returns the reverted subagent or None if not found/not built-in.
    """
    subagent = get_subagent(subagent_id)
    if not subagent:
        return None

    if not subagent.get("is_builtin"):
        return None

    # Check if defaults exist
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT default_name, default_description, default_prompt, default_tools, default_model
               FROM subagents WHERE id = ?""",
            (subagent_id,)
        )
        row = cursor.fetchone()
        if not row or not row["default_prompt"]:
            # No defaults stored
            return None

        # Revert to defaults
        cursor.execute(
            """UPDATE subagents SET
                name = default_name,
                description = default_description,
                prompt = default_prompt,
                tools = default_tools,
                model = default_model,
                updated_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), subagent_id)
        )

    return get_subagent(subagent_id)


def has_subagent_been_modified(subagent_id: str) -> bool:
    """
    Check if a built-in subagent has been modified from its defaults.

    Returns True if any field differs from the stored default.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT
                name != default_name OR
                description != default_description OR
                prompt != default_prompt OR
                COALESCE(tools, '') != COALESCE(default_tools, '') OR
                COALESCE(model, '') != COALESCE(default_model, '')
               AS is_modified
               FROM subagents WHERE id = ? AND is_builtin = 1""",
            (subagent_id,)
        )
        row = cursor.fetchone()
        return bool(row and row["is_modified"])


# ============================================================================
# Permission Rules Operations
# ============================================================================

def add_permission_rule(
    profile_id: str,
    tool_name: str,
    tool_pattern: Optional[str],
    decision: str
) -> Dict[str, Any]:
    """Add a permission rule for a profile"""
    import uuid
    rule_id = f"rule-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO permission_rules (id, profile_id, tool_name, tool_pattern, decision, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rule_id, profile_id, tool_name, tool_pattern, decision, now)
        )
    return get_permission_rule(rule_id)


def get_permission_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    """Get a permission rule by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM permission_rules WHERE id = ?", (rule_id,))
        return row_to_dict(cursor.fetchone())


def get_permission_rules(
    profile_id: Optional[str] = None,
    tool_name: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get permission rules with optional filters"""
    query = "SELECT * FROM permission_rules WHERE 1=1"
    params = []

    if profile_id:
        query += " AND profile_id = ?"
        params.append(profile_id)
    if tool_name:
        query += " AND tool_name = ?"
        params.append(tool_name)

    query += " ORDER BY created_at DESC"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def delete_permission_rule(rule_id: str) -> bool:
    """Delete a permission rule"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM permission_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0


def delete_profile_permission_rules(profile_id: str) -> int:
    """Delete all permission rules for a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM permission_rules WHERE profile_id = ?", (profile_id,))
        return cursor.rowcount


# ============================================================================
# System Settings Operations
# ============================================================================

def get_system_setting(key: str) -> Optional[str]:
    """Get a system setting by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None


def set_system_setting(key: str, value: str) -> None:
    """Set a system setting (upsert)"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (key, value, now)
        )


def delete_system_setting(key: str) -> bool:
    """Delete a system setting"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = ?", (key,))
        return cursor.rowcount > 0


def get_all_system_settings() -> Dict[str, str]:
    """Get all system settings as a dictionary"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings")
        rows = cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}


# ============================================================================
# Tag Operations
# ============================================================================

def get_tag(tag_id: str) -> Optional[Dict[str, Any]]:
    """Get a tag by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tags WHERE id = ?", (tag_id,))
        return row_to_dict(cursor.fetchone())


def get_all_tags() -> List[Dict[str, Any]]:
    """Get all tags ordered by name"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tags ORDER BY name ASC")
        return rows_to_list(cursor.fetchall())


def create_tag(
    tag_id: str,
    name: str,
    color: str = "#6366f1"
) -> Dict[str, Any]:
    """Create a new tag"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO tags (id, name, color, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tag_id, name, color, now, now)
        )
    return get_tag(tag_id)


def update_tag(
    tag_id: str,
    name: Optional[str] = None,
    color: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a tag"""
    existing = get_tag(tag_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if color is not None:
        updates.append("color = ?")
        values.append(color)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(tag_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE tags SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_tag(tag_id)


def delete_tag(tag_id: str) -> bool:
    """Delete a tag (session_tags will cascade delete)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return cursor.rowcount > 0


# ============================================================================
# Session Tag Operations
# ============================================================================

def get_session_tags(session_id: str) -> List[Dict[str, Any]]:
    """Get all tags for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT t.* FROM tags t
               INNER JOIN session_tags st ON t.id = st.tag_id
               WHERE st.session_id = ?
               ORDER BY t.name ASC""",
            (session_id,)
        )
        return rows_to_list(cursor.fetchall())


def add_session_tag(session_id: str, tag_id: str) -> bool:
    """Add a tag to a session"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT OR IGNORE INTO session_tags (session_id, tag_id, created_at)
                   VALUES (?, ?, ?)""",
                (session_id, tag_id, now)
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to add tag {tag_id} to session {session_id}: {e}")
        return False


def remove_session_tag(session_id: str, tag_id: str) -> bool:
    """Remove a tag from a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM session_tags WHERE session_id = ? AND tag_id = ?",
            (session_id, tag_id)
        )
        return cursor.rowcount > 0


def set_session_tags(session_id: str, tag_ids: List[str]) -> List[Dict[str, Any]]:
    """Set all tags for a session (replaces existing)"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        # Remove existing tags
        cursor.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
        # Add new tags
        for tag_id in tag_ids:
            cursor.execute(
                """INSERT OR IGNORE INTO session_tags (session_id, tag_id, created_at)
                   VALUES (?, ?, ?)""",
                (session_id, tag_id, now)
            )
    return get_session_tags(session_id)


def get_sessions_by_tag(tag_id: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get all sessions with a specific tag"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT s.* FROM sessions s
               INNER JOIN session_tags st ON s.id = st.session_id
               WHERE st.tag_id = ?
               ORDER BY s.updated_at DESC
               LIMIT ? OFFSET ?""",
            (tag_id, limit, offset)
        )
        return rows_to_list(cursor.fetchall())


# ============================================================================
# Analytics Operations
# ============================================================================

def get_analytics_usage_stats(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    api_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get aggregate usage statistics for a date range.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        Dictionary with total_tokens_in, total_tokens_out, total_cost_usd, session_count, query_count
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(ul.created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(ul.created_at) <= ?"
            params.append(end_date)

        # Add user filter if specified
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = ?"
            params.append(api_user_id)

        # Get usage log aggregates (join with sessions for user filtering)
        cursor.execute(f"""
            SELECT
                COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                COUNT(*) as query_count
            FROM usage_log ul
            LEFT JOIN sessions s ON ul.session_id = s.id
            WHERE 1=1 {date_filter} {user_filter}
        """, params)
        usage_row = cursor.fetchone()

        # Get session count for the same period
        cursor.execute(f"""
            SELECT COUNT(DISTINCT ul.session_id) as session_count
            FROM usage_log ul
            LEFT JOIN sessions s ON ul.session_id = s.id
            WHERE ul.session_id IS NOT NULL {date_filter} {user_filter}
        """, params)
        session_row = cursor.fetchone()

        return {
            "total_tokens_in": usage_row["total_tokens_in"],
            "total_tokens_out": usage_row["total_tokens_out"],
            "total_cost_usd": round(usage_row["total_cost_usd"], 6),
            "query_count": usage_row["query_count"],
            "session_count": session_row["session_count"]
        }


def get_analytics_cost_breakdown(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    group_by: str = "profile",
    api_user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get cost breakdown grouped by profile, user, or date.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        group_by: Grouping - 'profile', 'user', or 'date'
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        List of dictionaries with key, name, total_cost_usd, total_tokens_in, total_tokens_out, query_count
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(ul.created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(ul.created_at) <= ?"
            params.append(end_date)

        # Add user filter if specified
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = ?"
            params.append(api_user_id)

        if group_by == "profile":
            cursor.execute(f"""
                SELECT
                    COALESCE(ul.profile_id, 'unknown') as key,
                    p.name as name,
                    COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                    COUNT(*) as query_count
                FROM usage_log ul
                LEFT JOIN profiles p ON ul.profile_id = p.id
                LEFT JOIN sessions s ON ul.session_id = s.id
                WHERE 1=1 {date_filter} {user_filter}
                GROUP BY ul.profile_id, p.name
                ORDER BY total_cost_usd DESC
            """, params)

        elif group_by == "user":
            # Join with sessions to get api_user_id, then with api_users for name
            cursor.execute(f"""
                SELECT
                    COALESCE(s.api_user_id, 'admin') as key,
                    CASE
                        WHEN s.api_user_id IS NULL THEN 'Admin'
                        ELSE COALESCE(au.name, 'Unknown User')
                    END as name,
                    COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                    COUNT(*) as query_count
                FROM usage_log ul
                LEFT JOIN sessions s ON ul.session_id = s.id
                LEFT JOIN api_users au ON s.api_user_id = au.id
                WHERE 1=1 {date_filter} {user_filter}
                GROUP BY s.api_user_id, au.name
                ORDER BY total_cost_usd DESC
            """, params)

        elif group_by == "date":
            cursor.execute(f"""
                SELECT
                    DATE(ul.created_at) as key,
                    DATE(ul.created_at) as name,
                    COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                    COUNT(*) as query_count
                FROM usage_log ul
                LEFT JOIN sessions s ON ul.session_id = s.id
                WHERE 1=1 {date_filter} {user_filter}
                GROUP BY DATE(ul.created_at)
                ORDER BY DATE(ul.created_at) DESC
            """, params)

        else:
            return []

        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "key": str(row["key"]) if row["key"] else "unknown",
                "name": row["name"],
                "total_tokens_in": row["total_tokens_in"],
                "total_tokens_out": row["total_tokens_out"],
                "total_cost_usd": round(row["total_cost_usd"], 6),
                "query_count": row["query_count"]
            })
        return result


def get_analytics_usage_trends(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "day",
    api_user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get usage trends over time.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        interval: Time interval - 'day', 'week', or 'month'
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        List of dictionaries with date, tokens_in, tokens_out, cost_usd, query_count
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build filter clauses
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(u.created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(u.created_at) <= ?"
            params.append(end_date)

        # User filter for API users (join with sessions to get api_user_id)
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = ?"
            params.append(api_user_id)

        # Choose date grouping based on interval
        if interval == "week":
            # SQLite strftime: %Y-W%W gives year and week number
            date_expr = "strftime('%Y-W%W', u.created_at)"
        elif interval == "month":
            date_expr = "strftime('%Y-%m', u.created_at)"
        else:  # day
            date_expr = "DATE(u.created_at)"

        cursor.execute(f"""
            SELECT
                {date_expr} as date,
                COALESCE(SUM(u.tokens_in), 0) as tokens_in,
                COALESCE(SUM(u.tokens_out), 0) as tokens_out,
                COALESCE(SUM(u.cost_usd), 0) as cost_usd,
                COUNT(*) as query_count
            FROM usage_log u
            JOIN sessions s ON u.session_id = s.id
            WHERE 1=1 {date_filter} {user_filter}
            GROUP BY {date_expr}
            ORDER BY date ASC
        """, params)

        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "date": row["date"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "cost_usd": round(row["cost_usd"], 6),
                "query_count": row["query_count"]
            })
        return result


def get_analytics_top_sessions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 10,
    api_user_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get top sessions by cost.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        limit: Maximum number of sessions to return
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        List of dictionaries with session_id, title, profile_id, profile_name, total_cost_usd, etc.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build filter clauses
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(s.created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(s.created_at) <= ?"
            params.append(end_date)

        # User filter for API users
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = ?"
            params.append(api_user_id)

        params.append(limit)

        cursor.execute(f"""
            SELECT
                s.id as session_id,
                s.title,
                s.profile_id,
                p.name as profile_name,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.created_at
            FROM sessions s
            LEFT JOIN profiles p ON s.profile_id = p.id
            WHERE s.total_cost_usd > 0 {date_filter} {user_filter}
            ORDER BY s.total_cost_usd DESC
            LIMIT ?
        """, params)

        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "profile_id": row["profile_id"],
                "profile_name": row["profile_name"],
                "total_cost_usd": round(row["total_cost_usd"], 6) if row["total_cost_usd"] else 0,
                "total_tokens_in": row["total_tokens_in"] or 0,
                "total_tokens_out": row["total_tokens_out"] or 0,
                "created_at": row["created_at"]
            })
        return result


# ============================================================================
# Template Operations
# ============================================================================

def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    """Get a template by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM templates WHERE id = ?", (template_id,))
        return row_to_dict(cursor.fetchone())


def get_all_templates(
    profile_id: Optional[str] = None,
    category: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get all templates with optional filters"""
    query = "SELECT * FROM templates WHERE 1=1"
    params = []

    if profile_id:
        # Return templates for this profile OR templates with no profile (global)
        query += " AND (profile_id = ? OR profile_id IS NULL)"
        params.append(profile_id)

    if category:
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY is_builtin DESC, category ASC, name ASC"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def create_template(
    template_id: str,
    name: str,
    prompt: str,
    description: Optional[str] = None,
    profile_id: Optional[str] = None,
    icon: Optional[str] = None,
    category: Optional[str] = None,
    is_builtin: bool = False
) -> Dict[str, Any]:
    """Create a new template"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO templates (id, name, description, prompt, profile_id, icon, category, is_builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (template_id, name, description, prompt, profile_id, icon, category, is_builtin, now, now)
        )
    return get_template(template_id)


def update_template(
    template_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    prompt: Optional[str] = None,
    profile_id: Optional[str] = None,
    icon: Optional[str] = None,
    category: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a template"""
    existing = get_template(template_id)
    if not existing:
        return None

    # Don't allow updating builtin templates
    if existing.get("is_builtin"):
        return None

    updates = []
    values = []

    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if description is not None:
        updates.append("description = ?")
        values.append(description if description else None)
    if prompt is not None:
        updates.append("prompt = ?")
        values.append(prompt)
    if profile_id is not None:
        updates.append("profile_id = ?")
        values.append(profile_id if profile_id else None)
    if icon is not None:
        updates.append("icon = ?")
        values.append(icon if icon else None)
    if category is not None:
        updates.append("category = ?")
        values.append(category if category else None)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(template_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE templates SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_template(template_id)


def delete_template(template_id: str) -> bool:
    """Delete a template"""
    existing = get_template(template_id)
    if not existing:
        return False

    # Don't allow deleting builtin templates
    if existing.get("is_builtin"):
        return False

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        return cursor.rowcount > 0


def get_template_categories() -> List[str]:
    """Get all unique template categories"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT category FROM templates WHERE category IS NOT NULL ORDER BY category ASC"
        )
        return [row["category"] for row in cursor.fetchall()]


def set_template_builtin(template_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a template"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE templates SET is_builtin = ?, updated_at = ? WHERE id = ?",
            (is_builtin, datetime.now(timezone.utc).isoformat(), template_id)
        )
        return cursor.rowcount > 0


# ============================================================================
# Webhook Operations
# ============================================================================

def get_all_webhooks() -> List[Dict[str, Any]]:
    """Get all webhooks"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhooks ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("events"):
                row["events"] = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        return rows


def get_active_webhooks() -> List[Dict[str, Any]]:
    """Get all active webhooks"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhooks WHERE is_active = TRUE ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("events"):
                row["events"] = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        return rows


def get_webhook(webhook_id: str) -> Optional[Dict[str, Any]]:
    """Get a webhook by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,))
        row = row_to_dict(cursor.fetchone())
        if row and row.get("events"):
            row["events"] = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        return row


def create_webhook(
    webhook_id: str,
    url: str,
    events: List[str],
    secret: Optional[str] = None,
    is_active: bool = True
) -> Optional[Dict[str, Any]]:
    """Create a new webhook"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO webhooks (id, url, secret, events, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (webhook_id, url, secret, json.dumps(events), is_active, now)
        )
    return get_webhook(webhook_id)


def update_webhook(
    webhook_id: str,
    url: Optional[str] = None,
    events: Optional[List[str]] = None,
    secret: Optional[str] = None,
    is_active: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Update a webhook"""
    existing = get_webhook(webhook_id)
    if not existing:
        return None

    updates = []
    values = []

    if url is not None:
        updates.append("url = ?")
        values.append(url)
    if events is not None:
        updates.append("events = ?")
        values.append(json.dumps(events))
    if secret is not None:
        updates.append("secret = ?")
        values.append(secret if secret else None)
    if is_active is not None:
        updates.append("is_active = ?")
        values.append(is_active)

    if updates:
        values.append(webhook_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE webhooks SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_webhook(webhook_id)


def delete_webhook(webhook_id: str) -> bool:
    """Delete a webhook"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        return cursor.rowcount > 0


def update_webhook_triggered(webhook_id: str, success: bool) -> None:
    """Update the last_triggered_at timestamp and failure_count for a webhook"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            # Reset failure count on success
            cursor.execute(
                "UPDATE webhooks SET last_triggered_at = ?, failure_count = 0 WHERE id = ?",
                (now, webhook_id)
            )
        else:
            # Increment failure count
            cursor.execute(
                "UPDATE webhooks SET last_triggered_at = ?, failure_count = failure_count + 1 WHERE id = ?",
                (now, webhook_id)
            )


def get_webhooks_for_event(event_type: str) -> List[Dict[str, Any]]:
    """Get all active webhooks subscribed to a specific event type"""
    webhooks = get_active_webhooks()
    return [w for w in webhooks if event_type in w.get("events", [])]


# ============================================================================
# Connector Operations
# ============================================================================

def _parse_connector_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse JSON fields in a connector row."""
    if not row:
        return None
    if row.get("events") and isinstance(row["events"], str):
        row["events"] = json.loads(row["events"])
    if row.get("config") and isinstance(row["config"], str):
        row["config"] = json.loads(row["config"])
    return row


def create_connector(
    connector_id: str,
    connector_type: str,
    name: str,
    config: Dict[str, Any],
    events: List[str],
    api_user_id: Optional[str] = None,
    is_active: bool = False,
) -> Optional[Dict[str, Any]]:
    """Create a new connector."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connectors (id, type, name, api_user_id, config, events, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (connector_id, connector_type, name, api_user_id,
             json.dumps(config), json.dumps(events), is_active, now, now)
        )
    return get_connector(connector_id)


def get_connector(connector_id: str) -> Optional[Dict[str, Any]]:
    """Get a connector by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors WHERE id = ?", (connector_id,))
        return _parse_connector_row(row_to_dict(cursor.fetchone()))


def get_all_connectors() -> List[Dict[str, Any]]:
    """Get all connectors."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_connector_row(r) for r in rows]


def get_active_connectors() -> List[Dict[str, Any]]:
    """Get all active connectors."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors WHERE is_active = TRUE ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_connector_row(r) for r in rows]


def get_connectors_for_event(event_type: str) -> List[Dict[str, Any]]:
    """Get all active connectors subscribed to a specific event type."""
    connectors = get_active_connectors()
    return [c for c in connectors if event_type in c.get("events", [])]


def update_connector(connector_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update a connector. Pass keyword arguments for fields to update."""
    existing = get_connector(connector_id)
    if not existing:
        return None

    updates = []
    values = []

    for key in ("name", "is_active", "status", "status_message", "last_connected_at",
                "last_error", "last_event_at", "api_user_id"):
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(kwargs[key])

    if "config" in kwargs:
        updates.append("config = ?")
        values.append(json.dumps(kwargs["config"]))
    if "events" in kwargs:
        updates.append("events = ?")
        values.append(json.dumps(kwargs["events"]))

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(connector_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE connectors SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_connector(connector_id)


def update_connector_status(
    connector_id: str,
    status: str,
    status_message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update the runtime status of a connector."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if status == "connected":
            cursor.execute(
                """UPDATE connectors
                   SET status = ?, status_message = ?, last_error = NULL,
                       last_connected_at = ?, failure_count = 0, updated_at = ?
                   WHERE id = ?""",
                (status, status_message, now, now, connector_id)
            )
        elif status == "error":
            cursor.execute(
                """UPDATE connectors
                   SET status = ?, status_message = ?, last_error = ?,
                       failure_count = failure_count + 1, updated_at = ?
                   WHERE id = ?""",
                (status, status_message, error, now, connector_id)
            )
        else:
            cursor.execute(
                """UPDATE connectors
                   SET status = ?, status_message = ?, updated_at = ?
                   WHERE id = ?""",
                (status, status_message, now, connector_id)
            )


def delete_connector(connector_id: str) -> bool:
    """Delete a connector and all related data (cascades)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM connectors WHERE id = ?", (connector_id,))
        return cursor.rowcount > 0


def update_connector_event_dispatched(connector_id: str, success: bool) -> None:
    """Update last_event_at and failure_count after event dispatch."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            cursor.execute(
                "UPDATE connectors SET last_event_at = ?, failure_count = 0, updated_at = ? WHERE id = ?",
                (now, now, connector_id)
            )
        else:
            cursor.execute(
                "UPDATE connectors SET last_event_at = ?, failure_count = failure_count + 1, updated_at = ? WHERE id = ?",
                (now, now, connector_id)
            )


# ---- Connector Event Log ----

def create_connector_event_log(
    log_id: str,
    connector_id: str,
    event_type: str,
    direction: str = "outbound",
    payload: Optional[Dict[str, Any]] = None,
    status: str = "pending",
    error: Optional[str] = None,
) -> None:
    """Create a connector event log entry."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connector_event_log (id, connector_id, event_type, direction, payload, status, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (log_id, connector_id, event_type, direction,
             json.dumps(payload) if payload else None, status, error, now)
        )


def get_connector_event_log(connector_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent event log entries for a connector."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_event_log WHERE connector_id = ? ORDER BY created_at DESC LIMIT ?",
            (connector_id, limit)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("payload") and isinstance(row["payload"], str):
                try:
                    row["payload"] = json.loads(row["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return rows


# ---- Connector Sessions ----

def create_connector_session(
    session_id: str,
    connector_id: str,
    platform_chat_id: str,
    platform_user_id: Optional[str] = None,
    platform_user_name: Optional[str] = None,
    ai_session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create a new connector session mapping."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO connector_sessions
               (id, connector_id, platform_chat_id, platform_user_id, platform_user_name, session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, connector_id, platform_chat_id, platform_user_id,
             platform_user_name, ai_session_id, now, now)
        )
    return get_connector_session_by_id(session_id)


def get_connector_session(connector_id: str, platform_chat_id: str) -> Optional[Dict[str, Any]]:
    """Get a connector session by connector ID and platform chat ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_sessions WHERE connector_id = ? AND platform_chat_id = ?",
            (connector_id, platform_chat_id)
        )
        return row_to_dict(cursor.fetchone())


def get_connector_session_by_id(session_id: str) -> Optional[Dict[str, Any]]:
    """Get a connector session by its ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connector_sessions WHERE id = ?", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_connector_sessions_for_ai_session(ai_session_id: str) -> List[Dict[str, Any]]:
    """Get all connector sessions linked to a given AI session."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_sessions WHERE session_id = ? AND is_active = TRUE",
            (ai_session_id,)
        )
        return rows_to_list(cursor.fetchall())


def update_connector_session_ai_session(
    session_id: str, ai_session_id: Optional[str]
) -> None:
    """Update the AI session linked to a connector session."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE connector_sessions SET session_id = ?, updated_at = ? WHERE id = ?",
            (ai_session_id, now, session_id)
        )


# ============================================================================
# Rate Limit Operations
# ============================================================================

def get_rate_limit(rate_limit_id: str) -> Optional[Dict[str, Any]]:
    """Get a rate limit configuration by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rate_limits WHERE id = ?", (rate_limit_id,))
        return row_to_dict(cursor.fetchone())


def get_rate_limit_for_user(user_id: Optional[str], api_key_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Get the most specific rate limit configuration for a user.
    Priority: api_key_id specific > user_id specific > default (null/null)
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Try API key specific first
        if api_key_id:
            cursor.execute("SELECT * FROM rate_limits WHERE api_key_id = ?", (api_key_id,))
            result = cursor.fetchone()
            if result:
                return row_to_dict(result)

        # Try user specific
        if user_id:
            cursor.execute("SELECT * FROM rate_limits WHERE user_id = ? AND api_key_id IS NULL", (user_id,))
            result = cursor.fetchone()
            if result:
                return row_to_dict(result)

        # Fall back to default (user_id and api_key_id both NULL)
        cursor.execute("SELECT * FROM rate_limits WHERE user_id IS NULL AND api_key_id IS NULL")
        result = cursor.fetchone()
        return row_to_dict(result) if result else None


def get_all_rate_limits() -> List[Dict[str, Any]]:
    """Get all rate limit configurations"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rate_limits ORDER BY priority DESC, created_at ASC")
        return rows_to_list(cursor.fetchall())


def create_rate_limit(
    rate_limit_id: str,
    user_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    requests_per_minute: int = 20,
    requests_per_hour: int = 200,
    requests_per_day: int = 1000,
    concurrent_requests: int = 3,
    priority: int = 0,
    is_unlimited: bool = False
) -> Dict[str, Any]:
    """Create a new rate limit configuration"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO rate_limits
               (id, user_id, api_key_id, requests_per_minute, requests_per_hour,
                requests_per_day, concurrent_requests, priority, is_unlimited, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rate_limit_id, user_id, api_key_id, requests_per_minute, requests_per_hour,
             requests_per_day, concurrent_requests, priority, is_unlimited, now, now)
        )
    return get_rate_limit(rate_limit_id)


def update_rate_limit(
    rate_limit_id: str,
    requests_per_minute: Optional[int] = None,
    requests_per_hour: Optional[int] = None,
    requests_per_day: Optional[int] = None,
    concurrent_requests: Optional[int] = None,
    priority: Optional[int] = None,
    is_unlimited: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Update a rate limit configuration"""
    existing = get_rate_limit(rate_limit_id)
    if not existing:
        return None

    updates = ["updated_at = ?"]
    values = [datetime.now(timezone.utc).isoformat()]

    if requests_per_minute is not None:
        updates.append("requests_per_minute = ?")
        values.append(requests_per_minute)
    if requests_per_hour is not None:
        updates.append("requests_per_hour = ?")
        values.append(requests_per_hour)
    if requests_per_day is not None:
        updates.append("requests_per_day = ?")
        values.append(requests_per_day)
    if concurrent_requests is not None:
        updates.append("concurrent_requests = ?")
        values.append(concurrent_requests)
    if priority is not None:
        updates.append("priority = ?")
        values.append(priority)
    if is_unlimited is not None:
        updates.append("is_unlimited = ?")
        values.append(is_unlimited)

    values.append(rate_limit_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE rate_limits SET {', '.join(updates)} WHERE id = ?",
            values
        )

    return get_rate_limit(rate_limit_id)


def delete_rate_limit(rate_limit_id: str) -> bool:
    """Delete a rate limit configuration"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rate_limits WHERE id = ?", (rate_limit_id,))
        return cursor.rowcount > 0


# ============================================================================
# Request Log Operations
# ============================================================================

def log_request(
    request_id: str,
    user_id: Optional[str],
    api_key_id: Optional[str],
    endpoint: str,
    status: str = "success",
    duration_ms: Optional[int] = None
) -> Dict[str, Any]:
    """Log a request for rate limit tracking"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO request_log (id, user_id, api_key_id, endpoint, timestamp, duration_ms, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (request_id, user_id, api_key_id, endpoint, now, duration_ms, status)
        )
    return {
        "id": request_id,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "endpoint": endpoint,
        "timestamp": now,
        "duration_ms": duration_ms,
        "status": status
    }


def get_request_count(
    user_id: Optional[str],
    api_key_id: Optional[str],
    since: datetime,
    endpoint: Optional[str] = None
) -> int:
    """Get the count of requests since a given time"""
    with get_db() as conn:
        cursor = conn.cursor()

        query = "SELECT COUNT(*) as count FROM request_log WHERE timestamp >= ?"
        params = [since.isoformat()]

        if api_key_id:
            query += " AND api_key_id = ?"
            params.append(api_key_id)
        elif user_id:
            query += " AND (user_id = ? OR api_key_id IN (SELECT id FROM api_users WHERE name = ?))"
            params.extend([user_id, user_id])

        if endpoint:
            query += " AND endpoint = ?"
            params.append(endpoint)

        cursor.execute(query, params)
        row = cursor.fetchone()
        return row["count"] if row else 0


def cleanup_old_request_logs(older_than: datetime) -> int:
    """Remove request logs older than a given time"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM request_log WHERE timestamp < ?",
            (older_than.isoformat(),)
        )
        return cursor.rowcount


# ============================================================================
# Git Repository Operations
# ============================================================================

def get_git_repository(repository_id: str) -> Optional[Dict[str, Any]]:
    """Get a git repository by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories WHERE id = ?", (repository_id,))
        return row_to_dict(cursor.fetchone())


def get_git_repository_by_project(project_id: str) -> Optional[Dict[str, Any]]:
    """Get a git repository by project ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories WHERE project_id = ?", (project_id,))
        return row_to_dict(cursor.fetchone())


def get_all_git_repositories() -> List[Dict[str, Any]]:
    """Get all git repositories"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories ORDER BY created_at DESC")
        return rows_to_list(cursor.fetchall())


def create_git_repository(
    repository_id: str,
    project_id: str,
    remote_url: Optional[str] = None,
    default_branch: str = "main",
    github_repo_name: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Create a new git repository record"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO git_repositories (id, project_id, remote_url, default_branch, github_repo_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (repository_id, project_id, remote_url, default_branch, github_repo_name, now)
        )
    return get_git_repository(repository_id)


def update_git_repository(
    repository_id: str,
    remote_url: Optional[str] = None,
    default_branch: Optional[str] = None,
    github_repo_name: Optional[str] = None,
    last_synced_at: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a git repository"""
    existing = get_git_repository(repository_id)
    if not existing:
        return None

    updates = []
    values = []

    if remote_url is not None:
        updates.append("remote_url = ?")
        values.append(remote_url if remote_url else None)
    if default_branch is not None:
        updates.append("default_branch = ?")
        values.append(default_branch)
    if github_repo_name is not None:
        updates.append("github_repo_name = ?")
        values.append(github_repo_name if github_repo_name else None)
    if last_synced_at is not None:
        updates.append("last_synced_at = ?")
        values.append(last_synced_at)

    if updates:
        values.append(repository_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE git_repositories SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_git_repository(repository_id)


def update_git_repository_synced(repository_id: str) -> Optional[Dict[str, Any]]:
    """Update the last_synced_at timestamp for a git repository"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_repositories SET last_synced_at = ? WHERE id = ?",
            (now, repository_id)
        )
    return get_git_repository(repository_id)


def delete_git_repository(repository_id: str) -> bool:
    """Delete a git repository (worktrees will be cascade deleted)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM git_repositories WHERE id = ?", (repository_id,))
        return cursor.rowcount > 0


# ============================================================================
# Worktree Operations
# ============================================================================

def get_worktree(worktree_id: str) -> Optional[Dict[str, Any]]:
    """Get a worktree by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worktrees WHERE id = ?", (worktree_id,))
        return row_to_dict(cursor.fetchone())


def get_worktree_by_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get a worktree linked to a session.

    Uses the new sessions.worktree_id relationship (preferred) with fallback
    to the deprecated worktrees.session_id for backward compatibility.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # First try the new relationship: sessions.worktree_id -> worktrees.id
        cursor.execute("""
            SELECT w.* FROM worktrees w
            INNER JOIN sessions s ON s.worktree_id = w.id
            WHERE s.id = ?
        """, (session_id,))
        result = cursor.fetchone()
        if result:
            return row_to_dict(result)

        # Fallback to deprecated worktrees.session_id for backward compatibility
        cursor.execute("SELECT * FROM worktrees WHERE session_id = ?", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_sessions_for_worktree(worktree_id: str) -> List[Dict[str, Any]]:
    """Get all sessions that have used this worktree (current and historical)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM sessions WHERE worktree_id = ? ORDER BY updated_at DESC",
            (worktree_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_worktrees_with_sessions(repository_id: str) -> List[Dict[str, Any]]:
    """Get all worktrees for a repository with their sessions in a single query.
    
    This avoids the N+1 query pattern by fetching all data in one query.
    Returns worktrees with a 'sessions' field containing list of session dicts.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # Get all worktrees with their sessions using a LEFT JOIN
        cursor.execute("""
            SELECT 
                w.id as worktree_id,
                w.repository_id,
                w.session_id as legacy_session_id,
                w.branch_name,
                w.worktree_path,
                w.base_branch,
                w.status,
                w.created_at as worktree_created_at,
                s.id as session_id,
                s.title as session_title,
                s.status as session_status,
                s.updated_at as session_updated_at
            FROM worktrees w
            LEFT JOIN sessions s ON s.worktree_id = w.id
            WHERE w.repository_id = ?
            ORDER BY w.created_at DESC, s.updated_at DESC
        """, (repository_id,))
        
        rows = cursor.fetchall()
        
        # Group by worktree
        worktrees_map = {}
        for row in rows:
            wt_id = row["worktree_id"]
            if wt_id not in worktrees_map:
                worktrees_map[wt_id] = {
                    "id": wt_id,
                    "repository_id": row["repository_id"],
                    "session_id": row["legacy_session_id"],  # Deprecated field
                    "branch_name": row["branch_name"],
                    "worktree_path": row["worktree_path"],
                    "base_branch": row["base_branch"],
                    "status": row["status"],
                    "created_at": row["worktree_created_at"],
                    "sessions": [],
                    "session_count": 0,
                    "active_session": None
                }
            
            # Add session if present
            if row["session_id"]:
                session = {
                    "id": row["session_id"],
                    "title": row["session_title"],
                    "status": row["session_status"],
                    "updated_at": row["session_updated_at"]
                }
                worktrees_map[wt_id]["sessions"].append(session)
                worktrees_map[wt_id]["session_count"] += 1
                
                if row["session_status"] == "active" and not worktrees_map[wt_id]["active_session"]:
                    worktrees_map[wt_id]["active_session"] = session
        
        return list(worktrees_map.values())


def get_worktree_by_branch(repository_id: str, branch_name: str) -> Optional[Dict[str, Any]]:
    """Get a worktree by repository and branch name"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = ? AND branch_name = ?",
            (repository_id, branch_name)
        )
        return row_to_dict(cursor.fetchone())


def get_worktrees_for_repository(repository_id: str) -> List[Dict[str, Any]]:
    """Get all worktrees for a repository"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = ? ORDER BY created_at DESC",
            (repository_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_active_worktrees_for_repository(repository_id: str) -> List[Dict[str, Any]]:
    """Get all active worktrees for a repository"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = ? AND status = 'active' ORDER BY created_at DESC",
            (repository_id,)
        )
        return rows_to_list(cursor.fetchall())


def create_worktree(
    worktree_id: str,
    repository_id: str,
    branch_name: str,
    worktree_path: str,
    session_id: Optional[str] = None,
    base_branch: Optional[str] = None,
    status: str = "active"
) -> Optional[Dict[str, Any]]:
    """Create a new worktree record"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO worktrees (id, repository_id, session_id, branch_name, worktree_path, base_branch, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (worktree_id, repository_id, session_id, branch_name, worktree_path, base_branch, status, now)
        )
    return get_worktree(worktree_id)


def update_worktree(
    worktree_id: str,
    session_id: Optional[str] = None,
    status: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a worktree"""
    existing = get_worktree(worktree_id)
    if not existing:
        return None

    updates = []
    values = []

    if session_id is not None:
        updates.append("session_id = ?")
        values.append(session_id if session_id else None)
    if status is not None:
        updates.append("status = ?")
        values.append(status)

    if updates:
        values.append(worktree_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE worktrees SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_worktree(worktree_id)


def delete_worktree(worktree_id: str) -> bool:
    """Delete a worktree record"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM worktrees WHERE id = ?", (worktree_id,))
        return cursor.rowcount > 0


def delete_worktrees_for_repository(repository_id: str) -> int:
    """Delete all worktrees for a repository. Returns count of deleted worktrees."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM worktrees WHERE repository_id = ?", (repository_id,))
        return cursor.rowcount


# ============================================================================
# Notes Operations
# ============================================================================

def get_note(note_id: str) -> Optional[Dict[str, Any]]:
    """Get a note by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
        return row


def get_all_notes(
    search: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    Get all notes with optional search and tag filtering.
    Notes are ordered by pinned status (pinned first) then by updated_at descending.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build query with filters
        query = "SELECT * FROM notes WHERE 1=1"
        params: List[Any] = []

        if search:
            query += " AND (title LIKE ? OR content LIKE ?)"
            search_term = f"%{search}%"
            params.extend([search_term, search_term])

        if tag:
            # JSON array contains check using JSON functions
            query += " AND json_array_length(json_extract(tags, '$')) > 0 AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)"
            params.append(tag)

        query += " ORDER BY is_pinned DESC, updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = rows_to_list(cursor.fetchall())

        # Parse JSON fields
        for row in rows:
            row["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]

        return rows


def create_note(
    note_id: str,
    title: str,
    content: str = "",
    tags: Optional[List[str]] = None,
    is_pinned: bool = False,
    file_path: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new note"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO notes (id, title, content, tags, is_pinned, file_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note_id,
                title,
                content,
                json.dumps(tags or []),
                is_pinned,
                file_path,
                now,
                now
            )
        )
    return get_note(note_id)


def update_note(
    note_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[List[str]] = None,
    is_pinned: Optional[bool] = None,
    file_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a note"""
    existing = get_note(note_id)
    if not existing:
        return None

    updates = []
    values = []

    if title is not None:
        updates.append("title = ?")
        values.append(title)
    if content is not None:
        updates.append("content = ?")
        values.append(content)
    if tags is not None:
        updates.append("tags = ?")
        values.append(json.dumps(tags))
    if is_pinned is not None:
        updates.append("is_pinned = ?")
        values.append(is_pinned)
    if file_path is not None:
        updates.append("file_path = ?")
        values.append(file_path)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(note_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE notes SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_note(note_id)


def delete_note(note_id: str) -> bool:
    """Delete a note"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return cursor.rowcount > 0


def get_all_note_tags() -> List[Dict[str, Any]]:
    """
    Get all unique tags used in notes with their counts.
    Returns list of {name: str, count: int}.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # Get all notes
        cursor.execute("SELECT tags FROM notes")
        rows = cursor.fetchall()

        # Count tags
        tag_counts: Dict[str, int] = {}
        for row in rows:
            tags = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        # Sort by count descending, then name
        return sorted(
            [{"name": name, "color": None, "count": count} for name, count in tag_counts.items()],
            key=lambda x: (-x["count"], x["name"])
        )


def get_notes_autocomplete(
    search: Optional[str] = None,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """
    Get notes for autocomplete in chat.
    Returns simplified note info with preview.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        query = "SELECT id, title, content, tags, file_path FROM notes WHERE 1=1"
        params: List[Any] = []

        if search:
            query += " AND (title LIKE ? OR content LIKE ?)"
            search_term = f"%{search}%"
            params.extend([search_term, search_term])

        query += " ORDER BY is_pinned DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        result = []
        for row in rows:
            content = row["content"] or ""
            preview = content[:100] + "..." if len(content) > 100 else content
            tags = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            result.append({
                "id": row["id"],
                "title": row["title"],
                "preview": preview,
                "tags": tags,
                "file_path": row["file_path"]
            })

        return result


def count_all_notes() -> int:
    """Count total notes"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM notes")
        row = cursor.fetchone()
        return row["count"] if row else 0


def get_all_note_filenames() -> set:
    """Get the set of all note filenames (extracted from file_path) for collision detection."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM notes WHERE file_path IS NOT NULL")
        filenames = set()
        for row in cursor.fetchall():
            if row["file_path"]:
                # file_path is stored as 'notes/{filename}', extract just the filename
                filenames.add(row["file_path"].split("/")[-1])
        return filenames


def get_notes_without_file_path() -> List[Dict[str, Any]]:
    """Get all notes that don't have a file_path set (need materialization)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE file_path IS NULL")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
        return rows


def bulk_update_note_file_paths(updates: Dict[str, str]) -> None:
    """Bulk update file_path for multiple notes. updates is {note_id: file_path}."""
    with get_db() as conn:
        cursor = conn.cursor()
        for note_id, file_path in updates.items():
            cursor.execute(
                "UPDATE notes SET file_path = ? WHERE id = ?",
                (file_path, note_id)
            )


# ============================================================================
# Project Hook Operations (delegates to app.db.hooks)
# ============================================================================

from app.db import hooks as _hooks_mod

def get_hook(hook_id: str) -> Optional[Dict[str, Any]]:
    return _hooks_mod.get_hook(hook_id)

def get_hooks_for_project(project_id: str) -> List[Dict[str, Any]]:
    return _hooks_mod.get_hooks_for_project(project_id)

def get_app_hooks() -> List[Dict[str, Any]]:
    return _hooks_mod.get_app_hooks()

def get_all_hooks_for_project(project_id: str) -> List[Dict[str, Any]]:
    return _hooks_mod.get_all_hooks_for_project(project_id)

def get_enabled_hooks_for_project(project_id: str) -> List[Dict[str, Any]]:
    return _hooks_mod.get_enabled_hooks_for_project(project_id)

def get_hooks_for_project_and_event(project_id: str, event: str) -> List[Dict[str, Any]]:
    return _hooks_mod.get_hooks_for_project_and_event(project_id, event)

def get_app_hooks_for_event(event: str) -> List[Dict[str, Any]]:
    return _hooks_mod.get_app_hooks_for_event(event)

def create_hook(
    hook_id: str,
    name: str,
    event: str,
    hook_type: str,
    project_id: Optional[str] = None,
    command: Optional[str] = None,
    timeout: int = 30000,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    enabled: bool = True,
    tool_matcher: Optional[str] = None,
    message_template: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    return _hooks_mod.create_hook(
        hook_id=hook_id, project_id=project_id, name=name, event=event,
        hook_type=hook_type, command=command, timeout=timeout, url=url,
        secret=secret, enabled=enabled, tool_matcher=tool_matcher,
        message_template=message_template
    )

def update_hook(
    hook_id: str,
    name: Optional[str] = None,
    event: Optional[str] = None,
    hook_type: Optional[str] = None,
    command: Optional[str] = None,
    timeout: Optional[int] = None,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    enabled: Optional[bool] = None,
    tool_matcher: Optional[str] = None,
    message_template: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    return _hooks_mod.update_hook(
        hook_id=hook_id, name=name, event=event, hook_type=hook_type,
        command=command, timeout=timeout, url=url, secret=secret,
        enabled=enabled, tool_matcher=tool_matcher, message_template=message_template
    )

def delete_hook(hook_id: str) -> bool:
    return _hooks_mod.delete_hook(hook_id)

def update_hook_triggered(hook_id: str, success: bool) -> None:
    _hooks_mod.update_hook_triggered(hook_id, success)


# ============================================================================
# Mobile Device Operations
# ============================================================================

def create_mobile_device(
    device_id: str,
    device_name: str,
    platform: str,
    api_key_hash: str,
    capabilities: Dict[str, Any]
) -> Dict[str, Any]:
    """Register a new mobile device"""
    import uuid
    record_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mobile_devices (id, device_id, device_name, platform, api_key_hash, capabilities, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (record_id, device_id, device_name, platform, api_key_hash, json.dumps(capabilities), now, now))

    return get_mobile_device(device_id)


def get_mobile_device(device_id: str) -> Optional[Dict[str, Any]]:
    """Get a mobile device by device_id"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_devices WHERE device_id = ?", (device_id,))
        row = cursor.fetchone()
        if row:
            result = dict(row)
            if result.get("capabilities"):
                result["capabilities"] = json.loads(result["capabilities"])
            return result
        return None


def get_mobile_device_by_api_key_hash(api_key_hash: str) -> Optional[Dict[str, Any]]:
    """Get a mobile device by API key hash"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_devices WHERE api_key_hash = ?", (api_key_hash,))
        row = cursor.fetchone()
        if row:
            result = dict(row)
            if result.get("capabilities"):
                result["capabilities"] = json.loads(result["capabilities"])
            return result
        return None


def get_all_mobile_devices() -> List[Dict[str, Any]]:
    """Get all registered mobile devices"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_devices ORDER BY created_at DESC")
        rows = cursor.fetchall()
        results = []
        for row in rows:
            result = dict(row)
            if result.get("capabilities"):
                result["capabilities"] = json.loads(result["capabilities"])
            results.append(result)
        return results


def update_mobile_device_capabilities(device_id: str, capabilities: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update capabilities for a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE mobile_devices SET capabilities = ?, last_seen = ? WHERE device_id = ?
        """, (json.dumps(capabilities), now, device_id))
        if cursor.rowcount == 0:
            return None
    return get_mobile_device(device_id)


def update_mobile_device_fcm_token(device_id: str, fcm_token: str) -> Optional[Dict[str, Any]]:
    """Update FCM push token for a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE mobile_devices SET fcm_token = ?, last_seen = ? WHERE device_id = ?
        """, (fcm_token, now, device_id))
        if cursor.rowcount == 0:
            return None
    return get_mobile_device(device_id)


def update_mobile_device_last_seen(device_id: str) -> None:
    """Update last_seen timestamp for a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE mobile_devices SET last_seen = ? WHERE device_id = ?", (now, device_id))


def delete_mobile_device(device_id: str) -> bool:
    """Delete a mobile device"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mobile_devices WHERE device_id = ?", (device_id,))
        return cursor.rowcount > 0


# ============================================================================
# Pending Action Operations
# ============================================================================

def create_pending_action(
    device_id: str,
    action_type: str,
    params: Dict[str, Any],
    security_level: str,
    expires_at: str
) -> Dict[str, Any]:
    """Create a new pending action"""
    import uuid
    action_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO pending_actions (id, device_id, action_type, params, security_level, created_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (action_id, device_id, action_type, json.dumps(params), security_level, now, expires_at))

    return get_pending_action(action_id)


def get_pending_action(action_id: str) -> Optional[Dict[str, Any]]:
    """Get a pending action by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
        row = cursor.fetchone()
        if row:
            result = dict(row)
            if result.get("params"):
                result["params"] = json.loads(result["params"])
            return result
        return None


def get_pending_actions_for_device(device_id: str) -> List[Dict[str, Any]]:
    """Get all pending actions for a device that haven't expired"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM pending_actions
            WHERE device_id = ? AND status = 'pending' AND expires_at > ?
            ORDER BY created_at DESC
        """, (device_id, now))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            result = dict(row)
            if result.get("params"):
                result["params"] = json.loads(result["params"])
            results.append(result)
        return results


def update_pending_action_status(action_id: str, status: str) -> Optional[Dict[str, Any]]:
    """Update the status of a pending action"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_actions SET status = ? WHERE id = ?",
            (status, action_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_pending_action(action_id)


def cleanup_expired_actions() -> int:
    """Mark expired pending actions as expired"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE pending_actions SET status = 'expired'
            WHERE status = 'pending' AND expires_at <= ?
        """, (now,))
        return cursor.rowcount


# ============================================================================
# Trusted Action Operations
# ============================================================================

def create_trusted_action(
    device_id: str,
    action_pattern: str,
    auto_approve: bool = True,
    require_preview: bool = False
) -> Dict[str, Any]:
    """Create a trusted action pattern"""
    import uuid
    trusted_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trusted_actions (id, device_id, action_pattern, auto_approve, require_preview, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (trusted_id, device_id, action_pattern, auto_approve, require_preview, now))

    return get_trusted_action(trusted_id)


def get_trusted_action(trusted_id: str) -> Optional[Dict[str, Any]]:
    """Get a trusted action by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trusted_actions WHERE id = ?", (trusted_id,))
        return row_to_dict(cursor.fetchone())


def get_trusted_actions_for_device(device_id: str) -> List[Dict[str, Any]]:
    """Get all trusted actions for a device"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM trusted_actions WHERE device_id = ? ORDER BY created_at DESC",
            (device_id,)
        )
        return rows_to_list(cursor.fetchall())


def delete_trusted_action(trusted_id: str) -> bool:
    """Delete a trusted action"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM trusted_actions WHERE id = ?", (trusted_id,))
        return cursor.rowcount > 0


# ============================================================================
# Mobile Schedule Operations (for proactive notifications)
# ============================================================================

def get_mobile_schedules() -> List[Dict[str, Any]]:
    """Get all mobile schedules"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_schedules ORDER BY created_at DESC")
        return rows_to_list(cursor.fetchall())


def get_mobile_schedule(schedule_id: str) -> Optional[Dict[str, Any]]:
    """Get a mobile schedule by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_schedules WHERE id = ?", (schedule_id,))
        return row_to_dict(cursor.fetchone())


def save_mobile_schedule(
    schedule_id: str,
    device_id: str,
    schedule_type: str,
    trigger_time: Optional[str],
    hour: Optional[int],
    minute: Optional[int],
    params: str,
    enabled: bool,
    last_triggered_at: Optional[str]
) -> Dict[str, Any]:
    """Save or update a mobile schedule"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mobile_schedules (id, device_id, schedule_type, trigger_time, hour, minute, params, enabled, created_at, last_triggered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_id = excluded.device_id,
                schedule_type = excluded.schedule_type,
                trigger_time = excluded.trigger_time,
                hour = excluded.hour,
                minute = excluded.minute,
                params = excluded.params,
                enabled = excluded.enabled,
                last_triggered_at = excluded.last_triggered_at
        """, (schedule_id, device_id, schedule_type, trigger_time, hour, minute, params, enabled, now, last_triggered_at))
    return get_mobile_schedule(schedule_id)


def delete_mobile_schedule(schedule_id: str) -> bool:
    """Delete a mobile schedule"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mobile_schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0


# ============================================================================
# Mobile Action Operations (extended for action_queue service)
# ============================================================================

def get_mobile_actions() -> List[Dict[str, Any]]:
    """Get all mobile actions (for service initialization)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_actions ORDER BY created_at DESC")
        rows = cursor.fetchall()
        results = []
        for row in rows:
            result = dict(row)
            if result.get("params"):
                result["params"] = json.loads(result["params"])
            results.append(result)
        return results


def save_mobile_action(
    action_id: str,
    device_id: str,
    action_type: str,
    params: str,
    security_level: str,
    status: str,
    executor_result: Optional[str],
    error_message: Optional[str],
    expires_at: str,
    confirmed_at: Optional[str],
    completed_at: Optional[str]
) -> Dict[str, Any]:
    """Save or update a mobile action"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO pending_actions (id, device_id, action_type, params, security_level, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_id = excluded.device_id,
                action_type = excluded.action_type,
                params = excluded.params,
                security_level = excluded.security_level,
                status = excluded.status,
                expires_at = excluded.expires_at
        """, (action_id, device_id, action_type, params, security_level, status, now, expires_at))
    return get_pending_action(action_id)


def delete_mobile_action(action_id: str) -> bool:
    """Delete a mobile action"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
        return cursor.rowcount > 0


# ============================================================================
# Mobile Device Operations (extended for mobile_device_manager service)
# ============================================================================

def get_mobile_devices() -> List[Dict[str, Any]]:
    """Get all mobile devices (alias for get_all_mobile_devices)"""
    return get_all_mobile_devices()


def save_mobile_device(
    device_id: str,
    name: str,
    platform: str,
    model: Optional[str],
    os_version: Optional[str],
    app_version: Optional[str],
    fcm_token: Optional[str],
    api_key_hash: Optional[str],
    capabilities: str,
    is_active: bool,
    last_seen_at: Optional[str]
) -> Dict[str, Any]:
    """Save or update a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        # Check if device exists
        cursor.execute("SELECT id FROM mobile_devices WHERE device_id = ?", (device_id,))
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE mobile_devices SET
                    device_name = ?,
                    platform = ?,
                    fcm_token = ?,
                    api_key_hash = COALESCE(?, api_key_hash),
                    capabilities = ?,
                    last_seen = ?
                WHERE device_id = ?
            """, (name, platform, fcm_token, api_key_hash, capabilities, last_seen_at or now, device_id))
        else:
            import uuid
            record_id = str(uuid.uuid4())[:8]
            cursor.execute("""
                INSERT INTO mobile_devices (id, device_id, device_name, platform, api_key_hash, fcm_token, capabilities, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (record_id, device_id, name, platform, api_key_hash, fcm_token, capabilities, now, last_seen_at or now))

    return get_mobile_device(device_id)


# ============================================================================
# Automation Operations
# ============================================================================

def _parse_automation_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse JSON fields in an automation row."""
    if not row:
        return None
    for field in ("trigger_config", "output_config"):
        if row.get(field) and isinstance(row[field], str):
            row[field] = json.loads(row[field])
    # Deserialize action_steps from JSON string
    if row.get("action_steps") and isinstance(row["action_steps"], str):
        row["action_steps"] = json.loads(row["action_steps"])
    # Deserialize triggers from JSON string
    if row.get("triggers") and isinstance(row["triggers"], str):
        row["triggers"] = json.loads(row["triggers"])
    # Backward compatibility: synthesize triggers from trigger_type/trigger_config if missing
    if not row.get("triggers"):
        row["triggers"] = [{"type": row.get("trigger_type", "cron"), "config": row.get("trigger_config", {})}]
    return row


def _parse_automation_run_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse JSON fields in an automation run row."""
    if not row:
        return None
    if row.get("trigger_data") and isinstance(row["trigger_data"], str):
        row["trigger_data"] = json.loads(row["trigger_data"])
    return row


def create_automation(
    automation_id: str,
    name: str,
    trigger_type: str,
    trigger_config: Dict[str, Any],
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    description: Optional[str] = None,
    output_config: Optional[Dict[str, Any]] = None,
    max_turns: int = 1,
    timeout_seconds: int = 300,
    max_cost_per_run: Optional[float] = None,
    max_cost_per_day: Optional[float] = None,
    max_cost_total: Optional[float] = None,
    max_runs_per_day: Optional[int] = None,
    requires_approval: bool = False,
    action_steps: Optional[List[Dict[str, Any]]] = None,
    triggers: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Create a new automation."""
    now = datetime.now(timezone.utc).isoformat()
    if output_config is None:
        output_config = {"type": "session"}
    # Build triggers array if not provided
    if not triggers:
        triggers = [{"type": trigger_type, "config": trigger_config}]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO automations
               (id, name, description, trigger_type, trigger_config, prompt,
                profile_id, project_id, output_config, max_turns, timeout_seconds,
                max_cost_per_run, max_cost_per_day, max_cost_total, max_runs_per_day,
                requires_approval, action_steps, triggers, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (automation_id, name, description, trigger_type,
             json.dumps(trigger_config), prompt, profile_id, project_id,
             json.dumps(output_config), max_turns, timeout_seconds,
             max_cost_per_run, max_cost_per_day, max_cost_total, max_runs_per_day,
             requires_approval, json.dumps(action_steps) if action_steps else None,
             json.dumps(triggers),
             now, now)
        )
    return get_automation(automation_id)


def get_automation(automation_id: str) -> Optional[Dict[str, Any]]:
    """Get an automation by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automations WHERE id = ?", (automation_id,))
        return _parse_automation_row(row_to_dict(cursor.fetchone()))


def get_all_automations() -> List[Dict[str, Any]]:
    """Get all automations."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automations ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_automation_row(r) for r in rows]


def get_enabled_automations() -> List[Dict[str, Any]]:
    """Get all enabled automations."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automations WHERE is_enabled = TRUE ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_automation_row(r) for r in rows]


def get_automations_for_event(trigger_type: str, event_type: str, connector_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get enabled automations matching a connector event or connector trigger.

    Searches the triggers array for any matching trigger.
    """
    automations = get_enabled_automations()
    results = []
    for a in automations:
        triggers = a.get("triggers") or []
        matched = False
        for trig in triggers:
            if trig.get("type") != trigger_type:
                continue
            tc = trig.get("config", {})
            if trigger_type == "connector_event":
                if tc.get("event_type") != event_type:
                    continue
                if connector_id and tc.get("connector_id") and tc["connector_id"] != connector_id:
                    continue
            elif trigger_type == "connector_trigger":
                if tc.get("trigger_id") != event_type:
                    continue
                if connector_id and tc.get("connector_id") and tc["connector_id"] != connector_id:
                    continue
            matched = True
            break
        if matched:
            results.append(a)
    return results


def update_automation(automation_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update an automation. Pass keyword arguments for fields to update."""
    existing = get_automation(automation_id)
    if not existing:
        return None

    updates = []
    values = []

    for key in ("name", "description", "prompt", "profile_id", "project_id",
                "max_turns", "timeout_seconds", "max_cost_per_run", "max_cost_per_day",
                "max_cost_total", "max_runs_per_day", "is_enabled", "status",
                "last_run_at", "last_error", "run_count", "failure_count",
                "total_cost_used", "requires_approval"):
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(kwargs[key])

    for key in ("trigger_config", "output_config"):
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(json.dumps(kwargs[key]))

    if "action_steps" in kwargs:
        updates.append("action_steps = ?")
        values.append(json.dumps(kwargs["action_steps"]) if kwargs["action_steps"] is not None else None)

    if "triggers" in kwargs:
        updates.append("triggers = ?")
        values.append(json.dumps(kwargs["triggers"]) if kwargs["triggers"] is not None else None)

    if "trigger_type" in kwargs:
        updates.append("trigger_type = ?")
        values.append(kwargs["trigger_type"])

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(automation_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE automations SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_automation(automation_id)


def delete_automation(automation_id: str) -> bool:
    """Delete an automation (cascades to runs and approvals)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
        return cursor.rowcount > 0


# ---- Automation Runs ----

def create_automation_run(
    run_id: str,
    automation_id: str,
    status: str = "pending",
    trigger_data: Optional[Dict[str, Any]] = None,
    prompt_rendered: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create a new automation run."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO automation_runs
               (id, automation_id, status, trigger_data, prompt_rendered, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, automation_id, status,
             json.dumps(trigger_data) if trigger_data else None,
             prompt_rendered, now)
        )
    return get_automation_run(run_id)


def get_automation_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Get an automation run by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automation_runs WHERE id = ?", (run_id,))
        return _parse_automation_run_row(row_to_dict(cursor.fetchone()))


def get_automation_runs(automation_id: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get runs for an automation, most recent first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM automation_runs WHERE automation_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (automation_id, limit, offset)
        )
        rows = rows_to_list(cursor.fetchall())
        return [_parse_automation_run_row(r) for r in rows]


def update_automation_run(run_id: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Update an automation run."""
    updates = []
    values = []

    for key in ("status", "session_id", "prompt_rendered", "response", "error",
                "cost_usd", "tokens_in", "tokens_out", "duration_ms",
                "output_status", "output_error", "started_at", "completed_at"):
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(kwargs[key])

    if "trigger_data" in kwargs:
        updates.append("trigger_data = ?")
        values.append(json.dumps(kwargs["trigger_data"]) if kwargs["trigger_data"] else None)

    if updates:
        values.append(run_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE automation_runs SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_automation_run(run_id)


def get_automation_daily_stats(automation_id: str) -> Dict[str, Any]:
    """Get today's run count and cost for an automation."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) as run_count, COALESCE(SUM(cost_usd), 0) as total_cost
               FROM automation_runs
               WHERE automation_id = ? AND created_at >= ?""",
            (automation_id, today)
        )
        row = cursor.fetchone()
        return {"run_count": row["run_count"], "total_cost": row["total_cost"]}


def get_automation_aggregate_stats(automation_id: str) -> Dict[str, Any]:
    """Get all-time aggregate statistics for an automation using SQL queries."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT
                COUNT(*) as total_runs,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) as successful_runs,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed_runs,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd,
                AVG(CASE WHEN duration_ms > 0 THEN duration_ms END) as avg_duration_ms
            FROM automation_runs
            WHERE automation_id = ?""",
            (automation_id,)
        )
        row = cursor.fetchone()
        return {
            "total_runs": row["total_runs"],
            "successful_runs": row["successful_runs"],
            "failed_runs": row["failed_runs"],
            "total_cost_usd": row["total_cost_usd"],
            "avg_duration_ms": row["avg_duration_ms"] or 0,
        }


# ---- Automation Approvals ----

def create_automation_approval(
    approval_id: str,
    run_id: str,
    automation_id: str,
    prompt_preview: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create a new automation approval."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO automation_approvals
               (id, run_id, automation_id, prompt_preview, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (approval_id, run_id, automation_id, prompt_preview, now)
        )
    return get_automation_approval(approval_id)


def get_automation_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    """Get an automation approval by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automation_approvals WHERE id = ?", (approval_id,))
        return row_to_dict(cursor.fetchone())


def get_pending_automation_approvals() -> List[Dict[str, Any]]:
    """Get all pending automation approvals."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT a.*, au.name as automation_name
               FROM automation_approvals a
               JOIN automations au ON a.automation_id = au.id
               WHERE a.status = 'pending'
               ORDER BY a.created_at DESC"""
        )
        return rows_to_list(cursor.fetchall())


def update_automation_approval(approval_id: str, status: str, decided_by: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Update an automation approval status."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE automation_approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (status, decided_by, now, approval_id)
        )
    return get_automation_approval(approval_id)


# ============================================================================
# Queued Message Operations (delegates to app.db.queued_messages)
# ============================================================================

from app.db import queued_messages as _queued_mod

def add_queued_message(session_id: str, content: str, profile_id=None, project_id=None):
    return _queued_mod.add_queued_message(session_id, content, profile_id, project_id)

def get_queued_messages(session_id: str):
    return _queued_mod.get_queued_messages(session_id)

def pop_next_queued_message(session_id: str):
    return _queued_mod.pop_next_queued_message(session_id)

def remove_queued_message(message_id: str):
    return _queued_mod.remove_queued_message(message_id)

def clear_queued_messages(session_id: str):
    return _queued_mod.clear_queued_messages(session_id)

