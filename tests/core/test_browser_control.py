"""Bounded workspace broker ownership, delivery, lifecycle and validation."""

import asyncio
import time

import pytest

from app.core import browser_control as control
from app.core import browser_panel_registry as registry


def state(runtime="runtime", revision=0, viewer=None):
    value = control.unavailable(runtime)
    value.update(
        revision=revision,
        available=True,
        target_id="target",
        active_tab_id="tab",
        tabs=[
            {
                "id": "tab",
                "url": "https://example.test",
                "title": "Example",
                "current": True,
            }
        ],
    )
    if viewer:
        value["control"].update(owner="human", viewer_id=viewer)
    return value


@pytest.fixture
async def broker():
    b = control.get("broker-test")
    await b.register("runtime")
    await b.report("runtime", state(), [])
    yield b
    registry.clear("broker-test")
    await asyncio.sleep(0)


def command(cid="cmd", op="takeover", args=None, **extra):
    return dict(
        id=cid, runtime_id="runtime", tab_id="tab", op=op, args=args or {}, **extra
    )


@pytest.mark.asyncio
async def test_native_edit_requires_owner_and_valid_utf16_selection(broker):
    args = {"field": "field", "action": "replace", "value": "😀", "start": 2, "end": 2}
    with pytest.raises(control.ControlError, match="Take control"):
        await broker.submit("viewer", command("unowned", "edit", args))
    await broker.report("runtime", state(revision=1, viewer="viewer"), [])
    await broker.submit("viewer", command("valid", "edit", args))
    assert (await broker.poll("runtime"))[0]["args"] == args
    for index, bad in enumerate(({**args, "end": 3}, {**args, "start": -1}, {**args, "script": "evil"})):
        with pytest.raises(control.ControlError):
            await broker.submit("viewer", command(f"bad{index}", "edit", bad))


def test_native_edit_state_rejects_password_disclosure():
    value = state()
    field = dict(id="field", type="password", label="Password", value="", x=0, y=0, width=100, height=30)
    value["editors"] = [field]
    control.validate_state(value, "runtime")
    field["value"] = "must-not-broadcast"
    with pytest.raises(control.ControlError, match="Password"):
        control.validate_state(value, "runtime")


@pytest.mark.asyncio
async def test_no_redelivery_immediate_results_and_stale_revision(broker):
    received = []

    async def send(msg):
        received.append(msg)

    broker.viewers["viewer"] = send
    await broker.submit("viewer", command())
    batch = await broker.poll("runtime")
    assert len(batch) == 1 and batch[0]["viewer_id"] == "viewer"
    assert batch[0]["expires_at"] > time.time() * 1000
    assert broker.queue == []
    pending_poll = asyncio.create_task(broker.poll("runtime"))
    await asyncio.sleep(0)
    await broker.report("runtime", state(revision=0), [{"id": "cmd", "ok": True}])
    assert received[-1] == {"type": "command-result", "id": "cmd", "ok": True}
    assert not pending_poll.done()
    pending_poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_poll
    with pytest.raises(control.ControlError, match="not replayed"):
        await broker.submit("viewer", command())


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['a' * 16384, '漢' * 16384, '\x00' * 16384, '😀' * 16384],
                         ids=['ascii', 'cjk', 'escaped-control', 'emoji'])
async def test_poll_bounds_actual_response_bytes_without_losing_queued_text(broker, text):
    from starlette.responses import JSONResponse
    await broker.report('runtime', state(revision=1, viewer='viewer'), [])
    for index in range(16):
        await broker.submit('viewer', command(str(index), 'input', {
            'method': 'Input.insertText', 'params': {'text': text}}), internal=True)
    delivered = []
    while broker.queue:
        batch = await broker.poll('runtime')
        assert batch
        assert len(JSONResponse({'commands': batch}).body) <= control.MAX_BODY
        delivered.extend(item['id'] for item in batch)
        assert len(broker.queue) == 16 - len(delivered)
    assert delivered == [str(index) for index in range(16)]
    assert len(broker.pending) == 16  # Delivery still waits for explicit execution ACK.


@pytest.mark.asyncio
async def test_replacement_retires_identity_and_clears_pending(broker):
    await broker.submit("viewer", command())
    await broker.register("replacement")
    assert not broker.pending and not broker.queue
    for action in (
        broker.register("runtime"),
        broker.poll("runtime"),
        broker.report("runtime", state(), []),
    ):
        with pytest.raises(control.ControlError):
            await action
    await broker.register("replacement")


@pytest.mark.asyncio
async def test_expired_runtime_cannot_resurrect(broker):
    broker.last_poll -= control.TTL + 1
    with pytest.raises(control.ControlError):
        await broker.register("runtime")
    with pytest.raises(control.ControlError):
        await broker.report("runtime", state(), [])
    await broker.retire()
    assert not broker.state["available"]
    assert registry.get_target("broker-test").target_id is None


@pytest.mark.asyncio
async def test_ownership_reservation_and_disconnect_release(broker):
    await broker.submit("viewer", command())
    with pytest.raises(control.ControlError, match="Another viewer"):
        await broker.submit("competitor", command("other"))
    await broker.detach("viewer")
    queued = await broker.poll("runtime")
    assert [c["op"] for c in queued] == ["release"]
    assert queued[0]["viewer_id"] == "viewer"


@pytest.mark.asyncio
async def test_human_only_and_stale_tab(broker):
    with pytest.raises(control.ControlError, match="Take control"):
        await broker.submit(
            "viewer", command(op="navigate", args={"url": "https://example.test"})
        )
    await broker.report("runtime", state(revision=1, viewer="viewer"), [])
    stale = command(op="navigate", args={"url": "https://example.test"})
    stale["tab_id"] = "old"
    with pytest.raises(control.ControlError, match="Stale tab"):
        await broker.submit("viewer", stale)
    await broker.submit(
        "viewer",
        command(op="viewport", args={"mode": "fit", "width": 900, "height": 600}),
    )
    assert (await broker.poll("runtime"))[0]["op"] == "viewport"


@pytest.mark.asyncio
async def test_runtime_loss_advances_revision_for_connected_viewers(broker):
    await broker.report("runtime", state(revision=200), [])
    received = []

    async def send(message):
        received.append(message)

    broker.viewers["viewer"] = send
    await broker.retire()
    assert received[-1]["state"]["revision"] == 201
    assert received[-1]["state"]["available"] is False


@pytest.mark.asyncio
async def test_replacement_does_not_inherit_the_old_active_poll(broker):
    old_poll = asyncio.create_task(broker.poll("runtime"))
    await asyncio.sleep(0)
    await broker.register("replacement")
    await broker.report("replacement", state(runtime="replacement"), [])
    await broker.submit("viewer", {**command(), "runtime_id": "replacement"})
    assert (await broker.poll("replacement"))[0]["runtime_id"] == "replacement"
    with pytest.raises(control.ControlError):
        await old_poll


@pytest.mark.asyncio
async def test_concurrent_registrations_retire_every_superseded_runtime(broker):
    await asyncio.gather(broker.register("second"), broker.register("third"))
    assert broker.runtime_id == "third"
    assert {"runtime", "second"} <= broker.retired
    with pytest.raises(control.ControlError):
        await broker.register("second")


@pytest.mark.parametrize("width,height", [(1, 4096), (4096, 1)])
def test_agent_viewport_range_is_valid_state_but_not_panel_command(width, height):
    value = state()
    value["viewport"] = {"mode": "fixed", "width": width, "height": height}
    assert control.validate_state(value, "runtime") == value
    with pytest.raises(control.ControlError):
        control.viewport(value["viewport"])


@pytest.mark.asyncio
async def test_existing_committed_text_limit_is_preserved(broker):
    await broker.report("runtime", state(revision=1, viewer="viewer"), [])
    await broker.submit(
        "viewer",
        command(
            op="input",
            args={
                "method": "Input.insertText",
                "params": {"text": "x" * 16384},
            },
        ),
        internal=True,
    )
    assert len((await broker.poll("runtime"))[0]["args"]["params"]["text"]) == 16384


@pytest.mark.asyncio
async def test_queue_bound_and_single_poll(broker, monkeypatch):
    monkeypatch.setattr(control, "MAX_PENDING", 1)
    await broker.submit("viewer", command())
    with pytest.raises(control.ControlError, match="queue full"):
        await broker.submit("viewer", command("second"))
    await broker.poll("runtime")
    task = asyncio.create_task(broker.poll("runtime"))
    await asyncio.sleep(0)
    with pytest.raises(control.ControlError, match="already active"):
        await broker.poll("runtime")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_expired_queued_command_not_delivered(broker):
    await broker.submit("viewer", command())
    broker.queue[0]["expires_at"] = 0
    assert await broker.poll("runtime") == []


@pytest.mark.asyncio
async def test_clear_cancels_broker_and_wakes_poll(broker):
    task = asyncio.create_task(broker.poll("runtime"))
    await asyncio.sleep(0)
    control.clear("broker-test")
    with pytest.raises(control.ControlError):
        await task
    await asyncio.sleep(0)
    assert broker.timer.done()


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf"), 1e20, "x" * 16385]
)
def test_finite_payload_limits(value):
    with pytest.raises(control.ControlError):
        control.finite({"value": value})


@pytest.mark.parametrize(
    "change",
    [
        {"revision": -1},
        {"revision": True},
        {"runtime_id": "other"},
        {"tabs": []},
        {"viewport": {"mode": "fit", "width": 0, "height": 600}},
        {"busy": "yes"},
        {
            "tabs": [
                {"id": "tab", "current": False, "url": "about:blank", "title": "Tab"}
            ]
        },
        {"active_tab_id": None},
    ],
)
def test_state_validation(change):
    value = state()
    value.update(change)
    with pytest.raises(control.ControlError):
        control.validate_state(value, "runtime")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op,args",
    [
        ("eval", {}),
        ("heartbeat", {}),
        ("navigate", {"url": "javascript:alert(1)"}),
        ("viewport", {"mode": "fixed", "width": 3841, "height": 600}),
        ("command", {"command": "execute"}),
        ("input", {"method": "Runtime.evaluate", "params": {}}),
    ],
)
async def test_reject_arbitrary_operations(broker, op, args):
    await broker.report("runtime", state(revision=1, viewer="viewer"), [])
    with pytest.raises(control.ControlError):
        await broker.submit("viewer", command(op=op, args=args))
