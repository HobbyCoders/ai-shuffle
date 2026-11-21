"""Settings > Vault: the owner's saved sign-ins managed with no chat open."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.core import vault_access, vault_store
from app.core.vault_access import AccessDenied, Broker, Grant, GrantInput, new_entry, vault_owner
from tests.core.test_vault_store import vault  # noqa: F401 - fixture

FINGERPRINT = "SHA256:" + "a" * 43
MACHINE = {"kind": "machine", "label": "HomeLab", "host": "10.0.0.20", "port": 2222, "username": "root",
           "password": "MUST-NOT-ECHO"}
SERVICE = {"kind": "service", "label": "Cloud API", "url": "https://api.example.com/v4",
           "secret": "MUST-NOT-ECHO-TOKEN"}
SECRETS = ("MUST-NOT-ECHO", "MUST-NOT-ECHO-TOKEN")


@pytest.fixture
def broker(monkeypatch):
    from app.api import vault as api
    from app.network import vault_routing
    instance = Broker()
    monkeypatch.setattr(api, "broker", instance)
    monkeypatch.setattr(vault_access, "publish_saved_change", AsyncMock())
    monkeypatch.setattr(vault_routing, "notify_watchers", lambda *args, **kwargs: None)
    return instance


def client(scheme="https"):
    from app.api import vault as api
    app = FastAPI()
    app.include_router(api.entries_router)
    app.dependency_overrides[api.require_admin] = lambda: "owner"
    return AsyncClient(transport=ASGITransport(app), base_url=f"{scheme}://test")


def grant_on(broker, session_id, saved_id, owner=("device", "worker", None)):
    grant = Grant("g-" + session_id, session_id, owner, "HomeLab", "machine", "root@10.0.0.20:2222",
                  time.time() + 600, saved_id=saved_id)
    broker.grants[grant.id] = grant
    return grant


# --- validation ------------------------------------------------------------------------------

def test_a_new_machine_sign_in_keeps_only_its_own_fields():
    entry = new_entry(GrantInput.model_validate({**MACHINE, "label": "  HomeLab  ", "host": "HomeLab.Example ",
                                                 "fingerprint": FINGERPRINT + "="}))
    assert (entry.kind, entry.label, entry.host, entry.port, entry.username) == (
        "machine", "HomeLab", "homelab.example", 2222, "root")
    assert entry.fingerprint == FINGERPRINT and entry.password.get_secret_value() == "MUST-NOT-ECHO"
    assert not entry.url and not entry.secret.get_secret_value() and not entry.path and not entry.writable
    # No fingerprint is fine: the first connection the owner approves pins the key.
    assert new_entry(GrantInput.model_validate(MACHINE)).fingerprint == ""


def test_a_new_service_sign_in_keeps_only_its_own_fields():
    entry = new_entry(GrantInput.model_validate({**SERVICE, "writable": True, "auth_header": "X-Api-Key",
                                                 "auth_scheme": "", "fingerprint": "AB" * 32}))
    assert (entry.kind, entry.url, entry.auth_header, entry.auth_scheme, entry.writable) == (
        "service", "https://api.example.com/v4", "X-Api-Key", "", True)
    assert entry.fingerprint == "ab" * 32 and entry.secret.get_secret_value() == "MUST-NOT-ECHO-TOKEN"
    assert not entry.host and not entry.username and not entry.password.get_secret_value()


@pytest.mark.parametrize("change", [
    {"label": "   "},
    {"password": ""},                                         # no secret at all
    {"private_key": "MUST-NOT-ECHO"},                         # password and key
    {"password": "", "passphrase": "MUST-NOT-ECHO"},          # passphrase without a key
    {"password": "", "private_key": "MUST-NOT-ECHO"},         # not a readable key
    {"username": "root; rm -rf /"},
    {"host": "https://10.0.0.20"},
    {"host": "0.0.0.0"},
    {"fingerprint": "MD5:aa:bb"},
    {"url": "https://api.example.com"},                       # a service field
    {"secret": "MUST-NOT-ECHO-TOKEN"},
    {"writable": True},
    {"path": "/srv"},
    {"auth_header": "X-Api-Key"},
])
def test_an_invalid_machine_sign_in_is_refused_without_echo(change):
    with pytest.raises(AccessDenied) as error:
        new_entry(GrantInput.model_validate({**MACHINE, **change}))
    assert "MUST-NOT-ECHO" not in str(error.value) and "rm -rf" not in str(error.value)


@pytest.mark.parametrize("change", [
    {"secret": ""},
    {"url": "http://api.example.com"},
    {"url": "https://user:MUST-NOT-ECHO@api.example.com"},
    {"auth_header": "Host"},
    {"fingerprint": "not-a-pin"},
    {"host": "10.0.0.20"},                                    # machine fields
    {"username": "root"},
    {"port": 2222},
    {"password": "MUST-NOT-ECHO"},
    {"path": "/srv"},
])
def test_an_invalid_service_sign_in_is_refused_without_echo(change):
    with pytest.raises(AccessDenied) as error:
        new_entry(GrantInput.model_validate({**SERVICE, **change}))
    assert "MUST-NOT-ECHO" not in str(error.value)


def test_a_folder_is_never_kept_in_the_vault():
    with pytest.raises(AccessDenied, match="Only SSH machine and web service"):
        new_entry(GrantInput(kind="folder", label="Media", path="/srv/media"))


# --- API -------------------------------------------------------------------------------------

def test_every_entries_route_requires_the_owner():
    from app.api import vault as api
    from app.api.auth import require_admin
    routes = {(tuple(sorted(route.methods)), route.path) for route in api.entries_router.routes}
    assert routes == {(("GET",), "/api/v1/vault/entries"), (("POST",), "/api/v1/vault/entries"),
                      (("PATCH",), "/api/v1/vault/entries/{entry_id}"),
                      (("DELETE",), "/api/v1/vault/entries/{entry_id}")}
    for route in api.entries_router.routes:
        assert any(dep.call is require_admin for dep in route.dependant.dependencies), route.path


@pytest.mark.asyncio
async def test_add_list_edit_remove_never_returns_a_secret(broker, vault):
    async with client() as http:
        assert (await http.get("/api/v1/vault/entries")).json() == {"entries": [], "vault_available": True}
        added = await http.post("/api/v1/vault/entries", json=MACHINE)
        assert added.status_code == 200, added.text
        machine_id = added.json()["id"]
        service = await http.post("/api/v1/vault/entries", json=SERVICE)
        assert service.status_code == 200, service.text
        listed = await http.get("/api/v1/vault/entries")
        by_label = {entry["label"]: entry for entry in listed.json()["entries"]}
        assert by_label["HomeLab"] == {
            "id": machine_id, "kind": "machine", "label": "HomeLab", "target": "root@10.0.0.20:2222",
            "fingerprint": "", "host": "10.0.0.20", "port": 2222, "username": "root", "uses_key": False}
        assert by_label["Cloud API"]["target"] == "https://api.example.com/v4"
        edited = await http.patch(f"/api/v1/vault/entries/{machine_id}",
                                  json={"label": "Rack", "password": "MUST-NOT-ECHO-2"})
        assert edited.json() == {"updated": True, "revoked": 0, "host_key_cleared": False}
        relisted = await http.get("/api/v1/vault/entries")
        for response in (added, service, listed, edited, relisted):
            assert not any(secret in response.text for secret in SECRETS)
        # Nothing readable reaches storage either.
        assert not any(secret in json.dumps(vault) or "10.0.0.20" in json.dumps(vault) for secret in SECRETS)
        stored = vault_store.load(vault_owner(), machine_id)
        assert (stored.label, stored.password.get_secret_value()) == ("Rack", "MUST-NOT-ECHO-2")
        removed = await http.delete(f"/api/v1/vault/entries/{machine_id}")
        assert removed.json() == {"deleted": True, "revoked": 0}
        assert [entry["label"] for entry in (await http.get("/api/v1/vault/entries")).json()["entries"]] == ["Cloud API"]


@pytest.mark.asyncio
async def test_invalid_input_is_refused_without_echo(broker, vault):
    async with client() as http:
        for invalid in ({**MACHINE, "port": 70000}, {**MACHINE, "unknown": True}, {**MACHINE, "kind": "database"},
                        {**MACHINE, "label": ""}, {"password": "MUST-NOT-ECHO"}):
            response = await http.post("/api/v1/vault/entries", json=invalid)
            assert response.status_code == 422 and "MUST-NOT-ECHO" not in response.text
        response = await http.post("/api/v1/vault/entries", json={**MACHINE, "host": "https://MUST-NOT-ECHO"})
        assert response.status_code == 400 and "MUST-NOT-ECHO" not in response.text
        response = await http.post("/api/v1/vault/entries", json={"kind": "folder", "label": "Media", "path": "/srv"})
        assert response.status_code == 400
        assert not vault
        entry_id = (await http.post("/api/v1/vault/entries", json=MACHINE)).json()["id"]
        duplicate = await http.post("/api/v1/vault/entries", json={**MACHINE, "label": "homelab"})
        assert duplicate.status_code == 400 and "MUST-NOT-ECHO" not in duplicate.text
        response = await http.patch(f"/api/v1/vault/entries/{entry_id}", json={"password": "MUST-NOT-ECHO", "port": "22"})
        assert response.status_code == 422 and "MUST-NOT-ECHO" not in response.text
        response = await http.patch(f"/api/v1/vault/entries/{entry_id}", json={"url": "https://api.example.com"})
        assert response.status_code == 400
        for unknown in ("f" * 32, "not-an-id"):
            assert (await http.patch(f"/api/v1/vault/entries/{unknown}", json={"label": "X"})).status_code == 400
            assert (await http.delete(f"/api/v1/vault/entries/{unknown}")).status_code == 409


@pytest.mark.asyncio
async def test_changing_or_removing_a_sign_in_ends_access_built_on_it_in_every_chat(broker, vault):
    async with client() as http:
        entry_id = (await http.post("/api/v1/vault/entries", json=MACHINE)).json()["id"]
        other_id = (await http.post("/api/v1/vault/entries", json={**MACHINE, "label": "Other"})).json()["id"]
        first, second = grant_on(broker, "chat-a", entry_id), grant_on(broker, "chat-b", entry_id)
        unrelated = grant_on(broker, "chat-c", other_id)
        # The same id under another principal is another Vault.
        foreign = grant_on(broker, "chat-d", entry_id, owner=("device", "worker", "alice"))
        renamed = await http.patch(f"/api/v1/vault/entries/{entry_id}", json={"label": "Rack"})
        assert renamed.json()["revoked"] == 0 and not first.revoked
        changed = await http.patch(f"/api/v1/vault/entries/{entry_id}", json={"username": "admin"})
        assert changed.json()["revoked"] == 2 and first.revoked and second.revoked
        assert not unrelated.revoked and not foreign.revoked
        third = grant_on(broker, "chat-e", entry_id)
        removed = await http.delete(f"/api/v1/vault/entries/{entry_id}")
        assert removed.json() == {"deleted": True, "revoked": 1} and third.revoked
        assert set(broker.grants) == {unrelated.id, foreign.id}


@pytest.mark.asyncio
async def test_settings_and_chats_share_one_vault_per_principal(broker, vault):
    """A sign-in added in Settings is the one a chat's request reuses; another principal never sees it."""
    alice = vault_store.save(("device", "worker", "alice"), new_entry(GrantInput.model_validate(SERVICE)))
    async with client() as http:
        entry_id = (await http.post("/api/v1/vault/entries", json=MACHINE)).json()["id"]
        listed = (await http.get("/api/v1/vault/entries")).json()["entries"]
        assert [entry["id"] for entry in listed] == [entry_id]
        assert (await http.delete(f"/api/v1/vault/entries/{alice}")).status_code == 409
        assert (await http.patch(f"/api/v1/vault/entries/{alice}", json={"label": "Mine"})).status_code == 400
    # A chat of the install owner, on any device row, loads the same entry.
    assert vault_store.load(("other-device", "other-worker", None), entry_id).host == "10.0.0.20"
    assert vault_store.load(("device", "worker", "alice"), alice).url == "https://api.example.com/v4"
    with pytest.raises(AccessDenied):
        vault_store.load(("device", "worker", "alice"), entry_id)


@pytest.mark.asyncio
async def test_a_locked_vault_is_reported_and_refuses_writes(broker, vault, monkeypatch):
    from app.core import encryption
    monkeypatch.setattr(encryption, "_encryption_key", None)
    async with client() as http:
        assert (await http.get("/api/v1/vault/entries")).json() == {"entries": [], "vault_available": False}
        response = await http.post("/api/v1/vault/entries", json=MACHINE)
        assert response.status_code == 400 and "MUST-NOT-ECHO" not in response.text
    assert not vault


@pytest.mark.asyncio
async def test_every_change_refreshes_the_owners_settings_and_chats(vault, monkeypatch):
    from app.api import vault as api
    from app.core import preference_identity
    from app.core.global_events import global_events
    from app.network import vault_routing
    broker = Broker()
    monkeypatch.setattr(api, "broker", broker)
    published, pushed = [], []
    monkeypatch.setattr(global_events, "resources_changed",
                        lambda *names, user_type, user_id: published.append((names, user_type, user_id)))
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda principal: ("admin", "owner"))
    monkeypatch.setattr(vault_routing, "notify_watchers", lambda session_id, saved=False: pushed.append(saved))
    async with client() as http:
        entry_id = (await http.post("/api/v1/vault/entries", json=MACHINE)).json()["id"]
        await http.patch(f"/api/v1/vault/entries/{entry_id}", json={"label": "Rack"})
        await http.delete(f"/api/v1/vault/entries/{entry_id}")
        # A refused change publishes nothing.
        await http.delete(f"/api/v1/vault/entries/{entry_id}")
        await asyncio.gather(*broker._notifications)
    # Only an invalidation: no label, address or secret travels with it.
    assert published == [(("vault",), "admin", "owner")] * 3
    assert pushed == [True] * 3


# --- transport and preview -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_http_is_refused_for_reads_and_writes(broker, vault):
    async with client("http") as http:
        for response in (await http.get("/api/v1/vault/entries"),
                         await http.post("/api/v1/vault/entries", json=MACHINE),
                         await http.patch(f"/api/v1/vault/entries/{'a' * 32}", json={"label": "X"}),
                         await http.delete(f"/api/v1/vault/entries/{'a' * 32}")):
            assert response.status_code == 403
            assert "Open Settings" in response.json()["detail"] and "MUST-NOT-ECHO" not in response.text
    assert not vault


def test_a_preview_login_cannot_read_or_change_the_vault():
    from app.api import vault as api
    from app.core.preview_access import mutation_allowed
    for path in ("/api/v1/vault/entries", f"/api/v1/vault/entries/{'a' * 32}", "/api/v1/vault/sessions/chat/grants"):
        for method in ("POST", "PATCH", "DELETE"):
            assert not mutation_allowed(path, method)
    # Reads are refused by the Vault API itself, on any transport.
    request = Request({"type": "http", "scheme": "https", "method": "GET", "path": "/api/v1/vault/entries",
                       "headers": [(b"host", b"worker.example.com")], "server": ("worker.example.com", 443),
                       "client": ("10.0.0.9", 1234), "state": {"preview_grant": object()}})
    with pytest.raises(HTTPException) as error:
        api.private_transport(request, api.INSECURE_SETTINGS)
    assert error.value.status_code == 403 and "Preview access" in error.value.detail
