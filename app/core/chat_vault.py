"""Opt-in SSH and HTTPS-service connections using the existing credential-key custody and rotation.

Only encrypted envelopes enter system_settings. The namespace is worker-local
and excluded from network replication. There is no model-facing decrypt API.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid

from app.core import encryption
from app.core.config import settings
from app.core.chat_files import AccessDenied
from app.db import database

# Folder grants are path-only and per-session; only credentials are worth saving.
SAVABLE_KINDS = ("machine", "service")


def describe(spec) -> dict:
    """Catalogue fields shown to the owner. Never includes a secret."""
    if spec.kind == "service":
        return {"kind": "service", "label": spec.label, "target": spec.url,
                "fingerprint": spec.fingerprint, "writable": spec.writable}
    return {"kind": "machine", "label": spec.label, "target": f"{spec.username}@{spec.host}:{spec.port}",
            "fingerprint": spec.fingerprint}


def prefix(owner: tuple) -> str:
    scope = hashlib.sha256(json.dumps(owner[2] if len(owner) > 2 else None).encode()).hexdigest()
    return f"chat_connection:{settings.effective_instance_id}:{scope}:"


def key(owner: tuple, connection_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", connection_id):
        raise AccessDenied("Unknown saved connection.")
    return prefix(owner) + connection_id


def unpack(owner: tuple, connection_id: str, ciphertext: str):
    if not encryption.is_encryption_ready() or not encryption.is_encrypted(ciphertext):
        raise AccessDenied("Secure connection storage is unavailable or locked.")
    try:
        data = json.loads(encryption.decrypt_value(ciphertext))
        if data["binding"] != key(owner, connection_id):
            raise ValueError("binding")
        from app.core.chat_access import GrantInput
        spec = GrantInput.model_validate(data["connection"])
        if spec.kind not in SAVABLE_KINDS:
            raise ValueError("kind")
        return spec
    except Exception:
        raise AccessDenied("Saved connection could not be unlocked on this worker.") from None


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
        cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s ORDER BY key LIMIT 51", (prefix(owner) + "%",))
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


def save(owner: tuple, spec) -> str:
    if not encryption.is_encryption_ready() or spec.kind not in SAVABLE_KINDS:
        raise AccessDenied("Secure connection storage is unavailable.")
    existing = catalog(owner)
    if len(existing) >= 50:
        raise AccessDenied("Remove an unused saved connection before saving another.")
    if any(item["label"].casefold() == spec.label.casefold() for item in existing):
        raise AccessDenied("That connection name is already saved. Choose a different name.")
    connection_id = uuid.uuid4().hex
    from app.core.chat_access import SECRET_FIELDS
    payload = spec.model_dump(exclude=set(SECRET_FIELDS))
    payload.update({name: getattr(spec, name).get_secret_value() for name in SECRET_FIELDS})
    binding = key(owner, connection_id)
    ciphertext = encryption.encrypt_value(json.dumps({"binding": binding, "connection": payload}))
    if not encryption.is_encrypted(ciphertext):
        raise AccessDenied("Secure storage refused an unencrypted value.")
    database.set_system_setting(binding, ciphertext)
    _invalidate_catalog()
    return connection_id


def delete(owner: tuple, connection_id: str):
    if not database.delete_system_setting(key(owner, connection_id)):
        raise AccessDenied("Unknown saved connection.")
    _invalidate_catalog()
