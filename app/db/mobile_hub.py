"""Immutable account-hub trust root and transactional per-install credentials."""
import json
import uuid
from datetime import datetime, timezone
from app.db.base import get_db

def create_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mobile_hub_binding (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            hub_url TEXT NOT NULL, server_id TEXT NOT NULL,
            linked_by TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mobile_hub_authorizations (
            device_id TEXT PRIMARY KEY,
            paired_client_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL, hub_device_id TEXT NOT NULL UNIQUE,
            server_id TEXT NOT NULL
        )
    """)

def get_binding():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT b.*, s.value AS encrypted_secret FROM mobile_hub_binding b
            LEFT JOIN system_settings s ON s.key = 'mobile_account_hub_secret'
            WHERE b.id = 1""")
        row = c.fetchone()
        return dict(row) if row else None


def bind(hub_url, server_id, encrypted_secret, linked_by):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO mobile_hub_binding
            (id, hub_url, server_id, linked_by)
            VALUES (1, %s, %s, %s) ON CONFLICT(id) DO NOTHING RETURNING id""",
            (hub_url, server_id, linked_by))
        if c.fetchone():
            # Use the standard encrypted settings store so admin password
            # rotation discovers/re-encrypts this secret too. Same transaction.
            c.execute("""INSERT INTO system_settings (key, value)
                VALUES ('mobile_account_hub_secret', %s)""", (encrypted_secret,))
    return get_binding()


def activate():
    with get_db() as conn:
        conn.cursor().execute("UPDATE mobile_hub_binding SET active = TRUE WHERE id = 1")


def ownership(device_id=None, *, paired_client_id=None):
    """Return {device_id, paired_client_id, account_id, hub_device_id, server_id} or None.

    This is a binding lookup, NOT a live authorization check. Call the core
    require_active helper before using it to authorize a request.
    """
    with get_db() as conn:
        c = conn.cursor()
        if paired_client_id is not None:
            c.execute("SELECT * FROM mobile_hub_authorizations WHERE paired_client_id = %s", (paired_client_id,))
        else:
            c.execute("SELECT * FROM mobile_hub_authorizations WHERE device_id = %s", (device_id,))
        row = c.fetchone()
        return dict(row) if row else None


def enroll(identity, info, key_hash, expires_at, scopes):
    """Never upsert: all three rows commit together or none do.

    Tombstone authorizations are intentionally retained after local deletion.
    Reusing an old hub device identity requires explicit administrative repair.
    """
    device_id = identity['device_id']
    client_id = 'pcl_' + uuid.uuid4().hex[:20]
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        c = conn.cursor()
        for table in ('mobile_devices', 'paired_clients', 'mobile_hub_authorizations'):
            c.execute(f"SELECT device_id FROM {table} WHERE device_id = %s", (device_id,))
            if c.fetchone():
                raise ValueError('Device already authorized; use a fresh installation identity')
        c.execute("""INSERT INTO paired_clients
            (id, device_id, device_name, platform, client_type, scopes, paired_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'mobile', %s, %s, %s, %s)""",
            (client_id, device_id, identity['device_name'], info.platform, json.dumps(scopes), now, now, now))
        c.execute("""INSERT INTO mobile_devices
            (id, device_id, device_name, platform, api_key_hash, capabilities,
             paired_client_id, api_key_expires_at, scopes, paired_at, created_at, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (uuid.uuid4().hex[:8], device_id, identity['device_name'], info.platform,
             key_hash, json.dumps(info.capabilities.model_dump()), client_id,
             expires_at.isoformat(), json.dumps(scopes), now, now, now))
        c.execute("""INSERT INTO mobile_hub_authorizations
            (device_id, paired_client_id, account_id, hub_device_id, server_id)
            VALUES (%s, %s, %s, %s, %s)""",
            (device_id, client_id, identity['account_id'], device_id, identity['server_id']))
    return device_id
