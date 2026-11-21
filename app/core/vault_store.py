"""Opt-in SSH and HTTPS-service connections using the existing credential-key custody and rotation.

Only encrypted envelopes enter system_settings. The entries follow the owner
to every worker in the network, as other shared settings do: worker sync
carries them under the network key and each worker stores them under its own
data key. There is no model-facing decrypt API.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid

from app.core import encryption
from app.core.config import settings
from app.core.vault_files import AccessDenied
from app.db import database

logger = logging.getLogger(__name__)

# Folder grants are path-only and per-session; only credentials are worth saving.
SAVABLE_KINDS = ("machine", "service")

# Entries live under this prefix, which worker sync shares (app.network.policy
# shares every settings key that is not worker-local).
SHARED_PREFIX = "vault_entry:"
# Where each worker kept its own entries before they synced. Still worker-local
# in the sync policy, so a row left here never leaves this worker.
LOCAL_PREFIX = "chat_connection:"

MAX_ENTRIES = 50  # per principal, enforced when one is added
# Workers that each held entries before they synced can together exceed the
# cap. Every entry must stay listed so the owner can remove the extra ones.
_READ_LIMIT = 500


def describe(spec) -> dict:
    """Catalogue fields shown to the owner. Never includes a secret.

    The separate address fields prefill the panel's edit form; ``uses_key``
    says only which kind of secret is stored, never the secret.
    """
    if spec.kind == "service":
        return {"kind": "service", "label": spec.label, "target": spec.url,
                "fingerprint": spec.fingerprint, "writable": spec.writable,
                "auth_header": spec.auth_header, "auth_scheme": spec.auth_scheme,
                # Set when the stored secret is this account's password (HTTP Basic).
                "username": spec.username.strip()}
    return {"kind": "machine", "label": spec.label, "target": f"{spec.username}@{spec.host}:{spec.port}",
            "fingerprint": spec.fingerprint, "host": spec.host, "port": spec.port, "username": spec.username,
            "uses_key": bool(spec.private_key.get_secret_value())}


def prefix(owner: tuple) -> str:
    # The principal only (None is the install owner; API user ids are the same
    # on every worker), so the key names the same entry on each of them.
    scope = hashlib.sha256(json.dumps(owner[2] if len(owner) > 2 else None).encode()).hexdigest()
    return f"{SHARED_PREFIX}{scope}:"


def key(owner: tuple, connection_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", connection_id):
        raise AccessDenied("Unknown saved connection.")
    return prefix(owner) + connection_id


def _open(binding: str, ciphertext: str):
    """The connection inside an envelope stored under ``binding``, and no other key."""
    if not encryption.is_encryption_ready() or not encryption.is_encrypted(ciphertext):
        raise AccessDenied("The Vault is locked or unavailable on this worker.")
    try:
        data = json.loads(encryption.decrypt_value(ciphertext))
        if data["binding"] != binding:
            raise ValueError("binding")
        from app.core.vault_access import GrantInput
        spec = GrantInput.model_validate(data["connection"])
        if spec.kind not in SAVABLE_KINDS:
            raise ValueError("kind")
        return spec
    except Exception:
        raise AccessDenied("This saved sign-in could not be unlocked on this worker.") from None


def unpack(owner: tuple, connection_id: str, ciphertext: str):
    return _open(key(owner, connection_id), ciphertext)


# Only absence is cached: no credentials or decrypted catalogue entries survive a
# snapshot. Saving/deleting invalidates all scopes, including concurrent readers.
_EMPTY_CATALOG_TTL = 300
_empty_catalog: dict[str, float] = {}
_catalog_generation = 0
_catalog_lock = threading.Lock()


def _invalidate_catalog():
    global _catalog_generation
    with _catalog_lock:
        _catalog_generation += 1
        _empty_catalog.clear()


def changed_elsewhere() -> None:
    """Another worker's saved sign-ins landed here: an owner cached as having none may have some now."""
    _invalidate_catalog()


def _written() -> None:
    """An entry changed here: forget cached absence and let worker sync publish it now."""
    _invalidate_catalog()
    try:
        from app.network import transport
        # Without this wake the row waits for the next full scan (up to a minute).
        transport.request_scan(["system_settings"])
    except Exception:  # noqa: BLE001 - the periodic scan still publishes it
        logger.debug("Could not wake sync for a Vault change", exc_info=True)


def catalog(owner: tuple, *, cache_empty: bool = False) -> list[dict]:
    if not encryption.is_encryption_ready():
        return []
    scope = prefix(owner)
    with _catalog_lock:
        generation = _catalog_generation
        if cache_empty and _empty_catalog.get(scope, 0) > time.monotonic():
            return []
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s ORDER BY key LIMIT %s",
                       (scope + "%", _READ_LIMIT))
        rows = cursor.fetchall()
    if cache_empty and not rows:
        with _catalog_lock:
            if generation == _catalog_generation:
                if len(_empty_catalog) >= 128:
                    _empty_catalog.clear()
                _empty_catalog[scope] = time.monotonic() + _EMPTY_CATALOG_TTL
    result = []
    for row in rows:
        connection_id = row["key"].rsplit(":", 1)[-1]
        spec = unpack(owner, connection_id, row["value"])
        result.append({"id": connection_id, **describe(spec)})
    return result


def load(owner: tuple, connection_id: str):
    raw = database.get_system_setting(key(owner, connection_id))
    if not raw:
        raise AccessDenied("Unknown saved connection on this worker.")
    return unpack(owner, connection_id, raw)


def _store(binding: str, spec) -> None:
    """Encrypt ``spec`` (secrets included) under its owner-scoped binding."""
    from app.core.vault_access import SECRET_FIELDS
    payload = spec.model_dump(exclude=set(SECRET_FIELDS))
    payload.update({name: getattr(spec, name).get_secret_value() for name in SECRET_FIELDS})
    ciphertext = encryption.encrypt_value(json.dumps({"binding": binding, "connection": payload}))
    if not encryption.is_encrypted(ciphertext):
        raise AccessDenied("Secure storage refused an unencrypted value.")
    database.set_system_setting(binding, ciphertext)


def _label_taken(owner: tuple, label: str, *, except_id: str = "") -> bool:
    return any(item["label"].casefold() == label.casefold() and item["id"] != except_id
               for item in catalog(owner))


# Edits, delete and post-connect host-key pinning read and rewrite one entry;
# serialising them means none can resurrect or overwrite another's result.
# Adding takes it too, so its name and capacity checks see every earlier write.
_write_lock = threading.Lock()


def same_connection(a, b) -> bool:
    """Whether two specs describe the same saved connection (grant length aside)."""
    return a.model_copy(update={"minutes": b.minutes}) == b


def save(owner: tuple, spec) -> str:
    if not encryption.is_encryption_ready() or spec.kind not in SAVABLE_KINDS:
        raise AccessDenied("The Vault is locked or unavailable on this worker.")
    with _write_lock:
        existing = catalog(owner)
        if len(existing) >= MAX_ENTRIES:
            raise AccessDenied("The Vault is full. Remove a sign-in you no longer use before saving another.")
        if any(item["label"].casefold() == spec.label.casefold() for item in existing):
            raise AccessDenied("The Vault already has a sign-in with that name. Choose a different name.")
        connection_id = uuid.uuid4().hex
        _store(key(owner, connection_id), spec)
    _written()
    return connection_id


def update(owner: tuple, connection_id: str, change):
    """Replace a saved connection with ``change(current)``; returns ``(old, new)``.

    ``change`` validates and raises ``AccessDenied``; nothing is written then.
    The id, and so the binding, stays the same.
    """
    if not encryption.is_encryption_ready():
        raise AccessDenied("The Vault is locked or unavailable on this worker.")
    with _write_lock:
        current = load(owner, connection_id)
        new = change(current)
        if new.kind != current.kind or new.kind not in SAVABLE_KINDS:
            raise AccessDenied("A saved sign-in keeps its type.")
        if new.label.casefold() != current.label.casefold() and _label_taken(owner, new.label, except_id=connection_id):
            raise AccessDenied("The Vault already has a sign-in with that name. Choose a different name.")
        _store(key(owner, connection_id), new)
    _written()
    return current, new


def pin_fingerprint(owner: tuple, connection_id: str, expected, fingerprint: str) -> bool:
    """Remember the SSH host key a saved connection without one was first verified with.

    Only when the entry still holds exactly ``expected`` (no edit or delete in
    between) and still has no pinned key; later uses then verify against it.
    """
    if not fingerprint:
        return False
    with _write_lock:
        try:
            current = load(owner, connection_id)
        except AccessDenied:
            return False
        if current.kind != "machine" or current.fingerprint.strip() or not same_connection(current, expected):
            return False
        _store(key(owner, connection_id), current.model_copy(update={"fingerprint": fingerprint}))
    _written()
    return True


def delete(owner: tuple, connection_id: str):
    with _write_lock:
        if not database.delete_system_setting(key(owner, connection_id)):
            raise AccessDenied("Unknown saved connection.")
    _written()


def adopt_local_entries() -> int:
    """Move the entries this worker kept for itself into the shared namespace; returns how many.

    Runs at startup once credential storage is unlocked, before sync starts.
    An entry that cannot be unlocked here stays where it was, as unusable as
    before. Safe to repeat after a crash between the two writes: a shared row
    that already exists is kept (another worker may have changed it since)
    and only the old row goes.
    """
    if not encryption.is_encryption_ready():
        return 0
    local = f"{LOCAL_PREFIX}{settings.effective_instance_id}:"
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s ORDER BY key", (LOCAL_PREFIX + "%",))
        rows = cursor.fetchall()
    moved = 0
    with _write_lock:
        for row in rows:
            # Compared here, not in SQL: an instance id may hold LIKE wildcards.
            match = re.fullmatch(re.escape(local) + r"([0-9a-f]{64}):([0-9a-f]{32})", row["key"])
            if not match:
                continue
            shared = f"{SHARED_PREFIX}{match[1]}:{match[2]}"
            try:
                spec = _open(row["key"], row["value"])
            except AccessDenied:
                continue
            if not database.get_system_setting(shared):
                _store(shared, spec)
            database.delete_system_setting(row["key"])
            moved += 1
    if moved:
        _written()
    return moved
