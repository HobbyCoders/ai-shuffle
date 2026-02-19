"""Mobile device, action, schedule, and trusted action database operations."""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class MobileDeviceRow(TypedDict):
    """Full mobile device row from SELECT * FROM mobile_devices."""
    id: str
    device_id: str
    device_name: str
    platform: str
    api_key_hash: str
    fcm_token: Optional[str]
    capabilities: Dict[str, Any]
    created_at: str
    last_seen: Optional[str]


class PendingActionRow(TypedDict):
    """Full pending action row from SELECT * FROM pending_actions."""
    id: str
    device_id: str
    action_type: str
    params: Dict[str, Any]
    security_level: str
    created_at: str
    expires_at: str
    status: str


class TrustedActionRow(TypedDict):
    """Full trusted action row from SELECT * FROM trusted_actions."""
    id: str
    device_id: str
    action_pattern: str
    auto_approve: bool
    require_preview: bool
    created_at: str


class MobileScheduleRow(TypedDict):
    """Full mobile schedule row from SELECT * FROM mobile_schedules."""
    id: str
    device_id: str
    schedule_type: str
    trigger_time: Optional[str]
    hour: Optional[int]
    minute: Optional[int]
    params: str
    enabled: bool
    created_at: str
    last_triggered_at: Optional[str]


# ============================================================================
# Mobile Device Operations
# ============================================================================

def create_mobile_device(
    device_id: str,
    device_name: str,
    platform: str,
    api_key_hash: str,
    capabilities: Dict[str, Any]
) -> Optional[MobileDeviceRow]:
    """Register a new mobile device"""
    import uuid
    record_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mobile_devices (id, device_id, device_name, platform, api_key_hash, capabilities, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (record_id, device_id, device_name, platform, api_key_hash, json.dumps(capabilities), now, now))

    return get_mobile_device(device_id)


def get_mobile_device(device_id: str) -> Optional[MobileDeviceRow]:
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


def get_mobile_device_by_api_key_hash(api_key_hash: str) -> Optional[MobileDeviceRow]:
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


def get_all_mobile_devices() -> List[MobileDeviceRow]:
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


def update_mobile_device_capabilities(device_id: str, capabilities: Dict[str, Any]) -> Optional[MobileDeviceRow]:
    """Update capabilities for a mobile device"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE mobile_devices SET capabilities = ?, last_seen = ? WHERE device_id = ?
        """, (json.dumps(capabilities), now, device_id))
        if cursor.rowcount == 0:
            return None
    return get_mobile_device(device_id)


def update_mobile_device_fcm_token(device_id: str, fcm_token: str) -> Optional[MobileDeviceRow]:
    """Update FCM push token for a mobile device"""
    now = datetime.utcnow().isoformat()
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
    now = datetime.utcnow().isoformat()
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
) -> Optional[PendingActionRow]:
    """Create a new pending action"""
    import uuid
    action_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO pending_actions (id, device_id, action_type, params, security_level, created_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (action_id, device_id, action_type, json.dumps(params), security_level, now, expires_at))

    return get_pending_action(action_id)


def get_pending_action(action_id: str) -> Optional[PendingActionRow]:
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


def get_pending_actions_for_device(device_id: str) -> List[PendingActionRow]:
    """Get all pending actions for a device that haven't expired"""
    now = datetime.utcnow().isoformat()
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


def update_pending_action_status(action_id: str, status: str) -> Optional[PendingActionRow]:
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
    now = datetime.utcnow().isoformat()
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
) -> Optional[TrustedActionRow]:
    """Create a trusted action pattern"""
    import uuid
    trusted_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trusted_actions (id, device_id, action_pattern, auto_approve, require_preview, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (trusted_id, device_id, action_pattern, auto_approve, require_preview, now))

    return get_trusted_action(trusted_id)


def get_trusted_action(trusted_id: str) -> Optional[TrustedActionRow]:
    """Get a trusted action by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trusted_actions WHERE id = ?", (trusted_id,))
        return row_to_dict(cursor.fetchone())


def get_trusted_actions_for_device(device_id: str) -> List[TrustedActionRow]:
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

def get_mobile_schedules() -> List[MobileScheduleRow]:
    """Get all mobile schedules"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_schedules ORDER BY created_at DESC")
        return rows_to_list(cursor.fetchall())


def get_mobile_schedule(schedule_id: str) -> Optional[MobileScheduleRow]:
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
) -> Optional[MobileScheduleRow]:
    """Save or update a mobile schedule"""
    now = datetime.utcnow().isoformat()
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

def get_mobile_actions() -> List[PendingActionRow]:
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
) -> Optional[PendingActionRow]:
    """Save or update a mobile action"""
    now = datetime.utcnow().isoformat()
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

def get_mobile_devices() -> List[MobileDeviceRow]:
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
) -> Optional[MobileDeviceRow]:
    """Save or update a mobile device"""
    now = datetime.utcnow().isoformat()
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
