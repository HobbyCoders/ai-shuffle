"""Unit tests for the owner-side cluster permission relay endpoints.

A pending tool-permission request lives ONLY in the owning install's in-memory
``permission_handler``. When a user drives a cloud-mode session from a
*different* install, that viewer install:

- relays ``get_pending_permissions`` to the owner's
  ``/api/v1/cluster/sessions/{id}/pending_permissions`` so the modal can
  surface, and
- relays the approve/deny/defer decision to the owner's
  ``/api/v1/cluster/sessions/{id}/permission_response`` so the owner's awaiting
  ``request_permission`` coroutine unblocks (instead of sitting until the 300s
  auto-deny).

These endpoints operate on the LOCAL handler (they run on the owner) and never
re-relay. Everything is mocked at the handler/DB boundary — no app, no DB, no
network. Mirrors tests/api/test_cluster_question_relay.py.
"""

import pytest
from fastapi import HTTPException

import app.api.cluster as cluster_api
from app.api.cluster import (
    ClusterPermissionResponseRequest,
    cluster_pending_permissions,
    cluster_permission_response,
)


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def test_pending_permissions_returns_local_handler_sets(monkeypatch):
    """Reads the owner's LOCAL pending + deferred permission sets by shared id."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured = {"pending": None, "deferred": None}
    pending = [{"request_id": "p-1", "tool_name": "Bash"}]
    deferred = [{"request_id": "p-2", "tool_name": "Read"}]

    from app.core import permission_handler as ph_mod

    def fake_pending(session_id):
        captured["pending"] = session_id
        return pending

    def fake_deferred(session_id):
        captured["deferred"] = session_id
        return deferred

    monkeypatch.setattr(ph_mod.permission_handler, "get_pending_requests", fake_pending)
    monkeypatch.setattr(ph_mod.permission_handler, "get_deferred_requests", fake_deferred)

    result = await cluster_pending_permissions("sess-1", install_id="peer-9")

    assert result == {
        "session_id": "sess-1",
        "pending": pending,
        "deferred": deferred,
    }
    assert captured == {"pending": "sess-1", "deferred": "sess-1"}


async def test_pending_permissions_404_when_session_missing(monkeypatch):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_pending_permissions("missing", install_id="x")
    assert ei.value.status_code == 404


async def test_permission_response_resolves_local_waiter(monkeypatch):
    """Delivers the decision to the owner's LOCAL handler.respond and reports ok."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured = {}

    from app.core import permission_handler as ph_mod

    async def fake_respond(*, request_id, session_id, decision, remember, pattern, broadcast_func):
        captured["request_id"] = request_id
        captured["session_id"] = session_id
        captured["decision"] = decision
        captured["remember"] = remember
        captured["pattern"] = pattern
        # A resolved request returns {"resolved": True, ...}
        return {"resolved": True, "auto_resolved_count": 0, "auto_resolved_ids": []}

    monkeypatch.setattr(ph_mod.permission_handler, "respond", fake_respond)

    body = ClusterPermissionResponseRequest(
        request_id="p-1", decision="allow", remember="session", pattern="Bash:npm*"
    )
    result = await cluster_permission_response("sess-1", body, install_id="peer-9")

    assert result == {"ok": True, "session_id": "sess-1", "request_id": "p-1"}
    assert captured == {
        "request_id": "p-1",
        "session_id": "sess-1",
        "decision": "allow",
        "remember": "session",
        "pattern": "Bash:npm*",
    }


async def test_permission_response_defer_drops_remember_and_pattern(monkeypatch):
    """A defer decision must not carry a remember/pattern (mirrors the WS handler)."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    captured = {}

    from app.core import permission_handler as ph_mod

    async def fake_respond(*, request_id, session_id, decision, remember, pattern, broadcast_func):
        captured["remember"] = remember
        captured["pattern"] = pattern
        return {"resolved": True, "deferred": True}

    monkeypatch.setattr(ph_mod.permission_handler, "respond", fake_respond)

    body = ClusterPermissionResponseRequest(
        request_id="p-1", decision="defer", remember="session", pattern="X"
    )
    result = await cluster_permission_response("sess-1", body, install_id="x")

    assert result["ok"] is True
    assert captured == {"remember": None, "pattern": None}


async def test_permission_response_reports_false_when_no_pending(monkeypatch):
    """A stale/unknown request_id surfaces ok=False (respond returned an error)."""
    fake_session = {"id": "sess-1", "instance_id": "owner"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)

    from app.core import permission_handler as ph_mod

    async def fake_respond(*, request_id, session_id, decision, remember, pattern, broadcast_func):
        return {"error": "Request not found"}

    monkeypatch.setattr(ph_mod.permission_handler, "respond", fake_respond)

    body = ClusterPermissionResponseRequest(request_id="stale", decision="allow")
    result = await cluster_permission_response("sess-1", body, install_id="x")

    assert result == {"ok": False, "session_id": "sess-1", "request_id": "stale"}


async def test_permission_response_404_when_session_missing(monkeypatch):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_permission_response(
            "missing",
            ClusterPermissionResponseRequest(request_id="p-1", decision="allow"),
            install_id="x",
        )
    assert ei.value.status_code == 404


# --- auth rejection ----------------------------------------------------------
#
# Both endpoints depend on cluster.require_cluster_auth, which raises 401 for a
# missing/invalid shared cluster secret. Exercised via the dependency directly
# (the endpoints never run without it satisfied).


def _fake_request(headers=None):
    from types import SimpleNamespace

    return SimpleNamespace(headers=headers or {})


async def test_cluster_permission_endpoints_reject_bad_secret(monkeypatch):
    from app.core import cluster as cluster_core

    monkeypatch.setattr(cluster_core, "verify_cluster_secret", lambda presented: False)

    with pytest.raises(HTTPException) as ei:
        cluster_core.require_cluster_auth(_fake_request())
    assert ei.value.status_code == 401
