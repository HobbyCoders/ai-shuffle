import json
from unittest.mock import patch

import pytest
from app.core import checkout_transfer
from app.core.config import settings
from app.db import database, projects_v2, checkouts, runs
from app.db.base import get_db
from app.network import control, engine, handoff
from tests.core.network.harness import Worker, join, converge
from tests.core.test_checkout_transfer import repositories, git


@pytest.fixture
def pair(tmp_path, repositories):
    a, b = Worker("a", tmp_path), Worker("b", tmp_path)
    source, target = repositories
    with a.active():
        engine.create_network("Handoff test")
    join(a, b)
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


@pytest.mark.asyncio
async def test_receive_transfers_unpushed_dirty_branch_and_native_history(pair, monkeypatch):
    a,b,source,target = pair
    (source / "file.txt").write_text("local staged change")
    git(source,"add",".")
    (source / "new.txt").write_text("untracked work")
    before = checkout_transfer.fingerprint(source)
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
        assert checkout_transfer.fingerprint(cwd) == before
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
        assert checkout_transfer.fingerprint(source) == before
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


def test_live_source_run_blocks_snapshot(pair):
    a,b,source,target=pair
    with a.active():
        run=runs.claim_run("handoff-test",settings.effective_instance_id,300)
        assert run
        with pytest.raises(ValueError,match="busy"):
            handoff.begin("handoff-test",b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id

@pytest.mark.asyncio
async def test_projectless_session_transfers_history_without_checkout(pair, monkeypatch):
    a,b,source,target=pair
    for worker in (a,b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s",("handoff-test",))
    async def call(device,action,body,**kwargs):
        with a.active():
            return await control.handle(action,body,from_device=b.device_id)
    monkeypatch.setattr(control,"call",call)
    with b.active():
        cwd=await handoff.receive(database.get_session("handoff-test"))
        assert cwd == str(settings.effective_workspace_dir)
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
        assert database.get_session_messages("handoff-test")[0]["content"] == "Keep this conversation"

