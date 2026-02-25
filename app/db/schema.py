"""Database schema initialization and migrations."""

import sqlite3
import logging

from app.core.config import settings
from app.db.base import get_db, column_exists, table_exists, column_is_not_null, logger


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
SCHEMA_VERSION = 36


def _run_column_migrations(cursor: sqlite3.Cursor):
    """Run idempotent column migrations.

    These migrations run on EVERY startup to ensure columns exist,
    regardless of schema version. This handles cases where:
    - Schema version was bumped but migration didn't run
    - Database was restored from backup
    - Docker volume has stale schema version
    """
    migrations_applied = []

    # Admin 2FA columns
    admin_2fa_cols = [
        "totp_secret", "totp_enabled", "recovery_codes", "totp_verified_at"
    ]
    if table_exists(cursor, "admin"):
        for col in admin_2fa_cols:
            if not column_exists(cursor, "admin", col):
                if col == "totp_enabled":
                    col_type, default = "BOOLEAN", " DEFAULT FALSE"
                elif col == "totp_verified_at":
                    col_type, default = "TIMESTAMP", ""
                else:
                    col_type, default = "TEXT", ""
                sql = f"ALTER TABLE admin ADD COLUMN {col} {col_type}{default}"
                cursor.execute(sql)
                migrations_applied.append(f"admin.{col}")

    # Session messages profile tracking (v30)
    if table_exists(cursor, "session_messages"):
        if not column_exists(cursor, "session_messages", "profile_id"):
            cursor.execute(
                "ALTER TABLE session_messages ADD COLUMN profile_id TEXT"
            )
            migrations_applied.append("session_messages.profile_id")

    # Built-in subagent columns (v26)
    subagent_cols = [
        "default_name", "default_description", "default_prompt",
        "default_tools", "default_model"
    ]
    if table_exists(cursor, "subagents"):
        for col in subagent_cols:
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

    # Pending 2FA sessions (for login flow)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_2fa_sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add 2FA columns to admin table (migration for existing DBs)
    try:
        cursor.execute("ALTER TABLE admin ADD COLUMN totp_secret TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE admin ADD COLUMN totp_enabled BOOLEAN DEFAULT FALSE")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE admin ADD COLUMN recovery_codes TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE admin ADD COLUMN totp_verified_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # Column already exists

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
    # Project Hooks - For Claude Code event handling
    # =========================================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_hooks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            event TEXT NOT NULL,
            hook_type TEXT NOT NULL,
            command TEXT,
            timeout INTEGER DEFAULT 30000,
            url TEXT,
            secret TEXT,
            enabled BOOLEAN DEFAULT TRUE,
            tool_matcher TEXT,
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
        ('fal_api_key', 'optional', 'fal.ai API key for all AI tools (image, video, 3D, audio)'),
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
    # fal.ai Model Catalog — dynamic model discovery cache
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
