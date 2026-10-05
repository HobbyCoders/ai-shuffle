import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core import application_updates as updates
from app.core import update_workers as fleet
from app.core import desktop_lifecycle


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet.progress, "_path", lambda: tmp_path / "progress.json")
    values = {}
    monkeypatch.setattr(updates.database, "get_system_setting", lambda key: values.get(key))
    monkeypatch.delenv("SHUFFLE_CONTAINER_UPDATES", raising=False)
    monkeypatch.delenv("SHUFFLE_UPDATE_POLICY_FILE", raising=False)
    monkeypatch.setattr(updates, "_desktop_seen", 0)
    monkeypatch.setattr(updates, "_desktop_request", None)
    monkeypatch.setattr(updates, "_desktop_followup", None)
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: [])
    fleet._receipts.clear()
    fleet._peer_status.clear()
    # Refusals and discovery times are module state: one test's must not hold another's restart.
    fleet._refusals.clear()
    fleet._discovery.clear()
    monkeypatch.setattr(fleet, "_refusals_at", 0.0)
    rows = [{"device_id": "self", "device_name": "Local", "is_self": True, "online": True},
            {"device_id": "peer", "device_name": "Peer", "is_self": False, "online": True},
            {"device_id": "offline", "device_name": "Offline", "is_self": False, "online": False}]
    monkeypatch.setattr(fleet.runners, "directory", lambda: {"self_device_id": "self", "runners": rows})
    remote = AsyncMock(return_value={"accepted": True, "reason": "Request queued"})
    monkeypatch.setattr(fleet.control, "call", remote)
    updates.desktop_exchange({"phase": "ready"})
    return values, remote


def test_default_current_dispatch_never_calls_peers(setup):
    _, remote = setup
    result = asyncio.run(fleet.dispatch("install"))
    assert result["scope"] == "current" and len(result["results"]) == 1
    assert result["results"][0]["accepted"]
    remote.assert_not_called()
    assert updates.desktop_exchange({"phase": "ready"})["action"] == "install"
    assert updates.desktop_exchange({"phase": "ready"}) == {}


def test_all_scope_targets_online_once_and_local_last(setup, monkeypatch):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    async def sent(*args, **kwargs):
        assert updates._desktop_request is None  # requester must remain up through fanout
        assert args[0] == "peer" and args[1] == "application-update"
        if args[2]["operation"] == "status":
            return {"phase": "ready", "version": "b" * 12}
        assert args[2]["expires_at"] > time.time()
        return {"accepted": True, "reason": "Request queued"}
    remote.side_effect = sent
    result = asyncio.run(fleet.dispatch("install"))
    assert result["scope"] == "all"
    assert [r["device_id"] for r in result["results"]] == ["peer", "offline", "self"]
    assert result["results"][1]["reason"] == "Offline; not queued"
    assert updates._desktop_request["action"] == "install"
    # The install guard's fresh reading, the staged target, then the action.
    assert remote.await_count == 3


def test_disabled_busy_and_unavailable_do_not_receive_commands(setup, monkeypatch):
    values, _ = setup
    values[updates.KEY] = "false"
    assert not fleet.local_action("install", "disabled")["accepted"]
    values.clear()
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: ["running"])
    assert "Busy" in fleet.local_action("install", "busy")["reason"]
    monkeypatch.setattr(updates, "_desktop_seen", time.monotonic() - 30)
    assert not fleet.local_action("install", "offline")["accepted"]
    assert updates._desktop_request is None


def test_all_scope_respects_individual_refusals_without_aborting_others(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"accepted": False, "reason": "Updates disabled"}
    result = asyncio.run(fleet.dispatch("install"))
    assert not result["results"][0]["accepted"]
    assert result["results"][-1]["accepted"]


def test_expired_native_requests_and_retries_are_not_replayed(setup, monkeypatch):
    first = fleet.local_action("install", "same-id")
    assert first["accepted"]
    assert updates.desktop_exchange({"phase": "ready"})["action"] == "install"
    assert fleet.local_action("install", "same-id") == first
    assert updates._desktop_request is None
    updates.enqueue("install")
    updates._desktop_request["expires_at"] = 0
    assert updates.desktop_exchange({"phase": "ready"}) == {}


def test_peer_boundary_rechecks_membership_expiry_and_target_policy(setup, monkeypatch):
    values, _ = setup
    member = SimpleNamespace(kind="worker", status="active")
    monkeypatch.setattr(fleet.engine, "context", lambda: SimpleNamespace(member=lambda _: member))
    body = {"operation": "action", "action": "install", "request_id": "one", "expires_at": time.time() + 10}
    values[updates.KEY] = "false"
    assert not asyncio.run(fleet.peer_action(body, "peer"))["accepted"]
    body["expires_at"] = 0
    with pytest.raises(ValueError, match="expired"):
        asyncio.run(fleet.peer_action(body, "peer"))
    member.kind = "hub"
    with pytest.raises(PermissionError):
        asyncio.run(fleet.peer_action({"operation": "status"}, "hub"))


def test_peer_cannot_replace_target_identity_in_receipts(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"device_id": "self", "device_name": "spoofed", "accepted": True, "reason": "Request queued"}
    result = asyncio.run(fleet.dispatch("install"))
    assert result["results"][0]["device_id"] == "peer"
    assert result["results"][0]["device_name"] == "Peer"


def test_all_snapshot_handles_partial_failures_without_aborting(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.side_effect = RuntimeError("private endpoint detail must not escape")
    result = asyncio.run(fleet.snapshot())
    assert len(result["workers"]) == 3
    assert result["workers"][0]["supported"]
    assert "unavailable" in result["workers"][1]["reason"]
    # An online worker that cannot be read is unknown, not excluded (#1623).
    assert result["workers"][1]["phase"] == "unknown"
    assert result["workers"][2]["reason"] == "Offline; not queued"
    assert result["workers"][2]["phase"] == "unsupported"
    assert "private endpoint" not in str(result)

def test_peer_that_answers_with_a_refusal_is_excluded_not_unknown(setup):
    # #1623: a peer that answers 4xx (e.g. an older build without the update
    # endpoint) will never stage, so it must not hold restart as "unknown".
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.side_effect = fleet.control.ControlRefused("Unknown control action")
    result = asyncio.run(fleet.snapshot())
    peer = result["workers"][1]
    assert peer["phase"] == "unsupported" and not peer["supported"]
    assert peer["reason"] == "Worker declined application updates; not included"


A, B = "a" * 40, "b" * 40


def peer_calls(remote):
    return [c for c in remote.await_args_list if c.args[2] == {"operation": "status"}]


async def settle(device_id):
    # The background read may already be done by the time the snapshot returns.
    task = fleet._peer_status[device_id]["task"]
    if task is not None:
        await task


def test_idle_snapshot_reuses_a_peer_reading_then_refreshes_behind_the_reply(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"supported": True, "phase": "idle", "running_version": A}
    async def scenario():
        await fleet.snapshot()
        second = await fleet.snapshot()
        assert len(peer_calls(remote)) == 1
        assert second["workers"][1]["phase"] == "idle"
        # Past the TTL the poll still answers from the last reading at once.
        fleet._peer_status["peer"]["at"] -= fleet.PEER_STATUS_TTL + 1
        remote.return_value = {"supported": True, "phase": "available", "version": B[:12], "running_version": A}
        stale = await fleet.snapshot()
        assert stale["workers"][1]["phase"] == "idle"
        await settle("peer")
        fresh = await fleet.snapshot()
        assert fresh["workers"][1]["phase"] == "available"
        assert len(peer_calls(remote)) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("reading", [{"phase": "downloading", "percent": 10},
                                     # A queued request the peer has not taken yet (#1623).
                                     {"phase": "idle", "request_pending": True}])
def test_busy_peer_is_reread_sooner(setup, reading):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"supported": True, **reading}
    async def scenario():
        await fleet.snapshot()
        fleet._peer_status["peer"]["at"] -= fleet.PEER_STATUS_BUSY_TTL + 1
        await fleet.snapshot()
        await settle("peer")
        assert len(peer_calls(remote)) == 2
    asyncio.run(scenario())


def test_concurrent_snapshots_share_one_peer_read(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    release = asyncio.Event()
    async def slow(*_args, **_kwargs):
        await release.wait()
        return {"supported": True, "phase": "idle"}
    remote.side_effect = slow
    async def scenario():
        pending = [asyncio.ensure_future(fleet.snapshot()) for _ in range(3)]
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(*pending)
        assert all(r["workers"][1]["phase"] == "idle" for r in results)
    asyncio.run(scenario())
    assert remote.await_count == 1


def test_completed_install_stops_reading_its_cohort(setup):
    # The reported loop: a finished all-worker install kept the peer polled every 2 s.
    values, remote = setup
    remote.return_value = {"supported": True, "version": B[:12], "running_version": A}
    fleet.progress.begin("done", [{"device_id": "peer", "device_name": "Peer"},
                                  {"device_id": "self", "device_name": "Local"}])
    fleet.progress.record("done", "peer", result={"accepted": False, "reason": "A newer runtime release is required"})
    fleet.progress.record("done", "self", result={"accepted": True, "version": B[:12]})
    updates.desktop_exchange({"phase": "idle", "running_version": B})
    assert asyncio.run(fleet.snapshot())["operation"]["phase"] == "complete"
    remote.reset_mock()
    for _ in range(3):
        state = asyncio.run(fleet.snapshot())
        assert state["operation"]["phase"] == "complete"
        assert [w["device_id"] for w in state["workers"]] == ["self"]
    remote.assert_not_called()


def test_completion_is_stamped_once_so_the_ui_can_tell_news_from_history(setup, monkeypatch):
    # #1623: a fresh browser replayed an old completion while a newer update was staged.
    fleet.progress.begin("done", [{"device_id": "self", "device_name": "Local"}])
    fleet.progress.record("done", "self", target_version=B[:12], result={"accepted": True})
    updates.desktop_exchange({"phase": "idle", "running_version": B})
    monkeypatch.setattr(fleet.progress.time, "time", lambda: 1_000.0)
    first = asyncio.run(fleet.snapshot())
    assert first["operation"]["phase"] == "complete"
    assert first["operation"]["completed_at"] == 1_000.0
    assert fleet.progress.load()["completed_at"] == 1_000.0
    monkeypatch.setattr(fleet.progress.time, "time", lambda: 5_000.0)
    later = asyncio.run(fleet.snapshot())
    assert later["operation"]["completed_at"] == 1_000.0
    assert later["server_time"] > later["operation"]["completed_at"]


def test_an_action_discards_cached_peer_readings(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"supported": True, "phase": "available", "version": B[:12]}
    async def scenario():
        await fleet.snapshot()
        await fleet.dispatch("download")
        remote.return_value = {"supported": True, "phase": "downloading", "percent": 5}
        state = await fleet.snapshot()
        assert state["workers"][1]["phase"] == "downloading"
    asyncio.run(scenario())


def test_install_survives_restart_and_waits_for_every_included_worker(setup, monkeypatch):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    peer_state = {"supported": True, "allow_updates": True, "phase": "ready", "version": B[:12], "running_version": A}
    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return dict(peer_state)
        # The target and cohort must already be durable when the command leaves.
        saved = fleet.progress.load()
        assert next(w for w in saved["workers"] if w["device_id"] == "peer")["target_version"] == B[:12]
        return {"accepted": True, "reason": "Request queued", "version": B[:12]}
    remote.side_effect = call
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    result = asyncio.run(fleet.dispatch("install"))
    assert result["results"][-1]["accepted"]
    saved = fleet.progress.load()
    assert {w["device_id"] for w in saved["workers"]} == {"self", "peer"}  # not offline workers
    assert updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})["action"] == "install"
    # Recreate volatile backend/launcher state; only the disk receipt survives.
    fleet._receipts.clear()
    monkeypatch.setattr(updates, "_desktop_seen", 0)
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["id"] == saved["id"]
    assert state["operation"]["phase"] == "waiting"
    updates.desktop_exchange({"phase": "idle", "running_version": B})
    values[updates.ALL_KEY] = "false"  # a policy/scope change must not shrink the cohort
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["phase"] == "waiting"
    assert [w["status"] for w in state["operation"]["workers"]] == ["waiting", "verified"]
    peer_state.update(phase="idle", running_version=B, version=None)
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["phase"] == "complete"
    assert all(w["status"] == "verified" for w in state["operation"]["workers"])
    # Completion is terminal, even if a worker later disconnects.
    remote.side_effect = RuntimeError("offline")
    assert asyncio.run(fleet.snapshot())["operation"]["phase"] == "complete"


def test_missing_worker_and_stale_available_version_cannot_complete(setup, monkeypatch):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    remote.return_value = {"accepted": True, "supported": True, "version": B[:12], "running_version": A}
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    asyncio.run(fleet.dispatch("install"))
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": B})
    monkeypatch.setattr(fleet.runners, "directory", lambda: {"self_device_id": "self", "runners": []})
    state = asyncio.run(fleet.snapshot())
    assert state["operation"]["phase"] == "waiting"
    assert state["operation"]["workers"][0]["status"] == "waiting"
    for action in ("install", "check", "download"):
        with pytest.raises(ValueError, match="previous update"):
            asyncio.run(fleet.dispatch(action))


def test_lost_action_reply_remains_unconfirmed_not_skipped(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return {"supported": True, "version": B[:12], "running_version": A}
        raise TimeoutError("reply lost after command delivery")
    remote.side_effect = call
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    asyncio.run(fleet.dispatch("install"))
    updates.desktop_exchange({"phase": "idle", "running_version": B})
    result = asyncio.run(fleet.snapshot())["operation"]
    assert result["phase"] == "waiting"
    assert result["workers"][0]["accepted"] is None
    assert result["workers"][0]["status"] == "waiting"


def test_startup_timeout_and_launcher_errors_never_celebrate(setup, monkeypatch):
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    asyncio.run(fleet.dispatch("install"))
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A, "error": "Restart failed"})
    assert asyncio.run(fleet.snapshot())["operation"]["phase"] == "error"
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    saved = fleet.progress.load()
    monkeypatch.setattr(fleet.progress.time, "time", lambda: saved["started_at"] + 601)
    result = asyncio.run(fleet.snapshot())["operation"]
    assert result["phase"] == "error"
    assert "Startup not confirmed" in result["workers"][0]["message"]


@pytest.mark.parametrize("running,target", [(None, B), (B, None), ("", ""), (A, B), ("b", B), ("latest", "latest")])
def test_version_verification_requires_a_matching_commit(running, target):
    assert not fleet.progress.same_version(running, target)


def test_container_reports_running_code_not_staged_status(setup, monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    monkeypatch.setenv("GIT_SHA", A)
    (tmp_path / "status.json").write_text(json.dumps({"phase": "ready", "version": B, "running_version": B}))
    state = updates.status()
    assert state["running_version"] == A
    assert state["version"] == B


def test_rejected_duplicate_install_recovers_after_both_workers_reach_target(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    # The peer checked its feed after the desktop found B and found nothing, so
    # the install guard counts it out rather than sending it a download.
    peer_state = {"supported": True, "allow_updates": True, "phase": "idle",
                  "version": B[:12], "running_version": A, "checked_at": time.time()}

    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return dict(peer_state)
        return {"accepted": False, "reason": "No staged update; current version left running",
                "version": B[:12]}

    remote.side_effect = call
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A})
    updates.enqueue("install")
    queued = dict(updates._desktop_request)
    result = asyncio.run(fleet.dispatch("install"))
    assert not any(row["accepted"] for row in result["results"])
    assert result["results"][-1]["reason"] == "An update request is already queued"
    assert updates._desktop_request == queued  # Never enqueue another restart.
    failed = asyncio.run(fleet.snapshot())["operation"]
    assert failed["phase"] == "error"
    # Also recover an error persisted by an older worker revision.
    fleet.progress.atomic_json(fleet.progress._path(), failed)

    assert updates.desktop_exchange({"phase": "idle", "running_version": B})["action"] == "install"
    peer_state["running_version"] = B
    recovered = asyncio.run(fleet.snapshot())["operation"]
    assert recovered["phase"] == "complete"
    assert all(row["status"] == "verified" for row in recovered["workers"])
    assert all(row["accepted"] is False for row in recovered["workers"])
    assert fleet.progress.load() == recovered
    assert updates._desktop_request is None


@pytest.mark.parametrize("change", [
    {"online": False}, {"supported": False}, {"running_version": A},
    {"running_version": None}, {"running_version": "b"},
])
def test_declined_install_without_live_matching_revision_stays_unconfirmed(setup, change):
    fleet.progress.begin("declined", [{"device_id": "self", "device_name": "Local"}])
    fleet.progress.record("declined", "self", target_version=B[:12],
                          result={"accepted": False, "reason": "Updates disabled"})
    live = {"device_id": "self", "online": True, "supported": True,
            "running_version": B, **change}
    operation = fleet.progress.observe(fleet.progress.load(), [live])
    assert operation["phase"] == "error"
    assert operation["workers"][0]["status"] == "skipped"
    assert updates._desktop_request is None


# #1623 live test: a container peer that had not run its daily check sat in
# "idle" and refused the fanned-out download, so it was never updated.
@pytest.fixture
def container(setup, monkeypatch, tmp_path):
    root = tmp_path / "updater"
    root.mkdir()
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(root))
    def status(phase, **fields):
        (root / "status.json").write_text(json.dumps({"phase": phase, **fields}))
    return root, status


@pytest.mark.parametrize("phase", ["idle", "error", "available"])
def test_download_reaches_a_container_that_has_not_found_the_update(container, phase):
    root, status = container
    status(phase, message="Application update is running." if phase == "idle" else None)
    result = fleet.local_action("download", "peer:" + phase)
    assert result["accepted"], result
    assert result["reason"] == ("Checking for an update to download" if phase == "idle" else "Request queued")
    assert json.loads((root / "request.json").read_text())["action"] == "download"
    assert updates.status()["request_pending"] is True


@pytest.mark.parametrize("phase,reason", [
    ("checking", "An update is already in progress"),
    ("downloading", "An update is already in progress"),
    ("image-required", "A newer runtime release is required"),
    ("ready", "Already staged; ready to restart"),
])
def test_download_is_still_refused_while_the_container_is_busy_or_blocked(container, phase, reason):
    root, status = container
    status(phase)
    result = fleet.local_action("download", "peer:" + phase)
    assert (result["accepted"], result["reason"]) == (False, reason)
    assert not (root / "request.json").exists()
    assert updates.status()["request_pending"] is False


def test_download_is_refused_when_updates_are_disabled_on_the_container(setup, container):
    values, _ = setup
    root, status = container
    status("idle")
    values[updates.KEY] = "false"
    result = fleet.local_action("download", "peer:disabled")
    assert (result["accepted"], result["reason"]) == (False, "Updates disabled")
    assert not (root / "request.json").exists()


def test_install_still_needs_a_staged_update_and_no_live_sessions(container, monkeypatch):
    root, status = container
    status("idle")
    assert fleet.local_action("install", "peer:idle")["reason"] == "No staged update; current version left running"
    status("ready")
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: ["running"])
    assert "Busy" in fleet.local_action("install", "peer:busy")["reason"]
    assert not (root / "request.json").exists()


def test_fanned_out_download_queues_an_idle_container_peer(setup):
    # The peer side runs local_action; this checks the receipt the initiator shows.
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    updates.desktop_exchange({"phase": "available", "version": B[:12]})
    async def peer(*args, **kwargs):
        return {"accepted": True, "reason": "Checking for an update to download", "phase": "idle"}
    remote.side_effect = peer
    result = asyncio.run(fleet.dispatch("download"))
    by_id = {row["device_id"]: row for row in result["results"]}
    assert by_id["peer"]["accepted"] and by_id["peer"]["reason"] == "Checking for an update to download"
    assert by_id["self"]["accepted"]


def test_idle_status_drops_a_message_that_claims_work_in_progress(container):
    _, status = container
    status("idle", message="Application update is running.")
    assert updates.status()["message"] is None
    status("idle", message="Updated version is running.")
    assert updates.status()["message"] == "Updated version is running."
    status("ready", message="Update ready. Choose Update and restart.")
    assert updates.status()["message"] == "Update ready. Choose Update and restart."
    status("downloading", message="Building frontend…")
    assert updates.status()["message"] == "Building frontend…"


def _never_checked_peer(remote, peer):
    """A container peer on A that has not read its feed since the desktop found B."""
    actions = []

    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return dict(peer)
        actions.append(body["action"])
        if body["action"] == "download":
            peer["phase"] = "checking"
        if body["action"] == "install":
            peer["phase"] = "idle"
        return {"accepted": True, "reason": "Request queued"}

    remote.side_effect = call
    return actions


def test_never_checked_peer_is_downloaded_before_anything_installs(setup):
    # #1623: the desktop staged B while Unraid sat idle on A with no check since
    # container start. Restart was offered and Unraid was never asked to download.
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    peer = {"supported": True, "allow_updates": True, "phase": "idle", "version": None,
            "running_version": A, "checked_at": time.time() - 3600}
    actions = _never_checked_peer(remote, peer)
    updates.desktop_exchange({"phase": "ready", "version": B[:12], "running_version": A,
                              "checkedAt": "2099-01-01T00:00:00Z"})

    async def scenario():
        state = await fleet.snapshot(fresh=True)
        rows = {row["device_id"]: row for row in state["workers"]}
        assert rows["peer"]["role"] == "available"
        assert rows["peer"]["label"] == fleet.NOT_CHECKED
        assert rows["self"]["role"] == "staged"
        assert state["plan"]["action"] == "download" and not state["plan"]["ready"]
        # An install click while the peer is behind downloads on the peer only.
        held = await fleet.dispatch("install")
        assert held["sent"] == "download" and "nothing was installed" in held["held"]
        assert [row["device_id"] for row in held["results"]] == ["peer"]
        assert actions[-1] == "download"
        assert "install" not in actions and updates._desktop_request is None
        assert fleet.progress.load() is None  # no install was begun
        state = await fleet.snapshot(fresh=True)
        assert state["plan"]["preparing"] and not state["plan"]["ready"]
        # The peer stages B: now, and only now, install fans out to both.
        peer.update(phase="ready", version=B[:12], checked_at=time.time())
        state = await fleet.snapshot(fresh=True)
        assert state["plan"]["ready"] and state["plan"]["action"] == "install"
        done = await fleet.dispatch("install")
        assert done["sent"] == "install"
        assert [row["device_id"] for row in done["results"]] == ["peer", "offline", "self"]
        assert actions[-1] == "install"
        assert updates._desktop_request["action"] == "install"
    asyncio.run(scenario())


@pytest.mark.parametrize("change,role", [
    ({"checked_at": None}, "available"),                 # never reported a check
    ({"checked_at": 10.0}, "available"),                 # checked before the desktop found B
    ({"checked_at": 4_000_000_000.0}, "excluded"),       # checked after and found nothing
    ({"running_version": B}, "excluded"),                # already runs the offered build
    ({"channel": "stable"}, "excluded"),                 # a different channel
])
def test_not_checked_rule(change, role):
    offers = [{"version": B[:12], "channel": "edge", "checked_at": 1_000.0}]
    row = {"device_id": "peer", "device_name": "Peer", "is_self": False, "online": True, "supported": True,
           "allow_updates": True, "phase": "idle", "version": None, "running_version": A, "channel": "edge",
           "checked_at": 1.0, **change}
    assert fleet.classify(row, True, offers)["role"] == role
    assert fleet.classify(row, True, [])["role"] == "excluded"


def test_discovery_checks_a_behind_peer_at_most_once_per_interval(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    peer = {"supported": True, "allow_updates": True, "phase": "idle", "version": None,
            "running_version": A, "checked_at": None}
    actions = _never_checked_peer(remote, peer)
    updates.desktop_exchange({"phase": "available", "version": B[:12], "running_version": A,
                              "checkedAt": "2099-01-01T00:00:00Z"})

    async def scenario():
        for _ in range(3):
            await fleet.snapshot(fresh=True)
            await asyncio.gather(*fleet._discovery_tasks)
        assert actions == ["check"]
        # Ten minutes on: the peer still has not checked, so it is asked again.
        fleet._discovery["peer"] -= fleet.DISCOVERY_SECONDS + 1
        await fleet.snapshot(fresh=True)
        await asyncio.gather(*fleet._discovery_tasks)
        assert actions == ["check", "check"]
    asyncio.run(scenario())


def test_discovery_waits_while_an_install_is_verified(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    peer = {"supported": True, "allow_updates": True, "phase": "idle", "version": None,
            "running_version": A, "checked_at": None}
    actions = _never_checked_peer(remote, peer)
    updates.desktop_exchange({"phase": "available", "version": B[:12], "running_version": A})
    fleet.progress.begin("verifying", [{"device_id": "peer", "device_name": "Peer"}])

    async def scenario():
        await fleet.snapshot()
        assert not fleet._discovery_tasks
    asyncio.run(scenario())
    assert actions == []


def test_refusals_hold_restart_until_a_later_install_completes(setup):
    values, remote = setup
    values[updates.ALL_KEY] = "true"
    peer = {"supported": True, "allow_updates": True, "phase": "available", "version": B[:12],
            "running_version": A, "checked_at": time.time()}

    async def call(_device, _kind, body, **kwargs):
        if body["operation"] == "status":
            return dict(peer)
        return {"accepted": False, "reason": "Busy with active sessions; retry when finished"}

    remote.side_effect = call
    updates.desktop_exchange({"phase": "available", "version": B[:12], "running_version": A})

    async def scenario():
        await fleet.dispatch("download")
        assert fleet._refusals["peer"] == "Busy with active sessions; retry when finished"
        peer["phase"] = "idle"
        state = await fleet.snapshot(fresh=True)
        row = next(row for row in state["workers"] if row["device_id"] == "peer")
        assert row["role"] == "unknown" and row["label"].startswith("Refused: Busy")
        assert state["refusals"] == fleet._refusals
    asyncio.run(scenario())


def test_container_status_reports_when_the_feed_was_checked(setup, monkeypatch, tmp_path):
    import json
    import os
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(tmp_path))
    (tmp_path / "status.json").write_text(json.dumps({"phase": "idle", "checked_at": 1234.5}))
    assert updates.status()["checked_at"] == 1234.5
    # Older supervisors do not say; an idle file's time stands in.
    (tmp_path / "status.json").write_text(json.dumps({"phase": "idle"}))
    os.utime(tmp_path / "status.json", (99.0, 99.0))
    assert updates.status()["checked_at"] == 99.0
    (tmp_path / "status.json").write_text(json.dumps({"phase": "downloading"}))
    assert updates.status()["checked_at"] is None


def test_desktop_bridge_converts_the_shells_check_time(setup):
    updates.desktop_exchange({"phase": "idle", "checkedAt": "1970-01-01T00:16:40Z"})
    assert updates.status()["checked_at"] == 1000.0
    updates.desktop_exchange({"phase": "idle", "checkedAt": "not a time"})
    assert updates.status()["checked_at"] is None


# #1818: a restart refused because work is running says why, names the work,
# and can wait for it or stop it first.
BUSY = [{"session_id": "s1", "title": "Fix login", "tasks": ["Run tests"]}]


@pytest.fixture
def busy(setup, monkeypatch):
    running = ["s1"]
    monkeypatch.setattr(desktop_lifecycle, "active_session_ids", lambda: list(running))
    monkeypatch.setattr(desktop_lifecycle, "busy_work", lambda: BUSY if running else [])
    monkeypatch.setattr(fleet, "DEFER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(fleet, "_deferred", None)
    return running


def test_refused_restart_names_the_running_work_and_leaves_no_failed_install(busy):
    result = asyncio.run(fleet.dispatch("install"))
    row = result["results"][-1]
    assert row["accepted"] is False
    assert row["reason"] == "Busy with running work: Fix login"
    assert row["busy"] == BUSY
    assert updates._desktop_request is None
    # Nothing restarted, so there is no install whose startup "could not be confirmed".
    assert fleet.progress.load() is None
    assert asyncio.run(fleet.snapshot())["operation"] is None


def test_restart_when_work_finishes_waits_then_installs(busy):
    async def scenario():
        result = await fleet.dispatch("install", "when_idle")
        row = result["results"][-1]
        assert row["accepted"] and row["deferred"] and row["reason"] == "Restarts when running work finishes"
        assert fleet.progress.load() is None  # nothing to verify yet
        state = await fleet.snapshot()
        assert state["workers"][0]["label"] == "Restarts when work finishes"
        await asyncio.sleep(0.05)
        assert updates._desktop_request is None  # still busy: no restart
        busy.clear()
        for _ in range(100):
            if updates._desktop_request:
                break
            await asyncio.sleep(0.01)
        assert updates._desktop_request["action"] == "install"
        assert fleet._deferred is None
    asyncio.run(scenario())


def test_cancel_drops_a_waiting_restart(busy):
    async def scenario():
        await fleet.dispatch("install", "when_idle")
        assert fleet._deferred is not None
        result = await fleet.dispatch("cancel")
        assert result["results"][-1] == {"device_id": "self", "device_name": "Current worker",
                                         "accepted": True, "reason": "Scheduled restart cancelled"}
        busy.clear()
        await asyncio.sleep(0.05)
        assert fleet._deferred is None and updates._desktop_request is None
    asyncio.run(scenario())


def test_stop_them_and_restart_stops_each_session_then_installs(busy, monkeypatch):
    stopped = []
    async def stop(sid, source=None):
        assert source == "worker_update"
        stopped.append(sid)
        busy.clear()
    monkeypatch.setattr(desktop_lifecycle, "stop_session_work", stop)
    async def scenario():
        result = await fleet.dispatch("install", "stop")
        assert result["results"][-1]["reason"] == "Stopping running work, then restarting"
        for _ in range(100):
            if updates._desktop_request:
                break
            await asyncio.sleep(0.01)
    asyncio.run(scenario())
    assert stopped == ["s1"]
    assert updates._desktop_request["action"] == "install"


def test_a_mode_only_applies_to_install(setup):
    with pytest.raises(ValueError, match="Unknown update mode"):
        asyncio.run(fleet.dispatch("download", "stop"))
