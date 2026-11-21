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
import secrets
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
# The SSH host key a first connection verified for an entry saved without one,
# as a row of its own. Sync keeps one whole row when two workers wrote it apart,
# so a worker that had not yet heard of a removal or a new password must never
# rewrite the entry to record a key: that would bring the old credentials back
# on every worker. This row holds no credentials. It is named by a random value
# the entry carries for as long as its address and its host key field stay as
# they are (see ``_same_host``), so a rename or a new password on one worker
# keeps the key another worker recorded, and a key recorded for an address the
# entry has since left is another row, which nothing reads.
#
# These rows are never removed. A removal would be published, and it could land
# on an entry that is still in use: one a later edit on another worker kept or
# brought back would lose its key and trust whichever host answered next. A row
# left behind holds one host key fingerprint, and no address or credentials.
PIN_PREFIX = "vault_pin:"
#: Settings keys whose arrival from another worker changes what the Vault holds.
SYNCED_PREFIXES = (SHARED_PREFIX, PIN_PREFIX)
# Where each worker kept its own entries before they synced. Still worker-local
# in the sync policy, so a row left here never leaves this worker.
LOCAL_PREFIX = "chat_connection:"

# Per principal, enforced when one is added. Workers that each held entries
# before they synced can together exceed it; every entry stays listed so the
# owner can remove the extra ones.
MAX_ENTRIES = 50
_HOST_KEY = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


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


def _pin_key(owner: tuple, connection_id: str, trust: str) -> str:
    """Where the host key recorded for an entry is stored, given the entry's ``trust`` value.

    Named by a digest of it: nothing from inside the encrypted entry is
    repeated outside it.
    """
    mark = hashlib.sha256(f"{PIN_PREFIX}{trust}".encode()).hexdigest()
    return f"{PIN_PREFIX}{key(owner, connection_id)[len(SHARED_PREFIX):]}:{mark}"


_SYNCED_KEY = re.compile(r"(?:vault_entry:[0-9a-f]{64}:([0-9a-f]{32})|vault_pin:[0-9a-f]{64}:([0-9a-f]{32}):[0-9a-f]{64})")


def connection_id_of(setting_key: str) -> str:
    """The entry a synced Vault row (the entry, or a host key recorded for it) belongs to; "" for any other key."""
    match = _SYNCED_KEY.fullmatch(setting_key or "")
    return (match[1] or match[2]) if match else ""


def _entry(binding: str, ciphertext: str):
    """The connection in an envelope stored under ``binding`` (and no other key), with its ``trust`` value."""
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
        trust = data.get("trust", "")
        return spec, trust if isinstance(trust, str) and re.fullmatch(r"[0-9a-f]{32}", trust) else ""
    except Exception:
        raise AccessDenied("This saved sign-in could not be unlocked on this worker.") from None


def _open(binding: str, ciphertext: str):
    return _entry(binding, ciphertext)[0]


def unpack(owner: tuple, connection_id: str, ciphertext: str):
    return _open(key(owner, connection_id), ciphertext)


_UNREAD = object()


def _pin_value(binding: str, raw) -> str:
    """The host key in the pin row stored under ``binding``; "" when it is missing or not one this worker can read."""
    if not raw or not encryption.is_encrypted(raw):
        return ""
    try:
        data = json.loads(encryption.decrypt_value(raw))
        if data["binding"] == binding and _HOST_KEY.fullmatch(data["fingerprint"]):
            return data["fingerprint"]
    except Exception:  # noqa: BLE001 - not a pin this worker can read
        pass
    return ""


def _pinned(owner: tuple, connection_id: str, spec, trust: str, pin=_UNREAD):
    """``spec`` with the host key a first connection recorded for it.

    ``pin`` is the stored pin row for ``trust`` when the caller already read
    it. A row that is missing or unreadable leaves the entry unpinned; one
    written under another ``trust`` is another row and is never looked at.
    """
    if spec.kind != "machine" or spec.fingerprint.strip() or not trust:
        return spec
    binding = _pin_key(owner, connection_id, trust)
    fingerprint = _pin_value(binding, database.get_system_setting(binding) if pin is _UNREAD else pin)
    return spec.model_copy(update={"fingerprint": fingerprint}) if fingerprint else spec


def _rows(scope: str, widths: tuple = (32,)) -> dict[str, str]:
    """Stored values by what follows ``scope`` in their key, for the keys that are exactly ``scope`` plus that.

    What follows is hex of the given ``widths``, joined by colons: an entry's
    id, or an id and the mark that names a host key's row. Matched in the query, so
    nothing is cut off: LIKE reads "_" as any one character, hence the escapes
    and the hex spelled as that many of them. Another setting whose name merely
    starts like one of these (the model-parameters API appends to a caller's
    key) is not a row of the Vault.
    """
    pattern = scope.replace("!", "!!").replace("%", "!%").replace("_", "!_") + ":".join("_" * width for width in widths)
    shape = ":".join(f"[0-9a-f]{{{width}}}" for width in widths)
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s ESCAPE '!' ORDER BY key", (pattern,))
        found = cursor.fetchall()
    return {row["key"][len(scope):]: row["value"] for row in found
            if row["key"].startswith(scope) and re.fullmatch(shape, row["key"][len(scope):])}


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
    rows = _rows(scope)
    if cache_empty and not rows:
        with _catalog_lock:
            if generation == _catalog_generation:
                if len(_empty_catalog) >= 128:
                    _empty_catalog.clear()
                _empty_catalog[scope] = time.monotonic() + _EMPTY_CATALOG_TTL
    result, pins = [], None
    for connection_id, value in rows.items():
        spec, trust = _entry(scope + connection_id, value)
        if spec.kind == "machine" and not spec.fingerprint.strip() and trust:
            pin_scope = PIN_PREFIX + scope[len(SHARED_PREFIX):]
            if pins is None:
                pins = _rows(pin_scope, (32, 64))
            binding = _pin_key(owner, connection_id, trust)
            spec = _pinned(owner, connection_id, spec, trust, pins.get(binding[len(pin_scope):]))
        result.append({"id": connection_id, **describe(spec)})
    return result


def load(owner: tuple, connection_id: str):
    binding = key(owner, connection_id)
    raw = database.get_system_setting(binding)
    if not raw:
        raise AccessDenied("Unknown saved connection on this worker.")
    spec, trust = _entry(binding, raw)
    return _pinned(owner, connection_id, spec, trust)


def _seal(binding: str, body: dict) -> str:
    ciphertext = encryption.encrypt_value(json.dumps({"binding": binding, **body}))
    if not encryption.is_encrypted(ciphertext):
        raise AccessDenied("Secure storage refused an unencrypted value.")
    return ciphertext


def _sealed(binding: str, spec, trust: str = "") -> str:
    """``spec`` (secrets included) encrypted under its owner-scoped binding.

    Each stored version carries a random value of its own. Worker sync keeps a
    hash of what it decrypted, in its own tables and in what it sends; without
    this, that hash would let someone holding a database backup test guesses
    of a weak password without the data key.

    ``trust`` is a second random value, which names the row of the host key
    recorded for the entry (``_pin_key``). An edit passes the one it read to
    keep that key, and nothing to start without one.
    """
    from app.core.vault_access import SECRET_FIELDS
    payload = spec.model_dump(exclude=set(SECRET_FIELDS))
    payload.update({name: getattr(spec, name).get_secret_value() for name in SECRET_FIELDS})
    return _seal(binding, {"connection": payload, "version": secrets.token_hex(16),
                           "trust": trust or secrets.token_hex(16)})


def _store(binding: str, spec) -> None:
    database.set_system_setting(binding, _sealed(binding, spec))


def _read(owner: tuple, connection_id: str):
    """An entry, the stored row it was read from (for ``_replace``) and that row's ``trust`` value."""
    binding = key(owner, connection_id)
    row = database.get_system_setting_snapshot(binding)
    if row is None or not row.value:
        raise AccessDenied("Unknown saved connection on this worker.")
    spec, trust = _entry(binding, row.value)
    return _pinned(owner, connection_id, spec, trust), row, trust


def _replace(owner: tuple, connection_id: str, spec, row, trust: str) -> bool:
    """Rewrite an entry only if its row is still the one ``_read`` returned.

    Worker sync applies another worker's edit or removal without taking
    ``_write_lock``. Writing over that would bring a removed sign-in back, or
    undo a password change, and publish the stale one to every worker.
    """
    binding = key(owner, connection_id)
    return database.set_system_setting_if_unchanged(binding, _sealed(binding, spec, trust), row)


def _same_host(old, new) -> bool:
    """Whether an edit left alone what a recorded host key rests on: the address, and the key field itself.

    A rename, another account or a new secret does, and keeps the key, as the
    edit form promises: also the key another worker recorded and this one has
    not received yet. Another address, or a key the owner cleared or replaced,
    starts again from the next connection.
    """
    from app.core.vault_access import _same_address
    return (old.kind == new.kind == "machine" and old.port == new.port
            and old.fingerprint == new.fingerprint and _same_address(old.host, new.host))


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
    The id, and so the binding, stays the same. A host key recorded by a first
    connection is part of ``current`` and of the ``new`` returned, but an edit
    that keeps it leaves it where it was recorded (see ``PIN_PREFIX``): the
    entry holds only a host key the owner entered.
    """
    if not encryption.is_encryption_ready():
        raise AccessDenied("The Vault is locked or unavailable on this worker.")
    with _write_lock:
        current, row, trust = _read(owner, connection_id)
        new = change(current)
        if new.kind != current.kind or new.kind not in SAVABLE_KINDS:
            raise AccessDenied("A saved sign-in keeps its type.")
        if new.label.casefold() != current.label.casefold() and _label_taken(owner, new.label, except_id=connection_id):
            raise AccessDenied("The Vault already has a sign-in with that name. Choose a different name.")
        keeps, kept = _same_host(current, new), new
        if keeps and not _open(key(owner, connection_id), row.value).fingerprint.strip():
            # The key ``current`` shows was read from the row beside the entry,
            # and stays only there. Copied into the entry it would outrank that
            # row for good: also after sync settled the row on the key another
            # worker recorded, which this worker had not received yet.
            kept = new.model_copy(update={"fingerprint": ""})
        if not _replace(owner, connection_id, kept, row, trust if keeps else ""):
            raise AccessDenied("This sign-in was just changed or removed on another worker. Check it and try again.")
    _written()
    return current, new


def pin_fingerprint(owner: tuple, connection_id: str, expected, fingerprint: str) -> bool:
    """Remember the SSH host key a saved connection without one was first verified with.

    Only when the entry still holds exactly ``expected`` (no edit or delete in
    between) and still has no pinned key; later uses then verify against it.
    The entry is not rewritten (see ``PIN_PREFIX``): the key is stored beside
    it, in the row the entry read here names. If the entry is at another
    address, or gone, by the time the row is written or reaches another
    worker, nothing reads the row. A key another worker recorded for the same
    address in the meantime stands.
    """
    fingerprint = (fingerprint or "").rstrip("=")
    if not _HOST_KEY.fullmatch(fingerprint):
        return False
    with _write_lock:
        try:
            current, _row, trust = _read(owner, connection_id)
        except AccessDenied:
            return False
        if (current.kind != "machine" or current.fingerprint.strip() or not trust
                or not same_connection(current, expected)):
            return False
        binding = _pin_key(owner, connection_id, trust)
        held = database.get_system_setting_snapshot(binding)
        if held is not None and _pin_value(binding, held.value):
            return False
        # Over nothing, or over a row that holds no readable key; sync may have stored one since.
        if not database.set_system_setting_if_unchanged(binding, _seal(binding, {"fingerprint": fingerprint}), held):
            return False
    _written()
    return True


def delete(owner: tuple, connection_id: str):
    with _write_lock:
        # The entry only: a host key recorded for it stays (see ``PIN_PREFIX``).
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
            # Written only where no shared row exists; one that does is kept.
            database.set_system_setting_if_unchanged(shared, _sealed(shared, spec), None)
            database.delete_system_setting(row["key"])
            moved += 1
    if moved:
        _written()
    return moved
