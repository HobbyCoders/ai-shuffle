"""Attachment provenance survives actual intake, recording and sent-message rewind."""
from unittest.mock import patch

import pytest

from app.api import commands
from app.core import session_runtime, turn_manager, turn_recorder
from app.core.session_modes import chat_uploads_dir
from app.db import database, session_events, turns
from tests.api.test_chat_ws_v2_turns import _make_handler, _Patches
from tests.core.test_rewind_resolution import (
    SID, _asst_ev, _cp, _patch_checkpoints, rewind_env,
)
from tests.core.test_steering_delivery_flows import _pump, _pump_env
from tests.rewind_helpers import execute_rewind_and_wait


@pytest.fixture
def staged_upload(mock_db, monkeypatch, tmp_path):
    from app.core import config
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    from app.core.config import settings
    database.register_install(settings.effective_instance_id, "test-runner", None, "local", "1", {}, approve_new=True)
    database.create_session(SID, "default", mode="chat", title="t")
    upload = chat_uploads_dir() / "photo.png"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(b"image")
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    return upload


@pytest.mark.parametrize("compact_first", [False, True])
async def test_intake_keeps_upload_paths(staged_upload, compact_first):
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "session_id": SID, "profile": "default",
            "prompt": f"inspect @{staged_upload}", "upload_paths": [str(staged_upload)],
        }, compact_first=compact_first)
    queued = turns.get_queued_turns(SID)
    assert queued, handler.send_json.call_args_list
    assert queued[-1]["payload"]["upload_paths"] == [str(staged_upload)]


@pytest.mark.parametrize("target_role,include_response,attached", [
    ("user", False, True), ("user", False, False),
    ("assistant", False, True), ("user", True, True),
])
async def test_sent_upload_round_trip(staged_upload, rewind_env, monkeypatch,
                                     target_role, include_response, attached):
    paths = [str(staged_upload)] if attached else []
    prompt = f"inspect @{staged_upload}"
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({"session_id": SID, "profile": "default",
                                    "prompt": prompt, "upload_paths": paths})
    queued = turns.get_queued_turns(SID)[0]
    live = turns.get_turn(queued["id"])
    real_create = turn_recorder.TurnRecorder.create_for_run
    with _pump_env(_pump([{"type": "done", "metadata": {}}])):
        with patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create):
            assert await session_runtime.SessionRuntime(live).execute() == "completed"
    sent = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert len(sent) == 1
    assert sent[0]["payload"].get("upload_paths", []) == paths
    session_events.append_events(SID, [_asst_ev("answer", uuid="answer-id")])
    _patch_checkpoints(monkeypatch, [_cp("user-id", prompt)])
    result = await execute_rewind_and_wait(SID, commands.RewindRequestV2(
        target_uuid="user-id" if target_role == "user" else "answer-id",
        target_role=target_role, include_response=include_response,
    ))
    assert result.success, result.error
    assert result.restored_upload_paths == (paths if target_role == "user" and not include_response else [])


async def test_steered_message_records_upload_paths(staged_upload):
    recorder = await turn_recorder.TurnRecorder.maybe_create(SID)
    try:
        await recorder.start("opening")
        assert await recorder.record_steered_user_message(
            f"inspect @{staged_upload}", upload_paths=[str(staged_upload)],
        )
        users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
        assert users[-1]["payload"]["upload_paths"] == [str(staged_upload)]
    finally:
        await recorder.close()


@pytest.mark.parametrize("compact_first", [False, True])
@pytest.mark.parametrize("bad_kind", ["outside", "traversal", "symlink", "other-session"])
async def test_intake_refuses_untrusted_attachment_paths(staged_upload, tmp_path, compact_first, bad_kind):
    from app.core.upload_transfer import session_upload_dir
    outside = tmp_path / "private.txt"
    outside.write_text("not an upload")
    if bad_kind == "outside":
        bad = outside
    elif bad_kind == "traversal":
        bad = staged_upload.parent / ".." / "private.txt"
    elif bad_kind == "symlink":
        bad = staged_upload.parent / "link.txt"
        bad.symlink_to(outside)
    else:
        bad = session_upload_dir("another-chat") / "photo.png"
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({"session_id": SID, "profile": "default",
                                    "prompt": "inspect this", "upload_paths": [str(staged_upload), str(bad)]},
                                   compact_first=compact_first)
    assert turns.get_queued_turns(SID) == []
    assert any("upload area" in call.args[0].get("message", "") for call in handler.send_json.call_args_list)


async def test_steered_intake_carries_attachment_provenance(staged_upload, monkeypatch):
    opening = turns.enqueue_turn(SID, "user", prompt="opening")
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({"session_id": SID, "profile": "default",
                                    "prompt": f"inspect @{staged_upload}", "upload_paths": [str(staged_upload)]})
    queued = turns.get_queued_turns(SID)[1]
    events = [{"type": "steer_delivered", "steers": [{"id": queued["id"]}]},
              {"type": "done", "metadata": {}}]
    real_create = turn_recorder.TurnRecorder.create_for_run
    with _pump_env(_pump(events)):
        with patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create):
            assert await session_runtime.SessionRuntime(opening).execute() == "completed"
    users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert users[-1]["payload"]["upload_paths"] == [str(staged_upload)]
    assert users[-1]["payload"]["checkpoint_backed"] is False
    assert database.get_session_messages(SID)[-1]["metadata"]["upload_paths"] == [str(staged_upload)]


async def test_remote_materialization_records_the_local_paths(staged_upload, rewind_env, tmp_path, monkeypatch):
    from contextlib import ExitStack
    from app.core import query_engine, upload_transfer
    from tests.core.query_engine_flow_harness import engine_patches

    paths = [str(staged_upload)]
    blob_ids = upload_transfer.lift_paths_to_blobs(paths, {"path": str(staged_upload.parent)}, SID)
    prompt = f"inspect @{staged_upload}"
    queued = turns.enqueue_turn(SID, "user", prompt=prompt, profile_id="default", payload={
        "upload_paths": paths, "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    destination = tmp_path / "remote-checkout"
    destination.mkdir()
    local_path = destination / "uploads" / staged_upload.name
    real_stream = query_engine._stream_to_websocket_impl
    real_create = turn_recorder.TurnRecorder.create_for_run
    with _pump_env(None), ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(destination), session_row=database.get_session(SID))
        stack.enter_context(patch.object(query_engine, "_stream_to_websocket_impl", real_stream))
        stack.enter_context(patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create))
        assert await session_runtime.SessionRuntime(queued).execute() == "completed"
        assert str(local_path) in str(bounds.adapter.send_query.call_args)
    assert local_path.read_bytes() == b"image"
    users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert len(users) == 1
    assert users[0]["payload"]["content"] == f"inspect @{local_path}"
    assert users[0]["payload"]["upload_paths"] == [str(local_path)]
    persisted = database.get_session_messages(SID)
    assert persisted[-1]["metadata"]["upload_paths"] == [str(local_path)]
    _patch_checkpoints(monkeypatch, [_cp("remote-user", f"inspect @{local_path}")])
    result = await execute_rewind_and_wait(SID, commands.RewindRequestV2(target_uuid="remote-user", include_response=False))
    assert result.success, result.error
    assert result.restored_upload_paths == [str(local_path)]


def test_project_upload_validation_uses_the_session_checkout(mock_db, tmp_path):
    from app.core.upload_transfer import validate_upload_paths, UploadLiftError
    project = tmp_path / "project"
    (project / "uploads").mkdir(parents=True)
    database.create_project("upload-project", "Uploads", None, str(project))
    row = database.create_session("project-chat", "default", project_id="upload-project")
    upload = project / "uploads" / "file.txt"
    upload.write_bytes(b"file")
    validate_upload_paths([str(upload)], row)
    outside = project / "not-an-upload.txt"
    outside.write_bytes(b"file")
    with pytest.raises(UploadLiftError, match="upload area"):
        validate_upload_paths([str(outside)], row)
