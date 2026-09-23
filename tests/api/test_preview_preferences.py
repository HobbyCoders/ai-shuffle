"""Agent preview clicks cannot publish changes to the owner's synced UI state."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import preferences
from app.core import preview_access
from app.core.config import settings
from app.serve import IngressApplication

KEY = "sidebar_collapsed_groups"
ORIGIN = "http://10.0.0.135:18090"


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    # The direct-LAN owner here needs "trusted LAN, no login" (off by default).
    from app.core import owner_auth
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(preferences, "request_preference_identity", lambda request: ("account", "owner"))
    rows = {}

    def put(user_type, user_id, key, value, only_if_missing=False, **kwargs):
        if not only_if_missing or key not in rows:
            rows[key] = {"key": key, "value": deepcopy(value), "revision": "saved"}
        return deepcopy(rows[key])

    writes = Mock(side_effect=put)
    deletes = Mock(side_effect=lambda user_type, user_id, key: rows.pop(key, None) is not None)
    monkeypatch.setattr(preferences.database, "set_user_preference", writes)
    monkeypatch.setattr(preferences.database, "delete_user_preference", deletes)
    monkeypatch.setattr(preferences.database, "get_user_preference", lambda t, u, k: deepcopy(rows.get(k)))
    monkeypatch.setattr(preferences.database, "get_all_user_preferences", lambda t, u: deepcopy(list(rows.values())))
    api = FastAPI()
    api.include_router(preferences.router)
    app = IngressApplication(api, local_port=18090)
    owner = TestClient(app, base_url=ORIGIN, client=("10.0.0.200", 54321), headers={"Origin": ORIGIN})
    preview = TestClient(app, base_url=ORIGIN, client=("10.0.0.200", 54322), headers={"Origin": ORIGIN})
    preview.cookies.set(preview_access.COOKIE_NAME, preview_access.mint("preview-session"))
    return owner, preview, writes, deletes


@pytest.mark.parametrize("proxied", [False, True], ids=["direct-LAN", "vite-proxy"])
@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE"])
@pytest.mark.parametrize("key,value", [(KEY, {"settled": True}), ("theme", "dark")])
def test_preview_cannot_reset_owner_preferences(clients, proxied, method, key, value):
    owner, preview, writes, deletes = clients
    path = f"/api/v1/preferences/{key}"
    # Drive the producing path: a real owner action is persisted normally.
    assert owner.put(path, json={"key": key, "value": value}).status_code == 200
    writes.reset_mock()
    if proxied:
        preview.headers["X-Forwarded-Host"] = "localhost:5173"
    body = {} if method == "DELETE" else {"json": {"key": key, "value": {}}}
    rejected = preview.request(method, path, **body)
    assert rejected.status_code == 403
    assert "previews" in rejected.json()["detail"]
    writes.assert_not_called()
    deletes.assert_not_called()
    # Both preview read routes still show the real saved choice. Normal clients
    # still receive it through the API used by cross-window/worker sync.
    assert preview.get(path).json()["value"] == value
    assert preview.get("/api/v1/preferences/all").json()["preferences"][key] == value
    assert owner.get(path).json()["value"] == value


def test_normal_owner_can_still_update_and_delete(clients):
    owner, preview, writes, deletes = clients
    path = f"/api/v1/preferences/{KEY}"
    for method, value in [("PUT", {"settled": True}), ("POST", {"settled": False})]:
        assert owner.request(method, path, json={"key": KEY, "value": value}).status_code == 200
        assert preview.get(path).json()["value"] == value
    assert writes.call_count == 2
    assert owner.delete(path).json() == {"deleted": True}
    assert preview.get(path).json() is None
    deletes.assert_called_once()
