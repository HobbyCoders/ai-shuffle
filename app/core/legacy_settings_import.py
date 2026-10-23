"""One-release, name-only compatibility import for retired behaviour env names.

Delete this reader in the next release. Future batches append a NEW version,
never extend an already-applied version. Credentials must never enter this map.
"""

import logging
from pydantic import ValidationError
from pydantic_settings import DotEnvSettingsSource, EnvSettingsSource

from app.core.config import Settings, _resolve_env_file
from app.core.owner_settings import OwnerPolicy, PREFIX
from app.db import database

logger = logging.getLogger(__name__)
VERSION = 1
# Deliberately worker-local (network policy excludes migration:*). A peer's
# marker cannot suppress this worker's import before its settings have synced.
MIGRATION_KEY = "migration:legacy_owner_policy:1"
LEGACY_NAMES = {
    "network_require_token_binding": ("NETWORK_REQUIRE_TOKEN_BINDING",),
    "network_peer_terminals": ("NETWORK_PEER_TERMINALS",),
    "session_expire_days": ("SESSION_EXPIRE_DAYS",),
}
# Append a new numbered batch for later PRs; existing batches are immutable.
MIGRATIONS: dict[int, dict[str, tuple[str, ...]]] = {VERSION: LEGACY_NAMES}


def _explicit_values(env_file: str | None = None) -> dict[str, str | None]:
    # Use the SAME case-insensitive source parsing and env-over-dotenv precedence
    # as startup Settings, without its blank-to-default field validator. False,
    # zero, blank and missing must remain distinguishable here.
    dotenv = DotEnvSettingsSource(Settings, env_file=env_file if env_file is not None else _resolve_env_file())
    env = EnvSettingsSource(Settings)
    return {**dotenv.env_vars, **env.env_vars}


def import_legacy_settings(*, env_file: str | None = None) -> None:
    """Adopt explicit, valid, nonblank values only when no persisted key exists.

    Rows use the normal system_settings write path so they join peer sync. The
    boot lock serializes shared-DB workers; insert-only seeding also protects a
    concurrent UI write. A crash before the marker can safely retry.
    """
    with database.boot_init_lock():
        source = None
        for version, names in sorted(MIGRATIONS.items()):
            marker = f"migration:legacy_owner_policy:{version}"
            if database.get_system_setting(marker) is not None:
                continue
            if source is None:
                source = _explicit_values(env_file)
            for name, aliases in names.items():
                key = PREFIX + name
                if database.get_system_setting(key) is not None:
                    continue
                explicit = next((alias for alias in aliases if alias.lower() in source), None)
                if explicit is None:
                    continue
                value = source[explicit.lower()]
                if value is None or not value.strip():
                    logger.info("Ignoring blank legacy setting %s", explicit)
                    continue
                try:
                    parsed = getattr(OwnerPolicy.model_validate({name: value}), name)
                except ValidationError:
                    logger.warning("Ignoring invalid legacy setting %s", explicit)
                    continue
                database.get_or_create_system_setting(key, lambda: str(parsed).lower())
                logger.info("Adopted legacy setting %s", explicit)
            database.set_system_setting(marker, str(version))
