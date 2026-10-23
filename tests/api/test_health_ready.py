"""Tests for the /health/ready readiness probe (Phase 4.4).

The readiness endpoint must:
  * require NO auth (probes are unauthenticated)
  * return 200 + a small JSON body when the DB answers, however busy the
    session pool is (the container healthcheck probes it; busy != unhealthy)
  * return 503 + reasons when the DB is down

These use FastAPI's TestClient against a bare app mounting only the system
router (matching the pattern in tests/api/test_system.py::TestValidateWorkspaceAuth).
"""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from app.api.system import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class _FakePool:
    """Minimal stand-in for SessionPool exposing the accessors the probe reads."""

    def __init__(self, size=0, max_size=3):
        self._size = size
        self.max_size = max_size
        self.delegated_size = size

    def __len__(self):
        return self._size


class TestReadinessReady:
    def test_ready_returns_200_and_body(self):
        client = _client()
        with patch("app.api.system._probe_db", return_value=None), \
             patch("app.core.session_pool.get_session_pool",
                   return_value=_FakePool(size=1, max_size=3)):
            resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"] == "ok"
        assert body["checks"]["session_pool"]["size"] == 1
        assert body["checks"]["session_pool"]["warm_size"] == 3
        assert "reasons" not in body

    def test_api_prefixed_alias_works(self):
        client = _client()
        with patch("app.api.system._probe_db", return_value=None), \
             patch("app.core.session_pool.get_session_pool",
                   return_value=_FakePool(size=0, max_size=3)):
            resp = client.get("/api/v1/health/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_no_auth_required(self):
        """A ready probe with no credentials must still get 200 (not 401/403)."""
        client = _client()
        with patch("app.api.system._probe_db", return_value=None), \
             patch("app.core.session_pool.get_session_pool",
                   return_value=_FakePool(size=0, max_size=3)):
            resp = client.get("/health/ready")  # no cookie, no bearer
        assert resp.status_code == 200


class TestReadinessNotReady:
    def test_database_credentials_are_not_exposed_by_either_public_probe(self):
        client = _client()
        with patch("app.api.system._probe_db", side_effect=Exception(
            "cannot connect to postgresql://worker:private-test-password@localhost/db"
        )), patch("app.core.session_pool.get_session_pool", return_value=_FakePool()):
            for path in ("/health/ready", "/api/v1/health/ready"):
                response = client.get(path)
                assert response.status_code == 503
                assert response.json()["checks"]["database"] == "error"
                assert "private-test-password" not in response.text
                assert "postgresql://" not in response.text

    def test_db_down_returns_503_with_reason(self):
        client = _client()
        with patch("app.api.system._probe_db",
                   side_effect=Exception("connection refused")), \
             patch("app.core.session_pool.get_session_pool",
                   return_value=_FakePool(size=0, max_size=3)):
            resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["database"] == "error"
        assert any("database unreachable" in r for r in body["reasons"])

    def test_db_down_with_busy_pool_reports_only_the_database(self):
        client = _client()
        with patch("app.api.system._probe_db",
                   side_effect=Exception("db gone")), \
             patch("app.core.session_pool.get_session_pool",
                   return_value=_FakePool(size=40, max_size=3)):
            resp = client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["reasons"] == ["database unreachable"]


class TestBusyPoolStaysReady:
    """#1690: a worker running many agents is busy, not unhealthy."""

    def test_pool_far_past_warm_size_is_still_ready(self):
        client = _client()
        with patch("app.api.system._probe_db", return_value=None), \
             patch("app.core.session_pool.get_session_pool",
                   return_value=_FakePool(size=40, max_size=4)):
            for path in ("/health/ready", "/api/v1/health/ready"):
                resp = client.get(path)
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "ready"
                assert body["checks"]["session_pool"]["delegated_size"] == 40
                assert "reasons" not in body


class TestLivenessAndDedicatedReadiness:
    """#436: liveness never touches the DB; readiness has its own thread."""

    def test_liveness_answers_without_the_database(self):
        client = _client()
        with patch("app.api.system._probe_db", side_effect=AssertionError("liveness must not query")):
            for path in ("/health/live", "/api/v1/health/live"):
                resp = client.get(path)
                assert resp.status_code == 200
                assert resp.json() == {"status": "alive"}

    def test_readiness_query_runs_on_its_own_daemon_thread(self, monkeypatch):
        import threading

        from app.api import system
        monkeypatch.setattr(system, "_readiness_probe", system._ReadinessProbe())
        threads = []
        client = _client()
        with patch("app.api.system._probe_db", side_effect=lambda: threads.append(threading.current_thread())), \
             patch("app.core.session_pool.get_session_pool", return_value=_FakePool()):
            assert client.get("/health/ready").status_code == 200
            assert client.get("/health/ready").status_code == 200
        assert len(threads) == 2 and threads[0] is threads[1], "one dedicated thread serves every probe"
        assert threads[0].name == "readiness-db"
        assert threads[0].daemon, "a hung query must never hold up interpreter exit"

    def test_a_hung_database_check_times_out_and_is_shared(self, monkeypatch):
        import threading

        from app.api import system
        monkeypatch.setattr(system, "READINESS_DB_TIMEOUT_S", 0.2)
        monkeypatch.setattr(system, "_readiness_probe", system._ReadinessProbe())
        release = threading.Event()
        calls = []
        def hung():
            calls.append(1)
            release.wait(5)
        client = _client()
        try:
            with patch("app.api.system._probe_db", side_effect=hung), \
                 patch("app.core.session_pool.get_session_pool", return_value=_FakePool()):
                for _ in range(3):
                    resp = client.get("/health/ready")
                    assert resp.status_code == 503
                    assert resp.json()["reasons"] == ["database unreachable"]
                assert len(calls) == 1, "probes join the check in flight instead of queueing more"
                release.set()
                system._readiness_probe._inflight.result(timeout=5)
                assert client.get("/health/ready").status_code == 200, "the next probe runs a fresh check"
                assert len(calls) == 2
        finally:
            release.set()
