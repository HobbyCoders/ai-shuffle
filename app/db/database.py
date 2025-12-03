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
SCHEMA_VERSION = 7


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
            api_key_hash TEXT NOT NULL,
            project_id TEXT,
            profile_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

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

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_created ON usage_log(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_users_active ON api_users(is_active)")
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
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get sessions with optional filters"""
    query = "SELECT * FROM sessions WHERE 1=1"
    params = []

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if profile_id:
        query += " AND profile_id = ?"
        params.append(profile_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if api_users_only:
        # Filter for sessions that have an API user (exclude admin sessions)
        query += " AND api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            query += " AND api_user_id = ?"
            params.append(api_user_id)
        else:
            query += " AND api_user_id IS NULL"

    query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
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
    api_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new session"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sessions (id, profile_id, project_id, title, api_user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, project_id, title, api_user_id, now, now)
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
    description: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new API user"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO api_users (id, name, api_key_hash, project_id, profile_id, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, api_key_hash, project_id, profile_id, description, now, now)
        )
    return get_api_user(user_id)


def update_api_user(
    user_id: str,
    name: Optional[str] = None,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None
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
