import contextlib
import json
from unittest.mock import patch

import pytest
from app.core import checkout_transfer
from app.core.config import settings
from app.db import database, projects_v2, checkouts, runs
from app.db.base import get_db
from app.network import control, engine, handoff, tokens, transport
from tests.core.network.harness import Worker, join, converge
from tests.core.test_checkout_transfer import repositories, git


@pytest.fixture
def pair(tmp_path, repositories, monkeypatch):
    # Worker.active isolates databases and network identities. Also isolate the
    # legacy install id: the test process may inherit one explicit INSTANCE_ID.
    original_active = Worker.active

    @contextlib.contextmanager
    def active(worker):
        with original_active(worker), patch.object(settings, "instance_id", "handoff-" + worker.name):
            yield worker

    monkeypatch.setattr(Worker, "active", active)
    a, b = Worker("a", tmp_path), Worker("b", tmp_path)
    source, target = repositories
    with a.active():
        engine.create_network("Handoff test")
    with b.active():
        target_instance = settings.effective_instance_id
    join(a, b, legacy_instance_id=target_instance)
    for worker, cwd in ((a, source), (b, target)):
        with worker.active():
            project = projects_v2.upsert_project("git:github.com/example/repo", name="Repo")
            checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(cwd))
    with a.active():
        checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(source), branch="feature/only-on-worker")
        database.create_session("handoff-test", "default", title="Handoff", project_v2_id=project["id"])
        database.update_session("handoff-test", sdk_session_id="ses_source")
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET branch=%s WHERE id=%s", ("feature/only-on-worker", "handoff-test"))
        database.add_session_message("handoff-test", "user", "Keep this conversation")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO fluid_sessions (session_id,head_node_id,created_at) VALUES (%s,%s,%s)", ("ses_source","node1",1.0))
            cur.execute("INSERT INTO fluid_session_nodes (session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)",
                        ("ses_source","node1",1.0,json.dumps({"role":"user","content":"Native history"})))
        engine.scan_all()
    converge([a,b])
    yield a,b,source,target
    a.close()
    b.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


@pytest.mark.asyncio
async def test_model_receipts_move_under_the_handoff_lease_but_live_prompts_do_not(pair, monkeypatch):
    from app.core import model_override as mo
    from app.db import interactions

    a, b, _, _ = pair
    from app.db import turns
    with a.active():
        assert mo._remember_approval("handoff-test", "opus")
        turn = turns.enqueue_turn("handoff-test", "user", prompt="use Haiku")
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
        assert await mo.confirm("handoff-test", "default", model="haiku", baseline="opus", action="start") == mo.NAMED
        launch = turns.enqueue_turn("handoff-test", "agent_message", prompt="start brief",
                                    command_id="orchestration:start:launch",
                                    payload={"orchestration_sender": "origin-chat"})
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (launch["id"],))
        interactions.create_interaction("handoff-test", "permission", {"tool_name": "Bash"}, None,
                                        interaction_id="live-prompt")
    phases = []

    async def call(device, action, body, **kwargs):
        assert device == a.device_id and action == "handoff"
        phases.append(body["phase"])
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        assert not mo._has_approval("handoff-test", "opus")
        await handoff.receive(database.get_session("handoff-test"))
        assert mo._has_approval("handoff-test", "opus")
        assert mo._has_named("handoff-test", "haiku")
        from app.core.session_orchestration import _start_turn
        assert _start_turn("handoff-test")["payload"]["orchestration_sender"] == "origin-chat"
        assert not interactions.get_interaction("live-prompt")
        # Re-importing the same leased snapshot cannot duplicate receipts or
        # launch markers, and cannot erase unrelated session receipts.
        database.create_session("unrelated", "default", title="Unrelated")
        assert mo._remember_approval("unrelated", "haiku")
        snapshot = {"sdk_session_id": database.get_session("handoff-test")["sdk_session_id"],
                    "history": handoff._history(database.get_session("handoff-test"))}
        for _ in range(2):
            handoff.install_history("handoff-test", snapshot, ".")
        assert mo._has_approval("unrelated", "haiku")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM pending_interactions WHERE session_id='handoff-test'")
            assert cursor.fetchone()["n"] == 2
            cursor.execute("SELECT COUNT(*) AS n FROM turns WHERE session_id='handoff-test' "
                           "AND command_id LIKE 'orchestration:start:%%'")
            assert cursor.fetchone()["n"] == 1
    with a.active():
        assert interactions.get_interaction("live-prompt")["state"] == "pending"
    assert phases == ["begin", "commit"]


@pytest.mark.asyncio
async def test_receive_transfers_unpushed_dirty_branch_and_native_history(pair, monkeypatch):
    a,b,source,target = pair
    (source / "file.txt").write_text("local staged change")
    git(source,"add",".")
    (source / "new.txt").write_text("untracked work")
    before = git(source, "rev-parse", "HEAD").decode().strip()
    from app.db import session_events
    with a.active():
        session_events.append_events("handoff-test", [{"type": "message.user", "payload": {"content": "unsynced final event"}}])
    phases=[]
    commits=[]
    async def call(device, action, body, **kwargs):
        assert device == a.device_id and action == "handoff"
        phases.append(body["phase"])
        with a.active():
            result = await control.handle(action, body, from_device=b.device_id)
            if body["phase"] == "commit":
                commits.append(body)
            return result
    monkeypatch.setattr(control, "call", call)
    with b.active():
        session=database.get_session("handoff-test")
        cwd=await handoff.receive(session)
        assert checkout_transfer.fingerprint(cwd) == checkout_transfer.fingerprint(source)
        assert checkout_transfer.fingerprint(cwd) != before
        assert (source / "new.txt").read_text() == "untracked work"
        row=database.get_session("handoff-test")
        assert row["owner_device_id"] == b.device_id
        assert row["sdk_session_id"].startswith("ses_") and row["sdk_session_id"] != "ses_source"
        with get_db() as conn:
            cur=conn.cursor()
            cur.execute("SELECT * FROM fluid_session_nodes WHERE session_id=%s",(row["sdk_session_id"],))
            assert len(cur.fetchall()) == 1
        assert database.get_session_messages("handoff-test")[0]["content"] == "Keep this conversation"
        assert session_events.get_events("handoff-test", 0)[-1]["payload"]["content"] == "unsynced final event"
        assert session_events.get_max_seq("handoff-test") == 1
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert checkout_transfer.fingerprint(source) != before
    assert phases == ["begin","commit"]
    with a.active():
        assert handoff.finish("handoff-test", b.device_id, commits[0]) == {"committed": True}
    converge([a,b])
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert database.get_session("handoff-test")["sdk_session_id"].startswith("ses_")


@pytest.mark.asyncio
async def test_offline_worker_never_falls_back_to_remote_ref(pair, monkeypatch):
    a,b,source,target=pair
    async def offline(*args,**kwargs):
        raise control.RunnerUnreachable("Worker offline")
    monkeypatch.setattr(control,"call",offline)
    with b.active():
        with pytest.raises(control.RunnerUnreachable):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert not git(target,"branch","--list","feature/only-on-worker").strip()


@pytest.mark.asyncio
async def test_changed_source_aborts_commit_and_keeps_owner(pair,monkeypatch):
    a,b,source,target=pair
    async def call(device,action,body,**kwargs):
        with a.active():
            if body["phase"] == "commit":
                (source/"file.txt").write_text("changed during transfer")
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    with b.active():
        with pytest.raises(ValueError,match="files changed"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            cur=conn.cursor()
            cur.execute("SELECT state FROM runs WHERE session_id=%s",("handoff-test",))
            assert cur.fetchone()["state"] == "released"


@pytest.mark.asyncio
async def test_fluid_symlink_outside_target_checkout_refuses_and_keeps_owner(pair, monkeypatch):
    a,b,source,target=pair
    outside = target.parent / "outside-fluid"
    outside.mkdir()
    try:
        (target / ".fluid").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    phases=[]
    async def call(device,action,body,**kwargs):
        phases.append(body["phase"])
        with a.active():
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    with b.active():
        # A ValueError, so the resume endpoint answers 409 rather than 500.
        with pytest.raises(ValueError,match="outside the project checkout"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    # The receiving path is checked before the owner is asked to commit/push.
    assert phases == []
    assert not git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").strip()
    assert list(outside.iterdir()) == []


def test_live_source_run_blocks_snapshot(pair):
    a,b,source,target=pair
    with a.active():
        run=runs.claim_run("handoff-test",settings.effective_instance_id,300)
        assert run
        with pytest.raises(ValueError,match="busy"):
            handoff.begin("handoff-test", b.device_id, checkout_transfer.remote_identity(target))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id

@pytest.mark.asyncio
async def test_projectless_session_transfers_history_only(pair, monkeypatch):
    a,b,source,target=pair
    for worker in (a,b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s",("handoff-test",))
    async def call(device,action,body,**kwargs):
        with a.active():
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    monkeypatch.setattr(checkout_transfer, "export_checkout",
                        lambda *a, **k: pytest.fail("plain chat must not export a checkout"))
    with b.active():
        cwd = await handoff.receive(database.get_session("handoff-test"))
        assert cwd == str(settings.effective_workspace_dir)
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id

@pytest.mark.asyncio
async def test_push_failure_releases_reservation_without_moving_owner(pair, monkeypatch):
    a, b, source, target = pair
    original_git = checkout_transfer.git
    def fail_push(cwd, *args):
        if args[0] == "push":
            raise ValueError("Git push failed during resume")
        return original_git(cwd, *args)
    monkeypatch.setattr(checkout_transfer, "git", fail_push)
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    (source / "new.txt").write_text("saved despite push failure")
    with b.active():
        with pytest.raises(ValueError, match="push failed"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active(), get_db() as conn:
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        cur = conn.cursor()
        cur.execute("SELECT state FROM runs WHERE session_id=%s", ("handoff-test",))
        assert cur.fetchone()["state"] == "released"
    assert not git(source, "status", "--porcelain")


@pytest.mark.asyncio
async def test_missing_origin_refuses_before_contacting_owner(pair, monkeypatch):
    a, b, source, target = pair
    git(target, "remote", "remove", "origin")
    from unittest.mock import AsyncMock
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(ValueError):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    call.assert_not_called()


def test_legacy_receiver_cannot_trigger_publication(pair):
    a, b, source, target = pair
    before = git(source, "rev-parse", "HEAD")
    with a.active():
        with pytest.raises(ValueError, match="Update both"):
            handoff.begin("handoff-test", b.device_id)
    assert git(source, "rev-parse", "HEAD") == before

@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_base", [False, True])
async def test_base_session_without_branch_metadata_resolves_after_resume(pair, monkeypatch, reuse_base):
    from app.core import project_resolver
    a, b, source, target = pair
    if reuse_base:
        git(source, "checkout", "main")
        (source / "file.txt").write_text("new base checkout work")
    # The shared fixture registers this path as a branch checkout. A base
    # session instead has a branch-NULL checkout row at that path.
    with a.active():
        project_id = database.get_session("handoff-test")["project_v2_id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(source), branch="")
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET branch=NULL WHERE id=%s", ("handoff-test",))
    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    with b.active():
        cwd = await handoff.receive(database.get_session("handoff-test"))
        assert checkout_transfer.fingerprint(cwd) == checkout_transfer.fingerprint(source)
        row = database.get_session("handoff-test")
        assert row["branch"] == (None if reuse_base else "feature/only-on-worker")
        assert project_resolver.has_local_base_checkout(row)
        assert project_resolver.resolve_existing_cwd(row) == cwd
        if reuse_base:
            assert cwd == str(target)
            assert row["worktree_branch"] is None
    with a.active():
        assert database.get_session("handoff-test")["branch"] == (None if reuse_base else "feature/only-on-worker")


def _base_session_on_clean_main(pair, monkeypatch):
    """The owner's base-checkout session on a clean main that matches origin."""
    a, b, source, _target = pair
    git(source, "checkout", "main")
    with a.active():
        project_id = database.get_session("handoff-test")["project_v2_id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(source), branch="")
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET branch=NULL WHERE id=%s", ("handoff-test",))

    async def call(device, action, body, **kwargs):
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)


@pytest.mark.asyncio
async def test_continue_here_clean_main_with_receiver_work_already_merged(pair, monkeypatch):
    """Owner report 2026-10-03: Continue here on a session bound to a clean,
    in-sync main refused. The owner had nothing to publish; the receiving
    worker's main held a commit that origin already has through a squash merge."""
    _a, b, source, target = pair
    (target / "fix.txt").write_text("fixed on the receiver")
    git(target, "add", ".")
    git(target, "commit", "-m", "receiver fix")
    (source / "fix.txt").write_text("fixed on the receiver")
    (source / "review.txt").write_text("review follow-up")
    git(source, "checkout", "main")
    git(source, "add", ".")
    git(source, "commit", "-m", "Squash-merge the receiver fix (#1)")
    git(source, "push", "origin", "main")
    _base_session_on_clean_main(pair, monkeypatch)
    head = git(source, "rev-parse", "HEAD").decode().strip()
    with b.active():
        cwd = await handoff.receive(database.get_session("handoff-test"))
        assert cwd == str(target)
        assert checkout_transfer.fingerprint(target) == head
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    assert git(source, "rev-parse", "HEAD").decode().strip() == head


@pytest.mark.asyncio
async def test_continue_here_names_unpublished_receiver_commit_and_keeps_owner(pair, monkeypatch):
    a, b, source, target = pair
    (target / "mine.txt").write_text("only on the receiver")
    git(target, "add", ".")
    git(target, "commit", "-m", "receiver only")
    git(source, "checkout", "main")
    git(source, "push", "origin", "main")
    _base_session_on_clean_main(pair, monkeypatch)
    with b.active():
        with pytest.raises(ValueError, match=r"Branch main in the receiving worker's checkout .* has 1 commit .* not on origin"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert (target / "mine.txt").exists()


@pytest.mark.asyncio
async def test_source_branch_switch_at_same_commit_refuses_ownership(pair, monkeypatch):
    a, b, source, target = pair
    async def call(device, action, body, **kwargs):
        with a.active():
            if body["phase"] == "commit":
                git(source, "checkout", "-b", "different-branch")
            return await control.handle(action, body, from_device=b.device_id)
    monkeypatch.setattr(control, "call", call)
    with b.active():
        with pytest.raises(ValueError, match="different branch"):
            await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id



@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_move_endpoint_produces_git_and_native_context_handoff(pair, monkeypatch, dirty):
    from app.api.sessions import move_session_endpoint
    from app.api.sessions_helpers import MoveSessionRequest
    from app.db import session_events
    from app.network import crypto
    from tests.api.helpers import fake_request

    a, b, source, target = pair
    with b.active():
        target_instance = settings.effective_instance_id
    with a.active():
        database.register_install(target_instance, "Target", None, "linux", "test")
        session_events.append_events("handoff-test", [{"type": "message.user", "payload": {"content": "Final unsynced context"}}])
    if dirty:
        (source / "file.txt").write_text("dirty staged work")
        git(source, "add", "file.txt")
        (source / "new.txt").write_text("untracked source work")
    (source / ".env").write_text("NEVER_COPY_THIS=private")
    phases = []
    exported = []

    # Exercise actual peer challenge/signature tokens and HTTP control routing;
    # only transport is in-process, so no real user worker or remote is touched.
    import httpx
    from app.core import cluster
    from app.db import network as network_store
    from tests.core.network.test_transport import ServedBy

    served = {a.device_id: ServedBy(a, "http://a.local:8000"),
              b.device_id: ServedBy(b, "http://b.local:8000")}
    real_async, real_call = httpx.AsyncClient, control.call
    transport.reset_for_tests()
    tokens.reset_for_tests()
    # Authentication publishes the caller's own address. Use each actual test
    # server's URL, not a host/CI LAN address that the MockTransport cannot serve.
    monkeypatch.setattr(settings, "cluster_advertise_url", None)
    for worker, peer in ((a, b), (b, a)):
        with worker.active():
            cluster.set_advertise_url_override(served[worker.device_id].base_url)
            assert engine._own_endpoints() == [served[worker.device_id].base_url]
            network_store.touch_member(peer.device_id, endpoints=[served[peer.device_id].base_url])

    def route(request):
        server = served[a.device_id] if request.url.host == "a.local" else served[b.device_id]
        return server.handler(request)

    def async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(route)
        return real_async(*args, **kwargs)

    async def call(device, action, body, **kwargs):
        assert action in ("handoff", "git-state")
        if action == "handoff":
            phases.append(body["phase"])
            if body["phase"] == "receive":
                assert body == {"session_id": "handoff-test", "phase": "receive"}
        reply = await real_call(device, action, body, **kwargs)
        if action == "git-state":
            assert reply["eligible"] is True
            assert all("path" not in row for row in reply["checkouts"])
        if action == "handoff" and body["phase"] == "begin":
            ctx = engine.require_context()
            raw = crypto.decrypt_payload(ctx.network_key, reply["payload"], handoff._aad("handoff-test", b.device_id))
            exported.append(json.loads(raw))
        return reply

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr("app.core.placement.capable_runners", lambda *args: [target_instance])
    with a.active():
        # The target's real checkout was registered on B, not seeded into A.
        project_id = database.get_session("handoff-test")["project_v2_id"]
        assert not checkouts.has_active_base_checkout(project_id, target_instance)
        result = await move_session_endpoint(fake_request(), "handoff-test", MoveSessionRequest(target_instance_id=target_instance), token="test")
        assert result.moved is True
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        head = checkout_transfer.fingerprint(source)
        assert git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").decode().split()[0] == head
    assert phases == ["receive", "begin", "commit"]
    for server in served.values():
        assert ("POST", "/api/v1/network/peer/auth/challenge") in server.requests
        assert ("POST", "/api/v1/network/peer/auth/token") in server.requests
        assert ("POST", "/api/v1/network/peer/control/handoff") in server.requests
    assert set(exported[0]["checkout"]) == {"protocol", "branch", "head", "fingerprint", "remote"}
    with b.active():
        from app.core import project_resolver
        assert network_store.get_member(a.device_id)["endpoints"] == [served[a.device_id].base_url]
        row = database.get_session("handoff-test")
        cwd = project_resolver.resolve_existing_cwd(row)
        assert checkout_transfer.fingerprint(cwd) == head
        assert row["owner_device_id"] == b.device_id
        placement = row["placement"]
        assert (json.loads(placement) if isinstance(placement, str) else placement) == {"where": "runner:" + b.device_id}
        assert row["sdk_session_id"].startswith("ses_") and row["sdk_session_id"] != "ses_source"
        assert database.get_session_messages("handoff-test")[0]["content"] == "Keep this conversation"
        assert session_events.get_events("handoff-test", 0)[-1]["payload"]["content"] == "Final unsynced context"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT message FROM fluid_session_nodes WHERE session_id=%s", (row["sdk_session_id"],))
            message = cur.fetchone()["message"]
            assert (json.loads(message) if isinstance(message, str) else message)["content"] == "Native history"
        from pathlib import Path
        assert not (Path(cwd) / ".env").exists()
        if dirty:
            assert (Path(cwd) / "new.txt").read_text() == "untracked source work"
    converge([a, b])
    with b.active():
        assert database.get_session("handoff-test")["sdk_session_id"] == row["sdk_session_id"]
        assert project_resolver.resolve_existing_cwd(database.get_session("handoff-test")) == cwd


@pytest.mark.asyncio
async def test_move_preserves_dirty_destination_and_source_ownership(pair, monkeypatch, tmp_path):
    a, b, source, target = pair
    state = checkout_transfer.export_checkout(source, "feature/only-on-worker")
    from pathlib import Path
    destination = Path(checkout_transfer.import_checkout(target, tmp_path / "existing", state))
    (destination / "file.txt").write_text("Keep destination edits")
    phases = []

    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        phases.append(body["phase"])
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)

    monkeypatch.setattr(control, "call", call)
    with a.active():
        with pytest.raises(ValueError, match="receiving checkout has local changes"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM runs WHERE session_id=%s", ("handoff-test",))
            assert cur.fetchone()["state"] == "released"
    assert phases == ["receive", "begin", "cancel"]
    assert (destination / "file.txt").read_text() == "Keep destination edits"
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("worker", "revoked"), ("client", "active")])
async def test_move_rejects_ineligible_network_destination_before_git(pair, monkeypatch, kind, status):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    a, b, source, target = pair
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    with a.active():
        ctx = engine.require_context()
        ctx.members[b.device_id] = replace(ctx.member(b.device_id), kind=kind, status=status)
        with pytest.raises(ValueError, match="active worker"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    call.assert_not_called()
    assert not git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["foreign-network-device", "revoked", "client"])
async def test_receive_request_requires_authenticated_active_worker(pair, monkeypatch, caller):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    a, b, source, target = pair
    receive = AsyncMock()
    monkeypatch.setattr(handoff, "receive", receive)
    with b.active():
        ctx = engine.require_context()
        peer = caller
        if caller in ("revoked", "client"):
            peer = a.device_id
            ctx.members[peer] = replace(ctx.member(peer), status="revoked" if caller == "revoked" else "active", kind="client" if caller == "client" else "worker")
        with pytest.raises(PermissionError, match="authenticated receiving worker"):
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "receive"}, from_device=peer)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    receive.assert_not_called()


@pytest.mark.asyncio
async def test_move_to_this_worker_uses_the_same_leased_protocol(pair, monkeypatch):
    a, b, source, target = pair
    phases = []

    async def call(device, action, body, **kwargs):
        assert device == a.device_id
        phases.append(body["phase"])
        with a.active():
            return await control.handle(action, body, from_device=b.device_id)

    monkeypatch.setattr(control, "call", call)
    with b.active():
        assert await handoff.move(database.get_session("handoff-test"), b.device_id) is True
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    assert phases == ["begin", "commit"]


@pytest.mark.asyncio
async def test_move_cannot_publish_while_source_has_live_run(pair, monkeypatch):
    a, b, source, target = pair

    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)

    monkeypatch.setattr(control, "call", call)
    with a.active():
        assert runs.claim_run("handoff-test", settings.effective_instance_id, 300)
        with pytest.raises(ValueError, match="busy"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert not git(source, "ls-remote", "origin", "refs/heads/feature/only-on-worker").strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    control.RunnerUnreachable("Source offline"),
    control.ControlRefused("Source refused the handoff"),
])
async def test_receive_control_hop_preserves_owner_refusal(pair, monkeypatch, error):
    from unittest.mock import AsyncMock
    a, b, source, target = pair
    monkeypatch.setattr(handoff, "receive", AsyncMock(side_effect=error))
    with b.active():
        with pytest.raises(ValueError, match=str(error)):
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "receive"}, from_device=a.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id


@pytest.mark.asyncio
async def test_receiver_that_cannot_reach_the_owner_names_both_workers(pair, monkeypatch):
    """The live #1524 failure: the destination's call back to the owner never connects."""
    import httpx
    from app.db import network as store
    a, b, source, target = pair
    real_async = httpx.AsyncClient
    dialed = []

    def refuse(request):
        dialed.append(str(request.url))
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution", request=request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *args, **kwargs: real_async(**{**kwargs, "transport": httpx.MockTransport(refuse)}))
    monkeypatch.setattr(control, "CONNECT_RETRY_DELAY", 0)
    head = git(source, "rev-parse", "HEAD")
    with b.active():
        transport._links[a.device_id] = transport.PeerLink(a.device_id, ["https://a.tunnel.example"])
        with pytest.raises(ValueError) as raised:
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "receive"},
                                 from_device=a.device_id)
        owner_name = store.get_member(a.device_id)["name"]  # the founder defaults to its host name
        assert str(raised.value) == f"Couldn't reach {owner_name} from b (address lookup failed on its only route)."
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM runs WHERE state='active' AND runner_id LIKE %s",
                        (handoff.HOLD + "%",))
            assert cur.fetchone()["n"] == 0
    assert len(dialed) == control.CONNECT_ROUNDS
    assert git(source, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_session_handed_back_to_its_first_worker_lists_as_local(pair, monkeypatch):
    """A -> B -> A: the first worker owns the row again and must not list it as
    remote with an unknown state because instance_id still named B."""
    from app.api import sessions_helpers

    a,b,source,target=pair
    for worker in (a,b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s",("handoff-test",))

    def owner_call(owner):
        async def call(device,action,body,**kwargs):
            with owner.active():
                return await control.handle(action,body,from_device=(b if owner is a else a).device_id)
        return call

    monkeypatch.setattr(control,"call",owner_call(a))
    with b.active():
        await handoff.receive(database.get_session("handoff-test"))
    monkeypatch.setattr(control,"call",owner_call(b))
    with a.active():
        await handoff.receive(database.get_session("handoff-test"))
        row = database.get_session("handoff-test")
        assert row["owner_device_id"] == a.device_id
        assert row["instance_id"] == settings.effective_instance_id
        sessions_helpers._annotate_cluster_fields([row])
        assert row["is_remote"] is False
        assert "remote_runtime_state" not in row


# --- Other sessions sharing the published or receiving checkout (#1524) ---

BRANCH = "feature/only-on-worker"


def _route_between(a, b, phases):
    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        phases.append(body["phase"])
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)
    return call


def _bind_session(sid, branch, cwd=None):
    """Bind another session to this worker's canonical checkout of ``branch``."""
    from app.core import project_resolver
    project_id = database.get_session("handoff-test")["project_v2_id"]
    database.create_session(sid, "default", title=sid, project_v2_id=project_id)
    database.set_session_branch(sid, branch)
    row = database.get_session(sid)
    assert control.owner_of(row) is None
    if cwd is not None:
        assert project_resolver.resolve_existing_cwd(row) == str(cwd)
    return row


def _start_work(sid, live_lease):
    from app.db import turns
    run = runs.claim_run(sid, settings.effective_instance_id, 300 if live_lease else -1)
    assert run
    turn = turns.enqueue_turn(sid, "user", prompt="Working in this checkout")
    started = turns.start_turn(turn["id"], run["id"], settings.effective_instance_id)
    assert started["state"] == "running"
    return run, turn


def _assert_still_working(sid, run, turn, live_lease):
    from app.db import turns
    assert turns.get_turn(turn["id"])["state"] == "running"
    active = runs.get_active_run(sid)
    if live_lease:
        assert active and active["id"] == run["id"]
    else:
        assert active is None or active["id"] == run["id"]


def _active_holds():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT session_id, runner_id FROM runs WHERE state='active'")
        return [row["session_id"] for row in cur.fetchall() if row["runner_id"].startswith(handoff.HOLD)]


def _registered_receiving_worktree(a, b, source, target, tmp_path):
    """B has a real registered checkout of the branch, behind the source."""
    from pathlib import Path
    with a.active():
        state = checkout_transfer.export_checkout(source, BRANCH)
    destination = Path(checkout_transfer.import_checkout(target, tmp_path / "existing", state))
    with b.active():
        project_id = database.get_session("handoff-test")["project_v2_id"]
        checkouts.upsert_checkout(project_id, settings.effective_instance_id, str(destination), branch=BRANCH)
    (source / "file.txt").write_text("newer source work")
    git(source, "commit", "-am", "newer source work")
    return destination, state["head"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("live_lease", [True, False])
async def test_move_refuses_publication_while_another_session_works_in_source_checkout(pair, monkeypatch, dirty, live_lease):
    a, b, source, target = pair
    if dirty:
        (source / "file.txt").write_text("dirty staged work")
        git(source, "add", "file.txt")
        (source / "new.txt").write_text("untracked source work")
    head = git(source, "rev-parse", "HEAD")
    status = git(source, "status", "--porcelain")
    with a.active():
        _bind_session("sibling", BRANCH, cwd=source)
        run, turn = _start_work("sibling", live_lease)
    phases = []
    monkeypatch.setattr(control, "call", _route_between(a, b, phases))
    with a.active():
        with pytest.raises(ValueError, match="Another session is working in this checkout"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        _assert_still_working("sibling", run, turn, live_lease)
        assert runs.get_active_run("handoff-test") is None
        assert _active_holds() == []
    assert phases == ["receive", "begin"]
    assert git(source, "rev-parse", "HEAD") == head
    assert git(source, "status", "--porcelain") == status
    assert not git(source, "ls-remote", "origin", "refs/heads/" + BRANCH).strip()
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert not git(target, "branch", "--list", BRANCH).strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("live_lease", [True, False])
async def test_move_refuses_receiving_checkout_used_by_another_session(pair, monkeypatch, tmp_path, dirty, live_lease):
    a, b, source, target = pair
    destination, published = _registered_receiving_worktree(a, b, source, target, tmp_path)
    if dirty:
        (source / "new.txt").write_text("untracked source work")
    source_head = git(source, "rev-parse", "HEAD")
    with b.active():
        _bind_session("receiver-sibling", BRANCH, cwd=destination)
        run, turn = _start_work("receiver-sibling", live_lease)
    phases = []
    monkeypatch.setattr(control, "call", _route_between(a, b, phases))
    with a.active():
        with pytest.raises(ValueError, match="Another session is working in this checkout"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        assert runs.get_active_run("handoff-test") is None
    # Refused before the owner was asked to commit or push anything.
    assert phases == ["receive"]
    assert git(destination, "rev-parse", "HEAD").decode().strip() == published
    assert git(source, "rev-parse", "HEAD") == source_head
    assert git(source, "ls-remote", "origin", "refs/heads/" + BRANCH).decode().split()[0] == published
    if dirty:
        assert (source / "new.txt").read_text() == "untracked source work"
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
        _assert_still_working("receiver-sibling", run, turn, live_lease)
        assert _active_holds() == []


@pytest.mark.asyncio
async def test_receiving_checkout_work_started_during_publication_blocks_import(pair, monkeypatch, tmp_path):
    """Work that starts on the receiving checkout after the first check still blocks the import."""
    a, b, source, target = pair
    destination, published = _registered_receiving_worktree(a, b, source, target, tmp_path)
    with b.active():
        _bind_session("late-sibling", BRANCH, cwd=destination)
    phases = []
    route = _route_between(a, b, phases)
    started = {}

    async def call(device, action, body, **kwargs):
        reply = await route(device, action, body, **kwargs)
        if body["phase"] == "begin":
            with b.active():
                # The pre-publication hold blocks run admission...
                assert runs.claim_run("late-sibling", "probe", 60) is None
                # ...so model a hold that lapsed while the owner published and
                # a turn that started in that gap.
                with get_db() as conn:
                    conn.cursor().execute(
                        "UPDATE runs SET lease_expires_at=NOW() + (%s * INTERVAL '1 second') "
                        "WHERE session_id=%s AND state='active'", (-1, "late-sibling"))
                started["run"], started["turn"] = _start_work("late-sibling", True)
        return reply

    monkeypatch.setattr(control, "call", call)
    with a.active():
        with pytest.raises(ValueError, match="Another session is working in this checkout"):
            await handoff.move(database.get_session("handoff-test"), b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert phases == ["receive", "begin", "cancel"]
    assert git(destination, "rev-parse", "HEAD").decode().strip() == published
    with b.active():
        _assert_still_working("late-sibling", started["run"], started["turn"], True)
        assert _active_holds() == []
    with a.active():
        assert runs.get_active_run("handoff-test") is None
        assert _active_holds() == []


@pytest.mark.asyncio
async def test_idle_source_checkout_users_cannot_start_until_the_handoff_ends(pair, monkeypatch):
    from app.core import turn_claim
    a, b, source, _ = pair
    with a.active():
        _bind_session("idle-sibling", BRANCH, cwd=source)
    real_export = checkout_transfer.export_checkout
    probes = {}

    def export(*args, **kwargs):
        # Inside the owner's publication: admission is held for every session
        # on this checkout/branch, including one bound after the scan.
        probes["sibling_claim"] = runs.claim_run("idle-sibling", "probe", 60)
        _bind_session("new-session", BRANCH)
        _bind_session("other-branch", "unrelated-branch")
        probes["new_held"] = handoff.admission_held("new-session")
        probes["other_held"] = handoff.admission_held("other-branch")
        return real_export(*args, **kwargs)

    kicks = []
    monkeypatch.setattr(turn_claim, "kick", lambda name=None: kicks.append(name))
    monkeypatch.setattr(checkout_transfer, "export_checkout", export)
    monkeypatch.setattr(control, "call", _route_between(a, b, []))
    with a.active():
        assert await handoff.move(database.get_session("handoff-test"), b.device_id) is True
        assert probes == {"sibling_claim": None, "new_held": True, "other_held": False}
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert _active_holds() == []
        assert not handoff.admission_held("new-session")
        assert runs.claim_run("idle-sibling", settings.effective_instance_id, 60)
    assert "turn_claim" in kicks


@pytest.mark.asyncio
async def test_idle_receiving_checkout_users_cannot_start_until_the_import_ends(pair, monkeypatch, tmp_path):
    a, b, source, target = pair
    destination, _ = _registered_receiving_worktree(a, b, source, target, tmp_path)
    with b.active():
        _bind_session("idle-receiver", BRANCH, cwd=destination)
    real_import = checkout_transfer.import_checkout
    probes = {}

    def import_checkout(*args, **kwargs):
        probes["sibling_claim"] = runs.claim_run("idle-receiver", "probe", 60)
        _bind_session("new-receiver", BRANCH)
        probes["new_held"] = handoff.admission_held("new-receiver")
        return real_import(*args, **kwargs)

    monkeypatch.setattr(checkout_transfer, "import_checkout", import_checkout)
    monkeypatch.setattr(control, "call", _route_between(a, b, []))
    with a.active():
        assert await handoff.move(database.get_session("handoff-test"), b.device_id) is True
    assert probes == {"sibling_claim": None, "new_held": True}
    assert git(destination, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    with b.active():
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert _active_holds() == []
        assert runs.get_active_run("handoff-test") is None
        assert not handoff.admission_held("new-receiver")
        assert runs.claim_run("idle-receiver", settings.effective_instance_id, 60)
