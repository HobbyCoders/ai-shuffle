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
                 last_seen, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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


def get_install(install_id: str) -> Optional[InstallRow]:
    """Get one install row by id (= instance_id), with derived ``online``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT *, {_ONLINE_EXPR} FROM installs WHERE id = %s",
            (ONLINE_WINDOW_SECONDS, install_id),
        )
        return _row_to_install(cursor.fetchone())


def get_installs() -> List[InstallRow]:
    """List all registered installs, most recently seen first."""
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
    """Return {install_id: row} for batch session enrichment."""
    return {row["id"]: row for row in get_installs()}


def delete_install(install_id: str) -> bool:
    """Remove an install from the registry."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM installs WHERE id = %s", (install_id,))
        return cursor.rowcount > 0
