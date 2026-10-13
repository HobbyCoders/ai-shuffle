from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.api.notifications import NotificationCreate, create_notification


def test_create_model_defaults_to_notification_and_accepts_legacy_values():
    assert NotificationCreate().surface == "notification"
    assert NotificationCreate(surface="toast").surface == "toast"
    assert NotificationCreate(surface="inline").surface == "inline"
    with pytest.raises(ValidationError):
        NotificationCreate(surface="modal")


@pytest.mark.asyncio
async def test_create_route_forwards_serialized_action_and_user_scope():
    body = NotificationCreate(
        id="n1",
        severity="warning",
        surface="toast",
        message="Needs attention",
        session_id="session-1",
        action={"kind": "open_session", "payload": {"session_id": "session-1"}},
    )
    expected = {"id": "n1", "surface": "notification"}

    with patch(
        "app.api.notifications.notifications_db.create_notification",
        return_value=expected,
    ) as create:
        result = await create_notification(body, user_id="user-1")

    assert result == expected
    create.assert_called_once_with(
        "user-1",
        severity="warning",
        message="Needs attention",
        surface="toast",
        category=None,
        source="client",
        title=None,
        session_id="session-1",
        action_kind="open_session",
        action_payload={"session_id": "session-1"},
        notification_id="n1",
    )
