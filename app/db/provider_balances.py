"""Synced billing keys with worker-local trust markers and balance snapshots.

Poll commits compare the binding and credential revision under a lock.
"""
import json
from uuid import uuid4

from app.db.base import get_db


def bind(row, instance_id, encrypted_key):
    from app.core.api_key_balances import credential_fingerprint

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE model_providers SET balance_key = %s, balance_instance_id = %s,
                balance_snapshot = %s, revision = %s
            WHERE id = %s AND revision = %s
                AND (balance_instance_id IS NULL OR balance_instance_id = %s)
            RETURNING id
        """, (encrypted_key, instance_id,
              json.dumps({"credential": credential_fingerprint({**row, "balance_key": encrypted_key})}) if encrypted_key else None, str(uuid4()),
              row["id"], row.get("revision"), instance_id))
        return cur.fetchone() is not None


def snapshot(row, value):
    from app.core.api_key_balances import credential_fingerprint

    value = {**value, "credential": credential_fingerprint(row)}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE model_providers SET balance_snapshot = %s
            WHERE id = %s AND revision = %s AND balance_key = %s
                AND balance_instance_id = %s AND enabled = TRUE
            RETURNING id
        """, (json.dumps(value), row["id"], row.get("revision"), row.get("balance_key"),
              row.get("balance_instance_id")))
        return cur.fetchone() is not None
