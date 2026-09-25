"""Mobile device and pending action database operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db


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
    unifiedpush_endpoint: Optional[str]
    capabilities: Dict[str, Any]
    paired_client_id: Optional[str]
    api_key_expires_at: Optional[str]
    scopes: List[str]
    paired_at: Optional[str]
    revoked_at: Optional[str]
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


def _parse_capabilities(raw: Any) -> Dict[str, Any]:
    """Parse capabilities field, handling both str and dict (JSONB)."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if raw else {}


def _parse_params(raw: Any) -> Dict[str, Any]:
    """Parse params field, handling both str and dict (JSONB)."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if raw else {}


def _parse_scopes(raw: Any) -> List[str]:
    """Parse a JSON/JSONB scope list without trusting malformed rows."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(scope) for scope in raw if isinstance(scope, str)]


def _parse_mobile_device(row: Any) -> Optional[MobileDeviceRow]:
    if row is None:
        return None
    result = dict(row)
    result["capabilities"] = _parse_capabilities(result.get("capabilities"))
    result["scopes"] = _parse_scopes(result.get("scopes"))
    return result  # type: ignore[return-value]


# ============================================================================
# Mobile Device Operations
# ============================================================================

def mobile_key_renewal_device(key_hash):
    """Previous keys are accepted ONLY by renewal, never ordinary API auth."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT d.* FROM mobile_key_renewals r
            JOIN mobile_devices d ON d.device_id=r.device_id AND d.id=r.mobile_record_id
                AND d.api_key_hash=r.current_hash
            WHERE r.previous_hash=%s AND r.previous_expires_at>%s""",
            (key_hash, datetime.now(timezone.utc).isoformat()))
        return _parse_mobile_device(c.fetchone())


def renew_mobile_key(device, presented_hash, new_key):
    """Compare-and-swap with a recoverable receipt until the OLD key's expiry.

    A lost response or simultaneous renewal returns the exact same replacement;
    it never extends the old key or permits it on the API. Pairing/repair changes
    the record/key, so a stale receipt cannot undo revocation or re-enrollment.
    """
    import hashlib
    from datetime import timedelta

    from app.core.device_keys import KEY_LIFETIME_DAYS, RENEW_BEFORE_DAYS, key_expiry
    from app.core.encryption import decrypt_value, encrypt_value
    with get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM paired_clients WHERE id=%s FOR UPDATE', (device['paired_client_id'],))
        client = c.fetchone()
        c.execute('SELECT * FROM mobile_devices WHERE id=%s FOR UPDATE', (device['id'],))
        row = c.fetchone()
        if (not client or client['revoked_at'] or not row or row['revoked_at']
                or row['paired_client_id'] != client['id'] or row['device_id'] != client['device_id']):
            raise ValueError('Device credential changed; pair this device again')
        # Re-check under the same client lock used by decisions, without taking
        # a second pool connection while this transaction holds row locks.
        for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
            c.execute(f"""SELECT a.paired_client_id, f.decision, f.mobile_record_id
                FROM {table} a LEFT JOIN mobile_worker_confirmations f
                ON f.device_id=a.device_id AND f.server_id=a.server_id
                    AND f.paired_client_id=a.paired_client_id
                WHERE a.device_id=%s""", (device['device_id'],))
            approval = c.fetchone()
            if approval and (approval['decision'] != 'approved'
                    or approval['paired_client_id'] != client['id']
                    or approval['mobile_record_id'] != row['id']):
                raise ValueError('Confirm this device on the worker before connecting')
        current = dict(row)
        now = datetime.now(timezone.utc)
        expiry = key_expiry(current)
        if expiry <= now:
            raise ValueError('Device credential expired; pair this device again')
        if current['api_key_hash'] != presented_hash:
            c.execute('SELECT * FROM mobile_key_renewals WHERE device_id=%s', (device['device_id'],))
            receipt = c.fetchone()
            if (not receipt or receipt['previous_hash'] != presented_hash
                    or receipt['current_hash'] != current['api_key_hash']
                    or receipt['mobile_record_id'] != current['id']
                    or datetime.fromisoformat(receipt['previous_expires_at']) <= now):
                raise ValueError('Device credential changed; pair this device again')
            return decrypt_value(receipt['encrypted_key']), expiry
        if expiry > now + timedelta(days=RENEW_BEFORE_DAYS):
            return None, expiry
        key_hash = hashlib.sha256(new_key.encode()).hexdigest()
        encrypted = encrypt_value(new_key)
        new_expiry = now + timedelta(days=KEY_LIFETIME_DAYS)
        c.execute("""INSERT INTO mobile_key_renewals
            (device_id, mobile_record_id, previous_hash, current_hash, encrypted_key, previous_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(device_id) DO UPDATE SET
            mobile_record_id=excluded.mobile_record_id, previous_hash=excluded.previous_hash,
            current_hash=excluded.current_hash, encrypted_key=excluded.encrypted_key,
            previous_expires_at=excluded.previous_expires_at""",
            (device['device_id'], current['id'], presented_hash, key_hash, encrypted, expiry.isoformat()))
        c.execute('UPDATE mobile_devices SET api_key_hash=%s, api_key_expires_at=%s WHERE id=%s',
                  (key_hash, new_expiry.isoformat(), current['id']))
        # Pending mints are tied to the old key. Existing sessions remain device
        # bound and are still checked for expiry/revocation/confirmation per use.
        c.execute('DELETE FROM paired_client_web_reservations WHERE client_id=%s', (client['id'],))
        return new_key, new_expiry


def create_mobile_device(
    device_id: str,
    device_name: str,
    platform: str,
    api_key_hash: str,
    capabilities: Dict[str, Any],
    *,
    paired_client_id: Optional[str] = None,
    api_key_expires_at: Optional[datetime] = None,
    scopes: Optional[List[str]] = None,
) -> Optional[MobileDeviceRow]:
    """Register a new mobile device"""
    import uuid
    record_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mobile_devices
                (id, device_id, device_name, platform, api_key_hash,
                 capabilities, paired_client_id, api_key_expires_at, scopes,
                 paired_at, created_at, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            record_id,
            device_id,
            device_name,
            platform,
            api_key_hash,
            json.dumps(capabilities),
            paired_client_id,
            api_key_expires_at.isoformat() if api_key_expires_at else None,
            json.dumps(scopes or []),
            now,
            now,
            now,
        ))

    return get_mobile_device(device_id)


def get_mobile_device(device_id: str) -> Optional[MobileDeviceRow]:
    """Get a mobile device by device_id"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_devices WHERE device_id = %s", (device_id,))
        return _parse_mobile_device(cursor.fetchone())


def get_mobile_device_by_api_key_hash(api_key_hash: str) -> Optional[MobileDeviceRow]:
    """Get a mobile device by API key hash"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_devices WHERE api_key_hash = %s", (api_key_hash,))
        return _parse_mobile_device(cursor.fetchone())


def get_all_mobile_devices() -> List[MobileDeviceRow]:
    """Get all registered mobile devices"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM mobile_devices ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [
            parsed for row in rows
            if (parsed := _parse_mobile_device(row)) is not None
        ]


def update_mobile_device_capabilities(device_id: str, capabilities: Dict[str, Any]) -> Optional[MobileDeviceRow]:
    """Update capabilities for a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE mobile_devices SET capabilities = %s, last_seen = %s WHERE device_id = %s
        """, (json.dumps(capabilities), now, device_id))
        if cursor.rowcount == 0:
            return None
    return get_mobile_device(device_id)


def update_mobile_device_fcm_token(device_id: str, fcm_token: str) -> Optional[MobileDeviceRow]:
    """Update FCM push token for a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE mobile_devices SET fcm_token = %s, last_seen = %s WHERE device_id = %s
        """, (fcm_token, now, device_id))
        if cursor.rowcount == 0:
            return None
    return get_mobile_device(device_id)


def update_mobile_device_unifiedpush_endpoint(
    device_id: str, endpoint: str
) -> Optional[MobileDeviceRow]:
    """Update the UnifiedPush endpoint URL for a mobile device.

    UnifiedPush lets self-hosted servers POST notifications directly to the
    user's own push distributor (e.g. ntfy) without a shared Firebase project.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE mobile_devices SET unifiedpush_endpoint = %s, last_seen = %s WHERE device_id = %s
        """, (endpoint, now, device_id))
        if cursor.rowcount == 0:
            return None
    return get_mobile_device(device_id)


def update_mobile_device_last_seen(device_id: str) -> None:
    """Update last_seen timestamp for a mobile device"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE mobile_devices SET last_seen = %s WHERE device_id = %s", (now, device_id))


def delete_mobile_device(device_id: str) -> bool:
    """Delete a mobile device"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""UPDATE paired_clients
            SET revoked_at = NOW(), session_token_hash = NULL, session_expires_at = NULL
            WHERE id IN (SELECT paired_client_id FROM mobile_devices WHERE device_id = %s)
        """, (device_id,))
        cursor.execute("""DELETE FROM paired_client_native_sessions WHERE client_id IN
            (SELECT paired_client_id FROM mobile_devices WHERE device_id=%s)""", (device_id,))
        cursor.execute("""DELETE FROM paired_client_web_reservations WHERE client_id IN
            (SELECT paired_client_id FROM mobile_devices WHERE device_id=%s)""", (device_id,))
        cursor.execute("DELETE FROM mobile_devices WHERE device_id = %s", (device_id,))
        return cursor.rowcount > 0


# ============================================================================
# Pending Action Operations
# ============================================================================

def get_pending_action(action_id: str) -> Optional[PendingActionRow]:
    """Get a pending action by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_actions WHERE id = %s", (action_id,))
        row = cursor.fetchone()
        if row:
            result = dict(row)
            if result.get("params"):
                result["params"] = _parse_params(result["params"])
            return result
        return None


def get_pending_actions_for_device(device_id: str) -> List[PendingActionRow]:
    """Get all pending actions for a device that haven't expired"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM pending_actions
            WHERE device_id = %s AND status = 'pending' AND expires_at > %s
            ORDER BY created_at DESC
        """, (device_id, now))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            result = dict(row)
            if result.get("params"):
                result["params"] = _parse_params(result["params"])
            results.append(result)
        return results


def update_pending_action_status(action_id: str, status: str) -> Optional[PendingActionRow]:
    """Update the status of a pending action"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_actions SET status = %s WHERE id = %s",
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
            WHERE status = 'pending' AND expires_at <= %s
        """, (now,))
        return cursor.rowcount


# ============================================================================
# Mobile Device Operations
# ============================================================================

def get_mobile_devices() -> List[MobileDeviceRow]:
    """Get all mobile devices (alias for get_all_mobile_devices)"""
    return get_all_mobile_devices()
