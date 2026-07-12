"""Tests for the per-session ``sessions.v2_stream`` flag (V2 Phase 1).

Covers the three surfaces the flag touches:
  * stamping at chat/REST session creation from ``settings.v2_stream_default``
    (the chat WS handler passes ``v2_stream=settings.v2_stream_default`` into the
    shared ``database.create_session`` helper);
  * the PATCH ``/api/v1/sessions/{id}`` route flipping the flag, with the
    detail and list endpoints reflecting the new value;
  * the ``/api/v1/sessions/{id}/live`` payload carrying the field.
"""

import uuid

from app.db import database
from app.core.config import settings


def _new_session(v2_stream: bool = False) -> str:
    """Create a session through the shared create helper and return its id."""
    session_id = str(uuid.uuid4())
    database.create_session(
        session_id=session_id,
        profile_id="default",
        title="v2-stream test",
        v2_stream=v2_stream,
    )
    return session_id


# ---------------------------------------------------------------------------
# Stamping at creation
# ---------------------------------------------------------------------------

def test_v2_stream_default_is_on():
    """Phase-1 cutover: the shipped default opts NEW sessions into v2 streaming.

    Asserted on the field default (not a Settings instance) so a local
    V2_STREAM_DEFAULT env override can't skew the check.
    """
    from app.core.config import Settings
    assert Settings.model_fields["v2_stream_default"].default is True


def test_create_stamps_true_when_default_true(mock_db, monkeypatch):
    """A chat/REST create with v2_stream_default=True stamps the column true."""
    monkeypatch.setattr(settings, "v2_stream_default", True, raising=False)

    # Mirror the chat WS handler: it passes settings.v2_stream_default through.
    session_id = _new_session(v2_stream=settings.v2_stream_default)

    row = database.get_session(session_id)
    assert row is not None
    assert bool(row["v2_stream"]) is True


def test_create_stamps_false_by_default(mock_db, monkeypatch):
    """v2_stream_default=False (the legacy fallback) stamps the column false."""
    monkeypatch.setattr(settings, "v2_stream_default", False, raising=False)

    session_id = _new_session(v2_stream=settings.v2_stream_default)

    row = database.get_session(session_id)
    assert row is not None
    assert bool(row["v2_stream"]) is False


def test_create_default_param_is_false(mock_db):
    """Non-chat creation paths that omit v2_stream get the FALSE default."""
    session_id = str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default")

    row = database.get_session(session_id)
    assert row is not None
    assert bool(row["v2_stream"]) is False


# ---------------------------------------------------------------------------
# PATCH flip + detail/list surfacing
# ---------------------------------------------------------------------------

def test_patch_flips_flag_and_detail_returns_it(authenticated_client):
    session_id = _new_session(v2_stream=False)

    resp = authenticated_client.patch(
        f"/api/v1/sessions/{session_id}", params={"v2_stream": True}
    )
    assert resp.status_code == 200, resp.text

    # Persisted on the row.
    row = database.get_session(session_id)
    assert bool(row["v2_stream"]) is True

    # Detail endpoint reflects it.
    detail = authenticated_client.get(f"/api/v1/sessions/{session_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["v2_stream"] is True


def test_list_endpoint_returns_flag(authenticated_client):
    session_id = _new_session(v2_stream=True)

    resp = authenticated_client.get("/api/v1/sessions")
    assert resp.status_code == 200, resp.text
    sessions = {s["id"]: s for s in resp.json()}
    assert session_id in sessions
    assert sessions[session_id]["v2_stream"] is True


# ---------------------------------------------------------------------------
# /live payload
# ---------------------------------------------------------------------------

def test_live_payload_contains_flag(authenticated_client):
    session_id = _new_session(v2_stream=True)

    resp = authenticated_client.get(f"/api/v1/sessions/{session_id}/live")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "v2_stream" in body
    assert body["v2_stream"] is True
