"""User preferences and system settings database operations."""

from __future__ import annotations

import json
import threading
import time
from uuid import uuid4
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list


# ============================================================================
# TypedDict definitions
# ============================================================================

class UserPreferenceRow(TypedDict):
    """User preference row."""
    user_type: str
    user_id: str
    key: str
    value: Any
    updated_at: str
    revision: Optional[str]


# ============================================================================
# User Preferences Operations
# ============================================================================

# Read JSONB as JSON text so the code below decodes exactly once. Psycopg
# otherwise already decodes it, and json.loads("1") corrupts a string preference
# (notably composer.fastMode) into a number on both SELECT and RETURNING paths.
_PREFERENCE_COLUMNS = (
    "id, user_type, user_id, key, CAST(value AS TEXT) AS value, updated_at, revision"
)


def get_user_preference(user_type: str, user_id: str, key: str) -> Optional[UserPreferenceRow]:
    """Get a user preference by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_PREFERENCE_COLUMNS} FROM user_preferences WHERE user_type = %s AND user_id = %s AND key = %s",
            (user_type, user_id, key)
        )
        row = cursor.fetchone()
        if row:
            result = row_to_dict(row)
            # Parse JSON value
            if result and result.get("value"):
                raw = result["value"]
                if isinstance(raw, str):
                    try:
                        result["value"] = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        # Legacy bare-string value stored before preference values
                        # were JSON-encoded (e.g. timezone "America/New_York").
                        # Tolerate on read: use the raw string as the value.
                        result["value"] = raw
            return result
        return None


_UNCONDITIONAL = object()


class PreferenceConflict(ValueError):
    """The user edited a preference snapshot that is no longer current."""


def set_user_preference(user_type: str, user_id: str, key: str, value: Any, only_if_missing: bool = False,
                        *, expected_version=_UNCONDITIONAL) -> UserPreferenceRow:
    """Upsert with optional optimistic concurrency independent of wall clock.

    Omitted version supports legacy clients; explicit null requires a missing
    row. A supplied UUID must match the row owned by this exact user and key.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value)
        revision = str(uuid4())

        if only_if_missing or expected_version is None:
            cursor.execute(
                f"""INSERT INTO user_preferences (user_type, user_id, key, value, updated_at, revision)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT(user_type, user_id, key) DO NOTHING RETURNING {_PREFERENCE_COLUMNS}""",
                (user_type, user_id, key, value_json, now, revision),
            )
        elif expected_version is not _UNCONDITIONAL:
            cursor.execute(
                f"""UPDATE user_preferences SET value = %s, updated_at = %s, revision = %s
                   WHERE user_type = %s AND user_id = %s AND key = %s AND revision = %s RETURNING {_PREFERENCE_COLUMNS}""",
                (value_json, now, revision, user_type, user_id, key, expected_version))
        else:
            cursor.execute(
                f"""INSERT INTO user_preferences (user_type, user_id, key, value, updated_at, revision)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT(user_type, user_id, key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at,
                       revision = excluded.revision RETURNING {_PREFERENCE_COLUMNS}""",
                (user_type, user_id, key, value_json, now, revision)
            )
        result = row_to_dict(cursor.fetchone())
        inserted = result is not None
        if result is None:
            if not only_if_missing:
                raise PreferenceConflict('Preference changed elsewhere; refresh before editing')
            cursor.execute(
                f"SELECT {_PREFERENCE_COLUMNS} FROM user_preferences WHERE user_type = %s AND user_id = %s AND key = %s",
                (user_type, user_id, key),
            )
            result = row_to_dict(cursor.fetchone())
        if isinstance(result["value"], str):
            try:
                result["value"] = json.loads(result["value"])
            except (ValueError, TypeError):
                pass

    if inserted:
        from app.core.global_events import global_events
        global_events.preference_changed(user_type, user_id, key)
    return result


def delete_user_preference(user_type: str, user_id: str, key: str) -> bool:
    """Delete a user preference"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_preferences WHERE user_type = %s AND user_id = %s AND key = %s",
            (user_type, user_id, key)
        )
        deleted = cursor.rowcount > 0
    if deleted:
        from app.core.global_events import global_events
        global_events.preference_changed(user_type, user_id, key)
    return deleted


def get_all_user_preferences(user_type: str, user_id: str) -> List[UserPreferenceRow]:
    """Get all preferences for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_PREFERENCE_COLUMNS} FROM user_preferences WHERE user_type = %s AND user_id = %s",
            (user_type, user_id)
        )
        rows = rows_to_list(cursor.fetchall())
        # Parse JSON values
        for row in rows:
            if row.get("value"):
                raw = row["value"]
                if isinstance(raw, str):
                    try:
                        row["value"] = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        # Legacy bare-string value stored before preference values
                        # were JSON-encoded. Tolerate on read: use the raw string.
                        row["value"] = raw
        return rows


# ============================================================================
# System Settings Operations
# ============================================================================

def get_system_setting(key: str) -> Optional[str]:
    """Get a system setting by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None


# ---------------------------------------------------------------------------
# Short-TTL memo for settings read on hot paths
# ---------------------------------------------------------------------------
#
# A handful of settings are consulted per REQUEST rather than per user action —
# ``cluster_share_sessions`` is read by every ``get_sessions*`` /
# ``search_sessions*`` call to decide whether to add the per-install filter;
# unmemoized, every sidebar refresh would pay a dedicated pool checkout +
# SELECT + commit for a value that changes approximately never.
#
# The memo is per-process and deliberately short-lived: writes THROUGH this
# module invalidate it immediately (so an in-process change is visible at once),
# and the TTL bounds how long another install's write to the shared database can
# go unnoticed. Reads that must be exact use :func:`get_system_setting`.
SYSTEM_SETTING_MEMO_TTL_SECONDS = 5.0

# key -> (expires_at_monotonic, value)
_setting_memo: Dict[str, tuple] = {}
_setting_memo_lock = threading.Lock()


def invalidate_system_setting_memo(key: Optional[str] = None) -> None:
    """Drop memoized system settings (one key, or all when ``key`` is None)."""
    with _setting_memo_lock:
        if key is None:
            _setting_memo.clear()
        else:
            _setting_memo.pop(key, None)


def get_system_setting_cached(
    key: str, ttl: float = SYSTEM_SETTING_MEMO_TTL_SECONDS
) -> Optional[str]:
    """Memoized :func:`get_system_setting` for per-request hot paths.

    Serves a value cached up to ``ttl`` seconds old. Misses (``None``) are
    memoized too — an unset setting is the common case and re-querying it on
    every call is exactly the overhead this exists to remove. Any write through
    :func:`set_system_setting` / :func:`delete_system_setting` /
    :func:`get_or_create_system_setting` invalidates the key immediately.
    """
    now = time.monotonic()
    with _setting_memo_lock:
        entry = _setting_memo.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]

    value = get_system_setting(key)

    with _setting_memo_lock:
        _setting_memo[key] = (time.monotonic() + ttl, value)
    return value


def get_system_setting_revision(key: str) -> Optional[str]:
    """Opaque write generation, without disclosing the setting's value."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT revision FROM system_settings WHERE key = %s", (key,))
        row = cursor.fetchone()
        return row["revision"] if row else None


def set_system_setting(key: str, value: str) -> None:
    """Set a system setting (upsert)"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at, revision)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at,
                   revision = excluded.revision""",
            (key, value, now, str(uuid4()))
        )
        if key == "fal_billing_api_key":
            # Same transaction and lock order as conditional billing writes.
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", ("fal-ai",))
        if key.startswith("openrouter_billing_key:"):
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s",
                           ("openrouter-account:" + key.removeprefix("openrouter_billing_key:"),))
    # Explicit invalidation: the memo must never outlive a write we made.
    invalidate_system_setting_memo(key)


def delete_system_setting(key: str) -> bool:
    """Delete a system setting"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = %s", (key,))
        deleted = cursor.rowcount > 0
        if key == "fal_billing_api_key":
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", ("fal-ai",))
        if key.startswith("openrouter_billing_key:"):
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s",
                           ("openrouter-account:" + key.removeprefix("openrouter_billing_key:"),))
    invalidate_system_setting_memo(key)
    return deleted


def get_or_create_system_setting(key: str, default_factory) -> str:
    """Return the stored value for ``key``, atomically seeding it if absent.

    Unlike ``set_system_setting`` (which clobbers via DO UPDATE), this uses
    ON CONFLICT DO NOTHING then re-reads, so concurrent installs booting against
    the same shared database converge on a single first-written value (used for
    the auto-generated cluster secret). ``default_factory`` is a zero-arg
    callable producing the seed value; it is only consulted when seeding.
    """
    existing = get_system_setting(key)
    if existing:
        return existing
    now = datetime.now(timezone.utc).isoformat()
    value = default_factory()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at, revision)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(key) DO NOTHING""",
            (key, value, now, str(uuid4())),
        )
    # Re-read: another install may have won the insert race, so return the
    # value that actually landed rather than our candidate.
    invalidate_system_setting_memo(key)
    return get_system_setting(key) or value


def get_all_system_settings() -> Dict[str, str]:
    """Get all system settings as a dictionary"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings")
        rows = cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}
