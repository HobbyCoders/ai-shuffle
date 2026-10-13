"""Application update routes (``app.api.settings_updates``) through the real
app and the per-test SQLite database.

Reading the policy is open to any authenticated caller; changing it, asking
the updater to act, and listing workers are owner-only, and never available to
an agent's browser preview. The container updater is a temporary directory.
"""
import json

import pytest

from app.api import settings_updates
from app.api.auth import hash_api_key
from app.core import application_updates, update_workers
from app.db import database

URL = "/api/v1/settings/application-updates"
USER_KEY = "aih_updates_user_key"


@pytest.fixture
def updater(tmp_path, monkeypatch):
    root = tmp_path / "updater"
    root.mkdir()
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(root))
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(tmp_path / "policy.json"))
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setattr(update_workers.progress, "_path", lambda: tmp_path / "progress.json")
    monkeypatch.setattr(update_workers.runners, "directory", lambda: {"self_device_id": None, "runners": []})
    update_workers._receipts.clear()
    return root


@pytest.fixture
def c(authenticated_client, updater):
    return authenticated_client


@pytest.fixture
def api_user(mock_db):
    database.create_api_user("updates-user", "Updates User", hash_api_key(USER_KEY))
    return {"Authorization": f"Bearer {USER_KEY}"}


def test_status_reports_policy_and_updater_state(c, updater):
    (updater / "status.json").write_text(json.dumps({
        "phase": "available", "version": "2.0.0", "channel": "stable", "notes": "n", "secret": "not-a-field"}))
    body = c.get(URL).json()
    assert body["allow_updates"] is True and body["update_all_workers"] is False
    assert body["supported"] is True
    assert (body["phase"], body["version"], body["channel"], body["notes"]) == ("available", "2.0.0", "stable", "n")
    assert body["running_version"] == "abc1234"
    assert "secret" not in body
    # Reading the default never writes the synced policy.
    assert database.get_system_setting(application_updates.KEY) is None


def test_status_with_unreadable_updater_file_is_an_error_phase(c, updater):
    (updater / "status.json").write_text("{not json")
    body = c.get(URL).json()
    assert body["phase"] == "error"
    assert body["error"] == "Cannot read application updater status. Check the container logs."


def test_status_without_an_updater_is_unsupported(c, monkeypatch):
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES")
    body = c.get(URL).json()
    assert body["supported"] is False and body["phase"] == "idle"
    assert body["running_version"] is None


def test_policy_update_persists_and_mirrors(c, updater, tmp_path):
    r = c.put(URL, json={"allow_updates": False, "update_all_workers": True})
    assert r.status_code == 200
    assert (r.json()["allow_updates"], r.json()["update_all_workers"]) == (False, True)
    assert database.get_system_setting(application_updates.KEY) == "false"
    assert database.get_system_setting(application_updates.ALL_KEY) == "true"
    assert json.loads((tmp_path / "policy.json").read_text()) == {"allow_updates": False}


@pytest.mark.parametrize("body,detail", [
    ({}, "Supply an update setting"),
    ({"allow_updates": None}, "Supply an update setting"),
    ({"allow_updates": 1}, None),
    ({"allow_updates": "true"}, None),
    ({"allow_updates": True, "channel": "beta"}, None),
])
def test_policy_update_validation(c, body, detail):
    r = c.put(URL, json=body)
    assert r.status_code == 422, r.text
    if detail:
        assert r.json()["detail"] == detail
    assert database.get_system_setting(application_updates.KEY) is None


def test_action_queues_a_fixed_request_and_is_refused_when_disabled(c, updater):
    # Nothing found yet: the download is queued and the updater checks first (#1623).
    r = c.post(URL, json={"action": "download"})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert (result["accepted"], result["reason"]) == (True, "Checking for an update to download")
    assert json.loads((updater / "request.json").read_text())["action"] == "download"
    (updater / "request.json").unlink()
    r = c.post(URL, json={"action": "check"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "current"
    assert r.json()["results"][0]["device_name"] == "Current worker"
    queued = json.loads((updater / "request.json").read_text())
    assert queued["action"] == "check"
    c.put(URL, json={"allow_updates": False})
    assert not (updater / "request.json").exists()
    r = c.post(URL, json={"action": "install"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Application updates are disabled in Account settings"
    assert not (updater / "request.json").exists()


@pytest.mark.parametrize("action", ["shell", "rm -rf /", "", None])
def test_action_accepts_only_known_verbs(c, updater, action):
    assert c.post(URL, json={"action": action}).status_code == 422
    assert not (updater / "request.json").exists()


def test_current_scope_acts_on_this_worker_only(c, updater, monkeypatch):
    seen = []
    async def dispatch(action, mode=None, scope=None):
        seen.append((action, mode, scope))
        return {"scope": scope or "all", "sent": action, "results": []}
    monkeypatch.setattr("app.core.update_workers.dispatch", dispatch)
    r = c.post(URL, json={"action": "install", "mode": "when_idle", "scope": "current"})
    assert r.status_code == 200, r.text
    assert seen == [("install", "when_idle", "current")]
    for scope in ("all", "peer", ""):
        assert c.post(URL, json={"action": "install", "scope": scope}).status_code == 422
    assert len(seen) == 1


def test_workers_lists_this_worker_without_peer_endpoints(c):
    body = c.get(f"{URL}/workers").json()
    assert body["operation"] is None
    assert [w["device_name"] for w in body["workers"]] == ["Current worker"]
    assert set(body["workers"][0]) == {
        "device_id", "device_name", "is_self", "online", "supported", "allow_updates", "phase", "version",
        "running_version", "percent", "message", "error", "reason", "request_pending", "checked_at",
        "channel", "release", "notes", "role", "label", "deferred", "notice"}
    assert body["plan"]["action"] in ("check", "download", "install")


def test_api_users_may_read_but_not_change_or_act(client, api_user, updater):
    assert client.get(URL, headers=api_user).status_code == 200
    assert client.put(URL, json={"allow_updates": False}, headers=api_user).status_code == 403
    assert client.post(URL, json={"action": "install"}, headers=api_user).status_code == 403
    assert client.get(f"{URL}/workers", headers=api_user).status_code == 403
    assert database.get_system_setting(application_updates.KEY) is None
    assert not (updater / "request.json").exists()


def test_agent_preview_cannot_change_updates(c, updater, monkeypatch):
    monkeypatch.setattr(settings_updates.preview_access, "grant", lambda request: object())
    r = c.put(URL, json={"allow_updates": False})
    assert r.status_code == 403
    assert r.json()["detail"] == "Agent previews cannot change application updates"
    assert c.post(URL, json={"action": "check"}).status_code == 403
    assert database.get_system_setting(application_updates.KEY) is None
    assert not (updater / "request.json").exists()
