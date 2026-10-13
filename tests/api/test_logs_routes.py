"""Log routes in ``app.api.logs``: the in-memory ring buffer and the error log.

``/logs``, ``/logs/export`` and ``/logs/errors`` are owner-only.
``/logs/client-error`` accepts any signed-in caller, API keys included, so its
free-form fields are bounded before they are stored.
"""
import logging

import pytest

from app.api.auth import hash_api_key
from app.db import database
from app.db import error_logs

KEY = "aih_logs_routes_key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def owner(client, monkeypatch):
    from app.api import auth, logs

    monkeypatch.setattr(auth.owner_auth, "local_access",
                        lambda request: "authorization" not in request.headers)
    client.headers["Origin"] = "http://testserver"
    database.create_api_user("k-logs", "Key", hash_api_key(KEY), project_id=None)
    # A private buffer so records from app startup or other tests don't count.
    ring = logs._RingBufferHandler(capacity=50)
    ring.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    monkeypatch.setattr(logs, "ring_handler", ring)
    for level, msg in [(logging.INFO, "alpha started"), (logging.WARNING, "Beta slow"),
                       (logging.ERROR, "alpha failed"), (logging.INFO, "gamma done")]:
        ring.emit(logging.LogRecord("t.logs", level, __file__, 1, msg, None, None))
    return client


@pytest.mark.parametrize("path", ["/api/v1/logs", "/api/v1/logs/export", "/api/v1/logs/errors"])
def test_reading_logs_is_owner_only(owner, path):
    assert owner.get(path, headers=AUTH).status_code == 403


class TestRingBuffer:
    def test_newest_first_with_levels(self, owner):
        body = owner.get("/api/v1/logs").json()
        assert body["total"] == 4 and body["returned"] == 4
        assert body["levels"] == ["ERROR", "INFO", "WARNING"]
        assert body["lines"][0]["message"] == "INFO gamma done"
        assert body["lines"][0]["logger_name"] == "t.logs"

    def test_level_filter_is_case_insensitive(self, owner):
        body = owner.get("/api/v1/logs", params={"level": "info"}).json()
        assert [line["message"] for line in body["lines"]] == ["INFO gamma done", "INFO alpha started"]

    def test_search_and_limit(self, owner):
        body = owner.get("/api/v1/logs", params={"search": "ALPHA", "limit": 1}).json()
        assert [line["message"] for line in body["lines"]] == ["ERROR alpha failed"]
        assert body["total"] == 4

    @pytest.mark.parametrize("limit", [0, 5001])
    def test_limit_out_of_range_is_422(self, owner, limit):
        assert owner.get("/api/v1/logs", params={"limit": limit}).status_code == 422

    def test_export_is_a_download_in_order(self, owner):
        r = owner.get("/api/v1/logs/export")
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith('attachment; filename="ai-shuffle-logs-')
        assert r.text.splitlines() == [
            "INFO alpha started", "WARNING Beta slow", "ERROR alpha failed", "INFO gamma done"]


class TestClientErrors:
    def _capture(self, monkeypatch):
        calls = []

        def fake_record(source, message, **kw):
            # Backend WARNING+ records reach the same function without a user.
            if kw.get("user_id") is not None:
                calls.append({"source": source, "message": message, **kw})
            return "err-1"

        monkeypatch.setattr(error_logs, "record_error", fake_record)
        return calls

    def test_api_key_report_is_attributed_to_the_key(self, owner, monkeypatch):
        calls = self._capture(monkeypatch)
        r = owner.post("/api/v1/logs/client-error", headers=AUTH,
                       json={"message": "boom", "source": "backend", "level": "warning"})
        assert r.json() == {"ok": True, "id": "err-1"}
        (call,) = calls
        # A client cannot pose as the backend; the level is normalised.
        assert (call["source"], call["level"], call["user_id"]) == ("frontend", "WARNING", "k-logs")

    def test_owner_report_keeps_its_source(self, owner, monkeypatch):
        calls = self._capture(monkeypatch)
        owner.post("/api/v1/logs/client-error", json={"message": "ws dropped", "source": "ws"})
        assert (calls[0]["source"], calls[0]["user_id"], calls[0]["level"]) == ("ws", "admin", "ERROR")

    def test_message_is_required(self, owner):
        assert owner.post("/api/v1/logs/client-error", json={"source": "ws"}).status_code == 422

    def test_oversize_free_form_fields_are_bounded(self, owner, monkeypatch):
        calls = self._capture(monkeypatch)
        r = owner.post("/api/v1/logs/client-error", headers=AUTH, json={
            "message": "big", "level": "L" * 500, "url": "https://x/" + "u" * 10_000,
            "session_id": "s" * 1000, "context": {"blob": "c" * 100_000},
        })
        assert r.status_code == 200
        (call,) = calls
        assert len(call["level"]) == 16
        assert len(call["url"]) == 2048
        assert len(call["session_id"]) == 200
        assert call["context"]["truncated"] is True
        assert call["context"]["original_bytes"] > 100_000

    def test_small_context_is_kept(self, owner, monkeypatch):
        calls = self._capture(monkeypatch)
        owner.post("/api/v1/logs/client-error", json={"message": "m", "context": {"route": "/chat"}})
        assert calls[0]["context"] == {"route": "/chat"}

    def test_reports_are_readable_by_the_owner(self, owner):
        owner.post("/api/v1/logs/client-error", headers=AUTH,
                   json={"message": "stored one", "source": "api", "session_id": "sess-9"})
        body = owner.get("/api/v1/logs/errors", params={"source": "api", "session_id": "sess-9"}).json()
        assert body["returned"] == 1
        assert body["errors"][0]["message"] == "stored one"
        assert owner.get("/api/v1/logs/errors", params={"limit": 501}).status_code == 422
