"""Deterministic two-phase pause admission: completion or explicit refusal."""
import asyncio
import json
import threading

import pytest

from scripts.devloop import observer


def gate(tmp_path):
    obj = observer.ActivityObserver.__new__(observer.ActivityObserver)
    obj.lock = threading.Lock()
    obj.pid, obj.identity = 123, "start"
    obj.active, obj.activity, obj.uncertain = 0, 0, False
    obj.gate_id, obj.gate_until = None, 0
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


def test_frame_during_gate_is_explicit_error_and_disconnect_not_delivered(tmp_path, monkeypatch):
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
        release.set()
        await task
        assert delivered == [{"type": "websocket.disconnect", "code": 1013}]
        error = json.loads(sent[0]["text"])
        assert error["type"] == "error" and "NOT sent" in error["message"]
        assert sent[1]["type"] == "websocket.close" and sent[1]["code"] == 1013
        assert "private" not in wrapped.gate_request.read_text()

    asyncio.run(exercise())
