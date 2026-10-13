"""Per-install Claude usage-snapshot database operations.

The Claude usage windows belong to whichever Claude login the CLI on an install
uses. On a shared database two installs (e.g. a primary + an offload worker) may
each be signed in differently, so each install persists the windows its own CLI
reported here, keyed by ``instance_id``, and the usage endpoint serves EVERY
install's snapshot (joined to the installs registry for
``device_name``/``online``). No Claude account identity is stored: only
``account_key``, an opaque hash the usage monitor derives from the CLI's
sign-in, so rows of devices on the same account can be told apart from rows of
devices on different accounts (NULL when unknown or written by an older build).

``windows`` is stored as a JSON blob (JSONB in Postgres, TEXT via the test
shim); reads parse it defensively — accepting a str or an already-decoded list —
mirroring ``app.db.installs._parse_capabilities``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db.base import get_db, rows_to_list

logger = logging.getLogger(__name__)


def _parse_windows(raw: Any) -> List[Dict[str, Any]]:
    """Parse the windows field, handling both str and list (JSONB)."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return raw if isinstance(raw, list) else []


def _parse_account_key(raw: Any) -> Optional[str]:
    """A stored ``account_key``, or None when absent or not a short string.

    A peer row is written by another device, so its value is checked here
    rather than trusted.
    """
    if isinstance(raw, str) and 0 < len(raw) <= 128:
        return raw
    return None


def upsert_usage_snapshot(
    instance_id: str,
    windows: List[Dict[str, Any]],
    available: bool,
    generation: Optional[int],
    fetched_at: Optional[int],
    account_key: Optional[str] = None,
) -> None:
    """Insert or replace this install's usage snapshot row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO usage_snapshots
                (instance_id, windows, available, generation, fetched_at, account_key, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(instance_id) DO UPDATE SET
                windows = EXCLUDED.windows,
                available = EXCLUDED.available,
                generation = EXCLUDED.generation,
                fetched_at = EXCLUDED.fetched_at,
                account_key = EXCLUDED.account_key,
                updated_at = EXCLUDED.updated_at
            """,
            (
                instance_id,
                json.dumps(windows or []),
                available,
                generation,
                fetched_at,
                account_key,
                now,
            ),
        )


def touch_usage_snapshot_observed(
    instance_id: str,
    window_type: str,
    observed_at: int,
) -> bool:
    """Advance one window's ``observed_at`` (and the row's ``fetched_at``) only.

    For a repeat reading whose values did not change: the utilization, reset,
    availability and generation stay as they are, so peers learn the window was
    seen again without a new generation or a rewrite of the other windows.
    Never moves a time backwards. Returns whether the row changed.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT windows, fetched_at FROM usage_snapshots WHERE instance_id = %s FOR UPDATE",
            (instance_id,),
        )
        rows = rows_to_list(cursor.fetchall())
        if not rows:
            return False
        windows = _parse_windows(rows[0].get("windows"))
        changed = False
        for w in windows:
            if not isinstance(w, dict) or w.get("type") != window_type:
                continue
            prev = w.get("observed_at")
            if not isinstance(prev, (int, float)) or prev < observed_at:
                w["observed_at"] = observed_at
                changed = True
        if not changed:
            return False
        prev_fetched = rows[0].get("fetched_at")
        fetched = max(int(prev_fetched), observed_at) if isinstance(prev_fetched, (int, float)) else observed_at
        cursor.execute(
            "UPDATE usage_snapshots SET windows = %s, fetched_at = %s, updated_at = %s "
            "WHERE instance_id = %s",
            (json.dumps(windows), fetched, datetime.now(timezone.utc).isoformat(), instance_id),
        )
        return True


def get_all_usage_snapshots() -> List[Dict[str, Any]]:
    """List all persisted usage snapshots, ``windows`` parsed to a list."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT instance_id, windows, available, generation, fetched_at, account_key "
            "FROM usage_snapshots"
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["windows"] = _parse_windows(row.get("windows"))
        row["available"] = bool(row.get("available"))
        row["account_key"] = _parse_account_key(row.get("account_key"))
    return rows


def get_usage_snapshot_publishers() -> Dict[str, str]:
    """``{instance_id: device_id}`` of the network device that last wrote each row.

    On a worker network every usage row arrives as an op signed by the device
    that published it, and the sync layer records that device per row. It
    names the row's device even when the publisher's membership carries no
    legacy instance id, and on any publisher version, because the publisher
    sends nothing new. Raises when the sync tables are absent (no network);
    callers treat that as "no publishers known".
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT record_id, origin_device_id FROM sync_records "
            "WHERE collection = %s AND deleted = FALSE",
            ("usage_snapshots",),
        )
        rows = rows_to_list(cursor.fetchall())
    return {r["record_id"]: r["origin_device_id"] for r in rows
            if r.get("record_id") and r.get("origin_device_id")}


def delete_usage_snapshot(instance_id: str) -> bool:
    """Delete an install's usage snapshot row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM usage_snapshots WHERE instance_id = %s", (instance_id,)
        )
        return cursor.rowcount > 0
