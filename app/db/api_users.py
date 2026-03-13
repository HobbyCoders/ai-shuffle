"""API user, credential, and credential policy database operations."""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class ApiUserRow(TypedDict):
    """Full API user row with enriched profile_ids."""
    id: str
    name: str
    username: Optional[str]
    password_hash: Optional[str]
    api_key_hash: str
    project_id: Optional[str]
    profile_id: Optional[str]
    is_active: bool
    web_login_allowed: bool
    description: Optional[str]
    created_at: str
    updated_at: str
    last_used_at: Optional[str]
    profile_ids: Optional[List[str]]


class CredentialPolicyRow(TypedDict):
    """Credential policy row from SELECT * FROM credential_policies."""
    id: str
    policy: str
    description: Optional[str]
    updated_at: str


class UserCredentialPolicyRow(TypedDict):
    """User credential policy override row."""
    id: str
    api_user_id: str
    credential_type: str
    policy: str
    created_at: str
    updated_at: str


class EffectiveCredentialPolicy(TypedDict):
    """Effective credential policy result."""
    policy: str
    source: str
    credential_type: str


class UserCredentialRow(TypedDict):
    """API user credential row."""
    id: str
    api_user_id: str
    credential_type: str
    encrypted_value: str
    created_at: str
    updated_at: str


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


def _enrich_api_user_with_profiles(cursor, user: Optional[Dict[str, Any]]) -> Optional[ApiUserRow]:
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
) -> ApiUserRow:
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


def get_api_user(user_id: str) -> Optional[ApiUserRow]:
    """Get an API user by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_users WHERE id = ?", (user_id,))
        user = row_to_dict(cursor.fetchone())
        return _enrich_api_user_with_profiles(cursor, user)


def get_api_user_by_key_hash(key_hash: str) -> Optional[ApiUserRow]:
    """Get an API user by API key hash"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_users WHERE api_key_hash = ? AND is_active = TRUE",
            (key_hash,)
        )
        user = row_to_dict(cursor.fetchone())
        return _enrich_api_user_with_profiles(cursor, user)


def get_api_users_by_ids(user_ids: list[str]) -> dict[str, dict]:
    """Get multiple API users by their IDs in a single query.

    Returns {user_id: user_dict} for all found users.
    Missing users are omitted from the result.
    """
    if not user_ids:
        return {}
    unique_ids = list(set(user_ids))
    placeholders = ','.join('?' * len(unique_ids))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM api_users WHERE id IN ({placeholders})",
            unique_ids
        )
        users = rows_to_list(cursor.fetchall())
        result: dict[str, dict] = {}
        for user in users:
            result[user['id']] = user
        return result


def get_api_user_names_for_sessions(session_ids: list[str], sessions: list[dict]) -> dict[str, str]:
    """Get API user display names for sessions that have an api_user_id.

    Args:
        session_ids: List of session IDs (used for result key alignment)
        sessions: List of session dicts containing api_user_id fields

    Returns {api_user_id: display_name} for all unique API user IDs found.
    """
    api_user_ids = list({s["api_user_id"] for s in sessions if s.get("api_user_id")})
    if not api_user_ids:
        return {}
    users = get_api_users_by_ids(api_user_ids)
    return {
        uid: user.get("name") or user.get("username") or uid
        for uid, user in users.items()
    }


def update_api_user_last_used(user_id: str):
    """Update the last_used_at timestamp for an API user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE api_users SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id)
        )


def get_all_api_users(active_only: bool = False) -> List[ApiUserRow]:
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


def update_api_user(user_id: str, **kwargs) -> Optional[ApiUserRow]:
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

def get_all_credential_policies() -> List[CredentialPolicyRow]:
    """Get all credential policies"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM credential_policies ORDER BY id")
        return rows_to_list(cursor.fetchall())


def get_credential_policy(policy_id: str) -> Optional[CredentialPolicyRow]:
    """Get a specific credential policy"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM credential_policies WHERE id = ?", (policy_id,))
        return row_to_dict(cursor.fetchone())


def update_credential_policy(policy_id: str, policy: str, description: Optional[str] = None) -> Optional[CredentialPolicyRow]:
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

def get_user_credential_policy(api_user_id: str, credential_type: str) -> Optional[UserCredentialPolicyRow]:
    """Get a user's policy override for a specific credential type"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_credential_policies WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return row_to_dict(cursor.fetchone())


def get_all_user_credential_policies(api_user_id: str) -> List[UserCredentialPolicyRow]:
    """Get all policy overrides for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_credential_policies WHERE api_user_id = ? ORDER BY credential_type",
            (api_user_id,)
        )
        return rows_to_list(cursor.fetchall())


def set_user_credential_policy(api_user_id: str, credential_type: str, policy: str) -> Optional[UserCredentialPolicyRow]:
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


def get_effective_credential_policy(api_user_id: str, credential_type: str) -> EffectiveCredentialPolicy:
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

def get_user_credential(api_user_id: str, credential_type: str) -> Optional[UserCredentialRow]:
    """Get a specific credential for a user (returns encrypted value)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_user_credentials WHERE api_user_id = ? AND credential_type = ?",
            (api_user_id, credential_type)
        )
        return row_to_dict(cursor.fetchone())


def get_all_user_credentials(api_user_id: str) -> List[UserCredentialRow]:
    """Get all credentials for a user (returns encrypted values)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM api_user_credentials WHERE api_user_id = ? ORDER BY credential_type",
            (api_user_id,)
        )
        return rows_to_list(cursor.fetchall())


def set_user_credential(api_user_id: str, credential_type: str, encrypted_value: str) -> UserCredentialRow:
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
