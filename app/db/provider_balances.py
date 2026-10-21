"""Synced billing keys with worker-local trust markers and balance snapshots.

Poll commits compare the binding and credential revision under a lock.
"""
import json
import time
from uuid import uuid4

from app.db.base import get_db
from app.db.provider_usage_snapshots import MAX_FUTURE_SKEW_MS


def bind(row, instance_id, encrypted_key):
    from app.core.api_key_balances import credential_fingerprint

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE model_providers SET balance_key = %s, balance_instance_id = %s,
                balance_snapshot = %s, revision = %s
            WHERE id = %s AND revision = %s
            RETURNING id
        """, (encrypted_key, instance_id,
              json.dumps({"credential": credential_fingerprint({**row, "balance_key": encrypted_key})}) if encrypted_key else None, str(uuid4()),
              row["id"], row.get("revision")))
        changed = cur.fetchone() is not None
        if changed:
            from app.db.provider_usage_snapshots import delete_shared_observations
            delete_shared_observations(conn, row["id"])
        return changed


def snapshot(row, value):
    from app.core.api_key_balances import credential_fingerprint

    value = {**value, "credential": credential_fingerprint(row)}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance_snapshot FROM model_providers WHERE id = %s FOR UPDATE", (row["id"],))
        stored_row = cur.fetchone() or {}
        try:
            stored = json.loads(stored_row.get("balance_snapshot") or "{}")
        except (ValueError, TypeError):
            stored = {}
        fetched = value.get("fetched_at") or 0
        # An import runs independently of a local poll. A deferred/stale poll
        # must never restore the cached reading it captured before the import.
        # A fast clock later corrected must not freeze readings or hide a 401.
        if (stored.get("fetched_at") or 0) > time.time() * 1000 + MAX_FUTURE_SKEW_MS:
            stored = {}
        if (stored.get("fetched_at") or 0) > fetched:
            if value.get("last_error_kind") and (value.get("last_attempted_at") or 0) >= stored["fetched_at"]:
                value = {**stored, **{k: value[k] for k in
                         ("last_error_kind", "last_error_scope", "last_attempted_at")}}
            else:
                return False
        elif value.get("source_worker") and (stored.get("last_attempted_at") or 0) >= fetched:
            return False  # a newer local failure won the race with this import
        cur.execute("""
            UPDATE model_providers SET balance_snapshot = %s
            WHERE id = %s AND revision = %s AND balance_key = %s
                AND balance_instance_id = %s AND enabled = TRUE
            RETURNING id
        """, (json.dumps(value), row["id"], row.get("revision"), row.get("balance_key"),
              row.get("balance_instance_id")))
        return cur.fetchone() is not None
