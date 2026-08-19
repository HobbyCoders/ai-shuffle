"""API coverage for SuperGrok banked usage-limit reset credits.

All upstream calls use ``httpx.MockTransport``. No test reaches the network.
"""

import json
from datetime import datetime

import httpx
import pytest

from app.api import model_providers as model_providers_api
from app.core import (
    grok_rate_limit_resets,
    encryption,
    http_client,
)
from app.core.grok_rate_limit_resets import GROK_TOKEN_AUTH
from app.db import database as db


ROOT = "/api/v1/model-providers"
PROVIDER_ID = "prov-grok-banked-reset"
ACCESS_TOKEN = "grok-banked-reset-access-secret"
REFRESH_TOKEN = "grok-banked-reset-refresh-secret"
GET_PATH = f"{ROOT}/{PROVIDER_ID}/grok/rate-limit-reset-credits"
POST_PATH = f"{GET_PATH}/consume"


@pytest.fixture(autouse=True)
def _async_client_shutdown_compat(monkeypatch):
    """Keep the app's legacy ``AsyncClient.close()`` shutdown seam deterministic."""
    if not hasattr(httpx.AsyncClient, "close"):
        monkeypatch.setattr(
            httpx.AsyncClient, "close", httpx.AsyncClient.aclose, raising=False
        )


def _unlock(mock_db):
    encryption.set_encryption_key("test-password", db)


def _seed_provider(
    *,
    provider_type="grok",
    auth_mode="grok",
    access_token=ACCESS_TOKEN,
    needs_reauth=False,
):
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Grok",
        provider_type=provider_type,
        base_url="https://attacker.invalid/must-not-be-used",
        enabled=True,
    )
    if access_token is not None:
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=encryption.encrypt_value(access_token),
            refresh_token=encryption.encrypt_value(REFRESH_TOKEN),
            account_id=None,
            needs_reauth=needs_reauth,
            auth_mode=auth_mode,
        )
    elif auth_mode != "chatgpt":
        db.update_model_provider(PROVIDER_ID, auth_mode=auth_mode)


def _inventory_payload():
    return {
        "available_count": 1,
        "credits": [
            {
                "id": "credit-1",
                "reset_type": "grok_rate_limits",
                "status": "available",
                "granted_at": "2026-06-17T00:00:00Z",
                "expires_at": "2026-09-13T00:00:00Z",
                "title": "Usage Limit Reset",
                "description": "Ready",
            }
        ],
    }


def _install_transport(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client, "get_http_client", lambda: client)
    return client


def _success_handler(request):
    return httpx.Response(200, json=_inventory_payload())


class TestAdminAndProviderValidation:
    @pytest.mark.parametrize(
        ("method", "path", "json_body"),
        [
            ("get", GET_PATH, None),
            (
                "post",
                POST_PATH,
                {"credit_id": "credit-1", "idempotency_key": "request-1"},
            ),
        ],
    )
    def test_requires_admin(self, client, method, path, json_body):
        if json_body is None:
            response = getattr(client, method)(path)
        else:
            response = getattr(client, method)(path, json=json_body)
        assert response.status_code in (401, 403)

    def test_missing_provider_is_404(self, authenticated_client, mock_db):
        _unlock(mock_db)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 404

    def test_chatgpt_row_on_grok_route_is_400(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _seed_provider(provider_type="chatgpt", auth_mode="chatgpt")
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 400

    def test_xai_api_key_row_is_400(self, authenticated_client, mock_db):
        _unlock(mock_db)
        db.create_model_provider(
            provider_id=PROVIDER_ID,
            name="xAI",
            provider_type="xai",
            base_url="https://api.x.ai/v1",
        )
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("provider_type", "auth_mode"),
        [
            ("grok", "grok"),
            ("custom", "grok"),
        ],
        ids=["canonical-provider-type", "legacy-auth-mode"],
    )
    def test_canonical_and_legacy_classification(
        self,
        authenticated_client,
        mock_db,
        monkeypatch,
        provider_type,
        auth_mode,
    ):
        _unlock(mock_db)
        _seed_provider(provider_type=provider_type, auth_mode=auth_mode)
        _install_transport(monkeypatch, _success_handler)

        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 200, response.text
        assert response.json()["available_count"] == 1
        assert response.json()["credits"][0]["reset_type"] == "grok_rate_limits"

    def test_missing_access_token_is_reconnect_conflict(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider(access_token=None)

        def _must_not_call(request):
            raise AssertionError("missing token must not call upstream")

        _install_transport(monkeypatch, _must_not_call)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Reconnect the Grok provider before using reset credits."
        )

    def test_needs_reauth_is_reconnect_conflict(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider(needs_reauth=True)

        def _must_not_call(request):
            raise AssertionError("needs_reauth must not call upstream")

        _install_transport(monkeypatch, _must_not_call)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 409
        assert "reconnect" in response.json()["detail"].lower()


class TestRequestContract:
    def test_get_uses_primary_url_headers_and_five_second_timeout(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider()
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json=_inventory_payload())

        _install_transport(monkeypatch, handler)
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 200, response.text
        request = captured["request"]
        assert request.method == "GET"
        assert str(request.url) == grok_rate_limit_resets.RESET_CREDITS_URL
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert request.headers["x-xai-token-auth"] == GROK_TOKEN_AUTH
        assert request.headers["accept"] == "application/json"
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }

    def test_post_uses_primary_url_body_headers_and_ten_second_timeout(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider()
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"code": "ok", "windows_reset": 2})

        _install_transport(monkeypatch, handler)
        response = authenticated_client.post(
            POST_PATH,
            json={"credit_id": "credit-7", "idempotency_key": "idem-9"},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"outcome": "reset", "windows_reset": 2}
        request = captured["request"]
        assert request.method == "POST"
        assert str(request.url) == grok_rate_limit_resets.CONSUME_RESET_CREDIT_URL
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert request.headers["x-xai-token-auth"] == GROK_TOKEN_AUTH
        body = json.loads(request.content)
        assert body["credit_id"] == "credit-7"
        assert body["idempotency_key"] == "idem-9"
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }


class TestConsumeHappyPath:
    def test_consume_happy_path(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, json={"code": "reset", "windows_reset": 1}
            ),
        )
        response = authenticated_client.post(
            POST_PATH,
            json={"credit_id": "credit-1", "idempotency_key": "idem-1"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"outcome": "reset", "windows_reset": 1}


class TestUpstreamFailureSanitization:
    def test_upstream_502_is_sanitized(
        self, authenticated_client, mock_db, monkeypatch, caplog
    ):
        _unlock(mock_db)
        _seed_provider()
        raw_body = "RAW-UPSTREAM-SECRET-BODY"

        def handler(request):
            return httpx.Response(503, text=raw_body)

        _install_transport(monkeypatch, handler)
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 502
        assert response.json() == {
            "detail": "The Grok reset-credit service is temporarily unavailable."
        }
        combined = response.text + caplog.text
        assert raw_body not in combined
        assert ACCESS_TOKEN not in combined
        assert REFRESH_TOKEN not in combined
        assert "authorization" not in combined.lower()

    def test_normalizes_inventory_timestamps(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=_inventory_payload())
        )
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 200, response.text
        credit = response.json()["credits"][0]
        assert credit["granted_at"] == int(
            datetime.fromisoformat("2026-06-17T00:00:00+00:00").timestamp()
        )
        assert credit["expires_at"] == int(
            datetime.fromisoformat("2026-09-13T00:00:00+00:00").timestamp()
        )


class TestRefreshOnUnauthorized:
    def test_401_without_refresh_is_reconnect(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(401, text="private unauthorized body"),
        )

        async def no_refresh(provider_id, access_token, client):
            return False

        monkeypatch.setattr(
            model_providers_api, "_try_grok_refresh_on_401", no_refresh
        )
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Reconnect the Grok provider before using reset credits."
        )
        assert "private unauthorized body" not in response.text

    def test_first_401_refreshes_and_retries_once(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _seed_provider()
        requests = []
        refresh_calls = []

        def handler(request):
            requests.append(request.headers["authorization"])
            if len(requests) == 1:
                return httpx.Response(401, text="stale token body")
            return httpx.Response(200, json=_inventory_payload())

        mock_client = _install_transport(monkeypatch, handler)

        async def fake_refresh(provider_id, access_token, client):
            refresh_calls.append((provider_id, access_token, client))
            stored = db.get_codex_tokens(provider_id)
            db.set_codex_token_bundle(
                provider_id,
                access_token=encryption.encrypt_value("rotated-access-token"),
                refresh_token=stored["codex_refresh_token"],
                account_id=stored["codex_account_id"],
                needs_reauth=False,
                auth_mode="grok",
            )
            return True

        monkeypatch.setattr(
            model_providers_api, "_try_grok_refresh_on_401", fake_refresh
        )

        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 200, response.text
        assert requests == [
            f"Bearer {ACCESS_TOKEN}",
            "Bearer rotated-access-token",
        ]
        assert refresh_calls == [(PROVIDER_ID, ACCESS_TOKEN, mock_client)]
