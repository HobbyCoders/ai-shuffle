"""Settled pages and cheap counts share visibility and membership boundaries."""
from unittest.mock import patch

from app.db import database


def test_settled_count_and_keysets_share_scopes(mock_db):
    def create(sid, **kw):
        database.create_session(session_id=sid, profile_id="default", **kw)
        mock_db.execute("UPDATE sessions SET updated_at = ?, instance_id = ? WHERE id = ?",
                        ("2026-10-01T12:00:00", "this-worker", sid))
        mock_db.commit()

    create("a")
    create("b")
    create("c", is_active=True)
    create("d", hidden=True)
    create("e", is_worker=True)
    create("f")
    database.update_session("f", status="archived")
    create("g")
    mock_db.execute("UPDATE sessions SET instance_id = ? WHERE id = ?", ("other-worker", "g"))
    mock_db.commit()
    scope = dict(membership="settled", api_user_id="", device_id="this-worker", exclude_archived=True)
    assert database.count_sessions(**scope) == 2
    first, cursor, cursor_id, more = database.get_sessions_cursor(limit=1, **scope)
    second, _, _, more2 = database.get_sessions_cursor(limit=1, cursor=cursor, cursor_id=cursor_id, **scope)
    assert [s["id"] for s in first + second] == ["b", "a"]
    assert more and not more2
    assert database.count_sessions(membership="active", exclude_archived=True) == 1
    assert database.get_session("d")["hidden"]  # retained, not deleted


def test_count_and_cursor_force_api_user_scope():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import sessions as routes
    from app.api.auth import require_auth

    user = {"id": "limited", "project_id": "permitted", "profile_id": "agent"}
    with patch("app.api.sessions.database") as db, \
            patch("app.api.sessions_helpers.get_api_user_from_request", return_value=user), \
            patch("app.api.sessions.fetch_and_enrich_sessions", return_value=([], None, None, False)):
        db.count_sessions.return_value = 4
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[require_auth] = lambda: "test"
        with TestClient(app) as client:
            query = "?membership=settled&admin_only=true&project_id=forbidden&api_user_id=someone&device_id=worker"
            assert client.get("/api/v1/sessions/count" + query).json() == {"total": 4}
            kwargs = db.count_sessions.call_args.kwargs
            assert kwargs["api_user_id"] == "limited" and kwargs["project_id"] == "permitted"
            assert kwargs["membership"] == "settled" and kwargs["device_id"] == "worker"
            assert client.get("/api/v1/sessions/count?membership=archived").status_code == 422
            assert client.get("/api/v1/sessions/cursor" + query + "&sync_claude_sessions=false").status_code == 200
            kwargs2 = routes.fetch_and_enrich_sessions.call_args.kwargs
            for key in ("api_user_id", "project_id", "profile_id", "membership", "device_id"):
                assert kwargs2[key] == kwargs[key]
