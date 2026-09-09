"""Install registry database operations.

On a shared database each AI Shuffle install self-registers a row in the
``installs`` table keyed by its ``instance_id``. Peers read this registry to
list every install, label which device owns a session, and resolve where to
relay live control for a session running on another install.

"Online" is derived from ``last_seen`` against the database clock (``NOW()``)
so it never depends on the reading process's wall clock — avoiding skew when
installs run on different hosts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from app.db.base import get_db

logger = logging.getLogger(__name__)

# An install counts as "online" if it has heartbeat-touched last_seen within
# this window. Kept at ~3x the heartbeat interval (~30s) so a single missed
# beat does not flap the indicator.
ONLINE_WINDOW_SECONDS = 90


class InstallRow(TypedDict):
    """Full install row from SELECT * FROM installs, plus derived ``online``."""
    id: str
    device_name: str
    base_url: Optional[str]
    platform: Optional[str]
    version: Optional[str]
    capabilities: Dict[str, Any]
    approved_at: Optional[str]
    revoked_at: Optional[str]
    approved: bool
    last_seen: Optional[str]
    created_at: str
    updated_at: str
    online: bool


def _parse_capabilities(raw: Any) -> Dict[str, Any]:
    """Parse the capabilities field, handling both str and dict (JSONB)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if raw else {}


def _row_to_install(row: Any) -> Optional[InstallRow]:
    if row is None:
        return None
    result = dict(row)
    result["capabilities"] = _parse_capabilities(result.get("capabilities"))
    result["online"] = bool(result.pop("online", False))
    result["approved"] = bool(result.get("approved_at") and not result.get("revoked_at"))
    return result  # type: ignore[return-value]


# The ``online`` expression is appended to every read so callers get a
# database-authoritative freshness flag without a second round-trip.
_ONLINE_EXPR = "(last_seen IS NOT NULL AND last_seen > NOW() - make_interval(secs => %s)) AS online"


def register_install(
    install_id: str,
    device_name: str,
    base_url: Optional[str],
    platform: Optional[str],
    version: Optional[str],
    capabilities: Optional[Dict[str, Any]] = None,
    approve_new: bool = False,
) -> Optional[InstallRow]:
    """Register (or refresh) this install's registry row.

    Called once on startup. On conflict it refreshes connection details and
    last_seen but deliberately leaves ``device_name`` untouched so a user
    rename (via the Devices panel) survives restarts.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO installs
                (id, device_name, base_url, platform, version, capabilities,
                 approved_at, last_seen, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                base_url = EXCLUDED.base_url,
                platform = EXCLUDED.platform,
                version = EXCLUDED.version,
                capabilities = EXCLUDED.capabilities,
                last_seen = EXCLUDED.last_seen,
                updated_at = EXCLUDED.updated_at
            """,
            (
                install_id,
                device_name,
                base_url,
                platform,
                version,
                json.dumps(capabilities or {}),
                now if approve_new else None,
                now,
                now,
                now,
            ),
        )
    return get_install(install_id)


def touch_install_last_seen(install_id: str) -> None:
    """Heartbeat: bump last_seen for this install (no other columns)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE installs SET last_seen = %s WHERE id = %s",
            (now, install_id),
        )


def touch_install_heartbeat(
    install_id: str, capabilities: Optional[Dict[str, Any]] = None
) -> None:
    """Heartbeat: bump last_seen AND refresh capabilities (V2 Phase 4).

    Capabilities are re-detected and rewritten every beat so a runner that
    gains/loses a capability (Chrome, display, account) is reflected within one
    heartbeat window. Only ``last_seen`` and ``capabilities`` are touched — a
    user rename (``device_name``) and the published ``base_url`` are left to
    ``register_install`` so they survive between beats.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE installs SET last_seen = %s, capabilities = %s WHERE id = %s",
            (now, json.dumps(capabilities or {}), install_id),
        )


def set_install_name(install_id: str, device_name: str) -> Optional[InstallRow]:
    """Rename an install (used by the Devices settings panel)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE installs SET device_name = %s, updated_at = %s WHERE id = %s",
            (device_name, now, install_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_install(install_id)


def set_install_base_url(install_id: str, base_url: Optional[str]) -> None:
    """Persist a (re)discovered peer-reachable base URL for this install."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE installs SET base_url = %s, updated_at = %s WHERE id = %s",
            (base_url, now, install_id),
        )


def set_install_approval(install_id: str, approved: bool) -> Optional[InstallRow]:
    """Approve or revoke a runner without deleting its registry history."""
    with get_db() as conn:
        cursor = conn.cursor()
        if approved:
            cursor.execute(
                """
                UPDATE installs
                SET approved_at = NOW(), revoked_at = NULL, updated_at = NOW()
                WHERE id = %s
                """,
                (install_id,),
            )
        else:
            cursor.execute(
                """
                UPDATE installs
                SET revoked_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (install_id,),
            )
        if cursor.rowcount == 0:
            return None
    return get_install(install_id)


def is_install_approved(install_id: str) -> bool:
    row = get_install(install_id)
    return bool(row and row.get("approved"))


def get_install(install_id: str) -> Optional[InstallRow]:
    """Get one install row by id (= instance_id), with derived ``online``.

    In a network the id may be a device id or a member's legacy instance id.
    """
    directory = _network_directory()
    if directory is not None:
        for row in directory:
            if row["id"] == install_id or row.get("legacy_instance_id") == install_id:
                return row
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT *, {_ONLINE_EXPR} FROM installs WHERE id = %s",
            (ONLINE_WINDOW_SECONDS, install_id),
        )
        return _row_to_install(cursor.fetchone())


def _network_directory() -> Optional[List[InstallRow]]:
    """Runner rows from the worker network, or None when this worker has no network.

    Once a worker belongs to a network its runners come from the members it
    trusts and the presence they published, not from a shared ``installs``
    table. The rows keep the ``installs`` shape so placement, the session
    enrichment and the composer's runner picker need no changes.
    """
    try:
        from app.network import engine, runners
        if engine.context() is None:
            return None
        return runners.directory()["runners"]  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — registry trouble must never break callers
        logger.debug("Network runner directory unavailable", exc_info=True)
        return None


def get_installs() -> List[InstallRow]:
    """List all registered installs, most recently seen first."""
    directory = _network_directory()
    if directory is not None:
        return directory
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT *, {_ONLINE_EXPR} FROM installs ORDER BY last_seen DESC NULLS LAST",
            (ONLINE_WINDOW_SECONDS,),
        )
        out: List[InstallRow] = []
        for row in cursor.fetchall():
            parsed = _row_to_install(row)
            if parsed is not None:
                out.append(parsed)
        return out


def get_installs_map() -> Dict[str, InstallRow]:
    """Return {install_id: row} for batch session enrichment.

    In a network the map is keyed by device id AND by each member's legacy
    instance id, so rows written before migration still resolve.
    """
    out = {row["id"]: row for row in get_installs()}
    for row in list(out.values()):
        legacy = row.get("legacy_instance_id")
        if legacy and legacy not in out:
            out[legacy] = row
    return out


def delete_install(install_id: str) -> bool:
    """Remove an install from the registry."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM installs WHERE id = %s", (install_id,))
        return cursor.rowcount > 0
