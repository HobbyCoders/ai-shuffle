"""All HTTP legacy inputs use the ordinary settlement boundary, never Stop."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from app.api import sessions as routes
from app.api.auth import require_auth
from app.core.task_registry import task_registry
from app.db import database, turns


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/old/archive", None),
    ("PATCH", "/old/active", {"is_active": True, "archived": True}),
    ("PATCH", "/old?status=archived", None),
])
def test_aliases_return_canonical_state_and_preserve_running_work(mock_db, method, path, payload):
    database.create_profile("agent", "Agent", None, {})
    database.create_session("old", "agent", is_active=True)
    tid = turns.enqueue_turn("old", "user", prompt="ongoing job", profile_id="agent")["id"]
    assert turns.start_turn(tid)
    before = turns.get_turn(tid)
    task_registry.mark_user_stopped("old")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_auth] = lambda: "test"
    with TestClient(app) as client:
        result = client.request(method, "/api/v1/sessions" + path, json=payload)
    assert result.status_code == 200, result.text
    body = result.json()
    canonical = body["session"] if method == "POST" else body
    assert canonical["status"] == "active" and not canonical["is_active"]
    assert "_settled_membership" not in canonical
    assert turns.get_turn(tid) == before
    assert task_registry.is_user_stopped("old")
    assert database.get_session("old")["status"] == "active"
