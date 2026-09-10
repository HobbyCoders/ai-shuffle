"""OpenAI credit sign-in stays provider-scoped and never replaces model credentials."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api import openai_billing as api
from app.core.config import settings
from app.db import database as db

ROOT = "/api/v1/model-providers"


@pytest.fixture
def billing(monkeypatch):
    state = {"status": "not_connected"}
    locks = {}
    service = Mock()
    service.operation_lock.side_effect = lambda pid: locks.setdefault(pid, asyncio.Lock())
    service.status.side_effect = lambda pid: dict(state)
    service.panel_key.side_effect = lambda pid: "openai-billing:" + pid

    async def start(pid):
        state.update(status="pending", flow_id="flow-test")
        return dict(state)

    async def close(pid):
        state.clear()
        state["status"] = "not_connected"

    service.start = AsyncMock(side_effect=start)
    service.close = AsyncMock(side_effect=close)
    service.refresh = AsyncMock()
    monkeypatch.setattr(api, "openai_billing_browser", service)
    monkeypatch.setattr(api, "_browser_running", AsyncMock(return_value=True))
    return service, state


@pytest.fixture
def provider(authenticated_client):
    response = authenticated_client.post(ROOT, json={
        "name": "OpenAI", "provider_type": "openai", "base_url": "https://api.openai.com",
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


def ready(billing):
    service, state = billing
    observation = {
        "organization_id": "org-test", "credits": {"balance_remaining": 21.09},
        "raw": {"source": "browser_platform", "organization_id": "org-test", "total_available": 21.09},
        "fetched_at": 1234567890000,
    }
    state.update(status="ready", flow_id="flow-test", observation_id="observation-test", **observation)

    def confirm(*args):
        state["status"] = "connected"
        return observation

    service.confirm.side_effect = confirm
    return {"flow_id": "flow-test", "observation_id": "observation-test", "organization_id": "org-test"}


@pytest.mark.parametrize("method,action", [
    ("get", "status"), ("post", "connect"), ("post", "confirm"),
    ("post", "cancel"), ("post", "disconnect"),
])
def test_admin_required(client, method, action):
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(f"{ROOT}/unknown/openai-billing/{action}", **kwargs)
    assert response.status_code in (401, 403)


def test_connect_does_not_modify_key_or_bind(authenticated_client, provider, billing):
    before = db.get_model_provider(provider, include_token=True)
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/connect")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    after = db.get_model_provider(provider, include_token=True)
    assert after.get("auth_token") == before.get("auth_token")
    assert after.get("openai_billing_org_id") is None
    billing[0].refresh.assert_not_called()


def test_confirm_persists_binding_and_same_provider_snapshot(authenticated_client, provider, billing):
    body = ready(billing)
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/confirm", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"
    assert response.json()["credits"]["balance_remaining"] == 21.09
    row = db.get_model_provider(provider)
    assert row["openai_billing_org_id"] == "org-test"
    assert row["openai_billing_instance_id"] == settings.effective_instance_id
    snapshots = db.get_all_provider_usage_snapshots()
    assert len(snapshots) == 1 and snapshots[0]["provider_id"] == provider
    assert "raw" not in response.json()
    public = authenticated_client.get(ROOT).json()["providers"][0]
    assert public["openai_billing_org_id"] == "org-test"


def test_rejects_expired_confirmation_without_binding(authenticated_client, provider, billing):
    body = ready(billing)
    billing[0].confirm.side_effect = api.BillingBrowserError("unavailable")
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/confirm", json=body)
    assert response.status_code == 409
    assert db.get_model_provider(provider).get("openai_billing_org_id") is None
    assert not db.get_all_provider_usage_snapshots()


@pytest.mark.parametrize("changes", [
    {"base_url": "https://example.com"}, {"provider_type": "chatgpt"}, {"enabled": False},
    {"base_url": "http://api.openai.com"}, {"base_url": "https://api.openai.com:8443"},
    {"base_url": "https://user@api.openai.com"},
])
def test_rejects_ineligible_provider(authenticated_client, provider, billing, changes):
    db.update_model_provider(provider, **changes)
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/connect")
    assert response.status_code in (400, 409)
    billing[0].start.assert_not_called()


def test_other_installation_cannot_take_over(authenticated_client, provider, billing):
    db.set_openai_billing_binding(provider, "org-test", "another-install")
    for action in ("connect", "disconnect"):
        assert authenticated_client.post(f"{ROOT}/{provider}/openai-billing/{action}").status_code == 409
    response = authenticated_client.get(f"{ROOT}/{provider}/openai-billing/status")
    assert response.json()["status"] == "other_installation"
    billing[0].start.assert_not_called()
    billing[0].close.assert_not_called()


def test_status_keeps_stale_balance_without_refresh(authenticated_client, provider, billing):
    body = ready(billing)
    authenticated_client.post(f"{ROOT}/{provider}/openai-billing/confirm", json=body)
    db.record_provider_usage_failure(
        provider, "openai", "unavailable", ("org-test", settings.effective_instance_id)
    )
    response = authenticated_client.get(f"{ROOT}/{provider}/openai-billing/status")
    assert response.json()["status"] == "reconnect_required"
    assert response.json()["credits"]["balance_remaining"] == 21.09
    billing[0].refresh.assert_not_called()


def test_cancel_requires_current_flow_and_preserves_binding(authenticated_client, provider, billing):
    db.set_openai_billing_binding(provider, "org-test", settings.effective_instance_id)
    authenticated_client.post(f"{ROOT}/{provider}/openai-billing/connect")
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/cancel", json={"flow_id": "old"})
    assert response.status_code == 409
    billing[0].close.assert_not_called()
    response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/cancel", json={"flow_id": "flow-test"})
    assert response.status_code == 200
    assert db.get_model_provider(provider)["openai_billing_org_id"] == "org-test"


@pytest.mark.parametrize("operation", ["disconnect", "disable", "custom", "delete"])
def test_cleanup_closes_only_owned_tracking(authenticated_client, provider, billing, operation):
    body = ready(billing)
    authenticated_client.post(f"{ROOT}/{provider}/openai-billing/confirm", json=body)
    if operation == "disconnect":
        response = authenticated_client.post(f"{ROOT}/{provider}/openai-billing/disconnect")
    elif operation == "disable":
        response = authenticated_client.post(f"{ROOT}/{provider}/toggle")
    elif operation == "custom":
        response = authenticated_client.patch(f"{ROOT}/{provider}", json={"base_url": "https://example.com"})
    else:
        response = authenticated_client.delete(f"{ROOT}/{provider}")
    assert response.status_code == 200, response.text
    billing[0].close.assert_awaited_once_with(provider)
    row = db.get_model_provider(provider)
    assert row is None or row.get("openai_billing_org_id") is None
    assert not db.get_all_provider_usage_snapshots()


def test_generic_update_cannot_fabricate_binding(authenticated_client, provider, billing):
    response = authenticated_client.patch(f"{ROOT}/{provider}", json={
        "openai_billing_org_id": "org-forged", "openai_billing_instance_id": "forged",
    })
    assert response.status_code == 200
    assert db.get_model_provider(provider).get("openai_billing_org_id") is None


@pytest.mark.parametrize("auth", [(False, None), (True, {"id": "api-user"})])
def test_browser_requires_admin(client, monkeypatch, auth):
    monkeypatch.setattr(api, "authenticate_websocket", AsyncMock(return_value=auth))
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f"{ROOT}/unknown/openai-billing/browser"):
            pass
    assert error.value.code == 4403


def test_browser_without_pending_flow_is_rejected(authenticated_client, provider, billing, monkeypatch):
    monkeypatch.setattr(api, "authenticate_websocket", AsyncMock(return_value=(True, None)))
    with pytest.raises(WebSocketDisconnect) as error:
        with authenticated_client.websocket_connect(f"{ROOT}/{provider}/openai-billing/browser"):
            pass
    assert error.value.code == 4404


def test_browser_is_provider_scoped_and_redacted(authenticated_client, provider, billing, monkeypatch):
    monkeypatch.setattr(api, "authenticate_websocket", AsyncMock(return_value=(True, None)))
    authenticated_client.post(f"{ROOT}/{provider}/openai-billing/connect")

    async def serve(ws, key, *, login_only, login_input):
        assert key == "openai-billing:" + provider
        assert login_only is True
        await ws.send_json({"type": "hello"})
        await ws.close()

    monkeypatch.setattr(api, "serve_browser_panel", serve)
    with authenticated_client.websocket_connect(f"{ROOT}/{provider}/openai-billing/browser") as ws:
        assert ws.receive_json() == {"type": "hello"}


@pytest.mark.asyncio
async def test_initial_viewport_and_typing_reach_the_owned_login_tab(monkeypatch):
    """Do not mock hub.dispatch_input: it rejects all workspace mutations."""
    import asyncio
    import json

    from app.api import browser_panel as panel
    from app.core import openai_billing_browser as billing_module
    from app.core.browser_stream import BrowserStreamHub, _Viewer

    service = billing_module.OpenAIBillingBrowser()
    session = billing_module._Session("provider", target_id="login-target", ws=AsyncMock())
    service._sessions["provider"] = session

    async def reply_to_viewport(raw):
        command = json.loads(raw)
        if command["method"] == "Emulation.setDeviceMetricsOverride":
            session.replies[command["id"]].set_result({})

    session.ws.send.side_effect = reply_to_viewport
    monkeypatch.setattr(billing_module.native_chrome, "cdp_get", AsyncMock())
    monkeypatch.setattr(api, "openai_billing_browser", service)
    monkeypatch.setattr(api, "_provider", AsyncMock(return_value={"id": "provider"}))
    monkeypatch.setattr(api, "authenticate_websocket", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(panel, "_initial_status", AsyncMock(return_value={"type": "status", "state": "live"}))
    socket = AsyncMock()
    socket.receive_text.side_effect = [
        json.dumps({"type": "viewport", "width": 390, "height": 844}),
        json.dumps({"type": "input", "kind": "mouse", "event": "move", "x": 100, "y": 80}),
        json.dumps({"type": "input", "kind": "text", "text": "login test"}),
        WebSocketDisconnect(),
    ]
    hub = BrowserStreamHub(service.panel_key("provider"))
    viewer = _Viewer(socket, redact_urls=True)
    monkeypatch.setattr(panel.browser_stream, "attach_viewer", AsyncMock(return_value=(hub, viewer)))
    detach = AsyncMock()
    monkeypatch.setattr(panel.browser_stream, "detach_viewer", detach)

    try:
        # The socket must read and forward text even without a mouse ACK.
        await asyncio.wait_for(api.billing_browser(socket, "provider", token=None), 0.5)
        commands = [json.loads(entry.args[0]) for entry in session.ws.send.await_args_list]
        assert [command["method"] for command in commands] == [
            "Emulation.setDeviceMetricsOverride", "Input.dispatchMouseEvent", "Input.insertText",
        ]
        assert commands[0]["params"]["width"] == 390
        assert commands[2]["params"]["text"] == "login test"
        socket.close.assert_not_awaited()
        detach.assert_awaited_once_with(service.panel_key("provider"), viewer)
    finally:
        await service.close("provider")
