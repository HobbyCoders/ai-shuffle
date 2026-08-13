"""
Auth domain database operations.

Handles: Admin, Audit Log, Auth Sessions,
Login Attempts, Account Lockouts, and API Key Sessions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger
from app.core.config import settings


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


class AccountLockoutRow(TypedDict):
    """Full account lockout row from SELECT * FROM account_lockouts."""
    id: int
    ip_address: Optional[str]
    username: Optional[str]
    locked_until: str
    reason: Optional[str]
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
    """Check if admin setup is required"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM admin")
        row = cursor.fetchone()
        return row["count"] == 0


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


def update_admin_password(password_hash: str) -> bool:
    """Update admin password"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE admin SET password_hash = %s WHERE id = 1",
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


_auth_session_empty_logged = False  # Rate-limit the verbose empty-table diagnostic


def get_auth_session(token: str) -> Optional[AuthSessionRow]:
    """Get an auth session by token"""
    global _auth_session_empty_logged
    with get_db() as conn:
        cursor = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "SELECT * FROM auth_sessions WHERE token = %s AND expires_at > %s",
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
                "SELECT token, expires_at, created_at FROM auth_sessions WHERE token = %s",
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
                cursor.execute("SELECT COUNT(*) as cnt FROM agents")
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
        cursor.execute("DELETE FROM auth_sessions WHERE token = %s", (token,))


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

def record_login_attempt(ip_address: str, username: Optional[str], success: bool):
    """Record a login attempt for rate limiting"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO login_attempts (ip_address, username, success, created_at)
               VALUES (%s, %s, %s, %s)""",
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
               WHERE ip_address = %s AND success = FALSE AND created_at > %s""",
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
               WHERE username = %s AND success = FALSE AND created_at > %s""",
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
               VALUES (%s, %s, %s, %s, %s)""",
            (ip_address, username, locked_until, reason, datetime.now(timezone.utc).isoformat())
        )


def is_ip_locked(ip_address: str) -> Optional[AccountLockoutRow]:
    """Check if an IP address is currently locked out"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM account_lockouts
               WHERE ip_address = %s AND locked_until > %s
               ORDER BY locked_until DESC LIMIT 1""",
            (ip_address, now)
        )
        return row_to_dict(cursor.fetchone())


def is_username_locked(username: str) -> Optional[AccountLockoutRow]:
    """Check if a username is currently locked out"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM account_lockouts
               WHERE username = %s AND locked_until > %s
               ORDER BY locked_until DESC LIMIT 1""",
            (username, now)
        )
        return row_to_dict(cursor.fetchone())


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
    """Get an API key session by token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT aks.*, au.name as user_name, au.project_id, au.is_active
               FROM api_key_sessions aks
               JOIN api_users au ON aks.api_user_id = au.id
               WHERE aks.token = %s AND aks.expires_at > %s AND au.is_active = TRUE""",
            (token, datetime.now(timezone.utc).isoformat())
        )
        session = row_to_dict(cursor.fetchone())
        if session:
            # Get profile_ids from junction table
            profile_ids = _get_api_user_profile_ids(cursor, session["api_user_id"])
            session["profile_ids"] = profile_ids if profile_ids else None
        return session
