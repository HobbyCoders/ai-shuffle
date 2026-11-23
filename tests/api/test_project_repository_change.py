"""Point a project at another repository (or a fork) on every worker, keeping it one project.

Real git against local bare repositories (no network). Peers are faked through
the same ``network`` fixture the cross-worker setup tests use.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.core import project_remote
from app.core.project_links import auto_link_group_id, maybe_auto_link_project
from app.db import database, git_ops as git_ops_db, projects_v2
from tests.api.test_project_setup import (  # noqa: F401  (fixtures)
    ME, PEER, _bare_remote, _peer_project, _run_setups, client, fresh_records, make_client, network, ws,
)
from tests.git_repo_helpers import git
from tests.shared_fixtures import pin_runner  # noqa: F401  (autouse fixture)


def _fork(ws: Path, upstream: str, name: str = "demo-fork") -> str:
    """A bare fork of ``upstream`` with one extra commit on main."""
    bare = ws / "_remotes" / f"{name}.git"
    git(ws.parent, "clone", "-q", "--bare", upstream, str(bare))
    work = ws.parent / f"{name}-work"
    git(ws.parent, "clone", "-q", str(bare), str(work))
    (work / "FORK.md").write_text("fork\n")
    git(work, "add", "FORK.md")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "fork")
    git(work, "push", "-q", "origin", "main")
    return bare.as_uri()


def _set_up_here(client, pv2) -> dict:
    resp = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME})
    assert resp.status_code == 202, resp.text
    _run_setups()
    op = git_ops_db.get_op(resp.json()["op_id"])
    assert op["status"] == "done", op
    return database.get_project(op["result"]["project_id"])


def _remote(folder: Path, name: str = "origin") -> str:
    return git(folder, "remote", "get-url", name).strip()


def _change(client, pv2, url, **extra):
    return client.put("/api/v1/projects/catalog/repository", params={"project_v2_id": pv2["id"]},
                      json={"remote_url": url, **extra})


def test_switching_to_a_fork_keeps_one_project_and_points_this_copy_at_it(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    row = _set_up_here(client, pv2)
    fork = _fork(ws, upstream)
    before = {p["id"] for p in projects_v2.list_projects_v2()}

    resp = _change(client, pv2, fork, keep_upstream=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    me = next(w for w in body["workers"] if w["install_id"] == ME)
    assert me["status"] == "updated" and me["ok"] is True
    assert me["base"] == "fast_forwarded"  # clean base strictly behind the fork
    assert body["kept_as_upstream"] is True and body["merged_duplicate"] is False

    folder = ws / "demo"
    assert _remote(folder) == fork
    assert _remote(folder, "upstream") == upstream
    assert (folder / "FORK.md").read_text() == "fork\n"

    # Still the same project everywhere: no new identity, the row pinned to it.
    assert {p["id"] for p in projects_v2.list_projects_v2()} == before
    stored = projects_v2.get_project_v2(pv2["id"])
    assert project_remote.pinned_remote(stored) == fork
    assert project_remote.generation(stored) == 1
    local = database.get_project(row["id"])
    assert local["link_group_id"] == pv2["origin"]
    assert local["settings"]["link_group_locked"] is True

    # Nothing that derives an identity from the new remote splits the project.
    assert project_remote.identity_for_remote(fork) == (pv2["origin"], True)
    assert maybe_auto_link_project(row["id"], fork) == pv2["origin"]
    assert project_remote.remote_names_origin(fork, pv2["origin"])
    from app.core import project_setup
    assert project_setup._repo_info_by_origin()[pv2["origin"]]["clone_url"] == fork
    # A worker that has not applied it yet reports the old remote: it never wins.
    assert projects_v2.record_remote(pv2["origin"], upstream) is False
    assert project_remote.pinned_remote(projects_v2.get_project_v2(pv2["id"])) == fork


def test_a_new_copy_elsewhere_clones_the_new_repository(client, ws, test_db, fresh_records):
    from app.core import checkout_manager

    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    fork = _fork(ws, upstream)
    assert _change(client, pv2, fork).status_code == 200
    stored = projects_v2.get_project_v2(pv2["id"])
    assert checkout_manager._resolve_clone_url(pv2["origin"], stored) == fork
    _set_up_here(client, pv2)
    assert _remote(ws / "demo") == fork
    assert (ws / "demo" / "FORK.md").is_file()


def test_an_existing_duplicate_for_the_new_repository_folds_into_the_project(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    fork = _fork(ws, upstream)
    kept = _peer_project(test_db, "demo", "Demo", remote=upstream)
    dup = _peer_project(test_db, "demo-fork", "Demo fork", remote=fork)
    dup_row = _set_up_here(client, dup)  # this worker only has the duplicate

    resp = _change(client, kept, fork)
    assert resp.status_code == 200, resp.text
    assert resp.json()["merged_duplicate"] is True

    relinked = database.get_project(dup_row["id"])
    assert relinked["link_group_id"] == kept["origin"]
    assert relinked["settings"]["link_group_locked"] is True
    assert project_remote.merged_into(projects_v2.get_project_v2(dup["id"])) == kept["origin"]
    assert project_remote.identity_for_remote(fork) == (kept["origin"], True)
    listed = {p["project_v2_id"] for p in client.get("/api/v1/projects/overview").json()["projects"]}
    assert kept["id"] in listed and dup["id"] not in listed  # one project, not two


# ---------------------------------------------------------------------------
# A project no worker holds any more must not get stuck on the Projects page
# ---------------------------------------------------------------------------

def _fail_setup_here(client, pv2, ws):
    (ws / "demo").mkdir()
    (ws / "demo" / "other.txt").write_text("not this project\n")
    resp = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME})
    assert resp.status_code == 202, resp.text
    _run_setups()


def _listed(client) -> set:
    return {p["project_v2_id"] for p in client.get("/api/v1/projects/overview").json()["projects"]}


def test_forget_takes_a_project_with_only_a_failed_setup_off_the_page(client, ws, test_db, fresh_records):
    remote = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=remote)
    projects_v2.record_remote(pv2["origin"], remote)  # the synced row remembers its repository
    test_db.execute("DELETE FROM projects WHERE id = 'demo'")  # no worker holds it any more
    _fail_setup_here(client, pv2, ws)
    assert pv2["id"] in _listed(client)  # stuck: only a failed setup keeps it listed

    resp = client.post("/api/v1/projects/catalog/forget", params={"project_v2_id": pv2["id"]})
    assert resp.status_code == 200, resp.text
    assert pv2["id"] not in _listed(client)
    assert (ws / "demo" / "other.txt").is_file()  # nothing on disk touched
    assert projects_v2.get_project_v2(pv2["id"]) is not None  # chats keep their project

    # Setting it up again brings it back.
    (ws / "demo" / "other.txt").unlink()
    (ws / "demo").rmdir()
    resp = client.post("/api/v1/projects/setup", json={"project_v2_id": pv2["id"], "install_id": ME})
    assert resp.status_code == 202, resp.text
    _run_setups()
    assert pv2["id"] in _listed(client)


def test_forget_is_refused_while_a_worker_still_has_it(client, ws, test_db, fresh_records):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    _set_up_here(client, pv2)
    resp = client.post("/api/v1/projects/catalog/forget", params={"project_v2_id": pv2["id"]})
    assert resp.status_code == 409 and "Remove it from Workers first" in resp.json()["detail"]
    assert pv2["id"] in _listed(client)


def test_a_worker_that_was_offline_applies_it_on_its_next_pass(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    fork = _fork(ws, upstream)
    # The change arrives only as the synced row (made on another worker).
    projects_v2.pin_remote(pv2["origin"], fork, default_branch="main", generation=1,
                           keep_upstream=False, previous=upstream)

    assert project_remote.reconcile_local() == 1
    assert _remote(ws / "demo") == fork
    assert project_remote.reconcile_local() == 0  # applied once, not every pass


def test_a_failed_fetch_is_retried_later_not_every_pass(client, ws, test_db, fresh_records, monkeypatch):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    row = _set_up_here(client, pv2)
    projects_v2.pin_remote(pv2["origin"], (ws / "_remotes" / "gone.git").as_uri(), default_branch=None,
                           generation=1, keep_upstream=False, previous=upstream)

    assert project_remote.reconcile_local() == 1
    applied = database.get_project(row["id"])["settings"]["remote_applied"]
    assert applied["ok"] is False and applied["status"] == "fetch_failed"
    assert project_remote.reconcile_local() == 0
    monkeypatch.setattr(time, "time", lambda: applied["at"] + project_remote.RETRY_FAILED_AFTER + 1)
    assert project_remote.reconcile_local() == 1


def test_online_peers_are_told_and_an_older_peer_is_reported(client, ws, test_db, fresh_records, network):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    fork = _fork(ws, upstream)

    async def peer(device_id, action, body):
        if action != "project-setup":
            return {"ok": True}  # the catalog-changed announcement
        return {"status": "updated", "ok": True, "branch": "main"}

    network.state["handler"] = peer
    resp = _change(client, pv2, fork, keep_upstream=True)
    assert resp.status_code == 200, resp.text
    sent = [body for _d, action, body in network.calls if action == "project-setup"]
    assert sent == [{"action": "apply-remote", "origin": pv2["origin"], "remote_url": fork,
                     "generation": 1, "keep_upstream": True, "merged_origin": None}]
    assert next(w for w in resp.json()["workers"] if w["install_id"] == PEER)["status"] == "updated"

    async def old(device_id, action, body):
        raise network.control.ControlRefused("Unknown setup action")

    network.state["handler"] = old
    resp = _change(client, pv2, upstream)
    studio = next(w for w in resp.json()["workers"] if w["install_id"] == PEER)
    assert studio["status"] == "unsupported" and "Update Studio" in studio["error"]
    assert project_remote.generation(projects_v2.get_project_v2(pv2["id"])) == 2


def test_peer_side_apply_skips_a_generation_it_already_applied(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    fork = _fork(ws, upstream)
    body = {"origin": pv2["origin"], "remote_url": fork, "generation": 3, "keep_upstream": False}
    first = asyncio.run(project_remote.handle_apply(body))
    assert first["status"] == "updated"
    again = asyncio.run(project_remote.handle_apply(body))
    assert again["status"] == "updated" and again["copies"] == 1  # last result, no second fetch


@pytest.mark.parametrize("url,message", [
    ("ext::sh -c touch% /tmp/x", "unsafe clone URL"),
    ("/etc", "inside the workspace"),
])
def test_unsafe_urls_are_refused(client, ws, test_db, url, message):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    resp = _change(client, pv2, url)
    assert resp.status_code == 400
    assert message in resp.json()["detail"]
    assert project_remote.pinned_remote(projects_v2.get_project_v2(pv2["id"])) is None


def test_an_unreachable_repository_is_refused_before_anything_changes(client, ws, test_db):
    pv2 = _peer_project(test_db, "demo", "Demo", remote=_bare_remote(ws))
    resp = _change(client, pv2, (ws / "_remotes" / "missing.git").as_uri())
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("Can't use that repository from this worker")
    assert project_remote.generation(projects_v2.get_project_v2(pv2["id"])) == 0


def test_a_repository_another_project_already_uses_is_refused(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    fork = _fork(ws, upstream)
    one = _peer_project(test_db, "one", "One", remote=upstream)
    two = _peer_project(test_db, "two", "Two", origin="manual:two")
    assert _change(client, one, fork).status_code == 200
    resp = _change(client, two, fork)
    assert resp.status_code == 400 and "One" in resp.json()["detail"]


def test_agent_tool_switches_by_project_name(client, ws, test_db, fresh_records):
    from app.mcp.config_projects import _create_project_tools
    from tests.mcp.tool_helpers import get_tool, tool_result_json

    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    row = _set_up_here(client, pv2)
    database.update_project(row["id"], name="Demo here")
    fork = _fork(ws, upstream)
    tool = get_tool(_create_project_tools(True), "changeProjectRepository")
    result = tool_result_json(asyncio.run(tool.handler(
        {"project": "demo here", "remote_url": fork, "keep_upstream": True})))
    assert result["success"] is True and result["kept_as_upstream"] is True
    assert _remote(ws / "demo") == fork
    denied = tool_result_json(asyncio.run(get_tool(_create_project_tools(False), "changeProjectRepository")
                                          .handler({"project": "demo", "remote_url": fork})))
    assert "admin" in str(denied).lower()
    assert auto_link_group_id(fork) != pv2["origin"]  # the identity really is kept, not re-derived
