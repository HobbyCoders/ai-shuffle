"""Worker-local balance bindings and snapshots, independent of spend/headers.

These fields deliberately do not appear in network.policy's shared allowlist.
Poll commits compare the binding and inference credential revision under a lock.
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
        """, (encrypted_key, instance_id if encrypted_key else None,
              json.dumps({"credential": credential_fingerprint(row)}) if encrypted_key else None, str(uuid4()),
              row["id"], row.get("revision"), instance_id))
        return cur.fetchone() is not None


def snapshot(row, value):
    previous = json.loads(row.get("balance_snapshot") or "{}")
    value = {**value, "credential": previous.get("credential")}
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
