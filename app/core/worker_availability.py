"""Launch-kind hints and the synced, per-worker owner availability override.

The install mode distinguishes containers from native installs, not desktops
from servers. Only the desktop launcher's existing markers identify a desktop;
a display, Docker CLI or OS alone says nothing about whether a worker sleeps.
"""
from __future__ import annotations

import os
from typing import TypedDict

from app.core.config import settings
from app.db import database

SETTING_PREFIX = "worker_always_on:"


class Availability(TypedDict):
    worker_id: str
    desktop: bool
    always_on: bool
    always_on_override: bool | None


def setting_key() -> str:
    """Like other per-worker settings, scope the row to the stable install ID."""
    return SETTING_PREFIX + settings.effective_instance_id


def is_desktop() -> bool:
    """Use launch provenance, never infer a desktop from an OS or a display."""
    if not settings.is_local_mode():
        return False
    from app.core import desktop_listener

    return (
        os.environ.get("SHUFFLE_DESKTOP") == "1"
        or desktop_listener.configured()
        or (
            os.environ.get("SHUFFLE_LAUNCHER_VERSION", "0").isdigit()
            and int(os.environ.get("SHUFFLE_LAUNCHER_VERSION", "0")) > 0
        )
    )


def snapshot() -> Availability:
    desktop = is_desktop()
    raw = database.get_system_setting_cached(setting_key())
    override = {"true": True, "false": False}.get(raw)
    return Availability(
        worker_id=settings.effective_instance_id,
        desktop=desktop,
        always_on=not desktop if override is None else override,
        always_on_override=override,
    )


def save(always_on: bool | None) -> Availability:
    """Null restores launch-kind defaults; ordinary settings sync carries the row."""
    if always_on is None:
        database.delete_system_setting(setting_key())
    else:
        database.set_system_setting(setting_key(), "true" if always_on else "false")
    return snapshot()
