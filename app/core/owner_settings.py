"""Typed owner policy, persisted in system_settings and shared with peer workers.

No environment reading belongs here. Reads happen on the executing worker; no
process cache can keep an imported value ahead of a later UI or peer write.
"""

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
PREFIX = "owner_policy_"


class OwnerPolicy(BaseModel):
    network_require_token_binding: bool = False
    # Capability only, never authorization: an independent owner-authenticated
    # grant on the recipient is still required for every remote terminal.
    network_peer_terminals: bool = False
    session_expire_days: int = Field(default=30, ge=0)
    # The memory learner's model calls per UTC day, per store. 0 is automatic:
    # 200 when a pass runs on a flat-rate subscription row (ChatGPT, SuperGrok),
    # 48 on an API-key, metered or unknown provider. Read at pass time.
    learner_daily_calls: int = Field(default=0, ge=0, le=1000)


def _read(name: str):
    from app.db import database

    default = OwnerPolicy.model_fields[name].default
    try:
        value = database.get_system_setting(PREFIX + name)
        if value is None:
            return default
        return getattr(OwnerPolicy.model_validate({name: value}), name)
    except Exception as exc:
        # Startup callers may run before the database is ready. Never print raw
        # database/validation errors: those can contain values or connection data.
        logger.warning("Could not read owner setting %s (%s); using default", name, type(exc).__name__)
        return default


def peer_terminals_enabled() -> bool:
    return _read("network_peer_terminals")


def require_token_binding() -> bool:
    return _read("network_require_token_binding")


def session_expire_days() -> int:
    return _read("session_expire_days")


def learner_daily_calls() -> int:
    return _read("learner_daily_calls")


def read() -> OwnerPolicy:
    return OwnerPolicy(**{name: _read(name) for name in OwnerPolicy.model_fields})


def write(changes: dict) -> OwnerPolicy:
    from app.db import database

    # Reject unknown keys and validate all changes before writing any of them.
    if changes.keys() - OwnerPolicy.model_fields.keys():
        raise ValueError("Unknown owner policy setting")
    policy = OwnerPolicy.model_validate(changes)
    for name in changes:
        value = getattr(policy, name)
        database.set_system_setting(PREFIX + name, str(value).lower())
    return read()
