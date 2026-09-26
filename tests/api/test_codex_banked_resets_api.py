"""API coverage for ChatGPT/Codex banked rate-limit reset credits.

All upstream calls use ``httpx.MockTransport``. No test reaches the network.
"""

import json
from datetime import datetime

import httpx
import pytest

from app.core import (
    codex_rate_limit_resets,
    codex_token_refresh,
    encryption,
    http_client,
)
from app.core.codex_rate_limit_resets import CODEX_USER_AGENT
from app.db import database as db
from tests.helpers import unlock_encryption


ROOT = "/api/v1/model-providers"
PROVIDER_ID = "prov-banked-reset"
ACCESS_TOKEN = "banked-reset-access-secret"
REFRESH_TOKEN = "banked-reset-refresh-secret"
ACCOUNT_ID = "acct-banked-reset"
GET_PATH = f"{ROOT}/{PROVIDER_ID}/codex/rate-limit-reset-credits"
POST_PATH = f"{GET_PATH}/consume"


@pytest.fixture(autouse=True)
def _async_client_shutdown_compat(monkeypatch):
    """Keep the app's legacy ``AsyncClient.close()`` shutdown seam deterministic."""
    if not hasattr(httpx.AsyncClient, "close"):
        monkeypatch.setattr(
            httpx.AsyncClient, "close", httpx.AsyncClient.aclose, raising=False
        )


def _seed_provider(
    *,
    provider_type="chatgpt",
    auth_mode="chatgpt",
    access_token=ACCESS_TOKEN,
    account_id=ACCOUNT_ID,
    needs_reauth=False,
):
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="Codex",
        provider_type=provider_type,
        base_url="https://attacker.invalid/must-not-be-used",
        enabled=True,
    )
    if access_token is not None:
        db.set_codex_token_bundle(
            PROVIDER_ID,
            access_token=encryption.encrypt_value(access_token),
            refresh_token=encryption.encrypt_value(REFRESH_TOKEN),
            account_id=account_id,
            needs_reauth=needs_reauth,
        )
    if auth_mode != "chatgpt":
        db.update_model_provider(PROVIDER_ID, auth_mode=auth_mode)
    elif access_token is None:
        db.update_model_provider(PROVIDER_ID, auth_mode="chatgpt")


def _inventory_payload():
    return {
        "available_count": 1,
        "credits": [
            {
                "id": "credit-1",
                "reset_type": "codex_rate_limits",
                "status": "available",
                "granted_at": "2026-06-17T00:00:00Z",
                "expires_at": "2026-07-17T01:02:03+01:00",
                "title": "Full reset",
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
        unlock_encryption(mock_db)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 404

    def test_wrong_provider_kind_is_400(self, authenticated_client, mock_db):
        unlock_encryption(mock_db)
        db.create_model_provider(
            provider_id=PROVIDER_ID,
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
        )
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("provider_type", "auth_mode"),
        [
            ("chatgpt", None),
            ("custom", "chatgpt"),
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
        unlock_encryption(mock_db)
        _seed_provider(provider_type=provider_type, auth_mode=auth_mode)
        _install_transport(monkeypatch, _success_handler)

        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 200, response.text
        assert response.json()["available_count"] == 1

    def test_encryption_locked_is_400_before_network(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        encryption.clear_encryption_key()

        def _must_not_call(request):
            raise AssertionError("locked encryption must not call upstream")

        _install_transport(monkeypatch, _must_not_call)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 400

    def test_missing_access_token_is_reconnect_conflict(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider(access_token=None)

        def _must_not_call(request):
            raise AssertionError("missing token must not call upstream")

        _install_transport(monkeypatch, _must_not_call)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 409
        assert "reconnect" in response.json()["detail"].lower()

    def test_needs_reauth_is_reconnect_conflict(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider(needs_reauth=True)

        def _must_not_call(request):
            raise AssertionError("needs_reauth must not call upstream")

        _install_transport(monkeypatch, _must_not_call)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 409
        assert "reconnect" in response.json()["detail"].lower()


class TestRequestContract:
    def test_get_uses_fixed_url_headers_and_five_second_timeout(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
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
        assert str(request.url) == codex_rate_limit_resets.RESET_CREDITS_URL
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert request.headers["chatgpt-account-id"] == ACCOUNT_ID
        assert request.headers["user-agent"] == CODEX_USER_AGENT
        assert "content-type" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }

    def test_get_omits_empty_account_header(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider(account_id="")
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            return httpx.Response(200, json=_inventory_payload())

        _install_transport(monkeypatch, handler)
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 200, response.text
        assert "chatgpt-account-id" not in captured["headers"]

    def test_post_uses_fixed_url_exact_body_headers_and_ten_second_timeout(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"code": "reset", "windows_reset": 2})

        _install_transport(monkeypatch, handler)
        response = authenticated_client.post(
            POST_PATH,
            json={"credit_id": "credit-7", "idempotency_key": "idem-9"},
        )

        assert response.status_code == 200, response.text
        request = captured["request"]
        assert request.method == "POST"
        assert str(request.url) == codex_rate_limit_resets.CONSUME_RESET_CREDIT_URL
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert request.headers["chatgpt-account-id"] == ACCOUNT_ID
        assert request.headers["user-agent"] == CODEX_USER_AGENT
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "credit_id": "credit-7",
            "redeem_request_id": "idem-9",
        }
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }

    @pytest.mark.parametrize(
        "body",
        [
            {"credit_id": "", "idempotency_key": "idem"},
            {"credit_id": "   ", "idempotency_key": "idem"},
            {"credit_id": "credit", "idempotency_key": ""},
            {"credit_id": "credit", "idempotency_key": "\t"},
            {"credit_id": "credit"},
            {"idempotency_key": "idem"},
            {
                "credit_id": "credit",
                "idempotency_key": "idem",
                "extra": "forbidden",
            },
        ],
    )
    def test_post_rejects_nonexact_or_blank_body(
        self, authenticated_client, body
    ):
        response = authenticated_client.post(POST_PATH, json=body)
        assert response.status_code == 422


class TestInventoryNormalization:
    def test_normalizes_subset_order_unknowns_optionals_and_timestamps(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        payload = {
            "available_count": 3,
            "credits": [
                {
                    "id": "first",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00.999Z",
                    "expires_at": "2026-07-17T01:02:03+01:00",
                    "title": "First",
                    "description": "One",
                    "profile_image_url": "ignored",
                },
                {
                    "id": "second",
                    "reset_type": "future_reset_type",
                    "status": "future_status",
                    "granted_at": "2026-06-18T00:00:00-04:00",
                    "expires_at": None,
                    "ignored": {"nested": "field"},
                },
                {
                    "id": "third",
                    "reset_type": "codex_rate_limits",
                    "status": "redeeming",
                    "granted_at": "2026-06-19T00:00:00Z",
                    "title": None,
                    "description": None,
                },
                {
                    "id": "fourth",
                    "reset_type": "codex_rate_limits",
                    "status": "redeemed",
                    "granted_at": "2026-06-20T00:00:00Z",
                },
            ],
            "total_earned_count": 99,
        }

        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=payload)
        )
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "available_count": 3,
            "credits": [
                {
                    "id": "first",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": int(
                        datetime.fromisoformat(
                            "2026-06-17T00:00:00.999+00:00"
                        ).timestamp()
                    ),
                    "expires_at": int(
                        datetime.fromisoformat(
                            "2026-07-17T01:02:03+01:00"
                        ).timestamp()
                    ),
                    "title": "First",
                    "description": "One",
                },
                {
                    "id": "second",
                    "reset_type": "unknown",
                    "status": "unknown",
                    "granted_at": int(
                        datetime.fromisoformat(
                            "2026-06-18T00:00:00-04:00"
                        ).timestamp()
                    ),
                    "expires_at": None,
                    "title": None,
                    "description": None,
                },
                {
                    "id": "third",
                    "reset_type": "codex_rate_limits",
                    "status": "redeeming",
                    "granted_at": int(
                        datetime.fromisoformat(
                            "2026-06-19T00:00:00+00:00"
                        ).timestamp()
                    ),
                    "expires_at": None,
                    "title": None,
                    "description": None,
                },
                {
                    "id": "fourth",
                    "reset_type": "codex_rate_limits",
                    "status": "redeemed",
                    "granted_at": int(
                        datetime.fromisoformat(
                            "2026-06-20T00:00:00+00:00"
                        ).timestamp()
                    ),
                    "expires_at": None,
                    "title": None,
                    "description": None,
                },
            ],
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"available_count": -1, "credits": []},
            {"available_count": True, "credits": []},
            {"available_count": 0, "credits": "not-a-list"},
            {"available_count": 0, "credits": [{}]},
            {
                "available_count": 0,
                "credits": [{
                    "id": "credit",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "not-a-timestamp",
                }],
            },
            {
                "available_count": 0,
                "credits": [{
                    "id": "credit",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00",
                }],
            },
            {
                "available_count": 0,
                "credits": [{
                    "id": "credit",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00Z",
                    "expires_at": "bad",
                }],
            },
            {
                "available_count": 0,
                "credits": [{
                    "id": "credit",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00Z",
                    "title": 123,
                }],
            },
        ],
    )
    def test_invalid_inventory_contract_is_sanitized_502(
        self, authenticated_client, mock_db, monkeypatch, payload
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=payload)
        )
        response = authenticated_client.get(GET_PATH)
        assert response.status_code == 502
        assert response.json()["detail"].endswith("temporarily unavailable.")
        assert "timestamp" not in response.text


class TestConsumeOutcomes:
    @pytest.mark.parametrize(
        "outcome",
        ["reset", "nothing_to_reset", "no_credit", "already_redeemed"],
    )
    def test_normalizes_all_four_outcomes(
        self, authenticated_client, mock_db, monkeypatch, outcome
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, json={"code": outcome, "windows_reset": 7, "extra": "ignored"}
            ),
        )
        response = authenticated_client.post(
            POST_PATH,
            json={"credit_id": "credit", "idempotency_key": "idem"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"outcome": outcome, "windows_reset": 7}

    def test_windows_reset_defaults_to_zero(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(200, json={"code": "reset"}),
        )
        response = authenticated_client.post(
            POST_PATH,
            json={"credit_id": "credit", "idempotency_key": "idem"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"outcome": "reset", "windows_reset": 0}

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"code": "future_code"},
            {"code": "reset", "windows_reset": -1},
            {"code": "reset", "windows_reset": True},
            {"code": "reset", "windows_reset": "1"},
        ],
    )
    def test_invalid_consume_contract_is_sanitized_502(
        self, authenticated_client, mock_db, monkeypatch, payload
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=payload)
        )
        response = authenticated_client.post(
            POST_PATH,
            json={"credit_id": "credit", "idempotency_key": "idem"},
        )
        assert response.status_code == 502


class TestUpstreamFailureSanitization:
    @pytest.mark.parametrize("failure", ["timeout", "transport", "status", "json"])
    def test_failures_are_safe_generic_502(
        self,
        authenticated_client,
        mock_db,
        monkeypatch,
        caplog,
        failure,
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        raw_body = "RAW-UPSTREAM-SECRET-BODY"

        def handler(request):
            if failure == "timeout":
                raise httpx.ReadTimeout(raw_body, request=request)
            if failure == "transport":
                raise httpx.ConnectError(raw_body, request=request)
            if failure == "status":
                return httpx.Response(503, text=raw_body)
            return httpx.Response(200, text=raw_body)

        _install_transport(monkeypatch, handler)
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 502
        assert response.json() == {
            "detail": "The ChatGPT/Codex reset-credit service is temporarily unavailable."
        }
        combined = response.text + caplog.text
        assert raw_body not in combined
        assert ACCESS_TOKEN not in combined
        assert REFRESH_TOKEN not in combined
        assert ACCOUNT_ID not in combined
        assert "authorization" not in combined.lower()


class TestRefreshOnUnauthorized:
    def test_first_401_refreshes_reloads_rotated_token_and_retries_once(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        requests = []
        refresh_calls = []

        def handler(request):
            requests.append(request.headers["authorization"])
            if len(requests) == 1:
                return httpx.Response(401, text="stale token body")
            return httpx.Response(200, json=_inventory_payload())

        mock_client = _install_transport(monkeypatch, handler)

        async def fake_refresh(
            provider_id, *, stale_access_token=None, client=None
        ):
            refresh_calls.append((provider_id, stale_access_token, client))
            stored = db.get_codex_tokens(provider_id)
            db.set_codex_token_bundle(
                provider_id,
                access_token=encryption.encrypt_value("rotated-access-token"),
                refresh_token=stored["codex_refresh_token"],
                account_id=stored["codex_account_id"],
                needs_reauth=False,
            )
            return True

        monkeypatch.setattr(
            codex_token_refresh, "refresh_on_401_once", fake_refresh
        )
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 200, response.text
        assert requests == [
            f"Bearer {ACCESS_TOKEN}",
            "Bearer rotated-access-token",
        ]
        assert refresh_calls == [(PROVIDER_ID, ACCESS_TOKEN, mock_client)]

    def test_second_401_returns_reconnect_without_second_refresh(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        request_count = 0
        refresh_count = 0

        def handler(request):
            nonlocal request_count
            request_count += 1
            return httpx.Response(401, text="private unauthorized body")

        _install_transport(monkeypatch, handler)

        async def fake_refresh(
            provider_id, *, stale_access_token=None, client=None
        ):
            nonlocal refresh_count
            refresh_count += 1
            return True

        monkeypatch.setattr(
            codex_token_refresh, "refresh_on_401_once", fake_refresh
        )
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 409
        assert "reconnect" in response.json()["detail"].lower()
        assert request_count == 2
        assert refresh_count == 1
        assert "private unauthorized body" not in response.text

    @pytest.mark.parametrize("refresh_result", [False, RuntimeError("refresh failed")])
    def test_refresh_failure_returns_reconnect_conflict(
        self,
        authenticated_client,
        mock_db,
        monkeypatch,
        refresh_result,
    ):
        unlock_encryption(mock_db)
        _seed_provider()
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(401, text="private unauthorized body"),
        )
        refresh_count = 0

        async def fake_refresh(
            provider_id, *, stale_access_token=None, client=None
        ):
            nonlocal refresh_count
            refresh_count += 1
            if isinstance(refresh_result, Exception):
                raise refresh_result
            return refresh_result

        monkeypatch.setattr(
            codex_token_refresh, "refresh_on_401_once", fake_refresh
        )
        response = authenticated_client.get(GET_PATH)

        assert response.status_code == 409
        assert refresh_count == 1
        assert "refresh failed" not in response.text
        assert "private unauthorized body" not in response.text
