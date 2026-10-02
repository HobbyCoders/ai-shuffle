"""The fal.ai media key and its on/off switch.

Every fal.ai call on this worker (the agents' AI tools, dictation and
text-to-speech on fal models, fal voice calls, the model catalog) spends one
key: the owner's ``fal_api_key`` or, for an API user, the key its credential
policy picks. Settings > Providers > fal.ai can turn fal.ai off without
removing the key. While it is off no reader gets a key, including the
``FAL_KEY`` / ``FAL_API_KEY`` environment fallback the AI tools honour.

The switch is the ``fal_enabled`` system setting. Unset means on, so an
install that never touched it keeps working.

Synchronous (database reads): call via ``run_sync`` from async code.
"""

from __future__ import annotations

from typing import Optional, Tuple

from app.db import database

KEY = "fal_enabled"
API_KEY_SETTING = "fal_api_key"
ENV_KEYS = ("FAL_KEY", "FAL_API_KEY")

# The credential policy a disabled fal.ai reports, next to the API-user
# policies ``user_provided`` / ``optional`` / ``admin_provided``.
OFF_POLICY = "fal_off"
OFF_MESSAGE = "fal.ai is off. Turn it on in Settings > Providers > fal.ai."

_OFF_VALUES = ("false", "0", "off", "no")


def is_enabled() -> bool:
    """Whether fal.ai is on for this worker (unset reads as on)."""
    value = database.get_system_setting_cached(KEY)
    if value is None:
        return True
    return value.strip().lower() not in _OFF_VALUES


def set_enabled(enabled: bool) -> bool:
    """Turn fal.ai on or off. The settings write announces itself to clients."""
    database.set_system_setting(KEY, "true" if enabled else "false")
    return bool(enabled)


def key_stored() -> bool:
    """Whether the owner's fal.ai key is stored (on or off, not decrypted)."""
    return bool(database.get_system_setting(API_KEY_SETTING))


def api_key(api_user_id: Optional[str] = None) -> Optional[str]:
    """The stored fal.ai key a caller may spend, or None (also when off).

    The owner (no ``api_user_id``) spends the owner's key; an API user gets
    what its fal.ai credential policy resolves to.
    """
    return api_key_with_policy(api_user_id)[0]


def api_key_with_policy(api_user_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(key or None, policy)``.

    The policy is :data:`OFF_POLICY` while fal.ai is off, None for the owner
    and the API user's fal.ai credential policy otherwise.
    """
    if not is_enabled():
        return None, OFF_POLICY
    from app.core import environment

    if not api_user_id:
        return environment._get_decrypted_api_key(API_KEY_SETTING), None
    return environment.resolve_api_credential_with_policy(API_KEY_SETTING, api_user_id)

