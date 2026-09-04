from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import notification_service


@pytest.fixture(autouse=True)
def reset_push_throttle():
    notification_service._last_push.clear()
    yield
    notification_service._last_push.clear()


async def _emit_with_mocks(**kwargs):
    created = MagicMock(
        side_effect=lambda user_id, **values: {
            "id": "n1",
            "user_id": user_id,
            "severity": values["severity"],
            "surface": values["surface"],
            "message": values["message"],
            "persist": True,
            "action": (
                {"kind": values["action_kind"], "payload": values["action_payload"]}
                if values["action_kind"]
                else None
            ),
        }
    )

    async def run_sync(fn):
        return fn()

    with patch("app.db.async_database.run_sync", side_effect=run_sync), \
         patch("app.db.notifications.create_notification", created), \
         patch("app.core.sync_engine.sync_engine.broadcast_notification", new=AsyncMock()) as broadcast, \
         patch("app.services.push_notification.fire_and_forget_notify") as push:
        result = await notification_service.emit_notification("user-1", **kwargs)
    return result, created, broadcast, push


@pytest.mark.asyncio
async def test_notification_persists_all_severities_by_default():
    result, created, broadcast, push = await _emit_with_mocks(
        severity="success", message="Finished"
    )

    created.assert_called_once()
    assert created.call_args.kwargs["surface"] == "notification"
    assert result["surface"] == "notification"
    broadcast.assert_awaited_once_with("user-1", result)
    push.assert_not_called()


@pytest.mark.asyncio
async def test_banner_is_live_only_unless_persistence_is_explicit():
    with patch("app.db.async_database.run_sync", new=AsyncMock()) as run_sync, \
         patch("app.core.sync_engine.sync_engine.broadcast_notification", new=AsyncMock()) as broadcast, \
         patch("app.services.push_notification.fire_and_forget_notify") as push:
        result = await notification_service.emit_notification(
            "user-1", severity="info", message="Reconnect", surface="banner"
        )

    run_sync.assert_not_awaited()
    assert result["surface"] == "banner"
    assert result["persist"] is False
    broadcast.assert_awaited_once_with("user-1", result)
    push.assert_not_called()

    _, created, _, _ = await _emit_with_mocks(
        severity="info", message="Reconnect", surface="banner", persist=True
    )
    created.assert_called_once()
    assert created.call_args.kwargs["surface"] == "banner"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_surface", ["toast", "inline"])
async def test_legacy_surface_normalizes_before_persistence_and_broadcast(legacy_surface):
    result, created, broadcast, _ = await _emit_with_mocks(
        severity="info", message="Legacy", surface=legacy_surface
    )

    assert created.call_args.kwargs["surface"] == "notification"
    assert result["surface"] == "notification"
    broadcast.assert_awaited_once_with("user-1", result)


@pytest.mark.asyncio
async def test_mobile_push_default_and_throttle_are_unchanged():
    with patch("app.db.async_database.run_sync", new=AsyncMock(return_value=None)), \
         patch("app.core.sync_engine.sync_engine.broadcast_notification", new=AsyncMock()), \
         patch("app.services.push_notification.fire_and_forget_notify") as push:
        first = await notification_service.emit_notification(
            "user-1", severity="error", message="Boom", category="api_error"
        )
        second = await notification_service.emit_notification(
            "user-1", severity="error", message="Boom again", category="api_error"
        )

    assert first["persist"] is True
    assert second["persist"] is True
    push.assert_called_once()
    assert push.call_args.kwargs["data"]["type"] == "notification"
