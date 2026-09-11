"""Cloud backup is an explicit, per-worker option; peer delivery is always on."""
from app.db import network as store


def enabled() -> bool:
    return ((store.get_state() or {}).get("hub") or {}).get("backup_enabled") is True


def configure(value: bool) -> None:
    from app.network import hub
    engine_state = store.get_state()
    if engine_state is None:
        raise ValueError("Join a worker network before configuring backup")
    hub._save_hub_state(backup_enabled=value)
    hub.kick()
