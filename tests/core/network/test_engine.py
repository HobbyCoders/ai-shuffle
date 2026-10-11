"""Two and three workers converging through the real engine."""
import pytest

from app.db import database
from app.db import network as store
from app.network import crypto, engine, membership
from tests.core.network.harness import (
    converge,
    exchange,
    join,
    owner_pref,
    set_owner_pref,
)


def _old_build_cascade(agent_id):
    """What an older build's ``ON DELETE CASCADE`` on api_user_profiles does
    when its agent row goes, locally or from a peer's tombstone. This build's
    schema has no such key, so the test emulates it."""
    with database.get_db() as conn:
        conn.cursor().execute("DELETE FROM api_user_profiles WHERE profile_id = %s", (agent_id,))


def test_old_worker_cascade_cannot_widen_a_restricted_key(workers):
    """#1870: an older worker still cascades an agent delete into the API-key
    assignments and publishes those deletions. On an updated worker, applying
    the deletion of a key's last assignment to an agent it does not have
    would make the key unrestricted, so that deletion is refused (kept as a
    conflict). Narrowing deletions and deliberate edits still apply."""
    from app.core.api_user_scope import worker_refusal
    from app.network import policy

    old, updated, third = workers("old-build"), workers("updated-build"), workers("third-build")
    with old.active():
        engine.create_network("Mixed versions")
        for agent_id in ("deleted-there", "deleted-here", "kept"):
            database.create_profile(agent_id, agent_id, None, {})
        database.create_api_user("only-there", "Only", "only-there-hash", profile_ids=["deleted-there"])
        database.create_api_user("mixed-there", "Mixed", "mixed-there-hash", profile_ids=["deleted-there", "kept"])
        database.create_api_user("only-here", "Only here", "only-here-hash", profile_ids=["deleted-here"])
        database.create_api_user("open-later", "Opened", "open-later-hash", profile_ids=["kept"])
        engine.scan_all()
    join(old, updated)
    join(old, third)
    converge([old, updated, third])

    # 1. Deleted on the older worker: its cascade publishes the deletions.
    with old.active():
        database.delete_profile("deleted-there")
        _old_build_cascade("deleted-there")
        # A deliberate edit on the older worker still opens a key up.
        database.update_api_user("open-later", profile_ids=[])
        engine.scan_all()
    # 2. Deleted on an updated worker: the older worker cascades on apply.
    with updated.active():
        database.delete_profile("deleted-here")
        engine.scan_all()
    exchange(updated, old)
    with old.active():
        _old_build_cascade("deleted-here")
        engine.scan_all()
    result = exchange(old, updated)
    assert result.retry == result.rejected == result.pending == 0, result.reasons
    # The third worker has neither tombstone yet. The older worker forwards
    # the updated worker's agent tombstone with its own cascade deletions;
    # streams from different origins are not causally ordered, so deliver
    # the worst order: the older worker's deletions first.
    with third.active():
        marks = store.local_watermarks()
    with old.active():
        ops = store.ops_since(marks, limit=500, exclude_origin=third.device_id)
    ops.sort(key=lambda op: op["origin_device_id"] != old.device_id)
    assert {op["origin_device_id"] for op in ops} == {old.device_id, updated.device_id}
    with third.active():
        result = engine.apply_batch(ops)
    assert result.retry == result.rejected == result.pending == 0, result.reasons
    converge([old, updated, third])

    for receiver in (updated, third):
        with receiver.active():
            assert database.get_profile("deleted-there") is None
            assert database.get_api_user("only-there")["profile_ids"] == ["deleted-there"]
            assert worker_refusal("only-there", "kept") is not None
            assert database.get_api_user("only-here")["profile_ids"] == ["deleted-here"]
            assert worker_refusal("only-here", "kept") is not None
            assert database.get_api_user("mixed-there")["profile_ids"] == ["kept"]
            assert database.get_api_user("open-later")["profile_ids"] is None
            # The refused deletions stay visible as conflicts, and a rescan
            # publishes nothing that would undo the older worker's state.
            conflicts = {c["record_id"] for c in store.list_conflicts() if c["collection"] == "api_user_profiles"}
            coll = policy.collection("api_user_profiles")
            assert coll.record_id({"api_user_id": "only-there", "profile_id": "deleted-there"}) in conflicts
            assert engine.scan_all(include_catalog=False, collections=["api_user_profiles", "api_users"]) == 0


def test_agent_recreated_on_a_peer_does_not_inherit_a_deleted_agents_grant(workers):
    """#1870: grants that outlive a deleted agent never attach to a NEW agent
    with the same id, also when that agent arrives from a peer (here an older
    build that creates it without detaching anything)."""
    from app.core.api_user_scope import worker_refusal

    old, updated = workers("old-build"), workers("updated-build")
    with old.active():
        engine.create_network("Mixed versions")
        database.create_profile("helper", "Helper", None, {})
        database.create_profile("other", "Other", None, {})
        database.create_api_user("only-helper", "Only", "only-helper-hash", profile_ids=["helper"])
        database.create_api_user("helper-other", "Mixed", "helper-other-hash", profile_ids=["helper", "other"])
        engine.scan_all()
    join(old, updated)
    converge([old, updated])
    with updated.active():
        database.delete_profile("helper")
        engine.scan_all()
    converge([old, updated])
    with old.active():
        _old_build_cascade("helper")
        engine.scan_all()
    converge([old, updated])
    with updated.active():
        assert database.get_api_user("only-helper")["profile_ids"] == ["helper"]
    with old.active():
        # The older build's create path: a plain insert, nothing detached.
        with database.get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO agents (id, name, description, config, is_builtin, mcp_tools, created_at, updated_at) "
                "VALUES ('helper', 'A different helper', NULL, '{}', FALSE, '[]', '2026-10-05', '2026-10-05')")
        engine.scan_all()
    result = exchange(old, updated)
    assert result.retry == result.rejected == result.pending == 0, result.reasons
    with updated.active():
        assert database.get_profile("helper")["name"] == "A different helper"
        assert not database.get_api_user("only-helper")["is_active"]
        assert worker_refusal("only-helper", "helper") is not None
        assert database.get_api_user("helper-other")["profile_ids"] == ["other"]
        assert worker_refusal("helper-other", "helper") is not None
        engine.scan_all()
    # The switched-off key reaches the older worker too.
    result = exchange(updated, old)
    assert result.retry == result.rejected == result.pending == 0, result.reasons
    with old.active():
        assert not database.get_api_user("only-helper")["is_active"]


def test_grant_that_arrives_before_its_agent_is_kept(workers):
    """#1875 review: a grant made on B for an agent created on A can reach a
    third worker before the agent itself (B forwards both streams; streams
    from different origins are ordered by device id, not causally). The
    agent's first arrival is not a re-creation: the keys must keep working,
    there and, after the next scan, everywhere else."""
    a, b, c = workers("agent-origin"), workers("grant-origin"), workers("third")
    with a.active():
        engine.create_network("Three workers")
        engine.scan_all()
    join(a, b)
    join(a, c)
    converge([a, b, c])
    with a.active():
        database.create_profile("late", "Late", None, {})
        database.create_profile("other", "Other", None, {})
        engine.scan_all()
    exchange(a, b)
    with b.active():
        database.create_api_user("only-late", "Only", "only-late-hash", profile_ids=["late"])
        database.create_api_user("late-other", "Two", "late-other-hash", profile_ids=["late", "other"])
        engine.scan_all()
    with c.active():
        marks = store.local_watermarks()
    with b.active():
        ops = store.ops_since(marks, limit=500, exclude_origin=c.device_id)
    # The worst order: the grants before the agents.
    ops.sort(key=lambda op: op["origin_device_id"] != b.device_id)
    assert {op["origin_device_id"] for op in ops} == {a.device_id, b.device_id}
    with c.active():
        result = engine.apply_batch(ops)
    assert result.retry == result.rejected == result.pending == 0, result.reasons
    with c.active():
        engine.scan_all()
    converge([a, b, c])
    for worker in (a, b, c):
        with worker.active():
            only, both = database.get_api_user("only-late"), database.get_api_user("late-other")
            assert only["is_active"] and only["profile_ids"] == ["late"]
            assert both["is_active"] and sorted(both["profile_ids"]) == ["late", "other"]


@pytest.mark.parametrize("retired_id", ["Explore", "Plan"])
def test_old_worker_cannot_sync_retired_builtin_back(workers, retired_id):
    from app.core.builtin_subagents import seed_builtin_subagents
    from app.network import policy

    old, updated = workers("old-build"), workers("updated-build")
    with old.active():
        engine.create_network("Mixed versions")
        seed_builtin_subagents()
        # Emulate the previous build's startup producer (not the new defaults).
        database.create_profile(retired_id, retired_id, None, {"model": "haiku"}, is_builtin=True)
        database.create_api_user("search-key", "Restricted key", "synthetic-key-hash", profile_ids=[retired_id])
        database.create_api_user("mixed-key", "Mixed key", "synthetic-mixed-hash", profile_ids=[retired_id, "frontend-designer"])
        database.create_api_user("open-key", "Unrestricted key", "synthetic-open-hash")
        database.create_automation("search-job", "Old search", "interval", {"seconds": 300},
                                   retired_id, prompt="Find it", enabled=True)
        engine.scan_all()
    with updated.active():
        seed_builtin_subagents()
    join(old, updated)
    result = exchange(old, updated)
    assert result.retry == result.rejected == result.pending == 0
    with updated.active():
        assert database.get_profile(retired_id) is None
        assert retired_id not in {a["id"] for a in database.get_all_profiles()}
        job = database.get_automation("search-job")
        assert job["profile_id"] == "general-purpose"
        assert not job["is_enabled"]
        assert f"{retired_id} was removed" in job["last_error"]
        # Bootstrap snapshots also materialize through this seam.
        with database.get_db() as conn:
            assert engine.materialize(conn.cursor(), policy.collection("agents"), retired_id, {
                "id": retired_id, "name": retired_id, "is_builtin": True, "config": {},
            }) is None
        engine.scan_all()
    result = exchange(updated, old)
    assert result.retry == result.rejected == result.pending == 0
    with old.active():
        # No retirement tombstone is sent to the older worker. Normalized
        # mirror hashes prevent re-publishing this peer's automation rewrite;
        # that worker keeps its old definition until it upgrades.
        assert database.get_profile(retired_id) is not None
        assert database.get_automation("search-job")["profile_id"] == retired_id
        assert database.get_automation("search-job")["is_enabled"]
        database.update_profile(retired_id, description="Re-seeded on the old build", allow_builtin=True)
        engine.scan_all()
    result = exchange(old, updated)
    assert result.retry == result.rejected == result.pending == 0
    with updated.active():
        assert database.get_profile(retired_id) is None
        assert database.get_automation("search-job")["profile_id"] == "general-purpose"
        from app.core.api_user_scope import worker_refusal
        assert database.get_api_user("search-key")["profile_ids"] == [retired_id]
        assert worker_refusal("search-key", "general-purpose") is not None
        assert worker_refusal("mixed-key", "frontend-designer") is None
        assert worker_refusal("mixed-key", "general-purpose") is not None
        assert worker_refusal("open-key", "general-purpose") is None
        seed_builtin_subagents()
        engine.scan_all()
    result = exchange(updated, old)
    assert result.retry == result.rejected == result.pending == 0
    with old.active():
        assert database.get_api_user("search-key")["profile_ids"] == [retired_id]
        seed_builtin_subagents()  # older worker upgrading now also stays restricted
        assert worker_refusal("search-key", "general-purpose") is not None


@pytest.mark.parametrize("retired_id", ["Explore", "Plan"])
def test_local_retirement_does_not_publish_agent_tombstone(workers, retired_id):
    from app.core.builtin_subagents import seed_builtin_subagents

    a = workers("upgrading-worker")
    with a.active():
        engine.create_network("Upgrade")
        database.create_profile(retired_id, retired_id, None, {"model": "haiku"}, is_builtin=True)
        database.create_automation("before-upgrade", "Search", "interval", {"seconds": 300},
                                   retired_id, prompt="Find it", enabled=True)
        engine.scan_all()
        with database.get_db() as conn:
            assert store.get_record(conn.cursor(), "agents", retired_id) is not None
        seed_builtin_subagents()
        with database.get_db() as conn:
            assert store.get_record(conn.cursor(), "agents", retired_id) is None
        engine.scan_all()
        assert database.get_profile(retired_id) is None
        with database.get_db() as conn:
            assert store.get_record(conn.cursor(), "agents", retired_id) is None


@pytest.mark.parametrize("retired_id", ["Explore", "Plan"])
def test_sync_keeps_custom_agent_with_retired_id(workers, retired_id):
    from app.core.builtin_subagents import seed_builtin_subagents

    a, b = workers("custom-source"), workers("custom-peer")
    with a.active():
        engine.create_network("Custom agents")
        database.create_profile(retired_id, retired_id, None, {"model": "opus"})
        database.create_automation("custom-job", "Custom search", "interval", {"seconds": 300},
                                   retired_id, prompt="Find it", enabled=True)
        engine.scan_all()
    join(a, b)
    exchange(a, b)
    with b.active():
        seed_builtin_subagents()
        assert not database.get_profile(retired_id)["is_builtin"]
        assert database.get_automation("custom-job")["profile_id"] == retired_id
        assert database.get_automation("custom-job")["is_enabled"]


def test_observed_docker_address_is_published_to_other_workers(workers, monkeypatch):
    from fastapi import Request
    from app.core import cluster
    from app.core.config import settings
    from app.network import runners, hub
    a, b = workers("docker"), workers("desktop")
    monkeypatch.setattr(settings, "local_mode", False)
    monkeypatch.setattr(settings, "cluster_advertise_url", "")
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(cluster, "_build_capabilities", lambda source: {"os": "linux"})
    monkeypatch.setattr(cluster, "register_self", lambda: True)
    monkeypatch.setattr(hub, "kick", lambda: None)
    request = Request({"type": "http", "scheme": "http", "path": "/api/v1/auth/status",
                       "root_path": "", "query_string": b"", "method": "GET",
                       "headers": [(b"host", b"10.0.0.200:18000")],
                       "client": ("10.0.0.135", 12345), "server": ("172.17.0.11", 8000),
                       "shuffle.ingress": "local"})
    with a.active():
        engine.create_network("LAN")
    join(a, b)
    with a.active():
        assert cluster.observe_local_advertise_url(request)
        assert engine.presence_of(a.device_id)["endpoints"] == ["http://10.0.0.200:18000"]
        engine.scan_all()
    exchange(a, b)
    with b.active():
        row = next(r for r in runners.directory()["runners"] if r["id"] == a.device_id)
        assert row["base_url"] == "http://10.0.0.200:18000"
        assert database.get_system_setting(cluster._observed_advertise_key()) is None
    # Re-loading the network does not lose the persisted host/port.
    with a.active():
        engine.load_context()
        assert cluster.advertise_url() == ("http://10.0.0.200:18000", "observed")
        assert not cluster.observe_local_advertise_url(request)
        cluster.set_advertise_url_override("http://10.0.0.200:19000")
        assert engine.presence_of(a.device_id)["endpoints"] == ["http://10.0.0.200:19000"]


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


@pytest.mark.parametrize("state", ["disconnected", "waiting", "limited", "locked", "unavailable",
                                   "connecting", "reconnecting", "connected"])
def test_presence_advertises_the_tunnel_first_while_the_account_link_exists(workers, monkeypatch, state):
    """The tunnel is listed, first, for as long as the account has assigned it,
    including while the connector restarts: peers dial only the tunnel once
    they see it, so a momentarily down tunnel reads as offline rather than
    letting peer traffic drop back to plain http. Signed out, refused or
    unassigned states advertise the LAN address alone. Beside the tunnel,
    Docker's guessed bridge address is left out: no other host reaches it."""
    from app.core import cluster, managed_connection
    from app.core.config import settings
    a, b = workers("docker"), workers("desktop")
    lan, cloud = "http://172.17.0.11:8000", "https://worker.example.com"
    monkeypatch.setattr(settings, "local_mode", False)
    monkeypatch.setattr(cluster, "advertise_url", lambda: (lan, "guess"))
    monkeypatch.setattr(managed_connection, "status", lambda: {"status": state, "url": cloud})
    with a.active():
        engine.create_network("Home")
        engine.publish_presence(version="test")
        endpoints = engine.presence_of(a.device_id)["endpoints"]
        assert endpoints == ([cloud] if state in ("connected", "connecting", "reconnecting") else [lan])
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


@pytest.mark.parametrize("corrupt", [False, True])
def test_enrolled_worker_missing_identity_requires_recovery_without_new_device(workers, corrupt):
    from app.network import identity, startup
    worker = workers("persisted-worker")
    with worker.active():
        ctx = engine.create_network("Home", legacy_instance_id="stable-install")
        before = [row["device_id"] for row in store.list_members()]
        path = worker.data_dir / ".credential-keys" / identity.IDENTITY_FILE
        if corrupt:
            path.write_text("not a key")
        else:
            path.unlink()
        identity.reset_cache_for_tests()
        engine.reset_for_tests()
        with pytest.raises(startup.PrivateDatabaseRequiredError):
            startup.initialize()
        assert [row["device_id"] for row in store.list_members()] == before
        assert store.get_state()["network_id"] == ctx.network_id
        if not corrupt:
            assert not path.exists()
            with pytest.raises(identity.IdentityError):
                engine.load_context()
            assert not path.exists()


def test_restart_after_worker_update_keeps_identity_and_membership(workers, monkeypatch):
    from app.core.config import settings
    from app.network import identity, startup
    worker = workers("persistent-data")
    with worker.active():
        original = engine.create_network("Home", legacy_instance_id="stable-install")
        fingerprint = original.identity.fingerprint
        before = [row["device_id"] for row in store.list_members()]
        identity.reset_cache_for_tests()
        engine.reset_for_tests()
        monkeypatch.setattr(settings, "service_name", "updated-container-name")
        restored = startup.initialize()
        assert restored.device_id == original.device_id
        assert restored.identity.fingerprint == fingerprint
        assert restored.network_id == original.network_id
        assert [row["device_id"] for row in store.list_members()] == before
        assert restored.legacy_instance_map["stable-install"] == original.device_id


@pytest.mark.asyncio
async def test_account_connection_starts_before_runner_approval_and_stops_cleanly(workers, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from app.core import cluster, managed_connection
    from app.network import enrollment, hub, startup, transport
    worker = workers("waiting-for-approval")
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def connection():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    supervisor = AsyncMock(side_effect=connection)
    monkeypatch.setattr(managed_connection, "supervise", supervisor)
    monkeypatch.setattr(transport, "start", Mock(return_value=[]))
    monkeypatch.setattr(transport, "stop", AsyncMock())
    monkeypatch.setattr(hub, "start", Mock(return_value=[]))
    monkeypatch.setattr(hub, "stop", AsyncMock())
    monkeypatch.setattr(enrollment, "resume_enrollment", Mock())
    monkeypatch.setattr(startup, "_tasks", [])
    with worker.active():
        database.register_install(install_id="existing-worker", device_name="Existing",
                                  base_url=None, platform="test", version="before-update", approve_new=True)
        assert cluster.register_self() is False
        tasks = await startup.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            supervisor.assert_awaited_once()
            assert not tasks[0].done()
        finally:
            await startup.stop()
        assert stopped.is_set()
        assert all(task.done() for task in tasks)
        assert engine.context() is None


def test_exchange_batch_bounds_queries_frontier_and_detects_pruned_gaps(workers, monkeypatch):
    a = workers("frontier")
    with a.active():
        engine.create_network("Frontier")
        database.set_system_setting("tts_model", "first")
        engine.scan_collection("system_settings")
        before_update = store.local_watermarks()
        database.set_system_setting("tts_model", "second")
        engine.scan_collection("system_settings")
        marks = store.local_watermarks()
        queries = []
        a.raw.set_trace_callback(queries.append)
        try:
            assert store.exchange_batch(marks) == ([], marks, False)
        finally:
            a.raw.set_trace_callback(None)
        assert len([q for q in queries if q.lstrip().upper().startswith(("SELECT", "WITH"))]) == 2
        read_heads = store.local_watermarks

        def append_after_frontier(conn=None):
            heads = read_heads(conn)
            # Interleave a real producer after the response frontier was read.
            database.set_system_setting("tts_model", "third")
            engine.scan_collection("system_settings")
            return heads

        with monkeypatch.context() as patch:
            patch.setattr(store, "local_watermarks", append_after_frontier)
            ops, heads, snapshot = store.exchange_batch(before_update)
        assert heads == marks and not snapshot
        assert len(ops) == 1 and ops[0]["origin_seq"] == marks[a.device_id]["1"]
        later, current, snapshot = store.exchange_batch(heads)
        assert len(later) == 1 and not snapshot
        assert later[0]["origin_seq"] == current[a.device_id]["1"]
        # Use the actual pruner: it removes superseded versions but preserves
        # the latest envelope, which still backs snapshots and stream heads.
        assert store.prune_ops(keep_days=-1) >= 2
        assert store.exchange_batch(before_update)[2] is True
        assert store.exchange_batch(current) == ([], current, False)
        assert store.exchange_batch({}, exclude_origin=a.device_id)[0] == []
        assert store.exchange_batch({}, exclude_origin=a.device_id)[2] is False


@pytest.mark.parametrize("key", [
    "cluster_secret", "workspace_path:peer", "claude_auth_mode:peer",
    "claude_auth_mode:local", "claude_cloud_config", "claude_cloud_secrets",
])
@pytest.mark.parametrize("out_of_order", [False, True])
def test_local_settings_from_old_peers_are_consumed_not_stored(workers, monkeypatch, key, out_of_order):
    from dataclasses import replace
    from app.network import policy

    old, updated = workers("old"), workers("updated")
    with old.active():
        engine.create_network("Mixed settings policies")
    join(old, updated)
    converge([old, updated])
    coll = policy.collection("system_settings")
    # Drive the real producer with the old peer's more permissive policy.
    with old.active(), monkeypatch.context() as patch:
        patch.setitem(policy.COLLECTIONS, coll.name, replace(
            coll, shared_row=lambda row: row.get("key") == key or coll.shared_row(row)))
        before = store.local_watermarks()
        database.set_system_setting(key, "peer-value")
        engine.scan_collection(coll.name)
        [blocked] = store.ops_since(before)
        database.set_system_setting("tts_model", "shared-value")
        engine.scan_collection(coll.name)
        [later] = store.ops_since({old.device_id: {"1": blocked["origin_seq"]}})
    with updated.active():
        if out_of_order:
            assert engine.apply_batch([later]).pending == 1
            result = engine.apply_batch([blocked])
        else:
            result = engine.apply_batch([blocked, later])
        assert result.rejected == 1 and result.applied == 1, result.reasons
        assert result.reasons == ["local-only row"]
        assert database.get_system_setting(key) is None
        assert database.get_system_setting("tts_model") == "shared-value"
        assert engine.apply_batch([blocked, later]).duplicate == 2
        with database.get_db() as conn:
            assert store.get_watermark(conn.cursor(), old.device_id, 1) == later["origin_seq"]
        assert engine.scan_collection(coll.name) == 0
    for table in ("sync_records", "sync_ops", "sync_conflicts"):
        assert not updated.sql(f"SELECT * FROM {table} WHERE collection = ? AND record_id = ?", (coll.name, key))
    assert not updated.sql("SELECT * FROM sync_pending")

    # A peer's tombstone must not erase a setting already owned locally.
    with updated.active():
        database.set_system_setting(key, "owner-value")
    with old.active(), monkeypatch.context() as patch:
        patch.setitem(policy.COLLECTIONS, coll.name, replace(
            coll, shared_row=lambda row: row.get("key") == key or coll.shared_row(row)))
        before = store.local_watermarks()
        database.delete_system_setting(key)
        database.delete_system_setting("tts_model")
        engine.scan_collection(coll.name)
        deletes = store.ops_since(before)
    assert {op["record_id"] for op in deletes} == {key, "tts_model"}
    with updated.active():
        result = engine.apply_batch(deletes)
        assert result.rejected == 1 and result.applied == 1, result.reasons
        assert database.get_system_setting(key) == "owner-value"
        assert database.get_system_setting("tts_model") is None
        assert engine.scan_collection(coll.name) == 0
    for table in ("sync_records", "sync_ops", "sync_conflicts"):
        assert not updated.sql(f"SELECT * FROM {table} WHERE collection = ? AND record_id = ?", (coll.name, key))


def test_local_settings_snapshot_preserves_previously_synced_copies(workers, monkeypatch):
    from dataclasses import replace
    from app.network import policy

    old, updated = workers("old"), workers("updated")
    key = "claude_auth_mode:local"
    coll = policy.collection("system_settings")
    with old.active():
        engine.create_network("Mixed settings policies")
    # Both workers once allowed this key: produce and receive a genuine copy.
    with monkeypatch.context() as patch:
        patch.setitem(policy.COLLECTIONS, coll.name, replace(
            coll, shared_row=lambda row: row.get("key") == key or coll.shared_row(row)))
        with old.active():
            database.set_system_setting(key, "original-method")
            engine.scan_collection(coll.name)
        join(old, updated)
        with updated.active():
            assert database.get_system_setting(key) == "original-method"
        with old.active():
            database.set_system_setting(key, "cloud")
            database.set_system_setting("tts_model", "new-shared-value")
            engine.scan_collection(coll.name)
            chunk = engine.snapshot_chunk(coll.name)
    legacy_records = updated.sql("SELECT * FROM sync_records WHERE collection = ? AND record_id = ?", (coll.name, key))
    with updated.active():
        assert engine.apply_snapshot_chunk(chunk) == 1
        assert database.get_system_setting(key) == "original-method"
        assert database.get_system_setting("tts_model") == "new-shared-value"
        assert engine.scan_collection(coll.name) == 0
        # Rejected local-only writes must not create a conflict or log row.
        before = store.count_ops()
        result = exchange(old, updated)
        assert result.rejected == 1, result.reasons
        assert store.count_ops() == before + 1  # only the shared tts_model op
    assert updated.sql("SELECT * FROM sync_records WHERE collection = ? AND record_id = ?", (coll.name, key)) == legacy_records
    assert not updated.sql("SELECT * FROM sync_conflicts WHERE collection = ? AND record_id = ?", (coll.name, key))

    with old.active(), monkeypatch.context() as patch:
        patch.setitem(policy.COLLECTIONS, coll.name, replace(
            coll, shared_row=lambda row: row.get("key") == key or coll.shared_row(row)))
        database.delete_system_setting(key)
        database.delete_system_setting("tts_model")
        engine.scan_collection(coll.name)
        deleted_chunk = engine.snapshot_chunk(coll.name)
    with updated.active():
        assert engine.apply_snapshot_chunk(deleted_chunk) == 1
        assert database.get_system_setting(key) == "original-method"
        assert database.get_system_setting("tts_model") is None
    assert updated.sql("SELECT * FROM sync_records WHERE collection = ? AND record_id = ?", (coll.name, key)) == legacy_records


def test_incoming_setting_policy_checks_payload_and_authoritative_record_id():
    from app.network import policy

    coll = policy.collection("system_settings")
    assert not engine._incoming_row_is_shared(coll, "claude_auth_mode:local", {"key": "tts_model"})
    assert not engine._incoming_row_is_shared(coll, "tts_model", {"key": "claude_cloud_secrets"})
    assert not engine._incoming_row_is_shared(coll, "claude_auth_mode:local", None)
    assert engine._incoming_row_is_shared(coll, "tts_model", None)


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
    assert owner_pref(b, "layout") == {"sidebarWidth": 320}

    # A membership record for B is now known to A and published.
    with a.active():
        assert engine.context().member(b.device_id).status == "active"
    # Live change on A reaches B within one exchange.
    set_owner_pref(a, "layout", {"sidebarWidth": 400})
    result = exchange(a, b)
    assert result.applied == 1 and result.rejected == 0
    assert owner_pref(b, "layout") == {"sidebarWidth": 400}
    # And B's change reaches A.
    set_owner_pref(b, "theme", {"preference": "dark"})
    exchange(b, a)
    assert owner_pref(a, "theme") == {"preference": "dark"}
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
    set_owner_pref(a, "layout", {"sidebarWidth": 1})
    converge([a, b])
    # Both edit the same key while partitioned.
    set_owner_pref(a, "layout", {"sidebarWidth": 100})
    set_owner_pref(b, "layout", {"sidebarWidth": 200})
    set_owner_pref(b, "layout", {"sidebarWidth": 201})  # B's clock is now ahead
    converge([a, b])
    assert owner_pref(a, "layout") == owner_pref(b, "layout") == {"sidebarWidth": 201}
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


@pytest.mark.asyncio
async def test_curated_provider_models_survive_first_sync_without_refetch(workers, monkeypatch):
    from unittest.mock import Mock
    from app.api import model_providers as api
    from app.api.model_registry import list_models
    from app.core import encryption
    from app.api import settings_crypto
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    monkeypatch.setattr(settings_crypto, "get_decrypted_api_key", lambda _name: None)
    source, fresh = workers("configured"), workers("first-load")
    model = {"id": "synced/chat", "label": "My saved model", "context_window": 128000,
             "reasoning_capabilities": {"version": 1, "effort_levels": [], "default_effort": None}}
    # No upstream catalog refresh is needed to use a previously saved selection.
    upstream = Mock(side_effect=AssertionError("First sync must retain curated models"))
    monkeypatch.setattr(api, "fetch_upstream_models", upstream)
    with source.active():
        engine.create_network("Home")
        provider = await api.create_provider(api.ModelProviderCreate(
            name="My gateway", provider_type="openai", base_url="https://provider.example/v1",
            auth_token="test-key"), token="")
        await api.update_provider(provider.id, api.ModelProviderUpdate(
            models=[model], default_model=model["id"]), token="")
        engine.scan_all()
    join(source, fresh)
    with fresh.active():
        registry = await list_models(token="")
        synced = next(p for p in registry["providers"] if p["id"] == provider.id)
        assert synced["default_model"] == model["id"]
        assert [(m["id"], m["label"]) for m in synced["models"]] == [(model["id"], model["label"])]
        assert synced["models"][0]["context_window"] == model["context_window"]
        assert registry["claude"]["auth_kind"] is None
    upstream.assert_not_called()


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


def test_simultaneous_model_effort_edits_and_clears_survive_two_worker_sync(workers):
    from app.core.model_effort import effort_preference_key
    a, b = workers("gaming"), workers("unraid")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    opus, sol = effort_preference_key("opus"), effort_preference_key("gpt-6.1-sol")
    # Neither worker has received the other's edit when it makes its own.
    set_owner_pref(a, opus, "low")
    set_owner_pref(b, sol, "xhigh")
    converge([a, b])
    for worker in (a, b):
        assert owner_pref(worker, opus) == "low"
        assert owner_pref(worker, sol) == "xhigh"
    set_owner_pref(a, opus, None)
    set_owner_pref(b, sol, "max")
    converge([a, b])
    for worker in (a, b):
        assert owner_pref(worker, opus) is None
        assert owner_pref(worker, sol) == "max"


def test_same_operation_over_two_routes_applies_once(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    set_owner_pref(a, "layout", {"sidebarWidth": 7})
    exchange(a, b)   # route 1: direct
    exchange(a, c)
    forwarded = exchange(c, b)  # route 2: forwarded by C
    assert forwarded.applied == 0 and forwarded.rejected == 0
    assert owner_pref(b, "layout") == {"sidebarWidth": 7}
    with b.active():
        assert len([o for o in store.ops_since({}) if o["collection"] == "user_preferences"]) == 1


def test_out_of_order_delivery_is_held_until_the_hole_fills(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    set_owner_pref(a, "k1", 1)
    set_owner_pref(a, "k2", 2)
    set_owner_pref(a, "k3", 3)
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
    assert owner_pref(b, "k3") == 3


def test_revoked_device_is_rejected_and_cannot_read_new_records(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    # B goes offline and keeps writing.
    set_owner_pref(b, "offline_write", True)
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
    set_owner_pref(a, "after", "rotation")
    with a.active():
        op = [o for o in store.ops_since({}) if o["record_id"] == "admin|@owner|after"][0]
        assert op["key_version"] == 2
    with b.active():
        result = engine.apply_batch([op])
        # A key B does not hold is a transient condition (the key record may
        # still be on its way), so the op is left for a retry, not skipped.
        assert result.applied == 0 and result.retry == 1 and "unknown key version 2" in result.reasons
    # C still converges with A on the new key.
    converge([a, c])
    assert owner_pref(c, "after") == "rotation"


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
    set_owner_pref(d, "from_d", 1)
    result = exchange(d, a)
    assert result.rejected == 0 and result.applied >= 1
    with a.active():
        assert engine.context().member(d.device_id).status == "active"
    assert owner_pref(a, "from_d") == 1


def test_bad_envelopes_are_rejected_without_side_effects(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    set_owner_pref(a, "x", 1)
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
    assert owner_pref(b, "x") == 1


def test_stale_incarnation_is_rejected(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    set_owner_pref(a, "x", 1)
    with a.active():
        [op] = [o for o in store.ops_since({a.device_id: {"1": 1}}, exclude_origin=b.device_id)
                if o["collection"] == "user_preferences"]
    with b.active():
        with store.get_db() as conn:
            store.set_watermark(conn.cursor(), a.device_id, 2, 0)
        result = engine.apply_batch([op])
        assert result.rejected == 1 and "stale incarnation" in result.reasons
