"""Revocation against a shell being opened, on the real relay, with barriers on the link store."""
import asyncio
import base64
import threading
from types import SimpleNamespace

import pytest

from app.db import terminal_device_links as link_store
from app.network import engine, terminal_relay as relay
from tests.core.test_terminal_targets import DEVICE, peers, signed_proof  # noqa: F401

WAIT = 10


def hold(monkeypatch, name):
    """Park one link-store call in its worker thread until the test lets it go."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(link_store, name)

    def held(key_id):
        entered.set()
        assert release.wait(WAIT)
        return real(key_id)

    monkeypatch.setattr(link_store, name, held)
    return entered, release


async def reached(event):
    assert await asyncio.to_thread(event.wait, WAIT)


async def until(check):
    for _ in range(WAIT * 100):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def start_open():
    lease = relay.authorize(SimpleNamespace(valid=True), "owner", "peer")
    proof = await signed_proof()
    body = {"lease": lease, "op": "open", "options": {}, "proof": proof}
    return lease, body, asyncio.create_task(relay.handle(body, "source"))


async def assert_no_writable_shell(receiver, lease):
    assert receiver.count == 0 and not relay._sessions and not relay._opening
    data = base64.b64encode(b"after\n").decode("ascii")
    with pytest.raises(LookupError):
        await relay.handle({"lease": lease, "op": "write", "data": data}, "source")


@pytest.mark.asyncio
async def test_revocation_during_final_registration_leaves_no_writable_shell(peers, monkeypatch):  # noqa: F811
    entered, release = hold(monkeypatch, "touch")
    lease, _, opening = await start_open()
    await reached(entered)
    revoking = asyncio.create_task(relay.revoke_device(DEVICE.key_id))
    # Revocation either waits for this open or the open is refused. Returning
    # first and letting the shell appear afterwards is the failure.
    await asyncio.wait({revoking}, timeout=0.3)
    release.set()
    await asyncio.gather(opening, return_exceptions=True)
    assert await revoking == {"revoked": True}
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_open_reaching_its_final_check_during_revocation_is_refused(peers, monkeypatch):  # noqa: F811
    entered, release = hold(monkeypatch, "delete")
    revoking = asyncio.create_task(relay.revoke_device(DEVICE.key_id))
    await reached(entered)
    lease, _, opening = await start_open()
    await until(lambda: peers[0].count == 1)
    await asyncio.sleep(0.05)
    assert not opening.done()
    release.set()
    assert await revoking == {"revoked": True}
    with pytest.raises(PermissionError, match="revoked"):
        await opening
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_a_failed_final_step_kills_the_shell_and_frees_the_lease(peers, monkeypatch):  # noqa: F811
    def broken(key_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(link_store, "touch", broken)
    lease, _, opening = await start_open()
    with pytest.raises(RuntimeError, match="database"):
        await opening
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_cancelling_during_the_final_step_kills_the_shell(peers, monkeypatch):  # noqa: F811
    entered, release = hold(monkeypatch, "touch")
    lease, body, opening = await start_open()
    await reached(entered)
    # The reservation lasts until registration: the lease can't be opened twice.
    with pytest.raises(PermissionError, match="already used"):
        await relay.handle(body, "source")
    opening.cancel()
    release.set()
    await asyncio.gather(opening, return_exceptions=True)
    assert opening.cancelled()
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_cancelling_while_waiting_behind_a_revocation_kills_the_shell(peers, monkeypatch):  # noqa: F811
    entered, release = hold(monkeypatch, "delete")
    revoking = asyncio.create_task(relay.revoke_device(DEVICE.key_id))
    await reached(entered)
    lease, _, opening = await start_open()
    await until(lambda: peers[0].count == 1)
    await asyncio.sleep(0.05)
    opening.cancel()
    await asyncio.gather(opening, return_exceptions=True)
    release.set()
    await revoking
    assert opening.cancelled()
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_cancelling_the_cleanup_does_not_strand_the_shell(peers, monkeypatch):  # noqa: F811
    entered, release = hold(monkeypatch, "touch")
    lease, _, opening = await start_open()
    await reached(entered)
    terminal = peers[0].list()[0]
    real_kill = terminal.kill
    killing, finish_kill = asyncio.Event(), asyncio.Event()

    async def slow_kill():
        killing.set()
        await finish_kill.wait()
        await real_kill()

    monkeypatch.setattr(terminal, "kill", slow_kill)
    opening.cancel()
    release.set()
    await asyncio.wait_for(killing.wait(), WAIT)
    opening.cancel()  # A second cancel lands while the shell is being killed.
    await asyncio.gather(opening, return_exceptions=True)
    finish_kill.set()
    await until(lambda: terminal.state == "exited")
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["feature-off", "worker-removed", "source-revoked"])
async def test_other_authority_lost_during_final_registration_is_refused(peers, monkeypatch, change):  # noqa: F811
    entered, release = hold(monkeypatch, "touch")
    lease, _, opening = await start_open()
    await reached(entered)
    if change == "feature-off":
        monkeypatch.setattr(relay, "peer_targets_enabled", lambda: False)
    elif change == "worker-removed":
        monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="peer", member=lambda device: None))
    else:
        assert await relay.handle({"lease": lease, "op": "revoke"}, "source") == {"revoked": True}
    release.set()
    with pytest.raises(PermissionError):
        await opening
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: True)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="peer", member=lambda device: (
        SimpleNamespace(kind="worker", status="active"))))
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_a_link_moved_to_another_worker_during_open_is_refused(peers, monkeypatch):  # noqa: F811
    entered, release = hold(monkeypatch, "touch")
    real_get = link_store.get
    lease, _, opening = await start_open()
    await reached(entered)
    monkeypatch.setattr(link_store, "get", lambda key_id: {**real_get(key_id), "via_device": "elsewhere"})
    release.set()
    with pytest.raises(PermissionError):
        await opening
    await assert_no_writable_shell(peers[0], lease)


@pytest.mark.asyncio
async def test_a_cancelled_revocation_still_finishes(peers, monkeypatch):  # noqa: F811
    lease, _, opening = await start_open()
    await opening
    entered, release = hold(monkeypatch, "delete")
    revoking = asyncio.create_task(relay.revoke_device(DEVICE.key_id))
    await reached(entered)
    revoking.cancel()
    await asyncio.gather(revoking, return_exceptions=True)
    release.set()
    await until(lambda: not relay._sessions and peers[0].count == 0)
    assert link_store.get(DEVICE.key_id) is None
    await assert_no_writable_shell(peers[0], lease)
