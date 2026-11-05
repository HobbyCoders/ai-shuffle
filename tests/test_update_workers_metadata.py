"""Updater metadata crosses fleet boundaries only through the shared validator."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import application_updates as updates
from app.core import update_workers as fleet
from docker.application_updates import ContainerUpdates
from tests.test_update_workers import setup

A, B = "a" * 40, "b" * 40
UNKNOWN = {"revision": None, "capabilities": [], "refresh": "unknown"}


@pytest.fixture
def metadata_fleet(setup, monkeypatch, tmp_path):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    monkeypatch.delenv("SHUFFLE_HANDED_OVER", raising=False)
    monkeypatch.setattr(fleet, "_async_waiting_status", AsyncMock(return_value={}))
    monkeypatch.setattr(fleet, "_discover", Mock())
    states = []
    for name, revision in (("local", A), ("peer", B)):
        producer = ContainerUpdates(tmp_path / name, {"revision": revision, "sequence": 1, "runtime": "fixture"},
                                    b"", lambda: True, lambda _: {}, refresh_mode="disabled")
        producer.begin_diagnostics(revision)
        with producer.stage("tools_build") as result:
            result["outcome"] = "hit"
        producer.publish("idle")
        monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(producer.root))
        states.append(updates.status())
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path / "local"))
    remote.return_value = states[1]
    # Directory identity/discovery records are not updater metadata producers.
    listing = fleet.runners.directory()
    for row in listing["runners"]:
        if not row["is_self"]:
            row.update(updater=states[0]["updater"], diagnostics=states[0]["diagnostics"])
    monkeypatch.setattr(fleet.runners, "directory", lambda: listing)
    return remote, states


def test_snapshot_preserves_distinct_actual_producers_and_clears_offline_metadata(metadata_fleet):
    remote, states = metadata_fleet
    rows = {row["device_id"]: row for row in asyncio.run(fleet.snapshot(fresh=True))["workers"]}
    for device, state in zip(("self", "peer"), states):
        assert rows[device]["updater"] == state["updater"]
        assert rows[device]["diagnostics"] == state["diagnostics"]
    assert rows["offline"]["updater"] == UNKNOWN
    assert rows["offline"]["diagnostics"] is None
    remote.assert_awaited_once()
    assert remote.call_args.args[2] == {"operation": "status"}


@pytest.mark.parametrize("response", ["legacy", "refused", "unavailable", "invalid_response"])
def test_old_and_unreadable_peers_never_borrow_local_metadata(metadata_fleet, response):
    remote, _ = metadata_fleet
    if response == "legacy":
        remote.return_value = {"phase": "idle", "supported": True, "allow_updates": True}
    elif response == "refused":
        remote.side_effect = fleet.control.ControlRefused("private diagnostic")
    elif response == "unavailable":
        remote.side_effect = RuntimeError("private diagnostic")
    else:
        remote.return_value = ["private diagnostic"]
    rows = {row["device_id"]: row for row in asyncio.run(fleet.snapshot(fresh=True))["workers"]}
    assert rows["self"]["updater"]["revision"] == A
    for device in ("peer", "offline"):
        assert rows[device]["updater"] == UNKNOWN
        assert rows[device]["diagnostics"] is None
    assert "private diagnostic" not in json.dumps(rows)


def test_remote_and_final_local_rows_apply_the_same_redaction(metadata_fleet, monkeypatch):
    remote, states = metadata_fleet
    def with_extra(state):
        state = json.loads(json.dumps(state))
        state["updater"]["environment"] = "private environment"
        state["diagnostics"]["command"] = "private command"
        state["diagnostics"]["stages"][0]["command"] = "private command"
        return state
    monkeypatch.setattr(updates, "status", lambda: with_extra(states[0]))
    remote.return_value = with_extra(states[1])
    received = asyncio.run(fleet._remote({"device_id": "peer"}, {"operation": "status"}))
    assert received["updater"] == states[1]["updater"]
    assert received["diagnostics"] == states[1]["diagnostics"]
    rows = {row["device_id"]: row for row in asyncio.run(fleet.snapshot(fresh=True))["workers"]}
    assert rows["self"]["updater"] == states[0]["updater"]
    assert rows["self"]["diagnostics"] == states[0]["diagnostics"]
    assert rows["peer"]["updater"] == states[1]["updater"]
    assert rows["peer"]["diagnostics"] == states[1]["diagnostics"]
    assert "private" not in json.dumps(rows)


@pytest.mark.parametrize("invalid", ["revision", "capability", "stage", "negative", "nonfinite", "unbounded"])
def test_invalid_metadata_is_bounded_for_both_local_and_peer_rows(metadata_fleet, monkeypatch, invalid):
    remote, states = metadata_fleet
    bad = json.loads(json.dumps(states[0]))
    if invalid == "revision":
        bad["updater"]["revision"] = "private revision"
    elif invalid == "capability":
        bad["updater"]["capabilities"] = ["private capability"]
    elif invalid == "stage":
        bad["diagnostics"]["stages"][0]["stage"] = "private command"
    elif invalid == "negative":
        bad["diagnostics"]["stages"][0]["elapsed_ms"] = -1
    elif invalid == "nonfinite":
        bad["diagnostics"]["total_elapsed_ms"] = float("inf")
    else:
        bad["diagnostics"]["stages"] *= 97
    monkeypatch.setattr(updates, "status", lambda: bad)
    remote.return_value = bad
    rows = {row["device_id"]: row for row in asyncio.run(fleet.snapshot(fresh=True))["workers"]}
    expected = updates.safe_update_metadata({"updater": bad["updater"], "diagnostics": bad["diagnostics"]})
    for device in ("self", "peer"):
        assert rows[device]["updater"] == expected["updater"]
        assert rows[device]["diagnostics"] == expected["diagnostics"]
    assert "private" not in json.dumps(rows)


def test_refresh_stays_outside_remote_and_bulk_dispatch(metadata_fleet):
    remote, _ = metadata_fleet
    with pytest.raises(ValueError, match="Unknown update action"):
        asyncio.run(fleet.dispatch("refresh-updater"))
    with pytest.raises(ValueError, match="Unknown update action"):
        fleet.local_action("refresh-updater", "peer:request")
    remote.assert_not_awaited()
