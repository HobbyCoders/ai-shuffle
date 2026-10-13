from datetime import datetime, timezone

import pytest

from app.db import notifications


def test_legacy_rows_normalize_on_read_without_rewriting_storage(mock_db):
    created_at = datetime.now(timezone.utc).isoformat()
    for notification_id, surface in (("old-toast", "toast"), ("old-inline", "inline")):
        mock_db.execute(
            """INSERT INTO notifications
               (id, user_id, severity, surface, source, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (notification_id, "user-1", "info", surface, "client", surface, created_at),
        )
    mock_db.commit()

    rows = notifications.list_notifications("user-1", include_dismissed=True)

    assert {row["surface"] for row in rows} == {"notification"}
    stored = mock_db.execute(
        "SELECT surface FROM notifications ORDER BY id"
    ).fetchall()
    assert [row[0] for row in stored] == ["inline", "toast"]


@pytest.mark.parametrize("surface", [None, "notification", "toast", "inline", "unknown"])
def test_surface_normalization(surface):
    assert notifications.normalize_surface(surface) == "notification"


def test_banner_surface_is_preserved():
    assert notifications.normalize_surface("banner") == "banner"


def test_schema_default_is_notification(mock_db):
    mock_db.execute(
        "INSERT INTO notifications (id, user_id, message) VALUES (?, ?, ?)",
        ("default-surface", "user-1", "Defaulted"),
    )
    mock_db.commit()

    surface = mock_db.execute(
        "SELECT surface FROM notifications WHERE id = ?", ("default-surface",)
    ).fetchone()[0]
    assert surface == "notification"


def test_stalled_notices_coalesce_per_owner_session_until_dismissed(mock_db):
    from app.db import database
    database.create_session("stalled", "default")
    first = notifications.create_notification("admin", message="first", session_id="stalled", category="wake_failed")
    second = notifications.create_notification("admin", message="new reason", session_id="stalled", category="wake_failed")
    assert second["id"] == first["id"]
    assert second["message"] == "new reason"
    assert second["occurrence_count"] == 2
    other_owner = notifications.create_notification("other", message="other", session_id="stalled", category="wake_failed")
    assert other_owner["id"] != first["id"]
    notifications.dismiss("admin", first["id"])
    recovered = notifications.create_notification("admin", message="new stall", session_id="stalled", category="wake_failed")
    assert recovered["id"] != first["id"]
    assert recovered["occurrence_count"] == 1


def test_create_defaults_to_notification_and_preserves_action_session_and_scope(mock_db):
    created = notifications.create_notification(
        "user-1",
        message="Fix it",
        notification_id="same-id",
        action_kind="fix_with_ai",
        action_payload={"status_code": 500},
        session_id="session-1",
    )
    duplicate = notifications.create_notification(
        "user-1", message="Must not replace", notification_id="same-id"
    )

    assert created["surface"] == "notification"
    assert created["action"] == {
        "kind": "fix_with_ai", "payload": {"status_code": 500}
    }
    assert created["session_id"] == "session-1"
    assert duplicate["message"] == "Fix it"
    assert notifications.get_notification("user-1", "same-id") is not None
    assert notifications.get_notification("user-2", "same-id") is None

    stored_surface = mock_db.execute(
        "SELECT surface FROM notifications WHERE id = ?", ("same-id",)
    ).fetchone()[0]
    assert stored_surface == "notification"
