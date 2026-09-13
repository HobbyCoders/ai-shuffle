"""Opt-in SSH connections using the existing credential-key custody and rotation.

Only encrypted envelopes enter system_settings. The namespace is worker-local
and excluded from network replication. There is no model-facing decrypt API.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from app.core import encryption
from app.core.config import settings
from app.core.chat_files import AccessDenied
from app.db import database


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
        if spec.kind != "machine":
            raise ValueError("kind")
        return spec
    except Exception:
        raise AccessDenied("Saved connection could not be unlocked on this worker.") from None


def catalog(owner: tuple) -> list[dict]:
    if not encryption.is_encryption_ready():
        return []
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s ORDER BY key LIMIT 51", (prefix(owner) + "%",))
        rows = cursor.fetchall()
    result = []
    for row in rows:
        connection_id = row["key"].rsplit(":", 1)[-1]
        spec = unpack(owner, connection_id, row["value"])
        result.append({"id": connection_id, "label": spec.label,
                       "target": f"{spec.username}@{spec.host}:{spec.port}",
                       "fingerprint": spec.fingerprint})
    return result


def load(owner: tuple, connection_id: str):
    raw = database.get_system_setting(key(owner, connection_id))
    if not raw:
        raise AccessDenied("Unknown saved connection on this worker.")
    return unpack(owner, connection_id, raw)


def save(owner: tuple, spec) -> str:
    if not encryption.is_encryption_ready() or spec.kind != "machine":
        raise AccessDenied("Secure SSH connection storage is unavailable.")
    existing = catalog(owner)
    if len(existing) >= 50:
        raise AccessDenied("Remove an unused saved connection before saving another.")
    if any(item["label"].casefold() == spec.label.casefold() for item in existing):
        raise AccessDenied("That connection name is already saved. Choose a different name.")
    connection_id = uuid.uuid4().hex
    payload = spec.model_dump(exclude={"password", "private_key", "passphrase"})
    payload.update({name: getattr(spec, name).get_secret_value()
                    for name in ("password", "private_key", "passphrase")})
    binding = key(owner, connection_id)
    ciphertext = encryption.encrypt_value(json.dumps({"binding": binding, "connection": payload}))
    if not encryption.is_encrypted(ciphertext):
        raise AccessDenied("Secure storage refused an unencrypted value.")
    database.set_system_setting(binding, ciphertext)
    return connection_id


def delete(owner: tuple, connection_id: str):
    if not database.delete_system_setting(key(owner, connection_id)):
        raise AccessDenied("Unknown saved connection.")
