"""``exclude_archived`` on the keyset session list (sidebar "Load older sessions").

The sidebar never shows archived chats (they live in Settings), but it used to
page through them: ``has_more`` counted archived rows, so "Load older sessions"
kept appearing and each click fetched a page that added nothing visible. The
filter is part of the shared WHERE clause, so the limit+1 ``has_more`` probe
and the cursor both skip archived rows.
"""

import uuid

from app.db import database


def _mk(conn, updated_at: str, status: str | None = None) -> str:
    session_id = str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default")
    if status is not None:
        database.update_session(session_id, status=status)
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (updated_at, session_id)
    )
    conn.commit()
    return session_id


def test_has_more_ignores_archived_rows_when_excluded(mock_db):
    conn = mock_db
    newest = _mk(conn, "2026-10-03T12:00:00")
    _mk(conn, "2026-10-02T12:00:00", status="archived")
    _mk(conn, "2026-10-01T12:00:00", status="archived")

    rows, next_cursor, next_cursor_id, has_more = database.get_sessions_cursor(
        limit=1, exclude_archived=True
    )
    assert [s["id"] for s in rows] == [newest]
    assert has_more is False
    assert next_cursor is None and next_cursor_id is None

    # Default stays unchanged: Settings' archived list and API callers that
    # want every row still page through archived sessions.
    rows, _, _, has_more = database.get_sessions_cursor(limit=1)
    assert [s["id"] for s in rows] == [newest]
    assert has_more is True


def test_cursor_skips_archived_rows_between_visible_pages(mock_db):
    conn = mock_db
    first = _mk(conn, "2026-10-03T12:00:00")
    _mk(conn, "2026-10-02T12:00:00", status="archived")
    second = _mk(conn, "2026-10-01T12:00:00")

    rows, cursor, cursor_id, has_more = database.get_sessions_cursor(
        limit=1, exclude_archived=True
    )
    assert [s["id"] for s in rows] == [first]
    assert has_more is True

    rows, _, _, has_more = database.get_sessions_cursor(
        limit=1, exclude_archived=True, cursor=cursor, cursor_id=cursor_id
    )
    assert [s["id"] for s in rows] == [second]
    assert has_more is False


def test_null_status_is_not_treated_as_archived(mock_db):
    conn = mock_db
    legacy = _mk(conn, "2026-10-03T12:00:00")
    conn.execute("UPDATE sessions SET status = NULL WHERE id = ?", (legacy,))
    conn.commit()

    rows, *_ = database.get_sessions_cursor(exclude_archived=True)
    assert legacy in {s["id"] for s in rows}


def test_cursor_endpoint_forwards_exclude_archived():
    """The sidebar's request shape: the endpoint passes the flag to the query,
    and leaves it off when the caller does not ask (Settings' archived list)."""
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import sessions as sessions_module
    from app.api.auth import require_auth

    with (
        patch("app.api.sessions.database") as mock_db,
        patch("app.api.sessions_helpers.database", mock_db),
        patch("app.api.sessions.get_api_user_from_request", return_value=None),
        patch("app.api.sessions_helpers.get_api_user_from_request", return_value=None),
    ):
        mock_db.get_sessions_cursor.return_value = ([], None, None, False)
        mock_db.get_installs_map.return_value = {}
        app = FastAPI()
        app.include_router(sessions_module.router)
        app.dependency_overrides[require_auth] = lambda: "test-token"
        base = "/api/v1/sessions/cursor?admin_only=true&sync_claude_sessions=false"
        with TestClient(app) as client:
            assert client.get(f"{base}&exclude_archived=true").status_code == 200
            assert mock_db.get_sessions_cursor.call_args.kwargs["exclude_archived"] is True
            assert client.get(base).status_code == 200
            assert mock_db.get_sessions_cursor.call_args.kwargs["exclude_archived"] is False
