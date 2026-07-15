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
  * ``account_ids`` are ids ONLY — never tokens/secrets.

Shape::

    {
        "os": "windows" | "linux" | "darwin",
        "has_display": bool,
        "has_chrome": bool,
        "docker": bool,
        "account_ids": [str, ...],
    }

Note on ``account_ids``: Claude accounts are shared-DB (the ``claude_accounts``
table holds the credentials blob and any install can read/use any account — see
``app/db/claude_accounts.py``), so the SAME id set is reported by every runner.
The ids are advertised for symmetry with OD-8; because availability is not
install-scoped, the placement account filter is a no-op (documented in
``app/core/placement.py``).
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


def _detect_account_ids() -> List[str]:
    """Ids of the Claude accounts visible to this runner (ids only, no secrets).

    Shared-DB, so this is the same set on every install (see module docstring).
    Best-effort — a registry/DB hiccup degrades to an empty list.
    """
    try:
        from app.db.claude_accounts import get_all_claude_accounts

        return [a["id"] for a in get_all_claude_accounts() if a.get("id")]
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
        "has_chrome": _detect_has_chrome(),
        "docker": _detect_docker(),
        "account_ids": _detect_account_ids(),
    }
