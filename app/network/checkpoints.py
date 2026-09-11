"""Verified, encrypted recovery checkpoints, published only after all parts.

Capture uses one repeatable-read database view. Publication may take many loop
passes; failed uploads retain prepared bytes and the last committed checkpoint.
Nothing here deletes historical operations, remote objects, or user records.
"""
from __future__ import annotations

import base64
import copy
import gzip
import json
import time
import zlib

from app.db import network as store
from app.network import crypto, engine, keyring, membership, object_sync, policy

CHECKPOINT_INTERVAL = 86400
CHECKPOINT_OP_INTERVAL = 5000


def _seal(value, ctx):
    aad = crypto.canonical_json({"kind": "checkpoint", "network_id": ctx.network_id, "key_version": ctx.key_version})
    raw = gzip.compress(crypto.canonical_json(value), mtime=0)
    return {"key_version": ctx.key_version, "ciphertext": crypto.encrypt_payload(ctx.network_key, raw, aad)}


def _open(value, ctx):
    aad = crypto.canonical_json({"kind": "checkpoint", "network_id": ctx.network_id, "key_version": value["key_version"]})
    raw = keyring.open_payload(ctx, value["key_version"], value["ciphertext"], aad)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    plain = decoder.decompress(raw, object_sync.MAX_BYTES + 1)
    if len(plain) > object_sync.MAX_BYTES or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise object_sync.ObjectError("Invalid checkpoint expansion")
    return json.loads(plain)


def _pending(scope, *, all_rows=False):
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_checkpoint_outbox WHERE hub_scope=%s" +
                       (" ORDER BY ordinal" if all_rows else " AND committed=FALSE ORDER BY ordinal LIMIT 1"), (scope,))
        return [dict(row) for row in cursor.fetchall()]


def capture(hub, scope):
    """Durably prepare a consistent checkpoint without any cloud side effects."""
    from app.network import blobs
    blobs.backfill_attachment_hashes()
    while blobs.backfill_artifact_inventory():
        pass
    engine.scan_all()
    if hub._backlog(hub._hub_state()):
        return False
    if object_sync.inbox_count(scope):
        return False
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        state = store.lock_state(cursor)
        if (state.get("hub") or {}).get("binding_scope") != scope:
            raise object_sync.ObjectError("Account changed before checkpoint capture")
        cursor.execute("SELECT COUNT(*) AS n FROM sync_pending")
        if cursor.fetchone()["n"]:
            return False  # No watermark may cover a missing sequence prefix.
        cursor.execute("SELECT COUNT(*) AS n FROM sync_blobs WHERE present=FALSE")
        if cursor.fetchone()["n"]:
            return False  # Never claim a complete checkpoint with missing files.
        ctx = engine.load_context(conn)
        if not keyring.safe(ctx, crypto.sha256_hex(ctx.network_key)):
            return False
        parts = []
        chunks = []
        for collection in policy.ordered_collections():
            offset, limit = 0, 100
            while True:
                chunk = engine.snapshot_chunk(collection.name, offset, ctx=ctx, limit=limit, _conn=conn)
                if not chunk["count"]:
                    break
                try:
                    d, payload = object_sync.encode_content("checkpoint_part", {"chunk": chunk}, ctx)
                except object_sync.ObjectError:
                    if limit == 1:
                        raise
                    limit = max(1, limit // 2)
                    continue
                parts.append((d, payload))
                chunks.append({"object_id": d["object_id"], "collection": collection.name,
                               "offset": offset, "count": chunk["count"], "more": chunk["more"]})
                offset += chunk["count"]
                if not chunk["more"]:
                    break
        # Reuse verified file backups; never rewrite file contents per snapshot.
        from app.network import cloud_blobs
        cursor.execute("SELECT hash,size FROM sync_blobs WHERE present=TRUE ORDER BY hash")
        files = []
        for blob in cursor.fetchall():
            d = cloud_blobs.receipt(scope, blob["hash"], conn)
            if d is None:
                return False
            files.append({"hash": blob["hash"], "size": int(blob["size"]), "object": d})
        keys = {str(version): crypto.b64u_encode(key) for version, key in ctx.keyring.items()}
        for version, candidates in ctx.key_candidates.items():
            for digest, key in candidates.items():
                keys[f"{version}@{digest}"] = crypto.b64u_encode(key)
        base_cursor = int((state.get("hub") or {}).get("cursor") or 0)
        content = {"network_id": ctx.network_id, "founder_device_id": ctx.founder_device_id,
                   "generation": ctx.generation, "base_cursor": base_cursor, "publisher": ctx.device_id,
                   "membership": [row["record"] for row in store.list_membership_records(conn)],
                   "keys": keys, "key_recipients": {k: sorted(v) for k, v in ctx.key_recipients.items()},
                   "key_generations": ctx.key_generations, "watermarks": store.local_watermarks(conn),
                   "legacy_instance_map": ctx.legacy_instance_map, "chunks": chunks, "files": files,
                   "parts": [d for d, _ in parts] + [f["object"] for f in files]}
        root, payload = object_sync.encode_content("checkpoint", {"sealed": _seal(content, ctx)}, ctx,
                                                  parts=[d["object_id"] for d in content["parts"]],
                                                  base_cursor=base_cursor, generation=ctx.generation,
                                                  key_version=ctx.key_version, key_digest=crypto.sha256_hex(ctx.network_key))
        parts.append((root, payload))
        if len(parts) > 4097:
            raise object_sync.ObjectError("Checkpoint has too many parts")
        for ordinal, (d, payload) in enumerate(parts):
            cursor.execute("""INSERT INTO sync_checkpoint_outbox(hub_scope,ordinal,descriptor,payload)
                VALUES(%s,%s,%s,%s)""", (scope, ordinal, json.dumps(d), base64.b64encode(payload).decode()))
    return True


def publish_once(hub, scope):
    rows = _pending(scope)
    state = hub._hub_state()
    ctx = engine.require_context()
    frontier = f"{ctx.generation}:{ctx.key_version}:{crypto.sha256_hex(ctx.network_key)}"
    if rows:
        root = store._load(_pending(scope, all_rows=True)[-1]["descriptor"])
        if (root.get("generation"), root.get("key_version"), root.get("key_digest")) != (
                ctx.generation, ctx.key_version, crypto.sha256_hex(ctx.network_key)):
            # Only the local plan is stale. Retain remote parts and the last root.
            with store.get_db() as conn:
                conn.cursor().execute("DELETE FROM sync_checkpoint_outbox WHERE hub_scope=%s", (scope,))
            rows = []
    if not rows:
        completed = _pending(scope, all_rows=True)
        if completed:
            root = store._load(completed[-1]["descriptor"])
            committed_frontier = f"{root['generation']}:{root['key_version']}:{root['key_digest']}"
            hub._save_hub_state(_expected_scope=scope, checkpoint_id=root["object_id"], checkpoint_cursor=root["base_cursor"],
                                checkpoint_last_at=time.time(), checkpoint_frontier=committed_frontier)
            with store.get_db() as conn:
                conn.cursor().execute("DELETE FROM sync_checkpoint_outbox WHERE hub_scope=%s", (scope,))
            return False
        delta = int(state.get("cursor") or 0) - int(state.get("checkpoint_cursor") or 0)
        if state.get("checkpoint_id") and state.get("checkpoint_frontier") == frontier and (delta <= 0 or
                (delta < CHECKPOINT_OP_INTERVAL and time.time() - state.get("checkpoint_last_at", 0) < CHECKPOINT_INTERVAL)):
            return False
        if not capture(hub, scope):
            return False
        rows = _pending(scope)
    row = rows[0]
    d = store._load(row["descriptor"])
    result = hub._call("POST", "objects/reserve", {"descriptor": d}, max_bytes=1 << 20)
    if result.get("quota_exceeded"):
        hub._block_upload(result, scope)
    if not result.get("committed"):
        object_sync.transfer(result["upload"], base64.b64decode(row["payload"]))
        result = hub._call("POST", f"objects/{d['object_id']}/commit", {}, max_bytes=1 << 20, timeout=60)
    if not result.get("committed") or result["descriptor"]["object_id"] != d["object_id"]:
        raise object_sync.ObjectError("Checkpoint commit was not acknowledged")
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        if (state.get("hub") or {}).get("binding_scope") != scope:
            raise object_sync.ObjectError("Account changed while publishing checkpoint")
        conn.cursor().execute("UPDATE sync_checkpoint_outbox SET committed=TRUE WHERE hub_scope=%s AND ordinal=%s",
                              (scope, row["ordinal"]))
    return True


def verify_history(content, ctx):
    """Rebuild the signed membership chain before trusting snapshot data or keys."""
    candidate = copy.deepcopy(ctx)
    keys = {}
    for label, encoded in content["keys"].items():
        key = crypto.b64u_decode(encoded)
        if len(key) != 32:
            raise object_sync.ObjectError("Invalid historical checkpoint key")
        if '@' in label and crypto.sha256_hex(key) != label.split('@', 1)[1]:
            raise object_sync.ObjectError("Historical checkpoint key hash mismatch")
        keys[label] = key
    members = {}
    for record in content["membership"]:
        attempts = list(keys.values()) if record.get("type") == membership.TYPE_RECOVERY else [None]
        for key in attempts:
            try:
                members = membership.apply_record(record, network_id=ctx.network_id, members=dict(members),
                                                  founder_device_id=ctx.founder_device_id, network_key=key)
                break
            except membership.MembershipError:
                continue
        else:
            raise object_sync.ObjectError("Checkpoint membership history did not verify")
    publisher = members.get(content["publisher"])
    if publisher is None or publisher.status != membership.STATUS_ACTIVE:
        raise object_sync.ObjectError("Checkpoint publisher is not an active member")
    candidate.members = members
    candidate.keyring = {int(label): key for label, key in keys.items() if label.isdigit()}
    candidate.key_candidates = {}
    for label, key in keys.items():
        candidate.key_candidates.setdefault(int(label.split('@')[0]), {})[crypto.sha256_hex(key)] = key
    candidate.key_generations = content["key_generations"]
    candidate.key_recipients = {k: set(v) for k, v in content["key_recipients"].items()}
    return candidate


def restore(hub, scope):
    ctx = engine.require_context()
    root = hub._call("POST", "objects/checkpoint", {"key_digest": crypto.sha256_hex(ctx.network_key)}, max_bytes=1 << 20).get("checkpoint")
    if root is None:
        return False
    grant = hub._call("POST", f"objects/{root['object_id']}/download", {}, max_bytes=1 << 20)
    sealed = object_sync.decode_content(root, object_sync.transfer(grant["download"]), ctx)
    content = _open(sealed["sealed"], ctx)
    if (content["network_id"] != ctx.network_id or content["founder_device_id"] != ctx.founder_device_id
            or content["generation"] != root["generation"]
            or content["base_cursor"] != root["base_cursor"] or content["publisher"] != root["publisher"]
            or [d["object_id"] for d in content["parts"]] != root["parts"]):
        raise object_sync.ObjectError("Checkpoint manifest does not match its signed frontier")
    trusted = verify_history(content, ctx)
    if crypto.b64u_encode(trusted.members[root["publisher"]].signing_public) != root["signing_public"]:
        raise object_sync.ObjectError("Checkpoint signing key is not in membership history")
    parts = {}
    for d in content["parts"]:
        if d["kind"] not in ("checkpoint_part", "blob") or (d["kind"] == "checkpoint_part" and d["publisher"] != root["publisher"]):
            raise object_sync.ObjectError("Invalid checkpoint part publisher")
        grant = hub._call("POST", f"objects/{d['object_id']}/download", {}, max_bytes=65536)
        parts[d["object_id"]] = object_sync.decode_content(d, object_sync.transfer(grant["download"]), trusted)
    offsets = {}
    for expected in content["chunks"]:
        chunk = parts[expected["object_id"]]["chunk"]
        body = engine.open_snapshot_chunk(chunk, ctx=trusted)
        if (body["collection"] != expected["collection"] or body["offset"] != expected["offset"]
                or len(body["items"]) != expected["count"] or body["more"] != expected["more"]
                or body["offset"] != offsets.get(body["collection"], 0)):
            raise object_sync.ObjectError("Checkpoint snapshot pages are incomplete")
        offsets[body["collection"]] = body["offset"] + len(body["items"])
    for collection in offsets:
        if [c for c in content["chunks"] if c["collection"] == collection][-1]["more"]:
            raise object_sync.ObjectError("Checkpoint snapshot has an incomplete final page")
    files = []
    from app.network import cloud_blobs
    for item in content["files"]:
        data = cloud_blobs.open_file(hub, item["object"], trusted, parts[item["object"]["object_id"]])
        if len(data) != item["size"] or crypto.sha256_hex(data) != item["hash"]:
            raise object_sync.ObjectError("Checkpoint attachment failed verification")
        files.append((item["hash"], bytes(data)))
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        current = dict(state.get("hub") or {})
        if current.get("binding_scope") != scope or state["bootstrap_state"] != engine.BOOTSTRAP_SYNCING:
            raise object_sync.ObjectError("Checkpoint restoration no longer matches this bootstrap")
        # The new device's admission/recovery certificate remains authoritative;
        # historical keys cannot create or replace its admission.
        trusted.members[ctx.device_id] = ctx.members[ctx.device_id]
        for record in content["membership"]:
            if record["type"] in (membership.TYPE_CERTIFICATE, membership.TYPE_RECOVERY):
                record_id = f"cert:{record['device_id']}:{record['generation']}"
            elif record["type"] == membership.TYPE_REVOCATION:
                record_id = f"rev:{record['device_id']}:{record['generation']}"
            else:
                record_id = keyring.record_id(record)
            store.put_membership_record(record_id, record, _conn=conn)
        for member in trusted.members.values():
            store.upsert_member(device_id=member.device_id, name=member.name, kind=member.kind,
                                signing_public=crypto.b64u_encode(member.signing_public),
                                encryption_public=crypto.b64u_encode(member.encryption_public), generation=member.generation,
                                status=member.status, last_seq_accepted=member.last_seq_accepted, _conn=conn)
        wrapped = keyring.for_recipient(trusted, ctx.identity.encryption_public)
        # Keep the recovery authorization key selected; never roll it backward.
        wrapped.update({str(v): crypto.wrap_key(ctx.identity.encryption_public, k) for v, k in ctx.keyring.items()})
        store.update_state(_conn=conn, keyring=wrapped, legacy_instance_map=content["legacy_instance_map"])
        for expected in content["chunks"]:
            engine.apply_snapshot_chunk(parts[expected["object_id"]]["chunk"], ctx=trusted, _conn=conn, validated_membership=True)
        engine.adopt_watermarks(content["watermarks"], ctx=trusted, _conn=conn)
        current["cursor"] = max(int(current.get("cursor") or 0), int(content["base_cursor"]))
        store.update_state(_conn=conn, hub=current)
    engine.load_context()
    from app.network import blobs
    for digest, data in files:
        if not blobs.store_fetched(digest, data):
            raise object_sync.ObjectError("Checkpoint attachment could not be persisted")
        with store.get_db() as conn:
            cloud_blobs._remember(scope, digest, next(item["object"] for item in content["files"] if item["hash"] == digest), conn)
    hub._save_hub_state(_expected_scope=scope, restored_checkpoint=root["object_id"])
    return True
