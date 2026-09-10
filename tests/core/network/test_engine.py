"""Two and three workers converging through the real engine."""
import pytest

from app.db import database
from app.db import network as store
from app.network import crypto, engine, membership
from tests.core.network.harness import Worker, converge, exchange, join


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()


def _set_pref(worker, key, value):
    with worker.active():
        from app.core import owner_auth
        database.set_user_preference("admin", owner_auth.preference_identity(), key, value)
        engine.scan_all()


def _pref(worker, key):
    with worker.active():
        from app.core import owner_auth
        row = database.get_user_preference("admin", owner_auth.preference_identity(), key)
        return row["value"] if row else None


@pytest.mark.asyncio
async def test_account_preferences_reach_fresh_worker_without_controller(workers, monkeypatch):
    from fastapi import Request
    from app.api import preferences as api
    from app.core import account_hub, encryption, preference_identity
    from app.core.config import settings
    from app.db import mobile_hub
    a, b = workers("account-source"), workers("fresh-desktop")
    account_id = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(settings, "mobile_account_hub_url", "https://hub.example.com")
    monkeypatch.setattr(account_hub, "call", lambda *args, **kwargs: {"account": {"id": account_id}})
    preference_identity._owner_cache.clear()
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    request = Request({"type": "http", "headers": []})
    request.state.is_admin = True
    values = {
        "composer.profile": "coding", "composer.permissionMode": "bypassPermissions",
        "composer.model": "prov:provider:model", "composer.effort": "high", "composer.fastMode": "1",
        "composer.contextWindowChoice": "standard", "composer.worktreeIntent": "worktree",
        "theme": {"preference": "dark", "presetId": "vercel"}, "timezone": "local",
        "chat_behavior": {"showThinkingBlocks": False}, "hotkey_settings": {"globalSttEnabled": False},
        "git_action_settings": {"mergeMethod": "squash"}, "notification_sound": False,
        "model_favorites": ["prov:provider:model"], "files_preferences": {"showHidden": True},
        "layout": {"sidebarWidth": 393}, "sidebar_workspace": {"shown": ["notes", "settings"]},
        "groups": {}, "context_usage_sections": {"plan": False}, "context_breakdown_open": False,
        "sidebar_collapsed_groups": {}, "build_subtab": "hooks", "session_mode": "code",
        "browser_panel": {"width": 550}, "install_prompt": {"never": True},
    }
    for worker, server in [(a, "11111111-1111-1111-1111-111111111111"),
                           (b, "33333333-3333-3333-3333-333333333333")]:
        with worker.active():
            mobile_hub.bind("https://hub.example.com", server, encryption.encrypt_value("test-secret"),
                            "managed:" + settings.effective_instance_id)
            mobile_hub.activate()
    with a.active():
        engine.create_network("Account")
        for key, value in values.items():
            await api.set_preference(request, key, api.PreferenceValue(key=key, value=value))
        engine.scan_all()
    join(a, b)
    with b.active():
        assert preference_identity.local_owner_account() == account_id
        result = await api.get_all_preferences(request)
        assert all(result.preferences[key] == value for key, value in values.items())
        await api.set_preference(request, "composer.fastMode", api.PreferenceValue(key="composer.fastMode", value=""))
        engine.scan_all()
    exchange(b, a)
    with a.active():
        assert (await api.get_preference(request, "composer.fastMode")).value == ""


@pytest.mark.parametrize("state", ["disconnected", "connecting", "reconnecting", "connected"])
def test_presence_advertises_only_verified_cloud_route(workers, monkeypatch, state):
    from app.core import cluster, managed_connection
    a, b = workers("docker"), workers("desktop")
    lan, cloud = "http://172.17.0.11:8000", "https://worker.example.com"
    monkeypatch.setattr(cluster, "advertise_url", lambda: (lan, "guess"))
    monkeypatch.setattr(managed_connection, "status", lambda: {"status": state, "url": cloud})
    with a.active():
        engine.create_network("Home")
        engine.publish_presence(version="test")
        endpoints = engine.presence_of(a.device_id)["endpoints"]
        assert endpoints == ([lan, cloud] if state == "connected" else [lan])
    join(a, b)
    with b.active():
        assert engine.presence_of(a.device_id)["endpoints"] == endpoints


def test_remote_catalog_updates_refresh_account_and_paired_views(workers, monkeypatch):
    from unittest.mock import Mock
    from app.core.global_events import global_events
    from app.network import startup
    a, b = workers("catalog-source"), workers("catalog-reader")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    monkeypatch.setattr(engine, "_listeners", [startup._on_applied])
    emit = Mock()
    monkeypatch.setattr(global_events, "resources_changed", emit)
    with a.active():
        database.set_system_setting("tts_model", "synced-voice")
        database.create_profile("agent-sync", "My agent", "", {"system_prompt": "Test agent"})
        engine.scan_all()
    emit.reset_mock()
    exchange(a, b)
    for resource in ("settings", "agents"):
        for kind in ("admin", "api_user", "account", "paired"):
            emit.assert_any_call(resource, user_type=kind)
    with b.active():
        assert database.get_system_setting("tts_model") == "synced-voice"
        assert database.get_profile("agent-sync")["name"] == "My agent"


def test_network_of_one_records_and_scans(workers):
    a = workers("a")
    with a.active():
        ctx = engine.create_network("Home")
        assert ctx.founder_device_id == a.device_id and ctx.key_version == 1
        assert engine.context().summary()["member_count"] == 1
        assert store.count_ops() == 1  # the founder certificate
        from app.core import owner_auth
        database.set_user_preference("admin", owner_auth.preference_identity(), "layout", {"sidebarWidth": 320})
        assert engine.scan_all() >= 1
        # A second scan publishes nothing.
        assert engine.scan_collection("user_preferences") == 0
        ops = store.ops_since({})
        pref_ops = [o for o in ops if o["collection"] == "user_preferences"]
        assert len(pref_ops) == 1 and pref_ops[0]["record_id"] == "admin|@owner|layout"
        assert pref_ops[0]["origin_device_id"] == a.device_id and pref_ops[0]["origin_seq"] >= 2
        # A local-only setting never leaves.
        database.set_system_setting("cluster_secret", "x")
        database.set_system_setting("tts_model", "voice-1")
        engine.scan_all()
        settings_ops = [o["record_id"] for o in store.ops_since({}) if o["collection"] == "system_settings"]
        assert "tts_model" in settings_ops and "cluster_secret" not in settings_ops
        with pytest.raises(RuntimeError):
            engine.create_network("Again")


def test_join_snapshot_and_tail_converge(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        from app.core import owner_auth
        database.set_user_preference("admin", owner_auth.preference_identity(), "layout", {"sidebarWidth": 320})
        database.set_system_setting("tts_model", "voice-1")
        database.create_tag(tag_id="t1", name="urgent", color="#f00")
        engine.scan_all()
    join(a, b)
    with b.active():
        ctx = engine.context()
        assert ctx.bootstrap_state == "ready" and ctx.device_id == b.device_id
        assert {m.device_id for m in ctx.active_members()} == {a.device_id, b.device_id}
        assert database.get_system_setting("tts_model") == "voice-1"
        assert database.get_tag("t1")["name"] == "urgent"
    assert _pref(b, "layout") == {"sidebarWidth": 320}

    # A membership record for B is now known to A and published.
    with a.active():
        assert engine.context().member(b.device_id).status == "active"
    # Live change on A reaches B within one exchange.
    _set_pref(a, "layout", {"sidebarWidth": 400})
    result = exchange(a, b)
    assert result.applied == 1 and result.rejected == 0
    assert _pref(b, "layout") == {"sidebarWidth": 400}
    # And B's change reaches A.
    _set_pref(b, "theme", {"preference": "dark"})
    exchange(b, a)
    assert _pref(a, "theme") == {"preference": "dark"}
    # Deletion propagates as a tombstone; the second delivery is a duplicate.
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
    assert exchange(a, b).applied == 1
    with b.active():
        assert database.get_tag("t1") is None
    again = exchange(a, b)
    assert again.applied == 0 and again.duplicate == 0  # watermark already past it
    # Snapshot-applied rows are not echoed back as new ops from B.
    with b.active():
        assert engine.scan_all() == 0


def test_partitioned_edits_resolve_by_clock_with_conflicts_on_both(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _set_pref(a, "layout", {"sidebarWidth": 1})
    converge([a, b])
    # Both edit the same key while partitioned.
    _set_pref(a, "layout", {"sidebarWidth": 100})
    _set_pref(b, "layout", {"sidebarWidth": 200})
    _set_pref(b, "layout", {"sidebarWidth": 201})  # B's clock is now ahead
    converge([a, b])
    assert _pref(a, "layout") == _pref(b, "layout") == {"sidebarWidth": 201}
    with a.active():
        conflicts = store.list_conflicts()
        assert conflicts and conflicts[0]["record_id"] == "admin|@owner|layout"
        assert conflicts[0]["losing_record"]["value"] == {"sidebarWidth": 100}
    with b.active():
        conflicts = store.list_conflicts()
        assert conflicts and conflicts[0]["losing_device_id"] == a.device_id
    # Nothing is left to say once converged.
    for worker in (a, b):
        with worker.active():
            assert engine.scan_all() == 0
    assert exchange(a, b).applied == 0 and exchange(b, a).applied == 0


def test_secret_added_on_a_is_usable_on_c_joined_from_b(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
        database.create_model_provider(provider_id="openai", name="OpenAI", provider_type="openai",
                                       base_url="https://api.openai.com/v1", auth_token="sk-secret")
        engine.scan_all()
        row = database.get_model_provider("openai", include_token=True)
        assert row["auth_token"] == "sk-secret"
    join(a, b)
    converge([a, b])
    # A goes away; C joins from B alone.
    join(b, c)
    with c.active():
        row = database.get_model_provider("openai", include_token=True)
        assert row["auth_token"] == "sk-secret"
        assert engine.context().member(a.device_id).status == "active"


def test_same_operation_over_two_routes_applies_once(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    _set_pref(a, "layout", {"sidebarWidth": 7})
    exchange(a, b)   # route 1: direct
    exchange(a, c)
    forwarded = exchange(c, b)  # route 2: forwarded by C
    assert forwarded.applied == 0 and forwarded.rejected == 0
    assert _pref(b, "layout") == {"sidebarWidth": 7}
    with b.active():
        assert len([o for o in store.ops_since({}) if o["collection"] == "user_preferences"]) == 1


def test_out_of_order_delivery_is_held_until_the_hole_fills(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    _set_pref(a, "k1", 1)
    _set_pref(a, "k2", 2)
    _set_pref(a, "k3", 3)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        ops = [o for o in store.ops_since(marks, exclude_origin=b.device_id) if o["collection"] == "user_preferences"]
    assert len(ops) == 3
    with b.active():
        first = engine.apply_batch([ops[2], ops[1]])
        assert first.pending == 2 and first.applied == 0
        assert store.pending_holes()[0]["count"] == 2
        second = engine.apply_batch([ops[0]])
        assert second.applied == 3 and store.pending_holes() == []
    assert _pref(b, "k3") == 3


def test_revoked_device_is_rejected_and_cannot_read_new_records(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    # B goes offline and keeps writing.
    _set_pref(b, "offline_write", True)
    with a.active():
        outcome = engine.revoke_device(b.device_id, reason="lost laptop")
        assert outcome["key_version"] == 2
        assert engine.context().member(b.device_id).status == "revoked"
    converge([a, c])
    with c.active():
        assert engine.context().member(b.device_id).status == "revoked"
        assert engine.context().key_version == 2
        assert 2 in engine.context().keyring
    # B's post-revocation write is rejected by every member.
    for member in (a, c):
        with b.active():
            ops = store.ops_since({b.device_id: {}}, exclude_origin=member.device_id)
            ops = [o for o in ops if o["origin_device_id"] == b.device_id and o["collection"] == "user_preferences"]
        with member.active():
            result = engine.apply_batch(ops)
            assert result.applied == 0 and result.rejected >= 1 and "origin revoked" in result.reasons
    # New records use key version 2, which B does not hold.
    _set_pref(a, "after", "rotation")
    with a.active():
        op = [o for o in store.ops_since({}) if o["record_id"] == "admin|@owner|after"][0]
        assert op["key_version"] == 2
    with b.active():
        result = engine.apply_batch([op])
        assert result.rejected == 1 and "unknown key version 2" in result.reasons
    # C still converges with A on the new key.
    converge([a, c])
    assert _pref(c, "after") == "rotation"


def test_recovery_certificate_admits_a_device_when_everyone_is_off(workers):
    a, d = workers("a"), workers("d")
    with a.active():
        ctx = engine.create_network("Home")
        network_key = ctx.network_key
        network_id = ctx.network_id
        founder = ctx.founder_device_id
        records = [r["record"] for r in store.list_membership_records()]
        database.set_system_setting("tts_model", "voice-9")
        engine.scan_all()
        manifest = engine.snapshot_manifest()
        chunks = [engine.snapshot_chunk(c, 0) for c in manifest["collections"]]
    # D unlocks with the network key (as the recovery flow does) and self-certifies.
    with d.active():
        ident = d.identity
        cert = membership.issue_recovery_certificate(
            network_id=network_id, network_key=network_key, signing_private=ident.signing_private,
            encryption_public=ident.encryption_public, name="Restored", kind="worker", generation=1)
        response = {
            "network_id": network_id, "name": "Home", "founder_device_id": founder, "generation": 1,
            "key_version": 1, "keyring": {"1": crypto.wrap_key(ident.encryption_public, network_key)},
            "certificate": cert, "membership_records": records, "members": [], "legacy_instance_map": {},
        }
        engine.enroll_from_response(response, device_name="Restored")
        for chunk in chunks:
            engine.apply_snapshot_chunk(chunk)
        engine.adopt_watermarks(manifest["watermarks"])
        engine.finish_bootstrap()
        assert database.get_system_setting("tts_model") == "voice-9"
        assert engine.context().member(d.device_id).status == "active"
    # A later-online worker accepts D purely from the ops D publishes.
    _set_pref(d, "from_d", 1)
    result = exchange(d, a)
    assert result.rejected == 0 and result.applied >= 1
    with a.active():
        assert engine.context().member(d.device_id).status == "active"
    assert _pref(a, "from_d") == 1


def test_bad_envelopes_are_rejected_without_side_effects(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    _set_pref(a, "x", 1)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        [op] = [o for o in store.ops_since(marks, exclude_origin=b.device_id)]
    with b.active():
        forged = dict(op, clock=op["clock"] + 5)
        result = engine.apply_batch([forged, dict(op, network_id="net_other"), {"garbage": True}])
        assert result.rejected == 3 and "bad signature" in result.reasons and "wrong network" in result.reasons
        assert store.get_watermark(store.get_db().__enter__().cursor(), a.device_id, 1) < op["origin_seq"]
        assert engine.apply_batch([op]).applied == 1
    assert _pref(b, "x") == 1


def test_stale_incarnation_is_rejected(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    _set_pref(a, "x", 1)
    with a.active():
        [op] = [o for o in store.ops_since({a.device_id: {"1": 1}}, exclude_origin=b.device_id)
                if o["collection"] == "user_preferences"]
    with b.active():
        with store.get_db() as conn:
            store.set_watermark(conn.cursor(), a.device_id, 2, 0)
        result = engine.apply_batch([op])
        assert result.rejected == 1 and "stale incarnation" in result.reasons
