from fastapi import FastAPI
from fastapi.testclient import TestClient
import json
import pytest

from app.api import settings_updates as api
from app.api.auth import require_auth, require_admin
from app.core import application_updates as updates


def envelope(payload):
    """A manifest envelope; the API parses it, the installing side verifies its signature."""
    import base64
    return json.dumps({"payload": base64.b64encode(json.dumps(payload).encode()).decode(), "signature": "c2ln"})


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.core import update_workers
    monkeypatch.setattr(update_workers.progress, "_path", lambda: tmp_path / "progress.json")
    monkeypatch.setattr(update_workers.runners, "directory", lambda: {"self_device_id": None, "runners": []})
    update_workers._receipts.clear()
    app = FastAPI()
    app.include_router(api.updates_router, prefix="/settings")
    app.include_router(api.channel_router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    app.dependency_overrides[require_admin] = lambda: "owner"
    values = {}
    monkeypatch.setattr(updates.database, "get_system_setting", lambda key: values.get(key))
    monkeypatch.setattr(updates.database, "set_system_setting", lambda key, value: values.__setitem__(key, value))
    monkeypatch.delenv("UPDATE_CHANNEL", raising=False)
    monkeypatch.setenv("SHUFFLE_UPDATE_CHANNEL", "stable")
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    monkeypatch.setattr(updates, "_desktop_channel_request", None)
    monkeypatch.setattr(updates, "_desktop_stable_manifest", None)
    monkeypatch.setattr(updates, "_desktop_channel_error", None)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(tmp_path / "allow-updates.json"))
    monkeypatch.setattr(api.preview_access, "grant", lambda request: None)
    with TestClient(app) as client:
        yield client, tmp_path, values, app


def test_default_enabled_write_readback_and_real_queue(client):
    client, root, values, _ = client
    assert client.get("/settings/application-updates").json()["allow_updates"] is True
    assert values == {}, "reading the default must not overwrite synced policy"
    assert client.get("/settings/application-updates").json()["update_all_workers"] is False
    assert client.put("/settings/application-updates", json={"update_all_workers": True}).json()["update_all_workers"] is True
    assert client.put("/settings/application-updates", json={"update_all_workers": False}).status_code == 200
    assert client.post("/settings/application-updates", json={"action": "check"}).status_code == 200
    queued = json.loads((root / "request.json").read_text())
    assert queued["action"] == "check" and queued["expires_at"] > 0
    assert client.put("/settings/application-updates", json={"allow_updates": False}).json()["allow_updates"] is False
    assert values == {"allow_updates": "false", "update_all_workers": "false"}
    assert client.get("/settings/application-updates").json()["allow_updates"] is False
    assert json.loads((root / "allow-updates.json").read_text()) == {"allow_updates": False}
    assert client.post("/settings/application-updates", json={"action": "install"}).status_code == 409
    assert client.put("/settings/application-updates", json={"allow_updates": True}).status_code == 200
    assert client.post("/settings/application-updates", json={"action": "install"}).status_code == 200


def test_preview_cannot_change_policy_or_request_code_execution(client, monkeypatch):
    client, root, values, _ = client
    monkeypatch.setattr(api.preview_access, "grant", lambda request: object())
    assert client.put("/settings/application-updates", json={"allow_updates": False}).status_code == 403
    assert client.post("/settings/application-updates", json={"action": "download"}).status_code == 403
    assert not values and not (root / "request.json").exists()


def test_schema_rejects_coerced_boolean_and_arbitrary_commands(client):
    client, _, _, _ = client
    assert client.put("/settings/application-updates", json={"allow_updates": "false"}).status_code == 422
    assert client.post("/settings/application-updates", json={"action": "shell"}).status_code == 422




def test_channel_file_restart_and_no_downgrade(client, monkeypatch):
    client, root, _, _ = client
    from app.core import stable_return
    # This container runs code ahead of the stable release: going back is refused.
    monkeypatch.setenv("GIT_SHA", "a" * 40)
    (root / "versions" / ("a" * 40)).mkdir(parents=True)
    (root / "versions" / ("a" * 40) / "release.json").write_text(
        envelope({"revision": "a" * 40, "channel": "nightly", "container": {"sequence": 20}}))
    monkeypatch.setattr(stable_return, "fetch_stable", lambda: ("signed", {
        "revision": "b" * 40, "channel": "stable", "version": "1.0.2", "sequence": 19}))
    assert client.get("/api/v1/updates/channel").json() == {
        "channel": "stable", "available": ["stable", "nightly"], "source": "default",
        "editable": True, "restart_required": False}
    result = client.put("/api/v1/updates/channel", json={"channel": "nightly"})
    assert result.status_code == 200
    assert result.json()["restart_required"] is True
    assert json.loads((root / "update-channel.json").read_text()) == {"channel": "nightly"}
    from app.core.update_channel import selection
    assert selection(root, {}) == ("nightly", "file")  # supervisor consumes the real write
    assert client.get("/api/v1/updates/channel").json()["source"] == "file"
    refused = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert refused.status_code == 409 and "newer than the current stable release (1.0.2)" in refused.json()["detail"]
    assert json.loads((root / "update-channel.json").read_text()) == {"channel": "nightly"}
    assert client.put("/api/v1/updates/channel", json={"channel": "beta"}).status_code == 422


def test_channel_env_wins_and_alias(client, monkeypatch):
    client, root, _, _ = client
    (root / "update-channel.json").write_text('{"channel":"stable"}')
    monkeypatch.setenv("UPDATE_CHANNEL", "edge")
    result = client.get("/api/v1/updates/channel").json()
    assert result["channel"] == "nightly" and result["source"] == "env" and not result["editable"]
    assert client.put("/api/v1/updates/channel", json={"channel": "nightly"}).status_code == 409
    monkeypatch.delenv("UPDATE_CHANNEL")
    assert client.put("/api/v1/updates/channel", json={"channel": "edge"}).json()["channel"] == "nightly"


def test_channel_owner_and_preview_guards(client, monkeypatch):
    client, _, _, app = client
    monkeypatch.setattr(api.preview_access, "grant", lambda request: object())
    assert client.get("/api/v1/updates/channel").status_code == 403
    assert client.put("/api/v1/updates/channel", json={"channel": "nightly"}).status_code == 403
    monkeypatch.setattr(api.preview_access, "grant", lambda request: None)
    app.dependency_overrides.pop(require_admin)
    assert client.get("/api/v1/updates/channel").status_code == 403
    assert client.put("/api/v1/updates/channel", json={"channel": "nightly"}).status_code == 403


def test_desktop_channel_write_waits_for_bridge_ack(client, monkeypatch):
    client, _, _, _ = client
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES")
    stable = {"phase": "idle", "channel_config": {"channel": "stable", "source": "default", "editable": True}}
    updates.desktop_exchange(stable)
    accepted = client.put("/api/v1/updates/channel", json={"channel": "nightly"})
    assert accepted.status_code == 202 and accepted.json()["channel"] == "stable"
    assert client.put("/api/v1/updates/channel", json={"channel": "stable"}).status_code == 409
    first = updates.desktop_exchange(stable)
    assert first["action"] == "set_channel" and first["channel"] == "nightly"
    assert updates.desktop_exchange(stable)["channel"] == "nightly"  # lost reply retries
    updates.desktop_exchange({"phase": "idle", "channel_config": {"channel": "nightly", "source": "file", "editable": True}})
    assert client.get("/api/v1/updates/channel").json()["channel"] == "nightly"
    assert updates._desktop_channel_request is None


def test_only_owner_can_mutate(client):
    client, _, _, app = client
    app.dependency_overrides.pop(require_admin)
    assert client.put("/settings/application-updates", json={"allow_updates": False}).status_code == 403


A, B = "a" * 40, "b" * 40
STABLE = {"revision": B, "channel": "stable", "version": "1.0.2", "sequence": 50}


def _nightly_container(root, monkeypatch, sequence, stable=STABLE):
    from app.core import stable_return
    (root / "update-channel.json").write_text('{"channel": "nightly"}')
    monkeypatch.setenv("SHUFFLE_UPDATE_CHANNEL", "nightly")
    monkeypatch.setenv("GIT_SHA", A)
    if sequence is not None:
        (root / "versions" / A).mkdir(parents=True)
        (root / "versions" / A / "release.json").write_text(
            envelope({"revision": A, "channel": "nightly", "container": {"sequence": sequence}}))
    monkeypatch.setattr(stable_return, "fetch_stable", lambda: ("signed", stable))


def test_container_goes_back_to_stable_when_not_ahead_of_it(client, monkeypatch):
    client, root, _, _ = client
    _nightly_container(root, monkeypatch, 49)
    result = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert result.status_code == 200 and result.json()["channel"] == "stable"
    assert json.loads((root / "update-channel.json").read_text()) == {"channel": "stable"}


def test_container_on_the_stable_revision_goes_back_and_equal_order_elsewhere_does_not(client, monkeypatch):
    client, root, _, _ = client
    _nightly_container(root, monkeypatch, 50, {**STABLE, "revision": A})
    assert client.put("/api/v1/updates/channel", json={"channel": "stable"}).status_code == 200
    from app.core import stable_return
    (root / "update-channel.json").write_text('{"channel": "nightly"}')
    monkeypatch.setattr(stable_return, "fetch_stable", lambda: ("signed", STABLE))
    assert client.put("/api/v1/updates/channel", json={"channel": "stable"}).status_code == 409


def test_container_image_code_and_staged_updates(client, monkeypatch, tmp_path_factory):
    client, root, _, _ = client
    _nightly_container(root, monkeypatch, None)
    image = tmp_path_factory.mktemp("image")
    (image / "image.json").write_text(json.dumps({"format": 2, "revision": A, "sequence": 40}))
    monkeypatch.setenv("SHUFFLE_IMAGE_DIR", str(image))
    (root / "state.json").write_text(json.dumps({"pending": B}))
    refused = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert refused.status_code == 409 and "current update" in refused.json()["detail"]
    (root / "state.json").write_text("{}")
    assert client.put("/api/v1/updates/channel", json={"channel": "stable"}).status_code == 200


def test_unreachable_feed_keeps_nightly(client, monkeypatch):
    client, root, _, _ = client
    from app.core import stable_return
    _nightly_container(root, monkeypatch, 1)

    def down():
        raise stable_return.Refused("Could not reach the update feed to compare this worker with the current stable release.")
    monkeypatch.setattr(stable_return, "fetch_stable", down)
    refused = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert refused.status_code == 409 and "Could not reach" in refused.json()["detail"]
    assert json.loads((root / "update-channel.json").read_text()) == {"channel": "nightly"}


def _desktop(monkeypatch, installed=None):
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES")
    state = {"phase": "idle", "channel_config": {"channel": "nightly", "source": "file", "editable": True}}
    if installed:
        state["installed_release"] = installed
    updates.desktop_exchange(state)
    return state


def test_desktop_return_to_stable_hands_the_checked_manifest_to_the_shell(client, monkeypatch):
    client, _, _, _ = client
    from app.core import stable_return
    nightly = _desktop(monkeypatch, {"revision": A, "container_sequence": 49, "version": "1.0.2-dev.1"})
    monkeypatch.setattr(stable_return, "fetch_stable", lambda: ("signed-stable", STABLE))
    accepted = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert accepted.status_code == 202 and accepted.json()["channel"] == "nightly"
    request = updates.desktop_exchange(nightly)
    assert request["action"] == "set_channel" and request["channel"] == "stable"
    assert request["stable_manifest"] == "signed-stable"
    # The shell refused it: the request is dropped and the picker is told why.
    assert updates.desktop_exchange({**nightly, "channel_error": "This app runs newer code"}) == {}
    assert client.get("/api/v1/updates/channel").json()["error"] == "This app runs newer code"
    assert updates._desktop_channel_request is None and updates.desktop_exchange(nightly) == {}
    # A new attempt clears the old refusal and is acknowledged once the shell saves it.
    client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert client.get("/api/v1/updates/channel").json()["error"] is None
    assert updates.desktop_exchange(nightly)["stable_manifest"] == "signed-stable"
    updates.desktop_exchange({**nightly, "channel_config": {"channel": "stable", "source": "file", "editable": True}})
    assert updates._desktop_channel_request is None and updates._desktop_stable_manifest is None
    assert client.get("/api/v1/updates/channel").json()["channel"] == "stable"


def test_desktop_ahead_of_stable_or_too_old_to_check_is_refused_at_once(client, monkeypatch):
    client, _, _, _ = client
    from app.core import stable_return
    monkeypatch.setattr(stable_return, "fetch_stable", lambda: ("signed-stable", STABLE))
    _desktop(monkeypatch)
    old = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert old.status_code == 409 and "Install the next update" in old.json()["detail"]
    _desktop_state = {"phase": "idle", "channel_config": {"channel": "nightly", "source": "file", "editable": True},
                      "installed_release": {"revision": A, "container_sequence": 51, "version": None}}
    updates.desktop_exchange(_desktop_state)
    ahead = client.put("/api/v1/updates/channel", json={"channel": "stable"})
    assert ahead.status_code == 409 and "newer than the current stable release (1.0.2)" in ahead.json()["detail"]
    assert updates._desktop_channel_request is None
    # Its installer's own build: a final version not past stable's is enough.
    updates.desktop_exchange({**_desktop_state, "installed_release": {"revision": A, "container_sequence": None, "version": "1.0.1"}})
    assert client.put("/api/v1/updates/channel", json={"channel": "stable"}).status_code == 202


def test_bridge_model_accepts_installed_release_and_bounds_it():
    from pydantic import ValidationError
    from app.api.internal import DesktopUpdateState
    state = DesktopUpdateState(phase="idle", installed_release={"revision": A, "container_sequence": 7, "version": "1.0.2"},
                               channel_error="refused")
    assert state.model_dump()["installed_release"]["container_sequence"] == 7
    for bad in ({"revision": "x"}, {"revision": A, "container_sequence": 0}, {"revision": A, "version": "v" * 81}):
        with pytest.raises(ValidationError):
            DesktopUpdateState(phase="idle", installed_release=bad)
