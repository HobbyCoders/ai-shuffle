"""A new CHAT session may carry attachments without a project.

The send path refuses a brand-new session that has ``upload_paths`` but no
``project_id``: on the code lane those paths name files in a checkout the
executing install does not have, so the prompt would point the agent at nothing.
A chat session is projectless BY DESIGN and its attachments live in the
workspace's chat-uploads folder, which every local turn can reach — so the
refusal has to make an exception for exactly that case, and only that case.

The membership test is ``chat_mode_read_permitted``, the same function the
permission callback gates ``Read`` with, so a file the send accepts is a file
the agent is allowed to open. These tests cover both directions of that.
"""

import pytest

from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT
from app.core.session_modes import chat_uploads_dir
from app.db import database

from tests.api.test_chat_ws_v2_turns import _make_handler, _Patches, _sent


@pytest.fixture
def chat_workspace(tmp_path, monkeypatch):
    """Workspace root at a temp dir; hand back the chat uploads directory."""
    import app.core.config as config

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    uploads = chat_uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _agent():
    """A real agent row — the send refuses an id that names nothing."""
    database.create_profile("chat-agent", "Chat", None, {"model": "opus"})
    return "chat-agent"


async def _send(mode, upload_paths, prompt="what is in this?"):
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": prompt,
            "session_id": None,
            "profile": _agent(),
            "project": None,
            "mode": mode,
            "upload_paths": [str(p) for p in upload_paths],
        })
    return handler


def _errors(handler):
    return [f["message"] for f in _sent(handler, "error")]


async def test_chat_session_with_uploads_and_no_project_is_created(
    mock_db, chat_workspace
):
    attachment = chat_workspace / "screenshot.png"
    attachment.write_bytes(b"png")

    handler = await _send("chat", [attachment])

    assert _errors(handler) == []
    created = _sent(handler, "session_created")
    assert len(created) == 1
    row = database.get_session(created[0]["session_id"])
    assert row["mode"] == "chat"
    assert row["project_id"] is None


async def test_code_session_with_uploads_and_no_project_is_still_refused(
    mock_db, chat_workspace
):
    """The case the guard was written for is untouched: a code-lane compose with
    no checkout still refuses rather than sending a prompt full of paths the
    agent cannot open."""
    attachment = chat_workspace / "screenshot.png"
    attachment.write_bytes(b"png")

    handler = await _send("code", [attachment])

    assert _errors(handler) == [ATTACHMENTS_NEED_LOCAL_CHECKOUT]
    assert _sent(handler, "session_created") == []


async def test_chat_session_with_a_path_outside_the_uploads_dir_is_refused(
    mock_db, chat_workspace, tmp_path
):
    """The exception is scoped to the uploads folder, not to the lane: a frame
    claiming mode 'chat' cannot smuggle an arbitrary disk path into the prompt,
    which is what makes the send agree with the Read fence."""
    outside = tmp_path / "id_rsa"
    outside.write_bytes(b"secret")

    handler = await _send("chat", [outside])

    assert _errors(handler) == [ATTACHMENTS_NEED_LOCAL_CHECKOUT]
    assert _sent(handler, "session_created") == []


async def test_chat_session_refuses_when_only_one_attachment_escapes(
    mock_db, chat_workspace, tmp_path
):
    """All-or-nothing: one bad path in the list refuses the whole send rather
    than quietly dropping it, so the user never gets an answer about a file the
    agent silently could not read."""
    good = chat_workspace / "ok.png"
    good.write_bytes(b"png")
    bad = tmp_path / "escape.txt"
    bad.write_bytes(b"x")

    handler = await _send("chat", [good, bad])

    assert _errors(handler) == [ATTACHMENTS_NEED_LOCAL_CHECKOUT]


async def test_chat_session_without_uploads_is_unaffected(mock_db, chat_workspace):
    handler = await _send("chat", [])

    assert _errors(handler) == []
    assert len(_sent(handler, "session_created")) == 1
