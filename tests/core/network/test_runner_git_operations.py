"""Runner Git state and operations cross the network owner boundary."""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import cluster as cluster_api
from app.api import sessions_worktree as sessions_worktree
from app.api.auth import require_auth
from app.api.sessions_helpers import CreateWorktreeSessionRequest
from app.core import git_ops_runner, project_resolver
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import database, git_ops as git_ops_db
from app.db import network as store
from app.network import engine, tokens, transport
from tests.core.network.harness import Worker, join
from tests.core.network.test_transport import ServedBy


LEGACY_A = "legacy-a"
LEGACY_B = "legacy-b"
PROJECT_ID = "project-1"


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


def _effective_id_for(*workers):
    by_device = {
        worker.device_id: legacy
        for worker, legacy in zip(workers, (LEGACY_A, LEGACY_B))
    }

    def _effective(_settings):
        context = engine.context()
        return by_device.get(getattr(context, "device_id", None), "outside-test")

    return property(_effective)


def _join_with_distinct_legacy_ids(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    monkeypatch.setattr(
        type(settings), "effective_instance_id", _effective_id_for(a, b)
    )
    with a.active():
        engine.create_network(
            "Home", device_name="A", legacy_instance_id=LEGACY_A
        )
    join(a, b, name="B", legacy_instance_id=LEGACY_B)
    return a, b


def _seed_project(worker, tmp_path):
    with worker.active():
        path = tmp_path / f"{worker.name}-repo"
        path.mkdir()
        database.create_project(
            project_id=PROJECT_ID, name="Repo", description=None, path=str(path)
        )
        pv2 = project_resolver.resolve_project_v2({"project_id": PROJECT_ID})
        assert pv2 is not None
        return {"legacy": database.get_project(PROJECT_ID), "pv2": pv2, "path": path}


def _serve_peer(monkeypatch, peer):
    served = ServedBy(peer, "http://peer.local:8000")
    real_async_client = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    return served


def _cluster_app():
    app = FastAPI()
    app.include_router(cluster_api.router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    return app


def _install(response_body, device_id):
    rows = [row for row in response_body["installs"] if row["id"] == device_id]
    assert rows, f"Missing {device_id} from git-state response"
    return rows[0]


def test_git_state_reads_each_independent_owner_with_legacy_checkout_ids(
    workers, monkeypatch, tmp_path
):
    """The UI receives device ids although each owner stores legacy runner ids."""
    a, b = _join_with_distinct_legacy_ids(workers, monkeypatch)
    a_project = _seed_project(a, tmp_path)
    b_project = _seed_project(b, tmp_path)
    assert a_project["pv2"]["id"] == b_project["pv2"]["id"]
    with b.active():
        assert checkouts_db.has_active_base_checkout(
            b_project["pv2"]["id"], LEGACY_B
        )
        assert not checkouts_db.has_active_base_checkout(
            b_project["pv2"]["id"], b.device_id
        )

    _serve_peer(monkeypatch, b)
    with a.active():
        store.touch_member(b.device_id, endpoints=["http://peer.local:8000"])
        transport.note_contact(b.device_id)
        with TestClient(_cluster_app()) as client:
            response = client.get(
                "/api/v1/cluster/git-state",
                params={"project_v2_id": a_project["pv2"]["id"]},
            )

    assert response.status_code == 200
    body = response.json()
    assert _install(body, a.device_id)["eligible"] is True
    peer = _install(body, b.device_id)
    assert peer["eligible"] is True
    assert peer["checkouts"]
    assert all("path" not in row for row in peer["checkouts"])


def test_refresh_dispatches_to_remote_owner_and_its_legacy_claim_loop(
    workers, monkeypatch, tmp_path
):
    """A device-id refresh is enqueued and claimed in B's separate database."""
    a, b = _join_with_distinct_legacy_ids(workers, monkeypatch)
    a_project = _seed_project(a, tmp_path)
    _seed_project(b, tmp_path)
    _serve_peer(monkeypatch, b)

    with a.active():
        store.touch_member(b.device_id, endpoints=["http://peer.local:8000"])
        transport.note_contact(b.device_id)
        with TestClient(_cluster_app()) as client:
            response = client.post(
                "/api/v1/cluster/git-state/refresh",
                json={"project_v2_id": a_project["pv2"]["id"], "runner_id": b.device_id},
            )
        assert response.status_code == 202
        assert response.json()["ops"][0]["runner_id"] == b.device_id
        assert git_ops_db.list_queued_for_runner(LEGACY_A) == []

    claimed = []

    async def record_claim(op):
        claimed.append(op)

    monkeypatch.setattr(git_ops_runner, "run_op", record_claim)
    with b.active():
        queued = git_ops_db.list_queued_for_runner(LEGACY_B)
        assert len(queued) == 1
        assert queued[0]["op"] == "refresh_status"
        assert asyncio.run(git_ops_runner.claim_once()) == 1
    assert claimed and claimed[0]["runner_id"] == LEGACY_B


def test_remote_worktree_target_queues_only_on_the_target_owner(
    workers, monkeypatch, tmp_path
):
    """A selected device is preflighted and receives Git work through control."""
    a, b = _join_with_distinct_legacy_ids(workers, monkeypatch)
    a_project = _seed_project(a, tmp_path)
    _seed_project(b, tmp_path)
    _serve_peer(monkeypatch, b)

    body = CreateWorktreeSessionRequest(
        project_id=PROJECT_ID,
        branch_name="feature/network-owner",
        base_branch="main",
        create_new_branch=True,
        target_instance_id=b.device_id,
    )
    with patch.object(sessions_worktree, "_broadcast_session_created"):
        with a.active():
            store.touch_member(b.device_id, endpoints=["http://peer.local:8000"])
            transport.note_contact(b.device_id)
            response = asyncio.run(
                sessions_worktree._create_remote_branch_session(
                    body,
                    a_project["legacy"],
                    None,
                    a_project["pv2"],
                    b.device_id,
                )
            )
            assert response.worktree_path is None
            assert git_ops_db.list_queued_for_runner(LEGACY_A) == []
            assert database.get_session(response.session_id)["owner_device_id"] == b.device_id

    with b.active():
        owned = database.get_session(response.session_id)
        assert owned["owner_device_id"] == b.device_id
        assert owned["instance_id"] == LEGACY_B
        queued = git_ops_db.list_queued_for_runner(LEGACY_B)
    assert len(queued) == 1
    assert queued[0]["op"] == "ensure_worktree"
    assert queued[0]["params"]["branch"] == "feature/network-owner"
