"""Retain concurrent rotation keys while preserving the v1 encrypted envelope.

A numeric epoch is an ordering hint, not a unique key identity. Alternate wraps
are addressed by their key digest; authenticated decryption tries the retained
set for that epoch. Numeric entries remain the selected legacy-compatible keys.
"""
from __future__ import annotations

from app.db import network as store
from app.network import crypto, envelope, membership


def record_id(record):
    return f"key:{record['key_version']}:{crypto.sha256_hex(crypto.canonical_json(record))}"


def restore(ctx, wrapped):
    ctx.key_candidates = {version: {crypto.sha256_hex(key): key} for version, key in ctx.keyring.items()}
    ctx.key_recipients = {digest: set(recipients) for digest, recipients in wrapped.get("@recipients", {}).items()}
    ctx.key_generations = {digest: int(generation) for digest, generation in wrapped.get("@generations", {}).items()}
    for label, wrap in wrapped.items():
        if "@" not in label or label.startswith("@"):
            continue
        version, digest = label.split("@", 1)
        key = crypto.unwrap_key(ctx.identity.encryption_private, wrap)
        if crypto.sha256_hex(key) != digest:
            raise ValueError("Network key digest mismatch")
        ctx.key_candidates.setdefault(int(version), {})[digest] = key


def candidates(ctx, version):
    primary = ctx.keyring.get(version)
    if primary is not None:
        yield primary
    for key in ctx.key_candidates.get(version, {}).values():
        if key != primary:
            yield key


def open_envelope(ctx, env):
    for key in candidates(ctx, env.key_version):
        try:
            return env.open(key), key
        except (crypto.CryptoError, envelope.EnvelopeError):
            continue
    raise envelope.EnvelopeError("cannot open payload")


def open_payload(ctx, version, ciphertext, aad):
    for key in candidates(ctx, version):
        try:
            return crypto.decrypt_payload(key, ciphertext, aad)
        except crypto.CryptoError:
            continue
    raise crypto.CryptoError("cannot open payload")


def safe(ctx, digest):
    revoked = [m for m in ctx.members.values() if m.status == membership.STATUS_REVOKED]
    if not revoked:
        return True
    # A later admitted member can have received an earlier key via enrollment
    # without appearing in its original wraps. A safe write key must also be at
    # least as fresh as every learned revocation, not merely omit its recipient.
    return (ctx.key_generations.get(digest, -1) >= max(m.generation for m in revoked)
            and not ({m.device_id for m in revoked} & ctx.key_recipients.get(digest, set())))


def select(ctx):
    eligible = [(version, digest, key) for version, keys in ctx.key_candidates.items()
                for digest, key in keys.items() if safe(ctx, digest)]
    if not eligible:
        return False
    version, _digest, key = max(eligible, key=lambda item: (item[0], item[1]))
    ctx.key_version = version
    ctx.keyring[version] = key
    return True


def persist_choice(ctx, wrapped, conn):
    # Archive numeric-only legacy entries before changing their selected value.
    for version, keys in ctx.key_candidates.items():
        for digest, key in keys.items():
            alias = f"{version}@{digest}"
            if alias not in wrapped:
                wrapped[alias] = crypto.wrap_key(ctx.identity.encryption_public, key)
    for version, key in ctx.keyring.items():
        digest = crypto.sha256_hex(key)
        alias = f"{version}@{digest}"
        if alias not in wrapped:
            wrapped[alias] = crypto.wrap_key(ctx.identity.encryption_public, key)
        wrapped[str(version)] = wrapped[alias]
    wrapped["@recipients"] = {digest: sorted(recipients) for digest, recipients in ctx.key_recipients.items()}
    wrapped["@generations"] = ctx.key_generations
    store.update_state(_conn=conn, keyring=wrapped, key_version=ctx.key_version)


def install(ctx, record, key, conn):
    version = int(record["key_version"])
    digest = crypto.sha256_hex(key)
    # Preserve the current key too, including pre-upgrade numeric-only wraps.
    for old_version, old_key in ctx.keyring.items():
        ctx.key_candidates.setdefault(old_version, {})[crypto.sha256_hex(old_key)] = old_key
    previous_key = ctx.keyring.get(ctx.key_version)
    keys = ctx.key_candidates.setdefault(version, {})
    keys[digest] = key
    ctx.key_recipients.setdefault(digest, set()).update(record["wraps"])
    ctx.key_generations[digest] = min(ctx.key_generations.get(digest, record["generation"]), record["generation"])
    ctx.keyring[version] = keys[max(keys)]
    select(ctx)
    state = store.get_state(conn)
    wrapped = dict(state.get("keyring") or {})
    wrapped[f"{version}@{digest}"] = record["wraps"][ctx.device_id]
    persist_choice(ctx, wrapped, conn)
    if ctx.network_key != previous_key:
        from app.network import recovery
        recovery.refresh_wrap_after_rotation(_conn=conn)


def for_recipient(ctx, public_key):
    wrapped = {str(version): crypto.wrap_key(public_key, key) for version, key in ctx.keyring.items()}
    for version, keys in ctx.key_candidates.items():
        for digest, key in keys.items():
            wrapped[f"{version}@{digest}"] = crypto.wrap_key(public_key, key)
    wrapped["@recipients"] = {digest: sorted(recipients) for digest, recipients in ctx.key_recipients.items()}
    wrapped["@generations"] = ctx.key_generations
    return wrapped
