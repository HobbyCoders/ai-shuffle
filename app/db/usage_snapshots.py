"""Per-install Claude usage-snapshot database operations.

The 5-hour Claude usage window is PER-ACCOUNT. On a shared database two installs
(e.g. a primary + an offload worker) may each run a DIFFERENT Claude account, so
a single in-memory usage singleton polled with the local credential shows the
wrong account's reset time for sessions offloaded to the other install.

Each install therefore persists its own snapshot here, keyed by ``instance_id``,
and the usage endpoint serves EVERY install's snapshot (joined to the installs
registry for ``device_name``/``online``). ``account_*`` records which Claude
account produced the reading (Nones in legacy single-account / API-key mode).

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


def upsert_usage_snapshot(
    instance_id: str,
    windows: List[Dict[str, Any]],
    available: bool,
    generation: Optional[int],
    fetched_at: Optional[int],
    account_id: Optional[str] = None,
    account_email: Optional[str] = None,
    account_name: Optional[str] = None,
) -> None:
    """Insert or replace this install's usage snapshot row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO usage_snapshots
                (instance_id, windows, available, generation, fetched_at,
                 account_id, account_email, account_name, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(instance_id) DO UPDATE SET
                windows = EXCLUDED.windows,
                available = EXCLUDED.available,
                generation = EXCLUDED.generation,
                fetched_at = EXCLUDED.fetched_at,
                account_id = EXCLUDED.account_id,
                account_email = EXCLUDED.account_email,
                account_name = EXCLUDED.account_name,
                updated_at = EXCLUDED.updated_at
            """,
            (
                instance_id,
                json.dumps(windows or []),
                available,
                generation,
                fetched_at,
                account_id,
                account_email,
                account_name,
                now,
            ),
        )


def get_all_usage_snapshots() -> List[Dict[str, Any]]:
    """List all persisted usage snapshots, ``windows`` parsed to a list."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT instance_id, windows, available, generation, fetched_at, "
            "account_id, account_email, account_name FROM usage_snapshots"
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["windows"] = _parse_windows(row.get("windows"))
        row["available"] = bool(row.get("available"))
    return rows


def delete_usage_snapshot(instance_id: str) -> bool:
    """Delete an install's usage snapshot row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM usage_snapshots WHERE instance_id = %s", (instance_id,)
        )
        return cursor.rowcount > 0
