"""Runner capability detection for constraint-based placement (V2 Phase 4).

``detect_capabilities()`` returns a small, JSON-serializable dict describing
what THIS runner can do. It is merged into ``installs.capabilities`` at
register + heartbeat (``app/core/cluster.py``); the placement resolver
(``app/core/placement.py``) filters candidate runners on these keys.

Design contract (OD-8):
  * Every probe is BEST-EFFORT and NON-BLOCKING — pure PATH / env / filesystem
    lookups only, never a subprocess that could hang.
  * Any failure degrades to the safe default (``False`` / ``[]``) and NEVER
    raises: a detection miss must never block placement onto the local runner.
  * No credential or account identity is ever advertised.

Shape::

    {
        "os": "windows" | "linux" | "darwin",
        "has_display": bool,
        "computer_use": bool,
        "has_chrome": bool,
        "docker": bool,
        "claude_cli_signed_in": bool,
        "provider_kinds": ["openai", ...],
        "remote_task": int,
    }

``computer_use`` is whether computer use is switched on for this worker and
nothing is known to block it (Docker, a Wayland or headless Linux session,
missing packages). It never opens the display; the settings page's status
runs the full check.

``claude_cli_signed_in`` is whether this runner's own Claude Code CLI reports
a login, from the last ``claude auth status`` probe (no subprocess here). Each
worker signs in to its CLI separately, so placement can require it with a
``{"claude_cli_signed_in": True}`` constraint.

``provider_kinds`` lists the provider TYPES of this worker's enabled model
providers that hold a credential (presence only, never an identity, account
name or token). ``remote_task`` is the version of remote subagent dispatch this
release can run (0 or absent: none), so a parent refuses an older worker by
name before any round trip.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _detect_os() -> str:
    """Coarse OS family: windows / darwin / linux (everything else -> linux)."""
    plat = sys.platform
    if plat.startswith("win"):
        return "windows"
    if plat == "darwin":
        return "darwin"
    return "linux"


def _detect_has_display() -> bool:
    """Whether this runner has a graphical display (needed for local Chrome/GUI).

    Desktop OSes (Windows/macOS) present a display for a normal interactive run;
    on Linux a display exists iff an X11/Wayland env var is set. Best-effort —
    a headless Windows service would over-report, which is acceptable for a
    non-blocking capability hint.
    """
    try:
        if sys.platform.startswith("win") or sys.platform == "darwin":
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    except Exception:  # noqa: BLE001 — detection must never raise
        return False


def _detect_computer_use() -> bool:
    """Whether computer use is turned on here and nothing is known to block it."""
    try:
        from app.mcp.computer_use.scaling import computer_use_quick_check

        if not computer_use_quick_check().available:
            return False
        from app.api.computer_use import get_computer_use_enabled

        return bool(get_computer_use_enabled())
    except Exception:  # noqa: BLE001 — detection must never raise
        return False


# Names probed on PATH (covers Linux/macOS/Windows variants).
_CHROME_NAMES = (
    "chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome.exe",
)
# Well-known fixed install locations checked when PATH lookup misses.
_CHROME_WIN_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_CHROME_MAC_PATHS = (
    "/Applications/Google Chrome.app",
    "/Applications/Chromium.app",
)


def _detect_has_chrome() -> bool:
    """Whether a Chrome/Chromium binary is present (built-in browser tooling)."""
    try:
        for name in _CHROME_NAMES:
            if shutil.which(name):
                return True
        if sys.platform.startswith("win"):
            return any(os.path.exists(p) for p in _CHROME_WIN_PATHS)
        if sys.platform == "darwin":
            return any(os.path.exists(p) for p in _CHROME_MAC_PATHS)
        return False
    except Exception:  # noqa: BLE001 — detection must never raise
        return False


def _detect_docker() -> bool:
    """Whether docker is available on this runner.

    True when running inside a container (``/.dockerenv`` present) OR a
    ``docker`` CLI is on PATH. Pure filesystem/PATH lookups — no subprocess.
    """
    try:
        if os.path.exists("/.dockerenv"):
            return True
        return shutil.which("docker") is not None
    except Exception:  # noqa: BLE001 — detection must never raise
        return False


def _detect_claude_cli_signed_in() -> bool:
    """Whether the last CLI probe reported a login (cached; never spawns)."""
    try:
        from app.core import claude_cli_status

        status = claude_cli_status.peek()
        return bool(status and status.signed_in)
    except Exception:  # noqa: BLE001 — detection must never raise
        return False


# The remote subagent wire this release understands. Version 2 adds the
# optional ``context_window`` start field, version 3 the optional ``fast``
# field; a parent sends each only to a target at that version or later (an
# older target refuses unknown fields).
REMOTE_TASK_VERSION = 3


def _detect_provider_kinds() -> List[str]:
    """Sorted provider types with an enabled, credentialed row (no decryption)."""
    try:
        from app.db.base import get_db

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT provider_type FROM model_providers
                WHERE enabled = TRUE
                  AND ((auth_token IS NOT NULL AND auth_token <> '')
                    OR (codex_access_token IS NOT NULL AND codex_access_token <> ''))
                """
            )
            return sorted({str(row["provider_type"]) for row in cursor.fetchall()
                           if row.get("provider_type")})
    except Exception:  # noqa: BLE001 — detection must never raise
        return []


def detect_capabilities() -> Dict[str, Any]:
    """Best-effort snapshot of this runner's placement capabilities.

    Never raises: each probe is independently guarded and falls back to a safe
    default so registration/heartbeat can always proceed.
    """
    return {
        "os": _detect_os(),
        "has_display": _detect_has_display(),
        "computer_use": _detect_computer_use(),
        "has_chrome": _detect_has_chrome(),
        "docker": _detect_docker(),
        "claude_cli_signed_in": _detect_claude_cli_signed_in(),
        "provider_kinds": _detect_provider_kinds(),
        "remote_task": REMOTE_TASK_VERSION,
    }
