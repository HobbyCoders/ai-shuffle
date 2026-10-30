"""Rolling-upgrade safety for schema-defined nested credentials.

Presence opts in to the new wire form. Historical signed ops cannot be
rewritten for an old recipient: it must take a ciphertext-form state copy.
"""
import hashlib
import json

from app.core import encryption
from app.db import network as store

CAPABILITY = "nested-secret-rekey"


def _supports(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return isinstance(value, dict) and value.get(CAPABILITY) == 1


def enabled(cursor=None, *, recipient=None):
    """All active members, including offline/unknown ones, must advertise it.

    Direct-contact capabilities are independent of signed/relayed presence.
    Both must confirm support; only another direct contact can lift a withdrawal.
    Unknown contacts fail closed; revoked members do not block rollout.
    """
    if cursor is None:
        with store.get_db() as conn:
            return enabled(conn.cursor(), recipient=recipient)
    cursor.execute("""SELECT m.device_id, m.capabilities AS contact, p.capabilities AS presence
                      FROM network_members m LEFT JOIN network_presence p ON p.device_id = m.device_id
                      WHERE m.status = 'active'""")
    rows = cursor.fetchall()
    if recipient is not None:
        rows = [row for row in rows if row["device_id"] == recipient]
    return bool(rows) and all(_supports(row["contact"]) and _supports(row["presence"]) for row in rows)


def quorum(cursor=None):
    """Bind acknowledgement to this membership/presence generation, not just a bool.

    A joining peer may receive a legacy snapshot after an earlier all-new
    quorum published plaintext. Its first advertisement must republish too.
    """
    if cursor is None:
        with store.get_db() as conn:
            return quorum(conn.cursor())
    if not enabled(cursor):
        return None
    cursor.execute("""SELECT m.device_id, r.clock FROM network_members m
                      LEFT JOIN sync_records r ON r.collection = 'presence' AND r.record_id = m.device_id
                      WHERE m.status = 'active' ORDER BY m.device_id""")
    return hashlib.sha256(json.dumps([(row["device_id"], row["clock"]) for row in cursor.fetchall()]).encode()).hexdigest()


def ops_compatible(ops, recipient, cursor):
    """Protect relays and old history too, without holes in signed streams."""
    if not ops or not recipient:
        return True
    from app.network import engine, envelope, keyring, policy
    protected = [(op, policy.COLLECTIONS.get(op.get("collection"))) for op in ops]
    protected = [(op, coll) for op, coll in protected
                 if coll is not None and coll.map_secrets is not None and op.get("op") == envelope.OP_UPSERT]
    if not protected or enabled(cursor, recipient=recipient):
        return True
    ctx = engine.require_context()
    for op, coll in protected:
        try:
            record, _ = keyring.open_envelope(ctx, envelope.Envelope.from_dict(op))
            plaintext = False

            def inspect(value):
                nonlocal plaintext
                plaintext |= not encryption.is_encrypted(value)
                return value

            coll.map_secrets(dict(record), inspect)
            if plaintext:
                return False
        except (ValueError, RuntimeError, TypeError):
            return False  # cannot prove this payload safe for a legacy receiver
    return True


def transfer_compatible(manifest, recipient):
    # Missing means a transfer captured by the pre-gate implementation: never
    # serve its pages to a legacy recipient, even within the capture cache TTL.
    return manifest.get("nested_secret_rekey") is False or (enabled() and enabled(recipient=recipient))
