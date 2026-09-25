"""Contract tests for the live voice-session context endpoint."""

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import voice_context
from app.api.auth import require_auth
from app.db import database as db


PATH = "/api/v1/voice/sessions/{}/context"


@pytest.fixture
def unauthenticated_context_client(mock_db):
    app = FastAPI()
    app.include_router(voice_context.router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def context_client(mock_db):
    app = FastAPI()
    app.include_router(voice_context.router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    with TestClient(app) as client:
        yield client


def _session(session_id="voice-1", **overrides):
    values = {
        "session_id": session_id,
        "profile_id": "default",
        "title": "Voice session",
        "mode": "voice",
        "provider_id": "realtime-provider",
        "model": "sonnet",
        "effort": "medium",
        "permission_mode": "default",
        "context_window": 200_000,
        "fast": False,
    }
    values.update(overrides)
    db.create_session(**values)
    return session_id


@contextmanager
def _voice_binding_helper(monkeypatch):
    """Keep model validation local while exercising real binding persistence."""
    class Verdict:
        ok = True
        reason = None

    monkeypatch.setattr(
        "app.core.provider_agent.validate_provider_model",
        lambda provider_id, model_id, require_servable=False: Verdict(),
    )
    yield


def test_requires_auth(unauthenticated_context_client):
    response = unauthenticated_context_client.patch(PATH.format("missing"), json={})
    assert response.status_code in (401, 403)


def test_missing_and_non_voice_sessions_are_not_voice_resources(context_client):
    missing = context_client.patch(PATH.format("missing"), json={})
    assert missing.status_code == 404

    db.create_session(session_id="chat-1", profile_id="default", mode="code")
    chat = context_client.patch(PATH.format("chat-1"), json={})
    assert chat.status_code == 404
    assert chat.json()["detail"] == "Voice session not found."


def test_sparse_patch_roundtrips_all_context_and_preserves_realtime_provider(
    context_client, monkeypatch
):
    session_id = _session()
    with _voice_binding_helper(monkeypatch):
        response = context_client.patch(
            PATH.format(session_id),
            json={
                "model": "prov:worker-provider:openai/gpt-5.6",
                "effort": "high",
                "permission_mode": "bypassPermissions",
                "context_window": 400_000,
                "fast": True,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": session_id,
        "model": "prov:worker-provider:openai/gpt-5.6",
        "effort": "high",
        "permission_mode": "bypassPermissions",
        "context_window": 400_000,
        "fast": True,
    }
    row = db.get_session(session_id)
    assert row["provider_id"] == "realtime-provider"
    assert row["model"] == "prov:worker-provider:openai/gpt-5.6"


def test_sparse_patch_leaves_omitted_fields_unchanged(context_client, monkeypatch):
    session_id = _session(fast=True)
    with _voice_binding_helper(monkeypatch):
        response = context_client.patch(
            PATH.format(session_id), json={"permission_mode": "auto", "fast": False}
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": session_id,
        "model": "sonnet",
        "effort": "medium",
        "permission_mode": "auto",
        "context_window": 200_000,
        "fast": False,
    }


def test_explicit_null_clears_pins_without_rebinding_realtime(context_client):
    session_id = _session(fast=True, context_window=1_000_000)
    fields = ("model", "effort", "permission_mode", "context_window", "fast")
    response = context_client.patch(PATH.format(session_id), json=dict.fromkeys(fields))
    assert response.status_code == 200, response.text
    assert all(response.json()[field] is None for field in fields)
    assert db.get_session(session_id)["provider_id"] == "realtime-provider"


def test_ordinary_turn_binding_none_still_preserves_existing_pins(mock_db):
    session_id = _session(fast=True)
    db.set_session_run_binding(session_id, effort=None, context_window=None, fast=None)
    row = db.get_session(session_id)
    assert row["effort"] == "medium"
    assert row["context_window"] == 200_000
    assert row["fast"]


@pytest.mark.parametrize(
    "payload,detail",
    [
        ({"model": "   "}, "model must not be empty"),
        ({"model": "prov:broken"}, "valid provider/model"),
        ({"effort": "extreme"}, "effort must be one of"),
        ({"permission_mode": "root"}, "permission_mode must be one of"),
    ],
)
def test_rejects_invalid_query_settings(context_client, payload, detail):
    session_id = _session()
    response = context_client.patch(PATH.format(session_id), json=payload)
    assert response.status_code == 400
    assert detail in response.json()["detail"]


def test_context_window_uses_normal_positive_integer_validation(context_client):
    session_id = _session()
    for value in (0, -1):
        response = context_client.patch(
            PATH.format(session_id), json={"context_window": value}
        )
        assert response.status_code == 422


def test_provider_model_is_validated_before_persistence(
    context_client, monkeypatch
):
    session_id = _session()

    class Verdict:
        ok = False
        reason = "worker model is disabled"

    monkeypatch.setattr(
        "app.core.provider_agent.validate_provider_model",
        lambda provider_id, model_id, require_servable=False: Verdict(),
    )
    response = context_client.patch(
        PATH.format(session_id),
        json={"model": "prov:worker-provider:openai/gpt-5.6"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "worker model is disabled"
    assert db.get_session(session_id)["model"] == "sonnet"


def test_api_user_session_access_is_enforced(context_client, monkeypatch):
    session_id = _session()
    monkeypatch.setattr(
        "app.api.sessions_helpers.get_api_user_from_request",
        lambda request: {"id": "user-b", "profile_ids": ["other-profile"]},
    )
    response = context_client.patch(PATH.format(session_id), json={"fast": True})
    assert response.status_code == 403
    assert bool(db.get_session(session_id)["fast"]) is False
