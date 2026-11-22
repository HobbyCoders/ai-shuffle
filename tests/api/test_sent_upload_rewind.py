"""Attachment provenance survives actual intake, recording and sent-message rewind."""
import asyncio
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
    expected_refs = [{"owner_id": sent[0]["payload"]["upload_owner_id"], "event_id": sent[0]["id"], "index": 0, "path": paths[0]}] if paths and target_role == "user" and not include_response else []
    assert result.restored_upload_references == expected_refs


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


def _steer_into_live_turn(queued_id, cwd):
    """A live turn whose pump asks to steer, then receipts ``queued_id``."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from tests.core.test_steering_delivery_flows import _Adapter

    adapter = _Adapter()
    pool = MagicMock(name="session_pool")
    pool.peek.return_value = SimpleNamespace(
        adapter=adapter, is_streaming=True, connect_options=SimpleNamespace(cwd=str(cwd)))
    seen = {}

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "steer_delivered", "steers": [{"id": queued_id}]}
        yield {"type": "done", "metadata": {}}

    return adapter, pool, seen, _impl


async def test_an_attachment_sent_through_another_worker_steers_into_the_live_turn(staged_upload, tmp_path):
    """A file sent from another worker's page arrives as a blob. It joins the
    running turn like a local file: written into the turn's folder and named
    by that local path, both in the steer and in the recorded message."""
    from app.core import upload_transfer

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    destination = tmp_path / "remote-checkout"
    destination.mkdir()
    local = destination / "uploads" / staged_upload.name
    turns.enqueue_turn(SID, "user", prompt="opening")  # runs first, as the live turn
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    adapter, pool, seen, pump = _steer_into_live_turn(queued["id"], destination)
    opening = turns.get_queued_turns(SID)[0]
    real_create = turn_recorder.TurnRecorder.create_for_run
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(pump):
        with patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create):
            assert await session_runtime.SessionRuntime(opening).execute() == "completed"

    assert seen["steered"] == 1
    assert adapter.calls == [(f"look @{local}", queued["id"])]
    assert local.read_bytes() == b"image"
    users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert users[-1]["payload"]["content"] == f"look @{local}"
    assert users[-1]["payload"]["upload_paths"] == [str(local)]
    assert database.get_session_messages(SID)[-1]["metadata"]["upload_paths"] == [str(local)]
    assert turns.get_queued_turns(SID) == []
    # Delivered, so the transferred bytes are released and the copy recorded.
    from app.db import upload_blobs
    assert upload_blobs.get_blob_data(blob_ids[0]) is None
    assert upload_blobs.get_blob(blob_ids[0])["materialized_path"] == str(local)


async def test_a_refused_attachment_steer_leaves_its_own_turn_able_to_write_the_file(staged_upload, tmp_path):
    """The pooled client refuses the steer (an idle client still for another
    checkout, say). The file was written for it, but the transferred bytes
    stay, so the message's own turn can write it into the folder it runs in."""
    from app.core import upload_transfer
    from app.db import upload_blobs
    from tests.core.test_steering_delivery_flows import _Adapter

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    stale = tmp_path / "old-checkout"
    stale.mkdir()
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    _, pool, seen, _ = _steer_into_live_turn(queued["id"], stale)
    pool.peek.return_value.adapter = _Adapter(refuse=RuntimeError("no running turn"))

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "done", "metadata": {}}

    opening = turns.get_queued_turns(SID)[0]
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl):
        assert await session_runtime.SessionRuntime(opening).execute() == "completed"

    assert seen["steered"] == 0
    assert [r["id"] for r in turns.get_queued_turns(SID)] == [queued["id"]]
    assert upload_blobs.get_blob_data(blob_ids[0]) == b"image"
    current = tmp_path / "new-checkout"
    current.mkdir()
    mapping = upload_transfer.materialize_blobs_for_turn(blob_ids, str(current))
    assert (current / "uploads" / staged_upload.name).read_bytes() == b"image"
    assert mapping == {source: str(current / "uploads" / staged_upload.name)}


async def test_an_idle_pooled_client_gets_no_attachment_files(staged_upload, tmp_path):
    """While the next turn connects, the pooled client can still be the idle
    one from before, in another folder. Nothing is written for it, and the
    message keeps its bytes for its own turn."""
    from app.core import upload_transfer
    from app.db import upload_blobs

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    stale = tmp_path / "old-checkout"
    stale.mkdir()
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    adapter, pool, seen, _ = _steer_into_live_turn(queued["id"], stale)
    pool.peek.return_value.is_streaming = False

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "done", "metadata": {}}

    opening = turns.get_queued_turns(SID)[0]
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl):
        assert await session_runtime.SessionRuntime(opening).execute() == "completed"

    assert seen["steered"] == 0
    assert adapter.calls == []
    assert not (stale / "uploads").exists()
    assert [r["id"] for r in turns.get_queued_turns(SID)] == [queued["id"]]
    assert upload_blobs.get_blob_data(blob_ids[0]) == b"image"


async def test_a_delivered_attachment_that_is_requeued_keeps_its_bytes(staged_upload, tmp_path):
    """The turn receipts the attachment, then dies before the model answers:
    the message is re-queued, and its own run, in whatever folder it then
    runs, can still write the file."""
    from app.core import upload_transfer
    from app.db import upload_blobs

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    old = tmp_path / "old-checkout"
    old.mkdir()
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    adapter, pool, seen, _ = _steer_into_live_turn(queued["id"], old)

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "steer_delivered", "steers": [{"id": queued["id"]}]}
        yield {"type": "error", "message": "boom"}

    opening = turns.get_queued_turns(SID)[0]
    real_create = turn_recorder.TurnRecorder.create_for_run
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl):
        with patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create):
            await session_runtime.SessionRuntime(opening).execute()

    assert seen["steered"] == 1
    assert (old / "uploads" / staged_upload.name).read_bytes() == b"image"
    requeued = turns.get_queued_turns(SID)
    assert [r["payload"].get("requeued_from_turn_id") for r in requeued] == [queued["id"]]
    assert upload_blobs.get_blob_data(blob_ids[0]) == b"image"
    current = tmp_path / "new-checkout"
    current.mkdir()
    upload_transfer.materialize_blobs_for_turn(requeued[0]["payload"]["upload_blob_ids"], str(current))
    assert (current / "uploads" / staged_upload.name).read_bytes() == b"image"


async def test_a_turn_that_ends_while_attachments_are_written_does_not_get_them(staged_upload, tmp_path):
    """The turn's client goes away while the files are written: the row runs
    as its own turn, so nothing is handed to whatever client is pooled now."""
    from types import SimpleNamespace
    from app.core import upload_transfer
    from tests.core.test_steering_delivery_flows import _Adapter

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    adapter, pool, seen, _ = _steer_into_live_turn(queued["id"], tmp_path)
    replacement = _Adapter()
    real_materialize = upload_transfer.materialize_blobs_for_turn

    def _materialize_as_the_client_changes(ids, cwd, **kwargs):
        mapping = real_materialize(ids, cwd, **kwargs)
        pool.peek.return_value = SimpleNamespace(
            adapter=replacement, is_streaming=True, connect_options=SimpleNamespace(cwd=str(cwd)))
        return mapping

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "done", "metadata": {}}

    opening = turns.get_queued_turns(SID)[0]
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl), \
            patch.object(upload_transfer, "materialize_blobs_for_turn", _materialize_as_the_client_changes):
        assert await session_runtime.SessionRuntime(opening).execute() == "completed"
    assert seen["steered"] == 0
    assert adapter.calls == [] and replacement.calls == []
    assert [r["id"] for r in turns.get_queued_turns(SID)] == [queued["id"]]


async def test_a_restored_attachment_chip_steers_into_the_live_turn(staged_upload, tmp_path):
    """A chip brought back by a rewind names its file by the recorded message.
    Admitted by that identity, it joins the running turn instead of waiting."""
    from app.core.upload_transfer import recorded_upload_references

    recorder = await turn_recorder.TurnRecorder.maybe_create(SID)
    try:
        await recorder.start("earlier")
        assert await recorder.record_steered_user_message(
            f"inspect @{staged_upload}", upload_paths=[str(staged_upload)])
    finally:
        await recorder.close()
    sent = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"][-1]
    references = recorded_upload_references(SID, sent)
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"again @{staged_upload}", payload={
        "upload_paths": [str(staged_upload)], "upload_references": references,
        "deferred_user_record": True,
    })
    adapter, pool, seen, pump = _steer_into_live_turn(queued["id"], tmp_path)
    opening = turns.get_queued_turns(SID)[0]
    real_create = turn_recorder.TurnRecorder.create_for_run
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(pump):
        with patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create):
            assert await session_runtime.SessionRuntime(opening).execute() == "completed"

    assert seen["steered"] == 1
    assert adapter.calls == [(f"again @{staged_upload}", queued["id"])]
    users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert users[-1]["payload"]["upload_paths"] == [str(staged_upload)]
    assert turns.get_queued_turns(SID) == []


async def test_an_attachment_that_cannot_be_prepared_waits_for_its_own_turn(staged_upload, tmp_path):
    """A purged blob is refused at steer time too: nothing reaches the model,
    and the row stays queued for its own turn to report the failure."""
    queued_payload = {"upload_paths": [str(staged_upload)], "upload_blob_ids": ["blob-gone"],
                      "deferred_user_record": True}
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{staged_upload}", payload=queued_payload)
    adapter, pool, seen, _ = _steer_into_live_turn(queued["id"], tmp_path)

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "done", "metadata": {}}

    opening = turns.get_queued_turns(SID)[0]
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl):
        assert await session_runtime.SessionRuntime(opening).execute() == "completed"
    assert seen["steered"] == 0
    assert adapter.calls == []
    assert [row["id"] for row in turns.get_queued_turns(SID)] == [queued["id"]]


async def test_unsending_while_attachments_are_written_keeps_it_from_the_model(staged_upload, tmp_path):
    """Unsend lands while the files are still being written: the engine has
    nothing yet, so the verdict is not_submitted and the steer never goes out."""
    from app.core import upload_transfer

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    turns.enqueue_turn(SID, "user", prompt="opening")
    queued = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    adapter, pool, seen, _ = _steer_into_live_turn(queued["id"], tmp_path)
    real_materialize = upload_transfer.materialize_blobs_for_turn

    def _materialize_then_unsend(ids, cwd, **kwargs):
        mapping = real_materialize(ids, cwd, **kwargs)
        # The page's Unsend arrives on the loop while this thread still writes.
        verdict = asyncio.run_coroutine_threadsafe(
            session_runtime.try_cancel_steer(SID, queued["id"], withdraw=True), loop).result(5)
        seen["verdict"] = verdict
        return mapping

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        yield {"type": "done", "metadata": {}}

    loop = asyncio.get_running_loop()
    opening = turns.get_queued_turns(SID)[0]
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl), \
            patch.object(upload_transfer, "materialize_blobs_for_turn", _materialize_then_unsend):
        assert await session_runtime.SessionRuntime(opening).execute() == "completed"
    assert seen["verdict"] == "not_submitted"
    assert seen["steered"] == 0
    assert adapter.calls == []


async def test_a_message_sent_while_attachments_are_written_does_not_overtake_them(staged_upload, tmp_path):
    """A screenshot, then a quick follow-up sent while its file is still being
    written: the follow-up's own sweep waits, and the model gets them in order."""
    from app.core import upload_transfer

    source = str(staged_upload)
    blob_ids = upload_transfer.lift_paths_to_blobs([source], {"path": str(staged_upload.parent)}, SID)
    turns.enqueue_turn(SID, "user", prompt="opening")
    first = turns.enqueue_turn(SID, "user", prompt=f"look @{source}", payload={
        "upload_paths": [source], "upload_blob_ids": blob_ids, "deferred_user_record": True,
    })
    adapter, pool, seen, _ = _steer_into_live_turn(first["id"], tmp_path)
    real_materialize = upload_transfer.materialize_blobs_for_turn

    def _materialize_while_another_send_arrives(ids, cwd, **kwargs):
        seen["second"] = turns.enqueue_turn(SID, "user", prompt="and also this")
        # That send asks to steer while this thread still writes the file.
        seen["second_sweep"] = asyncio.run_coroutine_threadsafe(
            session_runtime.try_steer_pending(SID), loop)
        return real_materialize(ids, cwd, **kwargs)

    async def _impl(**kwargs):
        seen["steered"] = await session_runtime.try_steer_pending(SID)
        seen["steered"] += await asyncio.wrap_future(seen["second_sweep"])
        yield {"type": "steer_delivered", "steers": [{"id": first["id"]}, {"id": seen["second"]["id"]}]}
        yield {"type": "done", "metadata": {}}

    loop = asyncio.get_running_loop()
    opening = turns.get_queued_turns(SID)[0]
    real_create = turn_recorder.TurnRecorder.create_for_run
    with patch("app.core.session_pool.get_session_pool", return_value=pool), _pump_env(_impl), \
            patch.object(upload_transfer, "materialize_blobs_for_turn", _materialize_while_another_send_arrives), \
            patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create):
        assert await session_runtime.SessionRuntime(opening).execute() == "completed"
    assert seen["steered"] == 2
    assert [steer_id for _text, steer_id in adapter.calls] == [first["id"], seen["second"]["id"]]
    users = [row["payload"]["content"] for row in session_events.get_events(SID)
             if row["event_type"] == "message.user"]
    assert users[-1] == "and also this"


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
    # A newly-created explicit branch still receives the base-checkout staging
    # path from New Chat's follow-up WS send. Only that project's uploads count.
    checkout = tmp_path / "bound-branch"
    checkout.mkdir()
    with patch("app.core.project_resolver.resolve_existing_cwd", return_value=str(checkout)):
        validate_upload_paths([str(upload)], row)
    outside = project / "not-an-upload.txt"
    outside.write_bytes(b"file")
    with pytest.raises(UploadLiftError, match="upload area"):
        validate_upload_paths([str(outside)], row)


@pytest.mark.parametrize("mentioned", [True, False])
async def test_attachment_typed_into_a_call_is_recorded_and_its_rewind_is_refused(staged_upload, rewind_env, monkeypatch, mentioned):
    """Admission through recording, then a refused rewind; provider and agent stubbed."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core import voice_stream
    from app.core.voice_prompts import voice_turn_note

    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": SID}, {"model": "gpt-live-1", "voice": "marin"}, "secret", 5)
    call.upstream = AsyncMock()
    delegate = AsyncMock(return_value="Read.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    words = f"inspect @{staged_upload}" if mentioned else "inspect this"
    await call.receive_text(json.dumps({"type": "input_text", "id": "typed", "text": words, "upload_paths": [str(staged_upload)]}))
    await call.delegation_task
    sent = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert len(sent) == 1
    assert sent[0]["payload"]["content"] == words
    assert sent[0]["payload"]["upload_paths"] == [str(staged_upload)]

    # What the chat agent's transcript holds for that request.
    prompt = f"{delegate.await_args.args[1]}\n\n{voice_turn_note('')}"
    session_events.append_events(SID, [_asst_ev("answer", uuid="answer-id")])
    _patch_checkpoints(monkeypatch, [_cp("user-id", prompt)])
    before = session_events.get_events(SID)
    result = await execute_rewind_and_wait(SID, commands.RewindRequestV2(
        target_uuid="user-id", target_role="user", include_response=False))
    assert result.success is False
    assert "content guard" in (result.error or "")
    assert rewind_env.truncate_calls == []
    assert session_events.get_events(SID) == before


async def test_a_call_refuses_a_typed_attachment_a_chat_send_would_refuse(staged_upload, rewind_env, monkeypatch, tmp_path):
    """No stub on the check: the same validation as a chat send decides."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core import voice_stream

    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": SID}, {"model": "gpt-live-1", "voice": "marin"}, "secret", 5)
    call.upstream = AsyncMock()
    delegate = AsyncMock(return_value="Read.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    await call.receive_text(json.dumps({"type": "input_text", "id": "typed", "text": "inspect", "upload_paths": [str(outside)]}))
    sent = [c.args[0] for c in socket.send_json.await_args_list if c.args[0].get("type", "").startswith("text.")]
    assert [e["type"] for e in sent] == ["text.rejected"]
    delegate.assert_not_awaited()
    assert not [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
