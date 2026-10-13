"""Per-request bindings through the real generation HTTP and options paths.

Only credential/environment resolution and the provider transport are fake;
no provider request or billing takes place.
"""
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import generation
from app.core import generation_options
from app.db import database
from tests.api.test_generation import FakeAdapter, text_turn


@pytest.mark.parametrize("kind", ["profile", "subagent"])
@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize("selection,expected_model,expected_provider,expected_effort", [
    (None, "luna", "cheap", "low"),
    ("opus", "opus", None, None),
    ("prov:strong:astra", "astra", "strong", None),
    ("prov:strong:vendor/model:revision", "vendor/model:revision", "strong", None),
])
def test_request_binding_reaches_adapter_without_mutating_settings(
    mock_db, kind, continued, selection, expected_model, expected_provider, expected_effort
):
    database.set_system_setting("text_generation_model", "prov:cheap:luna")
    database.set_system_setting("text_generation_effort", "low")
    adapter = FakeAdapter(text_turn('{"id":"reviewer","name":"Reviewer","description":"Reviews security","prompt":"Review code","model":"sonnet"}'))
    app = FastAPI()
    app.include_router(generation.router)
    app.dependency_overrides[generation.require_auth] = lambda: "test"
    body = {"description": "Review code for security vulnerabilities"}
    if selection:
        body["model"] = selection
    if continued:
        body.update(generation_type=kind, answers={"scope": "Changed files"})
        endpoint = "continue"
    else:
        body["skip_clarification"] = True
        endpoint = kind

    with patch.object(generation, "make_adapter", return_value=adapter), \
         patch.object(generation_options, "_resolve_env", return_value={}) as resolve_env, \
         patch.object(database, "get_model_provider", return_value={"provider_type": "openai", "auth_mode": "api_key"}):
        with TestClient(app) as client:
            response = client.post(f"/api/v1/generate/{endpoint}", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "complete"
    assert adapter.options.model == expected_model
    assert adapter.options.aih_provider_id == expected_provider
    assert getattr(adapter.options, "effort", None) == expected_effort
    assert resolve_env.call_args.args[2] == expected_provider
    assert adapter.disconnect_calls == 1
    if kind == "subagent":
        # The model that authors the prompt is not the delegate's assignment.
        assert response.json()["result"]["subagent"]["model"] == "sonnet"
    assert database.get_system_setting("text_generation_model") == "prov:cheap:luna"
    assert database.get_system_setting("text_generation_effort") == "low"


def test_selected_unavailable_provider_is_refused_without_default_fallback(mock_db):
    from app.core.environment import ProviderConfigError

    database.set_system_setting("text_generation_model", "haiku")
    app = FastAPI()
    app.include_router(generation.router)
    app.dependency_overrides[generation.require_auth] = lambda: "test"
    with patch.object(generation_options, "_resolve_env", side_effect=ProviderConfigError("Selected provider is disabled")), \
         patch.object(generation, "make_adapter") as adapter:
        with TestClient(app) as client:
            response = client.post("/api/v1/generate/profile", json={
                "description": "Review security vulnerabilities", "skip_clarification": True,
                "model": "prov:disabled:astra",
            })
    assert response.status_code == 400
    assert response.json()["detail"] == "Selected provider is disabled"
    adapter.assert_not_called()
