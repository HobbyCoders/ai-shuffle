"""Account sync through the hub feed, device approval and recovery."""
import pytest

from app.core import account_hub
from app.db import database
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.network import engine, hub, recovery, tokens, transport
from tests.core.network.fake_hub import FakeHub
from tests.core.network.harness import Worker, exchange


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
    tokens.reset_for_tests()
    transport.reset_for_tests()
    hub.reset_for_tests()


@pytest.fixture
def fake(monkeypatch):
    fake_hub = FakeHub()
    monkeypatch.setattr(account_hub, "call", fake_hub.call)
    servers = {}

    def binding():
        name = servers.get("current")
        if name is None:
            return None
        return {"hub_url": "https://hub.example", "server_id": name, "linked_by": "managed:x", "active": True,
                "encrypted_secret": "enc:x"}

    monkeypatch.setattr(binding_store, "get_binding", binding)
    fake_hub.servers = servers
    return fake_hub


def _signed_in(fake, worker):
    fake.servers["current"] = "server-" + worker.name


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


async def test_worker_rename_retries_registration_and_survives_restart(workers, fake, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="Old name")
        _signed_in(fake, a)
        hub.register()
        # A pre-upgrade rename can exist in the DB with a stale cached context.
        store.rename_member(a.device_id, "Unraid")
        original_call = account_hub.call

        def unavailable(*args, **kwargs):
            from fastapi import HTTPException
            raise HTTPException(503, "Unavailable")

        monkeypatch.setattr(account_hub, "call", unavailable)
        await hub._loop_body()
        assert hub._hub_state()["registered_name"] == "Old name"
        assert fake.devices[a.device_id]["name"] == "Old name"

        hub.reset_for_tests()
        engine.load_context()
        monkeypatch.setattr(account_hub, "call", original_call)
        await hub._loop_body()
        assert fake.devices[a.device_id]["name"] == "Unraid"
        assert hub._hub_state()["registered_name"] == "Unraid"
        calls = fake.calls.count(("server-a", "POST", "network"))
        await hub._loop_body()
        assert fake.calls.count(("server-a", "POST", "network")) == calls


async def test_existing_registration_refreshes_name_after_upgrade(workers, fake):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="Unraid")
        _signed_in(fake, a)
        hub.register()
        state = hub._hub_state()
        state.pop("registered_name")
        store.update_state(hub=state)
        fake.devices[a.device_id]["name"] = "Old name"
        await hub._loop_body()
        assert fake.devices[a.device_id]["name"] == "Unraid"


async def test_register_push_pull_and_device_approval(workers, fake):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        _signed_in(fake, a)
        assert hub.status()["linked"] and not hub.status()["registered"]
        result = hub.register()
        assert result["status"] == "active" and hub.status()["registered"]
        pushed = hub.push_once()
        assert pushed >= 2 and hub.status()["backlog"] == 0
        assert hub.push_once() == 0
        assert hub.pull_once() == 0  # our own ops come back and are ignored
        assert hub.status()["cursor"] == len(fake.feed)
    assert fake.network["network_id"] and fake.devices[a.device_id]["status"] == "active"

    # B is signed in to the same account but has no network yet: it waits for a member to be online.
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is False
        assert fake.devices[b.device_id]["status"] == "pending"
        assert engine.context() is None
        waiting = hub.status()["account_network"]
        assert waiting["network_id"] == fake.network["network_id"]
        assert [d["name"] for d in waiting["devices"] if d["status"] == "active"] == ["A"]
    # A's next pass admits B without anyone comparing fingerprints (the account is the authorization).
    with a.active():
        _signed_in(fake, a)
        assert [d["device_id"] for d in hub.pending_devices()] == [b.device_id]
        await hub._loop_body()
        assert fake.devices[b.device_id]["status"] == "approved"
        assert hub.status()["pending_devices"] == []
        assert engine.context().member(b.device_id).status == "active"
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is True
        ctx = engine.context()
        assert ctx is not None and ctx.bootstrap_state == "ready"
        assert database.get_system_setting("tts_model") == "voice-a"
        assert ctx.member(a.device_id).name == "A" and ctx.member(b.device_id).status == "active"
        assert fake.devices[b.device_id]["status"] == "active"
    # A change on B goes to the hub, A pulls it; the same op over LAN is a no-op.
    _set_pref(b, "from_b", 1)
    with b.active():
        _signed_in(fake, b)
        assert hub.push_once() >= 1
    with a.active():
        _signed_in(fake, a)
        assert hub.pull_once() >= 1
    assert _pref(a, "from_b") == 1
    assert exchange(b, a).applied == 0


async def test_first_worker_to_sign_in_founds_the_account_network(workers, fake):
    a = workers("a")
    with a.active():
        _signed_in(fake, a)
        assert engine.context() is None
        assert await hub._loop_body() is True
        ctx = engine.context()
        assert ctx is not None and ctx.founder_device_id == a.device_id and ctx.bootstrap_state == "ready"
        assert fake.network["network_id"] == ctx.network_id
        assert fake.devices[a.device_id]["status"] == "active"
        assert hub.status()["registered"] and hub.status()["status"] == "active"
        # Later passes just sync.
        database.set_system_setting("tts_model", "voice-founder")
        engine.scan_all()
        assert await hub._loop_body() is False
        assert hub.status()["backlog"] == 0 and len(fake.feed) >= 2


async def test_worker_with_its_own_network_switches_to_the_account_network_on_sign_in(workers, fake):
    """The account always wins: a clean-install worker that signs in later drops its
    own network and joins the account's, with nothing to decide."""
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        _signed_in(fake, a)
        hub.register()
        hub.push_once()
    with b.active():
        engine.create_network("Laptop", device_name="B")
        database.set_system_setting("tts_model", "voice-b")
        database.set_system_setting("only_on_b", "yes")
        engine.scan_all()
        own_network = engine.context().network_id
        _signed_in(fake, b)
        # One pass: the hub answers conflict, B leaves its network and registers again as pending.
        assert await hub._loop_body() is True
        status = hub.status()
        assert status["status"] == "pending"
        assert status["account_network"]["network_id"] == fake.network["network_id"]
        assert engine.context() is None and fake.devices[b.device_id]["status"] == "pending"
        assert b.sql("SELECT COUNT(*) AS n FROM sync_ops")[0]["n"] == 0
        # Nothing of B's old network reached the account.
        assert all(env["origin_device_id"] == a.device_id for env in fake.feed)
        # The manual retry is a no-op once the switch happened.
        assert (await hub.adopt_account_network())["adopted"] is False
    with a.active():
        _signed_in(fake, a)
        await hub._loop_body()
        assert fake.devices[b.device_id]["status"] == "approved"
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is True
        ctx = engine.context()
        assert ctx.network_id == fake.network["network_id"] != own_network
        # The account's copy wins; B's own row for the same key is gone.
        assert database.get_system_setting("tts_model") == "voice-a"
        # Rows only B had are merged into the account's network.
        engine.scan_all()
        assert hub.push_once() >= 1
    with a.active():
        _signed_in(fake, a)
        hub.pull_once()
        assert database.get_system_setting("only_on_b") == "yes"
        assert database.get_system_setting("tts_model") == "voice-a"


async def test_quota_exceeded_keeps_lan_sync_and_shows_backlog(workers, fake):
    fake.quota_ops = 3
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        for i in range(6):
            database.set_user_preference("admin", "u", f"k{i}", i)
        engine.scan_all()
        with pytest.raises(hub.HubError):
            hub.push_once()
        status = hub.status()
        assert status["backlog"] > 0 and status["quota"]["limit"] == 3
        assert await hub._loop_body() is False
        assert "quota" in (hub._state.get("last_error") or "")


@pytest.mark.parametrize("combined", [True, False])
async def test_idle_poll_combines_admissions_with_legacy_fallback(workers, fake, combined):
    fake.combined_poll = combined
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        hub.push_once()
        hub.pull_once()
        hub.push_once()
        fake.calls.clear()
        await hub._loop_body()
        routes = [suffix for _, _, suffix in fake.calls]
        assert len(routes) == (1 if combined else 2)
        assert routes[0].endswith("&include_pending=1")
        assert ("network/pending" in routes) is not combined


async def test_quota_cooldown_survives_wakes_restart_and_keeps_admitting(workers, fake, monkeypatch):
    a, b = workers("a"), workers("b")
    now = [172800.0 + 30]
    monkeypatch.setattr(hub.time, "time", lambda: now[0])
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        original_push = fake.push
        attempts = []

        def blocked(*args):
            attempts.append(now[0])
            return {"quota_exceeded": True, "quota_reason": "daily_upload", "quota": {
                "daily_upload_bytes": 99, "daily_upload_limit": 100, "daily_upload_reset_at": 259200000}}

        monkeypatch.setattr(fake, "push", blocked)
        monkeypatch.setattr(fake, "_quota", lambda: {"daily_upload_bytes": 99, "daily_upload_limit": 100,
                                                   "daily_upload_reset_at": 259200000})
        with pytest.raises(hub.UploadBlocked):
            hub.push_once()
    with b.active():
        _signed_in(fake, b)
        hub.register()
    with a.active():
        _signed_in(fake, a)
        # 60 wake-triggered passes plus a process restart do not retry uploads.
        for _ in range(60):
            hub.kick()
            await hub._loop_body()
        hub.reset_for_tests()
        engine.load_context()
        await hub._loop_body()
        assert len(attempts) == 1
        assert fake.devices[b.device_id]["status"] == "approved"
        assert len([c for c in fake.calls if "network/feed?" in c[2]]) == 61
        assert hub.status()["backlog"] > 0
        now[0] = 259200
        assert hub.status()["quota"]["daily_upload_bytes"] == 0
        monkeypatch.setattr(fake, "push", original_push)
        await hub._loop_body()
        assert hub._hub_state()["upload_blocked"] is None
        assert hub.status()["backlog"] == 0


async def test_commit_timeout_retries_without_loss_or_duplicates(workers, fake, monkeypatch):
    from fastapi import HTTPException
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        original = fake.push

        def commit_then_timeout(*args):
            original(*args)
            raise HTTPException(503, "response lost after commit")

        monkeypatch.setattr(fake, "push", commit_then_timeout)
        with pytest.raises(hub.HubError):
            hub.push_once()
        count = len(fake.feed)
        assert count > 0 and hub.status()["backlog"] == count
        hub.reset_for_tests()
        monkeypatch.setattr(fake, "push", original)
        assert hub.push_once() == count
        assert len(fake.feed) == count and hub.status()["backlog"] == 0


def test_hub_receipts_preserve_lan_relay_and_pending_sequence_holes(workers, fake):
    from tests.core.network.harness import join
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        _signed_in(fake, a)
        hub.register()
        hub.push_once()
    with b.active():
        _signed_in(fake, b)
        hub.register()
        hub.pull_once()
        hub.push_once()
    # The origin is offline from the hub; B still relays newly received LAN ops.
    _set_pref(a, "relay", 1)
    exchange(a, b)
    with b.active():
        _signed_in(fake, b)
        assert hub.push_once() > 0
    with a.active():
        _signed_in(fake, a)
        hub.pull_once()
        before = len(fake.calls)
        hub.push_once()
        assert len(fake.calls) == before  # A learned B already relayed these.
        last_ingest = a.sql("SELECT MAX(ingest_id) AS n FROM sync_ops")[0]["n"]
    _set_pref(a, "gap1", 1)
    _set_pref(a, "gap2", 2)
    with a.active():
        ops = [env for _, env in store.ops_after_ingest(last_ingest)]
    assert len(ops) >= 2
    # Cloud can contain a later sequence before its predecessor. Receipt of it
    # must never acknowledge the missing prefix, even after restart/draining.
    fake.feed.append(ops[-1])
    fake.seen.add((ops[-1]["origin_device_id"], ops[-1]["origin_incarnation"], ops[-1]["origin_seq"]))
    with b.active():
        _signed_in(fake, b)
        hub.pull_once()
        hub.reset_for_tests()
        engine.apply_batch(ops[:-1])  # LAN delivery drains the pending cloud op.
        page = store.hub_ops_after_ingest(hub._hub_state()["pushed_ingest_id"], hub._ensure_hub_scope())
        assert ops[-1] not in [env for _, env in page]
        assert ops[0] in [env for _, env in page]
        hub.push_once()
        assert all(op in fake.feed for op in ops)


def test_receipt_and_apply_roll_back_together(workers, fake, monkeypatch):
    from tests.core.network.harness import join
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        database.set_system_setting("tts_model", "atomic-receipt")
        engine.scan_all()
        ops = [env for _, env in store.ops_after_ingest(0) if env["collection"] == "system_settings"]
    with b.active():
        previous = database.get_system_setting("tts_model")
        hub._save_hub_state(binding_scope="test-hub")
        original = store.record_hub_receipt

        def fail_after_receipt(cursor, scope, env):
            original(cursor, scope, env)
            raise RuntimeError("simulated storage failure")

        monkeypatch.setattr(store, "record_hub_receipt", fail_after_receipt)
        with pytest.raises(RuntimeError, match="simulated storage failure"):
            engine.apply_batch(ops, hub_scope="test-hub")
        assert b.sql("SELECT * FROM sync_hub_receipts") == []
        assert database.get_system_setting("tts_model") == previous
        monkeypatch.setattr(store, "record_hub_receipt", original)
        engine.load_context()
        engine.apply_batch(ops, hub_scope="test-hub")
        assert database.get_system_setting("tts_model") == "atomic-receipt"


async def test_upload_quota_does_not_prevent_applying_remote_edits(workers, fake):
    from tests.core.network.harness import join
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        _signed_in(fake, a)
        hub.register()
        hub.push_once()
    with b.active():
        _signed_in(fake, b)
        hub.register()
        hub.pull_once()
        hub.push_once()
    _set_pref(b, "remote_while_blocked", 4)
    with b.active():
        _signed_in(fake, b)
        hub.push_once()
    fake.quota_ops = len(fake.feed)
    _set_pref(a, "local_backlog", 5)
    with a.active():
        _signed_in(fake, a)
        await hub._loop_body()
        assert hub.status()["upload_blocked"]["reason"] == "ops"
        assert hub.status()["backlog"] > 0
        assert _pref(a, "remote_while_blocked") == 4


def test_feed_cursor_is_retained_when_receipt_persistence_fails(workers, fake, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        hub.push_once()
        original = store.record_hub_receipt

        def fail(*args):
            raise RuntimeError("disk failure")

        monkeypatch.setattr(store, "record_hub_receipt", fail)
        with pytest.raises(hub.HubError, match="feed cursor kept"):
            hub.pull_once()
        assert hub._hub_state().get("cursor", 0) == 0
        hub.reset_for_tests()
        monkeypatch.setattr(store, "record_hub_receipt", original)
        hub.pull_once()
        assert hub._hub_state()["cursor"] == len(fake.feed)


def test_legacy_quota_response_uses_persistent_bounded_reprobe(workers, fake, monkeypatch):
    a = workers("a")
    now = [100.0]
    monkeypatch.setattr(hub.time, "time", lambda: now[0])
    fake.quota_ops = 1
    original = fake.push

    def legacy(*args):
        result = original(*args)
        result.pop("quota_reason", None)
        return result

    monkeypatch.setattr(fake, "push", legacy)
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "legacy-quota")
        engine.scan_all()
        _signed_in(fake, a)
        hub.register()
        with pytest.raises(hub.UploadBlocked):
            hub.push_once()
        assert hub.status()["upload_blocked"]["retry_at"] == 3700
        hub.reset_for_tests()
        calls = len(fake.calls)
        now[0] = 3699
        with pytest.raises(hub.UploadBlocked):
            hub.push_once()
        assert len(fake.calls) == calls
        fake.quota_ops = 1000
        now[0] = 3700
        assert hub.push_once() > 0
        assert hub.status()["upload_blocked"] is None


def test_response_from_previous_binding_cannot_acknowledge_new_network(workers, fake, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        original = fake.push

        def change_binding_after_commit(*args):
            result = original(*args)
            fake.servers["current"] = "another-server"
            hub._ensure_hub_scope()
            return result

        monkeypatch.setattr(fake, "push", change_binding_after_commit)
        with pytest.raises(hub.HubError, match="changed during sync"):
            hub.push_once()
        assert hub._hub_state()["pushed_ingest_id"] == 0
        assert hub._hub_state()["registered"] is False
        with pytest.raises(RuntimeError, match="changed during feed"):
            engine.apply_batch(fake.feed, hub_scope="old-scope")


async def test_recovery_key_restores_a_clean_device_with_every_worker_off(workers, fake):
    a, d = workers("a"), workers("d")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-9")
        database.create_tag(tag_id="t1", name="urgent", color="#f00")
        engine.scan_all()
        _signed_in(fake, a)
        hub.register()
        key = recovery.create()["recovery_key"]
        hub._upload_recovery()
        hub.push_once()
        assert fake.recovery and fake.recovery["check"]
    # Every worker is off; D signs in with the recovery key.
    with d.active():
        _signed_in(fake, d)
        with pytest.raises(recovery.RecoveryError):
            await recovery.unlock_from_hub("A1-00000-00000-00000-00000-00000-0", device_name="D")
        result = await recovery.unlock_from_hub(key, device_name="D")
        assert result["restored"]
        ctx = engine.context()
        assert ctx.bootstrap_state == "ready" and ctx.member(a.device_id).status == "active"
        assert ctx.member(d.device_id).status == "active"
        assert database.get_system_setting("tts_model") == "voice-9"
        assert database.get_tag("t1")["name"] == "urgent"
        assert fake.devices[d.device_id]["status"] == "active"
        hub.push_once()
    # A comes back later and accepts D purely from the feed.
    with a.active():
        _signed_in(fake, a)
        assert hub.pull_once() >= 1
        assert engine.context().member(d.device_id).status == "active"
    _set_pref(d, "from_d", 5)
    with d.active():
        _signed_in(fake, d)
        hub.push_once()
    with a.active():
        _signed_in(fake, a)
        hub.pull_once()
    assert _pref(a, "from_d") == 5


def test_hub_device_routes_approve_and_reject_through_the_api(workers, fake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import network as network_api
    from app.api.auth import require_admin, require_auth

    a, b = workers("a"), workers("b")
    app = FastAPI()
    app.include_router(network_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    client = TestClient(app)
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
    with b.active():
        _signed_in(fake, b)
        hub.register()
    with a.active():
        _signed_in(fake, a)
        listed = client.get("/api/v1/network/hub/devices").json()["devices"]
        assert [d["device_id"] for d in listed] == [b.device_id]
        approved = client.post(f"/api/v1/network/hub/devices/{b.device_id}/approve", json={})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        assert client.post(f"/api/v1/network/hub/devices/{b.device_id}/approve", json={}).status_code == 400
        assert client.post("/api/v1/network/hub/devices/dev_nobody/reject", json={}).status_code == 200
        status = client.get("/api/v1/network/status").json()
        assert status["hub"]["linked"] and status["hub"]["registered"]


@pytest.fixture(autouse=True)
def explicit_cloud_backup(monkeypatch):
    """These tests exercise the optional cloud transport."""
    from app.network import backup
    monkeypatch.setattr(backup, "enabled", lambda: True)
