"""Shared active-account selections for built-in model-provider slots.

``enabled`` remains the public projection. A single shared system setting per
slot is the selection register: network LWW resolves concurrent switches on ONE
record, rather than independently merging enabled flags on several accounts.
Custom gateways have no slot and retain independent enabled flags.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from psycopg import Cursor

SLOT_SETTING_PREFIX = "model_provider_slot:"
# Shared by local writers and network materialization, including empty slots.
_SLOT_LOCK_KEY = 7143029164601


def model_provider_slot(row: Mapping[str, Any]) -> Optional[str]:
    """Match Settings' classifyRow, including auth_mode taking precedence."""
    auth = row.get("auth_mode")
    kind = row.get("provider_type")
    if auth == "chatgpt":
        return "openai:subscription"
    if auth == "grok" or kind == "grok":
        return "grok:subscription"
    if kind == "chatgpt":
        return "openai:subscription"
    return {"openai": "openai:key", "xai": "grok:key",
            "openrouter": "openrouter:key"}.get(kind)


def lock_model_provider_slots(cursor: Cursor) -> None:
    """Transaction lock, not a process lock: covers multiple app workers too."""
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_SLOT_LOCK_KEY,))


def select_model_provider_slot(cursor: Cursor, slot: str, provider_id: Optional[str]) -> None:
    """Write the slot selection register, or an explicit off selection."""
    cursor.execute(
        "INSERT INTO system_settings (key, value, updated_at, revision) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = EXCLUDED.updated_at, revision = EXCLUDED.revision",
        (SLOT_SETTING_PREFIX + slot, provider_id or "", datetime.now(timezone.utc).isoformat(), str(uuid4())),
    )


def clear_model_provider_slot(cursor: Cursor, slot: str, provider_id: str) -> None:
    """Clear only this account's selection, never a sibling's newer selection."""
    cursor.execute("SELECT value FROM system_settings WHERE key = %s", (SLOT_SETTING_PREFIX + slot,))
    selection = cursor.fetchone()
    if selection and selection["value"] == provider_id:
        select_model_provider_slot(cursor, slot, None)


def reconcile_model_provider_slots(cursor: Cursor, *, initialize: bool = False) -> None:
    """Project selections atomically; initialize legacy slots deterministically.

    A selection can arrive before its account in sync: leave that slot off until
    the row arrives. Never substitute another account for a missing/deleted row.
    Initialization keeps the earliest-created enabled account (id breaks ties).
    Existing selections, including an explicit off selection, are never reset.
    """
    cursor.execute("SELECT id, provider_type, auth_mode, enabled, created_at FROM model_providers ORDER BY created_at, id")
    groups = {}
    for row in cursor.fetchall():
        slot = model_provider_slot(row)
        if slot:
            groups.setdefault(slot, []).append(row)
    cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s", (SLOT_SETTING_PREFIX + "%",))
    selected = {r["key"][len(SLOT_SETTING_PREFIX):]: r["value"] for r in cursor.fetchall()}
    for slot, rows in groups.items():
        if slot not in selected:
            # An old peer without selection registers can still introduce rows.
            # Until the register arrives, reduce duplicates in a stable order.
            winner = next((r["id"] for r in rows if r["enabled"]), None)
            if initialize:
                select_model_provider_slot(cursor, slot, winner)
        else:
            winner = selected[slot]
        for row in rows:
            enabled = row["id"] == winner
            if bool(row["enabled"]) == enabled:
                continue
            cursor.execute(
                "UPDATE model_providers SET enabled = %s, balance_snapshot = NULL, revision = %s WHERE id = %s",
                (enabled, str(uuid4()), row["id"]),
            )
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", (row["id"],))
