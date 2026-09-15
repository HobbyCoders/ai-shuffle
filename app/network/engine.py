"""The sync engine: outbox, inbox, apply, conflicts, snapshot.

One worker, one private database, one ``NetworkContext`` in memory (this
device's identity, the network it belongs to, the decrypted key ring and the
members it trusts). Everything below runs inside the caller's transaction
cursor so a row write and its outbox entry commit together.

Write path
    ``record(cursor, collection, record_id, row)`` serializes the row,
    compares it with what the network already knows (``sync_records``) and,
    when it changed, bumps the Lamport clock, allocates the next origin
    sequence, seals and signs an envelope and appends it to ``sync_ops``.
    ``scan_collection`` does the same for every row of a table so domain
    code needs no per-function hooks; ``record`` is for hot paths.

Read path
    ``apply_envelope`` verifies, orders (per origin stream, holes are held in
    ``sync_pending``), decrypts, decides by last-writer-wins, materializes the
    row in the domain table, keeps the loser in ``sync_conflicts``, stores the
    envelope for forwarding and advances the watermark. All in one
    transaction.

Bootstrap
    ``snapshot_chunks`` serves a member the current records of every
    collection; ``apply_snapshot_chunk`` applies them. A joining worker keeps
    ``bootstrap_state='syncing'`` (no outbox) until the snapshot is complete.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from app.core import encryption
from app.db import network as store
from app.db.base import get_db
from app.network import crypto, envelope as envelope_mod, identity as identity_mod, membership, policy
from app.network.envelope import Envelope, OP_DELETE, OP_UPSERT
from app.network.membership import Member

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_rollback_lock = threading.Lock()
_context: Optional["NetworkContext"] = None
_listeners: List[Callable[[List[Tuple[str, str, str]]], None]] = []
_kick: Optional[Callable[[], None]] = None

BOOTSTRAP_READY = "ready"
BOOTSTRAP_SYNCING = "syncing"
_REVISION_TABLES = {"user_preferences", "system_settings", "model_providers"}
_HISTORY_COLLECTIONS = {"session_events", "artifacts", "note_attachments"}


class NotEnrolled(RuntimeError):
    """This worker has no network yet."""


class ApplyError(ValueError):
    """An envelope was rejected for good; the message says why (no secrets).

    A permanent rejection at the head of a stream advances the watermark past
    the operation: the same identity can never carry a different, valid
    operation, so retrying it would only stall everything behind it.
    """


class TransientApplyError(ApplyError):
    """An envelope cannot be applied *yet* (a key or certificate from another
    stream has not arrived, credential storage is locked). The watermark stays
    put and the peer resends it on the next pass."""


# Storage errors usually describe the operation, not the database. Foreign-key
# violations are the exception: a parent from another stream may arrive later.
# Connection loss, disk exhaustion and other operational errors also retry.
def _permanent_db_errors() -> tuple:
    import sqlite3
    import psycopg2
    return (psycopg2.IntegrityError, psycopg2.DataError, sqlite3.IntegrityError, sqlite3.DataError)


def _savepoint(cursor, statement: str) -> None:
    """PostgreSQL aborts the whole transaction on a statement error, so the
    apply of one op runs under a savepoint. SQLite (the test shim) fails only
    the statement and cannot nest savepoints under the shim; skip it there."""
    if type(cursor).__module__.startswith("psycopg2"):
        cursor.execute(statement)


# Out-of-order ops are buffered per stream. Beyond these bounds a peer is
# either broken or hostile; its later ops are re-requested once the hole fills.
PENDING_CAP_PER_STREAM = 5000
PENDING_WINDOW = 20000

_outbox_held = False


def hold_outbox(held: bool) -> None:
    """Keep local writes out of the outbox until the transport has compared
    this device's stream with a peer (see ``transport.sync_pass``): a worker
    restored from a backup must learn its new incarnation before it allocates
    sequence numbers that peers already hold under different content."""
    global _outbox_held
    _outbox_held = bool(held)


def outbox_held() -> bool:
    return _outbox_held


@dataclass
class NetworkContext:
    identity: identity_mod.DeviceIdentity
    network_id: str
    name: Optional[str]
    founder_device_id: str
    generation: int
    key_version: int
    keyring: Dict[int, bytes]
    members: Dict[str, Member]
    bootstrap_state: str
    legacy_instance_map: Dict[str, str] = field(default_factory=dict)
    key_candidates: Dict[int, Dict[str, bytes]] = field(default_factory=dict)
    key_recipients: Dict[str, set] = field(default_factory=dict)
    key_generations: Dict[str, int] = field(default_factory=dict)

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    @property
    def network_key(self) -> bytes:
        return self.keyring[self.key_version]

    def member(self, device_id: str) -> Optional[Member]:
        return self.members.get(device_id)

    def active_members(self) -> List[Member]:
        return [m for m in self.members.values() if m.status == membership.STATUS_ACTIVE]

    def summary(self) -> dict:
        return {
            "network_id": self.network_id,
            "name": self.name,
            "device_id": self.device_id,
            "fingerprint": self.identity.fingerprint,
            "founder_device_id": self.founder_device_id,
            "generation": self.generation,
            "key_version": self.key_version,
            "bootstrap_state": self.bootstrap_state,
            "member_count": len(self.active_members()),
        }


# ---------------------------------------------------------------------------
# Context lifecycle
# ---------------------------------------------------------------------------

def _member_from_row(row: Mapping[str, Any]) -> Member:
    return Member(device_id=row["device_id"], signing_public=crypto.b64u_decode(row["signing_public"]),
                  encryption_public=crypto.b64u_decode(row["encryption_public"]), name=row["name"],
                  kind=row["kind"], generation=int(row["generation"]), status=row["status"],
                  last_seq_accepted=row.get("last_seq_accepted"),
                  last_incarnation_accepted=row.get("last_incarnation_accepted"))


def _unwrap_keyring(wrapped: Mapping[str, Any], device_identity: identity_mod.DeviceIdentity) -> Dict[int, bytes]:
    keyring: Dict[int, bytes] = {}
    for version, wrap in (wrapped or {}).items():
        if not str(version).isdigit():
            continue
        try:
            keyring[int(version)] = crypto.unwrap_key(device_identity.encryption_private, wrap)
        except (crypto.CryptoError, ValueError, TypeError):
            logger.error("Network key version %s does not open with this device's key", version)
    return keyring


def load_context(_conn=None) -> Optional[NetworkContext]:
    """(Re)build the in-memory context from the database. None when not enrolled."""
    global _context
    state = store.get_state(_conn)
    if state is None:
        with _lock:
            _context = None
        return None
    device_identity = identity_mod.load(create=False)
    keyring = _unwrap_keyring(state.get("keyring") or {}, device_identity)
    members = {row["device_id"]: _member_from_row(row) for row in store.list_members(_conn)}
    ctx = NetworkContext(
        identity=device_identity, network_id=state["network_id"], name=state.get("name"),
        founder_device_id=state["founder_device_id"], generation=int(state["generation"]),
        key_version=int(state["key_version"]), keyring=keyring, members=members,
        bootstrap_state=state.get("bootstrap_state") or BOOTSTRAP_READY,
        legacy_instance_map=dict(state.get("legacy_instance_map") or {}),
    )
    from app.network import keyring as keyring_store
    keyring_store.restore(ctx, state.get("keyring") or {})
    if ctx.key_version not in keyring:
        logger.error("Network key version %d is not available on this device", ctx.key_version)
    with _lock:
        _context = ctx
    return ctx


def context(*, refresh: bool = False) -> Optional[NetworkContext]:
    with _lock:
        if _context is not None and not refresh:
            return _context
    return load_context()


def require_context() -> NetworkContext:
    ctx = context()
    if ctx is None:
        raise NotEnrolled("This worker has not joined a network")
    if ctx.key_version not in ctx.keyring:
        raise NotEnrolled("The network key is not available on this device")
    return ctx


def reset_for_tests() -> None:
    global _context, _kick
    with _lock:
        _context = None
        _listeners.clear()
        _kick = None


def on_applied(listener: Callable[[List[Tuple[str, str, str]]], None]) -> None:
    """Register a post-commit listener: receives [(collection, record_id, op)]."""
    _listeners.append(listener)


def set_kick(callback: Optional[Callable[[], None]]) -> None:
    """The transport registers a callback fired after new local ops exist."""
    global _kick
    _kick = callback


def kick() -> None:
    if _kick is not None:
        try:
            _kick()
        except Exception:  # noqa: BLE001
            logger.debug("Sync kick failed", exc_info=True)


def _notify(touched: List[Tuple[str, str, str]]) -> None:
    if not touched:
        return
    for listener in list(_listeners):
        try:
            listener(touched)
        except Exception:  # noqa: BLE001
            logger.warning("Sync listener failed", exc_info=True)


# ---------------------------------------------------------------------------
# Creating a network of one
# ---------------------------------------------------------------------------

def new_network_id() -> str:
    return "net_" + secrets.token_hex(12)


def create_network(name: Optional[str] = None, *, device_name: Optional[str] = None,
                   legacy_instance_id: Optional[str] = None) -> NetworkContext:
    """Make this worker the founder of a fresh network."""
    device_identity = identity_mod.load()
    if store.get_state() is not None:
        raise RuntimeError("This worker already belongs to a network")
    network_key = crypto.generate_network_key()
    network_id = new_network_id()
    device_name = device_name or _default_device_name()
    legacy_map = {legacy_instance_id: device_identity.device_id} if legacy_instance_id else {}
    with get_db() as conn:
        cursor = conn.cursor()
        store.create_state(network_id=network_id, name=name, founder_device_id=device_identity.device_id,
                           keyring={"1": crypto.wrap_key(device_identity.encryption_public, network_key)},
                           legacy_instance_map=legacy_map, _conn=conn)
        certificate = membership.issue_certificate(
            network_id=network_id, issuer_device_id=device_identity.device_id,
            issuer_signing_private=device_identity.signing_private, device_id=device_identity.device_id,
            signing_public=device_identity.signing_public, encryption_public=device_identity.encryption_public,
            name=device_name, kind=membership.KIND_WORKER, generation=0, legacy_instance_id=legacy_instance_id)
        store.upsert_member(device_id=device_identity.device_id, name=device_name, kind=membership.KIND_WORKER,
                            platform=identity_mod.platform_name(),
                            signing_public=crypto.b64u_encode(device_identity.signing_public),
                            encryption_public=crypto.b64u_encode(device_identity.encryption_public),
                            generation=0, added_by=device_identity.device_id,
                            legacy_instance_id=legacy_instance_id, _conn=conn)
        ctx = load_context(conn)
        record_membership(cursor, certificate, ctx=ctx)
    logger.info("Created network %s as device %s", network_id, device_identity.device_id)
    return context(refresh=True)


def leave_network() -> dict:
    """Forget this worker's network so it can join another one.

    Domain rows stay where they are. The sync bookkeeping of the old network
    is dropped: its operations cannot verify under another network's members,
    and its record hashes would otherwise stop the scanner from publishing
    local rows the new network lacks. After the next enrollment, records
    applied from the joined network overwrite same-id local rows (the joined
    network wins) and rows only this worker holds are published to it.
    """
    global _context
    ctx = context()
    if ctx is None:
        raise RuntimeError("This worker does not belong to a network")
    if ctx.bootstrap_state != BOOTSTRAP_READY:
        raise RuntimeError("Wait for the current settings sync to finish before switching networks")
    left = {"network_id": ctx.network_id, "name": ctx.name, "member_count": len(ctx.active_members())}
    with get_db() as conn:
        cursor = conn.cursor()
        store.lock_state(cursor)  # Same lock order as in-flight hub receipt transactions.
        for table in ("sync_pending", "sync_conflicts", "sync_watermarks", "sync_records", "sync_ops",
                      "sync_peers", "network_presence", "network_catalog_cursors", "network_membership_records",
                      "network_joins", "network_member_names", "network_members"):
            cursor.execute(f"DELETE FROM {table}")
        cursor.execute("DELETE FROM sync_blobs WHERE present = FALSE")
        store.delete_state(_conn=conn)
    with _lock:
        _context = None
    try:
        from app.network import transport
        transport.forget_peers()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.network import hub
        hub.forget_network()
    except Exception:  # noqa: BLE001
        pass
    logger.info("Left network %s", left["network_id"])
    return left


def _default_device_name() -> str:
    from app.core.cluster import default_device_name
    return default_device_name()


def _my_instance_id() -> str:
    from app.core.config import settings
    return settings.effective_instance_id


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _parse_json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return crypto.b64u_encode(bytes(value))
    return value


def serialize_row(coll: policy.Collection, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Plaintext record for a row, or None when the row is not shared."""
    if coll.shared_row is not None and not coll.shared_row(row):
        return None
    record: Dict[str, Any] = {}
    encrypted_columns: List[str] = []
    for name in coll.fields:
        if name in coll.local_fields:
            continue
        value = row.get(name)
        if name in coll.json_fields:
            value = _parse_json(value)
        elif name in coll.encrypted and isinstance(value, str) and encryption.is_encrypted(value):
            if not encryption.is_encryption_ready():
                raise TransientApplyError("Credential storage is locked; cannot share " + coll.name)
            value = encryption.decrypt_value(value)
            encrypted_columns.append(name)
        record[name] = _normalize_value(value)
    if encrypted_columns:
        record["_enc"] = sorted(encrypted_columns)
    if coll.serialize_hook is not None:
        record = coll.serialize_hook(record)
    return record


def content_hash(coll: policy.Collection, record: Mapping[str, Any]) -> str:
    subset = {name: record.get(name) for name in coll.hash_fields if name not in coll.local_fields}
    subset["_enc"] = record.get("_enc") or []
    return crypto.sha256_hex(envelope_mod.record_bytes(subset))


def _column_value(coll: policy.Collection, name: str, value: Any, encrypted_columns: Sequence[str]) -> Any:
    if name in coll.json_fields:
        # Wire values are already decoded JSON, including scalar strings/null.
        # Passing a string through produces invalid JSONB (or unparseable text).
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if name in encrypted_columns and isinstance(value, str) and value:
        if not encryption.is_encryption_ready():
            raise TransientApplyError("Credential storage is locked; cannot store " + coll.name)
        return encryption.encrypt_value(value)
    return value


def _local_keys(coll: policy.Collection, record_id: str) -> Tuple[Any, ...]:
    """Key column values for a record id, after the identity hook (``@owner`` -> local)."""
    values = dict(zip(coll.keys, coll.key_values(record_id)))
    if coll.materialize_hook is not None:
        values = coll.materialize_hook(values)
    return tuple(values[k] for k in coll.keys)


def materialize(cursor, coll: policy.Collection, record_id: str, record: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Write (or delete) the domain row. Returns the row as re-read, or None."""
    keys = _local_keys(coll, record_id)
    where = " AND ".join(f"{k} = %s" for k in coll.keys)
    if record is None:
        cursor.execute(f"DELETE FROM {coll.table} WHERE {where}", keys)
        return None
    record = dict(record)
    if coll.materialize_hook is not None:
        record = coll.materialize_hook(record)
    encrypted_columns = list(record.get("_enc") or [])
    columns: List[str] = []
    values: List[Any] = []
    for name in coll.fields:
        if name in coll.local_fields or name not in record:
            continue
        columns.append(name)
        values.append(_column_value(coll, name, record.get(name), encrypted_columns))
    # Key columns always come from the record id so a hook cannot move a row.
    for name, value in zip(coll.keys, keys):
        if name in columns:
            values[columns.index(name)] = value
        else:
            columns.append(name)
            values.append(value)
    if coll.table in _REVISION_TABLES:
        columns.append("revision")
        values.append(str(uuid4()))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in coll.keys)
    # Local NOT NULL columns get a placeholder on first insert only; an
    # existing row keeps whatever this worker put there.
    insert_columns, insert_values = list(columns), list(values)
    for name, default in coll.mirror_defaults.items():
        if name not in insert_columns:
            insert_columns.append(name)
            insert_values.append(default)
    conflict = ", ".join(coll.keys)
    sql = (f"INSERT INTO {coll.table} ({', '.join(insert_columns)}) VALUES ({', '.join(['%s'] * len(insert_columns))}) "
           f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}")
    cursor.execute(sql, tuple(insert_values))
    cursor.execute(f"SELECT * FROM {coll.table} WHERE {where}", keys)
    row = cursor.fetchone()
    return dict(row) if row else None


def _read_row(cursor, coll: policy.Collection, record_id: str) -> Optional[dict]:
    keys = _local_keys(coll, record_id)
    where = " AND ".join(f"{k} = %s" for k in coll.keys)
    cursor.execute(f"SELECT * FROM {coll.table} WHERE {where}", keys)
    row = cursor.fetchone()
    return dict(row) if row else None


def _lane(coll: policy.Collection) -> str:
    return store.LANE_HISTORY if coll.name in _HISTORY_COLLECTIONS else store.LANE_SETTINGS


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def record(cursor, collection: str, record_id: str, row: Optional[Mapping[str, Any]], *,
           ctx: Optional[NetworkContext] = None, force: bool = False,
           order_seq: Optional[int] = None) -> Optional[dict]:
    """Publish a row (or its deletion) if the network does not know it yet.

    Returns the envelope dict that was appended, or None when nothing
    changed or the worker is still bootstrapping.
    """
    ctx = ctx or context()
    if ctx is None or ctx.bootstrap_state != BOOTSTRAP_READY or ctx.key_version not in ctx.keyring:
        return None
    if _outbox_held and collection != "membership":
        return None  # the scanner publishes the row once the transport has compared streams
    if collection != "membership":
        from app.network.keyring import safe
        if not safe(ctx, crypto.sha256_hex(ctx.network_key)):
            return None
    coll = policy.collection(collection)
    plaintext = serialize_row(coll, row) if row is not None else None
    if row is not None and plaintext is None:
        return None  # not a shared row
    existing = store.get_record(cursor, collection, record_id)
    if plaintext is None:
        if existing is None or bool(existing["deleted"]):
            return None
        new_hash = ""
    else:
        new_hash = content_hash(coll, plaintext)
        if (not force and existing is not None and not bool(existing["deleted"])
                and existing["content_hash"] == new_hash):
            return None
    # Sequence numbers are allocated under the state row lock: two writers in
    # separate transactions must never sign the same (device, incarnation,
    # seq). ``bump_clock`` takes that lock implicitly; keep it explicit.
    store.lock_state(cursor)
    clock = store.bump_clock(cursor)
    seq = store.next_origin_seq(cursor, ctx.device_id, ctx.identity.incarnation)
    env = envelope_mod.build(
        network_id=ctx.network_id, collection=collection, record_id=record_id,
        op=OP_DELETE if plaintext is None else OP_UPSERT, clock=clock,
        origin_device_id=ctx.device_id, origin_incarnation=ctx.identity.incarnation, origin_seq=seq,
        schema_version=coll.schema_version, key_version=ctx.key_version, network_key=ctx.network_key,
        signing_private=ctx.identity.signing_private, record=plaintext, order_seq=order_seq)
    data = env.to_dict()
    store.insert_op(cursor, data, _lane(coll))
    store.put_record(cursor, collection, record_id, clock=clock, origin_device_id=ctx.device_id,
                     content_hash=new_hash, deleted=plaintext is None, order_seq=order_seq)
    return data


def record_membership(cursor, membership_record: Mapping[str, Any], *, ctx: Optional[NetworkContext] = None) -> Optional[dict]:
    """Store a membership record locally and publish it."""
    kind = membership_record.get("type")
    if kind in (membership.TYPE_CERTIFICATE, membership.TYPE_RECOVERY):
        record_id = f"cert:{membership_record['device_id']}:{membership_record['generation']}"
    elif kind == membership.TYPE_REVOCATION:
        record_id = f"rev:{membership_record['device_id']}:{membership_record['generation']}"
    elif kind == membership.TYPE_KEY:
        from app.network.keyring import record_id as key_record_id
        record_id = key_record_id(membership_record)
    else:
        raise ValueError("Unknown membership record")
    store.put_membership_record(record_id, dict(membership_record), _conn=_CursorConn(cursor))
    row = {"id": record_id, "record": dict(membership_record), "created_at": envelope_mod.now_iso()}
    return record(cursor, "membership", record_id, row, ctx=ctx, force=True)


class _CursorConn:
    """Adapter so store helpers taking ``_conn`` can reuse an open cursor."""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def record_after_commit(collection: str, record_id: str) -> None:
    """Convenience for domain code: publish one row in its own transaction, then kick."""
    ctx = context()
    if ctx is None:
        return
    coll = policy.collection(collection)
    changed = False
    with get_db() as conn:
        cursor = conn.cursor()
        row = _read_row(cursor, coll, record_id)
        changed = record(cursor, collection, record_id, row, ctx=ctx) is not None
    if changed:
        kick()


# ---------------------------------------------------------------------------
# Scanning (change detection without per-function hooks)
# ---------------------------------------------------------------------------

def scan_collection(collection: str, *, ctx: Optional[NetworkContext] = None, owner_only: bool = False) -> int:
    """Publish every row that differs from the network's view; return the count."""
    ctx = ctx or context()
    if ctx is None or ctx.bootstrap_state != BOOTSTRAP_READY:
        return 0
    coll = policy.collection(collection)
    published = 0
    # Phase one reads without publishing, so the state row lock that
    # ``record`` takes is held only while the changed rows are written out
    # (in bounded batches), not for the whole table walk.
    changed: List[Tuple[str, Optional[dict]]] = []
    with get_db() as conn:
        cursor = conn.cursor()
        known = store.records_for_collection(cursor, collection)
        cursor.execute(f"SELECT * FROM {coll.table}")
        rows = [dict(r) for r in cursor.fetchall()]
    seen = set()
    skipped = 0
    for row in rows:
        if owner_only:
            owner = row.get("owner_device_id")
            if owner not in (None, ctx.device_id):
                continue
            # NULL owner means "this worker" only for rows this install
            # wrote; a shared database's rows from other installs wait for
            # their device to join (legacy map) before anyone publishes them.
            if owner is None and (row.get("instance_id") or "") not in ("", _my_instance_id()):
                continue
        try:
            plaintext = serialize_row(coll, row)
        except ApplyError:
            # One unreadable row (locked credential storage, a bad value) must
            # not stop the rest of the collection.
            skipped += 1
            continue
        if plaintext is None:
            continue
        record_id = coll.record_id(plaintext)
        seen.add(record_id)
        existing = known.get(record_id)
        if existing is not None and not bool(existing["deleted"]) and existing["content_hash"] == content_hash(coll, plaintext):
            continue
        changed.append((record_id, row))
    if skipped:
        # A row we could not read has a record id we could not compute, so a
        # missing id proves nothing this pass: publish no deletions.
        logger.debug("Scan of %s skipped %d unreadable rows; deletions deferred", collection, skipped)
    else:
        for record_id, existing in known.items():
            if record_id in seen or bool(existing["deleted"]):
                continue
            if owner_only and existing.get("origin_device_id") != ctx.device_id:
                continue
            changed.append((record_id, None))
    for start in range(0, len(changed), SCAN_PUBLISH_BATCH):
        with get_db() as conn:
            cursor = conn.cursor()
            for record_id, row in changed[start:start + SCAN_PUBLISH_BATCH]:
                try:
                    if record(cursor, collection, record_id, row, ctx=ctx) is not None:
                        published += 1
                except TransientApplyError:
                    continue
    return published


SCAN_PUBLISH_BATCH = 200


def scan_all(*, ctx: Optional[NetworkContext] = None, include_catalog: bool = True,
             strict: bool = False, collections: Optional[Iterable[str]] = None) -> int:
    ctx = ctx or context()
    if ctx is None:
        return 0
    ctx = _ensure_safe_write_key(ctx)
    total = 0
    selected = set(collections) if collections is not None else None
    for name in policy.SCANNED_COLLECTIONS:
        if selected is not None and name not in selected:
            continue
        try:
            total += scan_collection(name, ctx=ctx)
        except Exception:  # noqa: BLE001
            logger.warning("Scan of %s failed", name, exc_info=True)
            if strict:
                raise
    if include_catalog:
        try:
            from app.network import catalog
            total += catalog.publish_owned(ctx=ctx)
        except Exception:  # noqa: BLE001
            logger.warning("Catalog publish failed", exc_info=True)
            if strict:
                raise
        for module_name in ("memory_records", "plugins_sync"):
            try:
                import importlib
                module = importlib.import_module("app.network." + module_name)
                total += module.publish(ctx=ctx)
            except Exception:  # noqa: BLE001
                logger.warning("%s publish failed", module_name, exc_info=True)
                if strict:
                    raise
    if total:
        kick()
    return total


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    applied: int = 0
    duplicate: int = 0
    pending: int = 0
    rejected: int = 0  # permanent: the op can never apply and the stream moved past it
    retry: int = 0     # transient: kept in place, the peer resends it
    lost: int = 0
    own_ahead: int = 0  # highest seq of our own current stream seen that we do not hold
    reasons: List[str] = field(default_factory=list)
    touched: List[Tuple[str, str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"applied": self.applied, "duplicate": self.duplicate, "pending": self.pending,
                "rejected": self.rejected, "retry": self.retry, "lost": self.lost, "reasons": self.reasons[:20]}


def _origin_allowed(cursor, ctx: NetworkContext, env: Envelope) -> Optional[Member]:
    """The member an envelope claims to come from, or None for an unknown origin.

    A revoked device is honoured only up to the stream position recorded in
    its revocation: nothing from a newer incarnation (a removed device could
    otherwise open a fresh stream with a fresh sequence budget) and nothing
    past ``last_seq_accepted`` in the incarnation the revoker knew.
    """
    member = ctx.member(env.origin_device_id)
    if member is None:
        return None
    if member.status == membership.STATUS_REVOKED:
        limit = member.last_seq_accepted
        limit_incarnation = member.last_incarnation_accepted
        if limit_incarnation is None:
            # Revocation from before incarnations were recorded: the streams
            # this worker already knew are the only ones it will ever honour.
            limit_incarnation = store.highest_incarnation(cursor, env.origin_device_id)
        if limit is None or env.origin_incarnation > int(limit_incarnation):
            raise ApplyError("origin revoked")
        if env.origin_incarnation == int(limit_incarnation) and env.origin_seq > int(limit):
            raise ApplyError("origin revoked")
    return member


def _verify_recovery_origin(ctx: NetworkContext, env: Envelope) -> Member:
    """An unknown origin may only introduce itself with a self-certifying record.

    Two cases: a recovery certificate (proof under the network key), and the
    founder's own self-signed certificate when a replica is replayed from
    the beginning (hub restore) and the founder is not yet in the members.
    """
    if env.collection != "membership" or env.key_version not in ctx.keyring:
        raise TransientApplyError("unknown origin")
    try:
        from app.network.keyring import open_envelope
        row, decryption_key = open_envelope(ctx, env)
        record_data = _parse_json((row or {}).get("record"))
        member = membership.verify_certificate(record_data, network_id=ctx.network_id, members=ctx.members,
                                               founder_device_id=ctx.founder_device_id,
                                               network_key=decryption_key)
    except (crypto.CryptoError, envelope_mod.EnvelopeError, membership.MembershipError, AttributeError, TypeError) as exc:
        raise TransientApplyError("unknown origin") from exc
    if member.device_id != env.origin_device_id:
        raise TransientApplyError("unknown origin")
    is_recovery = record_data.get("type") == membership.TYPE_RECOVERY
    is_founder = (record_data.get("type") == membership.TYPE_CERTIFICATE
                  and record_data.get("issued_by") == member.device_id == ctx.founder_device_id)
    if not (is_recovery or is_founder):
        raise TransientApplyError("unknown origin")
    return member


def apply_envelope(cursor, data: Mapping[str, Any], *, ctx: Optional[NetworkContext] = None,
                   result: Optional[ApplyResult] = None, _draining: bool = False) -> str:
    """Apply one envelope.

    Returns ``applied`` | ``duplicate`` | ``pending`` | ``lost`` | ``rejected``.
    ``rejected`` means the op was refused for good and, when it was the next
    in its stream, the watermark moved past it. Raises ``TransientApplyError``
    when the op must be retried later and ``ApplyError`` for a refusal that
    happened before the op's place in its stream was known (bad signature,
    wrong network, malformed).
    """
    ctx = ctx or require_context()
    result = result if result is not None else ApplyResult()
    try:
        env = Envelope.from_dict(data)
    except envelope_mod.EnvelopeError as exc:
        raise ApplyError("malformed: " + str(exc)) from exc
    if env.network_id != ctx.network_id:
        raise ApplyError("wrong network")
    if env.origin_device_id == ctx.device_id:
        if not env.verify_signature(ctx.identity.signing_public):
            raise ApplyError("bad signature")
        if env.origin_incarnation >= ctx.identity.incarnation:
            if env.origin_incarnation == ctx.identity.incarnation and \
                    env.origin_seq > store.own_watermark(cursor, ctx.device_id, env.origin_incarnation):
                # Signed by this device, yet not in its outbox: our history
                # went backwards (restore from backup). ``apply_batch`` moves
                # to a fresh incarnation once the batch is stored.
                result.own_ahead = max(result.own_ahead, env.origin_seq)
            return "duplicate"  # our own op coming back
        # An op this device signed in an earlier incarnation: our own history
        # that a backup restore lost. Re-import it like any other stream.
        member = ctx.member(ctx.device_id)
        if member is None:
            return "duplicate"
    else:
        refused: Optional[str] = None
        try:
            member = _origin_allowed(cursor, ctx, env)
        except ApplyError as exc:
            member, refused = ctx.member(env.origin_device_id), str(exc)
        if member is None:
            member = _verify_recovery_origin(ctx, env)
        if not env.verify_signature(member.signing_public):
            raise ApplyError("bad signature")
        if refused is not None:
            # The stream moved past what this device may still say. When the
            # op is the next in its stream, advance past it so the peer stops
            # resending it; otherwise simply drop it.
            highest = store.highest_incarnation(cursor, env.origin_device_id)
            if env.origin_incarnation >= highest:
                watermark = store.get_watermark(cursor, env.origin_device_id, env.origin_incarnation)
                if env.origin_seq == watermark + 1:
                    store.set_watermark(cursor, env.origin_device_id, env.origin_incarnation, env.origin_seq)
            result.reasons.append(refused)
            return "rejected"
    if env.key_version not in ctx.keyring:
        raise TransientApplyError("unknown key version %d" % env.key_version)

    # Stream ordering per (origin, incarnation).
    highest = store.highest_incarnation(cursor, env.origin_device_id)
    if env.origin_incarnation < highest:
        raise ApplyError("stale incarnation")
    watermark = store.get_watermark(cursor, env.origin_device_id, env.origin_incarnation)
    if env.origin_seq <= watermark:
        return "duplicate"
    if env.origin_seq > watermark + 1:
        if env.origin_seq > watermark + PENDING_WINDOW:
            raise ApplyError("sequence too far ahead of the stream")
        if store.count_pending(cursor, env.origin_device_id, env.origin_incarnation) >= PENDING_CAP_PER_STREAM:
            raise ApplyError("too many buffered operations for this stream")
        store.stash_pending(cursor, env.to_dict())
        return "pending"

    outcome = _apply_next(cursor, ctx, env, member, result)
    store.set_watermark(cursor, env.origin_device_id, env.origin_incarnation, env.origin_seq)
    store.drop_pending_upto(cursor, env.origin_device_id, env.origin_incarnation, env.origin_seq)
    if env.origin_incarnation > highest and highest:
        store.clear_pending_stream(cursor, env.origin_device_id, highest)
    # Drain buffered successors.
    if not _draining:
        next_seq = env.origin_seq + 1
        while True:
            buffered = store.pop_pending(cursor, env.origin_device_id, env.origin_incarnation, next_seq)
            if buffered is None:
                break
            try:
                inner = apply_envelope(cursor, buffered, ctx=ctx, result=result, _draining=True)
            except TransientApplyError as exc:
                # Not applicable yet (a key or certificate from another stream
                # is still on its way). Keep it buffered; the stream resumes
                # here once it can be opened.
                store.stash_pending(cursor, buffered)
                result.retry += 1
                result.reasons.append(str(exc))
                break
            except ApplyError as exc:
                result.rejected += 1
                result.reasons.append(str(exc))
                store.set_watermark(cursor, env.origin_device_id, env.origin_incarnation, next_seq)
                next_seq += 1
                continue
            if inner == "applied":
                result.applied += 1
            elif inner == "lost":
                result.lost += 1
            elif inner == "rejected":
                result.rejected += 1
            store.set_watermark(cursor, env.origin_device_id, env.origin_incarnation, next_seq)
            next_seq += 1
    return outcome


def _apply_next(cursor, ctx: NetworkContext, env: Envelope, member: Member, result: ApplyResult) -> str:
    """Apply the next op of a stream; classify what cannot be applied.

    Transient failures propagate (the watermark stays, the peer resends).
    Permanent ones (a membership record that does not verify, a row the
    database refuses) become ``rejected``: the loser is kept in the conflicts
    list so nothing disappears silently, and the caller moves the watermark.
    """
    _savepoint(cursor, "SAVEPOINT apply_op")
    try:
        outcome = _apply_in_order(cursor, ctx, env, member, result)
    except TransientApplyError:
        _savepoint(cursor, "ROLLBACK TO SAVEPOINT apply_op")
        raise
    except ApplyError as exc:
        _savepoint(cursor, "ROLLBACK TO SAVEPOINT apply_op")
        result.reasons.append(str(exc))
        return "rejected"
    except _permanent_db_errors() as exc:
        _savepoint(cursor, "ROLLBACK TO SAVEPOINT apply_op")
        import sqlite3
        from psycopg2.errors import ForeignKeyViolation

        if (isinstance(exc, ForeignKeyViolation) or getattr(exc, "pgcode", None) == "23503"
                or getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY):
            # Ordering is per origin, not global. Consuming this op now would
            # permanently lose a valid child whose parent is still in flight.
            raise TransientApplyError("Referenced record has not arrived yet") from exc
        reason = "storage refused the record: " + type(exc).__name__
        logger.warning("Operation %s/%s from %s could not be stored (%s); kept as a conflict",
                       env.collection, env.record_id, env.origin_device_id, type(exc).__name__)
        try:
            from app.network.keyring import open_envelope
            plaintext, _key = open_envelope(ctx, env)
        except (crypto.CryptoError, envelope_mod.EnvelopeError):
            plaintext = None
        existing = store.get_record(cursor, env.collection, env.record_id)
        store.add_conflict(cursor, collection=env.collection, record_id=env.record_id, losing_clock=env.clock,
                           losing_device_id=env.origin_device_id,
                           losing_record=plaintext if env.op == OP_UPSERT else {"_deleted": True},
                           winning_clock=int(existing["clock"]) if existing else env.clock,
                           winning_device_id=existing["origin_device_id"] if existing else ctx.device_id)
        result.reasons.append(reason)
        return "rejected"
    _savepoint(cursor, "RELEASE SAVEPOINT apply_op")
    return outcome


def _apply_in_order(cursor, ctx: NetworkContext, env: Envelope, member: Member, result: ApplyResult) -> str:
    coll = policy.collection(env.collection) if env.collection in policy.COLLECTIONS else None
    if coll is None:
        # Unknown collection (newer peer): keep the op for forwarding, skip materialization.
        store.insert_op(cursor, env.to_dict(), store.LANE_SETTINGS)
        store.observe_clock(cursor, env.clock)
        return "applied"
    store.observe_clock(cursor, env.clock)
    try:
        from app.network.keyring import open_envelope
        plaintext, _decryption_key = open_envelope(ctx, env)
    except (crypto.CryptoError, envelope_mod.EnvelopeError) as exc:
        raise ApplyError("cannot open payload") from exc

    existing = store.get_record(cursor, env.collection, env.record_id)
    if coll.mode == policy.MODE_OWNER_ORDERED:
        # Owner-ordered: the owner's stream order is the order. No clock race.
        wins = existing is None or env.origin_device_id == existing["origin_device_id"] or \
            envelope_mod.compare(env.clock, env.origin_device_id, int(existing["clock"]), existing["origin_device_id"]) > 0
    else:
        wins = existing is None or envelope_mod.compare(
            env.clock, env.origin_device_id, int(existing["clock"]), existing["origin_device_id"]) > 0
    if not wins:
        # The network already moved past this write; keep it as a conflict
        # record, unless it says the same thing as what we already hold.
        incoming_hash = content_hash(coll, plaintext) if env.op == OP_UPSERT else ""
        if incoming_hash == (existing["content_hash"] or "") and bool(existing["deleted"]) == (env.op == OP_DELETE):
            store.insert_op(cursor, env.to_dict(), _lane(coll))
            return "lost"
        losing_record = plaintext if env.op == OP_UPSERT else {"_deleted": True}
        store.add_conflict(cursor, collection=env.collection, record_id=env.record_id, losing_clock=env.clock,
                           losing_device_id=env.origin_device_id, losing_record=losing_record,
                           winning_clock=int(existing["clock"]), winning_device_id=existing["origin_device_id"])
        store.insert_op(cursor, env.to_dict(), _lane(coll))
        return "lost"

    if env.collection == "membership":
        _apply_membership(cursor, ctx, plaintext)

    # Keep the local loser (a concurrent local edit) for the conflicts list.
    if existing is not None and existing["origin_device_id"] != env.origin_device_id and not bool(existing["deleted"]):
        local_row = _read_row(cursor, coll, env.record_id)
        if local_row is not None:
            try:
                local_plain = serialize_row(coll, local_row)
            except ApplyError:
                local_plain = None
            incoming_hash = content_hash(coll, plaintext) if env.op == OP_UPSERT else ""
            if local_plain is not None and content_hash(coll, local_plain) != incoming_hash:
                if local_plain.get("_enc"):
                    local_plain = {k: ("<secret>" if k in local_plain["_enc"] else v) for k, v in local_plain.items() if k != "_enc"}
                store.add_conflict(cursor, collection=env.collection, record_id=env.record_id,
                                   losing_clock=int(existing["clock"]), losing_device_id=existing["origin_device_id"],
                                   losing_record=local_plain, winning_clock=env.clock,
                                   winning_device_id=env.origin_device_id)

    if env.op == OP_DELETE:
        materialize(cursor, coll, env.record_id, None)
        store.put_record(cursor, env.collection, env.record_id, clock=env.clock, origin_device_id=env.origin_device_id,
                         content_hash="", deleted=True, order_seq=env.order_seq)
    else:
        plaintext = _prepare_mirror(cursor, coll, plaintext)
        row = materialize(cursor, coll, env.record_id, plaintext)
        _after_mirror(cursor, coll, plaintext)
        local_hash = env.content_hash
        if row is not None:
            try:
                reserialized = serialize_row(coll, row)
                if reserialized is not None:
                    local_hash = content_hash(coll, reserialized)
            except ApplyError:
                pass
        store.put_record(cursor, env.collection, env.record_id, clock=env.clock, origin_device_id=env.origin_device_id,
                         content_hash=local_hash, deleted=False, order_seq=env.order_seq)
    # Stored for forwarding only once it applied: a failure above must not
    # leave an op in the log that this worker never materialized.
    store.insert_op(cursor, env.to_dict(), _lane(coll))
    result.touched.append((env.collection, env.record_id, env.op))
    return "applied"


def _prepare_mirror(cursor, coll: policy.Collection, plaintext: Mapping[str, Any]) -> Mapping[str, Any]:
    """Owner-ordered rows may reference local-only rows; drop what we cannot see."""
    if coll.name == "sessions":
        from app.network import catalog
        return catalog.prepare_session_mirror(cursor, dict(plaintext))
    return plaintext


def _after_mirror(cursor, coll: policy.Collection, plaintext: Mapping[str, Any]) -> None:
    if coll.name == "session_events":
        from app.network import catalog
        catalog.after_event_mirrored(cursor, dict(plaintext))
    elif coll.name in ("artifacts", "note_attachments"):
        # Bytes are fetched on demand from a holder; remember what is missing.
        from app.network import blobs
        digest = plaintext.get("hash") if coll.name == "artifacts" else plaintext.get("blob_hash")
        size = plaintext.get("size") if coll.name == "artifacts" else plaintext.get("size_bytes")
        holder = plaintext.get("owner_device_id")
        if isinstance(digest, str) and digest:
            try:
                blobs.note_missing(digest, int(size or 0), holder, _cursor=cursor)
            except Exception:  # noqa: BLE001
                logger.debug("Blob bookkeeping failed for %s", digest[:12], exc_info=True)


def _apply_membership(cursor, ctx: NetworkContext, plaintext: Mapping[str, Any]) -> None:
    record_data = _parse_json(plaintext.get("record")) if plaintext else None
    if not isinstance(record_data, dict):
        raise ApplyError("malformed membership record")
    members = dict(ctx.members)
    try:
        members = membership.apply_record(record_data, network_id=ctx.network_id, members=members,
                                          founder_device_id=ctx.founder_device_id,
                                          network_key=ctx.keyring.get(ctx.key_version))
    except membership.MembershipError as exc:
        raise ApplyError("membership: " + str(exc)) from exc
    kind = record_data.get("type")
    generation = int(record_data.get("generation", ctx.generation))
    conn = _CursorConn(cursor)
    if kind in (membership.TYPE_CERTIFICATE, membership.TYPE_RECOVERY):
        member = members[record_data["device_id"]]
        store.upsert_member(device_id=member.device_id, name=member.name, kind=member.kind,
                            signing_public=crypto.b64u_encode(member.signing_public),
                            encryption_public=crypto.b64u_encode(member.encryption_public),
                            generation=member.generation, status=member.status,
                            added_by=record_data.get("issued_by"),
                            legacy_instance_id=record_data.get("legacy_instance_id"), _conn=conn)
        legacy = record_data.get("legacy_instance_id")
        if legacy:
            ctx.legacy_instance_map[legacy] = member.device_id
            store.update_state(_conn=conn, legacy_instance_map=ctx.legacy_instance_map)
    elif kind == membership.TYPE_REVOCATION:
        member = members[record_data["device_id"]]
        last_incarnation = member.last_incarnation_accepted
        if last_incarnation is None:
            # An older revoker did not record the incarnation: honour only the
            # streams this worker already knew when it learned the revocation.
            last_incarnation = store.highest_incarnation(cursor, member.device_id)
            members[member.device_id] = member = membership.Member(
                device_id=member.device_id, signing_public=member.signing_public,
                encryption_public=member.encryption_public, name=member.name, kind=member.kind,
                generation=member.generation, status=member.status, last_seq_accepted=member.last_seq_accepted,
                last_incarnation_accepted=int(last_incarnation))
        store.upsert_member(device_id=member.device_id, name=member.name, kind=member.kind,
                            signing_public=crypto.b64u_encode(member.signing_public),
                            encryption_public=crypto.b64u_encode(member.encryption_public),
                            generation=member.generation, status=membership.STATUS_REVOKED,
                            last_seq_accepted=member.last_seq_accepted,
                            last_incarnation_accepted=int(last_incarnation), _conn=conn)
        # Nothing buffered from the removed device will ever apply.
        store.clear_pending_origin(cursor, member.device_id)
        # Certificates the removed device issued past the point its revoker
        # honours were applied here but rejected by the revoker: unwind them
        # so every member agrees on who is in the network.
        for op in store.membership_ops_after(cursor, member.device_id, int(last_incarnation),
                                             int(member.last_seq_accepted or 0)):
            parts = str(op.get("record_id") or "").split(":")
            if len(parts) != 3 or parts[0] != "cert":
                continue
            late = members.get(parts[1])
            if late is None or late.status != membership.STATUS_ACTIVE or late.device_id == ctx.device_id:
                continue
            row = store.get_member(late.device_id, _conn=conn)
            if not row or row.get("added_by") != member.device_id:
                continue
            members[late.device_id] = membership.Member(
                device_id=late.device_id, signing_public=late.signing_public,
                encryption_public=late.encryption_public, name=late.name, kind=late.kind,
                generation=generation, status=membership.STATUS_REVOKED, last_seq_accepted=0,
                last_incarnation_accepted=0)
            store.upsert_member(device_id=late.device_id, name=late.name, kind=late.kind,
                                signing_public=crypto.b64u_encode(late.signing_public),
                                encryption_public=crypto.b64u_encode(late.encryption_public),
                                generation=generation, status=membership.STATUS_REVOKED,
                                last_seq_accepted=0, last_incarnation_accepted=0, _conn=conn)
            store.clear_pending_origin(cursor, late.device_id)
            logger.warning("Device %s was admitted by %s after its revocation point; membership withdrawn",
                           late.device_id, member.device_id)
        if member.device_id == ctx.device_id:
            logger.error("This device was removed from network %s", ctx.network_id)
        # A recovery certificate under a predecessor key is no longer an
        # authority after any membership revocation.  Keep this in the same
        # transaction as the learned revocation.
        from app.network import recovery
        recovery.refresh_wrap_after_rotation(_conn=conn)
    elif kind == membership.TYPE_KEY:
        version = int(record_data["key_version"])
        try:
            key = membership.unwrap_from_key_record(record_data, ctx.device_id, ctx.identity.encryption_private)
        except crypto.CryptoError as exc:
            raise ApplyError("key record does not open") from exc
        if key is not None:
            from app.network import keyring as keyring_store
            ctx.members = members
            keyring_store.install(ctx, record_data, key, conn)
    if generation > ctx.generation:
        ctx.generation = generation
        store.update_state(_conn=conn, generation=generation)
    ctx.members = members
    # The membership record itself is materialized by the generic path.


def _ensure_safe_write_key(ctx: NetworkContext) -> NetworkContext:
    """Reconcile concurrent revocations without choosing a key known to an exile."""
    from app.network import keyring as keyring_store
    me = ctx.member(ctx.device_id)
    if (ctx.bootstrap_state != BOOTSTRAP_READY or me is None
            or me.status != membership.STATUS_ACTIVE or ctx.key_version not in ctx.keyring):
        return ctx
    if keyring_store.safe(ctx, crypto.sha256_hex(ctx.network_key)):
        return ctx
    with get_db() as conn:
        cursor = conn.cursor()
        store.lock_state(cursor)
        current = load_context(conn)
        if keyring_store.select(current):
            keyring_store.persist_choice(current, dict(store.get_state(conn)["keyring"]), conn)
        else:
            version = max(current.key_candidates, default=current.key_version) + 1
            key = crypto.generate_network_key()
            record_data = membership.issue_key_record(
                network_id=current.network_id, issuer_device_id=current.device_id,
                issuer_signing_private=current.identity.signing_private,
                key_version=version, generation=current.generation, new_key=key,
                recipients={m.device_id: m.encryption_public for m in current.active_members()},
            )
            # Only the wraps travel under a predecessor key. Subsequent user
            # operations use the fresh key, which excludes ALL known revocations.
            record_membership(cursor, record_data, ctx=current)
            keyring_store.install(current, record_data, key, conn)
            from app.network import recovery
            recovery.refresh_wrap_after_rotation(_conn=conn)
    kick()
    return current


def apply_batch(envelopes: Iterable[Mapping[str, Any]], *, ctx: NetworkContext | None = None,
                hub_scope: str | None = None) -> ApplyResult:
    """Apply a batch in one transaction per envelope so one bad op cannot poison the rest."""
    ctx = ctx or require_context()
    result = ApplyResult()
    for data in envelopes:
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                if hub_scope is not None:
                    state = store.lock_state(cursor)
                    if ((state or {}).get("hub") or {}).get("binding_scope") != hub_scope:
                        raise RuntimeError("Account network changed during feed application")
                outcome = apply_envelope(cursor, data, ctx=ctx, result=result)
                if hub_scope is not None:
                    store.record_hub_receipt(cursor, hub_scope, data)
        except TransientApplyError as exc:
            result.retry += 1
            result.reasons.append(str(exc))
            continue
        except ApplyError as exc:
            result.rejected += 1
            result.reasons.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; log once per op
            if hub_scope is not None:
                # A storage failure is not an invalid remote operation. Do not
                # let the hub reader advance its cursor past an uncommitted op.
                load_context()
                raise
            logger.warning("Applying an operation failed: %s", exc, exc_info=True)
            result.retry += 1
            result.reasons.append("apply failed")
            continue
        if outcome == "applied":
            result.applied += 1
        elif outcome == "duplicate":
            result.duplicate += 1
        elif outcome == "pending":
            result.pending += 1
        elif outcome == "lost":
            result.lost += 1
        elif outcome == "rejected":
            result.rejected += 1
    if result.own_ahead:
        restored_from_backup(result.own_ahead, ctx.identity.incarnation)
    if result.touched:
        touched = list(result.touched)
        if any(c in ("membership", "member_names") for c, _, _ in touched):
            ctx = load_context()
            _ensure_safe_write_key(ctx)
        _notify(touched)
    return result


def restored_from_backup(evidence_seq: int, evidence_incarnation: int) -> NetworkContext:
    """Start a fresh stream: an op this device signed exists that it does not hold.

    Called with proof only (an envelope that verifies under our own signing
    key, at a sequence past our outbox), never on a peer's bare claim.
    """
    with _rollback_lock:
        ctx = require_context()
        # Startup and background peer links can deliver the same proof. It
        # belongs to its signed stream, not a newer incarnation's outbox.
        if ctx.identity.incarnation != evidence_incarnation:
            return ctx
        with get_db() as conn:
            cursor = conn.cursor()
            held = store.own_watermark(cursor, ctx.device_id, evidence_incarnation)
        if evidence_seq <= held:
            return ctx
        logger.error("An operation this device signed (seq %d) is not in its outbox (last %d); this worker was "
                     "restored from a backup and moves to a new incarnation", evidence_seq, held)
        identity_mod.bump_incarnation()
        ctx = load_context()
    kick()
    return ctx


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

SNAPSHOT_CHUNK = 500


def snapshot_manifest(*, ctx: Optional[NetworkContext] = None) -> dict:
    ctx = ctx or require_context()
    # Everything local must be in sync_records before a joiner copies it.
    scan_all(ctx=ctx)
    return {
        "network_id": ctx.network_id,
        "generation": ctx.generation,
        "key_version": ctx.key_version,
        "collections": [c.name for c in policy.ordered_collections()],
        "counts": store.count_records(),
        "watermarks": store.local_watermarks(),
        "device_id": ctx.device_id,
    }


def snapshot_chunk(collection: str, offset: int = 0, *, ctx: Optional[NetworkContext] = None,
                   limit: int = SNAPSHOT_CHUNK, _conn=None) -> dict:
    """One signed, encrypted page of a collection's current records."""
    ctx = ctx or require_context()
    coll = policy.collection(collection)
    with nullcontext(_conn) if _conn is not None else get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_records WHERE collection = %s ORDER BY record_id LIMIT %s OFFSET %s",
                       (collection, limit + 1, offset))
        versions = [dict(r) for r in cursor.fetchall()]
        more = len(versions) > limit
        versions = versions[:limit]
        items = []
        for version in versions:
            item = {"record_id": version["record_id"], "clock": int(version["clock"]),
                    "origin_device_id": version["origin_device_id"], "deleted": bool(version["deleted"]),
                    "order_seq": version.get("order_seq"), "record": None}
            if not item["deleted"]:
                row = _read_row(cursor, coll, version["record_id"])
                item["record"] = serialize_row(coll, row) if row is not None else None
                if item["record"] is None:
                    item["deleted"] = True
            items.append(item)
    body = {"collection": collection, "offset": offset, "items": items, "more": more,
            "network_id": ctx.network_id, "device_id": ctx.device_id, "generation": ctx.generation}
    plaintext = envelope_mod.record_bytes(body)
    aad = crypto.canonical_json({"collection": collection, "offset": offset, "network_id": ctx.network_id})
    ciphertext = crypto.encrypt_payload(ctx.network_key, plaintext, aad)
    signed = {"collection": collection, "offset": offset, "network_id": ctx.network_id,
              "key_version": ctx.key_version, "device_id": ctx.device_id, "ciphertext": ciphertext,
              "content_hash": crypto.sha256_hex(plaintext), "more": more, "count": len(items)}
    signed["signature"] = crypto.b64u_encode(crypto.sign(ctx.identity.signing_private, crypto.canonical_json(
        {k: v for k, v in signed.items() if k != "signature"})))
    return signed


def open_snapshot_chunk(chunk: Mapping[str, Any], *, ctx: NetworkContext) -> dict:
    try:
        sender = ctx.member(chunk["device_id"])
        if sender is None or sender.status != membership.STATUS_ACTIVE:
            raise ApplyError("snapshot sender is not an active member")
        unsigned = {k: v for k, v in chunk.items() if k != "signature"}
        if not crypto.verify(sender.signing_public, crypto.canonical_json(unsigned), crypto.b64u_decode(chunk["signature"])):
            raise ApplyError("snapshot signature is invalid")
        if chunk["network_id"] != ctx.network_id:
            raise ApplyError("snapshot belongs to another network")
        from app.network.keyring import open_payload
        aad = crypto.canonical_json({"collection": chunk["collection"], "offset": chunk["offset"], "network_id": ctx.network_id})
        plaintext = open_payload(ctx, int(chunk["key_version"]), chunk["ciphertext"], aad)
        if crypto.sha256_hex(plaintext) != chunk["content_hash"]:
            raise ApplyError("snapshot hash mismatch")
        return json.loads(plaintext.decode("utf-8"))
    except ApplyError:
        raise
    except (KeyError, TypeError, ValueError, crypto.CryptoError) as exc:
        raise ApplyError("malformed snapshot chunk") from exc


def apply_snapshot_chunk(chunk: Mapping[str, Any], *, ctx: NetworkContext | None = None,
                         _conn=None, validated_membership: bool = False) -> int:
    """Apply a chunk: records newer than what we hold replace ours (LWW by clock)."""
    ctx = ctx or require_context()
    body = open_snapshot_chunk(chunk, ctx=ctx)
    coll = policy.collection(body["collection"])
    applied = 0
    touched: list[tuple[str, str, str]] = []
    with nullcontext(_conn) if _conn is not None else get_db() as conn:
        cursor = conn.cursor()
        for item in body["items"]:
            existing = store.get_record(cursor, coll.name, item["record_id"])
            if existing is not None and existing["origin_device_id"] != item["origin_device_id"] and coll.name != "membership":
                comparison = envelope_mod.compare(int(item["clock"]), item["origin_device_id"],
                                                  int(existing["clock"]), existing["origin_device_id"])
                incoming_hash = "" if item["deleted"] else content_hash(coll, item["record"])
                if incoming_hash != existing["content_hash"]:
                    loser, winner = (existing, item) if comparison > 0 else (item, existing)
                    if loser["deleted"]:
                        losing_record = {"_deleted": True}
                    elif comparison > 0:
                        local_row = _read_row(cursor, coll, item["record_id"])
                        losing_record = serialize_row(coll, local_row) if local_row else {}
                    else:
                        losing_record = dict(item["record"] or {})
                    if losing_record and losing_record.get("_enc"):
                        losing_record = {k: ("<secret>" if k in losing_record["_enc"] else v)
                                         for k, v in losing_record.items() if k != "_enc"}
                    store.add_conflict(cursor, collection=coll.name, record_id=item["record_id"],
                                       losing_clock=int(loser["clock"]), losing_device_id=loser["origin_device_id"],
                                       losing_record=losing_record, winning_clock=int(winner["clock"]),
                                       winning_device_id=winner["origin_device_id"])
            if existing is not None and envelope_mod.compare(
                    int(item["clock"]), item["origin_device_id"], int(existing["clock"]), existing["origin_device_id"]) <= 0:
                continue
            store.observe_clock(cursor, int(item["clock"]))
            if coll.name == "membership" and not item["deleted"] and not validated_membership:
                record_data = _parse_json((item["record"] or {}).get("record"))
                if isinstance(record_data, dict):
                    try:
                        _apply_membership(cursor, ctx, {"record": record_data})
                    except ApplyError as exc:
                        logger.warning("Snapshot membership record skipped: %s", exc)
                        continue
            if item["deleted"]:
                materialize(cursor, coll, item["record_id"], None)
                local_hash = ""
            else:
                prepared = _prepare_mirror(cursor, coll, item["record"])
                row = materialize(cursor, coll, item["record_id"], prepared)
                _after_mirror(cursor, coll, prepared)
                local_hash = content_hash(coll, item["record"])
                if row is not None:
                    try:
                        reserialized = serialize_row(coll, row)
                        if reserialized is not None:
                            local_hash = content_hash(coll, reserialized)
                    except ApplyError:
                        pass
            store.put_record(cursor, coll.name, item["record_id"], clock=int(item["clock"]),
                             origin_device_id=item["origin_device_id"], content_hash=local_hash,
                             deleted=bool(item["deleted"]), order_seq=item.get("order_seq"))
            applied += 1
            touched.append((coll.name, item["record_id"], OP_DELETE if item["deleted"] else OP_UPSERT))
    if touched and _conn is None:
        if coll.name in ("membership", "member_names"):
            load_context()
        _notify(touched)
    return applied


def adopt_watermarks(watermarks: Mapping[str, Mapping[str, int]], *, ctx: NetworkContext | None = None, _conn=None) -> None:
    """After a snapshot, start tailing from the sender's view of every stream."""
    ctx = ctx or require_context()
    with nullcontext(_conn) if _conn is not None else get_db() as conn:
        cursor = conn.cursor()
        for origin, streams in (watermarks or {}).items():
            if origin == ctx.device_id:
                continue
            for incarnation, last_seq in (streams or {}).items():
                current = store.get_watermark(cursor, origin, int(incarnation))
                if int(last_seq) > current:
                    store.set_watermark(cursor, origin, int(incarnation), int(last_seq))
                cursor.execute("DELETE FROM sync_pending WHERE origin_device_id=%s AND incarnation=%s AND seq<=%s",
                               (origin, int(incarnation), int(last_seq)))


def finish_bootstrap(*, ctx: Optional[NetworkContext] = None) -> NetworkContext:
    store.update_state(bootstrap_state=BOOTSTRAP_READY)
    ctx = context(refresh=True)
    # A device admitted through recovery certified itself; nobody else has
    # published that certificate, so it must be this device's first operation
    # (peers learn the new member from it before anything else arrives).
    own_recovery = [r["record"] for r in store.list_membership_records()
                    if r["record"].get("type") == membership.TYPE_RECOVERY
                    and r["record"].get("device_id") == ctx.device_id]
    if own_recovery:
        with get_db() as conn:
            cursor = conn.cursor()
            if store.next_origin_seq(cursor, ctx.device_id, ctx.identity.incarnation) == 1:
                record_membership(cursor, own_recovery[0], ctx=ctx)
    return ctx


# ---------------------------------------------------------------------------
# Membership actions
# ---------------------------------------------------------------------------

def admit_device(*, device_id: str, signing_public: bytes, encryption_public: bytes, name: str,
                 kind: str = membership.KIND_WORKER, platform: Optional[str] = None,
                 legacy_instance_id: Optional[str] = None) -> dict:
    """Issue a certificate for a new device and publish it. Returns the join response."""
    ctx = require_context()
    with get_db() as conn:
        cursor = conn.cursor()
        state = store.lock_state(cursor)
        generation = int(state["generation"]) + 1
        certificate = membership.issue_certificate(
            network_id=ctx.network_id, issuer_device_id=ctx.device_id,
            issuer_signing_private=ctx.identity.signing_private, device_id=device_id,
            signing_public=signing_public, encryption_public=encryption_public, name=name, kind=kind,
            generation=generation, legacy_instance_id=legacy_instance_id)
        store.upsert_member(device_id=device_id, name=name, kind=kind, platform=platform,
                            signing_public=crypto.b64u_encode(signing_public),
                            encryption_public=crypto.b64u_encode(encryption_public), generation=generation,
                            added_by=ctx.device_id, legacy_instance_id=legacy_instance_id, _conn=conn)
        legacy_map = dict(ctx.legacy_instance_map)
        if legacy_instance_id:
            legacy_map[legacy_instance_id] = device_id
        store.update_state(_conn=conn, generation=generation, legacy_instance_map=legacy_map)
        ctx.generation = generation
        ctx.legacy_instance_map = legacy_map
        ctx.members[device_id] = membership.certificate_fields(certificate)
        record_membership(cursor, certificate, ctx=ctx)
    if legacy_instance_id:
        # Rows this database still holds for that install now belong to its device.
        try:
            from app.network.startup import backfill_owner_device_ids
            backfill_owner_device_ids(ctx.device_id, include_unscoped=False)
        except Exception:  # noqa: BLE001
            logger.debug("Legacy owner backfill failed", exc_info=True)
    kick()
    return join_response(device_id=device_id, encryption_public=encryption_public, certificate=certificate)


def join_response(*, device_id: str, encryption_public: bytes, certificate: Mapping[str, Any]) -> dict:
    """Everything a new device needs: keys wrapped to it, the members, the trust roots."""
    ctx = require_context()
    from app.network.keyring import for_recipient
    keyring = for_recipient(ctx, encryption_public)
    return {
        "network_id": ctx.network_id,
        "name": ctx.name,
        "founder_device_id": ctx.founder_device_id,
        "generation": ctx.generation,
        "key_version": ctx.key_version,
        "keyring": keyring,
        "certificate": dict(certificate),
        "membership_records": [r["record"] for r in store.list_membership_records()],
        "members": [_public_member(m) for m in store.list_members()],
        "legacy_instance_map": dict(ctx.legacy_instance_map),
        "issuer": {"device_id": ctx.device_id, "fingerprint": ctx.identity.fingerprint,
                   "endpoints": _own_endpoints()},
    }


def _own_endpoints() -> List[str]:
    endpoints = []
    try:
        from app.core.cluster import advertise_url
        url, _ = advertise_url()
        if url:
            endpoints.append(url)
    except Exception:  # noqa: BLE001
        pass
    # Docker's guessed LAN address can be private to its bridge. Advertise the
    # verified managed route too; peers still authenticate the device identity.
    try:
        from app.core import managed_connection
        connection = managed_connection.status()
        url = connection.get("url")
        if connection.get("status") == "connected" and url and url not in endpoints:
            endpoints.append(url)
    except Exception:  # noqa: BLE001
        pass
    return endpoints


def _public_member(row: Mapping[str, Any]) -> dict:
    return {k: row.get(k) for k in ("device_id", "name", "kind", "platform", "signing_public", "encryption_public",
                                    "generation", "status", "last_seq_accepted", "legacy_instance_id", "endpoints",
                                    "capabilities", "version")}


def enroll_from_response(response: Mapping[str, Any], *, device_name: str,
                         legacy_instance_id: Optional[str] = None) -> NetworkContext:
    """Install the network state a join (or recovery) handed us; start in Syncing."""
    device_identity = identity_mod.load()
    if store.get_state() is not None:
        raise RuntimeError("This worker already belongs to a network")
    keyring_wrapped = dict(response["keyring"])
    keyring = _unwrap_keyring(keyring_wrapped, device_identity)
    if not keyring:
        raise RuntimeError("The network keys do not open with this device's key")
    network_id = response["network_id"]
    founder = response["founder_device_id"]
    with get_db() as conn:
        store.create_state(network_id=network_id, name=response.get("name"), founder_device_id=founder,
                           keyring=keyring_wrapped, bootstrap_state=BOOTSTRAP_SYNCING,
                           generation=int(response.get("generation", 0)), key_version=int(response.get("key_version", 1)),
                           legacy_instance_map=dict(response.get("legacy_instance_map") or {}), _conn=conn)
        # Trust chain: replay every membership record in order through the verifier.
        members: Dict[str, Member] = {}
        replay = list(response.get("membership_records") or [])
        if not any(r.get("device_id") == device_identity.device_id for r in replay if isinstance(r, dict)):
            replay.append(response["certificate"])
        key_for_verify = keyring.get(max(keyring))
        for record_data in replay:
            try:
                members = membership.apply_record(record_data, network_id=network_id, members=members,
                                                  founder_device_id=founder, network_key=key_for_verify)
            except membership.MembershipError as exc:
                logger.warning("Skipping membership record during enrollment: %s", exc)
                continue
            kind = record_data.get("type")
            if kind in (membership.TYPE_CERTIFICATE, membership.TYPE_RECOVERY):
                record_id = f"cert:{record_data['device_id']}:{record_data['generation']}"
            elif kind == membership.TYPE_REVOCATION:
                record_id = f"rev:{record_data['device_id']}:{record_data['generation']}"
            else:
                from app.network.keyring import record_id as key_record_id
                record_id = key_record_id(record_data)
            store.put_membership_record(record_id, dict(record_data), _conn=conn)
        if device_identity.device_id not in members:
            raise RuntimeError("The join response does not admit this device")
        legacy_by_device = {r.get("device_id"): r.get("legacy_instance_id") for r in replay if isinstance(r, dict)}
        for member in members.values():
            store.upsert_member(device_id=member.device_id, name=member.name, kind=member.kind,
                                signing_public=crypto.b64u_encode(member.signing_public),
                                encryption_public=crypto.b64u_encode(member.encryption_public),
                                generation=member.generation, status=member.status,
                                last_seq_accepted=member.last_seq_accepted,
                                legacy_instance_id=legacy_by_device.get(member.device_id), _conn=conn)
        for public in response.get("members") or []:
            if public.get("device_id") in members and (public.get("endpoints") or public.get("capabilities")):
                store.touch_member(public["device_id"], endpoints=public.get("endpoints") or [],
                                   capabilities=public.get("capabilities") or {}, version=public.get("version"),
                                   platform=public.get("platform"), _conn=conn)
        issuer = response.get("issuer") or {}
        if issuer.get("device_id") in members and issuer.get("endpoints"):
            store.touch_member(issuer["device_id"], endpoints=issuer["endpoints"], _conn=conn)
    ctx = load_context()
    logger.info("Enrolled device %s in network %s (%d members)", device_identity.device_id, network_id, len(members))
    return ctx


def revoke_device(device_id: str, *, reason: str = "") -> dict:
    """Remove a member: publish the revocation, rotate the key for the rest."""
    ctx = require_context()
    if device_id == ctx.device_id:
        raise ValueError("A device cannot remove itself")
    target = ctx.member(device_id)
    if target is None:
        raise KeyError("Unknown device")
    with get_db() as conn:
        cursor = conn.cursor()
        state = store.lock_state(cursor)
        generation = int(state["generation"]) + 1
        # The stream position we will still honour from the removed device:
        # its newest incarnation we know, up to what we have applied of it.
        last_incarnation = store.highest_incarnation(cursor, device_id)
        last_seq = store.get_watermark(cursor, device_id, last_incarnation) if last_incarnation else 0
        revocation = membership.issue_revocation(
            network_id=ctx.network_id, issuer_device_id=ctx.device_id,
            issuer_signing_private=ctx.identity.signing_private, device_id=device_id, generation=generation,
            last_seq_accepted=last_seq, last_incarnation_accepted=last_incarnation, reason=reason)
        ctx.members = membership.apply_record(revocation, network_id=ctx.network_id, members=dict(ctx.members),
                                              founder_device_id=ctx.founder_device_id)
        revoked = ctx.members[device_id]
        store.upsert_member(device_id=device_id, name=revoked.name, kind=revoked.kind,
                            signing_public=crypto.b64u_encode(revoked.signing_public),
                            encryption_public=crypto.b64u_encode(revoked.encryption_public),
                            generation=generation, status=membership.STATUS_REVOKED,
                            last_seq_accepted=last_seq, last_incarnation_accepted=last_incarnation, _conn=conn)
        store.clear_pending_origin(cursor, device_id)
        ctx.generation = generation
        store.update_state(_conn=conn, generation=generation)
        record_membership(cursor, revocation, ctx=ctx)
        # Rotate: a fresh key for everyone still active.
        new_version = ctx.key_version + 1
        new_key = crypto.generate_network_key()
        recipients = {m.device_id: m.encryption_public for m in ctx.active_members()}
        key_record = membership.issue_key_record(
            network_id=ctx.network_id, issuer_device_id=ctx.device_id,
            issuer_signing_private=ctx.identity.signing_private, key_version=new_version, generation=generation,
            new_key=new_key, recipients=recipients)
        record_membership(cursor, key_record, ctx=ctx)  # sealed under the old key so members can read it
        from app.network import keyring as keyring_store
        keyring_store.install(ctx, key_record, new_key, conn)
        from app.network import recovery
        recovery.refresh_wrap_after_rotation(_conn=conn)
    _notify([("membership", f"rev:{device_id}:{generation}", "upsert")])
    try:
        # The account hub must stop relaying for the removed device too; its
        # registration would otherwise keep feeding the account after the
        # LAN stopped listening to it. Retried by the hub loop on failure.
        from app.network import hub
        hub.notify_revoked(device_id)
    except Exception:  # noqa: BLE001
        logger.debug("Hub revocation notice deferred for %s", device_id, exc_info=True)
    kick()
    return {"device_id": device_id, "generation": generation, "key_version": new_version}


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def publish_presence(*, capabilities: Optional[dict] = None, version: Optional[str] = None,
                     endpoints: Optional[List[str]] = None) -> bool:
    ctx = context()
    if ctx is None:
        return False
    row = {
        "device_id": ctx.device_id,
        "endpoints": endpoints if endpoints is not None else _own_endpoints(),
        "capabilities": capabilities or {},
        "version": version,
        "platform": identity_mod.platform_name(),
        "incarnation": ctx.identity.incarnation,
        "updated_at": envelope_mod.now_iso(),
    }
    changed = False
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO network_presence (device_id, endpoints, capabilities, version, platform, incarnation, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (device_id) DO UPDATE SET endpoints = EXCLUDED.endpoints,
                   capabilities = EXCLUDED.capabilities, version = EXCLUDED.version, platform = EXCLUDED.platform,
                   incarnation = EXCLUDED.incarnation, updated_at = EXCLUDED.updated_at""",
            (row["device_id"], json.dumps(row["endpoints"]), json.dumps(row["capabilities"]), row["version"],
             row["platform"], row["incarnation"], row["updated_at"]))
        changed = record(cursor, "presence", ctx.device_id, row, ctx=ctx) is not None
        # Upgrade old local-only renames without asking the user to rename again.
        # Only seed our own name: a peer's cached hostname is not authoritative.
        cursor.execute("""INSERT INTO network_member_names (device_id, name, updated_at)
                          SELECT device_id, name, updated_at FROM network_members WHERE device_id = %s
                          ON CONFLICT (device_id) DO NOTHING""", (ctx.device_id,))
        cursor.execute("SELECT * FROM network_member_names WHERE device_id = %s", (ctx.device_id,))
        name_row = cursor.fetchone()
        if name_row is not None:
            changed = record(cursor, "member_names", ctx.device_id, dict(name_row), ctx=ctx) is not None or changed
    if changed:
        kick()
    return changed


def presence_of(device_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_presence WHERE device_id = %s", (device_id,))
        row = cursor.fetchone()
    if not row:
        return None
    out = dict(row)
    out["endpoints"] = _parse_json(out.get("endpoints")) or []
    out["capabilities"] = _parse_json(out.get("capabilities")) or {}
    return out


def all_presence() -> Dict[str, dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_presence")
        rows = [dict(r) for r in cursor.fetchall()]
    out = {}
    for row in rows:
        row["endpoints"] = _parse_json(row.get("endpoints")) or []
        row["capabilities"] = _parse_json(row.get("capabilities")) or {}
        out[row["device_id"]] = row
    return out
