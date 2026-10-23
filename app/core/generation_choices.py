"""Remembered generation choices, stored as account preferences.

The same per-account preference store as ``composer.modelFast.<model>``:

- ``generation.choice.<kind>``: the remembered pick for ``image``, ``video``,
  ``audio`` or ``3d``. A run that names no source or model uses it without
  asking; unattended runs use it too.
- ``generation.lastChoice.<kind>``: the latest pick from the choice card. It is
  only offered first ("Same as last time"), never run without asking.
- ``generation.choicesMigrated``: set once the saved Settings media defaults
  (``image_model``, ``video_model``, ``audio_model``, ``model3d_model`` and
  their stored options) were copied into remembered choices. From then on the
  Settings default no longer decides those four kinds for this account.

Values are validated here on every read; a malformed record is ignored.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

KINDS = ("image", "video", "audio", "3d")
CHOICE_PREFIX = "generation.choice."
LAST_PREFIX = "generation.lastChoice."
MIGRATED_KEY = "generation.choicesMigrated"
# The Settings media default each unified generation kind replaced.
LEGACY_SETTING = {"image": "image_model", "video": "video_model", "audio": "audio_model", "3d": "model3d_model"}
# Curated option settings saved beside the model (the Node tool applied them).
_LEGACY_OPTIONS = {
    "image": {"aspect_ratio": "image_aspect_ratio", "resolution": "image_resolution"},
    "video": {"aspect_ratio": "video_aspect_ratio", "duration": "video_duration", "resolution": "video_resolution"},
}
ORIGINS = ("card", "settings", "migrated")
_MAX_ID = 512
_MAX_OPTIONS_BYTES = 8192


def choice_key(kind: str) -> str:
    return CHOICE_PREFIX + kind


def last_key(kind: str) -> str:
    return LAST_PREFIX + kind


def clean(value: Any) -> dict | None:
    """A validated ``{source, account, worker, model, options[, origin]}`` or None."""
    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    if not isinstance(model, str) or not 0 < len(model.strip()) <= _MAX_ID:
        return None
    choice: dict[str, Any] = {"model": model.strip()}
    for key in ("source", "account", "worker"):
        item = value.get(key)
        if item is None or item == "":
            continue
        if not isinstance(item, str) or len(item) > _MAX_ID:
            return None
        choice[key] = item.strip()
    if not (choice.get("source") or choice.get("account")):
        return None
    options = value.get("options") or {}
    if not isinstance(options, Mapping):
        return None
    try:
        encoded = json.dumps(dict(options), sort_keys=True)
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_OPTIONS_BYTES:
        return None
    choice["options"] = json.loads(encoded)
    if value.get("origin") in ORIGINS:
        choice["origin"] = value["origin"]
    return choice


def _local_worker() -> str:
    from app.network import runners
    return runners.self_device_id() or "local"


def legacy_choice(kind: str) -> dict | None:
    """The explicitly saved Settings media default for ``kind`` as a choice.

    Only a saved fal.ai model qualifies. The ChatGPT-plan image setting is not
    a fal choice; it keeps working as the Settings image provider (#1796).
    """
    from app.api.settings_media import get_model_params
    from app.core.provider import subscription_images as sub
    from app.db import database

    setting = LEGACY_SETTING[kind]
    model = database.get_system_setting(setting)
    if not isinstance(model, str) or not model.strip() or sub.is_subscription_model(model):
        return None
    options: dict[str, Any] = {}
    for name, key in _LEGACY_OPTIONS.get(kind, {}).items():
        stored = database.get_system_setting(key)
        if stored not in (None, "", "0", 0):
            options[name] = int(stored) if name == "duration" and str(stored).isdigit() else stored
    options.update(get_model_params(setting))
    return clean({"source": "fal", "account": "fal-ai", "worker": _local_worker(), "model": model,
                  "options": options, "origin": "migrated"})


def migrate(identity: tuple[str, str]) -> None:
    """Copy saved Settings media defaults into this account's remembered choices once.

    Never overwrites a choice the account already made. The marker is written
    last, so an interrupted migration is retried on the next run.
    """
    from app.db import database

    if database.get_user_preference(*identity, MIGRATED_KEY) is not None:
        return
    for kind in KINDS:
        choice = legacy_choice(kind)
        if choice:
            database.set_user_preference(*identity, choice_key(kind), choice, only_if_missing=True)
    database.set_user_preference(*identity, MIGRATED_KEY, True, only_if_missing=True)


def load(identity: tuple[str, str], *, run_migration: bool = True) -> dict:
    """``{"remembered": {kind: choice}, "last": {kind: choice}, "migrated": bool}``."""
    from app.db import database

    if run_migration:
        migrate(identity)
    remembered: dict[str, dict] = {}
    last: dict[str, dict] = {}
    migrated = False
    for row in database.get_all_user_preferences(*identity):
        key = row.get("key") or ""
        if key == MIGRATED_KEY:
            migrated = row.get("value") is True
            continue
        for prefix, target in ((CHOICE_PREFIX, remembered), (LAST_PREFIX, last)):
            kind = key[len(prefix):] if key.startswith(prefix) else None
            if kind in KINDS:
                choice = clean(row.get("value"))
                if choice:
                    target[kind] = choice
    return {"remembered": remembered, "last": last, "migrated": migrated}


def save(identity: tuple[str, str], kind: str, value: Any, *, remember: bool, origin: str = "card") -> dict:
    """Record a pick: always as the last choice, and as remembered when asked."""
    from app.db import database

    if kind not in KINDS:
        raise ValueError("Unknown generation kind.")
    choice = clean({**(value if isinstance(value, Mapping) else {}), "origin": origin})
    if choice is None:
        raise ValueError("A choice needs a model and a source or account, with object options.")
    # Migrate first: the saved Settings default must never overwrite this pick later.
    migrate(identity)
    database.set_user_preference(*identity, last_key(kind), choice)
    if remember:
        database.set_user_preference(*identity, choice_key(kind), choice)
    return choice


def forget(identity: tuple[str, str], kind: str) -> bool:
    """Ask again next time: drop the remembered choice (the last one stays)."""
    from app.db import database

    if kind not in KINDS:
        raise ValueError("Unknown generation kind.")
    migrate(identity)
    return database.delete_user_preference(*identity, choice_key(kind))


def for_session(session_row: Mapping[str, Any] | None) -> dict:
    """Choices of the account behind a session (its owner, or its API user).

    Any failure (account service outage, ambiguous owner, database) returns no
    choices and ``migrated=False``, so the Settings default keeps applying.
    """
    from app.core.model_effort import _caller_preference_identity

    try:
        return load(_caller_preference_identity(session_row))
    except Exception:  # noqa: BLE001 - never block a run on preference reads
        logger.warning("Could not read remembered generation choices", exc_info=True)
        return {"remembered": {}, "last": {}, "migrated": False}
