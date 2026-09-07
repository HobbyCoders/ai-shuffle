"""Worker-scoped account bindings, separate from shared LAN authorization."""
import json
import uuid
from datetime import datetime, timezone
from app.db.base import get_db
from app.core.config import settings

def create_schema(cursor):
    from app.core.owner_auth import create_schema as create_owner_schema
    create_owner_schema(cursor)
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
    # Additive tables let older workers keep their legacy binding while each
    # upgraded installation signs in independently. A null server is a durable
    # sign-out tombstone: never fall back to the legacy binding after logout.
    cursor.execute("""CREATE TABLE IF NOT EXISTS mobile_worker_bindings (
        instance_id TEXT PRIMARY KEY, hub_url TEXT, server_id TEXT UNIQUE,
        linked_by TEXT, active BOOLEAN NOT NULL DEFAULT FALSE
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS mobile_worker_authorizations (
        device_id TEXT PRIMARY KEY, paired_client_id TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL, hub_device_id TEXT NOT NULL,
        server_id TEXT NOT NULL, UNIQUE(server_id, hub_device_id)
    )""")


def secret_key():
    return 'mobile_account_hub_secret:' + settings.effective_instance_id

def get_binding():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT b.*, s.value AS encrypted_secret FROM mobile_worker_bindings b
            LEFT JOIN system_settings s ON s.key = %s WHERE b.instance_id = %s""",
            (secret_key(), settings.effective_instance_id))
        row = c.fetchone()
        if row:
            return dict(row) if row['server_id'] else None
        c.execute("""SELECT b.*, s.value AS encrypted_secret FROM mobile_hub_binding b
            LEFT JOIN system_settings s ON s.key = 'mobile_account_hub_secret'
            WHERE b.id = 1 AND (b.linked_by = %s OR b.linked_by NOT LIKE 'managed:%%')""",
            ('managed:' + settings.effective_instance_id,))
        row = c.fetchone()
        return dict(row) if row else None


def bind(hub_url, server_id, encrypted_secret, linked_by):
    existing = get_binding()
    if existing:
        return existing
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO mobile_worker_bindings
            (instance_id, hub_url, server_id, linked_by)
            VALUES (%s, %s, %s, %s) ON CONFLICT(instance_id) DO UPDATE SET
            hub_url=excluded.hub_url, server_id=excluded.server_id,
            linked_by=excluded.linked_by, active=FALSE
            WHERE mobile_worker_bindings.server_id IS NULL RETURNING instance_id""",
            (settings.effective_instance_id, hub_url, server_id, linked_by))
        if c.fetchone():
            # Use the standard encrypted settings store so admin password
            # rotation discovers/re-encrypts this secret too. Same transaction.
            c.execute("""INSERT INTO system_settings (key, value)
                VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (secret_key(), encrypted_secret))
    return get_binding()


def activate(server_id=None):
    binding = get_binding()
    if not binding or (server_id is not None and binding['server_id'] != server_id):
        raise ValueError('Worker authorization changed')
    with get_db() as conn:
        c = conn.cursor()
        if 'instance_id' in binding:
            c.execute("""UPDATE mobile_worker_bindings SET active=TRUE
                WHERE instance_id=%s AND server_id=%s""", (settings.effective_instance_id, binding['server_id']))
        else:
            c.execute("UPDATE mobile_hub_binding SET active=TRUE WHERE id=1 AND server_id=%s", (binding['server_id'],))


def disconnect():
    """Remove only this installation's authority and secrets in one transaction."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO mobile_worker_bindings(instance_id) VALUES (%s)
            ON CONFLICT(instance_id) DO UPDATE SET hub_url=NULL, server_id=NULL,
            linked_by=NULL, active=FALSE""", (settings.effective_instance_id,))
        # Clear the owned legacy row too so rolling back cannot resurrect it.
        c.execute("""DELETE FROM mobile_hub_binding WHERE linked_by=%s RETURNING id""",
                  ('managed:' + settings.effective_instance_id,))
        if c.fetchone():
            c.execute("DELETE FROM system_settings WHERE key='mobile_account_hub_secret'")
        for key in (secret_key(), 'mobile_managed_pending:' + settings.effective_instance_id,
                    'mobile_managed_credential:' + settings.effective_instance_id):
            c.execute('DELETE FROM system_settings WHERE key=%s', (key,))
        c.execute('DELETE FROM owner_browser_requests WHERE instance_id=%s', (settings.effective_instance_id,))


def local_device_id(server_id, hub_device_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'shuffle-account-device:' + server_id + ':' + hub_device_id))


def public_device_id(device):
    owner = ownership(device['device_id'])
    return owner['hub_device_id'] if owner else device['device_id']


def valid_device_identity(owner):
    return owner['device_id'] in (owner['hub_device_id'], local_device_id(owner['server_id'], owner['hub_device_id']))


def ownership(device_id=None, *, paired_client_id=None):
    """Return {device_id, paired_client_id, account_id, hub_device_id, server_id} or None.

    This is a binding lookup, NOT a live authorization check. Call the core
    require_active helper before using it to authorize a request.
    """
    with get_db() as conn:
        c = conn.cursor()
        column, value = ('paired_client_id', paired_client_id) if paired_client_id is not None else ('device_id', device_id)
        for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
            c.execute(f"SELECT * FROM {table} WHERE {column} = %s", (value,))
            row = c.fetchone()
            if row:
                return dict(row)
        return None


def enroll(identity, info, key_hash, expires_at, scopes, *, session_token_hash=None, login_proof=None, scoped=False):
    """Never upsert: all three rows commit together or none do.

    Tombstone authorizations are intentionally retained after local deletion.
    Reusing an old hub device identity requires explicit administrative repair.
    """
    hub_device_id = identity['device_id']
    device_id = local_device_id(identity['server_id'], hub_device_id) if scoped else hub_device_id
    authorization_table = 'mobile_worker_authorizations' if scoped else 'mobile_hub_authorizations'
    legacy = ownership(hub_device_id) if scoped else None
    if legacy and legacy['server_id'] == identity['server_id']:
        raise ValueError('Device already authorized; use a fresh installation identity')
    client_id = 'pcl_' + uuid.uuid4().hex[:20]
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        c = conn.cursor()
        from app.db.device_identity import lock_device_identity
        lock_device_identity(c, device_id)
        if login_proof:
            from app.core.config import settings
            c.execute('''DELETE FROM owner_browser_requests WHERE proof_hash=%s
                AND instance_id=%s AND server_id=%s AND expires_at>%s RETURNING proof_hash''',
                (login_proof, settings.effective_instance_id, identity['server_id'], now))
            if not c.fetchone():
                raise ValueError('Browser sign-in expired or already consumed')
        for table in ('mobile_devices', 'paired_clients', 'mobile_hub_authorizations', 'mobile_worker_authorizations'):
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
        c.execute(f"""INSERT INTO {authorization_table}
            (device_id, paired_client_id, account_id, hub_device_id, server_id)
            VALUES (%s, %s, %s, %s, %s)""",
            (device_id, client_id, identity['account_id'], hub_device_id, identity['server_id']))
        if session_token_hash:
            c.execute('''UPDATE paired_clients SET session_token_hash=%s,
                session_expires_at=%s WHERE id=%s''',
                (session_token_hash, expires_at.isoformat(), client_id))
    return device_id
