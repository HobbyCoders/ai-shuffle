"""Private-input failures distinguish SSH rejection from worker faults."""
import asyncio
from unittest.mock import AsyncMock

import asyncssh
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.core.test_chat_access import (
    broker as broker,  # noqa: PLC0414 — pytest fixture re-export
)
from tests.core.test_chat_access import (
    ssh_target as ssh_target,  # noqa: PLC0414 — pytest fixture re-export
)


@pytest_asyncio.fixture
async def client(broker, monkeypatch):
    from app.api import chat_access as api
    monkeypatch.setattr(api, "broker", broker)
    monkeypatch.setattr(api, "checked", AsyncMock())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: "test-owner"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["password", "host_key"])
async def test_failed_ssh_submission_can_be_corrected_without_replacing_request(client, broker, ssh_target, failure):
    spec, commands, _ = ssh_target
    connection = spec.model_dump(mode="json")
    connection["password"] = spec.password.get_secret_value()
    requested = asyncio.create_task(broker.request("chat", "machine", "Inspect disposable server"))
    try:
        while not broker.requests:
            await asyncio.sleep(0)
        request_id = next(iter(broker.requests))
        body = {"connection": dict(connection), "minutes": 60}
        if failure == "password":
            body["connection"]["password"] = "incorrect-disposable-password"
            expected = "SSH sign-in was rejected."
        else:
            body["connection"]["fingerprint"] = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
            expected = "No host key on this server matches that fingerprint."
        endpoint = f"/api/v1/chat-access/chat/requests/{request_id}"
        response = await client.post(endpoint, json=body)
        assert response.status_code == 400
        assert response.json()["detail"].startswith(expected)
        assert spec.password.get_secret_value() not in response.text
        assert not requested.done() and not broker.grants and not commands
        assert not broker.requests[request_id].claiming
        response = await client.post(endpoint, json={"connection": connection, "minutes": 60})
        assert response.status_code == 200 and response.json()["granted"]
        result = await requested
        assert len(broker.grants) == 1 and not commands
        assert set(result) == {"id", "kind", "expires_at", "writable"}
        broker.revoke("chat", result["id"])
    finally:
        requested.cancel()
        await asyncio.gather(requested, return_exceptions=True)
        await asyncio.gather(*broker._notifications)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status,expected", [
    (TimeoutError("private-password-sentinel"), 400, "The SSH connection timed out."),
    (RuntimeError("private-password-sentinel"), 500, "Private access is unavailable on this worker."),
    (ModuleNotFoundError("private-password-sentinel"), 500, "Private access is unavailable on this worker."),
])
async def test_failure_feedback_never_echoes_exception_inputs(client, broker, monkeypatch, error, status, expected, caplog):
    monkeypatch.setattr(broker, "fulfill", AsyncMock(side_effect=error))
    response = await client.post("/api/v1/chat-access/chat/requests/request", json={})
    assert response.status_code == status
    assert response.json()["detail"].startswith(expected)
    assert "private-password-sentinel" not in response.text + caplog.text
