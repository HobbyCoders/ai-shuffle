"""Tests for the per-session ``sessions.v2_stream`` column (V2 Phase 1).

The V2 deletion forced v2_stream TRUE everywhere: the ``create_session`` helper
stamps the column TRUE unconditionally (the ``v2_stream`` parameter is gone —
the V2 event log is the only streaming path), the ``V2_STREAM_DEFAULT`` setting
is gone, and the PATCH route no longer accepts the flag. The column and its GET
serialization survive (a later commit retires the frontend read), so these tests
cover:
  * the shared ``database.create_session`` helper stamping the column TRUE;
  * the PATCH ``/api/v1/sessions/{id}`` route NO LONGER flipping the flag;
  * the detail / list / ``/live`` payloads carrying the field.
"""

import uuid

from app.db import database


def _new_session() -> str:
    """Create a session through the shared create helper and return its id."""
    session_id = str(uuid.uuid4())
    database.create_session(
        session_id=session_id,
        profile_id="default",
        title="v2-stream test",
    )
    return session_id


# ---------------------------------------------------------------------------
# Stamping at creation (the create_session helper always stamps the column)
# ---------------------------------------------------------------------------

def test_create_stamps_true(mock_db):
    """Every create stamps the column TRUE (the chat/REST path)."""
    session_id = _new_session()

    row = database.get_session(session_id)
    assert row is not None
    assert bool(row["v2_stream"]) is True


def test_create_always_stamps_true_no_param(mock_db):
    """A bare create (no v2_stream param — it no longer exists) still stamps
    TRUE: the V2 event log is the only streaming path, so there is no non-V2
    creation mode."""
    session_id = str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default")

    row = database.get_session(session_id)
    assert row is not None
    assert bool(row["v2_stream"]) is True


# ---------------------------------------------------------------------------
# PATCH no longer accepts v2_stream + detail/list surfacing
# ---------------------------------------------------------------------------

def test_patch_ignores_v2_stream(authenticated_client):
    # PATCH dropped v2_stream from its updatable fields (Commit 5): a v2_stream
    # query param is now an unknown arg and must not flip the column.
    session_id = _new_session()

    resp = authenticated_client.patch(
        f"/api/v1/sessions/{session_id}", params={"v2_stream": False}
    )
    assert resp.status_code == 200, resp.text

    # Unchanged on the row.
    row = database.get_session(session_id)
    assert bool(row["v2_stream"]) is True

    # Detail endpoint still serializes the (unchanged) flag.
    detail = authenticated_client.get(f"/api/v1/sessions/{session_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["v2_stream"] is True


def test_list_endpoint_returns_flag(authenticated_client):
    session_id = _new_session()

    resp = authenticated_client.get("/api/v1/sessions")
    assert resp.status_code == 200, resp.text
    sessions = {s["id"]: s for s in resp.json()}
    assert session_id in sessions
    assert sessions[session_id]["v2_stream"] is True


# ---------------------------------------------------------------------------
# /live payload
# ---------------------------------------------------------------------------

def test_live_payload_contains_flag(authenticated_client):
    session_id = _new_session()

    resp = authenticated_client.get(f"/api/v1/sessions/{session_id}/live")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "v2_stream" in body
    assert body["v2_stream"] is True
