"""``GET /api/v1/cluster/git-state`` — per-device git state for one project (W3-1).

This is the read side of D6: it tells the composer (and the Devices panel) which
installs can actually run a worktree session for a project, and what each one
already has on disk. Its ``eligible`` flag is the SAME
``checkouts.has_active_base_checkout`` predicate the with-worktree remote
preflight enforces (W3-2) — the whole point of the endpoint is that a device
badged eligible is exactly a device whose send will not 409, so these tests pin
the two halves of that agreement: an active BASE checkout counts, a branch-only
checkout does not.

Driven through a TestClient over the real cluster router on the pg_shim, with
only ``require_auth`` overridden — the unauthenticated case gets its own app so
the auth gate itself is exercised.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.cluster import router
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import database
from app.db import git_ops as git_ops_db

ME = "local-me"
PEER = "peer-install-1"
BARE = "peer-install-2"  # registered, but never checked this project out


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: ME)
    )


@pytest.fixture
def app(mock_db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def anon_client(mock_db):
    """No auth override — the dependency runs for real."""
    unauthed = FastAPI()
    unauthed.include_router(router)
    with TestClient(unauthed) as test_client:
        yield test_client


@pytest.fixture
def project(mock_db, tmp_path):
    """A legacy project, its resolver-minted projects_v2 row, and three installs.

    Resolving mints THIS install's base checkout too (the seed mirrors the v60
    collapse: ``runner_id = legacy.instance_id``), so ME is eligible out of the
    box — exactly as on a real serving install. BARE is the never-checked-out
    control.
    """
    path = tmp_path / "repo"
    path.mkdir(parents=True)
    database.create_project(
        project_id="proj-1", name="Repo", description=None, path=str(path)
    )
    database.register_install(ME, "This Mac", "http://me:8000", "darwin", "1", {})
    database.register_install(PEER, "Studio", "http://peer:8000", "win32", "1", {})
    database.register_install(BARE, "Laptop", "http://bare:8000", "linux", "1", {})

    from app.core import project_resolver

    # Mint the origin-keyed row exactly as the endpoint will — a hand-rolled
    # projects_v2 row would carry a different origin and the eligibility lookup
    # would inspect the wrong project.
    pv2 = project_resolver.resolve_project_v2({"project_id": "proj-1"})
    return {"pv2": pv2, "path": path}


def _get(client, project_id="proj-1", **extra):
    params = {}
    if project_id is not None:
        params["project_id"] = project_id
    params.update(extra)
    return client.get("/api/v1/cluster/git-state", params=params)


def _install(body, install_id):
    match = [i for i in body["installs"] if i["id"] == install_id]
    assert match, f"install {install_id} missing from {[i['id'] for i in body['installs']]}"
    return match[0]


# ═══ gates ═══════════════════════════════════════════════════════════════════


def test_auth_is_required(anon_client, project):
    assert _get(anon_client).status_code == 401


def test_neither_id_is_400(client, project):
    resp = client.get("/api/v1/cluster/git-state")
    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"]


def test_both_ids_are_400(client, project):
    resp = _get(client, project_id="proj-1", project_v2_id=project["pv2"]["id"])
    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"]


def test_unknown_project_404s(client, project):
    resp = _get(client, project_id="no-such-project")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_a_project_without_a_project_v2_is_not_an_error(client, project):
    """A non-git project has no origin-keyed row — that is a 200 with everything
    ineligible, not a 500. The composer must still be able to render devices."""
    from app.core import project_resolver

    with patch.object(project_resolver, "resolve_project_v2", MagicMock(return_value=None)):
        resp = _get(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["project_v2_id"] is None
    assert body["recent_ops"] == []
    assert len(body["installs"]) == 3
    assert all(i["eligible"] is False for i in body["installs"])
    assert all(i["checkouts"] == [] for i in body["installs"])


# ═══ eligibility (the shared predicate) ══════════════════════════════════════


def test_an_active_base_checkout_makes_a_device_eligible(client, project):
    checkouts_db.upsert_checkout(project["pv2"]["id"], PEER, str(project["path"]))

    body = _get(client).json()

    assert body["project_v2_id"] == project["pv2"]["id"]
    assert _install(body, PEER)["eligible"] is True
    assert _install(body, BARE)["eligible"] is False


def test_a_branch_only_checkout_is_not_eligible(client, project):
    """Exactly the case the remote preflight refuses: ``capable_runners`` would
    accept this row, but Phase 3 never clones so there is no base to fork from."""
    checkouts_db.upsert_checkout(
        project["pv2"]["id"], PEER, str(project["path"] / "feat"), branch="feat/x"
    )

    assert _install(_get(client).json(), PEER)["eligible"] is False


def test_self_is_listed_and_flagged(client, project):
    """Self is included, not filtered out — the picker offers it like any other
    device, and it is eligible off the resolver's own base-checkout seed."""
    body = _get(client).json()

    assert _install(body, ME)["is_self"] is True
    assert _install(body, PEER)["is_self"] is False
    assert _install(body, ME)["eligible"] is True


# ═══ checkout rows ═══════════════════════════════════════════════════════════


def test_checkouts_are_grouped_under_the_owning_install(client, project):
    checkouts_db.upsert_checkout(
        project["pv2"]["id"], PEER, str(project["path"]),
        head_sha="abc123", dirty=True,
    )
    checkouts_db.upsert_checkout(
        project["pv2"]["id"], PEER, str(project["path"] / "feat"), branch="feat/x"
    )
    checkouts_db.upsert_checkout(project["pv2"]["id"], ME, str(project["path"]))

    body = _get(client).json()

    peer = _install(body, PEER)["checkouts"]
    assert len(peer) == 2
    base = [c for c in peer if c["branch"] is None][0]
    assert base["path"] == str(project["path"])
    assert base["head_sha"] == "abc123"
    assert base["dirty"] is True
    assert base["status"] == "active"
    assert base["updated_at"]
    assert [c["branch"] for c in peer if c["branch"]] == ["feat/x"]

    assert len(_install(body, ME)["checkouts"]) == 1


def test_another_projects_checkouts_do_not_leak_in(client, project, mock_db):
    from app.db import projects_v2

    other = projects_v2.upsert_project("git:example/other", name="Other")
    checkouts_db.upsert_checkout(other["id"], PEER, str(project["path"] / "other"))

    body = _get(client).json()

    assert _install(body, PEER)["checkouts"] == []
    assert _install(body, PEER)["eligible"] is False


# ═══ recent ops ══════════════════════════════════════════════════════════════


def test_recent_ops_are_returned(client, project):
    op = git_ops_db.insert_op(
        PEER,
        "ensure_worktree",
        project_v2_id=project["pv2"]["id"],
        params={"branch": "feat/x", "base_branch": "main", "create_new_branch": True},
    )
    git_ops_db.complete_op(op["id"], {"path": "/w/feat-x", "branch": "feat/x"})

    ops = _get(client).json()["recent_ops"]

    assert len(ops) == 1
    assert ops[0]["id"] == op["id"]
    assert ops[0]["runner_id"] == PEER
    assert ops[0]["op"] == "ensure_worktree"
    assert ops[0]["status"] == "done"
    assert ops[0]["params"]["branch"] == "feat/x"
    assert ops[0]["result"]["path"] == "/w/feat-x"
    assert ops[0]["created_at"] and ops[0]["updated_at"]


def test_git_state_by_project_v2_id_without_a_local_projects_row(client, mock_db, tmp_path):
    """A remote-only project (projects_v2 + checkout, no local projects row)
    is readable by ``project_v2_id`` — not a 404."""
    from app.db import projects_v2

    path = tmp_path / "remote-repo"
    path.mkdir()
    database.register_install(ME, "This Mac", "http://me:8000", "darwin", "1", {})
    database.register_install(PEER, "Studio", "http://peer:8000", "win32", "1", {})
    database.register_install(BARE, "Laptop", "http://bare:8000", "linux", "1", {})
    pv2 = projects_v2.upsert_project("git:example/remote-only", name="Remote")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(path))

    resp = _get(client, project_id=None, project_v2_id=pv2["id"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["project_v2_id"] == pv2["id"]
    assert _install(body, PEER)["eligible"] is True
    assert _install(body, BARE)["eligible"] is False


def test_unknown_project_v2_id_404s(client, project):
    resp = _get(client, project_id=None, project_v2_id="pv2-does-not-exist")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_another_projects_ops_do_not_leak_in(client, project, mock_db):
    from app.db import projects_v2

    other = projects_v2.upsert_project("git:example/other", name="Other")
    git_ops_db.insert_op(
        PEER, "ensure_worktree", project_v2_id=other["id"], params={"branch": "b"}
    )

    assert _get(client).json()["recent_ops"] == []


# ═══ POST /git-state/refresh ═════════════════════════════════════════════════


def _refresh(client, **json_body):
    return client.post("/api/v1/cluster/git-state/refresh", json=json_body)


def test_refresh_auth_is_required(anon_client, project):
    assert _refresh(anon_client, project_id="proj-1", runner_id=PEER).status_code == 401


def test_refresh_202_shape(client, project):
    resp = _refresh(client, project_id="proj-1", runner_id=PEER)

    assert resp.status_code == 202
    body = resp.json()
    assert list(body.keys()) == ["ops"]
    assert len(body["ops"]) == 1
    op = body["ops"][0]
    assert op["runner_id"] == PEER
    assert op["id"]
    row = git_ops_db.get_op(op["id"])
    assert row is not None
    assert row["op"] == "refresh_status"
    assert row["status"] == "queued"
    assert row["runner_id"] == PEER
    assert row["project_v2_id"] == project["pv2"]["id"]


def test_refresh_400_without_runner_id(client, project):
    resp = _refresh(client, project_id="proj-1")

    assert resp.status_code == 400
    assert "runner_id" in resp.json()["detail"]


def test_refresh_400_both_ids(client, project):
    resp = _refresh(
        client,
        project_id="proj-1",
        project_v2_id=project["pv2"]["id"],
        runner_id=PEER,
    )

    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"]


def test_refresh_400_neither_id(client, project):
    resp = _refresh(client, runner_id=PEER)

    assert resp.status_code == 400
    assert "exactly one" in resp.json()["detail"]
