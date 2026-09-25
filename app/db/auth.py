"""
Auth domain database operations.

Handles: Admin, Audit Log, Auth Sessions,
Login Attempts, Account Lockouts, and API Key Sessions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class AdminRow(TypedDict):
    """Full admin row from SELECT * FROM admin."""
    id: int
    username: str
    password_hash: str
    created_at: str


class AdminCreateResult(TypedDict):
    """Result of creating an admin user."""
    id: int
    username: str


class AuditLogRow(TypedDict):
    """Full audit log row from SELECT * FROM audit_log."""
    id: str
    user_id: Optional[str]
    user_type: str
    event_type: str
    ip_address: Optional[str]
    user_agent: Optional[str]
    details: Optional[Dict[str, Any]]
    created_at: str


class AuthSessionRow(TypedDict):
    """Full auth session row from SELECT * FROM auth_sessions."""
    token: str
    expires_at: str
    created_at: str


class ApiKeySessionRow(TypedDict):
    """API key session with joined api_user fields."""
    token: str
    api_user_id: str
    expires_at: str
    created_at: str
    user_name: str
    project_id: Optional[str]
    is_active: bool
    profile_ids: Optional[List[str]]


# ============================================================================
# Admin Operations
# ============================================================================

def is_setup_required() -> bool:
    from app.core.owner_auth import setup_required
    return setup_required()


def get_admin() -> Optional[AdminRow]:
    """Get admin user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admin LIMIT 1")
        return row_to_dict(cursor.fetchone())


def create_admin(username: str, password_hash: str) -> AdminCreateResult:
    """Create admin user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO admin (id, username, password_hash) VALUES (1, %s, %s)",
            (username, password_hash)
        )
        return {"id": 1, "username": username}


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
) -> AuditLogRow:
    """Create an audit log entry"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO audit_log (id, user_id, user_type, event_type, ip_address, user_agent, details)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
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
) -> List[AuditLogRow]:
    """Get audit logs with optional filters"""
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []

    if user_id:
        query += " AND user_id = %s"
        params.append(user_id)
    if event_type:
        query += " AND event_type = %s"
        params.append(event_type)

    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
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
        query += " AND user_id = %s"
        params.append(user_id)
    if event_type:
        query += " AND event_type = %s"
        params.append(event_type)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return row["count"]


# ============================================================================
# Auth Session Operations
# ============================================================================

def create_auth_session(token: str, expires_at: datetime) -> AuthSessionRow:
    """Create an auth session token"""
    logger.info(
        f"create_auth_session: Creating token ...{token[-4:]} "
        f"(expires_at={expires_at.isoformat()})"
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO auth_sessions (token, expires_at) VALUES (%s, %s)",
            (token, expires_at.isoformat())
        )
        return {"token": token, "expires_at": expires_at}


def get_auth_session(token: str) -> Optional[AuthSessionRow]:
    """Legacy password sessions never qualify; only console-issued recovery."""
    from app.core.owner_auth import local_session
    return local_session(token)


def delete_auth_session(token: str):
    from app.core.owner_auth import revoke_local_session
    revoke_local_session(token)


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


# ============================================================================
# Login Attempt Operations
# ============================================================================


# ============================================================================
# Account Lockout Operations
# ============================================================================


# ============================================================================
# API Key Session Operations
# ============================================================================

def _get_api_user_profile_ids(cursor, user_id: str) -> List[str]:
    """Get profile IDs for an API user from junction table"""
    cursor.execute(
        "SELECT profile_id FROM api_user_profiles WHERE api_user_id = %s ORDER BY created_at",
        (user_id,)
    )
    return [row["profile_id"] for row in cursor.fetchall()]


def cleanup_expired_sessions() -> dict:
    """Remove expired auth sessions, API key sessions, login attempts, and lockouts.

    This should be called periodically (e.g. every hour) to prevent unbounded
    growth of the auth-related tables. Each cleanup is idempotent and safe
    to run concurrently.

    Returns:
        dict with counts of rows deleted from each table.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    results = {}

    with get_db() as conn:
        cursor = conn.cursor()

        # Expired admin sessions
        cursor.execute("DELETE FROM auth_sessions WHERE expires_at <= %s", (now_iso,))
        results["auth_sessions"] = cursor.rowcount

        # Expired API key sessions
        cursor.execute("DELETE FROM api_key_sessions WHERE expires_at <= %s", (now_iso,))
        results["api_key_sessions"] = cursor.rowcount

        # Expired account lockouts (locked_until has passed)
        cursor.execute("DELETE FROM account_lockouts WHERE locked_until <= %s", (now_iso,))
        results["account_lockouts"] = cursor.rowcount

        # Old login attempts (older than 24 hours — well past the 15-minute window)
        from datetime import timedelta
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cursor.execute("DELETE FROM login_attempts WHERE created_at < %s", (cutoff_24h,))
        results["login_attempts"] = cursor.rowcount

    total = sum(results.values())
    if total > 0:
        logger.info(f"Session cleanup: removed {results}")
    else:
        logger.debug("Session cleanup: nothing to remove")

    return results


def get_api_key_session(token: str) -> Optional[ApiKeySessionRow]:
    """Human web sessions for integration users are retired; use scoped API keys."""
    return None
