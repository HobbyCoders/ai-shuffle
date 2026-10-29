"""Worker-scoped account bindings, separate from shared LAN authorization."""
import hmac
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
    # Decisions and enrollment record IDs survive revocation and credential repair.
    cursor.execute("""CREATE TABLE IF NOT EXISTS mobile_worker_confirmations (
        server_id TEXT NOT NULL, device_id TEXT NOT NULL,
        paired_client_id TEXT NOT NULL, decision TEXT NOT NULL,
        mobile_record_id TEXT NOT NULL, decided_at TEXT NOT NULL,
        PRIMARY KEY(server_id, device_id, paired_client_id)
    )""")
    # Hash of the per-worker proof an account installation sends at enrollment.
    # Only the same installation can renew its entry and keep a decision.
    cursor.execute("""CREATE TABLE IF NOT EXISTS mobile_install_proofs (
        device_id TEXT PRIMARY KEY, proof_hash TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS mobile_key_renewals (
        device_id TEXT PRIMARY KEY, mobile_record_id TEXT NOT NULL,
        previous_hash TEXT NOT NULL UNIQUE, current_hash TEXT NOT NULL,
        encrypted_key TEXT NOT NULL, previous_expires_at TEXT NOT NULL
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


def enroll(identity, info, key_hash, expires_at, scopes, *, session_token_hash=None, scoped=False,
           install_proof_hash=None):
    """All rows commit together or none do; an existing entry is never overwritten
    except by renew_installation (same account, hub device and installation proof).

    Tombstone authorizations are intentionally retained after local deletion.
    Reusing a deleted or denied hub device identity requires a fresh identity.
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
        if scoped and install_proof_hash and _renew_installation(
                c, identity, info, device_id, key_hash, expires_at, scopes, install_proof_hash, now):
            return device_id
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
        # Record which credential the hub actually enrolled. A local repair must
        # not inherit account authorization for a replacement mobile record.
        c.execute("""INSERT INTO mobile_worker_confirmations
            (server_id, device_id, paired_client_id, decision, mobile_record_id, decided_at)
            SELECT %s, %s, %s, 'pending', id, %s FROM mobile_devices WHERE device_id=%s""",
            (identity['server_id'], device_id, client_id, now, device_id))
        if session_token_hash:
            c.execute('''UPDATE paired_clients SET session_token_hash=%s,
                session_expires_at=%s WHERE id=%s''',
                (session_token_hash, expires_at.isoformat(), client_id))
        if scoped and install_proof_hash:
            c.execute("""INSERT INTO mobile_install_proofs (device_id, proof_hash) VALUES (%s, %s)
                ON CONFLICT(device_id) DO UPDATE SET proof_hash=excluded.proof_hash""",
                (device_id, install_proof_hash))
    return device_id


def _renew_installation(c, identity, info, device_id, key_hash, expires_at, scopes, proof_hash, now):
    """Re-enrollment of an existing entry by the same account and hub device.

    Returns False when there is no entry (a normal first enrollment). Raises
    ValueError when the entry cannot be renewed. Otherwise replaces the key on
    the SAME rows, so one phone keeps one entry across account sign-ins:

    - Denied stays denied; a locally deleted entry stays a tombstone.
    - A stored proof must match. Only then is it the same installation and an
      approval is kept: the hub ticket proves the account and device id, but
      anyone holding the account password can obtain a ticket for a known id.
    - No stored proof (enrolled by an older app): the entry is treated like a
      fresh enrollment, pending exactly as a new identity would be.
    - Owner-revoked: comes back only as a request needing a manual decision;
      a new record id makes the managed-worker auto-approval refuse it.
    """
    c.execute('SELECT * FROM mobile_worker_authorizations WHERE device_id=%s FOR UPDATE', (device_id,))
    auth = c.fetchone()
    if not auth:
        return False
    if (auth['account_id'] != identity['account_id'] or auth['server_id'] != identity['server_id']
            or auth['hub_device_id'] != identity['device_id']):
        raise ValueError('Device already authorized; use a fresh installation identity')
    c.execute('SELECT * FROM paired_clients WHERE id=%s FOR UPDATE', (auth['paired_client_id'],))
    client = c.fetchone()
    c.execute('SELECT * FROM mobile_devices WHERE device_id=%s FOR UPDATE', (device_id,))
    device = c.fetchone()
    if (not client or not device or client['device_id'] != device_id
            or device['paired_client_id'] != client['id']):
        raise ValueError('Device already authorized; use a fresh installation identity')
    c.execute("""SELECT decision FROM mobile_worker_confirmations
        WHERE server_id=%s AND device_id=%s AND paired_client_id=%s FOR UPDATE""",
        (auth['server_id'], device_id, client['id']))
    previous = c.fetchone()
    if previous and previous['decision'] == 'denied':
        raise ValueError('This device was denied by the worker owner')
    c.execute('SELECT proof_hash FROM mobile_install_proofs WHERE device_id=%s FOR UPDATE', (device_id,))
    stored = c.fetchone()
    if stored and not hmac.compare_digest(stored['proof_hash'], proof_hash):
        raise ValueError('Device already authorized; use a fresh installation identity')
    revoked = bool(client['revoked_at'] or device['revoked_at'])
    keep_decision = stored is not None and not revoked
    record_id = device['id'] if keep_decision else uuid.uuid4().hex[:8]
    if not keep_decision:
        # Revoked: pin the decision to the OLD record, so the renewed one reads
        # as pending and is never "original"; only the owner can approve it.
        decision_record = device['id'] if revoked else record_id
        c.execute("""INSERT INTO mobile_worker_confirmations
            (server_id, device_id, paired_client_id, decision, mobile_record_id, decided_at)
            VALUES (%s, %s, %s, 'pending', %s, %s)
            ON CONFLICT(server_id, device_id, paired_client_id) DO UPDATE
            SET decision='pending', mobile_record_id=excluded.mobile_record_id, decided_at=excluded.decided_at
            WHERE mobile_worker_confirmations.decision != 'denied'""",
            (auth['server_id'], device_id, client['id'], decision_record, now))
    # The old key, its renewal receipt and every session derived from it end here.
    c.execute("""UPDATE mobile_devices SET id=%s, platform=%s, api_key_hash=%s, capabilities=%s,
        api_key_expires_at=%s, scopes=%s, revoked_at=NULL,
        fcm_token=CASE WHEN %s THEN fcm_token ELSE NULL END, last_seen=%s
        WHERE device_id=%s""",
        (record_id, info.platform, key_hash, json.dumps(info.capabilities.model_dump()),
         expires_at.isoformat(), json.dumps(scopes), keep_decision, now, device_id))
    # Expired slots are not bearer credentials, but their hashes let the next
    # authenticated mint advance matching chat bindings. Carry that continuity
    # only for a proof-matched, non-revoked installation. Logout clears even
    # expired slots; never recover continuity from the device id alone.
    c.execute("""UPDATE paired_clients SET revoked_at=NULL, scopes=%s,
        session_token_hash=CASE WHEN %s THEN session_token_hash ELSE NULL END,
        session_expires_at=NULL, updated_at=%s WHERE id=%s""",
        (json.dumps(scopes), keep_decision, now, client['id']))
    c.execute('DELETE FROM mobile_key_renewals WHERE device_id=%s', (device_id,))
    if keep_decision:
        c.execute('UPDATE paired_client_native_sessions SET expires_at=NOW() WHERE client_id=%s', (client['id'],))
    else:
        c.execute('DELETE FROM paired_client_native_sessions WHERE client_id=%s', (client['id'],))
    c.execute('DELETE FROM paired_client_web_reservations WHERE client_id=%s', (client['id'],))
    c.execute("""INSERT INTO mobile_install_proofs (device_id, proof_hash) VALUES (%s, %s)
        ON CONFLICT(device_id) DO UPDATE SET proof_hash=excluded.proof_hash""", (device_id, proof_hash))
    return True


def confirmation(owner):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""SELECT c.decision, c.mobile_record_id, d.id AS current_record_id
            FROM mobile_worker_confirmations c LEFT JOIN mobile_devices d ON d.device_id=c.device_id
            WHERE c.server_id=%s AND c.device_id=%s AND c.paired_client_id=%s""",
            (owner['server_id'], owner['device_id'], owner['paired_client_id']))
        row = c.fetchone()
        if row and (row['decision'] == 'denied' or row['mobile_record_id'] == row['current_record_id']):
            return row['decision']
        return 'pending'


def confirm_device(owner, approved, *, mobile_record_id=None, account_binding=None):
    """Serialize with revocation/repair; denial is a durable tombstone.

    A retry cannot resurrect a denied device. A new identity must be enrolled
    and confirmed. Repairing a credential never carries approval to the new key.
    """
    with get_db() as conn:
        c = conn.cursor()
        if account_binding is not None:
            # Fence account logout/rebinding while the approval is persisted.
            if 'instance_id' in account_binding:
                c.execute('SELECT * FROM mobile_worker_bindings WHERE instance_id=%s FOR SHARE',
                          (settings.effective_instance_id,))
            else:
                c.execute('SELECT * FROM mobile_hub_binding WHERE id=1 FOR SHARE')
            binding = c.fetchone()
            if (not binding or not binding['active']
                    or binding['linked_by'] != 'managed:' + settings.effective_instance_id
                    or binding['server_id'] != owner['server_id']
                    or any(binding[k] != account_binding[k] for k in ('server_id', 'hub_url', 'linked_by'))):
                raise ValueError('Account authorization revoked')
        c.execute('SELECT * FROM paired_clients WHERE id=%s FOR UPDATE', (owner['paired_client_id'],))
        client = c.fetchone()
        c.execute('SELECT * FROM mobile_devices WHERE device_id=%s FOR UPDATE', (owner['device_id'],))
        device = c.fetchone()
        if (not client or not device or client['revoked_at'] or device['revoked_at']
                or device['paired_client_id'] != owner['paired_client_id']
                or client['device_id'] != owner['device_id']
                or (mobile_record_id is not None and device['id'] != mobile_record_id)):
            raise ValueError('Device authorization changed')
        if account_binding is not None:
            c.execute("""SELECT decision, mobile_record_id FROM mobile_worker_confirmations
                WHERE server_id=%s AND device_id=%s AND paired_client_id=%s""",
                (owner['server_id'], owner['device_id'], owner['paired_client_id']))
            previous = c.fetchone()
            if previous and previous['decision'] == 'denied':
                raise ValueError('This device was denied by the worker owner')
            # Old enrollments predate the pending record. Their mobile and paired
            # client were created together; repair retains the old client but
            # creates a new mobile record. Do not bless that replacement.
            original = (previous['mobile_record_id'] == device['id'] if previous else
                        bool(device['created_at']) and device['created_at'] == client['created_at'])
            if not original:
                raise ValueError('Confirm this device on the worker before connecting')
        if approved:
            from app.core.device_keys import key_expiry
            if key_expiry(dict(device)) <= datetime.now(timezone.utc):
                raise ValueError('Device credential expired; pair this device again')
        decision = 'approved' if approved else 'denied'
        c.execute("""INSERT INTO mobile_worker_confirmations
            (server_id, device_id, paired_client_id, decision, mobile_record_id, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(server_id, device_id, paired_client_id) DO UPDATE
            SET decision=excluded.decision, mobile_record_id=excluded.mobile_record_id,
                decided_at=excluded.decided_at
            WHERE mobile_worker_confirmations.decision != 'denied'
            RETURNING decision""", (owner['server_id'], owner['device_id'], owner['paired_client_id'],
                                     decision, device['id'], datetime.now(timezone.utc).isoformat()))
        row = c.fetchone()
        if not row and approved:
            raise ValueError('Device was denied; enroll a new identity')
    return decision


def list_confirmations():
    binding = get_binding()
    if not binding or not binding['active']:
        return []
    result = []
    with get_db() as conn:
        c = conn.cursor()
        for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
            c.execute(f"""SELECT a.*, p.device_name, p.platform, p.revoked_at,
                d.id AS mobile_record_id, d.api_key_expires_at FROM {table} a
                JOIN paired_clients p ON p.id=a.paired_client_id
                JOIN mobile_devices d ON d.device_id=a.device_id
                WHERE a.server_id=%s""", (binding['server_id'],))
            result.extend(dict(row) for row in c.fetchall())
    for item in result:
        item['confirmation'] = confirmation(item)
    return result


def lock_account_entry(cursor, entry):
    """Hold a cleanup candidate against enrollment, use, repair and revocation.

    Lock in enrollment order. The caller must re-read the candidate afterwards
    and keep this transaction open through both the hub and local revocation.
    """
    from app.db.device_identity import lock_device_identity
    lock_device_identity(cursor, entry['device_id'])
    for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
        cursor.execute(f'SELECT device_id FROM {table} WHERE device_id=%s FOR UPDATE', (entry['device_id'],))
    cursor.execute('SELECT id FROM paired_clients WHERE id=%s FOR UPDATE', (entry['paired_client_id'],))
    cursor.execute('SELECT id FROM mobile_devices WHERE device_id=%s FOR UPDATE', (entry['device_id'],))


def live_account_entries(server_id, *, _conn=None):
    """Unrevoked account-enrolled phones, including activity and credential generation."""
    if _conn is None:
        with get_db() as conn:
            return live_account_entries(server_id, _conn=conn)
    result = []
    c = _conn.cursor()
    for table in ('mobile_worker_authorizations', 'mobile_hub_authorizations'):
        c.execute(f"""SELECT a.device_id, a.paired_client_id, a.account_id, a.hub_device_id,
            a.server_id, p.device_name, p.platform, p.last_seen AS client_seen,
            d.created_at, d.last_seen AS device_seen, d.id AS mobile_record_id, d.api_key_hash, f.decision
            FROM {table} a
            JOIN paired_clients p ON p.id=a.paired_client_id
            JOIN mobile_devices d ON d.device_id=a.device_id AND d.paired_client_id=p.id
            LEFT JOIN mobile_worker_confirmations f ON f.server_id=a.server_id
                AND f.device_id=a.device_id AND f.paired_client_id=a.paired_client_id
            WHERE a.server_id=%s AND p.revoked_at IS NULL AND d.revoked_at IS NULL""", (server_id,))
        result.extend(dict(row) for row in c.fetchall())
    return result
