import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_admin
from app.api import pairing as pairing_api


@pytest.fixture
def client(mock_db):
    app = FastAPI()
    app.include_router(pairing_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    with TestClient(app) as test_client:
        yield test_client


def _exchange_body(code: str) -> dict:
    return {
        "code": code,
        "device_id": "browser-device-12345678",
        "device_name": "Kitchen iPad",
        "platform": "ios-browser",
        "client_type": "browser",
    }


def test_admin_creates_qr_and_device_exchanges_once(client):
    pairing_api._exchange_limiter.reset()
    created = client.post(
        "/api/v1/pairing/grants",
        json={"endpoint_url": "https://shuffle.example"},
    )
    assert created.status_code == 201, created.text
    grant = created.json()
    assert len(grant["code"]) == 12
    assert grant["display_code"].count("-") == 2
    assert grant["pairing_url"].startswith("https://shuffle.example/pair#token=")
    assert grant["qr_svg"].startswith("<svg")
    assert "token_hash" not in grant["grant"]

    exchanged = client.post(
        "/api/v1/pairing/exchange",
        json=_exchange_body(grant["display_code"]),
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["client"]["device_name"] == "Kitchen iPad"
    assert exchanged.cookies["session"].startswith("shc_")

    reused = client.post(
        "/api/v1/pairing/exchange",
        json={**_exchange_body(grant["code"]), "device_id": "other-device-12345678"},
    )
    assert reused.status_code == 400


def test_loopback_endpoint_produces_code_without_qr(client):
    created = client.post(
        "/api/v1/pairing/grants",
        json={"endpoint_url": "http://127.0.0.1:8000"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["pairing_url"].startswith("http://127.0.0.1:8000/pair")
    assert created.json()["qr_svg"] is None


def test_runner_grant_is_code_only(client):
    created = client.post(
        "/api/v1/pairing/grants",
        json={"purpose": "runner", "endpoint_url": "https://shuffle.example"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["grant"]["purpose"] == "runner"
    assert body["grant"]["scopes"] == ["runner:enroll"]
    assert body["pairing_url"] is None
    assert body["qr_svg"] is None


def test_admin_can_list_and_revoke_paired_client(client):
    pairing_api._exchange_limiter.reset()
    created = client.post(
        "/api/v1/pairing/grants",
        json={"endpoint_url": None},
    ).json()
    assert client.post(
        "/api/v1/pairing/exchange",
        json=_exchange_body(created["code"]),
    ).status_code == 200

    clients = client.get("/api/v1/pairing/clients")
    assert clients.status_code == 200
    paired = clients.json()["clients"][0]
    assert "session_token_hash" not in paired

    revoked = client.delete(f"/api/v1/pairing/clients/{paired['id']}")
    assert revoked.status_code == 204
