"""Committed replicated writes wake targeted publication, not a periodic scan."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import owner_auth, resource_sync
from app.db import database, settings
from app.db import network as store
from app.network import engine, policy, startup, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_scan_pacing import ScanClock


@pytest.fixture
def resource_hook(monkeypatch):
    monkeypatch.setattr(resource_sync, "_emit", resource_sync._emit)
    monkeypatch.setattr(resource_sync, "_network_hooked",
                        getattr(resource_sync, "_network_hooked", False), raising=False)
    startup._install_resource_hook()
    wake = Mock(wraps=transport.request_scan)
    monkeypatch.setattr(transport, "request_scan", wake)
    return wake


@pytest.mark.parametrize("writer", ["put", "post", "delete", "setter", "deleter", "model_clear"])
async def test_preference_write_publishes_one_signed_op_and_applies_on_peer(
        workers, monkeypatch, resource_hook, writer):
    from app.api import preferences

    a, b = workers("source"), workers("peer")
    with a.active():
        engine.create_network("Preference wakes")
        # Use different actual principal namespaces on each worker.
        a.sql("INSERT INTO system_settings(key,value) VALUES ('owner_preference_identity','source-owner')")
    with b.active():
        b.sql("INSERT INTO system_settings(key,value) VALUES ('owner_preference_identity','peer-owner')")
    join(a, b)
    deleting = writer in ("delete", "deleter")
    key = "composer.modelEffort.example" if writer == "model_clear" else "theme"
    if deleting or writer == "model_clear":
        with a.active():
            settings.set_user_preference("admin", owner_auth.preference_identity(), key, "old")
            engine.scan_collection("user_preferences")
        converge([a, b])

    app = FastAPI()
    app.include_router(preferences.router)
    app.dependency_overrides[preferences.require_preference_write] = lambda: "authenticated"
    monkeypatch.setattr(preferences, "get_user_identity", lambda request: ("admin", "source-owner"))
    clock = ScanClock(monkeypatch, passes=2)
    original_scan = engine.scan_all
    scans = Mock(side_effect=lambda **kwargs: original_scan(**kwargs) if kwargs else 0)
    monkeypatch.setattr(engine, "scan_all", scans)
    kick = Mock(wraps=engine.kick)
    monkeypatch.setattr(engine, "kick", kick)
    resource_hook.reset_mock()

    with a.active(), TestClient(app) as client:
        marks = store.local_watermarks()

        def write():
            if writer in ("put", "post"):
                response = getattr(client, writer)(f"/api/v1/preferences/{key}",
                                                  json={"key": key, "value": "new"})
                assert response.status_code == 200
            elif writer == "delete":
                assert client.delete(f"/api/v1/preferences/{key}").json() == {"deleted": True}
            elif writer == "setter":
                settings.set_user_preference("admin", "source-owner", key, "new")
            else:
                settings.delete_user_preference("admin", "source-owner", key)

        clock.actions = [(1, write)]
        await clock.run()
        # Ratchet: the initial quiet pass cannot discover the later write.
        # Only the write's targeted wake may publish, before any periodic scan.
        assert scans.call_args_list[1].kwargs == {
            "collections": {"user_preferences"}, "include_catalog": False, "strict": True}
        resource_hook.assert_called_once_with(["user_preferences"], session_ids=None)
        assert kick.call_count == 1
        ops = store.ops_since(marks, limit=10, exclude_origin=b.device_id)
        assert len(ops) == 1 and ops[0]["signature"]
        assert ops[0]["record_id"] == f"admin|{policy.OWNER_PLACEHOLDER}|{key}"
    with b.active():
        result = engine.apply_batch(ops)  # Real signature validation/materialization.
        assert result.applied == 1 and result.rejected == 0
        row = settings.get_user_preference("admin", "peer-owner", key)
        if deleting:
            assert row is None
        else:
            assert row["value"] == (None if writer == "model_clear" else "new")
        assert settings.get_user_preference("admin", "source-owner", key) is None


@pytest.mark.parametrize("route", ["chat-instructions", "security-instructions"])
async def test_instruction_reset_api_publishes_a_tombstone_and_missing_reset_does_not_wake(
        workers, monkeypatch, resource_hook, route):
    from app.api import settings as settings_api

    a, b = workers("instruction-source"), workers("instruction-peer")
    with a.active():
        engine.create_network("Instruction reset wakes")
    join(a, b)
    with a.active():
        database.set_system_setting("security_instructions", "Customized instructions")
        engine.scan_collection("system_settings")
    converge([a, b])
    with b.active():
        assert database.get_system_setting("security_instructions") == "Customized instructions"

    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[settings_api.require_admin] = lambda: "authenticated"
    clock = ScanClock(monkeypatch, passes=2)
    original_scan = engine.scan_all
    scans = Mock(side_effect=lambda **kwargs: original_scan(**kwargs) if kwargs else 0)
    monkeypatch.setattr(engine, "scan_all", scans)
    kick = Mock(wraps=engine.kick)
    monkeypatch.setattr(engine, "kick", kick)
    resource_hook.reset_mock()

    with a.active(), TestClient(app) as client:
        marks = store.local_watermarks()

        def reset():
            response = client.delete(f"/api/v1/settings/{route}")
            assert response.status_code == 200 and response.json()["success"]

        clock.actions = [(1, reset)]
        await clock.run()
        expected = {"system_settings", "model_providers", "permission_rules"}
        assert resource_hook.call_count == 1
        assert set(resource_hook.call_args.args[0]) == expected
        assert scans.call_args_list[1].kwargs == {
            "collections": expected, "include_catalog": False, "strict": True}
        assert kick.call_count == 1
        ops = store.ops_since(marks, limit=10, exclude_origin=b.device_id)
        assert len(ops) == 1 and ops[0]["signature"]
        assert ops[0]["record_id"] == "security_instructions"
        assert database.get_system_setting("security_instructions") is None

        # Reset is still a successful API response when already at the default,
        # but an absent row must not wake publication or mint another tombstone.
        marks = store.local_watermarks()
        resource_hook.reset_mock()
        kick.reset_mock()
        reset()
        resource_hook.assert_not_called()
        kick.assert_not_called()
        assert store.ops_since(marks, limit=10, exclude_origin=b.device_id) == []
    with b.active():
        result = engine.apply_batch(ops)
        assert result.applied == 1 and result.rejected == 0
        assert database.get_system_setting("security_instructions") is None


def test_preference_noops_conflicts_and_failed_commit_do_not_wake(monkeypatch, resource_hook):
    settings.set_user_preference("account", "owner", "theme", "old")
    old = settings.get_user_preference("account", "owner", "theme")
    resource_hook.reset_mock()
    assert settings.set_user_preference("account", "owner", "theme", "ignored",
                                        only_if_missing=True)["revision"] == old["revision"]
    with pytest.raises(settings.PreferenceConflict):
        settings.set_user_preference("account", "owner", "theme", "stale", expected_version=None)
    assert not settings.delete_user_preference("account", "owner", "absent")
    resource_hook.assert_not_called()

    real_get_db = settings.get_db

    @contextmanager
    def failing_commit():
        with real_get_db() as conn:
            yield conn
            raise RuntimeError("commit failed")

    monkeypatch.setattr(settings, "get_db", failing_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        settings.set_user_preference("account", "owner", "theme", "uncommitted")
    resource_hook.assert_not_called()


@pytest.mark.parametrize("kind,expected", [
    ("artifact", {"artifacts"}),
    ("permission", {"system_settings", "model_providers", "permission_rules"}),
    ("session_tag", set(policy.CATALOG_COLLECTIONS)),
])
def test_other_replicated_writer_boundaries_wake_their_own_collection(
        workers, resource_hook, kind, expected):
    from app.db import artifacts, subagents, tags

    worker = workers("audit")
    with worker.active():
        engine.create_network("Resource boundary inventory")
        database.create_session(session_id="chat", profile_id="default", title="Chat")
        tags.create_tag("tag", "Tag")
        resource_hook.reset_mock()
        if kind == "artifact":
            artifacts.upsert_artifact("a" * 64, 1, "text/plain", "example.txt", "chat")
        elif kind == "permission":
            subagents.add_permission_rule("default", "Read", None, "allow")
        else:
            assert tags.add_session_tag("chat", "tag")
        assert resource_hook.call_count == 1
        assert set(resource_hook.call_args.args[0]) == expected
        if kind == "artifact":
            resource_hook.reset_mock()
            artifacts.upsert_artifact("a" * 64, 1, "text/plain", "example.txt", "chat")
            resource_hook.assert_not_called()


@pytest.mark.parametrize("value,enabled", [("", True), ("1", True), ("0", False),
                                           ("false", False), (" OFF ", False), ("no", False)])
def test_resource_wake_kill_switch(monkeypatch, value, enabled):
    monkeypatch.setenv("AI_SHUFFLE_PERF_RESOURCE_WAKE", value)
    assert resource_sync.replicated_wakes_enabled() is enabled
