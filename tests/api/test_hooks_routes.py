"""Hooks API routes against the real app and database.

Every ``/api/v1/hooks`` route is owner-only (API keys get 403). These cover the
create validation documented in docs/content/api-reference/hooks.md (400 for
an unknown event, a bash hook without ``command``, a webhook without ``url``;
404 for a missing project), the per-hook 404s, and the test/plugin endpoints.
"""
import pytest

from app.api.auth import hash_api_key
from app.core.models import VALID_HOOK_EVENTS
from app.db import database

KEY = "aih_hooks_routes_key"
AUTH = {"Authorization": f"Bearer {KEY}"}
BASE = "/api/v1/hooks"


@pytest.fixture
def owner(client, tmp_path, monkeypatch):
    from app.api import auth

    monkeypatch.setattr(auth.owner_auth, "local_access",
                        lambda request: "authorization" not in request.headers)
    client.headers["Origin"] = "http://testserver"
    database.create_project("proj-h", "H", None, str(tmp_path / "proj-h"))
    database.create_api_user("k-h", "Key", hash_api_key(KEY), project_id=None)
    return client


def _bash(**over):
    body = {"name": "lint", "event": "PostToolUse", "hook_type": "bash", "command": "echo hi"}
    body.update(over)
    return body


def test_events_lists_the_valid_types(owner):
    assert owner.get(f"{BASE}/events").json() == VALID_HOOK_EVENTS


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/events", None),
    ("GET", "/app", None),
    ("POST", "/app", _bash()),
    ("GET", "/project/proj-h", None),
    ("POST", "/project/proj-h", _bash()),
    ("GET", "/hook-x", None),
    ("PATCH", "/hook-x", {"name": "n"}),
    ("DELETE", "/hook-x", None),
    ("POST", "/hook-x/test", None),
    ("GET", "/plugins/all", None),
])
def test_api_keys_are_refused(owner, method, path, body):
    kwargs = {"headers": AUTH}
    if body is not None:
        kwargs["json"] = body
    assert owner.request(method, f"{BASE}{path}", **kwargs).status_code == 403


class TestCreate:
    def test_app_hook_round_trip(self, owner):
        r = owner.post(f"{BASE}/app", json=_bash(tool_matcher="Bash"))
        assert r.status_code == 201, r.text
        hook = r.json()
        assert hook["id"].startswith("hook-") and hook["project_id"] is None
        assert hook["tool_matcher"] == "Bash"
        assert [h["id"] for h in owner.get(f"{BASE}/app").json()] == [hook["id"]]
        assert owner.get(f"{BASE}/{hook['id']}").json()["command"] == "echo hi"

    def test_project_hook_lists_with_app_hooks(self, owner):
        app_hook = owner.post(f"{BASE}/app", json=_bash(name="global")).json()
        r = owner.post(f"{BASE}/project/proj-h",
                       json=_bash(hook_type="webhook", command=None, url="https://example.test/h"))
        assert r.status_code == 201, r.text
        assert r.json()["project_id"] == "proj-h"
        ids = {h["id"] for h in owner.get(f"{BASE}/project/proj-h").json()}
        assert ids == {app_hook["id"], r.json()["id"]}

    @pytest.mark.parametrize("path", ["/app", "/project/proj-h"])
    @pytest.mark.parametrize("over,detail", [
        ({"event": "OnSave"}, "Invalid event type: OnSave. Valid types are: "
                              + ", ".join(VALID_HOOK_EVENTS)),
        ({"command": None}, "Bash hooks must have a command"),
        ({"command": ""}, "Bash hooks must have a command"),
        ({"hook_type": "webhook", "command": None}, "Webhook hooks must have a URL"),
    ])
    def test_validation_is_400(self, owner, path, over, detail):
        r = owner.post(f"{BASE}{path}", json=_bash(**over))
        assert r.status_code == 400
        assert r.json()["detail"] == detail
        assert owner.get(f"{BASE}/app").json() == []

    @pytest.mark.parametrize("over", [
        {"hook_type": "python"},
        {"timeout": 999},
        {"timeout": 300001},
        {"name": ""},
        {"name": "n" * 101},
    ])
    def test_out_of_range_fields_are_422(self, owner, over):
        assert owner.post(f"{BASE}/app", json=_bash(**over)).status_code == 422

    def test_missing_project_is_404(self, owner):
        for method in ("GET", "POST"):
            kwargs = {"json": _bash()} if method == "POST" else {}
            r = owner.request(method, f"{BASE}/project/ghost", **kwargs)
            assert r.status_code == 404
            assert r.json()["detail"] == "Project not found: ghost"


class TestPerHook:
    @pytest.mark.parametrize("method,suffix,body", [
        ("GET", "", None),
        ("PATCH", "", {"enabled": False}),
        ("DELETE", "", None),
        ("POST", "/test", None),
    ])
    def test_missing_hook_is_404(self, owner, method, suffix, body):
        kwargs = {"json": body} if body is not None else {}
        r = owner.request(method, f"{BASE}/hook-missing{suffix}", **kwargs)
        assert r.status_code == 404
        assert r.json()["detail"] == "Hook not found: hook-missing"

    def test_patch_changes_only_sent_fields(self, owner):
        hook = owner.post(f"{BASE}/project/proj-h", json=_bash()).json()
        r = owner.patch(f"{BASE}/{hook['id']}", json={"enabled": False, "timeout": 5000})
        assert r.status_code == 200
        body = r.json()
        assert (body["enabled"], body["timeout"], body["command"]) == (False, 5000, "echo hi")

    def test_patch_unknown_event_is_400(self, owner):
        hook = owner.post(f"{BASE}/app", json=_bash()).json()
        r = owner.patch(f"{BASE}/{hook['id']}", json={"event": "Nope"})
        assert r.status_code == 400
        assert owner.get(f"{BASE}/{hook['id']}").json()["event"] == "PostToolUse"

    def test_delete_then_gone(self, owner):
        hook = owner.post(f"{BASE}/app", json=_bash()).json()
        assert owner.delete(f"{BASE}/{hook['id']}").status_code == 204
        assert owner.get(f"{BASE}/{hook['id']}").status_code == 404
        assert owner.delete(f"{BASE}/{hook['id']}").status_code == 404

    def test_test_runs_the_executor(self, owner, monkeypatch):
        from app.core import hook_executor

        seen = []

        async def fake_test(hook):
            seen.append(hook["id"])
            return {"success": True, "duration_ms": 3, "output": "ok", "error": None}

        monkeypatch.setattr(hook_executor, "test_hook", fake_test)
        hook = owner.post(f"{BASE}/app", json=_bash()).json()
        r = owner.post(f"{BASE}/{hook['id']}/test")
        assert r.status_code == 200
        assert r.json() == {"success": True, "duration_ms": 3, "output": "ok", "error": None}
        assert seen == [hook["id"]]


def test_plugin_hooks_are_listed(owner, monkeypatch):
    from app.core import plugin_service

    monkeypatch.setattr(plugin_service, "get_all_plugin_hooks", lambda: [{
        "plugin_id": "p@m", "plugin_name": "p", "event": "Stop", "hook_type": "command",
        "description": None, "tool_matcher": None,
    }])
    r = owner.get(f"{BASE}/plugins/all")
    assert r.status_code == 200
    assert [(h["plugin_id"], h["event"]) for h in r.json()] == [("p@m", "Stop")]
