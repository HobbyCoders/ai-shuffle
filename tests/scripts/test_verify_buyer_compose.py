"""Buyer acceptance must reach the credential gate, not a missing-agent error."""
import io
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.ci import verify_buyer_compose as buyer
from app.core import credential_gate

pytestmark = pytest.mark.real_agent_gate


class Response(io.BytesIO):
    def __init__(self, status, content):
        super().__init__(content)
        self.status = status


def stream(*events):
    return Response(200, b"".join(
        ("data: " + json.dumps(event) + "\n\n").encode() for event in events
    ))


class ClientApi(buyer.Api):
    """Run the script's HTTP/JSON/SSE consumer against the real app routes."""
    def __init__(self, client):
        self.client = client

    def open(self, method, path, body=None, bearer=None, timeout=30):
        response = self.client.request(
            method, path, json=body,
            headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        )
        return Response(response.status_code, response.content)


@pytest.fixture
def fresh_api(bare_db, monkeypatch, _api_unit_credentials):
    from app.api import agents, api_users, query
    from app.core import assistant_service, claude_auth_mode, claude_cli_status, owner_auth, profiles
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.db import database
    from app.db.base import get_db

    # Run the producing startup paths, not a fixture that pretends the list is empty.
    profiles.run_migrations()
    seed_builtin_subagents()
    with get_db() as conn:
        assistant_service.ensure_agent(conn.cursor())
    assert database.get_all_profiles() == []  # internal assistant is not selectable

    # Only the external CLI probe / worker mode are pinned; the gate itself runs.
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: "subscription")
    monkeypatch.setattr(claude_cli_status, "get_status", lambda: {"installed": True, "signed_in": False})
    app = FastAPI()
    for router in (agents.router, api_users.router, query.router):
        app.include_router(router)

    @app.middleware("http")
    async def local_listener(request, call_next):
        request.scope["shuffle.ingress"] = "local"
        return await call_next(request)

    token, _ = owner_auth.issue_session()
    with TestClient(app) as client:
        yield ClientApi(client), token


def test_fresh_owner_list_and_real_credential_refusal(fresh_api):
    from app.db import database

    api, token = fresh_api
    assert api.call("GET", "/api/v1/agents", bearer="invalid-owner-token")[0] == 401
    assert api.call("GET", "/api/v1/agents", bearer=token) == (200, [])
    assert credential_gate.ready_providers() == []
    report = buyer.check_turn_refused(api, token)
    assert credential_gate.NOT_SIGNED_IN_MESSAGE in report
    status, profiles = api.call("GET", "/api/v1/agents", bearer=token)
    assert status == 200 and len(profiles) == 1
    assert profiles[0]["id"] == "buyer-install-check"
    assert database.get_profile(profiles[0]["id"]) is not None
    assert database.get_profile("default") is None

    # A subsequent run reuses the real agent, rather than creating another.
    assert credential_gate.NOT_SIGNED_IN_MESSAGE in buyer.check_turn_refused(api, token)
    assert len(database.get_all_profiles()) == 1


def test_old_fallback_reproduces_missing_profile_stream(fresh_api):
    api, token = fresh_api
    status, user = api.call("POST", "/api/v1/api-users", {"name": "regression"}, bearer=token)
    assert status == 201
    with api.open("POST", "/api/v1/conversation/stream",
                  {"prompt": "ready", "profile": "default"}, bearer=user["api_key"]) as response:
        assert response.status == 200
        with pytest.raises(AssertionError, match="profile was not found: Profile not found: default"):
            buyer.stream_refusal(response, float("inf"))


def fake_api(profiles=(200, [{"id": "existing-agent"}]), created=None, response=None):
    api = Mock()
    replies = [profiles]
    if profiles == (200, []):
        replies.append(created)
    replies.append((201, {"api_key": "test-api-key"}))
    api.call.side_effect = replies
    api.open.return_value = response if response is not None else stream(
        {"type": "error", "message": credential_gate.NO_PROVIDER_MESSAGE})
    return api


def test_uses_created_id_and_owner_bearer():
    api = fake_api((200, []), (201, {"id": "server-allocated-id"}))
    assert credential_gate.NO_PROVIDER_MESSAGE in buyer.check_turn_refused(api, "owner-token")
    assert api.call.call_args_list[1].args == (
        "POST", "/api/v1/agents", {"name": "buyer-install-check", "config": {}})
    assert all(call.kwargs["bearer"] == "owner-token" for call in api.call.call_args_list)
    assert api.open.call_args.args[2]["profile"] == "server-allocated-id"
    assert api.open.call_args.kwargs["bearer"] == "test-api-key"


@pytest.mark.parametrize("listing", [(401, {"detail": "Not authenticated"}),
                                     (500, "failed"), (200, {"agents": []})])
def test_invalid_listing_fails_before_creating_anything(listing):
    api = fake_api(listing)
    with pytest.raises(AssertionError, match="listing agents answered"):
        buyer.check_turn_refused(api, "owner-token")
    assert api.call.call_count == 1
    api.open.assert_not_called()


@pytest.mark.parametrize("created", [(403, {"detail": "Forbidden"}), (201, {}), (201, {"id": ""})])
def test_failed_creation_never_starts_turn(created):
    api = fake_api((200, []), created)
    with pytest.raises(AssertionError, match="creating the check's agent|no usable id"):
        buyer.check_turn_refused(api, "owner-token")
    assert api.call.call_count == 2
    api.open.assert_not_called()


@pytest.mark.parametrize("selected", [{}, {"id": None}, {"id": 1}, {"id": " "}, "bad"])
def test_malformed_agent_never_starts_turn(selected):
    api = fake_api((200, [selected]))
    with pytest.raises(AssertionError, match="no usable id"):
        buyer.check_turn_refused(api, "owner-token")
    api.open.assert_not_called()


@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize("message,expected", [
    ("Profile not found: default", "profile was not found"),
    ("Profile not found: settings", "profile was not found"),
    ("API key required", "API key was refused"),
    ("database unavailable", "not with a setup message"),
])
def test_unrelated_refusals_are_not_credential_success(status, message, expected):
    response = (stream({"type": "error", "message": message}) if status == 200 else
                Response(status, json.dumps({"detail": message}).encode()))
    with pytest.raises(AssertionError, match=expected):
        buyer.check_turn_refused(fake_api(response=response), "owner-token")


@pytest.mark.parametrize("status", [200, 503])
def test_setup_refusal_in_http_or_stream(status):
    message = credential_gate.NO_PROVIDER_MESSAGE
    response = (stream({"type": "error", "message": message}) if status == 200 else
                Response(status, json.dumps({"detail": message}).encode()))
    assert message in buyer.check_turn_refused(fake_api(response=response), "owner-token")


def test_successful_turn_is_still_a_failure():
    response = stream({"type": "text", "text": "ready"}, {"type": "done"})
    with pytest.raises(AssertionError, match="started without any credential"):
        buyer.check_turn_refused(fake_api(response=response), "owner-token")
