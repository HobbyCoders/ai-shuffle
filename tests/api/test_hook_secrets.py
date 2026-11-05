"""Hook secrets are write-only across API responses and remain usable for signing."""
import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest

from app.db import database

BASE = "/api/v1/hooks"
SECRET = "dummy-hook-secret"


@pytest.fixture
def owner(client, tmp_path, monkeypatch):
    from app.api import auth

    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: True)
    client.headers["Origin"] = "http://testserver"
    database.create_project("proj-secret", "Secret tests", None, str(tmp_path))
    return client


def create(owner, scope, secret=SECRET):
    response = owner.post(f"{BASE}{scope}", json={
        "name": "Notify", "event": "Stop", "hook_type": "webhook",
        "url": "https://example.test/hook", "secret": secret,
    })
    assert response.status_code == 201, response.text
    return response.json()


def assert_safe(hook, present):
    assert "secret" not in hook
    assert hook["has_secret"] is present


@pytest.mark.parametrize("scope", ["/app", "/project/proj-secret"])
@pytest.mark.parametrize("secret", [SECRET, None, ""])
def test_every_response_exposes_only_secret_presence(owner, scope, secret):
    hook = create(owner, scope, secret)
    present = bool(secret)
    assert_safe(hook, present)
    assert_safe(owner.get(f"{BASE}/{hook['id']}").json(), present)
    for path in [scope, "/project/proj-secret"]:
        rows = owner.get(f"{BASE}{path}").json()
        assert len(rows) == 1
        assert_safe(rows[0], present)
    response = owner.patch(f"{BASE}/{hook['id']}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert_safe(response.json(), present)
    assert database.get_hook(hook["id"])["secret"] == secret


@pytest.mark.parametrize("scope", ["/app", "/project/proj-secret"])
@pytest.mark.parametrize("patch", [{}, {"secret": None}, {"secret": ""}, {"secret": "  \t"}])
def test_blank_update_preserves_stored_secret(owner, scope, patch):
    hook = create(owner, scope)
    response = owner.patch(f"{BASE}/{hook['id']}", json={"name": "Renamed", **patch})
    assert response.status_code == 200
    assert_safe(response.json(), True)
    assert database.get_hook(hook["id"])["secret"] == SECRET


@pytest.mark.parametrize("scope", ["/app", "/project/proj-secret"])
def test_explicit_clear_and_conflicting_replacement(owner, scope):
    hook = create(owner, scope)
    path = f"{BASE}/{hook['id']}"
    response = owner.patch(path, json={"clear_secret": True, "secret": "dummy-replacement"})
    assert response.status_code == 400
    assert database.get_hook(hook["id"])["secret"] == SECRET
    response = owner.patch(path, json={"clear_secret": True})
    assert response.status_code == 200
    assert_safe(response.json(), False)
    assert database.get_hook(hook["id"])["secret"] is None
    assert_safe(owner.get(path).json(), False)


def test_replacement_secret_signs_the_actual_webhook_after_blank_update(owner, monkeypatch):
    from app.core import http_client

    hook = create(owner, "/app")
    replacement = "dummy-replacement-secret"
    for patch in [{"secret": replacement}, {"secret": ""}]:
        response = owner.patch(f"{BASE}/{hook['id']}", json=patch)
        assert response.status_code == 200
        assert_safe(response.json(), True)
    post = AsyncMock(return_value=type("Response", (), {"status_code": 200, "text": "ok"})())
    monkeypatch.setattr(http_client, "get_http_client", lambda: type("Client", (), {"post": post})())
    response = owner.post(f"{BASE}/{hook['id']}/test")
    assert response.status_code == 200
    assert response.json()["success"] is True
    sent = post.call_args.kwargs
    expected = hmac.new(replacement.encode(), sent["content"].encode(), hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Hook-Signature"] == f"sha256={expected}"
