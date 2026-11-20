"""Update notices for the two delegated CLIs: Claude Code and Antigravity.

A worker asks each vendor once per start which version is current and keeps
the answer in memory for the notice the app shows. Nothing here downloads,
installs or removes anything: that happens only when the owner asks for it,
in Settings > Providers or outside the app.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

CLAUDE = "claude"
ANTIGRAVITY = "antigravity"
NAMES = {CLAUDE: "Claude Code", ANTIGRAVITY: "Antigravity"}
# One switch per provider and per worker; on until the owner turns it off.
_SETTINGS = {CLAUDE: "claude_cli_update_check", ANTIGRAVITY: "antigravity_cli_update_check"}
_lock = threading.Lock()
_found: Dict[str, Dict[str, Any]] = {}


def _key(provider: str) -> str:
    from app.core.config import settings
    return _SETTINGS[provider] + ":" + settings.effective_instance_id


def enabled(provider: str) -> bool:
    from app.db import database
    return database.get_system_setting(_key(provider)) != "0"


def set_enabled(provider: str, value: bool) -> None:
    from app.db import database
    database.set_system_setting(_key(provider), "1" if value else "0")
    if not value:
        forget(provider)


def suppressed() -> bool:
    """The test suite never asks a vendor anything."""
    return os.environ.get("TESTING") == "1"


def version_key(text: Optional[str]) -> Optional[tuple]:
    """``1.5.7`` → ``(1, 5, 7)``; None when the text is not a plain dotted version
    (``1.5.7-rc1`` included: such versions compare by equality only)."""
    match = re.fullmatch(r"\d+(?:\.\d+)*", (text or "").strip())
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def is_current(installed: Optional[str], latest: str) -> bool:
    """Whether ``installed`` is ``latest`` or newer."""
    if not installed:
        return False
    if installed == latest:
        return True
    have, want = version_key(installed), version_key(latest)
    return have is not None and want is not None and have >= want


def record(provider: str, installed: Optional[str], latest: Optional[str]) -> bool:
    """Remember what a check found. True when ``latest`` is newer than ``installed``.

    A version that cannot be compared (an unknown install, a pre-release tag)
    is never reported as an update.
    """
    newer = bool(installed and latest and version_key(installed) and version_key(latest)
                 and not is_current(installed, latest))
    with _lock:
        if newer:
            _found[provider] = {"installed_version": installed, "latest_version": latest,
                                "checked_at": time.time()}
        else:
            _found.pop(provider, None)
    return newer


def forget(provider: str) -> None:
    """After an install, update or removal the last answer no longer applies."""
    with _lock:
        _found.pop(provider, None)


def latest_version(provider: str, installed: Optional[str] = None) -> Optional[str]:
    """The newer version the last check found, or None.

    With ``installed``, an answer that version has caught up with (the CLI
    updated itself, or was updated outside the app) is dropped.
    """
    with _lock:
        found = _found.get(provider)
        if not found:
            return None
        if installed and is_current(installed, found["latest_version"]):
            del _found[provider]
            return None
        return found["latest_version"]


def available() -> List[Dict[str, Any]]:
    """The updates to tell the owner about, for the notice the app shows."""
    with _lock:
        found = {provider: dict(entry) for provider, entry in _found.items()}
    notices = []
    for provider in (CLAUDE, ANTIGRAVITY):
        entry = found.get(provider)
        if not entry:
            continue
        if provider == CLAUDE:
            # The CLI may have updated itself since the check; its probe is cached.
            from app.core import claude_cli_status
            installed = claude_cli_status.get_status().cli_version
            if not installed or latest_version(provider, installed) is None:
                continue
            entry["installed_version"] = installed
        notices.append({"provider": provider, "name": NAMES[provider],
                        "installed_version": entry["installed_version"],
                        "latest_version": entry["latest_version"]})
    return notices
