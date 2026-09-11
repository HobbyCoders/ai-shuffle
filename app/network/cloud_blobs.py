"""Content-addressed encrypted file backups, independent of state snapshots."""
from __future__ import annotations

import base64
import json

from app.db import network as store
from app.network import blobs, checkpoints, crypto, engine, object_sync

PART_BYTES = 2 * 1024 * 1024


def receipt(scope, digest, _conn=None):
    if _conn is None:
        with store.get_db() as conn:
            return receipt(scope, digest, conn)
    cursor = _conn.cursor()
    cursor.execute('SELECT descriptor FROM sync_cloud_blobs WHERE hub_scope=%s AND hash=%s', (scope, digest))
    row = cursor.fetchone()
    return store._load(row['descriptor']) if row else None


def _remember(scope, digest, descriptor, conn):
    state = store.lock_state(conn.cursor())
    if (state.get('hub') or {}).get('binding_scope') != scope:
        raise object_sync.ObjectError('Account changed while persisting file backup')
    conn.cursor().execute('''INSERT INTO sync_cloud_blobs(hub_scope,hash,descriptor) VALUES(%s,%s,%s)
        ON CONFLICT(hub_scope,hash) DO UPDATE SET descriptor=EXCLUDED.descriptor''', (scope, digest, json.dumps(descriptor)))


def open_file(hub, descriptor, ctx, body=None):
    if descriptor.get('kind') != 'blob':
        raise object_sync.ObjectError('Invalid file manifest kind')
    if body is None:
        grant = hub._call('POST', f"objects/{descriptor['object_id']}/download", {}, max_bytes=65536)
        body = object_sync.decode_content(descriptor, object_sync.transfer(grant['download']), ctx)
    manifest = checkpoints._open(body['sealed'], ctx)
    if (manifest['hash'] != descriptor['blob_hash'] or manifest['size'] != descriptor['file_bytes']
            or [d['object_id'] for d in manifest['parts']] != descriptor['parts'] or not 0 <= manifest['size'] <= blobs.MAX_BLOB_BYTES):
        raise object_sync.ObjectError('File manifest does not match its descriptor')
    data = bytearray()
    for part in manifest['parts']:
        if part['kind'] != 'checkpoint_part' or part['publisher'] != descriptor['publisher']:
            raise object_sync.ObjectError('Invalid file part publisher')
        grant = hub._call('POST', f"objects/{part['object_id']}/download", {}, max_bytes=65536)
        content = object_sync.decode_content(part, object_sync.transfer(grant['download']), ctx)
        fragment = checkpoints._open(content['blob'], ctx)
        if fragment['hash'] != manifest['hash'] or fragment['offset'] != len(data):
            raise object_sync.ObjectError('File parts are out of order')
        data.extend(base64.b64decode(fragment['data'], validate=True))
        if len(data) > manifest['size']:
            raise object_sync.ObjectError('File exceeds its declared length')
    if len(data) != manifest['size'] or crypto.sha256_hex(data) != manifest['hash']:
        raise object_sync.ObjectError('Cloud file failed integrity verification')
    return bytes(data)


def fetch_one(hub, scope, digest):
    descriptor = receipt(scope, digest)
    if descriptor is None:
        descriptor = hub._call('POST', 'objects/blob', {'hash': digest}, max_bytes=65536).get('file')
    if descriptor is None:
        return False
    data = open_file(hub, descriptor, engine.require_context())
    if not blobs.store_fetched(digest, data):
        return False
    with store.get_db() as conn:
        _remember(scope, digest, descriptor, conn)
    return True


def prepare(hub, scope, digest):
    ctx = engine.require_context()
    # Another device may already have durably backed up the identical file.
    existing = hub._call('POST', 'objects/blob', {'hash': digest}, max_bytes=65536).get('file')
    if existing is not None:
        data = open_file(hub, existing, ctx)
        if crypto.sha256_hex(data) != digest:
            raise object_sync.ObjectError('Existing cloud file did not verify')
        with store.get_db() as conn:
            _remember(scope, digest, existing, conn)
        return
    data = blobs.read_local(digest)
    if data is None or crypto.sha256_hex(data) != digest or len(data) > blobs.MAX_BLOB_BYTES:
        raise object_sync.ObjectError('Local file is unavailable for cloud backup')
    parts = []
    for offset in range(0, len(data), PART_BYTES):
        sealed = checkpoints._seal({'hash': digest, 'offset': offset,
                                    'data': base64.b64encode(data[offset:offset + PART_BYTES]).decode()}, ctx)
        parts.append(object_sync.encode_content('checkpoint_part', {'blob': sealed}, ctx))
    manifest = {'hash': digest, 'size': len(data), 'parts': [d for d, _ in parts]}
    parts.append(object_sync.encode_content('blob', {'sealed': checkpoints._seal(manifest, ctx)}, ctx,
                                           blob_hash=digest, file_bytes=len(data), parts=[d['object_id'] for d, _ in parts]))
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        if (state.get('hub') or {}).get('binding_scope') != scope:
            raise object_sync.ObjectError('Account changed before file backup')
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM sync_blob_outbox WHERE hub_scope=%s AND hash=%s LIMIT 1', (scope, digest))
        if cursor.fetchone():
            return
        for ordinal, (d, payload) in enumerate(parts):
            cursor.execute('''INSERT INTO sync_blob_outbox(hub_scope,hash,ordinal,descriptor,payload)
                VALUES(%s,%s,%s,%s,%s)''', (scope, digest, ordinal, json.dumps(d), base64.b64encode(payload).decode()))


def publish_once(hub, scope):
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sync_blob_outbox WHERE hub_scope=%s AND committed=FALSE ORDER BY hash,ordinal LIMIT 1', (scope,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute('''SELECT b.hash FROM sync_blobs b WHERE b.present=TRUE AND NOT EXISTS
                (SELECT 1 FROM sync_cloud_blobs c WHERE c.hub_scope=%s AND c.hash=b.hash) ORDER BY b.hash LIMIT 1''', (scope,))
            missing = cursor.fetchone()
    if row is None:
        if not missing:
            return False
        prepare(hub, scope, missing['hash'])
        return True
    d = store._load(row['descriptor'])
    result = hub._call('POST', 'objects/reserve', {'descriptor': d}, max_bytes=65536)
    if result.get('quota_exceeded'):
        hub._block_upload(result, scope)
    if not result.get('committed'):
        object_sync.transfer(result['upload'], base64.b64decode(row['payload']))
        result = hub._call('POST', f"objects/{d['object_id']}/commit", {}, max_bytes=65536, timeout=60)
    if not result.get('committed') or result['descriptor']['object_id'] != d['object_id']:
        raise object_sync.ObjectError('File backup was not committed')
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        if (state.get('hub') or {}).get('binding_scope') != scope:
            raise object_sync.ObjectError('Account changed before file backup acknowledgement')
        if d['kind'] == 'blob':
            _remember(scope, row['hash'], result['descriptor'], conn)
            conn.cursor().execute('DELETE FROM sync_blob_outbox WHERE hub_scope=%s AND hash=%s', (scope, row['hash']))
        else:
            conn.cursor().execute('UPDATE sync_blob_outbox SET committed=TRUE WHERE hub_scope=%s AND hash=%s AND ordinal=%s',
                                  (scope, row['hash'], row['ordinal']))
    return True


def pending_count(scope):
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT COUNT(*) AS n FROM sync_blobs b WHERE b.present=TRUE AND NOT EXISTS
            (SELECT 1 FROM sync_cloud_blobs c WHERE c.hub_scope=%s AND c.hash=b.hash)''', (scope,))
        return int(cursor.fetchone()['n'])
