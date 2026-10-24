"""Host-side messaging watch reads through the native or isolated connector interface."""
from __future__ import annotations

from typing import Any

from app.db import watches as store
from app.db.async_database import run_sync


async def poll(
    kind: str, instance: Any, source: dict[str, Any], cursor: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    channel_id = str(source.get("channel_id", ""))
    allowed = instance.config.get(
        "allowed_chat_ids" if kind == "telegram" else "allowed_channel_ids"
    )
    if allowed and channel_id not in {str(item) for item in allowed}:
        raise PermissionError("Watch channel is no longer allowed by the connector")
    if kind == "telegram":
        spec = getattr(instance, "PACKAGE_SPEC", None)
        if spec is not None:
            from app.core.connector_packages import CATALOG

            expected = CATALOG["packages"]["telegram"]["files"]["telegram.py"]
            if spec.get("files", {}).get("telegram.py") != expected:
                raise ValueError("Update the Telegram connector package to enable watch events")
        return await run_sync(store.connector_page, instance.connector_id, channel_id, cursor)
    try:
        events, checkpoint = await instance.poll_watch(source, cursor)
    except (AttributeError, RuntimeError) as exc:
        raise ValueError("Update the connector package to enable durable watch history") from exc
    return [(key, payload) for key, payload in events], checkpoint
