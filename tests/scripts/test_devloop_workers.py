"""Two disposable workers use real pairing, never the installed worker's state."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.devloop import __main__ as devloop
from scripts.devloop import guardian, seed, workers


def test_two_worker_cli_accepts_the_documented_command(monkeypatch):
    seen = []
    monkeypatch.setattr(devloop, "up", lambda parser, args: seen.append(args) or 0)
    assert devloop.main(["up", "--workers", "2", "--host", "0.0.0.0",
                         "--browser-host", "172.17.0.2"]) == 0
    assert seen[0].workers == 2


def _args(tmp_path, **overrides):
    values = {"host": ipaddress.ip_address("127.0.0.1"), "browser_host": None,
              "ttl_seconds": 60, "port": None, "vite_port": None, "no_vite": False,
              "transcript_producer": False, "voice_engine": "gpt-live", "seed": "default",
              "status": tmp_path / "dev.json", "workers": 2, "ci": False}
    return argparse.Namespace(**{**values, **overrides})


def test_workers_have_private_state_and_explicit_protected_peer_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/owner/live-data")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-inherited")
    monkeypatch.setenv("APP_VERSION", "host-worker-build")
    monkeypatch.setenv("CLUSTER_ADVERTISE_URL", "http://owner-live:8001")
    args = _args(tmp_path, port=18880, vite_port=18881)
    fake = SimpleNamespace(url="http://127.0.0.1:9/v1")
    voice = SimpleNamespace(url="ws://127.0.0.1:10")
    used = {18880, 18881, 9, 10}
    envs = [workers.worker_environment(args, tmp_path / name, index, fake, voice,
                                      used_ports=used)[0]
            for index, name in enumerate(("worker-a", "worker-b"))]
    all_ports = []
    for env in envs:
        all_ports.extend(int(env[key]) for key in ("PORT", "SHUFFLE_PROTECTED_PORT", "SHUFFLE_PREVIEW_PORT"))
        assert env["CLUSTER_ADVERTISE_URL"] == "http://127.0.0.1:" + env["SHUFFLE_PROTECTED_PORT"]
        assert env["SHUFFLE_PROTECTED_PORT"] != env["PORT"]
        assert "DATABASE_URL" not in env and "APP_VERSION" not in env
        assert len(env["SHUFFLE_DESKTOP_LISTENER_TOKEN"]) == 64
        data = Path(env["DATA_DIR"])
        assert (data.parent / ".branch-preview").read_text() == str(data.resolve())
        assert Path(env["SHUFFLE_DEV_REMOTES_DIR"]) == tmp_path / "remotes"
        assert not (tmp_path / "remotes").is_relative_to(Path(env["WORKSPACE_DIR"]))
    assert len(all_ports) == len(set(all_ports))
    for key in ("DATA_DIR", "HOME", "WORKSPACE_DIR", "SHUFFLE_DESKTOP_LISTENER_TOKEN"):
        assert envs[0][key] != envs[1][key]
    assert envs[0]["SHUFFLE_DEV_SEED"] == "default"
    assert envs[1]["SHUFFLE_DEV_SEED"] == "empty"


def test_fixed_ports_pin_worker_a_and_give_worker_b_the_next_pair(tmp_path):
    """A container publishes one small range; both workers must land inside it."""
    args = _args(tmp_path, port=18080, vite_port=18081)
    fake = SimpleNamespace(url="http://127.0.0.1:9/v1")
    voice = SimpleNamespace(url="ws://127.0.0.1:10")
    used = {18080, 18081, 9, 10}
    pairs = []
    for index, name in enumerate(("worker-a", "worker-b")):
        env, vite = workers.worker_environment(args, tmp_path / name, index, fake, voice,
                                               used_ports=used)
        pairs.append((int(env["SHUFFLE_PREVIEW_PORT"]), vite))
    assert pairs == [(18080, 18081), (18082, 18083)]




def test_single_worker_keeps_its_status_and_pair_reporters_can_publish_to_a_sink(tmp_path):
    args = _args(tmp_path)
    fake = SimpleNamespace(url="http://127.0.0.1:9", log_path=tmp_path / "log",
                           scenarios=SimpleNamespace(directory=tmp_path, load=dict))
    env = {"SHUFFLE_PREVIEW_PORT": "18880", "PORT": "18882", "SHUFFLE_PROTECTED_PORT": "18881",
           "DATA_DIR": str(tmp_path / "data"), "WORKSPACE_DIR": str(tmp_path / "workspace")}
    instance = devloop.Instance(args, tmp_path, env, fake, None)
    instance.write(state="ready")
    assert json.loads(args.status.read_text())["state"] == "ready"
    updates = []
    paired = devloop.Instance(args, tmp_path, env, fake, None,
                              status_writer=lambda data: updates.append(dict(data)))
    paired.write(state="restarting")
    paired.retire()
    paired.write(state="ready")
    assert [data["state"] for data in updates] == ["restarting", "stopping"]
    assert json.loads(args.status.read_text())["state"] == "ready"  # the sink didn't overwrite it


def test_worker_b_seed_creates_no_projects_or_chats():
    assert asyncio.run(seed.seed_empty()) == {"projects": {}, "chats": {}}


def test_owner_requests_use_the_real_listener_credential_not_origin(monkeypatch):
    connection = Mock()
    connection.getresponse.return_value = SimpleNamespace(status=200, read=lambda: b'{"ok": true}')
    factory = Mock(return_value=connection)
    monkeypatch.setattr(workers.http.client, "HTTPConnection", factory)
    instance = SimpleNamespace(env={"PORT": "18882", "SHUFFLE_DESKTOP_LISTENER_TOKEN": "private-test-token"})
    assert workers.owner_request(instance, "POST", "/api/v1/network/create", {"name": "Dev"}) == {"ok": True}
    factory.assert_called_once_with("127.0.0.1", 18882, timeout=5)
    headers = connection.request.call_args.kwargs["headers"]
    assert headers == {"X-Shuffle-Desktop-Listener": "private-test-token", "Content-Type": "application/json"}
    connection.close.assert_called_once()
    connection.getresponse.return_value = SimpleNamespace(status=403, read=lambda: b"do not log this credential")
    with pytest.raises(RuntimeError, match="HTTP 403") as error:
        workers.owner_request(instance, "GET", "/private")
    assert "credential" not in str(error.value)


def test_pairing_calls_normal_create_grant_and_enroll_without_fixture_ids(monkeypatch):
    a = SimpleNamespace(env={"CLUSTER_ADVERTISE_URL": "http://127.0.0.1:18881"})
    b = SimpleNamespace(env={"CLUSTER_ADVERTISE_URL": "http://127.0.0.1:18891"})
    request = Mock(side_effect=[{}, {"code": "one-time-test-code"}, {}])
    monkeypatch.setattr(workers, "owner_request", request)
    workers.begin_pairing([a, b])
    calls = request.call_args_list
    assert [(c.args[0], c.args[1], c.args[2]) for c in calls] == [
        (a, "POST", "/api/v1/network/create"),
        (a, "POST", "/api/v1/pairing/grants"),
        (b, "POST", "/api/v1/network/enroll")]
    assert calls[1].args[3] == {"purpose": "network:join", "endpoint_url": a.env["CLUSTER_ADVERTISE_URL"]}
    assert calls[2].args[3] == {"endpoint_url": a.env["CLUSTER_ADVERTISE_URL"],
                              "code": "one-time-test-code", "device_name": "Dev worker B"}


def _online_api():
    return {index: {
        "status": {"network": {"network_id": "dev-network", "bootstrap_state": "ready"},
                   "device": {"device_id": f"device-{index}"}, "instance_id": f"real-instance-{index}",
                   "members": [{"device_id": f"device-{1-index}", "status": "active", "online": True}]},
        "installs": {"installs": [{"id": f"device-{1-index}", "online": True, "approved": True, "is_self": False}]}}
            for index in (0, 1)}


@pytest.mark.parametrize("broken", [None, "offline-a", "offline-b", "not-approved", "revoked", "wrong-network", "syncing"])
def test_ready_requires_both_status_and_installs_to_attest_the_peer(monkeypatch, broken):
    api = _online_api()
    if broken == "offline-a":
        api[0]["status"]["members"][0]["online"] = False
    elif broken == "offline-b":
        api[1]["installs"]["installs"][0]["online"] = False
    elif broken == "not-approved":
        api[1]["installs"]["installs"][0]["approved"] = False
    elif broken == "revoked":
        api[1]["status"]["members"][0]["status"] = "revoked"
    elif broken == "wrong-network":
        api[1]["status"]["network"]["network_id"] = "other"
    elif broken == "syncing":
        api[1]["status"]["network"]["bootstrap_state"] = "syncing"
    monkeypatch.setattr(workers, "owner_request",
                        lambda i, method, path: api[i]["status" if path.endswith("status") else "installs"])
    result = workers.online_pair([0, 1])
    if broken:
        assert result is None
    else:
        assert result["network_id"] == "dev-network"
        assert [row["instance_id"] for row in result["workers"]] == ["real-instance-0", "real-instance-1"]
        assert all(row["peer_online"] and row["peer_approved"] for row in result["workers"])


def test_aggregate_status_waits_for_pairing_and_rechecks_after_a_restart(tmp_path):
    args = _args(tmp_path)
    group = workers.WorkerStatus(args, tmp_path, {"pid": 123}, threading.Event())
    a = {"state": "ready", "access_link": "http://worker-a/link", "app": {"pid": 10}}
    b = {"state": "ready", "access_link": "http://worker-b/link", "app": {"pid": 20}}
    group.update(0, a)
    group.update(1, b)
    status = json.loads(args.status.read_text())
    assert status["state"] == "pairing"
    assert status["access_link"] == a["access_link"]  # existing journey tooling still targets A
    assert [row["app"]["pid"] for row in status["workers"]] == [10, 20]
    group.paired({"network_id": "real-network"})
    assert json.loads(args.status.read_text())["state"] == "ready"
    group.update(1, {**b, "state": "restarting"})
    assert json.loads(args.status.read_text())["network"] is None
    group.update(1, b)
    assert json.loads(args.status.read_text())["state"] == "pairing"
    group.retire()
    group.paired({"network_id": "real-network"})
    group.update(0, a)
    assert json.loads(args.status.read_text())["state"] == "stopping"


@pytest.mark.parametrize("reported", [True, False])
def test_guardian_cleans_both_workers_and_unreported_postmasters(tmp_path, monkeypatch, reported):
    scratch = tmp_path / "ai-shuffle-preview-test"
    scratch.mkdir()
    status_path = tmp_path / "dev.json"
    launcher = 999001
    (scratch / guardian.SCRATCH_MARKER).write_text(json.dumps(
        {"pid": launcher, "start": "launch-start", "uid": os.getuid()}))
    pids = {101, 102, 103, 201, 202, 203}
    records = [{key: {"pid": base + n, "pid_identity": "child-start"}
                for n, key in enumerate(("app", "vite", "postgres"), 1)} for base in (100, 200)]
    if not reported:
        for record in records:
            record.pop("postgres")
    devloop.write_private_json(status_path, {"pid": launcher, "pid_identity": "launch-start", "workers": records})
    killed = []
    monkeypatch.setattr(guardian, "alive", lambda pid, start: pid in pids)
    monkeypatch.setattr(guardian, "_kill_group", lambda pid: (killed.append(pid), pids.remove(pid)))
    seen = []
    def postmaster(path):
        seen.append(path)
        return {scratch / "worker-a": 103, scratch / "worker-b": 203}.get(path)
    monkeypatch.setattr(guardian, "_postmaster", postmaster)
    guardian.cleanup(status_path, scratch, launcher, "launch-start")
    assert sorted(killed) == [101, 102, 103, 201, 202, 203]
    assert seen == [scratch, scratch / "worker-a", scratch / "worker-b"]
    assert not status_path.exists() and not scratch.exists()


@pytest.mark.parametrize("failure", [None, "supervisor", "pairing", "app-exit"])
def test_two_worker_lease_tears_everything_down_on_ttl_or_failure(tmp_path, monkeypatch, failure):
    """Exercise the real coordinator, not just status fixtures; children are stand-ins."""
    import time

    from app import preview_run
    from docker import container_runtime as runtime
    from scripts.devloop import fake_ai, fake_voice

    args = _args(tmp_path, ttl_seconds=10, claude_cli="none", fake_voice_host="127.0.0.1",
                 latency_ms=0, tps=0, poll=True)
    fake = SimpleNamespace(url="http://127.0.0.1:9", log_path=tmp_path / "fake.jsonl",
                           scenarios=SimpleNamespace(directory=tmp_path, load=dict), close=Mock())
    voice = SimpleNamespace(url="ws://127.0.0.1:10", port=10, log_path=tmp_path / "voice.jsonl",
                            scenarios=SimpleNamespace(directory=tmp_path, load=dict), close=Mock())
    monkeypatch.setattr(fake_ai, "FakeAI", lambda **kwargs: fake)
    monkeypatch.setattr(fake_voice, "FakeVoice", lambda **kwargs: voice)
    monkeypatch.setattr(guardian, "spawn_guardian", lambda *args: None)
    monkeypatch.setattr(devloop, "_ensure_frontend", Mock())
    monkeypatch.setattr(devloop, "answers_http", lambda *a, **kw: True)
    watcher_instances = []
    def watcher(*a, **kw):
        value = Mock()
        watcher_instances.append(value)
        return value
    monkeypatch.setattr(devloop, "Watcher", watcher)
    vites = []
    def popen(*a, **kw):
        value = SimpleNamespace(pid=999101 + len(vites))
        vites.append(value)
        return value
    monkeypatch.setattr(workers, "subprocess", SimpleNamespace(Popen=popen))
    stopped_vites = []
    monkeypatch.setattr(runtime, "stop_group", lambda process, timeout: stopped_vites.append(process))
    monkeypatch.setattr(runtime, "Database", lambda *a, **kw: SimpleNamespace(process=None))
    leases, exited = [], []
    started = time.monotonic()
    def supervise(command, env, database, **kwargs):
        index = 0 if "worker-a" in env["DATA_DIR"] else 1
        leases.append(kwargs)
        home = Path(env["DATA_DIR"]).parent
        (home / "access.json").write_text(json.dumps({"url": "http://127.0.0.1/link",
                                                      "session_id": "access", "token": "test-token"}))
        (home / "seed.json").write_text(json.dumps({"ok": True, "seed": env["SHUFFLE_DEV_SEED"]}))
        kwargs["on_start"](SimpleNamespace(pid=999201 + index, poll=lambda: None))
        try:
            if failure == "supervisor" and index == 0:
                raise RuntimeError("test supervisor failure")
            while not kwargs["stopped"].wait(0.01):
                if index == 0 and time.monotonic() - started > 1.3:
                    return 9 if failure == "app-exit" else 0  # Either lease ending stops both.
                assert time.monotonic() - started < 5, "coordinator failed to stop the pair"
        finally:
            exited.append(index)
    monkeypatch.setattr(preview_run, "supervise", supervise)
    begin = Mock(side_effect=RuntimeError("test enrollment failure") if failure == "pairing" else None)
    monkeypatch.setattr(workers, "begin_pairing", begin)
    monkeypatch.setattr(workers, "online_pair", lambda instances: {"network_id": "paired"})
    # up_workers owns runtime.STOP; restore it so no process-global test state leaks.
    monkeypatch.setattr(runtime, "STOP", threading.Event())
    assert workers.up_workers(args) == (1 if failure else 0)
    assert sorted(exited) == [0, 1]
    assert len(leases) == 2 and leases[0]["stopped"] is leases[1]["stopped"]
    assert all(0 < lease["ttl"] <= 10 for lease in leases)
    assert leases[0]["restart"] is not leases[1]["restart"]
    assert len(watcher_instances) == 2
    for value in watcher_instances:
        value.start.assert_called_once()
        value.stop.assert_called_once()
    assert stopped_vites == vites and len(vites) == 2
    fake.close.assert_called_once()
    voice.close.assert_called_once()
    assert not args.status.exists()
