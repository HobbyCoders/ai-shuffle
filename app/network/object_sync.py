"""Durable, immutable cloud bundles of the existing signed encrypted envelopes.

Only a verified manifest commit acknowledges the local outbox. Peer delivery is
independent, and never deletes pending cloud work.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import time
import zlib
from urllib.parse import urlsplit

import httpx

from app.db import network as store
from app.network import crypto, engine, membership

MAX_BYTES = 8 * 1024 * 1024
BATCH_DELAY = 60


class ObjectError(RuntimeError):
    pass


def identity(op):
    return (op["origin_device_id"], op["origin_incarnation"], op["origin_seq"])


def encode(ops, ctx):
    return encode_content("batch", {"ops": ops}, ctx, op_count=len(ops), origin_keys=origin_keys(ops, ctx))


def origin_keys(ops, ctx):
    result = {}
    for op in ops:
        origin = op["origin_device_id"]
        member = ctx.member(origin)
        if member is None:
            raise ObjectError("Operation origin has no verified membership key")
        result[origin] = crypto.b64u_encode(member.signing_public)
    return result


def encode_content(kind, content, ctx, **fields):
    raw = crypto.canonical_json({"v": 2, "network_id": ctx.network_id, **content})
    payload = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(raw) > MAX_BYTES or len(payload) > MAX_BYTES:
        raise ObjectError("Cloud batch is too large")
    descriptor = {"v": 2, "kind": kind, "network_id": ctx.network_id,
                  "publisher": ctx.device_id, "signing_public": crypto.b64u_encode(ctx.identity.signing_public),
                  "object_id": crypto.sha256_hex(payload), "bytes": len(payload), "raw_bytes": len(raw),
                  "op_count": 0, "md5": base64.b64encode(hashlib.md5(payload, usedforsecurity=False).digest()).decode(), **fields}
    descriptor["signature"] = crypto.b64u_encode(crypto.sign(ctx.identity.signing_private, crypto.canonical_json(descriptor)))
    return descriptor, payload


def decode(descriptor, payload, ctx):
    body = decode_content(descriptor, payload, ctx)
    try:
        ops = body["ops"]
        if descriptor["kind"] not in ("batch", "archive") or not isinstance(ops, list) or len(ops) != descriptor["op_count"]:
            raise ObjectError("Cloud object does not match its descriptor")
        if not 0 < len(ops) <= 200 or len({identity(op) for op in ops}) != len(ops):
            raise ObjectError("Cloud object operation identities are invalid")
        return ops
    except (KeyError, TypeError, ValueError) as exc:
        raise ObjectError("Malformed cloud operations") from exc


def decode_content(descriptor, payload, ctx):
    try:
        signed = {k: v for k, v in descriptor.items() if k not in ("signature", "cursor_start", "cursor_end")}
        public = crypto.b64u_decode(signed["signing_public"])
        if (signed["v"] != 2 or signed["network_id"] != ctx.network_id
                or membership.device_id_for(public) != signed["publisher"]
                or not crypto.verify(public, crypto.canonical_json(signed), crypto.b64u_decode(descriptor["signature"]))):
            raise ObjectError("Cloud descriptor signature is invalid")
        if not 0 < len(payload) <= MAX_BYTES or len(payload) != signed["bytes"] or crypto.sha256_hex(payload) != signed["object_id"]:
            raise ObjectError("Cloud object integrity check failed")
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(payload, MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data or len(raw) != signed["raw_bytes"]:
            raise ObjectError("Cloud object expansion is invalid")
        body = json.loads(raw)
        if body["v"] != 2 or body["network_id"] != ctx.network_id:
            raise ObjectError("Cloud object does not match its descriptor")
        return body
    except (KeyError, TypeError, ValueError, zlib.error, crypto.CryptoError) as exc:
        raise ObjectError("Malformed cloud object") from exc


def transfer(grant, payload=None):
    """Bounded direct R2 transfer: no account credentials and no redirects."""
    url = urlsplit(grant["url"])
    if (url.scheme != "https" or not (url.hostname or "").endswith(".r2.cloudflarestorage.com")
            or url.username or url.password or url.port or url.fragment):
        raise ObjectError("Invalid cloud transfer endpoint")
    method = "PUT" if payload is not None else "GET"
    if grant.get("method") != method:
        raise ObjectError("Invalid cloud transfer method")
    headers = grant.get("headers") or {}
    if set(headers) - {"content-length", "content-md5", "if-none-match"}:
        raise ObjectError("Invalid cloud transfer headers")
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:  # noqa: SIM117
            with client.stream(method, grant["url"], headers=headers, content=payload) as response:
                if payload is not None and response.status_code == 412:
                    return b""  # Retry of a create-only object; commit verifies its hash.
                response.raise_for_status()
                result = bytearray()
                for chunk in response.iter_bytes():
                    result.extend(chunk)
                    if len(result) > MAX_BYTES:
                        raise ObjectError("Cloud response is too large")
                return bytes(result)
    except httpx.HTTPError as exc:
        # Signed URLs are bearer credentials; never include them in logs/UI.
        raise ObjectError("Cloud object transfer failed; local work retained") from exc


def _outbox(scope):
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_object_outbox WHERE hub_scope=%s", (scope,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


def push(hub, scope, state):
    row = _outbox(scope)
    ctx = engine.require_context()
    if row is None:
        pushed = int(state.get("pushed_ingest_id") or 0)
        batch = store.hub_ops_after_ingest(pushed, scope, limit=hub.PUSH_BATCH)
        bounded, size = [], 0
        for item in batch:
            size += len(crypto.canonical_json(item[1])) if item[1] is not None else 0
            if bounded and size > MAX_BYTES - 65536:
                break
            bounded.append(item)
        batch = bounded
        if not batch:
            return 0
        ops = [op for _, op in batch if op is not None]
        if not ops:
            hub._save_hub_state(_expected_scope=scope, pushed_ingest_id=batch[-1][0])
            return len(batch)
        due = state.get("object_batch_due")
        if due is None:
            due = time.time() + BATCH_DELAY
            hub._save_hub_state(_expected_scope=scope, object_batch_due=due)
        if len(ops) < hub.PUSH_BATCH and time.time() < due:
            return 0
        proof = hub._call("POST", "objects/known", {"identities": [list(identity(op)) for op in ops]}, max_bytes=MAX_BYTES)
        expected = {identity(op): op for op in ops}
        for item in proof.get("legacy_proofs", []):
            if crypto.sha256_hex(crypto.canonical_json(expected.get(tuple(item["identity"])))) != item["hash"]:
                raise ObjectError("Legacy cloud history conflicts with this operation; local work retained")
        known = {tuple(item) for item in proof["known"]}
        with store.get_db() as conn:
            locked = store.lock_state(conn.cursor())
            if (locked.get("hub") or {}).get("binding_scope") != scope:
                raise ObjectError("Account changed while preparing upload")
            for op in ops:
                if identity(op) in known:
                    store.record_hub_receipt(conn.cursor(), scope, op)
        ops = [op for op in ops if identity(op) not in known]
        if not ops:
            hub._save_hub_state(_expected_scope=scope, pushed_ingest_id=batch[-1][0], object_batch_due=None)
            return len(batch)
        # Keep a single envelope inside the existing 8 MiB request ceiling too.
        while True:
            try:
                description, payload = encode(ops, ctx)
                break
            except ObjectError:
                if len(ops) == 1:
                    raise
                ops = ops[:max(1, len(ops) // 2)]
        included = {identity(op) for op in ops}
        last = pushed
        for ingest_id, op in batch:
            if op is not None and identity(op) not in included and identity(op) not in known:
                break
            last = ingest_id
        with store.get_db() as conn:
            locked = store.lock_state(conn.cursor())
            if (locked.get("hub") or {}).get("binding_scope") != scope:
                raise ObjectError("Account changed while preparing upload")
            conn.cursor().execute("""INSERT INTO sync_object_outbox(hub_scope,object_id,descriptor,payload,last_ingest_id)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(hub_scope) DO NOTHING""",
                                  (scope, description["object_id"], json.dumps(description), base64.b64encode(payload).decode(), last))
        row = _outbox(scope)
    description = store._load(row["descriptor"])
    if not (state.get("object_sync") or {}).get("writes"):
        # Rollback can drain an already prepared outbox through the legacy
        # all-or-nothing protocol. The source log remains until acknowledged.
        ops = decode(description, base64.b64decode(row["payload"]), ctx)
        legacy = hub._call("POST", "ops", {"ops": ops}, max_bytes=65536, timeout=60)
        if legacy.get("quota_exceeded"):
            hub._block_upload(legacy, scope)
        result = {"committed": True, "descriptor": description, "quota": legacy.get("quota")}
    else:
        result = hub._call("POST", "objects/reserve", {"descriptor": description}, max_bytes=65536)
        if result.get("quota_exceeded"):
            hub._block_upload(result, scope)
        if not result.get("committed"):
            transfer(result["upload"], base64.b64decode(row["payload"]))
            result = hub._call("POST", f"objects/{row['object_id']}/commit", {}, max_bytes=65536, timeout=60)
    if not result.get("committed") or (result.get("descriptor") or {}).get("object_id") != row["object_id"]:
        raise ObjectError("Cloud commit was not acknowledged")
    # Cursor advancement and outbox removal are one local transaction.
    with store.get_db() as conn:
        locked = store.lock_state(conn.cursor())
        current = dict(locked.get("hub") or {})
        if current.get("binding_scope") != scope:
            raise ObjectError("Account changed before cloud acknowledgement")
        current.update(pushed_ingest_id=max(int(current.get("pushed_ingest_id") or 0), int(row["last_ingest_id"])),
                       last_push_at=time.time(), object_batch_due=None, upload_blocked=None, quota=result.get("quota"))
        store.update_state(_conn=conn, hub=current)
        conn.cursor().execute("DELETE FROM sync_object_outbox WHERE hub_scope=%s AND object_id=%s", (scope, row["object_id"]))
    return description["op_count"]


def pull_entry(hub, scope, entry):
    if "object" not in entry:
        ops = entry.get("ops") or []
    else:
        description = entry["object"]
        grant = hub._call("POST", f"objects/{description['object_id']}/download", {}, max_bytes=65536)
        if grant["descriptor"] != description:
            raise ObjectError("Cloud manifest changed during download")
        ops = decode(description, transfer(grant["download"]), engine.require_context())
    # Unknown members/rotation keys may arrive in a later bundle. Keep those
    # encrypted envelopes durably while advancing the download cursor; never
    # confuse that cursor with an applied per-origin watermark.
    from app.network.envelope import Envelope
    ctx = engine.require_context()
    for op in ops:
        env = Envelope.from_dict(op)
        member = ctx.member(env.origin_device_id)
        public = ctx.identity.signing_public if env.origin_device_id == ctx.device_id else member.signing_public if member else None
        if env.network_id != ctx.network_id or (public is not None and not env.verify_signature(public)):
            raise ObjectError("Cloud operation signature did not verify")
    entry_id = (entry.get("object") or {}).get("object_id") or str(entry["cursor"])
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        if (state.get("hub") or {}).get("binding_scope") != scope:
            raise ObjectError("Account changed before downloaded operations were stored")
        conn.cursor().execute('''INSERT INTO sync_cloud_inbox(hub_scope,entry_id,ops) VALUES(%s,%s,%s)
            ON CONFLICT(hub_scope,entry_id) DO NOTHING''', (scope, entry_id, json.dumps(ops)))
    return drain_inbox(scope)


def inbox_count(scope):
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ops FROM sync_cloud_inbox WHERE hub_scope=%s', (scope,))
        return sum(len(store._load(row['ops'])) for row in cursor.fetchall())


def drain_inbox(scope):
    applied = 0
    while True:
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM sync_cloud_inbox WHERE hub_scope=%s ORDER BY entry_id', (scope,))
            rows = [dict(row) for row in cursor.fetchall()]
        progress = False
        for row in rows:
            remaining = []
            previous = store._load(row['ops'])
            for op in previous:
                result = engine.apply_batch([op], hub_scope=scope)
                applied += result.applied
                if result.rejected and not all(reason in ('origin revoked', 'stale incarnation') for reason in result.reasons):
                    remaining.append(op)
            if len(remaining) == len(previous):
                continue
            progress = True
            with store.get_db() as conn:
                state = store.lock_state(conn.cursor())
                if (state.get('hub') or {}).get('binding_scope') != scope:
                    raise ObjectError('Account changed while applying downloaded operations')
                if remaining:
                    conn.cursor().execute('UPDATE sync_cloud_inbox SET ops=%s WHERE hub_scope=%s AND entry_id=%s',
                                          (json.dumps(remaining), scope, row['entry_id']))
                else:
                    conn.cursor().execute('DELETE FROM sync_cloud_inbox WHERE hub_scope=%s AND entry_id=%s', (scope, row['entry_id']))
        if not progress:
            return applied


def archive_once(hub, scope):
    """Archive one verified legacy range; the D1 source is never deleted here."""
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_archive_outbox WHERE hub_scope=%s", (scope,))
        row = cursor.fetchone()
    if row is None:
        last = int(hub._hub_state().get("archive_cursor") or 0)
        page = hub._call("POST", "objects/archive-page", {"cursor": last}, max_bytes=MAX_BYTES, timeout=60)
        ops = page.get("ops") or []
        if not ops:
            if page["cursor"] > last:
                hub._save_hub_state(_expected_scope=scope, archive_cursor=page["cursor"])
                return True
            return False
        ctx = engine.require_context()
        d, payload = encode_content("archive", {"ops": ops, "archive_range": [page["source_start"], page["cursor"]]}, ctx,
                                    source_start=page["source_start"], source_end=page["cursor"], op_count=len(ops), origin_keys=origin_keys(ops, ctx))
        # Authenticate/decrypt each historical envelope locally before migration.
        # Existing trusted origins and retained rotation keys are required.
        from app.network import envelope, keyring
        for op in ops:
            env = envelope.Envelope.from_dict(op)
            member = ctx.member(env.origin_device_id)
            if member is None or not env.verify_signature(member.signing_public):
                raise ObjectError("Legacy history signature did not verify")
            keyring.open_envelope(ctx, env)
        with store.get_db() as conn:
            state = store.lock_state(conn.cursor())
            if (state.get("hub") or {}).get("binding_scope") != scope:
                raise ObjectError("Account changed while archiving history")
            conn.cursor().execute("""INSERT INTO sync_archive_outbox(hub_scope,descriptor,payload,cursor_end)
                VALUES(%s,%s,%s,%s) ON CONFLICT(hub_scope) DO NOTHING""",
                                  (scope, json.dumps(d), base64.b64encode(payload).decode(), page["cursor"]))
        # Re-read the actual durable winner if another local caller prepared it.
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sync_archive_outbox WHERE hub_scope=%s", (scope,))
            row = cursor.fetchone()
    d = store._load(row["descriptor"])
    result = hub._call("POST", "objects/reserve", {"descriptor": d}, max_bytes=65536)
    if result.get("quota_exceeded"):
        hub._block_upload(result, scope)
    if not result.get("committed"):
        transfer(result["upload"], base64.b64decode(row["payload"]))
        result = hub._call("POST", f"objects/{d['object_id']}/commit", {}, max_bytes=65536, timeout=60)
    if not result.get("committed") or result["descriptor"]["object_id"] != d["object_id"]:
        raise ObjectError("Legacy archive was not committed")
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        current = dict(state.get("hub") or {})
        if current.get("binding_scope") != scope:
            raise ObjectError("Account changed before archive acknowledgement")
        current["archive_cursor"] = max(int(current.get("archive_cursor") or 0), int(row["cursor_end"]))
        store.update_state(_conn=conn, hub=current)
        conn.cursor().execute("DELETE FROM sync_archive_outbox WHERE hub_scope=%s", (scope,))
    return True
