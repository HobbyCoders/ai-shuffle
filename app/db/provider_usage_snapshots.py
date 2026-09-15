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
    expected_binding: Optional[tuple[str, str]] = None,
    expected_credential_generation: Optional[str] = None,
    expected_admin_key: Optional[str] = None,
) -> bool:
    """Insert or replace a snapshot, rejecting observations of an old binding."""
    now = datetime.now(timezone.utc).isoformat()
    if provider_type == "openai" and credits is not None:
        raw = {**(raw or {}), "billing_fetched_at": fetched_at}
    with get_db() as conn:
        cursor = conn.cursor()
        if provider_type == "fal":
            if not _lock_fal_generation(cursor, expected_credential_generation):
                return False
            raw = {**(raw or {}), "credential_generation": expected_credential_generation}
        if provider_type == "openai":
            cursor.execute(
                "SELECT openai_billing_org_id, openai_billing_instance_id, openai_admin_key FROM model_providers WHERE id = %s FOR UPDATE",
                (provider_id,),
            )
            binding = cursor.fetchone() or {}
            if expected_admin_key is not None and binding.get("openai_admin_key") != expected_admin_key:
                return False
            current = (binding.get("openai_billing_org_id"), binding.get("openai_billing_instance_id"))
            if expected_binding is not None and current != expected_binding:
                return False
            if credits is not None and current[0] and expected_binding is None:
                return False
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
                credits = COALESCE(EXCLUDED.credits, provider_usage_snapshots.credits),
                raw = CASE WHEN EXCLUDED.provider_type = 'openai' AND EXCLUDED.credits IS NULL
                    THEN provider_usage_snapshots.raw ELSE EXCLUDED.raw END,
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
    return True


def record_provider_usage_failure(
    provider_id: str, provider_type: str, error_kind: str,
    expected_binding: Optional[tuple[str, str]] = None,
    expected_credential_generation: Optional[str] = None,
    expected_admin_key: Optional[str] = None,
) -> None:
    if error_kind not in {
        "billing_access_denied", "missing_credentials", "timeout", "unavailable",
        "billing_browser_unavailable", "billing_sign_in_required", "billing_organization_mismatch",
        "billing_organization_required", "billing_invalid_response", "billing_timeout",
    }:
        error_kind = "unavailable"
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        cursor = conn.cursor()
        if provider_type == "fal" and not _lock_fal_generation(cursor, expected_credential_generation):
            return
        if provider_type == "openai":
            cursor.execute(
                "SELECT openai_billing_org_id, openai_billing_instance_id, openai_admin_key FROM model_providers WHERE id = %s FOR UPDATE",
                (provider_id,),
            )
            binding = cursor.fetchone() or {}
            if expected_admin_key is not None and binding.get("openai_admin_key") != expected_admin_key:
                return False
            current = (binding.get("openai_billing_org_id"), binding.get("openai_billing_instance_id"))
            if expected_binding is not None and current != expected_binding:
                return
            if current[0] and expected_binding is None:
                return
        cursor.execute(
            "SELECT raw, credits, fetched_at FROM provider_usage_snapshots WHERE provider_id = %s",
            (provider_id,),
        )
        rows = rows_to_list(cursor.fetchall())
        previous = rows[0] if rows else {}
        previous_raw = _parse_json_dict(previous.get("raw")) or {}
        if provider_type == "fal" and previous_raw.get("credential_generation") != expected_credential_generation:
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", (provider_id,))
            previous = {}
            previous_raw = {}
        billing_fetched_at = previous_raw.get(
            "billing_fetched_at", previous.get("fetched_at") if previous.get("credits") else None
        )
        metadata = {}
        if previous_raw.get("source") in {"browser_platform", "openai_admin"}:
            metadata = {key: previous_raw.get(key) for key in ("source", "organization_id")}
        if provider_type == "openai" and expected_admin_key is not None:
            metadata.update(source="openai_admin", organization_id=current[0])
        raw = json.dumps({
            **metadata,
            **({"credential_generation": expected_credential_generation} if provider_type == "fal" else {}),
            "usage_error": error_kind,
            "usage_attempted_at": int(now.timestamp() * 1000),
            "billing_fetched_at": billing_fetched_at,
        })
        cursor.execute(
            """
            INSERT INTO provider_usage_snapshots
                (provider_id, provider_type, windows, raw, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(provider_id) DO UPDATE SET
                raw = EXCLUDED.raw,
                updated_at = EXCLUDED.updated_at
            """,
            (provider_id, provider_type, "[]", raw, now.isoformat()),
        )


def _lock_fal_generation(cursor, expected: Optional[str]) -> bool:
    # Lock the credential row until the usage write commits. Credential writes
    # and deletion take this same lock before invalidating the cached balance.
    cursor.execute("SELECT revision FROM system_settings WHERE key = %s FOR UPDATE", ("fal_billing_api_key",))
    row = cursor.fetchone()
    return bool(expected and row and row["revision"] == expected)


def get_all_provider_usage_snapshots() -> List[Dict[str, Any]]:
    """List all persisted provider usage snapshots.

    ``windows`` is parsed to a list; ``credits``/``raw`` to a dict or None.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT p.provider_id, p.provider_type, p.plan_type, p.windows, p.credits, "
            "p.raw, p.fetched_at, s.revision AS fal_credential_generation "
            "FROM provider_usage_snapshots p LEFT JOIN system_settings s ON s.key = 'fal_billing_api_key'"
        )
        rows = rows_to_list(cursor.fetchall())
    valid_rows = []
    for row in rows:
        row["windows"] = _parse_json_list(row.get("windows"))
        row["credits"] = _parse_json_dict(row.get("credits"))
        row["raw"] = _parse_json_dict(row.get("raw"))
        generation = row.pop("fal_credential_generation", None)
        if row["provider_type"] != "fal" or (generation and
                (row["raw"] or {}).get("credential_generation") == generation):
            valid_rows.append(row)
    return valid_rows


def delete_provider_usage_snapshot(provider_id: str) -> bool:
    """Delete a provider's usage snapshot row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM provider_usage_snapshots WHERE provider_id = %s",
            (provider_id,),
        )
        return cursor.rowcount > 0
