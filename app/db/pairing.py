"""One-time pairing grants and durable controller credentials.

Raw pairing codes and controller session tokens never enter the database.  The
API hashes them before calling this module; every lookup is performed against
the digest.  Grant consumption and client enrollment share one transaction so
a code cannot be redeemed twice, even when several Shuffle installs serve the
same database.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list
from app.db.device_identity import lock_device_identity


class PairingGrantRow(TypedDict, total=False):
    id: str
    token_hash: str
    purpose: str
    scopes: List[str]
    target_install_id: Optional[str]
    endpoint_url: Optional[str]
    device_label: Optional[str]
    created_by: str
    created_at: str
    expires_at: str
    consumed_at: Optional[str]
    revoked_at: Optional[str]
    attempt_count: int
    max_attempts: int


class PairedClientRow(TypedDict, total=False):
    id: str
    device_id: str
    device_name: str
    platform: str
    client_type: str
    scopes: List[str]
    session_token_hash: Optional[str]
    session_expires_at: Optional[str]
    paired_at: str
    last_seen: Optional[str]
    revoked_at: Optional[str]
    created_at: str
    updated_at: str


def _json_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw if isinstance(value, str)]


def _grant(row: Any) -> Optional[PairingGrantRow]:
    result = row_to_dict(row)
    if result is None:
        return None
    result["scopes"] = _json_list(result.get("scopes"))
    return result  # type: ignore[return-value]


def _client(row: Any) -> Optional[PairedClientRow]:
    result = row_to_dict(row)
    if result is None:
        return None
    result["scopes"] = _json_list(result.get("scopes"))
    return result  # type: ignore[return-value]


def create_pairing_grant(
    *,
    token_hash: str,
    purpose: str,
    scopes: List[str],
    target_install_id: Optional[str],
    endpoint_url: Optional[str],
    device_label: Optional[str],
    created_by: str,
    expires_at: datetime,
    max_attempts: int = 10,
) -> PairingGrantRow:
    grant_id = f"pgr_{uuid.uuid4().hex[:20]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO pairing_grants
                (id, token_hash, purpose, scopes, target_install_id,
                 endpoint_url, device_label, created_by, created_at,
                 expires_at, attempt_count, max_attempts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)
            RETURNING *
            """,
            (
                grant_id,
                token_hash,
                purpose,
                json.dumps(scopes),
                target_install_id,
                endpoint_url,
                device_label,
                created_by,
                now,
                expires_at.isoformat(),
                max_attempts,
            ),
        )
        result = _grant(cursor.fetchone())
    if result is None:
        raise RuntimeError("Pairing grant insert returned no row")
    return result


def list_pairing_grants(*, active_only: bool = True) -> List[PairingGrantRow]:
    with get_db() as conn:
        cursor = conn.cursor()
        if active_only:
            cursor.execute(
                """
                SELECT * FROM pairing_grants
                WHERE consumed_at IS NULL AND revoked_at IS NULL
                  AND expires_at > NOW() AND attempt_count < max_attempts
                ORDER BY created_at DESC
                """
            )
        else:
            cursor.execute(
                "SELECT * FROM pairing_grants ORDER BY created_at DESC LIMIT 100"
            )
        rows = rows_to_list(cursor.fetchall())
    return [_grant(row) for row in rows if row is not None]  # type: ignore[misc]


def revoke_pairing_grant(grant_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE pairing_grants SET revoked_at = NOW()
            WHERE id = %s AND consumed_at IS NULL AND revoked_at IS NULL
            """,
            (grant_id,),
        )
        return cursor.rowcount > 0


def _consume_grant(
    cursor: Any, token_hash: str, expected_purpose: str, *, target_install_id: Optional[str] = None
) -> Optional[PairingGrantRow]:
    """Atomically consume a live grant, returning it to the winner only."""
    target_clause = " AND target_install_id = %s" if target_install_id is not None else ""
    params = (token_hash, expected_purpose) + ((target_install_id,) if target_install_id is not None else ())
    cursor.execute(
        """
        UPDATE pairing_grants
        SET consumed_at = NOW(), attempt_count = attempt_count + 1
        WHERE token_hash = %s
          AND purpose = %s
          AND consumed_at IS NULL
          AND revoked_at IS NULL
          AND expires_at > NOW()
          AND attempt_count < max_attempts
        """ + target_clause + " RETURNING *",
        params,
    )
    row = cursor.fetchone()
    if row is not None:
        return _grant(row)

    # Count attempts against a real but expired/revoked/exhausted grant without
    # revealing which failure occurred. Random guesses have no row to update.
    cursor.execute(
        """
        UPDATE pairing_grants
        SET attempt_count = CASE
            WHEN attempt_count < max_attempts THEN attempt_count + 1
            ELSE max_attempts
        END
        WHERE token_hash = %s AND purpose = %s AND consumed_at IS NULL
        """ + target_clause,
        params,
    )
    return None


def exchange_grant_for_client(
    *,
    token_hash: str,
    device_id: str,
    device_name: str,
    platform: str,
    client_type: str,
    session_token_hash: str,
    session_expires_at: datetime,
    target_install_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Consume a grant and enroll/rotate a browser controller atomically."""
    now = datetime.now(timezone.utc).isoformat()
    client_id = f"pcl_{uuid.uuid4().hex[:20]}"
    with get_db() as conn:
        cursor = conn.cursor()
        # A browser exchange cannot turn a retained mobile account identity
        # into a browser controller or bypass its explicit repair flow.
        lock_device_identity(cursor, device_id)
        for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
            cursor.execute(f'SELECT device_id FROM {table} WHERE device_id = %s', (device_id,))
            if cursor.fetchone() is not None:
                return None
        grant = _consume_grant(cursor, token_hash, "controller", target_install_id=target_install_id)
        if grant is None:
            return None
        # The same one-time grant can enroll a native mobile client, but a
        # browser session receives only the general controller capability.
        # Mobile-only scopes must not hitch a ride into the web credential.
        scopes = [
            scope for scope in (grant.get("scopes") or [])
            if scope == "controller"
        ] or ["controller"]
        cursor.execute(
            """
            INSERT INTO paired_clients
                (id, device_id, device_name, platform, client_type, scopes,
                 session_token_hash, session_expires_at, paired_at, last_seen,
                 revoked_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
            ON CONFLICT(device_id) DO UPDATE SET
                device_name = EXCLUDED.device_name,
                platform = EXCLUDED.platform,
                client_type = EXCLUDED.client_type,
                scopes = EXCLUDED.scopes,
                session_token_hash = EXCLUDED.session_token_hash,
                session_expires_at = EXCLUDED.session_expires_at,
                paired_at = EXCLUDED.paired_at,
                last_seen = EXCLUDED.last_seen,
                revoked_at = NULL,
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            (
                client_id,
                device_id,
                device_name,
                platform,
                client_type,
                json.dumps(scopes),
                session_token_hash,
                session_expires_at.isoformat(),
                now,
                now,
                now,
                now,
            ),
        )
        client = _client(cursor.fetchone())
        if client is not None:
            cursor.execute('DELETE FROM paired_client_web_reservations WHERE client_id=%s', (client['id'],))
    if client is None:
        raise RuntimeError("Paired client upsert returned no row")
    return {"grant": grant, "client": client}


def exchange_grant_for_mobile(
    *,
    token_hash: str,
    device_id: str,
    device_name: str,
    platform: str,
    capabilities: Dict[str, Any],
    api_key_hash: str,
    api_key_expires_at: datetime,
    managed_repair: Optional[Dict[str, Any]] = None,
    target_install_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Consume a grant and create its paired-client/mobile rows atomically.

    ``duplicate`` is returned without consuming the grant so the operator can
    revoke the old device and retry with the same still-live code.
    """
    now = datetime.now(timezone.utc).isoformat()
    client_id = f"pcl_{uuid.uuid4().hex[:20]}"
    mobile_id = uuid.uuid4().hex[:8]
    with get_db() as conn:
        cursor = conn.cursor()
        lock_device_identity(cursor, device_id)
        cursor.execute(
            "SELECT id FROM mobile_devices WHERE device_id = %s",
            (device_id,),
        )
        if cursor.fetchone() is not None:
            return {"duplicate": True}

        # A retained account tombstone cannot be bypassed with generic direct
        # registration, even after administration removes its mobile row.
        owners = []
        for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
            cursor.execute(f'SELECT * FROM {table} WHERE device_id = %s FOR UPDATE', (device_id,))
            found = cursor.fetchone()
            if found is not None:
                owners.append(dict(found))
        if owners or managed_repair is not None:
            if managed_repair is None:
                return {'managed_repair_required': True}
            expected = managed_repair['owner']
            fields = ('device_id', 'paired_client_id', 'account_id', 'hub_device_id', 'server_id')
            if len(owners) != 1 or any(owners[0].get(key) != expected.get(key) for key in fields):
                return {'repair_invalid': True}
            owner = owners[0]
            from app.db.mobile_hub import local_device_id
            if owner['device_id'] != local_device_id(owner['server_id'], owner['hub_device_id']):
                return {'repair_invalid': True}
            if target_install_id is not None and target_install_id != managed_repair['instance_id']:
                return {'repair_invalid': True}
            target_install_id = managed_repair['instance_id']
            if not target_install_id:
                return {'repair_invalid': True}
            # Lock binding and client until key replacement commits, fencing
            # concurrent logout, changed ownership and another worker.
            cursor.execute('''SELECT server_id, active FROM mobile_worker_bindings
                WHERE instance_id = %s FOR SHARE''', (target_install_id,))
            binding = cursor.fetchone()
            cursor.execute('SELECT device_id, revoked_at FROM paired_clients WHERE id = %s FOR UPDATE',
                           (owner['paired_client_id'],))
            client = cursor.fetchone()
            if (not binding or not binding['active'] or binding['server_id'] != owner['server_id']
                    or not client or not client['revoked_at'] or client['device_id'] != device_id):
                return {'repair_invalid': True}

        grant = _consume_grant(cursor, token_hash, "controller", target_install_id=target_install_id)
        if grant is None:
            return None
        scopes = grant.get("scopes") or [
            "controller", "mobile:notifications", "mobile:actions", "web:session"
        ]
        cursor.execute(
            """
            INSERT INTO paired_clients
                (id, device_id, device_name, platform, client_type, scopes,
                 paired_at, last_seen, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'mobile', %s, %s, %s, %s, %s)
            ON CONFLICT(device_id) DO UPDATE SET
                device_name = EXCLUDED.device_name,
                platform = EXCLUDED.platform,
                client_type = 'mobile',
                scopes = EXCLUDED.scopes,
                paired_at = EXCLUDED.paired_at,
                last_seen = EXCLUDED.last_seen,
                session_token_hash = NULL,
                session_expires_at = NULL,
                revoked_at = NULL,
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            (
                client_id,
                device_id,
                device_name,
                platform,
                json.dumps(scopes),
                now,
                now,
                now,
                now,
            ),
        )
        client = _client(cursor.fetchone())
        if client is None:
            raise RuntimeError("Paired mobile client upsert returned no row")
        cursor.execute('DELETE FROM paired_client_native_sessions WHERE client_id = %s', (client['id'],))
        cursor.execute('DELETE FROM paired_client_web_reservations WHERE client_id = %s', (client['id'],))
        cursor.execute(
            """
            INSERT INTO mobile_devices
                (id, device_id, device_name, platform, api_key_hash,
                 capabilities, paired_client_id, api_key_expires_at, scopes,
                 paired_at, created_at, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                mobile_id,
                device_id,
                device_name,
                platform,
                api_key_hash,
                json.dumps(capabilities),
                client["id"],
                api_key_expires_at.isoformat(),
                json.dumps(scopes),
                now,
                now,
                now,
            ),
        )
    return {"grant": grant, "client": client}


def get_paired_client_by_session_hash(token_hash: str) -> Optional[PairedClientRow]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.* FROM paired_clients p
            WHERE p.revoked_at IS NULL AND (
                (p.session_token_hash = %s AND p.session_expires_at > NOW())
                OR EXISTS (SELECT 1 FROM paired_client_native_sessions n
                    WHERE n.client_id = p.id AND n.token_hash = %s AND n.expires_at > NOW()))
            """,
            (token_hash, token_hash),
        )
        return _client(cursor.fetchone())


# A paired client's last_seen only needs to be accurate to the minute; writing
# it on literally every authenticated request turns each read into a row update
# (and a WAL write) for no extra information. Skip the write while the stored
# value is still fresh.
PAIRED_CLIENT_TOUCH_INTERVAL_SECONDS = 60


def touch_paired_client(client_id: str) -> None:
    # The staleness threshold is computed in Python rather than as
    # ``NOW() - INTERVAL '60 seconds'`` so the statement also runs on the
    # SQLite test shim, which has no INTERVAL literal rewrite.
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=PAIRED_CLIENT_TOUCH_INTERVAL_SECONDS
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE paired_clients SET last_seen = NOW(), updated_at = NOW()
            WHERE id = %s AND (last_seen IS NULL OR last_seen < %s)
            """,
            (client_id, threshold),
        )


def list_paired_clients(*, include_revoked: bool = False) -> List[PairedClientRow]:
    with get_db() as conn:
        cursor = conn.cursor()
        if include_revoked:
            cursor.execute("SELECT * FROM paired_clients ORDER BY paired_at DESC")
        else:
            cursor.execute(
                "SELECT * FROM paired_clients WHERE revoked_at IS NULL ORDER BY paired_at DESC"
            )
        rows = rows_to_list(cursor.fetchall())
    return [_client(row) for row in rows if row is not None]  # type: ignore[misc]


def get_paired_client(client_id: str) -> Optional[PairedClientRow]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM paired_clients WHERE id = %s", (client_id,))
        return _client(cursor.fetchone())


def revoke_paired_client(client_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE paired_clients
            SET revoked_at = NOW(), session_token_hash = NULL,
                session_expires_at = NULL, updated_at = NOW()
            WHERE id = %s AND revoked_at IS NULL
            """,
            (client_id,),
        )
        changed = cursor.rowcount > 0
        if changed:
            cursor.execute("DELETE FROM paired_client_native_sessions WHERE client_id=%s", (client_id,))
            cursor.execute("DELETE FROM paired_client_web_reservations WHERE client_id=%s", (client_id,))
            cursor.execute(
                "UPDATE mobile_devices SET revoked_at = NOW() WHERE paired_client_id = %s",
                (client_id,),
            )
        return changed


def rename_paired_client(client_id: str, device_name: str) -> Optional[PairedClientRow]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE paired_clients SET device_name = %s, updated_at = NOW()
            WHERE id = %s AND revoked_at IS NULL RETURNING *
            """,
            (device_name, client_id),
        )
        client = _client(cursor.fetchone())
        if client is not None:
            cursor.execute(
                "UPDATE mobile_devices SET device_name = %s WHERE paired_client_id = %s",
                (device_name, client_id),
            )
        return client


def ensure_paired_client_for_mobile(device: Dict[str, Any]) -> Optional[PairedClientRow]:
    """Return/create the generic paired-client row for a legacy mobile row."""
    linked = device.get("paired_client_id")
    if linked:
        existing = get_paired_client(str(linked))
        if existing is not None:
            return existing

    now = datetime.now(timezone.utc).isoformat()
    client_id = f"pcl_{uuid.uuid4().hex[:20]}"
    scopes = _json_list(device.get("scopes")) or [
        "controller", "mobile:notifications", "mobile:actions", "web:session"
    ]
    with get_db() as conn:
        cursor = conn.cursor()
        lock_device_identity(cursor, device['device_id'])
        cursor.execute('SELECT id FROM paired_clients WHERE device_id = %s FOR UPDATE', (device['device_id'],))
        cursor.fetchone()
        cursor.execute('''SELECT id FROM mobile_devices WHERE id = %s AND device_id = %s
            AND api_key_hash = %s AND revoked_at IS NULL FOR UPDATE''',
            (device.get('id'), device['device_id'], device.get('api_key_hash')))
        if cursor.fetchone() is None:
            return None
        cursor.execute(
            """
            INSERT INTO paired_clients
                (id, device_id, device_name, platform, client_type, scopes,
                 paired_at, last_seen, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'mobile', %s, %s, %s, %s, %s)
            ON CONFLICT(device_id) DO UPDATE SET
                device_name = EXCLUDED.device_name,
                platform = EXCLUDED.platform,
                client_type = 'mobile',
                scopes = EXCLUDED.scopes,
                updated_at = EXCLUDED.updated_at
            WHERE paired_clients.revoked_at IS NULL
            RETURNING *
            """,
            (
                client_id,
                device["device_id"],
                device.get("device_name") or "Mobile device",
                device.get("platform") or "mobile",
                json.dumps(scopes),
                device.get("paired_at") or device.get("created_at") or now,
                device.get("last_seen"),
                device.get("created_at") or now,
                now,
            ),
        )
        client = _client(cursor.fetchone())
        if client is None:
            return None
        cursor.execute(
            "UPDATE mobile_devices SET paired_client_id = %s WHERE device_id = %s",
            (client["id"], device["device_id"]),
        )
    return client


class WebSessionReservationConflict(Exception):
    """An older or consumed mint must not rotate the current web cookie."""


def _lock_current_mobile_client(cursor, client_id, mobile_device):
    # Same lock order as revocation/deletion. Authentication before an await
    # is insufficient: require the exact current credential at commit time.
    cursor.execute('SELECT * FROM paired_clients WHERE id = %s AND revoked_at IS NULL FOR UPDATE',
                   (client_id,))
    client = _client(cursor.fetchone())
    if client is None:
        return None
    cursor.execute('''SELECT * FROM mobile_devices WHERE id = %s AND device_id = %s
        AND paired_client_id = %s AND api_key_hash = %s AND revoked_at IS NULL FOR SHARE''',
        (mobile_device.get('id'), mobile_device['device_id'], client_id, mobile_device.get('api_key_hash')))
    current = cursor.fetchone()
    if current is None:
        return None
    current = dict(current)
    from app.core.device_keys import key_expiry
    if key_expiry(current) <= datetime.now(timezone.utc):
        return None
    if current.get('scopes') and 'web:session' not in _json_list(current['scopes']):
        return None
    return client


def reserve_paired_client_web_session(client_id, reservation_hash, expires_at, *, mobile_device):
    """Replace only the pending reservation; the active session stays valid."""
    with get_db() as conn:
        cursor = conn.cursor()
        if _lock_current_mobile_client(cursor, client_id, mobile_device) is None:
            return False
        cursor.execute('''INSERT INTO paired_client_web_reservations
            (client_id, mobile_record_id, api_key_hash, reservation_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s) ON CONFLICT(client_id) DO UPDATE SET
            mobile_record_id=EXCLUDED.mobile_record_id, api_key_hash=EXCLUDED.api_key_hash,
            reservation_hash=EXCLUDED.reservation_hash, expires_at=EXCLUDED.expires_at''',
            (client_id, mobile_device['id'], mobile_device['api_key_hash'], reservation_hash, expires_at.isoformat()))
        return True


def set_paired_client_session(
    client_id: str, token_hash: str, expires_at: datetime, *, purpose: str = 'web',
    mobile_device: Optional[Dict[str, Any]] = None,
    web_reservation_hash: Optional[str] = None,
) -> Optional[PairedClientRow]:
    if purpose not in ('web', 'native'):
        raise ValueError('Unknown session purpose')
    if web_reservation_hash is not None and (purpose != 'web' or mobile_device is None):
        raise ValueError('Web reservation requires the current mobile credential')
    with get_db() as conn:
        cursor = conn.cursor()
        if mobile_device is not None:
            if _lock_current_mobile_client(cursor, client_id, mobile_device) is None:
                return None
        if purpose == 'native':
            cursor.execute("SELECT * FROM paired_clients WHERE id=%s AND revoked_at IS NULL FOR UPDATE",
                           (client_id,))
            client = _client(cursor.fetchone())
            if not client:
                return None
            cursor.execute("""INSERT INTO paired_client_native_sessions(client_id,token_hash,expires_at)
                VALUES(%s,%s,%s) ON CONFLICT(client_id) DO UPDATE SET
                token_hash=EXCLUDED.token_hash, expires_at=EXCLUDED.expires_at""",
                           (client_id, token_hash, expires_at.isoformat()))
            return client
        # Every web rotation serializes with reservations, including legacy
        # callers. A reservation alone never changes the active cookie.
        cursor.execute('SELECT id FROM paired_clients WHERE id=%s AND revoked_at IS NULL FOR UPDATE', (client_id,))
        if cursor.fetchone() is None:
            return None
        if web_reservation_hash is not None:
            cursor.execute('''DELETE FROM paired_client_web_reservations
                WHERE client_id=%s AND mobile_record_id=%s AND api_key_hash=%s
                AND reservation_hash=%s AND expires_at>%s RETURNING client_id''',
                (client_id, mobile_device['id'], mobile_device['api_key_hash'], web_reservation_hash,
                 datetime.now(timezone.utc).isoformat()))
            if cursor.fetchone() is None:
                raise WebSessionReservationConflict()
        else:
            cursor.execute('DELETE FROM paired_client_web_reservations WHERE client_id=%s', (client_id,))
        cursor.execute(
            """
            UPDATE paired_clients
            SET session_token_hash = %s, session_expires_at = %s,
                last_seen = NOW(), updated_at = NOW()
            WHERE id = %s AND revoked_at IS NULL
            RETURNING *
            """,
            (token_hash, expires_at.isoformat(), client_id),
        )
        return _client(cursor.fetchone())


def clear_paired_client_session(client_id: str, token_hash: Optional[str] = None) -> bool:
    """Invalidate a controller session without revoking the paired device."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM paired_clients WHERE id=%s FOR UPDATE', (client_id,))
        if cursor.fetchone() is None:
            return False
        cursor.execute("DELETE FROM paired_client_native_sessions WHERE client_id=%s"
                       + (" AND token_hash=%s" if token_hash else ""),
                       (client_id, token_hash) if token_hash else (client_id,))
        native_changed = cursor.rowcount > 0
        cursor.execute(
            """
            UPDATE paired_clients
            SET session_token_hash = NULL, session_expires_at = NULL,
                updated_at = NOW()
            WHERE id = %s AND revoked_at IS NULL
              AND (%s IS NULL OR session_token_hash = %s)
            """,
            (client_id, token_hash, token_hash),
        )
        web_changed = cursor.rowcount > 0
        if web_changed:
            cursor.execute('DELETE FROM paired_client_web_reservations WHERE client_id=%s', (client_id,))
        return native_changed or web_changed


def consume_grant_for_network_join(*, token_hash: str) -> Optional[PairingGrantRow]:
    """Consume a ``network:join`` grant issued by this worker."""
    with get_db() as conn:
        cursor = conn.cursor()
        return _consume_grant(cursor, token_hash, "network:join")


def consume_grant_for_runner(
    *, token_hash: str, install_id: str
) -> Optional[PairingGrantRow]:
    """Consume a runner-enrollment grant and approve an existing pending row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM installs WHERE id = %s", (install_id,))
        if cursor.fetchone() is None:
            return None
        grant = _consume_grant(cursor, token_hash, "runner")
        if grant is None:
            return None
        cursor.execute(
            """
            UPDATE installs
            SET approved_at = NOW(), revoked_at = NULL, updated_at = NOW()
            WHERE id = %s
            """,
            (install_id,),
        )
        if cursor.rowcount == 0:
            raise RuntimeError("Runner disappeared during enrollment")
        return grant
