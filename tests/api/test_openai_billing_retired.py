"""OpenAI API billing has no routes; subscription and other billing routes remain."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.model_providers import router


@pytest.fixture
def billing_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("method,path", [
    ("GET", "openai-usage"), ("PUT", "openai-usage"), ("DELETE", "openai-usage"),
    ("PUT", "openai-usage/balance"), ("DELETE", "openai-usage/balance"),
    ("GET", "openai-billing/status"), ("POST", "openai-billing/start"),
    ("POST", "balance-access/sign-in"),
])
def test_retired_routes_are_not_found(billing_client, method, path):
    response = billing_client.request(method, "/api/v1/model-providers/old-openai/" + path)
    assert response.status_code in (404, 405)


@pytest.mark.parametrize("path", ["openai-billing/browser", "balance-access/browser"])
def test_retired_browser_websockets_are_not_registered(billing_client, path):
    with pytest.raises(WebSocketDisconnect) as error:
        with billing_client.websocket_connect("/api/v1/model-providers/old-openai/" + path):
            pytest.fail("A removed billing websocket accepted a connection")
    assert error.value.code == 1000  # Starlette's unmatched-websocket close, not an endpoint.


def test_other_billing_and_subscription_routes_remain(billing_client):
    paths = set(billing_client.app.openapi()["paths"])
    assert not any("openai-usage" in path or "openai-billing" in path for path in paths)
    assert "/api/v1/model-providers/{provider_id}/balance-access" in paths
    assert "/api/v1/model-providers/{provider_id}/openrouter-usage" in paths
    assert any("codex" in path for path in paths)


def test_provider_operations_share_the_neutral_lock():
    from app.api import model_providers, provider_balances
    from app.core.provider_locks import operation_lock

    assert model_providers.operation_lock is provider_balances.operation_lock is operation_lock
    assert operation_lock("provider-a") is operation_lock("provider-a")
    assert operation_lock("provider-a") is not operation_lock("provider-b")
