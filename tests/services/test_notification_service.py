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
async def test_stalled_incident_concurrent_updates_preserve_read_state_and_do_not_repush(mock_db):
    import asyncio
    import threading
    from contextlib import contextmanager
    from app.db import database, notifications

    # pg_shim strips FOR UPDATE and shares ONE SQLite connection across threads.
    # Supply the transaction exclusion that PostgreSQL's session-row lock owns;
    # keep the actual notification reads/writes and all count assertions real.
    get_db = notifications.get_db
    lock = threading.RLock()
    @contextmanager
    def locked_db():
        with lock, get_db() as conn:
            yield conn

    database.create_session("notice-race", "default")
    with patch.object(notifications, "get_db", new=locked_db), \
         patch("app.core.sync_engine.sync_engine.broadcast_notification", new=AsyncMock()) as broadcast, \
         patch("app.services.push_notification.fire_and_forget_notify") as push:
        first = await notification_service.emit_notification(
            "admin", severity="error", category="wake_failed", session_id="notice-race", message="missing cwd",
        )
        assert notifications.mark_read("admin", first["id"])
        # Defeat the ordinary 60s push throttle: incident coalescing must itself
        # prevent another push even long after the first error.
        notification_service._last_push.clear()
        updates = await asyncio.gather(*[
            notification_service.emit_notification(
                "admin", severity="error", category="wake_failed", session_id="notice-race", message=f"retry {i}",
            ) for i in range(8)
        ])
    assert {row["id"] for row in updates} == {first["id"]}
    assert all(row["read"] for row in updates)
    assert notifications.get_notification("admin", first["id"])["occurrence_count"] == 9
    assert notifications.unread_count("admin") == 0
    assert len(notifications.list_notifications("admin")) == 1
    assert broadcast.await_count == 9
    push.assert_called_once()


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
