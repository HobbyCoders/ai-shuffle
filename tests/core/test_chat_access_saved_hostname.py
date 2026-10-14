"""Owner hostname approval and model-safe saved connection discovery."""
import asyncio
import json
import socket
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core import chat_access, chat_vault
from app.core.chat_access import AccessDenied, GrantInput, GrantSubmission, SavedConnectionEdit
from tests.core.test_chat_access import broker, saved_vault, ssh_target  # noqa: F401
from tests.core.test_chat_vault import vault  # noqa: F401


def dns(*addresses):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, "", (ip, 22)) for ip in addresses]


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["192.0.2.5", "2001:db8::5"])
async def test_hostname_accepts_one_unique_address(monkeypatch, address):
    resolver = AsyncMock(return_value=dns(address, address))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await chat_access.resolve_machine_address("BUILD.example", 2222) == address
    resolver.assert_awaited_once_with("build.example", 2222, type=socket.SOCK_STREAM)


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["::1"], ["0.0.0.0"],
    ["224.0.0.1"], ["169.254.1.1"], ["::ffff:127.0.0.1"],
    ["192.0.2.5", "127.0.0.1"], ["192.0.2.5", "192.0.2.6"],
    ["192.0.2.5", "2001:db8::5"]])
async def test_hostname_rejects_ambiguous_or_unsafe_answers(monkeypatch, addresses):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=dns(*addresses)))
    with pytest.raises(AccessDenied, match="one unicast, non-loopback"):
        await chat_access.resolve_machine_address("build.example", 22)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [socket.gaierror(), TimeoutError()])
async def test_hostname_resolution_failure_is_private(monkeypatch, error):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=error))
    with pytest.raises(AccessDenied, match="Could not resolve") as caught:
        await chat_access.resolve_machine_address("private-name.example", 22)
    assert "private-name" not in str(caught.value)


@pytest.mark.asyncio
async def test_hostname_is_resolved_once_before_key_probe_and_pinned_for_connections(broker, monkeypatch):
    resolver = AsyncMock(return_value=dns("192.0.2.5", "192.0.2.5"))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    observed = []

    async def pin(grant, fingerprint):
        observed.append(("pin", grant.host, fingerprint))
        grant.fingerprint = "SHA256:" + "a" * 43

    class Connection:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    async def connect(*args, **kwargs):
        observed.append(("connect", args[0]))
        return Connection()

    monkeypatch.setattr(broker, "pin_host_key", pin)
    monkeypatch.setattr(chat_access.asyncssh, "connect", connect)
    spec = GrantInput(kind="machine", label="Build", host="BUILD.example.", username="deploy", password="test-only")
    result = await broker.create("chat", spec)
    grant = broker.grants[result["id"]]
    try:
        assert grant.host == "192.0.2.5"
        assert result["target"] == "deploy@192.0.2.5:22"
        resolver.return_value = dns("192.0.2.99")
        await broker.connection(grant)
        assert resolver.await_count == 1
        assert observed == [("pin", "192.0.2.5", ""), ("connect", "192.0.2.5"), ("connect", "192.0.2.5")]
    finally:
        broker.revoke("chat", grant.id)


def test_owner_hostname_limit_and_saved_edit():
    host = ".".join(["a" * 63] * 3 + ["b" * 61])
    assert len(host) == 253
    spec = GrantInput(kind="machine", label="Build", host="192.0.2.1", fingerprint="SHA256:" + "a" * 43)
    updated = chat_access.apply_saved_edit(spec, SavedConnectionEdit(host=host))
    assert updated.host == host and updated.fingerprint == ""
    assert GrantInput(kind="machine", label="Build", host=host).host == host
    with pytest.raises(ValidationError):
        GrantInput(kind="machine", label="Build", host=host + "a")


@pytest.mark.asyncio
async def test_saved_discovery_uses_real_owner_scoped_vault_and_survives_broker_restart(broker, saved_vault):
    owner = ("local", "worker")
    spec = GrantInput(kind="service", label="Inventory API", url="https://private.example/api", secret="private-test-token")
    saved_id = chat_vault.save(owner, spec)
    foreign = chat_vault.save(("local", "worker", "another-owner"), spec.model_copy(update={"label": "Foreign"}))
    # A fresh broker has no grants, but the encrypted saved entry still exists.
    restarted = chat_access.Broker()
    resources = await restarted.resources("chat")
    assert resources["saved_connections"] == [{"id": saved_id, "kind": "service", "label": "Inventory API"}]
    assert resources["grants"] == []
    assert all(value not in json.dumps(resources) for value in (spec.url, "private-test-token", foreign, "Foreign"))
    result = await restarted.request("chat", "service", "Read inventory", saved_id=saved_id)
    request = restarted.requests[result["request_id"]]
    assert request.public()["saved_id"] == saved_id
    assert not restarted.grants
    assert await restarted.request("chat", "service", "Read inventory", saved_id=saved_id) == result
    await restarted.decline("chat", request.id)
    with pytest.raises(AccessDenied, match="Unknown saved connection"):
        await restarted.request("chat", "service", "Read inventory", saved_id=foreign)
    with pytest.raises(AccessDenied, match="Unknown saved connection"):
        await restarted.request("chat", "machine", "Wrong kind", saved_id=saved_id)
    with pytest.raises(AccessDenied, match="without connection hints"):
        await restarted.request("chat", "service", "Conflicting hints", saved_id=saved_id, service_provider="cloudflare")
    assert not restarted.grants and not restarted.requests


@pytest.mark.asyncio
async def test_saved_id_request_and_real_ssh_approval(broker, saved_vault, ssh_target):
    spec, _, _ = ssh_target
    saved_id = chat_vault.save(("local", "worker"), spec)
    task = asyncio.create_task(broker.request("chat", "machine", "Inspect saved server", saved_id=saved_id))
    try:
        async with asyncio.timeout(3):
            while not broker.requests:
                await asyncio.sleep(0)
        request = next(iter(broker.requests.values()))
        assert request.public()["saved_id"] == saved_id
        assert not broker.grants
        await broker.fulfill("chat", request.id, GrantSubmission(saved_id=saved_id))
        grant = await task
        assert grant["kind"] == "machine"
        assert set(grant) == {"id", "kind", "expires_at", "writable"}
        broker.revoke("chat", grant["id"])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_lists_and_requests_saved_service_without_private_details(broker, saved_vault, monkeypatch):
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp import chat_access_sdk
    monkeypatch.setattr(chat_access_sdk, "broker", broker)
    tools = {tool.name: tool for tool in recorded_sdk_server_tools(chat_access_sdk.create_chat_access_server("chat"))}
    saved_id = chat_vault.save(("local", "worker"), GrantInput(
        kind="service", label="Inventory", url="https://private.example/api", secret="vault-only-secret"))
    response = await tools["ChatResources"].handler({})
    assert not response.get("isError")
    metadata = json.loads(response["content"][0]["text"])
    assert metadata["saved_connections"] == [{"id": saved_id, "kind": "service", "label": "Inventory"}]
    response = await tools["RequestChatAccess"].handler({"kind": "service", "purpose": "Inspect inventory", "saved_id": saved_id})
    assert not response.get("isError")
    pending = json.loads(response["content"][0]["text"])
    assert broker.requests[pending["request_id"]].public()["saved_id"] == saved_id
    assert not broker.grants
    await broker.decline("chat", pending["request_id"])


@pytest.mark.asyncio
async def test_saved_catalogue_wait_is_bounded(broker, monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(chat_vault, "catalog", lambda *args, **kwargs: release.wait(5) and [])
    monkeypatch.setattr(chat_access, "LISTING_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(AccessDenied, match="timed out"):
            await asyncio.wait_for(broker.resources("chat"), 1)
    finally:
        release.set()
