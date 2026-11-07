"""Producing-path regressions from the sent-attachment rewind review."""
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.api import sessions_worktree as swt
from app.api.sessions_helpers import CreateWorktreeSessionRequest
from app.core import checkout_manager, project_resolver, query_engine, session_runtime, turn_recorder, upload_transfer
from app.core.config import settings
from app.db import database, session_events, turns
from tests.api.test_chat_ws_v2_turns import _Patches, _make_handler
from tests.api.test_sent_upload_rewind import staged_upload
from tests.api.test_worktree_remote_target import project, _Patches as RemotePatches, _give_peer_a_base_checkout, PEER
from tests.core.query_engine_flow_harness import engine_patches
from tests.core.test_rewind_resolution import SID, rewind_env
from tests.core.test_steering_delivery_flows import _pump_env


async def local_branch(tmp_path, monkeypatch, *, prompt):
    base = tmp_path / "review-project"
    (base / "uploads").mkdir(parents=True)
    upload = base / "uploads" / "proof.txt"
    upload.write_text("proof")
    checkout = tmp_path / "branch"
    checkout.mkdir()
    database.create_project("review-project", "Review", None, str(base))
    pv2 = project_resolver.resolve_project_v2({"project_id": "review-project"})
    monkeypatch.setattr(checkout_manager, "require_worktree_project", lambda *_: None)
    monkeypatch.setattr(checkout_manager, "local_branch_exists", lambda *_: False)
    monkeypatch.setattr(checkout_manager, "ensure_checkout", lambda *a, **k: str(checkout))
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda *_: str(checkout))
    monkeypatch.setattr(swt, "_broadcast_session_created", lambda *_: None)
    body = CreateWorktreeSessionRequest(project_id="review-project", branch_name="new-review", create_new_branch=True,
        profile_id="default", pending_upload_paths=[str(upload)], prompt=f"inspect @{upload}" if prompt else None)
    result = await swt._create_v2_branch_session(body, database.get_project("review-project"), None, pv2=pv2)
    return result, upload, checkout / "uploads" / upload.name


@pytest.mark.parametrize("auto_prompt", [False, True])
async def test_local_create_and_send_keeps_the_migrated_attachment(staged_upload, tmp_path, monkeypatch, auto_prompt):
    result, source, migrated = await local_branch(tmp_path, monkeypatch, prompt=auto_prompt)
    assert migrated.read_text() == "proof"
    if not auto_prompt:
        with _Patches():
            handler = _make_handler()
            await handler.handle_query({"session_id": result.session_id, "profile": "default",
                "prompt": f"inspect @{source}", "upload_paths": [str(source)]})
    queued = turns.get_queued_turns(result.session_id)
    assert queued, "the real create-and-send flow must accept the staged upload"
    expected = migrated if auto_prompt else source
    assert queued[-1]["payload"]["upload_paths"] == [str(expected)]
    assert queued[-1]["prompt"] == f"inspect @{expected}"


async def test_remote_branch_creator_carries_paths_with_its_blobs(project):
    _give_peer_a_base_checkout(project)
    source = project["path"] / "uploads" / "proof.txt"
    source.write_text("proof")
    body = CreateWorktreeSessionRequest(project_id="proj-1", branch_name="remote-review", profile_id="default",
        prompt=f"inspect @{source}", pending_upload_paths=[str(source)], target_instance_id=PEER)
    with RemotePatches():
        response = await swt._create_v2_branch_session(body, project["legacy"], None)
    turn = turns.get_queued_turns(response.session_id)[0]
    assert turn["payload"]["upload_paths"] == [str(source)]
    assert turn["payload"]["upload_blob_ids"]


@pytest.mark.parametrize("failure", ["credential", "materialize", "cancel"])
async def test_preparation_failure_keeps_a_restorable_unsent_message(staged_upload, tmp_path, failure, monkeypatch):
    from app.api import commands
    from app.core import credential_gate, config
    prompt = f"inspect @{staged_upload}"
    ids = upload_transfer.lift_paths_to_blobs([str(staged_upload)], {"path": str(staged_upload.parent)}, SID)
    turn = turns.enqueue_turn(SID, "user", prompt=prompt, profile_id="default", payload={
        "upload_paths": [str(staged_upload)], "upload_blob_ids": ids, "deferred_user_record": True})
    real_stream = query_engine._stream_to_websocket_impl
    real_create = turn_recorder.TurnRecorder.create_for_run
    destination = tmp_path / "destination"
    destination.mkdir()
    # The owner does not have the viewing worker's staging directory.
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", destination)
    with _pump_env(None), ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(destination), session_row=database.get_session(SID))
        stack.enter_context(patch.object(query_engine, "_stream_to_websocket_impl", real_stream))
        stack.enter_context(patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create))
        if failure == "credential":
            stack.enter_context(patch.object(credential_gate, "turn_refusal_async", new=AsyncMock(return_value="Setup required")))
        else:
            import asyncio
            error = asyncio.CancelledError() if failure == "cancel" else upload_transfer.UploadLiftError("disk unavailable")
            stack.enter_context(patch.object(upload_transfer, "materialize_blobs_for_turn", side_effect=error))
        assert await session_runtime.SessionRuntime(turn).execute() == "aborted"
        bounds.adapter.send_query.assert_not_awaited()
    users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert len(users) == 1
    assert users[0]["payload"]["not_sent"] is True
    assert users[0]["payload"]["upload_paths"] == [str(staged_upload)]
    assert users[0]["payload"]["upload_blob_ids"] == ids
    assert any(row["role"] == "user" and (row.get("metadata") or {}).get("not_sent")
               for row in database.get_session_messages(SID))
    restored = commands._restore_unsent_message(SID, users[0]["id"])
    assert restored.success, restored.error
    with _Patches():
        handler = _make_handler()
        await handler.handle_query({"session_id": SID, "profile": "default", "prompt": restored.restored_prompt,
            "upload_paths": restored.restored_upload_paths})
    retry = turns.get_queued_turns(SID)[0]
    assert retry["payload"]["upload_references"]
    with _pump_env(None), ExitStack() as stack:
        bounds = engine_patches(stack, cwd=str(destination), session_row=database.get_session(SID))
        stack.enter_context(patch.object(query_engine, "_stream_to_websocket_impl", real_stream))
        stack.enter_context(patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create))
        assert await session_runtime.SessionRuntime(retry).execute() == "completed"
        bounds.adapter.send_query.assert_awaited_once()
    sent = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"][-1]
    assert sent["payload"]["upload_paths"] == [str(destination / "uploads" / staged_upload.name)]
    assert (destination / "uploads" / staged_upload.name).read_bytes() == b"image"


async def test_remote_restored_path_uses_an_owner_event_reference(staged_upload, monkeypatch):
    from app.core import placement
    from types import SimpleNamespace
    owner = "other-owner"
    remote_path = r"C:\Owner\checkout\uploads\photo.png"
    database.adopt_session(SID, owner)
    session_events.append_events(SID, [{"id": "sent-owner-upload", "type": "message.user", "payload": {
        "content": f"inspect @{remote_path}", "upload_paths": [remote_path], "upload_owner_id": owner}}])
    monkeypatch.setattr(placement, "resolve_placement", lambda *_a, **_k: SimpleNamespace(kind="runner", runner_id=owner, reason="test"))
    with _Patches(), patch.object(turns, "notify_turns_changed", create=True):
        handler = _make_handler()
        handler._network_owner = AsyncMock(return_value="peer-device")
        handler._may_send_elsewhere = AsyncMock(return_value=True)
        with patch("app.network.control.submit_turn", new=AsyncMock(return_value={"id": "forwarded"})) as forward:
            await handler.handle_query({"session_id": SID, "profile": "default", "prompt": f"inspect @{remote_path}", "upload_paths": [remote_path]})
    forward.assert_awaited_once()
    payload = forward.call_args.kwargs["payload"]
    assert payload["upload_references"] == [{"owner_id": owner, "event_id": "sent-owner-upload", "index": 0}]
    assert not payload.get("upload_blob_ids")


def test_owner_resolves_only_its_own_session_attachment_references(staged_upload):
    event = {"id": "owner-attachment", "type": "message.user", "payload": {"content": "photo", "upload_paths": [str(staged_upload)], "upload_owner_id": settings.effective_instance_id}}
    session_events.append_events(SID, [event])
    ref = {"owner_id": settings.effective_instance_id, "event_id": event["id"], "index": 0}
    payload = {"upload_paths": [str(staged_upload)], "upload_references": [ref]}
    resolved = upload_transfer.resolve_upload_references(payload, database.get_session(SID))
    assert resolved["upload_paths"] == [str(staged_upload)]
    for bad in [{**ref, "owner_id": "wrong"}, {**ref, "event_id": "not-recorded"}, {**ref, "index": 1}]:
        with pytest.raises(upload_transfer.UploadLiftError):
            upload_transfer.resolve_upload_references({**payload, "upload_references": [bad]}, database.get_session(SID))


async def test_owner_materialized_attachment_rewinds_and_resends_through_a_viewer(staged_upload, tmp_path, monkeypatch, rewind_env):
    from types import SimpleNamespace
    from app.api import commands
    from app.core import config, placement
    from app.core.session_modes import chat_uploads_dir
    from tests.core.test_rewind_resolution import _cp, _patch_checkpoints
    from tests.rewind_helpers import execute_rewind_and_wait

    viewer = settings.effective_instance_id
    owner = "attachment-owner"
    database.create_project("transport-project", "Transport", None, str(staged_upload.parent.parent))
    database.set_session_project(SID, "transport-project")
    database.register_install(owner, "owner", None, "local", "1", {}, approve_new=True)
    database.adopt_session(SID, owner)
    monkeypatch.setattr(placement, "resolve_placement", lambda *_a, **_k: SimpleNamespace(kind="runner", runner_id=owner, reason="test"))

    async def relay(_device, session_id, **body):
        from app.network import control, git as network_git
        parts = await network_git.export_uploads((body.get("payload") or {}).get("upload_blob_ids") or [])
        with patch.object(settings, "instance_id", owner), patch.object(control, "_require_owned", return_value=database.get_session(SID)):
            return await control._submit_forwarded_turn({**body, "session_id": session_id, "uploads": parts}, "viewer-device", None)

    async def send(paths, prompt):
        with _Patches():
            handler = _make_handler()
            handler._network_owner = AsyncMock(return_value="peer-device")
            handler._may_send_elsewhere = AsyncMock(return_value=True)
            with patch("app.network.control.submit_turn", new=AsyncMock(side_effect=relay)) as forward:
                await handler.handle_query({"session_id": SID, "profile": "default", "project": "transport-project", "prompt": prompt, "upload_paths": paths})
            assert forward.await_count == 1, handler.send_json.call_args_list
        return turns.get_queued_turns(SID)[0]

    first = await send([str(staged_upload)], f"inspect @{staged_upload}")
    assert first["payload"]["upload_blob_ids"]
    monkeypatch.setattr(settings, "instance_id", owner)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "owner-workspace")
    destination = chat_uploads_dir()
    destination.mkdir(parents=True, exist_ok=True)
    real_stream = query_engine._stream_to_websocket_impl
    real_create = turn_recorder.TurnRecorder.create_for_run

    async def run(turn):
        with _pump_env(None), ExitStack() as stack:
            engine_patches(stack, cwd=str(destination), session_row=database.get_session(SID),
                project_row=database.get_project("transport-project"))
            stack.enter_context(patch.object(query_engine, "_stream_to_websocket_impl", real_stream))
            stack.enter_context(patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create))
            outcome = await session_runtime.SessionRuntime(turn).execute()
            assert outcome == "completed", (settings.effective_instance_id, owner, turns.get_turn(turn["id"]))

    await run(first)
    materialized = chat_uploads_dir() / "uploads" / staged_upload.name
    assert materialized.read_bytes() == b"image"
    user = next(row for row in session_events.get_events(SID) if row["event_type"] == "message.user")
    assert user["payload"]["upload_owner_id"] == owner
    _patch_checkpoints(monkeypatch, [_cp("first-upload", f"inspect @{materialized}")])
    restored = await execute_rewind_and_wait(SID, commands.RewindRequestV2(target_uuid="first-upload", include_response=False))
    assert restored.success, restored.error
    assert restored.restored_upload_paths == [str(materialized)]

    monkeypatch.setattr(settings, "instance_id", viewer)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    # Checkpoint rewinds restore text from the clicked transcript message in
    # the client; the endpoint supplies provenance, not restored_prompt.
    second = await send(restored.restored_upload_paths, user["payload"]["content"])
    assert second["payload"]["upload_references"] == [{"owner_id": owner, "event_id": user["id"], "index": 0}]
    assert not second["payload"].get("upload_blob_ids")
    monkeypatch.setattr(settings, "instance_id", owner)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "owner-workspace")
    await run(second)
    users = [row for row in session_events.get_events(SID) if row["event_type"] == "message.user"]
    assert len(users) == 2
    assert users[-1]["payload"]["upload_paths"] == [str(materialized)]
    assert users[-1]["payload"]["content"] == f"inspect @{materialized}"


async def test_voice_text_only_paths_keep_the_preexisting_send_behavior(staged_upload):
    """No inferred attachment metadata or new path gate for the voice bridge."""
    prompt = r"read @C:\call-worker\uploads\voice-note.txt"
    turn = turns.enqueue_turn(SID, "queue", prompt=prompt, profile_id="default", payload={"voice_delegation": True})
    real_create = turn_recorder.TurnRecorder.create_for_run
    from tests.core.test_steering_delivery_flows import _pump
    with _pump_env(_pump([{"type": "done", "metadata": {}}])):
        with patch.object(turn_recorder.TurnRecorder, "create_for_run", real_create), patch.object(
            upload_transfer, "validate_upload_paths", side_effect=AssertionError("text-only sends must not be reinterpreted")
        ):
            assert await session_runtime.SessionRuntime(turn).execute() == "completed"
    user = next(row for row in session_events.get_events(SID) if row["event_type"] == "message.user")
    assert user["payload"]["content"] == prompt
    assert not user["payload"].get("upload_paths")
