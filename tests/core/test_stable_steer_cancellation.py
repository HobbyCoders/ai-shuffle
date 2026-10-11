"""WS -> durable queue -> real adapter/delegated PostToolUse cancellation."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import session_runtime
from app.db import turns as turns_db
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import HookCallbackRequest
from fluid.store.base import MemoryStore
from tests.api.test_chat_ws_v2_turns import _make_session, _Patches
from tests.core.test_steering_delivery_flows import (
    _clean_module_state, _harness, _live_runtime, _pooled, _start_live,
    _ws_handler, _removal_outcomes,
)


def _runtime(sid, source):
    live = _start_live(sid, profile_id="default")
    if source == "unpark":
        assert turns_db.park_turn(live["id"], ["task-1"]) is not None
        wake = turns_db.enqueue_turn(sid, "unpark", prompt="task settled", profile_id="default")
        resumed = turns_db.start_turn(
            wake["id"], can_resume=session_runtime.SessionRuntime._can_resume_parked,
        )
        assert resumed["id"] == live["id"]
        assert resumed["payload"]["continued_turn"] is True
        assert turns_db.get_turn(wake["id"])["parent_turn_id"] == live["id"]
        live = resumed
    return _live_runtime(sid, live)


def _loop():
    from fluid.hooks import HookBus

    loop = DelegatedLoop(store=MemoryStore(), model="m", hooks=HookBus())
    loop._session = MagicMock(session_id="engine-session")
    loop._turn_task = MagicMock()
    loop._turn_task.done.return_value = False
    loop._proc = MagicMock(returncode=None)
    loop._active_turn_id = "engine-turn"
    loop._peer_admission_open = True
    loop.cli_hooks_registered = True
    loop._run_cli_hook = AsyncMock(side_effect=lambda *args: {})
    loop._write_frame = AsyncMock()
    loop._emit = AsyncMock()
    return loop


async def _boundary(loop):
    request = HookCallbackRequest(
        cli_request_id="hook-1", callback_id="fluid_PostToolUse",
        input={"tool_use_id": "tool-1"}, tool_use_id="tool-1",
    )
    mapper = MagicMock(turn_id="engine-turn", main_tool_use_ids={"tool-1"})
    await loop._answer_hook_request(request, mapper=mapper)
    return loop._write_frame.call_args.args[0]["response"]["response"]


@pytest.mark.parametrize("source", ["user", "unpark"])
@pytest.mark.parametrize("identity", ["row", "unmapped", "client"])
async def test_cancel_after_15_seconds_reaches_delegated_engine(mock_db, source, identity):
    sid = _make_session("stable-cancel")
    runtime = _runtime(sid, source)
    loop = _loop()
    frames = []
    handler = _ws_handler(frames)
    with _Patches(), _pooled(_harness(loop)):
        client_id = "client-" + "a" * 32
        await handler.handle_query({
            "prompt": "withdraw this", "session_id": sid, "profile": "default",
            **({"client_message_id": client_id} if identity == "client" else {}),
        })
        rows = turns_db.get_queued_turns(sid)
        assert len(rows) == 1
        row = rows[0]
        assert len(loop._pending_peers) == 1
        assert loop._pending_peers[0].id == (client_id if identity == "client" else row["id"])
        if identity == "client":
            assert row["command_id"] == client_id
        with patch.object(loop, "cancel_steer", wraps=loop.cancel_steer) as cancel, \
                patch("time.time", return_value=time.time() + 16):
            await handler.handle_remove_queued_message({
                "session_id": sid,
                "message_id": row["id"] if identity == "row" else "queued-local-unmapped",
                **({"client_message_id": client_id} if identity == "client" else {}),
            })
        output = await _boundary(loop)
    if identity == "unmapped":
        # Baseline reproduction: an unmapped local id never recalls the actual
        # queued steer, yet the old frontend already hid its chip.
        assert cancel.call_count == 0
        assert _removal_outcomes(frames) == [("queued-local-unmapped", "not_found")]
        assert output["hookSpecificOutput"]["additionalContext"].count("withdraw this") == 1
        assert loop._emit.call_count == 1
        assert row["id"] not in runtime._steer_withdrawn
    else:
        assert cancel.call_count == 1
        assert output == {}
        assert loop._emit.call_count == 0
        assert row["id"] in runtime._steer_withdrawn
        assert turns_db.get_turn(row["id"])["state"] == "aborted"
        verdicts = [f for f in frames if f.get("type") == "queued_message_removal"]
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "withdrawn"
        if identity == "client":
            assert verdicts[0]["client_message_id"] == client_id


@pytest.mark.parametrize("source", ["user", "unpark"])
@pytest.mark.parametrize("delivery_first", [False, True])
async def test_cancel_racing_post_tool_use(mock_db, source, delivery_first, caplog):
    import app.api.chat_ws_handler as cwh
    from app.core import turn_manager

    caplog.set_level("INFO")
    sid = _make_session("boundary-race")
    runtime = _runtime(sid, source)
    loop = _loop()
    frames = []
    handler = _ws_handler(frames)
    client_id = "client-" + "b" * 32
    passes = []
    with _Patches(), _pooled(_harness(loop)):
        await handler.handle_query({"session_id": sid, "profile": "default",
                                    "prompt": "private words", "client_message_id": client_id})
        rows = turns_db.get_queued_turns(sid)
        assert len(rows) == 1
        row = rows[0]
        assert [s.id for s in loop._pending_peers] == [client_id]
        real_sync = cwh.run_sync

        async def racing_sync(fn, *args, **kwargs):
            if fn is turn_manager.remove_queued:
                passes.append(await session_runtime.try_steer_pending(sid))
            return await real_sync(fn, *args, **kwargs)

        with patch.object(cwh, "run_sync", new=racing_sync):
            if delivery_first:
                # Pause the actual hook write AFTER the synchronous boundary
                # took its batch. Cancellation in this gap must report too late.
                entered, release = asyncio.Event(), asyncio.Event()

                async def write(frame):
                    entered.set()
                    await release.wait()

                loop._write_frame.side_effect = write
                boundary = asyncio.create_task(_boundary(loop))
                await entered.wait()
            await handler.handle_remove_queued_message({
                "session_id": sid, "client_message_id": client_id,
            })
            if delivery_first:
                release.set()
                output = await boundary
            else:
                output = await _boundary(loop)
            assert await session_runtime.try_steer_pending(sid) == 0

        verdicts = [f for f in frames if f.get("type") == "queued_message_removal"]
        assert len(verdicts) == 1
        assert verdicts[0]["client_message_id"] == client_id
        if delivery_first:
            assert verdicts[0]["verdict"] == "already_delivered"
            assert output["hookSpecificOutput"]["additionalContext"].count("private words") == 1
            assert loop._emit.call_count == 1
            receipt = loop._emit.call_args.args[0]
            assert receipt.steers == [{"id": client_id, "text": "private words", "tool_use_id": "tool-1"}]
            recorder = MagicMock(record_steered_user_message=AsyncMock(return_value=True))
            await runtime._on_steer_delivered(recorder, {"steers": receipt.steers})
            assert recorder.record_steered_user_message.call_count == 1
            assert recorder.record_steered_user_message.call_args.kwargs["command_id"] == client_id
            assert turns_db.get_turn(row["id"])["stop_reason"] == "steered"
            assert await _boundary(loop) == {}  # the next hook cannot deliver it twice
            assert loop._emit.call_count == 1
            assert passes == []
        else:
            assert verdicts[0]["verdict"] == "withdrawn"
            assert output == {}
            assert loop._emit.call_count == 0
            assert passes == [0]  # #2015's re-submission gap stays closed
            assert row["id"] in runtime._steer_withdrawn
            assert turns_db.get_turn(row["id"])["state"] == "aborted"
        # Repeating the cancel never repeats delivery or changes the verdict.
        await handler.handle_remove_queued_message({"session_id": sid, "client_message_id": client_id})
        replies = [f for f in frames if f.get("type") == "queued_message_removal"]
        assert len(replies) == 2
        assert replies[0]["verdict"] == replies[1]["verdict"]
    trace = [r.getMessage() for r in caplog.records if r.getMessage().startswith("steer ")]
    assert any("enqueue" in line and client_id in line for line in trace)
    assert any("cancel-request" in line and client_id in line for line in trace)
    assert any("cancel-verdict" in line and client_id in line for line in trace)
    if delivery_first:
        assert any("delivery" in line and client_id in line for line in trace)
    assert all("private words" not in line and "at=" in line for line in trace)


async def test_stable_cancel_is_session_scoped_and_legacy_commands_still_work(mock_db):
    sid = _make_session("scope-a")
    other = _make_session("scope-b")
    client_id = "client-" + "c" * 32
    first = turns_db.enqueue_turn(sid, "user", prompt="same", command_id=client_id)
    second = turns_db.enqueue_turn(other, "user", prompt="same", command_id=client_id)
    legacy = turns_db.enqueue_turn(sid, "user", prompt="old", command_id="req-old")
    frames = []
    handler = _ws_handler(frames)
    with _Patches():
        await handler.handle_remove_queued_message({"session_id": sid, "client_message_id": client_id})
        await handler.handle_remove_queued_message({"session_id": sid, "message_id": legacy["id"]})
    assert turns_db.get_turn(first["id"])["state"] == "aborted"
    assert turns_db.get_turn(second["id"])["state"] == "queued"
    assert turns_db.get_turn(legacy["id"])["state"] == "aborted"
    replies = [f for f in frames if f.get("type") == "queued_message_removal"]
    assert len(replies) == 2
    assert [f["verdict"] for f in replies] == ["withdrawn", "withdrawn"]
    assert [f["client_message_id"] for f in replies] == [client_id, None]


@pytest.mark.parametrize("first_by_client", [True, False])
async def test_held_cancellation_retries_survive_changing_storage(mock_db, first_by_client):
    from app.core import session_handoff
    from app.db import session_moves

    sid = _make_session("held-cancel")
    client_id = "client-" + "d" * 32
    session_moves.save(sid, target_id="other", label="other", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    with patch.object(session_handoff, "holds", return_value=True):
        held_id = session_handoff.hold(sid, "user", {
            "prompt": "held private text", "command_id": client_id,
        })
    assert held_id.startswith("held-")
    frames = []
    handler = _ws_handler(frames)
    def view(session_id):
        return (session_moves.get(session_id) or {}).get("held", [])

    with _Patches(), patch.object(session_handoff, "held_view", side_effect=view):
        await handler.handle_remove_queued_message({"session_id": sid, **(
            {"client_message_id": client_id} if first_by_client else {"message_id": held_id}
        )})
        assert session_moves.get(sid)["held"] == []
        tombstone = session_moves.held_withdrawal(sid, held_id=held_id)
        assert tombstone == {"id": held_id, "command_id": client_id}
        assert turns_db.get_turn(held_id) is None  # never manufacture a run/permission binding
        from app.db import session_events
        events = session_events.get_events(sid)
        receipts = [e for e in events if e["event_type"] == "queued_message.withdrawn"]
        assert len(receipts) == 1
        assert receipts[0]["payload"] == {"message_id": held_id}  # no content retained
        from app.contract.events import QueuedMessageWithdrawn
        receipt = receipts[0]
        typed = QueuedMessageWithdrawn.model_validate({
            **receipt, **receipt["payload"], "type": receipt["event_type"],
        })
        assert typed.message_id == held_id and typed.command_id == client_id
        assert session_moves.held_withdrawal("another-session", held_id=held_id) is None
        assert session_moves.held_withdrawal("another-session", command_id=client_id) is None
        # Removing the whole move (or restarting handoff state) cannot erase
        # the withdrawal receipt. Both client-id and held-id retries agree.
        assert session_moves.take(sid)["held"] == []
        assert session_moves.get(sid) is None
        await handler.handle_remove_queued_message({"session_id": sid, "client_message_id": client_id})
        await handler.handle_remove_queued_message({"session_id": sid, "message_id": held_id})
    replies = [f for f in frames if f.get("type") == "queued_message_removal"]
    assert len(replies) == 3
    assert [f["client_message_id"] for f in replies] == [client_id] * 3
    assert [f["verdict"] for f in replies] == ["withdrawn"] * 3
    assert turns_db.get_queued_turns(sid) == []


def test_held_withdrawal_receipt_and_removal_are_atomic(mock_db):
    from app.db import session_moves

    sid = _make_session("atomic-held")
    session_moves.save(sid, target_id="other", label="other", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    entry = {"id": "held-atomic", "source": "user", "prompt": "keep until committed",
             "command_id": "client-" + "f" * 32}
    assert session_moves.append_held(sid, entry)
    with patch("app.db.session_events.append_events", side_effect=RuntimeError("cannot record verdict")):
        with pytest.raises(RuntimeError, match="cannot record verdict"):
            session_moves.remove_held(sid, entry["id"])
    assert session_moves.get(sid)["held"] == [entry]
    assert session_moves.held_withdrawal(sid, held_id=entry["id"]) is None
    assert session_moves.remove_held(sid, entry["id"]) is True
    assert session_moves.get(sid)["held"] == []
    assert session_moves.held_withdrawal(sid, held_id=entry["id"])["command_id"] == entry["command_id"]


async def test_missing_held_row_is_not_proof_of_delivery(mock_db):
    sid = _make_session("missing-held")
    frames = []
    with _Patches():
        await _ws_handler(frames).handle_remove_queued_message({
            "session_id": sid, "message_id": "held-unknown",
        })
    replies = [f for f in frames if f.get("type") == "queued_message_removal"]
    assert len(replies) == 1
    assert replies[0]["outcome"] == "not_found"


async def test_cancel_while_parked_before_unpark_never_delivers(mock_db):
    sid = _make_session("cancel-parked")
    live = _start_live(sid, profile_id="default")
    parked = turns_db.park_turn(live["id"], ["task-1"])
    assert parked["state"] == "parked"
    runtime = _live_runtime(sid, parked)
    loop = _loop()
    frames = []
    client_id = "client-" + "e" * 32
    with _Patches(), _pooled(_harness(loop)):
        handler = _ws_handler(frames)
        await handler.handle_query({"session_id": sid, "profile": "default",
                                    "prompt": "withdraw before wake", "client_message_id": client_id})
        rows = turns_db.get_queued_turns(sid)
        assert len(rows) == 1
        assert turns_db.get_turn(live["id"])["state"] == "parked"
        await handler.handle_remove_queued_message({"session_id": sid, "client_message_id": client_id})
        assert turns_db.get_turn(live["id"])["state"] == "parked"
        assert turns_db.get_turn(rows[0]["id"])["state"] == "aborted"
        wake = turns_db.enqueue_turn(sid, "unpark", prompt="task settled", profile_id="default")
        resumed = turns_db.start_turn(wake["id"], can_resume=runtime._can_resume_parked)
        assert resumed["id"] == live["id"]
        assert resumed["payload"]["continued_turn"] is True
        runtime._turn = resumed
        assert await session_runtime.try_steer_pending(sid) == 0
        assert await _boundary(loop) == {}
        assert loop._emit.call_count == 0
        assert len(loop._pending_peers) == 0
    replies = [f for f in frames if f.get("type") == "queued_message_removal"]
    assert len(replies) == 1
    assert replies[0]["verdict"] == "withdrawn"
