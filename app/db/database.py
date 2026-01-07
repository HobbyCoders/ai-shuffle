"""
SQLite database setup and operations
"""

import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from app.core.config import settings

logger = logging.getLogger(__name__)


# Schema version for migrations
# v21: Force re-run to ensure agent_runs, agent_tasks, agent_logs tables exist
# v22: Add credential_policies and api_user_credentials tables
SCHEMA_VERSION = 22


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory"""
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
            profile_id TEXT NOT NULL,
            api_user_id TEXT,
            sdk_session_id TEXT,
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
            FOREIGN KEY (profile_id) REFERENCES profiles(id),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL
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

    # Checkpoints for rewind functionality
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sdk_session_id TEXT NOT NULL,
            message_uuid TEXT NOT NULL,
            message_preview TEXT,
            message_index INTEGER DEFAULT 0,
            git_ref TEXT,
            git_available BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
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

    # Knowledge base documents for per-project context injection
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text/plain',
            file_size INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # Knowledge base chunks for chunked content (enables future vector search)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            metadata JSON DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_api_user ON sessions(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_log_session ON sync_log(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_log_created ON sync_log(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_created ON login_attempts(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_lockouts_ip ON account_lockouts(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_lockouts_username ON account_lockouts(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_key_sessions_user ON api_key_sessions(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_message_uuid ON checkpoints(message_uuid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_user ON user_preferences(user_type, user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subagents_name ON subagents(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_rules_profile ON permission_rules(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_rules_tool ON permission_rules(tool_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_session ON session_tags(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_active ON webhooks(is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_documents_project ON knowledge_documents(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document ON knowledge_chunks(document_id)")

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            session_id TEXT UNIQUE,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_branch TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (repository_id) REFERENCES git_repositories(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
        )
    """)

    # Create git indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_git_repositories_project ON git_repositories(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_repository ON worktrees(repository_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_session ON worktrees(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_branch ON worktrees(branch_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_status ON worktrees(status)")

    # =========================================================================
    # Background Agent Runs (autonomous agent execution)
    # =========================================================================

    # Agent runs - tracks background agent executions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            progress REAL DEFAULT 0,
            profile_id TEXT,
            project_id TEXT,
            worktree_id TEXT,
            branch TEXT,
            base_branch TEXT,
            pr_url TEXT,
            auto_branch BOOLEAN DEFAULT TRUE,
            auto_pr BOOLEAN DEFAULT FALSE,
            auto_merge BOOLEAN DEFAULT FALSE,
            auto_review BOOLEAN DEFAULT FALSE,
            max_duration_minutes INTEGER DEFAULT 30,
            sdk_session_id TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT,
            result_summary TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        )
    """)

    # Migration: Add base_branch column to agent_runs (for existing DBs)
    try:
        cursor.execute("ALTER TABLE agent_runs ADD COLUMN base_branch TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migration: Add auto_merge column to agent_runs (for existing DBs)
    try:
        cursor.execute("ALTER TABLE agent_runs ADD COLUMN auto_merge BOOLEAN DEFAULT FALSE")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Agent tasks - hierarchical task tracking within agent runs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            agent_run_id TEXT NOT NULL,
            parent_task_id TEXT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            order_index INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        )
    """)

    # Agent logs - detailed logs for agent runs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            level TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            metadata JSON,
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        )
    """)

    # Create agent run indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_profile ON agent_runs(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_run ON agent_tasks(agent_run_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_parent ON agent_tasks(parent_task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_logs_run ON agent_logs(agent_run_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_logs_timestamp ON agent_logs(timestamp)")

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

    # Create indexes for new tables
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_credentials_user ON api_user_credentials(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_credentials_type ON api_user_credentials(credential_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_github_config_user ON api_user_github_config(api_user_id)")

    # Initialize default credential policies if not exist
    default_policies = [
        ('openai_api_key', 'optional', 'OpenAI API key for AI tools (TTS, STT, GPT Image, Sora)'),
        ('gemini_api_key', 'optional', 'Google Gemini API key for Nano Banana, Imagen, Veo'),
        ('github_pat', 'user_provided', 'GitHub Personal Access Token for repository access'),
    ]
    for policy_id, policy, description in default_policies:
        cursor.execute("""
            INSERT OR IGNORE INTO credential_policies (id, policy, description)
            VALUES (?, ?, ?)
        """, (policy_id, policy, description))


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Convert a sqlite3.Row to a dictionary"""
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Convert a list of sqlite3.Row to a list of dictionaries"""
    return [dict(row) for row in rows]


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


def update_admin_totp(totp_secret: Optional[str], totp_enabled: bool, recovery_codes: Optional[str] = None) -> bool:
    """Update admin 2FA settings"""
    with get_db() as conn:
        cursor = conn.cursor()
        if totp_enabled:
            cursor.execute(
                """UPDATE admin SET totp_secret = ?, totp_enabled = ?, recovery_codes = ?,
                   totp_verified_at = ? WHERE id = 1""",
                (totp_secret, totp_enabled, recovery_codes, datetime.utcnow().isoformat())
            )
        else:
            cursor.execute(
                "UPDATE admin SET totp_secret = NULL, totp_enabled = FALSE, recovery_codes = NULL, totp_verified_at = NULL WHERE id = 1",
                ()
            )
        return cursor.rowcount > 0


def get_admin_2fa_status() -> Dict[str, Any]:
    """Get admin 2FA status (without revealing secret)"""
    admin = get_admin()
    if not admin:
        return {"enabled": False, "has_recovery_codes": False}
    return {
        "enabled": bool(admin.get("totp_enabled")),
        "has_recovery_codes": bool(admin.get("recovery_codes")),
        "verified_at": admin.get("totp_verified_at")
    }


def update_admin_recovery_codes(recovery_codes: str) -> bool:
    """Update admin recovery codes"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE admin SET recovery_codes = ? WHERE id = 1",
            (recovery_codes,)
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
# Pending 2FA Session Operations
# ============================================================================

def create_pending_2fa_session(token: str, username: str, expires_at: datetime) -> Dict[str, Any]:
    """Create a pending 2FA session token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pending_2fa_sessions (token, username, expires_at) VALUES (?, ?, ?)",
            (token, username, expires_at.isoformat())
        )
        return {"token": token, "username": username, "expires_at": expires_at}


def get_pending_2fa_session(token: str) -> Optional[Dict[str, Any]]:
    """Get a pending 2FA session by token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_2fa_sessions WHERE token = ? AND expires_at > ?",
            (token, datetime.utcnow().isoformat())
        )
        return row_to_dict(cursor.fetchone())


def delete_pending_2fa_session(token: str):
    """Delete a pending 2FA session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_2fa_sessions WHERE token = ?", (token,))


def cleanup_expired_2fa_sessions():
    """Remove expired pending 2FA sessions"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM pending_2fa_sessions WHERE expires_at < ?",
            (datetime.utcnow().isoformat(),)
        )


# ============================================================================
# Auth Session Operations
# ============================================================================

def create_auth_session(token: str, expires_at: datetime) -> Dict[str, Any]:
    """Create an auth session token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO auth_sessions (token, expires_at) VALUES (?, ?)",
            (token, expires_at.isoformat())
        )
        return {"token": token, "expires_at": expires_at}


def get_auth_session(token: str) -> Optional[Dict[str, Any]]:
    """Get an auth session by token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM auth_sessions WHERE token = ? AND expires_at > ?",
            (token, datetime.utcnow().isoformat())
        )
        return row_to_dict(cursor.fetchone())


def delete_auth_session(token: str):
    """Delete an auth session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


def cleanup_expired_sessions():
    """Remove expired auth sessions"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM auth_sessions WHERE expires_at < ?",
            (datetime.utcnow().isoformat(),)
        )


def clear_all_sessions():
    """
    Clear all authentication sessions (admin and API user).
    Called on app startup to force re-login, which is required
    to restore the encryption key to memory.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # Clear admin sessions
        cursor.execute("DELETE FROM auth_sessions")
        admin_count = cursor.rowcount
        # Clear API user web sessions
        cursor.execute("DELETE FROM api_key_sessions")
        api_count = cursor.rowcount
        return admin_count, api_count


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
    now = datetime.utcnow().isoformat()
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
    """
    existing = get_profile(profile_id)
    if not existing:
        return None
    if existing["is_builtin"] and not allow_builtin:
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
        values.append(datetime.utcnow().isoformat())
        values.append(profile_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE profiles SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_profile(profile_id)


def delete_profile(profile_id: str) -> bool:
    """Delete a profile and handle foreign key references.

    Note: Sessions using this profile will also be deleted (along with their messages
    due to CASCADE). This is necessary because sessions.profile_id is NOT NULL and
    the FK constraint doesn't have ON DELETE SET NULL.
    """
    existing = get_profile(profile_id)
    if not existing:
        return False

    with get_db() as conn:
        cursor = conn.cursor()
        # Delete sessions using this profile (session_messages will cascade delete)
        cursor.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))
        # Now delete the profile
        cursor.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        return cursor.rowcount > 0


def set_profile_builtin(profile_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE profiles SET is_builtin = ?, updated_at = ? WHERE id = ?",
            (is_builtin, datetime.utcnow().isoformat(), profile_id)
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
    now = datetime.utcnow().isoformat()
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
    settings_dict: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Update a project"""
    existing = get_project(project_id)
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
    if settings_dict is not None:
        updates.append("settings = ?")
        values.append(json.dumps(settings_dict))

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(project_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_project(project_id)


def delete_project(project_id: str) -> bool:
    """Delete a project"""
    with get_db() as conn:
        cursor = conn.cursor()
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


def create_session(
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    title: Optional[str] = None,
    api_user_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    fork_point_message_index: Optional[int] = None
) -> Dict[str, Any]:
    """Create a new session"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sessions (id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, now, now)
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
    turn_increment: int = 0
) -> Optional[Dict[str, Any]]:
    """Update a session with usage stats"""
    updates = ["updated_at = ?"]
    values = [datetime.utcnow().isoformat()]

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
            (datetime.utcnow().isoformat(), session_id)
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
            (is_favorite, datetime.utcnow().isoformat(), session_id)
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
    metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Add a message to a session"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO session_messages (session_id, role, content, tool_name, tool_input, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, role, content, tool_name,
             json.dumps(tool_input) if tool_input else None,
             json.dumps(metadata) if metadata else None,
             now)
        )
        return {
            "id": cursor.lastrowid,
            "session_id": session_id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "metadata": metadata,
            "created_at": now
        }


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

def get_api_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Get an API user by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_users WHERE id = ?", (user_id,))
        return row_to_dict(cursor.fetchone())


def get_api_user_by_key_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    """Get an API user by API key hash"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_users WHERE api_key_hash = ? AND is_active = TRUE",
            (key_hash,)
        )
        return row_to_dict(cursor.fetchone())


def get_all_api_users() -> List[Dict[str, Any]]:
    """Get all API users"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_users ORDER BY created_at DESC")
        return rows_to_list(cursor.fetchall())


def create_api_user(
    user_id: str,
    name: str,
    api_key_hash: str,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    description: Optional[str] = None,
    username: Optional[str] = None,
    password_hash: Optional[str] = None,
    web_login_allowed: bool = True
) -> Dict[str, Any]:
    """Create a new API user"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO api_users (id, name, api_key_hash, project_id, profile_id, description, username, password_hash, web_login_allowed, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, api_key_hash, project_id, profile_id, description, username, password_hash, web_login_allowed, now, now)
        )
    return get_api_user(user_id)


def update_api_user(
    user_id: str,
    name: Optional[str] = None,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
    web_login_allowed: Optional[bool] = None
) -> Optional[Dict[str, Any]]:
    """Update an API user"""
    existing = get_api_user(user_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if project_id is not None:
        updates.append("project_id = ?")
        values.append(project_id if project_id else None)
    if profile_id is not None:
        updates.append("profile_id = ?")
        values.append(profile_id if profile_id else None)
    if description is not None:
        updates.append("description = ?")
        values.append(description)
    if is_active is not None:
        updates.append("is_active = ?")
        values.append(is_active)
    if web_login_allowed is not None:
        updates.append("web_login_allowed = ?")
        values.append(web_login_allowed)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(user_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE api_users SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_api_user(user_id)


def update_api_user_key(user_id: str, api_key_hash: str) -> Optional[Dict[str, Any]]:
    """Update an API user's key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_users SET api_key_hash = ?, updated_at = ? WHERE id = ?",
            (api_key_hash, datetime.utcnow().isoformat(), user_id)
        )
    return get_api_user(user_id)


def update_api_user_last_used(user_id: str):
    """Update the last_used_at timestamp for an API user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_users SET last_used_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), user_id)
        )


def delete_api_user(user_id: str) -> bool:
    """Delete an API user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


def get_api_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Get an API user by username"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_users WHERE username = ? AND is_active = TRUE",
            (username,)
        )
        return row_to_dict(cursor.fetchone())


def is_api_key_claimed(key_hash: str) -> bool:
    """Check if an API key has already been claimed (has username/password set)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username FROM api_users WHERE api_key_hash = ?",
            (key_hash,)
        )
        row = cursor.fetchone()
        if row:
            return row["username"] is not None
        return False


def claim_api_key(key_hash: str, username: str, password_hash: str) -> Optional[Dict[str, Any]]:
    """
    Claim an API key by setting username and password.
    Returns the updated API user or None if the key doesn't exist or is already claimed.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # First check if the key exists and isn't claimed
        cursor.execute(
            "SELECT id, username FROM api_users WHERE api_key_hash = ? AND is_active = TRUE",
            (key_hash,)
        )
        row = cursor.fetchone()
        if not row:
            return None  # API key doesn't exist or is inactive
        if row["username"] is not None:
            return None  # Already claimed

        # Claim it
        user_id = row["id"]
        cursor.execute(
            "UPDATE api_users SET username = ?, password_hash = ?, updated_at = ? WHERE id = ?",
            (username, password_hash, datetime.utcnow().isoformat(), user_id)
        )
    return get_api_user(user_id)


def is_username_taken(username: str) -> bool:
    """Check if a username is already taken"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM api_users WHERE username = ?", (username,))
        return cursor.fetchone() is not None


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
                (policy, description, datetime.utcnow().isoformat(), policy_id)
            )
        else:
            cursor.execute(
                "UPDATE credential_policies SET policy = ?, updated_at = ? WHERE id = ?",
                (policy, datetime.utcnow().isoformat(), policy_id)
            )
    return get_credential_policy(policy_id)


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
    now = datetime.utcnow().isoformat()

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
# API User GitHub Config Operations (per-user GitHub settings)
# ============================================================================

def get_user_github_config(api_user_id: str) -> Optional[Dict[str, Any]]:
    """Get GitHub configuration for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_user_github_config WHERE api_user_id = ?",
            (api_user_id,)
        )
        return row_to_dict(cursor.fetchone())


def set_user_github_config(
    api_user_id: str,
    github_username: Optional[str] = None,
    github_avatar_url: Optional[str] = None,
    default_repo: Optional[str] = None,
    default_branch: Optional[str] = None
) -> Dict[str, Any]:
    """Set or update GitHub configuration for a user"""
    import uuid
    now = datetime.utcnow().isoformat()

    existing = get_user_github_config(api_user_id)

    with get_db() as conn:
        cursor = conn.cursor()

        if existing:
            # Build update query dynamically for provided fields
            updates = ["updated_at = ?"]
            values = [now]

            if github_username is not None:
                updates.append("github_username = ?")
                values.append(github_username)
            if github_avatar_url is not None:
                updates.append("github_avatar_url = ?")
                values.append(github_avatar_url)
            if default_repo is not None:
                updates.append("default_repo = ?")
                values.append(default_repo)
            if default_branch is not None:
                updates.append("default_branch = ?")
                values.append(default_branch)

            values.append(api_user_id)
            cursor.execute(
                f"UPDATE api_user_github_config SET {', '.join(updates)} WHERE api_user_id = ?",
                values
            )
        else:
            # Insert new
            config_id = str(uuid.uuid4())
            cursor.execute(
                """INSERT INTO api_user_github_config
                   (id, api_user_id, github_username, github_avatar_url, default_repo, default_branch, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (config_id, api_user_id, github_username, github_avatar_url, default_repo, default_branch, now, now)
            )

    return get_user_github_config(api_user_id)


def delete_user_github_config(api_user_id: str) -> bool:
    """Delete GitHub configuration for a user (disconnect GitHub)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM api_user_github_config WHERE api_user_id = ?",
            (api_user_id,)
        )
        return cursor.rowcount > 0


def update_api_user_password(api_user_id: str, password_hash: str) -> bool:
    """Update an API user's password"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, datetime.utcnow().isoformat(), api_user_id)
        )
        return cursor.rowcount > 0


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
    now = datetime.utcnow().isoformat()
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


def cleanup_old_sync_logs(max_age_hours: int = 24):
    """Remove sync log entries older than max_age_hours"""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM sync_log WHERE created_at < ?",
            (cutoff,)
        )
        return cursor.rowcount


def update_message_content(
    message_id: int,
    content: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Update message content (used during streaming)"""
    now = datetime.utcnow().isoformat()
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
            (ip_address, username, success, datetime.utcnow().isoformat())
        )


def get_failed_attempts_count(ip_address: str, window_minutes: int = 15) -> int:
    """Get the number of failed login attempts from an IP in the time window"""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(minutes=window_minutes)).isoformat()
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
    cutoff = (datetime.utcnow() - timedelta(minutes=window_minutes)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) as count FROM login_attempts
               WHERE username = ? AND success = FALSE AND created_at > ?""",
            (username, cutoff)
        )
        row = cursor.fetchone()
        return row["count"]


def cleanup_old_login_attempts(max_age_hours: int = 24):
    """Remove old login attempt records"""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM login_attempts WHERE created_at < ?",
            (cutoff,)
        )
        return cursor.rowcount


# ============================================================================
# Account Lockout Operations
# ============================================================================

def create_lockout(ip_address: Optional[str], username: Optional[str],
                   duration_minutes: int, reason: str):
    """Create a lockout for an IP or username"""
    from datetime import timedelta
    locked_until = (datetime.utcnow() + timedelta(minutes=duration_minutes)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO account_lockouts (ip_address, username, locked_until, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (ip_address, username, locked_until, reason, datetime.utcnow().isoformat())
        )


def is_ip_locked(ip_address: str) -> Optional[Dict[str, Any]]:
    """Check if an IP address is currently locked out"""
    now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM account_lockouts
               WHERE username = ? AND locked_until > ?
               ORDER BY locked_until DESC LIMIT 1""",
            (username, now)
        )
        return row_to_dict(cursor.fetchone())


def cleanup_expired_lockouts():
    """Remove expired lockout records"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM account_lockouts WHERE locked_until < ?",
            (now,)
        )
        return cursor.rowcount


# ============================================================================
# API Key Web Session Operations
# ============================================================================

def create_api_key_session(token: str, api_user_id: str, expires_at: datetime) -> Dict[str, Any]:
    """Create a web session for an API key user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO api_key_sessions (token, api_user_id, expires_at) VALUES (?, ?, ?)",
            (token, api_user_id, expires_at.isoformat())
        )
        return {"token": token, "api_user_id": api_user_id, "expires_at": expires_at}


def get_api_key_session(token: str) -> Optional[Dict[str, Any]]:
    """Get an API key session by token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT aks.*, au.name as user_name, au.project_id, au.profile_id, au.is_active
               FROM api_key_sessions aks
               JOIN api_users au ON aks.api_user_id = au.id
               WHERE aks.token = ? AND aks.expires_at > ? AND au.is_active = TRUE""",
            (token, datetime.utcnow().isoformat())
        )
        return row_to_dict(cursor.fetchone())


def delete_api_key_session(token: str):
    """Delete an API key session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_key_sessions WHERE token = ?", (token,))


def delete_api_key_sessions_for_user(api_user_id: str):
    """Delete all sessions for an API user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_key_sessions WHERE api_user_id = ?", (api_user_id,))


def cleanup_expired_api_key_sessions():
    """Remove expired API key sessions"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM api_key_sessions WHERE expires_at < ?",
            (datetime.utcnow().isoformat(),)
        )
        return cursor.rowcount


# ============================================================================
# Checkpoint Operations (for rewind functionality)
# ============================================================================

def create_checkpoint(
    checkpoint_id: str,
    session_id: str,
    sdk_session_id: str,
    message_uuid: str,
    message_preview: Optional[str] = None,
    message_index: int = 0,
    git_ref: Optional[str] = None,
    git_available: bool = False
) -> Dict[str, Any]:
    """Create a checkpoint for rewind functionality"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO checkpoints (id, session_id, sdk_session_id, message_uuid,
               message_preview, message_index, git_ref, git_available, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (checkpoint_id, session_id, sdk_session_id, message_uuid,
             message_preview, message_index, git_ref, git_available, now)
        )
    return get_checkpoint(checkpoint_id)


def get_checkpoint(checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """Get a checkpoint by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,))
        return row_to_dict(cursor.fetchone())


def get_checkpoint_by_message_uuid(session_id: str, message_uuid: str) -> Optional[Dict[str, Any]]:
    """Get a checkpoint by session and message UUID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? AND message_uuid = ?",
            (session_id, message_uuid)
        )
        return row_to_dict(cursor.fetchone())


def get_session_checkpoints(session_id: str) -> List[Dict[str, Any]]:
    """Get all checkpoints for a session ordered by message index"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM checkpoints WHERE session_id = ?
               ORDER BY message_index ASC""",
            (session_id,)
        )
        return rows_to_list(cursor.fetchall())


def delete_checkpoint(checkpoint_id: str) -> bool:
    """Delete a checkpoint"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
        return cursor.rowcount > 0


def delete_session_checkpoints_after(session_id: str, message_index: int) -> int:
    """Delete all checkpoints after a specific message index (for rewind cleanup)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM checkpoints WHERE session_id = ? AND message_index > ?",
            (session_id, message_index)
        )
        return cursor.rowcount


def delete_all_session_checkpoints(session_id: str) -> int:
    """Delete all checkpoints for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
        return cursor.rowcount


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
        now = datetime.utcnow().isoformat()
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
    is_builtin: bool = False
) -> Dict[str, Any]:
    """Create a new subagent"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO subagents (id, name, description, prompt, tools, model, is_builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subagent_id, name, description, prompt, json.dumps(tools) if tools else None, model, is_builtin, now, now)
        )
    return get_subagent(subagent_id)


def update_subagent(
    subagent_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    prompt: Optional[str] = None,
    tools: Optional[List[str]] = None,
    model: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update a subagent"""
    existing = get_subagent(subagent_id)
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
    if prompt is not None:
        updates.append("prompt = ?")
        values.append(prompt)
    if tools is not None:
        updates.append("tools = ?")
        values.append(json.dumps(tools))
    if model is not None:
        updates.append("model = ?")
        values.append(model if model else None)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
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
            (is_builtin, datetime.utcnow().isoformat(), subagent_id)
        )
        return cursor.rowcount > 0


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
    now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
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
        values.append(datetime.utcnow().isoformat())
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
        now = datetime.utcnow().isoformat()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT OR IGNORE INTO session_tags (session_id, tag_id, created_at)
                   VALUES (?, ?, ?)""",
                (session_id, tag_id, now)
            )
            return cursor.rowcount > 0
    except Exception:
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
    now = datetime.utcnow().isoformat()
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

def get_analytics_usage_stats(start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Get aggregate usage statistics for a date range.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive

    Returns:
        Dictionary with total_tokens_in, total_tokens_out, total_cost_usd, session_count, query_count
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(created_at) <= ?"
            params.append(end_date)

        # Get usage log aggregates
        cursor.execute(f"""
            SELECT
                COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd,
                COUNT(*) as query_count
            FROM usage_log
            WHERE 1=1 {date_filter}
        """, params)
        usage_row = cursor.fetchone()

        # Get session count for the same period
        cursor.execute(f"""
            SELECT COUNT(DISTINCT session_id) as session_count
            FROM usage_log
            WHERE session_id IS NOT NULL {date_filter}
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
    group_by: str = "profile"
) -> List[Dict[str, Any]]:
    """
    Get cost breakdown grouped by profile, user, or date.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        group_by: Grouping - 'profile', 'user', or 'date'

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
                WHERE 1=1 {date_filter}
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
                WHERE 1=1 {date_filter}
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
                WHERE 1=1 {date_filter}
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
    interval: str = "day"
) -> List[Dict[str, Any]]:
    """
    Get usage trends over time.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        interval: Time interval - 'day', 'week', or 'month'

    Returns:
        List of dictionaries with date, tokens_in, tokens_out, cost_usd, query_count
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(created_at) <= ?"
            params.append(end_date)

        # Choose date grouping based on interval
        if interval == "week":
            # SQLite strftime: %Y-W%W gives year and week number
            date_expr = "strftime('%Y-W%W', created_at)"
        elif interval == "month":
            date_expr = "strftime('%Y-%m', created_at)"
        else:  # day
            date_expr = "DATE(created_at)"

        cursor.execute(f"""
            SELECT
                {date_expr} as date,
                COALESCE(SUM(tokens_in), 0) as tokens_in,
                COALESCE(SUM(tokens_out), 0) as tokens_out,
                COALESCE(SUM(cost_usd), 0) as cost_usd,
                COUNT(*) as query_count
            FROM usage_log
            WHERE 1=1 {date_filter}
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
    limit: int = 10
) -> List[Dict[str, Any]]:
    """
    Get top sessions by cost.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        limit: Maximum number of sessions to return

    Returns:
        List of dictionaries with session_id, title, profile_id, profile_name, total_cost_usd, etc.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += " AND DATE(s.created_at) >= ?"
            params.append(start_date)
        if end_date:
            date_filter += " AND DATE(s.created_at) <= ?"
            params.append(end_date)

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
            WHERE s.total_cost_usd > 0 {date_filter}
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
    now = datetime.utcnow().isoformat()
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
        values.append(datetime.utcnow().isoformat())
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
            (is_builtin, datetime.utcnow().isoformat(), template_id)
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
    now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
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
    values = [datetime.utcnow().isoformat()]

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
    now = datetime.utcnow().isoformat()
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
# Knowledge Base Operations
# ============================================================================

def get_knowledge_documents(project_id: str) -> List[Dict[str, Any]]:
    """Get all knowledge documents for a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM knowledge_documents WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_knowledge_document(document_id: str) -> Optional[Dict[str, Any]]:
    """Get a knowledge document by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM knowledge_documents WHERE id = ?", (document_id,))
        return row_to_dict(cursor.fetchone())


def create_knowledge_document(
    document_id: str,
    project_id: str,
    filename: str,
    content: str,
    content_type: str = "text/plain",
    file_size: int = 0,
    chunk_count: int = 0
) -> Optional[Dict[str, Any]]:
    """Create a new knowledge document"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO knowledge_documents
               (id, project_id, filename, content, content_type, file_size, chunk_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, project_id, filename, content, content_type, file_size, chunk_count, now, now)
        )
    return get_knowledge_document(document_id)


def update_knowledge_document(
    document_id: str,
    content: Optional[str] = None,
    chunk_count: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Update a knowledge document"""
    existing = get_knowledge_document(document_id)
    if not existing:
        return None

    updates = ["updated_at = ?"]
    values = [datetime.utcnow().isoformat()]

    if content is not None:
        updates.append("content = ?")
        values.append(content)
        updates.append("file_size = ?")
        values.append(len(content))
    if chunk_count is not None:
        updates.append("chunk_count = ?")
        values.append(chunk_count)

    values.append(document_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE knowledge_documents SET {', '.join(updates)} WHERE id = ?",
            values
        )

    return get_knowledge_document(document_id)


def delete_knowledge_document(document_id: str) -> bool:
    """Delete a knowledge document (chunks will be cascade deleted)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
        return cursor.rowcount > 0


def get_knowledge_chunks(document_id: str) -> List[Dict[str, Any]]:
    """Get all chunks for a document"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM knowledge_chunks WHERE document_id = ? ORDER BY chunk_index",
            (document_id,)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        return rows


def get_all_knowledge_chunks_for_project(project_id: str) -> List[Dict[str, Any]]:
    """Get all chunks for all documents in a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT kc.*, kd.filename
               FROM knowledge_chunks kc
               JOIN knowledge_documents kd ON kc.document_id = kd.id
               WHERE kd.project_id = ?
               ORDER BY kd.created_at, kc.chunk_index""",
            (project_id,)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        return rows


def create_knowledge_chunk(
    chunk_id: str,
    document_id: str,
    chunk_index: int,
    content: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Create a new knowledge chunk"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO knowledge_chunks (id, document_id, chunk_index, content, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chunk_id, document_id, chunk_index, content, json.dumps(metadata or {}), now)
        )
        cursor.execute("SELECT * FROM knowledge_chunks WHERE id = ?", (chunk_id,))
        row = row_to_dict(cursor.fetchone())
        if row and row.get("metadata"):
            row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        return row


def delete_knowledge_chunks_for_document(document_id: str) -> int:
    """Delete all chunks for a document. Returns count of deleted chunks."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
        return cursor.rowcount


def search_knowledge_chunks(project_id: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Simple keyword-based search for relevant knowledge chunks.
    Returns chunks that contain the query terms (case-insensitive).

    For future: This can be enhanced with vector embeddings for semantic search.
    """
    query_lower = query.lower()
    query_terms = query_lower.split()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT kc.*, kd.filename
               FROM knowledge_chunks kc
               JOIN knowledge_documents kd ON kc.document_id = kd.id
               WHERE kd.project_id = ?""",
            (project_id,)
        )
        rows = rows_to_list(cursor.fetchall())

        scored_chunks = []
        for row in rows:
            content_lower = row["content"].lower()
            score = sum(1 for term in query_terms if term in content_lower)
            if score > 0:
                row["relevance_score"] = score
                if row.get("metadata"):
                    row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
                scored_chunks.append(row)

        scored_chunks.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored_chunks[:limit]


def get_knowledge_stats_for_project(project_id: str) -> Dict[str, Any]:
    """Get knowledge base statistics for a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) as document_count,
                      COALESCE(SUM(file_size), 0) as total_size,
                      COALESCE(SUM(chunk_count), 0) as total_chunks
               FROM knowledge_documents WHERE project_id = ?""",
            (project_id,)
        )
        row = cursor.fetchone()
        return {
            "document_count": row["document_count"] if row else 0,
            "total_size": row["total_size"] if row else 0,
            "total_chunks": row["total_chunks"] if row else 0
        }


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
    now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
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
    """Get a worktree by session ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worktrees WHERE session_id = ?", (session_id,))
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
    now = datetime.utcnow().isoformat()
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
# Agent Run Operations (Background Autonomous Agents)
# ============================================================================

def create_agent_run(
    agent_run_id: str,
    name: str,
    prompt: str,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    auto_branch: bool = True,
    auto_pr: bool = False,
    auto_merge: bool = False,
    auto_review: bool = False,
    max_duration_minutes: int = 30,
    base_branch: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Create a new agent run record"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agent_runs
               (id, name, prompt, status, progress, profile_id, project_id,
                auto_branch, auto_pr, auto_merge, auto_review, max_duration_minutes, base_branch, started_at)
               VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_run_id, name, prompt, profile_id, project_id,
             auto_branch, auto_pr, auto_merge, auto_review, max_duration_minutes, base_branch, now)
        )
    return get_agent_run(agent_run_id)


def get_agent_run(agent_run_id: str) -> Optional[Dict[str, Any]]:
    """Get an agent run by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_runs WHERE id = ?", (agent_run_id,))
        return row_to_dict(cursor.fetchone())


def get_agent_runs(
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get agent runs with optional filtering"""
    query = "SELECT * FROM agent_runs WHERE 1=1"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)

    query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def get_agent_runs_count(
    status: Optional[str] = None,
    project_id: Optional[str] = None
) -> int:
    """Get count of agent runs with optional filtering"""
    query = "SELECT COUNT(*) as count FROM agent_runs WHERE 1=1"
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return row["count"]


def get_running_agent_runs() -> List[Dict[str, Any]]:
    """Get all currently running or paused agent runs"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_runs WHERE status IN ('running', 'paused') ORDER BY started_at DESC"
        )
        return rows_to_list(cursor.fetchall())


def get_queued_agent_runs() -> List[Dict[str, Any]]:
    """Get all queued agent runs"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_runs WHERE status = 'queued' ORDER BY started_at ASC"
        )
        return rows_to_list(cursor.fetchall())


def update_agent_run(
    agent_run_id: str,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    worktree_id: Optional[str] = None,
    branch: Optional[str] = None,
    pr_url: Optional[str] = None,
    sdk_session_id: Optional[str] = None,
    completed_at: Optional[str] = None,
    error: Optional[str] = None,
    result_summary: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update an agent run"""
    existing = get_agent_run(agent_run_id)
    if not existing:
        return None

    updates = []
    values = []

    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if progress is not None:
        updates.append("progress = ?")
        values.append(progress)
    if worktree_id is not None:
        updates.append("worktree_id = ?")
        values.append(worktree_id)
    if branch is not None:
        updates.append("branch = ?")
        values.append(branch)
    if pr_url is not None:
        updates.append("pr_url = ?")
        values.append(pr_url)
    if sdk_session_id is not None:
        updates.append("sdk_session_id = ?")
        values.append(sdk_session_id)
    if completed_at is not None:
        updates.append("completed_at = ?")
        values.append(completed_at)
    if error is not None:
        updates.append("error = ?")
        values.append(error)
    if result_summary is not None:
        updates.append("result_summary = ?")
        values.append(result_summary)

    if updates:
        values.append(agent_run_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE agent_runs SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_agent_run(agent_run_id)


def delete_agent_run(agent_run_id: str) -> bool:
    """Delete an agent run and its associated tasks/logs"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_runs WHERE id = ?", (agent_run_id,))
        return cursor.rowcount > 0


# ============================================================================
# Agent Task Operations
# ============================================================================

def create_agent_task(
    task_id: str,
    agent_run_id: str,
    name: str,
    parent_task_id: Optional[str] = None,
    status: str = "pending",
    order_index: int = 0
) -> Optional[Dict[str, Any]]:
    """Create a new agent task"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agent_tasks
               (id, agent_run_id, parent_task_id, name, status, order_index, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, agent_run_id, parent_task_id, name, status, order_index, now, now)
        )
    return get_agent_task(task_id)


def get_agent_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Get an agent task by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,))
        return row_to_dict(cursor.fetchone())


def get_agent_tasks(agent_run_id: str) -> List[Dict[str, Any]]:
    """Get all tasks for an agent run"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_tasks WHERE agent_run_id = ? ORDER BY order_index, created_at",
            (agent_run_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_agent_tasks_tree(agent_run_id: str) -> List[Dict[str, Any]]:
    """Get tasks as a hierarchical tree structure"""
    tasks = get_agent_tasks(agent_run_id)

    # Build parent -> children map
    children_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for task in tasks:
        parent_id = task.get("parent_task_id")
        if parent_id not in children_map:
            children_map[parent_id] = []
        children_map[parent_id].append(task)

    def build_tree(parent_id: Optional[str]) -> List[Dict[str, Any]]:
        children = children_map.get(parent_id, [])
        result = []
        for child in children:
            child_copy = dict(child)
            child_copy["children"] = build_tree(child["id"])
            result.append(child_copy)
        return result

    return build_tree(None)


def update_agent_task(
    task_id: str,
    name: Optional[str] = None,
    status: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Update an agent task"""
    existing = get_agent_task(task_id)
    if not existing:
        return None

    updates = ["updated_at = ?"]
    values = [datetime.utcnow().isoformat()]

    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if status is not None:
        updates.append("status = ?")
        values.append(status)

    values.append(task_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE agent_tasks SET {', '.join(updates)} WHERE id = ?",
            values
        )

    return get_agent_task(task_id)


def delete_agent_task(task_id: str) -> bool:
    """Delete an agent task"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0


# ============================================================================
# Agent Log Operations
# ============================================================================

def add_agent_log(
    agent_run_id: str,
    message: str,
    level: str = "info",
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Add a log entry for an agent run"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agent_logs (agent_run_id, timestamp, level, message, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (agent_run_id, now, level, message, json.dumps(metadata) if metadata else None)
        )
        return {
            "id": cursor.lastrowid,
            "agent_run_id": agent_run_id,
            "timestamp": now,
            "level": level,
            "message": message,
            "metadata": metadata
        }


def get_agent_logs(
    agent_run_id: str,
    level: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get logs for an agent run"""
    query = "SELECT * FROM agent_logs WHERE agent_run_id = ?"
    params = [agent_run_id]

    if level:
        query += " AND level = ?"
        params.append(level)

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = rows_to_list(cursor.fetchall())
        # Parse JSON metadata
        for row in rows:
            if row.get("metadata") and isinstance(row["metadata"], str):
                try:
                    row["metadata"] = json.loads(row["metadata"])
                except json.JSONDecodeError:
                    pass
        return rows


def get_agent_logs_count(agent_run_id: str, level: Optional[str] = None) -> int:
    """Get count of logs for an agent run"""
    query = "SELECT COUNT(*) as count FROM agent_logs WHERE agent_run_id = ?"
    params = [agent_run_id]

    if level:
        query += " AND level = ?"
        params.append(level)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return row["count"]


def clear_agent_logs(agent_run_id: str) -> int:
    """Clear all logs for an agent run. Returns count of deleted logs."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agent_logs WHERE agent_run_id = ?", (agent_run_id,))
        return cursor.rowcount


def get_agent_run_stats(
    days: int = 7,
    project_id: Optional[str] = None
) -> Dict[str, Any]:
    """Get statistics about agent runs for the specified period"""
    from datetime import timedelta

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    base_where = "WHERE started_at >= ?"
    params = [cutoff]

    if project_id:
        base_where += " AND project_id = ?"
        params.append(project_id)

    with get_db() as conn:
        cursor = conn.cursor()

        # Total count
        cursor.execute(f"SELECT COUNT(*) as count FROM agent_runs {base_where}", params)
        total = cursor.fetchone()["count"]

        # By status
        cursor.execute(
            f"""SELECT status, COUNT(*) as count
                FROM agent_runs {base_where}
                GROUP BY status""",
            params
        )
        by_status = {row["status"]: row["count"] for row in cursor.fetchall()}

        # Calculate success rate
        completed = by_status.get("completed", 0)
        failed = by_status.get("failed", 0)
        finished = completed + failed
        success_rate = (completed / finished * 100) if finished > 0 else 0

        # Average duration (for completed runs)
        cursor.execute(
            f"""SELECT AVG(
                    (julianday(completed_at) - julianday(started_at)) * 24 * 60
                ) as avg_minutes
                FROM agent_runs
                {base_where} AND status = 'completed' AND completed_at IS NOT NULL""",
            params
        )
        row = cursor.fetchone()
        avg_duration_minutes = row["avg_minutes"] if row["avg_minutes"] else 0

        # Runs by day
        cursor.execute(
            f"""SELECT date(started_at) as day, COUNT(*) as count
                FROM agent_runs {base_where}
                GROUP BY date(started_at)
                ORDER BY day""",
            params
        )
        by_day = {row["day"]: row["count"] for row in cursor.fetchall()}

        return {
            "total": total,
            "by_status": by_status,
            "success_rate": round(success_rate, 1),
            "avg_duration_minutes": round(avg_duration_minutes, 1),
            "by_day": by_day,
            "running": by_status.get("running", 0),
            "queued": by_status.get("queued", 0),
            "completed": completed,
            "failed": failed
        }
