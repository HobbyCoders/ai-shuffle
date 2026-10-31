"""Cross-worker project setup: ``/projects/overview``, ``/projects/workers``,
``/projects/setup`` (+ cancel) and ``DELETE /projects/workers/{id}``.

Real git against local bare repositories (no network). The worker network is
faked where a test needs a peer that is offline or runs an older version; the
setup itself always runs through the real git-ops claim pass.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import project_workers
from app.api.auth import require_admin, require_auth
from app.core import git_ops_runner, project_setup
from app.core.config import settings
from app.core.project_links import auto_link_group_id
from app.db import checkouts as checkouts_db
from app.db import database, git_ops as git_ops_db, projects_v2
from app.db import project_setup as setup_db
from tests.git_repo_helpers import git, hermetic_git_env, init_repo, auth_required_http_remote
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401  (autouse fixture)

PEER = "peer-install-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path, monkeypatch, mock_db):
    """A Docker-mode workspace (stored project paths are workspace-relative)."""
    from app.core import config

    hermetic_git_env(monkeypatch, tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", workspace)
    monkeypatch.delitem(config._runtime_overrides, "workspace_dir", raising=False)
    monkeypatch.setattr("app.api.projects.is_local_mode", lambda: False)
    project_setup._jobs.clear()
    database.register_install(ME, "This box", "http://me:8000", "linux", "1", {}, approve_new=True)
    database.register_install(PEER, "Studio", "http://peer:8000", "win32", "1", {}, approve_new=True)
    return workspace


def _caller(*, admin=True, api_user=None):
    def dependency(request: Request) -> str:
        request.state.is_admin = admin
        request.state.api_user = api_user
        return "test-token"
    return dependency


@pytest.fixture
def make_client():
    def make(**caller):
        app = FastAPI()
        app.include_router(project_workers.router)
        app.dependency_overrides[require_auth] = _caller(**caller)
        app.dependency_overrides[require_admin] = _caller(**caller)
        return TestClient(app)
    return make


@pytest.fixture
def client(make_client):
    with make_client() as test_client:
        yield test_client


def _bare_remote(ws: Path, name: str = "demo") -> str:
    """A bare repo inside the workspace (a local source the owner may clone)."""
    src = init_repo(ws.parent / f"{name}-src", files={"README.md": f"{name}\n"})
    bare = ws / "_remotes" / f"{name}.git"
    bare.parent.mkdir(exist_ok=True)
    git(ws.parent, "init", "-q", "--bare", "-b", "main", str(bare))
    git(src, "remote", "add", "origin", str(bare))
    git(src, "push", "-q", "-u", "origin", "main")
    return "file://" + str(bare)


def _peer_project(test_db, project_id: str, name: str, *, remote: str | None = None,
                  origin: str | None = None, instance: str = PEER) -> dict:
    """A project another worker registered in the shared database."""
    origin = origin or (auto_link_group_id(remote) if remote else f"local:{project_id}")
    test_db.execute(
        "INSERT INTO projects (id, name, description, path, settings, instance_id, link_group_id, "
        "created_at, updated_at) VALUES (?, ?, NULL, ?, '{}', ?, ?, '2026-01-01', '2026-01-01')",
        (project_id, name, f"C:/code/{project_id}", instance,
         origin if not origin.startswith("local:") else None),
    )
    if remote:
        database.create_git_repository(f"repo-{project_id}", project_id, remote_url=remote)
    return projects_v2.upsert_project(origin, name=name)


def _run_setups():
    async def go():
        await git_ops_runner.claim_once()
        while project_setup._tasks:
            await asyncio.gather(*list(project_setup._tasks), return_exceptions=True)
    asyncio.run(go())


def _worker(body: dict, install_id: str) -> dict:
    return next(w for w in body["workers"] if w["install_id"] == install_id)


# ---------------------------------------------------------------------------
# Setting up on this worker
# ---------------------------------------------------------------------------

def test_setup_here_clones_registers_and_is_eligible(client, ws, test_db):
    from app.core import checkout_manager

    remote = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=remote)

    resp = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["install_id"] == ME
    assert body["op_id"].startswith("psetup-")
    assert _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(),
                   ME)["state"] == "setting_up"

    _run_setups()

    op = git_ops_db.get_op(body["op_id"])
    assert op["status"] == "done", op
    target = ws / "demo"
    assert (target / ".git").is_dir() and (target / "README.md").read_text() == "demo\n"
    assert not [p for p in ws.iterdir() if p.name.startswith(".demo.setup-")]
    row = database.get_project(op["result"]["project_id"])
    assert row["instance_id"] == ME
    assert row["path"] == "demo"  # Docker mode stores workspace-relative paths
    assert row["link_group_id"] == pv2["origin"]
    assert database.get_git_repository_by_project(row["id"])["remote_url"] == remote
    assert pv2["id"] in checkouts_db.eligible_base_project_ids(ME)
    assert checkout_manager.ensure_checkout(projects_v2.get_project_v2(pv2["id"]), None) == str(target.resolve())

    detail = client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json()
    me = _worker(detail, ME)
    assert me["state"] == "ready" and me["is_self"] is True
    assert me["folder"] == str(ws / "demo")
    assert me["branch"] == "main" and me["dirty"] is False and me["changed_files"] == 0
    assert detail["kind"] == "git" and detail["remote"].endswith("ws/_remotes/demo")
    assert detail["local_project_id"] == row["id"]


def test_setup_is_idempotent_while_in_flight(client, ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    first = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()
    second = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME,
                                                          "folder": "elsewhere"}).json()
    assert second["op_id"] == first["op_id"]
    _run_setups()
    third = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()
    assert third["status"] == "done"  # already set up here: nothing to do


def test_setup_adopts_a_folder_that_is_already_the_same_repo(client, ws, test_db):
    remote = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=remote)
    git(ws, "clone", "-q", remote, "demo")
    (ws / "demo" / "local-only.txt").write_text("keep me")

    op_id = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()["op_id"]
    _run_setups()

    op = git_ops_db.get_op(op_id)
    assert op["status"] == "done" and op["result"]["adopted"] is True
    assert (ws / "demo" / "local-only.txt").read_text() == "keep me"


def test_setup_never_overwrites_another_folder(client, ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    (ws / "demo").mkdir()
    (ws / "demo" / "notes.txt").write_text("mine")

    op_id = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()["op_id"]
    _run_setups()

    op = git_ops_db.get_op(op_id)
    assert op["status"] == "error"
    assert op["result"]["error_code"] == "folder_exists"
    assert str(ws / "demo") in op["result"]["error"]
    assert (ws / "demo" / "notes.txt").read_text() == "mine"
    assert sorted(p.name for p in (ws / "demo").iterdir()) == ["notes.txt"]
    me = _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(), ME)
    assert me["state"] == "failed" and me["setup"]["error_code"] == "folder_exists"
    assert me["suggested_folder"] == str(ws / "demo")

    retry = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME,
                                                         "folder": "demo-2"}).json()
    _run_setups()
    assert git_ops_db.get_op(retry["op_id"])["status"] == "done"
    assert (ws / "demo-2" / ".git").is_dir()


def test_missing_repository_is_not_found(client, ws, test_db):
    gone = "file://" + str(ws / "_remotes" / "gone.git")
    pv2 = _peer_project(test_db, "gone", "Gone", remote=gone)
    op_id = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()["op_id"]
    _run_setups()
    result = git_ops_db.get_op(op_id)["result"]
    assert result["error_code"] == "not_found"
    assert not (ws / "gone").exists()
    assert not [p for p in ws.iterdir() if p.name.startswith(".gone.setup-")]


def test_remote_that_wants_credentials_is_github_auth(client, ws, test_db):
    with auth_required_http_remote() as url:
        pv2 = _peer_project(test_db, "private", "Private", remote=url)
        op_id = client.post("/api/v1/projects/setup",
                            json={"project_v2_id": pv2["id"], "install_id": ME}).json()["op_id"]
        _run_setups()
    result = git_ops_db.get_op(op_id)["result"]
    assert result["error_code"] == "github_auth"
    assert "127.0.0.1" not in result["error"]
    assert not (ws / "private").exists()


def test_folder_project_creates_an_empty_linked_folder(client, ws, test_db):
    pv2 = _peer_project(test_db, "notes", "Notes")  # no Git remote: origin local:notes
    assert pv2["origin"] == "local:notes"
    detail = client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json()
    assert detail["kind"] == "folder" and detail["remote"] is None
    assert detail["default_folder"] == "notes"

    op_id = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()["op_id"]
    _run_setups()

    op = git_ops_db.get_op(op_id)
    assert op["status"] == "done", op
    assert (ws / "notes").is_dir() and not any((ws / "notes").iterdir())
    row = database.get_project(op["result"]["project_id"])
    assert row["id"] != "notes" and row["instance_id"] == ME
    assert row["link_group_id"] == "local:notes" and row["settings"].get("link_group_locked") is True
    assert pv2["id"] in checkouts_db.eligible_base_project_ids(ME)


def test_invalid_folder_is_refused_before_queueing(client, ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    resp = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME,
                                                       "folder": "../outside"})
    assert resp.status_code == 400
    assert git_ops_db.list_ops_for_runner(ME, project_setup.SETUP_OP) == []
    assert client.post("/api/v1/projects/setup", json={"install_id": ME}).status_code == 400


# ---------------------------------------------------------------------------
# Cancel and remove
# ---------------------------------------------------------------------------

def test_cancel_a_queued_setup_then_it_reads_as_missing(client, ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    op_id = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME}).json()["op_id"]

    assert client.post(f"/api/v1/projects/setup/{op_id}/cancel").json() == {"status": "cancelled"}
    _run_setups()
    assert not (ws / "demo").exists()
    assert client.post(f"/api/v1/projects/setup/{op_id}/cancel").status_code == 409
    assert client.post("/api/v1/projects/setup/psetup-unknown/cancel").status_code == 404
    me = _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(), ME)
    assert me["state"] == "missing" and me["setup"] is None


def test_cancel_a_running_clone_kills_it(ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    row = git_ops_db.insert_op(ME, project_setup.SETUP_OP, project_v2_id=pv2["id"], params={},
                               op_id=project_setup.new_op_id())
    git_ops_db.claim_op(row["id"], ME)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    job = project_setup._Job(row["id"], None, None)
    job.proc = proc
    project_setup._jobs[row["id"]] = job
    try:
        assert project_setup.cancel_local(row["id"]) == {"status": "cancelled"}
        assert proc.wait(timeout=5) != 0
        assert job.cancelled is True
    finally:
        project_setup._jobs.pop(row["id"], None)
        if proc.poll() is None:
            proc.kill()


def test_a_cancelled_clone_leaves_no_temp_folder(ws):
    job = project_setup._Job(project_setup.new_op_id(), None, None)
    job.cancelled = True
    with pytest.raises(project_setup.SetupError) as raised:
        project_setup._clone(job, "file:///nowhere.git", ws / "demo")
    assert raised.value.error_code == "cancelled"
    assert list(ws.iterdir()) == []


def test_remove_from_this_worker_keeps_the_files(client, ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME})
    _run_setups()

    resp = client.delete(f"/api/v1/projects/workers/{ME}", params={"project_v2_id": pv2["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"]
    assert (ws / "demo" / "README.md").exists()
    assert pv2["id"] not in checkouts_db.eligible_base_project_ids(ME)
    assert _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(),
                   ME)["state"] == "missing"
    assert client.delete(f"/api/v1/projects/workers/{ME}", params={"project_v2_id": pv2["id"]}).status_code == 404


# ---------------------------------------------------------------------------
# Overview merging
# ---------------------------------------------------------------------------

def test_overview_merges_every_worker_state(client, ws, test_db, tmp_path):
    shared = _peer_project(test_db, "shared", "Shared", remote="https://github.com/acme/shared.git")
    checkouts_db.upsert_checkout(shared["id"], PEER, "/peer/code/shared")
    mine = tmp_path / "ws" / "mine"
    mine.mkdir()
    database.create_project(project_id="mine", name="Mine", description=None, path="mine")
    orphan = projects_v2.upsert_project("git:deleted-everywhere", name="Gone")

    body = client.get("/api/v1/projects/overview").json()
    by_name = {p["name"]: p for p in body["projects"]}
    assert "Gone" not in by_name and orphan["id"] not in str(body)
    assert set(by_name) == {"Shared", "Mine"}
    assert by_name["Shared"]["remote"] == "github.com/acme/shared"
    assert by_name["Shared"]["kind"] == "git" and by_name["Shared"]["local_project_id"] is None
    assert _worker(by_name["Shared"], PEER)["state"] == "ready"
    assert _worker(by_name["Shared"], ME)["state"] == "missing"
    assert _worker(by_name["Mine"], ME)["state"] == "ready"
    assert by_name["Mine"]["local_project_id"] == "mine"
    assert _worker(by_name["Mine"], PEER)["state"] == "missing"
    assert "C:/code" not in str(body) and "/peer/code" not in str(body) and str(ws) not in str(body)

    # The peer goes offline: it still has Shared (its record), not Mine.
    test_db.execute("UPDATE installs SET last_seen = '2020-01-01T00:00:00+00:00' WHERE id = ?", (PEER,))
    body = client.get("/api/v1/projects/overview").json()
    by_name = {p["name"]: p for p in body["projects"]}
    assert _worker(by_name["Shared"], PEER)["state"] == "offline_has"
    assert _worker(by_name["Shared"], PEER)["online"] is False
    assert _worker(by_name["Mine"], PEER)["state"] == "offline_missing"


def test_derive_state_fails_open_for_an_unread_peer():
    view = {"install": {"id": PEER, "device_name": "Studio"}, "is_self": False, "online": True,
            "status": None, "unsupported": False}
    assert project_workers.derive_state(pv2_id="pv2-x", view=view, has_offline=False, request=None,
                                        superseded_here=False)[0] == "unknown"
    view["status"] = {"project_ids": [], "ops": [], "projects": {}}
    assert project_workers.derive_state(pv2_id="pv2-x", view=view, has_offline=True, request=None,
                                        superseded_here=False)[0] == "missing"
    waiting = {"id": "psetup-1", "status": "queued_offline"}
    state, info = project_workers.derive_state(pv2_id="pv2-x", view={**view, "online": False, "status": None},
                                               has_offline=False, request=waiting, superseded_here=False)
    assert state == "queued_offline" and info["status"] == "queued"


def test_scoped_api_user_sees_only_its_project_and_no_paths(make_client, ws, test_db):
    database.create_project(project_id="mine", name="Mine", description=None, path="mine")
    (ws / "mine").mkdir()
    other = _peer_project(test_db, "other", "Other", remote="https://github.com/acme/other.git")
    checkouts_db.upsert_checkout(other["id"], PEER, "/peer/code/other")
    user = {"id": "key-1", "project_id": "mine"}
    with make_client(admin=False, api_user=user) as scoped:
        names = [p["name"] for p in scoped.get("/api/v1/projects/overview").json()["projects"]]
        assert names == ["Mine"]
        assert scoped.get("/api/v1/projects/workers", params={"project_v2_id": other["id"]}).status_code == 403
        detail = scoped.get("/api/v1/projects/workers", params={"project_id": "mine"}).json()
        assert all(w["folder"] is None and w["suggested_folder"] is None for w in detail["workers"])
    gone = {"id": "key-2", "project_id": "deleted"}
    with make_client(admin=False, api_user=gone) as orphaned:
        assert orphaned.get("/api/v1/projects/overview").json() == {"projects": []}


# ---------------------------------------------------------------------------
# Peers in a worker network (faked transport)
# ---------------------------------------------------------------------------

@pytest.fixture
def network(monkeypatch, ws):
    """This worker in a network with one peer, ``Studio``, whose answers the test sets."""
    from app.network import control, engine, transport

    ctx = SimpleNamespace(device_id="dev-me", network_id="net", member=lambda device_id: None)
    monkeypatch.setattr(engine, "context", lambda: ctx)
    peer = {"id": PEER, "device_name": "Studio", "approved": True, "online": True, "last_seen": None}
    rows = [{"id": "dev-me", "device_name": "This box", "approved": True, "online": True, "is_self": True}, peer]
    monkeypatch.setattr(project_workers, "_approved_workers", lambda: rows)
    monkeypatch.setattr(project_workers, "_self_install_id", lambda: "dev-me")
    monkeypatch.setattr(project_setup, "_pending_targets", None)
    calls = []
    state = {"handler": None}

    async def fake_call(device_id, action, body=None, *, timeout=None, secure=False):
        calls.append((device_id, action, body))
        return await state["handler"](device_id, action, body or {})

    async def fake_catalog(runner_id):
        return []

    monkeypatch.setattr(control, "call", fake_call)
    monkeypatch.setattr("app.network.git.catalog", fake_catalog)
    monkeypatch.setattr(transport, "online_devices", lambda: {PEER: 1.0} if peer["online"] else {})
    return SimpleNamespace(peer=peer, calls=calls, state=state, control=control)


def test_older_peer_is_unsupported(client, ws, test_db, network):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws), instance=ME)

    async def old_worker(device_id, action, body):
        raise network.control.ControlRefused("Unknown control action")

    network.state["handler"] = old_worker
    resp = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": PEER})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error_code": "unsupported",
                                     "error": "Update Studio to set projects up from here"}
    peer = _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(), PEER)
    assert peer["state"] == "failed"
    assert peer["setup"]["error_code"] == "unsupported"
    assert "Update Studio" in peer["setup"]["error"]


def test_offline_peer_is_queued_then_dispatched_when_back(client, ws, test_db, network):
    remote = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=remote, instance=ME)
    network.peer["online"] = False
    started = []

    async def peer_worker(device_id, action, body):
        if body.get("action") == "start":
            started.append(body)
            return {"id": body["op_id"], "status": "queued", "runner_id": device_id}
        if body.get("action") == "status":
            ops = [{"op_id": s["op_id"], "project_v2_id": pv2["id"], "status": "running", "progress": 40,
                    "error_code": None, "error": None} for s in started]
            return {"project_ids": [], "workspace": "/data/ws", "ops": ops, "projects": {}}
        raise AssertionError(body)

    network.state["handler"] = peer_worker
    first = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": PEER,
                                                         "folder": "demo-copy"}).json()
    assert first["status"] == "queued_offline"
    again = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": PEER}).json()
    assert again["op_id"] == first["op_id"]
    overview = client.get("/api/v1/projects/overview").json()
    assert _worker(overview["projects"][0], PEER)["state"] == "queued_offline"
    assert network.calls == []  # nothing was sent to an offline worker

    network.peer["online"] = True
    asyncio.run(project_setup.dispatch_once())
    assert setup_db.get_request(first["op_id"])["status"] == "dispatched"
    spec = started[0]["spec"]
    assert spec["clone_url"] == remote and spec["folder"] == "demo-copy" and spec["kind"] == "git"
    assert started[0]["op_id"] == first["op_id"]

    peer = _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(), PEER)
    assert peer["state"] == "setting_up" and peer["setup"]["progress"] == 40


def test_withdraw_a_request_waiting_for_an_offline_peer(client, ws, test_db, network):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws), instance=ME)
    network.peer["online"] = False
    op_id = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": PEER}).json()["op_id"]
    assert client.post(f"/api/v1/projects/setup/{op_id}/cancel").json() == {"status": "cancelled"}
    peer = _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(), PEER)
    assert peer["state"] == "offline_missing" and peer["setup"] is None


def test_unreadable_online_peer_is_unknown_never_missing(client, ws, test_db, network):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws), instance=ME)

    async def broken(device_id, action, body):
        raise network.control.RunnerUnreachable("no route")

    network.state["handler"] = broken
    peer = _worker(client.get("/api/v1/projects/workers", params={"project_v2_id": pv2["id"]}).json(), PEER)
    assert peer["state"] == "unknown"


def test_overview_and_workers_routes_precede_project_ids():
    from app.api import projects
    from app.main import app

    routers = [getattr(route, "original_router", route) for route in app.routes]
    assert routers.index(project_workers.router) < routers.index(projects.router)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_classify_clone_failure_codes_and_redaction():
    classify = project_setup.classify_clone_failure
    gh = "https://github.com/acme/private.git"
    assert classify("fatal: could not read Username for 'https://github.com': terminal prompts disabled",
                    gh).error_code == "github_auth"
    not_found = "remote: Repository not found.\nfatal: repository 'https://github.com/acme/private.git/' not found"
    assert classify(not_found, gh, github_signed_in=lambda: False).error_code == "github_auth"
    assert classify(not_found, gh, github_signed_in=lambda: True).error_code == "not_found"
    assert classify("fatal: unable to access 'https://github.com/a/b.git/': Could not resolve host: github.com",
                    gh).error_code == "network"
    other = classify("fatal: weird failure at https://user:s3cret@example.com/x.git", "https://example.com/x.git")
    assert other.error_code == "internal" and "s3cret" not in str(other)


def test_parse_progress_and_display_remote():
    assert project_setup.parse_progress("Receiving objects:  50% (5/10), 1 KiB") == 42
    assert project_setup.parse_progress("Resolving deltas: 100% (3/3), done.") == 95
    assert project_setup.parse_progress("remote: Counting objects: 10% (1/10)") is None
    assert project_setup.display_remote("https://tok:x@github.com/Acme/Repo.git") == "github.com/Acme/Repo"
    assert project_setup.display_remote("git@github.com:acme/repo.git") == "github.com/acme/repo"
    assert project_setup.display_remote(None) is None


def test_peer_control_action_start_status_cancel_remove(ws, test_db, monkeypatch):
    """The owner side of ``project-setup``, as a requesting worker drives it."""
    from app.network import control, engine, git as network_git

    assert control.known_action("project-setup")
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="dev-me"))
    monkeypatch.setattr(network_git, "device_id", lambda runner_id: runner_id)
    remote = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=remote)
    spec = {"project_v2_id": pv2["id"], "origin": pv2["origin"], "name": "Demo", "kind": "git",
            "clone_url": remote, "folder": "demo", "mode": "auto"}

    def call(body):
        return asyncio.run(project_setup.handle_control(body, "dev-requester"))

    with pytest.raises(ValueError):
        call({"action": "start", "op_id": "not-an-id", "spec": spec})
    with pytest.raises(ValueError):
        call({"action": "start", "op_id": project_setup.new_op_id(),
              "spec": {**spec, "clone_url": "https://github.com/someone/else.git"}})
    with pytest.raises(ValueError):
        call({"action": "nope"})

    op_id = project_setup.new_op_id()
    started = call({"action": "start", "op_id": op_id, "spec": spec})
    assert started == {"id": op_id, "status": "queued", "runner_id": "dev-me"}
    assert git_ops_db.get_op(op_id)["params"]["requested_by"] == "dev-requester"
    status = call({"action": "status", "project_v2_id": pv2["id"]})
    assert [op["op_id"] for op in status["ops"]] == [op_id]
    assert status["workspace"] == str(ws)
    assert call({"action": "cancel", "op_id": op_id}) == {"status": "cancelled"}
    with pytest.raises(PermissionError):
        call({"action": "cancel", "op_id": op_id})

    op_id = project_setup.new_op_id()
    call({"action": "start", "op_id": op_id, "spec": spec})
    _run_setups()
    status = call({"action": "status", "project_v2_id": pv2["id"]})
    assert pv2["id"] in status["project_ids"] and status["detail"]["branch"] == "main"
    assert status["projects"][pv2["id"]]["clone_url"] == remote
    assert call({"action": "remove", "project_v2_id": pv2["id"]})["removed"]
    with pytest.raises(LookupError):
        call({"action": "remove", "project_v2_id": pv2["id"]})
    assert (ws / "demo" / "README.md").exists()
