"""API surface for ProviderAgent worker attribution + stop-all (chunk P3-A4).

Plan §2 C.4 asks for two user-facing things alongside turn-lifetime teardown:
per-turn worker token/spend attributed to the worker's PROVIDER and visible
while the turn runs, and a single control that stops every live worker. These
are the HTTP routes behind both, plus the additive ``/live`` field.

The supervisor's kill path is spied on rather than executed — the point here is
routing, auth and wire shape, not the teardown mechanics (those are covered in
``tests/core/test_provider_agent_safety.py`` against the real registry).
"""

from tests.conftest import async_twin
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.core import provider_agent_supervisor as sup
from tests.api.helpers import mock_api_user  # noqa: F401  (fixture)


SESSION = "parent-session"


@pytest.fixture
def mock_database():
    with patch("app.api.sessions.database") as mock_db, \
            patch("app.api.sessions_helpers.database", mock_db):
        mock_db.get_session.return_value = {"id": SESSION, "profile_id": "p1"}
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        mock_db.get_installs_map.return_value = {}
        mock_db.get_project.return_value = None
        yield mock_db


@pytest.fixture
def client(mock_database, mock_api_user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clean_supervisor():
    sup.reset()
    yield
    sup.reset()


def _worker(worker_id, provider="prov-openai", model="gpt-5.5"):
    return sup.register_worker(sup.WorkerRecord(
        parent_session_id=SESSION,
        worker_session_id=worker_id,
        task_id=worker_id,
        tool_use_id=worker_id,
        provider_id=provider,
        model_id=model,
    ))


@pytest.fixture
def kill_spies():
    """Stub the two acts that end a worker so no session/subprocess is touched."""
    pool = MagicMock(name="session_pool")
    pool.remove_broken = AsyncMock(return_value=object())
    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
            patch("app.core.turn_manager.interrupt", _wt_interrupt := MagicMock(return_value={"id": "t"})), \
            patch("app.core.turn_manager.interrupt_async", new=async_twin(_wt_interrupt)), \
            patch("app.core.provider_agent_supervisor._note_parent", new=AsyncMock()):
        yield pool


# ---------------------------------------------------------------------------
# GET /{session_id}/provider-workers
# ---------------------------------------------------------------------------

class TestListProviderWorkers:

    def test_reports_per_provider_attribution(self, client):
        _worker("w1", provider="prov-openai")
        _worker("w2", provider="prov-openai", model="gpt-5.5-mini")
        _worker("w3", provider="prov-codex", model="gpt-5.5-codex")
        sup.record_usage(SESSION, "w1", {"tokens_in": 100, "total_cost_usd": 0.5})
        sup.record_usage(SESSION, "w3", {"tokens_in": 10, "total_cost_usd": 0.1})

        body = client.get(f"/api/v1/sessions/{SESSION}/provider-workers").json()

        assert body["session_id"] == SESSION
        assert body["totals"]["workers"] == 3
        assert body["totals"]["tokens_in"] == 110
        assert body["providers"]["prov-openai"]["workers"] == 2
        assert body["providers"]["prov-codex"]["tokens_in"] == 10

    def test_nothing_live_is_an_empty_report_not_a_404(self, client):
        response = client.get(f"/api/v1/sessions/{SESSION}/provider-workers")

        assert response.status_code == 200
        assert response.json()["workers"] == []


# ---------------------------------------------------------------------------
# POST /{session_id}/provider-workers/stop
# ---------------------------------------------------------------------------

class TestStopProviderWorkers:

    def test_stops_every_live_worker(self, client, kill_spies):
        for i in range(4):
            _worker(f"w{i}")

        body = client.post(f"/api/v1/sessions/{SESSION}/provider-workers/stop").json()

        assert body["stopped"] == 4
        assert kill_spies.remove_broken.await_count == 4
        assert sup.live_workers(SESSION) == []

    def test_is_idempotent(self, client, kill_spies):
        _worker("w1")

        first = client.post(f"/api/v1/sessions/{SESSION}/provider-workers/stop").json()
        second = client.post(f"/api/v1/sessions/{SESSION}/provider-workers/stop").json()

        assert first["stopped"] == 1
        assert second["stopped"] == 0

    def test_only_this_sessions_workers_are_stopped(self, client, kill_spies):
        _worker("w1")
        sup.register_worker(sup.WorkerRecord(
            parent_session_id="other-session", worker_session_id="w9",
            task_id="w9", tool_use_id="w9", provider_id="prov-openai",
        ))

        client.post(f"/api/v1/sessions/{SESSION}/provider-workers/stop")

        assert sup.live_workers(SESSION) == []
        assert [r.worker_session_id for r in sup.live_workers("other-session")] == ["w9"]


# ---------------------------------------------------------------------------
# The additive /live field
# ---------------------------------------------------------------------------

class TestLiveAttributionField:
    """Attached ONLY when workers are live, so the pinned /live wire shape is
    unchanged for every ordinary session."""

    def test_absent_when_no_workers_are_live(self):
        from app.core import provider_agent_supervisor

        assert provider_agent_supervisor.has_live_workers(SESSION) is False

    def test_present_and_shaped_once_workers_are_live(self):
        from app.core import provider_agent_supervisor

        _worker("w1")
        provider_agent_supervisor.record_usage(SESSION, "w1", {"tokens_out": 12})

        assert provider_agent_supervisor.has_live_workers(SESSION) is True
        report = provider_agent_supervisor.attribution(SESSION)
        assert set(report) == {"workers", "providers", "totals"}
        assert report["workers"][0]["workerSessionId"] == "w1"
        assert report["totals"]["tokens_out"] == 12
