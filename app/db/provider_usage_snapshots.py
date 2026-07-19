"""Per-provider usage-snapshot database operations.

Non-Claude provider paths (ChatGPT/Codex routed through the loopback translator
gateway, OpenRouter on the direct base-URL path) surface plan-usage/limit data
that does not fit the per-account, per-install ``usage_snapshots`` model. Each
enabled provider instead persists a single row here, keyed by ``provider_id``,
and the usage endpoint serves every provider's snapshot as a ``providers[]``
array (readers land in later chunks).

``windows``/``credits``/``raw`` are stored as JSON blobs (JSONB in Postgres,
TEXT via the test shim); reads parse them defensively — accepting a str or an
already-decoded value — mirroring ``app.db.usage_snapshots._parse_windows``.
``raw`` only ever holds allowlisted header names/values or a whitelisted subset
of a key-endpoint JSON — never an ``auth_token`` or Bearer value (enforced by
the normalizers that call this module).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db.base import get_db, rows_to_list

logger = logging.getLogger(__name__)


def _parse_json_list(raw: Any) -> List[Dict[str, Any]]:
    """Parse a JSON list field, handling both str and list (JSONB)."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return raw if isinstance(raw, list) else []


def _parse_json_dict(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse a nullable JSON dict field, handling str, dict (JSONB) and None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return raw if isinstance(raw, dict) else None


def upsert_provider_usage_snapshot(
    provider_id: str,
    provider_type: str,
    plan_type: Optional[str],
    windows: List[Dict[str, Any]],
    credits: Optional[Dict[str, Any]],
    raw: Optional[Dict[str, Any]],
    fetched_at: Optional[int],
) -> None:
    """Insert or replace this provider's usage snapshot row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO provider_usage_snapshots
                (provider_id, provider_type, plan_type, windows, credits, raw,
                 fetched_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(provider_id) DO UPDATE SET
                provider_type = EXCLUDED.provider_type,
                plan_type = EXCLUDED.plan_type,
                windows = EXCLUDED.windows,
                credits = EXCLUDED.credits,
                raw = EXCLUDED.raw,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = EXCLUDED.updated_at
            """,
            (
                provider_id,
                provider_type,
                plan_type,
                json.dumps(windows or []),
                json.dumps(credits) if credits is not None else None,
                json.dumps(raw) if raw is not None else None,
                fetched_at,
                now,
            ),
        )


def get_all_provider_usage_snapshots() -> List[Dict[str, Any]]:
    """List all persisted provider usage snapshots.

    ``windows`` is parsed to a list; ``credits``/``raw`` to a dict or None.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT provider_id, provider_type, plan_type, windows, credits, "
            "raw, fetched_at FROM provider_usage_snapshots"
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["windows"] = _parse_json_list(row.get("windows"))
        row["credits"] = _parse_json_dict(row.get("credits"))
        row["raw"] = _parse_json_dict(row.get("raw"))
    return rows


def delete_provider_usage_snapshot(provider_id: str) -> bool:
    """Delete a provider's usage snapshot row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM provider_usage_snapshots WHERE provider_id = %s",
            (provider_id,),
        )
        return cursor.rowcount > 0
