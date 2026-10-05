"""Client-facing session payloads never carry the chat's account label (#1849).

``sessions.provenance_owner`` names who started a chat (an account or paired
device id). It syncs between workers for the image check, but the raw session
rows a few endpoints return must not hand it to a client.
"""
from app.core.session_payload import client_view
from app.db import database
from app.db.base import get_db
from tests.api.test_assistant import client  # noqa: F401 - fixture
from tests.api.test_chat_ws_offload import _LoadSessionPatches
from tests.api.test_chat_ws_v2_turns import _make_handler, _make_session

LABEL = "account:acct-secret"


def _label(session_id):
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET provenance_owner = %s WHERE id = %s", (LABEL, session_id))
    assert database.get_session(session_id)["provenance_owner"] == LABEL


def _walk(value):
    """Every key and string anywhere in a JSON-like payload."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, str):
        yield value


def _clean(payload):
    seen = list(_walk(payload))
    assert "provenance_owner" not in seen
    assert LABEL not in seen


def test_patch_session_response_has_no_label(client):  # noqa: F811
    database.create_session(session_id="patched", profile_id="default", title="before")
    _label("patched")
    response = client.patch("/api/v1/sessions/patched", params={"title": "after"})
    assert response.status_code == 200
    assert response.json()["title"] == "after"
    _clean(response.json())
    # The column itself is untouched: workers still sync it.
    assert database.get_session("patched")["provenance_owner"] == LABEL


def test_assistant_open_response_has_no_label(client):  # noqa: F811
    client.patch("/api/v1/assistant", json={"create_agent": True})
    home_id = client.get("/api/v1/assistant").json()["home_session_id"]
    _label(home_id)
    response = client.post("/api/v1/assistant/open")
    assert response.status_code == 200
    assert response.json()["session"]["id"] == home_id
    _clean(response.json())


async def test_websocket_history_frame_has_no_label(mock_db):
    _make_session("sess-1")
    _label("sess-1")
    with _LoadSessionPatches():
        handler = _make_handler()
        await handler.handle_load_session({"session_id": "sess-1"})
    history = [c.args[0] for c in handler.send_json.call_args_list
               if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "history"]
    assert len(history) == 1 and history[0]["session"]["id"] == "sess-1"
    _clean(history[0])


def test_client_view_keeps_everything_else():
    row = {"id": "s", "title": "t", "provenance_owner": LABEL}
    assert client_view(row) == {"id": "s", "title": "t"}
    assert row["provenance_owner"] == LABEL  # a copy, not an edit of the row
    assert client_view(None) is None
