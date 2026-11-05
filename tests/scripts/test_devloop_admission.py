"""Deterministic close/commit pause admission: completion or explicit refusal."""
import asyncio
import json
import threading

import pytest

from types import SimpleNamespace

from scripts.devloop import idle, observer


def gate(tmp_path):
    obj = observer.ActivityObserver.__new__(observer.ActivityObserver)
    obj.lock = threading.Lock()
    obj.pid, obj.identity = 123, "start"
    obj.active, obj.activity, obj.uncertain = 0, 0, False
    obj.gate_id, obj.gate_until, obj.gate_state, obj.gate_commit_id = None, 0, "open", None
    obj.gate_request = tmp_path / "activity.gate"
    return obj


def close(obj, now=10):
    obj.gate_request.write_text(json.dumps({"id": "attempt", "pid": 123,
        "identity": "start", "before": 0, "expires": now + 5}))
    obj.control_gate()


@pytest.mark.parametrize("arrival", ["before-close", "after-close-before-ack", "after-ack"])
def test_request_at_each_gate_boundary_finishes_or_is_refused(tmp_path, monkeypatch, arrival):
    wrapped = gate(tmp_path)
    monkeypatch.setattr(observer.time, "monotonic", lambda: 10)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        completed, sent = [], []

        async def app(scope, receive, send):
            entered.set()
            await release.wait()
            completed.append("finished")

        async def send(message):
            sent.append(message)

        wrapped.app = app
        scope = {"type": "http", "method": "POST", "path": "/work"}
        if arrival == "before-close":
            task = asyncio.create_task(wrapped(scope, None, send))
            await entered.wait()
            close(wrapped)
            assert wrapped.gate_until == 0  # Finite work or new activity aborts close.
            release.set()
            await task
            assert completed == ["finished"] and sent == []
        else:
            close(wrapped)
            assert wrapped.gate_until == 15
            # Publishing the acknowledgement changes nothing: closed under the
            # same lock already, before a launcher can see that acknowledgement.
            await wrapped(scope, None, send)
            assert not entered.is_set() and completed == []
            assert sent[0]["status"] == 503
            assert (b"retry-after", b"1") in sent[0]["headers"]

    asyncio.run(exercise())


def test_completed_activity_since_idle_decision_refuses_close(tmp_path, monkeypatch):
    obj = gate(tmp_path)
    monkeypatch.setattr(observer.time, "monotonic", lambda: 10)
    obj.note()  # Completed between idle decision and close, not just in flight.
    close(obj)
    assert obj.active == 0 and obj.gate_until == 0


def test_gate_reopens_without_launcher_and_never_renews_old_command(tmp_path, monkeypatch):
    obj = gate(tmp_path)
    clock = [10]
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])
    close(obj)
    assert not obj.note(1, admit=True)
    clock[0] = 15.01
    obj.control_gate()  # A crashed/stalled launcher left the same file behind.
    assert obj.note(1, admit=True)
    assert obj.active == 1


@pytest.mark.parametrize("committed", [False, True])
def test_frame_during_gate_is_explicit_error_and_disconnect_not_delivered(tmp_path, monkeypatch, committed):
    wrapped = gate(tmp_path)
    monkeypatch.setattr(observer.time, "monotonic", lambda: 10)

    async def exercise():
        ready, release = asyncio.Event(), asyncio.Event()
        delivered, sent = [], []

        async def receive():
            await release.wait()
            return {"type": "websocket.receive", "text": '{"type":"query","prompt":"private"}'}

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            ready.set()
            delivered.append(await receive())

        wrapped.app = app
        task = asyncio.create_task(wrapped({"type": "websocket", "path": "/api/v1/ws/chat"}, receive, send))
        await ready.wait()
        close(wrapped)
        if committed:
            wrapped.gate_request.write_text(json.dumps({"action": "commit", "id": "attempt",
                                                       "pid": 123, "identity": "start"}))
            wrapped.control_gate()
            assert wrapped.gate_state == "committed"
            monkeypatch.setattr(observer.time, "monotonic", lambda: 100)
        release.set()
        await task
        assert delivered == [{"type": "websocket.disconnect", "code": 1013}]
        error = json.loads(sent[0]["text"])
        assert error["type"] == "error" and "NOT sent" in error["message"]
        assert sent[1]["type"] == "websocket.close" and sent[1]["code"] == 1013
        assert "private" not in wrapped.gate_request.read_text()

    asyncio.run(exercise())

@pytest.mark.parametrize("stall", ["before-commit", "after-commit"])
def test_launcher_stall_at_commit_boundary_completes_or_refuses_work(tmp_path, monkeypatch, stall):
    wrapped = gate(tmp_path)
    clock = [10]
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])
    lease = idle.IdleLease(SimpleNamespace(directory=tmp_path,
        data={"app": {"pid": 123, "pid_identity": "start"}}), threading.Event(), seconds=0)

    def command_and_receipt(path, value):
        path.write_text(json.dumps(value))
        wrapped.control_gate()
        lease.receipt.write_text(json.dumps({"pid": 123, "identity": "start",
            "gate_id": wrapped.gate_id, "gate_until": wrapped.gate_until,
            "gate_state": wrapped.gate_state, "gate_commit_id": wrapped.gate_commit_id,
            "active": wrapped.active, "uncertain": False}))

    monkeypatch.setattr("scripts.devloop.__main__.write_private_json", command_and_receipt)
    assert lease.close_admission()
    assert lease.admission_closed()  # The old launcher-side final proof succeeds.
    if stall == "before-commit":
        clock[0] += 6
        assert not lease.commit_admission()  # Expiry cannot be renewed by commit.
    else:
        assert lease.commit_admission()
        clock[0] += 6  # SIGTERM has not happened yet; committed must NOT expire.
        lease.open_admission()
        wrapped.control_gate()  # Even a missing file cannot reopen committed.

    async def exercise():
        completed, sent = [], []

        async def app(scope, receive, send):
            await asyncio.sleep(0)
            completed.append("work finished")
            await send({"type": "http.response.start", "status": 200})

        async def send(message):
            sent.append(message)

        wrapped.app = app
        await wrapped({"type": "http", "method": "POST", "path": "/late-work"}, None, send)
        if stall == "before-commit":
            assert completed == ["work finished"] and sent[0]["status"] == 200
        else:
            assert completed == [] and sent[0]["status"] == 503
            assert (b"retry-after", b"1") in sent[0]["headers"]

    asyncio.run(exercise())


def test_stale_commit_cannot_commit_another_cycle_or_child(tmp_path, monkeypatch):
    wrapped = gate(tmp_path)
    clock = [10]
    monkeypatch.setattr(observer.time, "monotonic", lambda: clock[0])
    close(wrapped)
    clock[0] = 16
    wrapped.gate_request.write_text(json.dumps({"id": "next-cycle", "pid": 123,
        "identity": "start", "before": 0, "expires": 21}))
    wrapped.control_gate()
    assert wrapped.gate_state == "closed" and wrapped.gate_id == "next-cycle"
    for token, identity in (("attempt", "start"), ("next-cycle", "previous-child")):
        wrapped.gate_request.write_text(json.dumps({"id": token, "action": "commit",
                                                   "pid": 123, "identity": identity}))
        wrapped.control_gate()
        assert wrapped.gate_state == "closed" and wrapped.gate_id == "next-cycle"
    wrapped.gate_state, wrapped.gate_id, wrapped.identity = "open", None, "new-child"
    wrapped.control_gate()
    assert wrapped.gate_state == "open"  # A prior child's commit cannot create a close.
