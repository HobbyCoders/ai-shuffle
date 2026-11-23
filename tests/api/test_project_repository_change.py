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


@pytest.fixture(autouse=True)
def _no_remembered_failures():
    project_remote._failed.clear()
    yield
    project_remote._failed.clear()


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


def test_a_failed_fetch_keeps_the_old_remote_and_is_retried_later(client, ws, test_db, fresh_records, monkeypatch):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    projects_v2.pin_remote(pv2["origin"], (ws / "_remotes" / "gone.git").as_uri(), default_branch=None,
                           generation=1, keep_upstream=True, previous=upstream)

    assert project_remote.reconcile_local() == 1
    assert _remote(ws / "demo") == upstream  # put back: the copy keeps working
    assert "upstream" not in git(ws / "demo", "remote").split()
    assert project_remote.reconcile_local() == 0  # not every pass
    later = time.monotonic() + project_remote.RETRY_FAILED_AFTER + 1
    monkeypatch.setattr(project_remote.time, "monotonic", lambda: later)
    assert project_remote.reconcile_local() == 1


def test_a_base_checkout_the_app_cloned_on_demand_switches_too(client, ws, test_db, fresh_records):
    from app.core import project_setup
    from app.db import checkouts as checkouts_db

    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)  # no project row on this worker
    base = ws / ".checkouts" / "demo" / "__base__"
    base.parent.mkdir(parents=True)
    git(ws, "clone", "-q", upstream, str(base))
    checkouts_db.upsert_checkout(pv2["id"], project_setup._me(), str(base), status="active")
    fork = _fork(ws, upstream)

    resp = _change(client, pv2, fork)
    assert resp.status_code == 200, resp.text
    me = next(w for w in resp.json()["workers"] if w["install_id"] == ME)
    assert me["status"] == "updated"
    assert _remote(base) == fork

    # One that missed the request switches on the next pass.
    git(base, "remote", "set-url", "origin", upstream)
    assert project_remote.reconcile_local() == 1
    assert _remote(base) == fork


def test_the_switch_never_overwrites_an_ignored_local_file(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    folder = ws / "demo"
    (folder / ".git" / "info" / "exclude").write_text(".env\n")
    (folder / ".env").write_text("LOCAL=1\n")
    fork = _fork(ws, upstream)
    work = ws.parent / "demo-fork-work"
    (work / ".env").write_text("FROM_FORK=1\n")
    git(work, "add", ".env")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "env")
    git(work, "push", "-q", "origin", "main")

    resp = _change(client, pv2, fork)
    assert resp.status_code == 200, resp.text
    me = next(w for w in resp.json()["workers"] if w["install_id"] == ME)
    assert me["status"] == "updated" and me["base"] == "behind"
    assert (folder / ".env").read_text() == "LOCAL=1\n"


def test_an_explicit_push_url_is_switched_too(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    folder = ws / "demo"
    git(folder, "remote", "set-url", "--push", "origin", upstream)
    fork = _fork(ws, upstream)

    assert _change(client, pv2, fork).status_code == 200
    assert git(folder, "remote", "get-url", "--push", "origin").strip() == fork
    assert project_remote.is_switched(str(folder), fork)


def test_a_project_that_moved_on_is_not_folded_as_a_duplicate(client, ws, test_db, fresh_records):
    repo_a = _bare_remote(ws)
    repo_b = _fork(ws, repo_a)
    moved = _peer_project(test_db, "demo", "Demo", remote=repo_a)
    moved_row = _set_up_here(client, moved)
    assert _change(client, moved, repo_b).status_code == 200  # Demo now uses repo B
    other = _peer_project(test_db, "other", "Other", origin="manual:other")

    resp = _change(client, other, repo_a)
    assert resp.status_code == 200, resp.text
    assert resp.json()["merged_duplicate"] is False
    assert project_remote.merged_into(projects_v2.get_project_v2(moved["id"])) is None
    assert database.get_project(moved_row["id"])["link_group_id"] == moved["origin"]
    assert _remote(ws / "demo") == repo_b
    assert project_remote.reconcile_local() == 0


def test_the_last_pin_wins_even_at_the_same_generation(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    loser = _fork(ws, upstream, "demo-loser")
    winner = _fork(ws, upstream, "demo-winner")
    # Two disconnected workers pinned generation 1; this one applied the losing URL.
    asyncio.run(project_remote.handle_apply(
        {"origin": pv2["origin"], "remote_url": loser, "generation": 1, "keep_upstream": False}))
    assert _remote(ws / "demo") == loser
    projects_v2.pin_remote(pv2["origin"], winner, default_branch="main", generation=1,
                           keep_upstream=False, previous=upstream)

    assert project_remote.reconcile_local() == 1
    assert _remote(ws / "demo") == winner


def test_every_old_fetch_and_push_url_is_replaced(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    folder = ws / "demo"
    mirror = _fork(ws, upstream, "demo-mirror")
    git(folder, "config", "--add", "remote.origin.url", mirror)  # a second fetch URL
    git(folder, "remote", "set-url", "--push", "origin", upstream)
    fork = _fork(ws, upstream)

    assert _change(client, pv2, fork).status_code == 200
    assert git(folder, "remote", "get-url", "--all", "origin").split() == [fork]
    assert git(folder, "remote", "get-url", "--push", "--all", "origin").split() == [fork]


def test_a_fetch_that_times_out_is_undone_like_any_failure(client, ws, test_db, fresh_records, monkeypatch):
    import subprocess

    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    fork = _fork(ws, upstream)
    real = project_remote._git

    def stalled(folder, args, timeout=30):
        if args[:1] == ["fetch"]:
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real(folder, args, timeout)

    monkeypatch.setattr(project_remote, "_git", stalled)
    out = asyncio.run(project_remote.handle_apply(
        {"origin": pv2["origin"], "remote_url": fork, "generation": 1, "keep_upstream": True}))
    assert out["status"] == "fetch_failed" and out["ok"] is False
    assert git(ws / "demo", "remote", "get-url", "--all", "origin").split() == [upstream]
    assert "upstream" not in git(ws / "demo", "remote").split()


def test_a_current_pin_beats_an_older_merge(client, ws, test_db, fresh_records):
    repo_a = _bare_remote(ws)
    repo_b = _fork(ws, repo_a, "demo-b")
    repo_c = _fork(ws, repo_a, "demo-c")
    first = _peer_project(test_db, "first", "First", origin="manual:first")
    _peer_project(test_db, "bee", "Bee", remote=repo_b)
    assert _change(client, first, repo_b).json()["merged_duplicate"] is True  # Bee folded into First
    assert _change(client, first, repo_c).status_code == 200  # First moves on
    second = _peer_project(test_db, "second", "Second", origin="manual:second")
    assert _change(client, second, repo_b).status_code == 200
    assert project_remote.identity_for_remote(repo_b) == ("manual:second", True)


def test_a_duplicate_held_only_as_an_on_demand_base_is_folded(client, ws, test_db, fresh_records):
    from app.core import project_setup
    from app.db import checkouts as checkouts_db

    upstream = _bare_remote(ws)
    fork = _fork(ws, upstream)
    kept = _peer_project(test_db, "demo", "Demo", remote=upstream)
    dup = _peer_project(test_db, "demo-fork", "Demo fork", remote=fork)
    base = ws / ".checkouts" / "demo-fork" / "__base__"
    base.parent.mkdir(parents=True)
    git(ws, "clone", "-q", fork, str(base))
    checkouts_db.upsert_checkout(dup["id"], project_setup._me(), str(base), status="active")

    assert _change(client, kept, fork).json()["merged_duplicate"] is True
    assert project_remote._own_folders_for(kept["origin"], lock=False) == [str(base)]
    # Chats bound to the duplicate can still run (and move) here.
    assert checkouts_db.has_active_base_checkout(dup["id"], project_setup._me())
    rows = checkouts_db.get_checkouts_by_path(str(base))
    assert [r["project_id"] for r in rows] == [kept["id"], dup["id"]]  # the kept project first
    assert checkouts_db.get_checkout_by_path(str(base), project_id=dup["id"])  # its chats still find it
    listed = {p["project_v2_id"] for p in client.get("/api/v1/projects/overview").json()["projects"]}
    assert kept["id"] in listed and dup["id"] not in listed  # one project, not two
    # A later change of the kept project reaches that checkout.
    other = _fork(ws, upstream, "demo-other")
    assert _change(client, kept, other).status_code == 200
    assert _remote(base) == other


def test_an_equal_generation_request_defers_to_the_synced_pin(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    winner = _fork(ws, upstream, "demo-winner")
    loser = _fork(ws, upstream, "demo-loser")
    projects_v2.pin_remote(pv2["origin"], winner, default_branch="main", generation=1,
                           keep_upstream=False, previous=upstream)
    asyncio.run(project_remote.handle_apply(
        {"origin": pv2["origin"], "remote_url": loser, "generation": 1, "keep_upstream": False}))
    assert _remote(ws / "demo") == winner


def test_a_late_request_never_switches_a_copy_back(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    fork = _fork(ws, upstream)
    projects_v2.pin_remote(pv2["origin"], fork, default_branch="main", generation=2,
                           keep_upstream=False, previous=upstream)
    out = asyncio.run(project_remote.handle_apply(
        {"origin": pv2["origin"], "remote_url": upstream, "generation": 1, "keep_upstream": False}))
    assert out["ok"] is True
    assert _remote(ws / "demo") == fork


def test_a_queued_setup_clones_the_current_repository(client, ws, test_db, fresh_records):
    from app.core import project_setup

    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    fork = _fork(ws, upstream)
    projects_v2.pin_remote(pv2["origin"], fork, default_branch="main", generation=1,
                           keep_upstream=False, previous=upstream)
    op = {"id": "op-queued", "project_v2_id": pv2["id"],
          "params": {"origin": pv2["origin"], "kind": "git", "clone_url": upstream, "folder": "demo",
                     "name": "Demo"}}
    project_setup.execute_setup(op)  # the queued request still names the old repository
    assert _remote(ws / "demo") == fork
    assert (ws / "demo" / "FORK.md").is_file()


def test_forget_is_refused_while_a_worker_cannot_be_read(client, ws, test_db, fresh_records, network):
    remote = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=remote)
    test_db.execute("DELETE FROM projects WHERE id = 'demo'")

    async def unreadable(device_id, action, body):
        raise RuntimeError("timed out")

    network.state["handler"] = unreadable
    resp = client.post("/api/v1/projects/catalog/forget", params={"project_v2_id": pv2["id"]})
    assert resp.status_code == 409 and "Couldn't check whether Studio" in resp.json()["detail"]
    assert projects_v2.get_project_v2(pv2["id"])["settings"].get("forgotten") is not True


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


def test_peer_side_apply_skips_a_copy_already_switched(client, ws, test_db, fresh_records):
    upstream = _bare_remote(ws)
    pv2 = _peer_project(test_db, "demo", "Demo", remote=upstream)
    _set_up_here(client, pv2)
    fork = _fork(ws, upstream)
    body = {"origin": pv2["origin"], "remote_url": fork, "generation": 3, "keep_upstream": False}
    first = asyncio.run(project_remote.handle_apply(body))
    assert first["status"] == "updated"
    again = asyncio.run(project_remote.handle_apply(body))
    assert again["status"] == "already" and again["copies"] == 1  # no second fetch


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
