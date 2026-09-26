"""Database-scoped credential keys with durable local custody and portable recovery.

Existing enc: data is verified and its DEK adopted unchanged. Only the DEK is
wrapped by a new random key; that wrapping key never enters the database.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.core.config import settings
from app.core import managed_credentials as custody

STATE_KEY = 'credential_key_state_v1'
_TABLES = (
    ('system_settings', ('value',), ()),
    ('automations', ('triggers', 'trigger_config'), ('triggers', 'trigger_config')),
    ('connectors', ('config',), ('config',)),
    ('api_user_credentials', ('encrypted_value',), ()),
    ('model_providers', ('auth_token', 'codex_access_token', 'codex_refresh_token', 'openai_admin_key'), ()),
)


class CredentialKeyError(RuntimeError):
    def __init__(self, state='recovery_required'):
        self.state = state
        super().__init__('Credential storage requires local recovery (' + state + ')')


def _state(raw):
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {'version', 'database_id', 'wrapped_dek'}
                or data['version'] != 1 or str(uuid.UUID(data['database_id'])) != data['database_id']
                or (data['wrapped_dek'] is not None and not isinstance(data['wrapped_dek'], str))):
            raise ValueError()
        return data
    except Exception:
        raise CredentialKeyError() from None


def _read_state(cursor, *, lock=False):
    cursor.execute('SELECT value FROM system_settings WHERE key=%s' + (' FOR UPDATE' if lock else ''),
                   (STATE_KEY,))
    row = cursor.fetchone()
    return _state(row['value']) if row else None


def key_path(database_id, *, create=False):
    if str(uuid.UUID(database_id)) != database_id:
        raise CredentialKeyError()
    directory = settings.effective_data_dir / '.credential-keys'
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or
            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
        raise CredentialKeyError()
    if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise CredentialKeyError()
    return directory / (database_id + '.key')


def _publish(database_id, key):
    """Publish complete, fsynced bytes without replacing any existing key."""
    path = key_path(database_id, create=True)
    raw = b'dpapi:' + base64.b64encode(custody._windows_protect(key)) if os.name == 'nt' else key
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            custody._publish_key(temporary, path)
        except FileExistsError:
            existing = custody._read_key(path)
            if not secrets.compare_digest(existing, key):
                raise CredentialKeyError()
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    custody._sync_key_location(path)
    return key


def _wrapping_key(database_id, *, create=False):
    try:
        path = key_path(database_id)
        key = custody._read_key(path)
        if create:
            custody._sync_key_location(path)
        return key
    except FileNotFoundError:
        if not create:
            raise CredentialKeyError() from None
        return _publish(database_id, Fernet.generate_key())


def _encrypted_values(cursor):
    """Every credential store, including orphan/inactive rows, on one connection.

    Query, JSON and shape errors propagate. A failed enumeration is never an
    empty store. installation:v1 managed-connector copies remain independent.
    """
    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, str) and value.startswith('enc:'):
            yield value

    values = []
    for table, columns, json_columns in _TABLES:
        cursor.execute('SELECT ' + ', '.join(columns) + ' FROM ' + table)
        for row in cursor.fetchall():
            for column in columns:
                value = row[column]
                if column in json_columns and isinstance(value, str):
                    value = json.loads(value)
                if column not in json_columns and value is not None and not isinstance(value, str):
                    raise CredentialKeyError()
                values.extend(walk(value))
    return values


def _verify(key, values, *, state='recovery_required'):
    try:
        cipher = Fernet(key)
        for value in values:
            cipher.decrypt(base64.b64decode(value[4:], validate=True)).decode('utf-8')
    except Exception:
        raise CredentialKeyError(state) from None


def _unwrap(metadata, wrapping_key):
    try:
        data = json.loads(Fernet(wrapping_key).decrypt(metadata['wrapped_dek'].encode()))
        if set(data) != {'database_id', 'data_key'} or data['database_id'] != metadata['database_id']:
            raise ValueError()
        key = data['data_key'].encode('ascii')
        Fernet(key)
        return key
    except Exception:
        raise CredentialKeyError() from None


def _legacy_key(cursor, values, legacy_password):
    if not values:
        return Fernet.generate_key()
    password = legacy_password() if callable(legacy_password) else legacy_password
    if not password:
        raise CredentialKeyError('legacy_unlock_required')
    cursor.execute('SELECT value FROM system_settings WHERE key=%s', ('encryption_salt',))
    row = cursor.fetchone()
    try:
        salt = base64.b64decode(row['value'], validate=True)
        if len(salt) != 32:
            raise ValueError()
    except Exception:
        raise CredentialKeyError('legacy_unlock_required') from None
    from app.core.encryption import derive_key_from_password
    key = derive_key_from_password(password, salt)
    _verify(key, values, state='legacy_unlock_required')
    return key


def load_or_adopt(db, legacy_password=None):
    """Return (DEK, database ID). Never rewrite a credential or accept a new key
    for an already activated database. The pending database ID is committed
    first so crashes/retries reuse the same file location across processes.
    """
    with db.get_db() as conn:
        cursor = conn.cursor()
        pending = json.dumps({'version': 1, 'database_id': str(uuid.uuid4()), 'wrapped_dek': None})
        cursor.execute('INSERT INTO system_settings (key,value) VALUES (%s,%s) ON CONFLICT(key) DO NOTHING',
                       (STATE_KEY, pending))
    # PostgreSQL row lock serializes concurrent initializers. All store reads,
    # verification and activation share this one transaction/commit boundary.
    with db.get_db() as conn:
        cursor = conn.cursor()
        metadata = _read_state(cursor, lock=True)
        if metadata is None:
            raise CredentialKeyError()
        values = _encrypted_values(cursor)
        if metadata['wrapped_dek'] is not None:
            key = _unwrap(metadata, _wrapping_key(metadata['database_id']))
            try:
                _verify(key, values)
            except CredentialKeyError:
                raise CredentialKeyError() from None
        else:
            key = _legacy_key(cursor, values, legacy_password)
            wrapping = _wrapping_key(metadata['database_id'], create=True)
            payload = json.dumps({'database_id': metadata['database_id'], 'data_key': key.decode()}).encode()
            metadata['wrapped_dek'] = Fernet(wrapping).encrypt(payload).decode()
            cursor.execute('UPDATE system_settings SET value=%s WHERE key=%s',
                           (json.dumps(metadata), STATE_KEY))
    return key, metadata['database_id']


def _export_key(passphrase, salt):
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise ValueError('Use a recovery passphrase of at least 12 characters')
    return base64.urlsafe_b64encode(Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode()))


def export_recovery(db, output, passphrase):
    """Write a passphrase-encrypted portable wrapping-key export; no stdout secret."""
    with db.get_db() as conn:
        metadata = _read_state(conn.cursor())
        if metadata is None or metadata['wrapped_dek'] is None:
            raise CredentialKeyError()
        wrapping = _wrapping_key(metadata['database_id'])
        _verify(_unwrap(metadata, wrapping), _encrypted_values(conn.cursor()))
    salt = os.urandom(32)
    payload = json.dumps({'version': 1, 'database_id': metadata['database_id'],
        'wrapped_dek_hash': hashlib.sha256(metadata['wrapped_dek'].encode()).hexdigest(),
        'wrapping_key': wrapping.decode()}).encode()
    package = json.dumps({'version': 1, 'salt': base64.b64encode(salt).decode(),
        'ciphertext': Fernet(_export_key(passphrase, salt)).encrypt(payload).decode()}).encode()
    path = Path(output)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(package)
        stream.flush()
        os.fsync(stream.fileno())
    custody._sync_directory(path.parent)


def import_recovery(db, source, passphrase):
    """Provision this OS user/host for the matching database, never replace a key."""
    try:
        path = Path(source)
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError()
        package = json.loads(path.read_bytes())
        if package['version'] != 1:
            raise ValueError()
        salt = base64.b64decode(package['salt'], validate=True)
        if len(salt) != 32:
            raise ValueError()
        payload = json.loads(Fernet(_export_key(passphrase, salt)).decrypt(package['ciphertext'].encode()))
        wrapping = payload['wrapping_key'].encode()
        Fernet(wrapping)
        with db.get_db() as conn:
            metadata = _read_state(conn.cursor(), lock=True)
            if (metadata is None or not metadata['wrapped_dek'] or payload['version'] != 1
                    or payload['database_id'] != metadata['database_id']
                    or payload['wrapped_dek_hash'] != hashlib.sha256(metadata['wrapped_dek'].encode()).hexdigest()):
                raise ValueError()
            _verify(_unwrap(metadata, wrapping), _encrypted_values(conn.cursor()))
            _publish(metadata['database_id'], wrapping)
    except Exception:
        raise CredentialKeyError() from None
