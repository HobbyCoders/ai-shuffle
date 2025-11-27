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
SCHEMA_VERSION = 1


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
            FOREIGN KEY (profile_id) REFERENCES profiles(id)
        )
    """)

    # Session messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSON,
            metadata JSON,
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

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_created ON usage_log(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)")


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
    config: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Update a profile"""
    existing = get_profile(profile_id)
    if not existing or existing["is_builtin"]:
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
    """Delete a profile (only non-builtin)"""
    existing = get_profile(profile_id)
    if not existing or existing["is_builtin"]:
        return False

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM profiles WHERE id = ? AND is_builtin = FALSE", (profile_id,))
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
    title: Optional[str] = None
) -> Dict[str, Any]:
    """Create a new session"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sessions (id, profile_id, project_id, title, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, project_id, title, now, now)
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
