"""Exercise the production peer boundary without the transport test's auth stubs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import terminal_targets, terminals
from app.core.config import Settings, settings
from app.network import control, engine, terminal_relay


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("https", [False, True])
@pytest.mark.parametrize("operation", ["open", "read", "write", "resize", "close"])
async def test_malicious_member_cannot_self_authorize(monkeypatch, enabled, https, operation):
    monkeypatch.setattr(settings, "network_peer_terminals", enabled)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device:
        SimpleNamespace(kind="worker", status="active")))
    # This is the exploit: the worker manufactures a lease AND answers its
    # own callback "yes". Neither an enrolled identity nor HTTPS is owner proof.
    malicious_callback = AsyncMock(return_value={"authorized": True})
    monkeypatch.setattr(control, "call", malicious_callback)
    registry = terminals.TerminalRegistry()
    create = AsyncMock()
    monkeypatch.setattr(registry, "create", create)
    monkeypatch.setattr(terminals, "registry", registry)
    context = control.inbound_https.set(https)
    try:
        with pytest.raises(PermissionError):
            await control.handle("terminal", {
                "lease": "forged-by-enrolled-worker-123456", "op": operation,
                "options": {"shell": "sh"}, "authorized": True,
            }, from_device="malicious-member")
    finally:
        control.inbound_https.reset(context)
    malicious_callback.assert_not_awaited()
    create.assert_not_awaited()
    assert registry.count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_unfinished_peer_feature_is_not_advertised_or_offered(monkeypatch, enabled):
    assert Settings.model_fields["network_peer_terminals"].default is False
    monkeypatch.setattr(settings, "network_peer_terminals", enabled)
    monkeypatch.setattr(terminals, "supported", lambda: True)
    monkeypatch.setattr(terminal_targets.runners, "directory", lambda: {"runners": [{
        "id": "peer", "device_name": "Peer", "is_self": False, "online": True, "capabilities": {"terminal": 2},
    }]})
    assert terminals.capability_version() == 1
    assert await terminal_targets.targets(None) == []


@pytest.mark.asyncio
async def test_owner_callback_rejects_plaintext_before_credential_lookup(monkeypatch):
    context = control.inbound_https.set(False)
    try:
        with pytest.raises(PermissionError, match="HTTPS"):
            await control.handle("terminal-owner", {"lease": "anything"}, from_device="peer")
    finally:
        control.inbound_https.reset(context)


@pytest.mark.asyncio
async def test_source_lease_has_independent_deadline(monkeypatch):
    from app.api import terminals as api

    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device:
        SimpleNamespace(kind="worker", status="active")))
    monkeypatch.setattr(api, "require_terminal_owner", lambda request: "owner")
    context = control.inbound_https.set(True)
    lease = terminal_relay.authorize(object(), "owner", "peer")
    try:
        assert (await terminal_relay.handle_owner({"lease": lease}, "peer"))["authorized"]
        entry = terminal_relay._owners[lease]
        terminal_relay._owners[lease] = (*entry[:3], 0)
        assert not (await terminal_relay.handle_owner({"lease": lease}, "peer"))["authorized"]
    finally:
        terminal_relay.forget(lease)
        control.inbound_https.reset(context)
