"""The dev loop launcher (scripts/devloop/__main__.py) and its seed bookkeeping."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.devloop import __main__ as devloop
from scripts.devloop import seed as seed_module


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


class _Process:
    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid, self.returncode = pid, returncode

    def poll(self) -> int | None:
        return self.returncode


def _instance(tmp_path: Path, gate: devloop.RestartGate | None = None) -> devloop.Instance:
    fake = SimpleNamespace(url="http://127.0.0.1:9/v1", log_path=tmp_path / "log.jsonl",
                           scenarios=SimpleNamespace(directory=tmp_path, load=dict))
    args = SimpleNamespace(host=ipaddress.ip_address("127.0.0.1"), browser_host=None,
                           ttl_seconds=60, status=tmp_path / "status" / "dev.json")
    env = {"SHUFFLE_PREVIEW_PORT": "18880", "PORT": "18882", "SHUFFLE_PROTECTED_PORT": "18881",
           "DATA_DIR": str(tmp_path / "data"), "WORKSPACE_DIR": str(tmp_path / "workspace")}
    return devloop.Instance(args, tmp_path, env, fake, None, gate)


# ─── readiness announcement ─────────────────────────────────────────────────


def test_link_and_seed_are_announced_when_the_first_start_crashed(tmp_path, monkeypatch):
    """A first start that dies (say a syntax error) must not lose the link: the
    first start that gets ready announces it, whichever start that is."""
    healthy: set[int] = set()
    current: list[_Process] = []
    monkeypatch.setattr(devloop, "answers_http",
                        lambda *a, **k: bool(current) and current[-1].pid in healthy)
    (tmp_path / "access.json").write_text(json.dumps({"url": "http://10.0.0.30:18880/link"}))
    (tmp_path / "seed.json").write_text(json.dumps({"ok": True, "seed": "default"}))
    restart = threading.Event()
    gate = devloop.RestartGate(restart)
    instance = _instance(tmp_path, gate)
    announced = []
    original = instance._announce
    monkeypatch.setattr(instance, "_announce",
                        lambda seconds: (announced.append(seconds), original(seconds)))
    reporter = threading.Thread(target=instance.report, daemon=True)
    reporter.start()
    try:
        crashed = _Process(101, returncode=1)
        current.append(crashed)
        instance.on_app_start(crashed)
        instance.on_app_exit(crashed)
        # A crash leaves nothing to cut: the fix may restart it ...
        assert gate.passing and not gate.is_open
        assert gate.request() is False and restart.is_set()
        restart.clear()
        fixed = _Process(102)
        current.append(fixed)
        instance.on_app_start(fixed)
        # ... but the start the fix triggers seeds, so it is held closed again.
        assert gate.request() is True and not restart.is_set()
        healthy.add(102)
        _wait_for(lambda: instance.data["state"] == "ready")
        _wait_for(lambda: gate.is_open)
        assert restart.is_set()  # the save held during the seeding start, applied once
        restart.clear()
        assert len(announced) == 1 and instance.starts == 2
        status = json.loads(instance.args.status.read_text())
        assert status["access_link"] == "http://10.0.0.30:18880/link"
        assert status["seed"] == {"ok": True, "seed": "default"}

        # A seed a later start ran (a deferred pair-b seed) reaches the status.
        (tmp_path / "seed.json").write_text(json.dumps({"ok": True, "seed": "pair-b", "chats": {}}))
        again = _Process(103)
        current.append(again)
        healthy.add(103)
        instance.on_app_start(again)
        _wait_for(lambda: instance.data["state"] == "ready" and instance.data["app"]["pid"] == 103)
        _wait_for(lambda: json.loads(instance.args.status.read_text())["state"] == "ready")
        assert len(announced) == 1  # later restarts only report "ready again"
        assert json.loads(instance.args.status.read_text())["seed"]["seed"] == "pair-b"
    finally:
        instance.done.set()
        reporter.join(5)


def test_restarts_wait_for_the_first_start_and_its_seed():
    restart = threading.Event()
    gate = devloop.RestartGate(restart)
    assert gate.request() is True and gate.request() is True  # held, folded
    assert not restart.is_set()
    gate.open()
    assert restart.is_set()  # one restart for everything seen meanwhile
    restart.clear()
    gate.open()
    assert not restart.is_set()
    assert gate.request() is False and restart.is_set()


def test_a_first_start_that_never_settles_lets_changes_through_after_the_grace(
        tmp_path, monkeypatch):
    monkeypatch.setattr(devloop, "answers_http", lambda *a, **k: False)
    monkeypatch.setattr(devloop, "FIRST_START_GRACE_SECONDS", 0.1)
    gate = devloop.RestartGate(threading.Event())
    instance = _instance(tmp_path, gate)
    reporter = threading.Thread(target=instance.report, daemon=True)
    reporter.start()
    try:
        instance.on_app_start(_Process(201))
        _wait_for(lambda: gate.passing)
        assert not gate.is_open  # only this hung start; the next one is held again
    finally:
        instance.done.set()
        reporter.join(5)


def test_a_crash_before_the_seed_never_opens_the_gate_for_good():
    """Regression: the first start crashed before seeding, so the fix's start
    seeds; a second save during that start must wait, not cut the seed."""
    restart = threading.Event()
    gate = devloop.RestartGate(restart)
    gate.started()
    assert gate.request() is True  # a save while the first start runs: held
    gate.release()  # that start crashed: the held save restarts it
    assert restart.is_set() and not gate.is_open
    restart.clear()
    gate.started()  # the fixed start, which runs the seed
    assert gate.request() is True and not restart.is_set()
    assert gate.request() is True and not restart.is_set()  # folded
    gate.open()  # ready: the seed is done
    assert restart.is_set()
    restart.clear()
    gate.started()  # later starts pass straight through
    assert gate.request() is False and restart.is_set()


def test_a_crash_after_the_gate_opened_changes_nothing():
    restart = threading.Event()
    gate = devloop.RestartGate(restart)
    gate.open()
    gate.release()
    assert gate.is_open and not gate.passing


# ─── status file ────────────────────────────────────────────────────────────


def test_status_directory_is_private_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert devloop.status_directory() == tmp_path / "ai-shuffle-devloop"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert devloop.status_directory() == (
        Path(tempfile.gettempdir()) / f"ai-shuffle-devloop-{os.getuid()}")
    assert devloop.default_status_path().parent == devloop.status_directory()


def test_private_dir_is_created_0700_and_shared_ones_are_refused(tmp_path):
    fresh = tmp_path / "fresh"
    devloop.ensure_private_dir(fresh)
    assert fresh.stat().st_mode & 0o777 == 0o700
    devloop.ensure_private_dir(fresh)  # ours: accepted again

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(devloop.StatusError, match="chmod 700"):
        devloop.ensure_private_dir(shared)
    link = tmp_path / "link"
    link.symlink_to(fresh)
    with pytest.raises(devloop.StatusError):
        devloop.ensure_private_dir(link)


def test_private_dir_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    directory = tmp_path / "theirs"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(directory).st_uid + 1)
    with pytest.raises(devloop.StatusError, match="owned by you"):
        devloop.ensure_private_dir(directory)


def test_status_write_never_follows_a_planted_link(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    path = tmp_path / "status" / "dev.json"
    devloop.ensure_private_dir(path.parent)
    path.with_name("dev.json.tmp").symlink_to(victim)  # a stale or planted tmp
    devloop.write_private_json(path, {"pid": 1})
    path.unlink()
    path.symlink_to(victim)  # the status path itself
    devloop.write_private_json(path, {"pid": 2})
    assert victim.read_text() == "keep"
    assert not path.is_symlink() and json.loads(path.read_text()) == {"pid": 2}
    assert path.stat().st_mode & 0o777 == 0o600


def test_down_and_status_refuse_a_status_file_they_do_not_own(tmp_path, monkeypatch, capsys):
    from app.core.temp_scratch import process_identity

    path = tmp_path / "dev.json"
    path.write_text(json.dumps({"pid": os.getpid(),
                                "pid_identity": process_identity(os.getpid())}))
    kill = Mock()
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)
    for command in ("down", "status"):
        assert devloop.main([command, "--status", str(path)]) == 2
        assert "not a regular file owned by you" in capsys.readouterr().err
    kill.assert_not_called()


def test_down_refuses_a_symlinked_status_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"pid": 1}))
    path = tmp_path / "dev.json"
    path.symlink_to(target)
    kill = Mock()
    monkeypatch.setattr(os, "kill", kill)
    assert devloop.main(["down", "--status", str(path)]) == 2
    assert "Refusing the status file" in capsys.readouterr().err
    kill.assert_not_called()


def test_down_removes_a_stale_status_file_it_owns(tmp_path, capsys):
    path = tmp_path / "dev.json"
    path.write_text(json.dumps({"pid": 2 ** 22 + 7, "pid_identity": "1"}))
    assert devloop.main(["down", "--status", str(path)]) == 0
    assert "stale" in capsys.readouterr().out and not path.exists()


# ─── seed bookkeeping ───────────────────────────────────────────────────────


@pytest.fixture
def seed_file(tmp_path, monkeypatch, bare_db):
    path = tmp_path / "seed.json"
    monkeypatch.setenv("SHUFFLE_DEV_SEED_FILE", str(path))
    return path


def test_seed_runs_once_and_restarts_reuse_it(seed_file, monkeypatch):
    calls = AsyncMock(return_value={"chats": {"long": "s1"}})
    monkeypatch.setitem(seed_module.SEEDS, "default", calls)
    asyncio.run(seed_module.seed("default"))
    assert json.loads(seed_file.read_text()) == {"ok": True, "seed": "default",
                                                 "chats": {"long": "s1"}}
    asyncio.run(seed_module.seed("default"))
    assert calls.await_count == 1
    assert json.loads(seed_file.read_text())["reused"] is True


def test_an_interrupted_seed_is_reported_never_re_run(seed_file, monkeypatch):
    """A start SIGKILLed mid-seed leaves "started": the next start must not seed
    a second set of chats on top of the first half."""
    from app.db import database

    class Killed(BaseException):
        """Stands in for the SIGKILL: no ``except Exception`` handler runs."""

    started = []

    async def killed_mid_seed():
        started.append(json.loads(database.get_system_setting("dev_seed:default")))
        raise Killed

    monkeypatch.setitem(seed_module.SEEDS, "default", killed_mid_seed)
    with pytest.raises(Killed):
        asyncio.run(seed_module.seed("default"))
    assert started == [{"state": "started"}]  # recorded before any seed write

    again = AsyncMock(return_value={})
    monkeypatch.setitem(seed_module.SEEDS, "default", again)
    asyncio.run(seed_module.seed("default"))
    again.assert_not_awaited()
    result = json.loads(seed_file.read_text())
    assert result["ok"] is False
    assert result["error"] == ("partial seed from an interrupted start; run `down` then `up` "
                               "for a fresh instance")


def test_a_failed_seed_is_reported_on_later_starts(seed_file, monkeypatch):
    monkeypatch.setitem(seed_module.SEEDS, "default",
                        AsyncMock(side_effect=RuntimeError("fake AI down")))
    asyncio.run(seed_module.seed("default"))
    assert json.loads(seed_file.read_text())["error"] == "RuntimeError: fake AI down"
    again = AsyncMock(return_value={})
    monkeypatch.setitem(seed_module.SEEDS, "default", again)
    asyncio.run(seed_module.seed("default"))
    again.assert_not_awaited()
    error = json.loads(seed_file.read_text())["error"]
    assert "RuntimeError: fake AI down" in error and "`down` then `up`" in error


def _provider_api(monkeypatch, rows: list[dict]):
    from app.api import model_providers as api
    from app.db import model_providers as db

    monkeypatch.setattr(db, "get_all_model_providers", lambda: rows)
    create = AsyncMock(return_value=SimpleNamespace(id="new"))
    fetch = AsyncMock(return_value=SimpleNamespace(models=[{"id": "fake-ai"}]))
    update = AsyncMock()
    monkeypatch.setattr(api, "create_provider", create)
    monkeypatch.setattr(api, "_fetch_models", fetch)
    monkeypatch.setattr(api, "update_provider", update)
    return create, fetch, update


def test_fake_provider_rediscovers_an_uncurated_existing_row(monkeypatch):
    create, fetch, update = _provider_api(
        monkeypatch, [{"id": "p1", "name": seed_module.PROVIDER_NAME, "default_model": None}])
    assert asyncio.run(seed_module._fake_provider("http://127.0.0.1:9/v1")) == ("p1", "fake-ai")
    create.assert_not_awaited()
    fetch.assert_awaited_once_with("p1")
    assert update.await_args.args[0] == "p1"
    assert update.await_args.args[1].default_model == "fake-ai"


def test_fake_provider_reuses_a_curated_row_and_creates_a_missing_one(monkeypatch):
    create, fetch, _ = _provider_api(
        monkeypatch, [{"id": "p1", "name": seed_module.PROVIDER_NAME, "default_model": "m"}])
    assert asyncio.run(seed_module._fake_provider("http://127.0.0.1:9/v1")) == ("p1", "m")
    create.assert_not_awaited()
    fetch.assert_not_awaited()
    create, fetch, _ = _provider_api(monkeypatch, [])
    assert asyncio.run(seed_module._fake_provider("http://127.0.0.1:9/v1")) == ("new", "fake-ai")
    create.assert_awaited_once()
    fetch.assert_awaited_once_with("new")


# ─── stopping, stale status and --ci ────────────────────────────────────────


def test_a_stop_says_stopping_at_once_and_nothing_rewrites_it(tmp_path):
    """TaskStop SIGKILLs the launcher about a second after its SIGTERM: the
    status must stop saying "ready" before the slow teardown starts."""
    instance = _instance(tmp_path)
    instance.write(state="ready", access_link="http://127.0.0.1:1/link")
    instance.retire()
    assert json.loads(instance.args.status.read_text())["state"] == "stopping"
    instance.write(state="ready")  # a late reporter thread
    instance.on_app_exit(_Process(5, returncode=1))
    assert json.loads(instance.args.status.read_text())["state"] == "stopping"


def test_launcher_handles_hangup_as_a_stop():
    import signal

    assert {signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= set(devloop.STOP_SIGNALS)


def test_only_the_launcher_that_wrote_the_status_removes_it(tmp_path):
    path = tmp_path / "dev.json"
    path.write_text(json.dumps({"pid": 4242, "pid_identity": "77"}))
    assert devloop.remove_own_status(path, 4243) is False  # a newer `up` wrote it
    assert devloop.remove_own_status(path, 4242, "78") is False  # a reused pid
    assert path.exists()
    assert devloop.remove_own_status(path, 4242, "77") is True and not path.exists()
    assert devloop.remove_own_status(path, 4242) is False  # already gone


def test_status_removes_a_stale_file_instead_of_showing_it(tmp_path, capsys):
    path = tmp_path / "dev.json"
    path.write_text(json.dumps({"pid": 2 ** 22 + 7, "pid_identity": "1", "state": "ready"}))
    assert devloop.main(["status", "--status", str(path)]) == 1
    assert "Stale" in capsys.readouterr().out and not path.exists()


def test_status_wait_returns_once_ready_and_refuses_a_stopping_instance(tmp_path, capsys):
    from app.core.temp_scratch import process_identity

    path = tmp_path / "dev.json"
    live = {"pid": os.getpid(), "pid_identity": process_identity(os.getpid())}
    path.write_text(json.dumps({**live, "state": "starting"}))

    def later():
        time.sleep(0.6)
        path.write_text(json.dumps({**live, "state": "ready"}))

    writer = threading.Thread(target=later)
    writer.start()
    assert devloop.main(["status", "--status", str(path), "--wait", "10"]) == 0
    writer.join()
    assert json.loads(capsys.readouterr().out)["state"] == "ready"
    path.write_text(json.dumps({**live, "state": "stopping"}))
    assert devloop.main(["status", "--status", str(path), "--wait", "10"]) == 1
    path.write_text(json.dumps({**live, "state": "starting"}))
    assert devloop.main(["status", "--status", str(path), "--wait", "0.6"]) == 1
    assert "did not get ready" in capsys.readouterr().err


def _parsed(argv):
    parser = Mock()
    parser.error.side_effect = SystemExit
    args = SimpleNamespace(host=ipaddress.ip_address("127.0.0.1"), browser_host=None, port=None,
                           vite_port=None, ttl_seconds=60, no_vite=False, ci=True, seed="default")
    for key, value in argv.items():
        setattr(args, key, value)
    return parser, args


def test_ci_serves_the_stamped_build_on_loopback_only(monkeypatch):
    monkeypatch.setattr(devloop, "stamped_build_problem", lambda: None)
    parser, args = _parsed({})
    devloop._validate(parser, args)
    assert args.no_vite is True
    for bad in ({"host": ipaddress.ip_address("0.0.0.0"),
                 "browser_host": ipaddress.ip_address("10.0.0.5")},
                {"vite_port": 18555}):
        parser, args = _parsed(bad)
        with pytest.raises(SystemExit):
            devloop._validate(parser, args)
    monkeypatch.setattr(devloop, "stamped_build_problem", lambda: "frontend/build has no stamp")
    parser, args = _parsed({})
    with pytest.raises(SystemExit):
        devloop._validate(parser, args)
    assert "has no stamp" in parser.error.call_args.args[0]


def test_ci_flag_defaults_from_the_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(devloop, "up", lambda parser, args: seen.setdefault("ci", args.ci) and 0)
    monkeypatch.setenv("DEVLOOP_CI", "1")
    devloop.main(["up"])
    assert seen["ci"] is True


def _ci_instance(tmp_path, seed):
    instance = _instance(tmp_path)
    instance.args.ci, instance.args.seed = True, "default"
    instance.stopped = threading.Event()
    (tmp_path / "access.json").write_text(json.dumps({"url": "http://127.0.0.1:18880/link"}))
    if seed is not None:
        (tmp_path / "seed.json").write_text(json.dumps(seed))
    return instance


def test_ci_prints_one_ready_line(tmp_path, capsys):
    instance = _ci_instance(tmp_path, {"ok": True, "seed": "default"})
    instance._announce(1.5)
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith(("DEVLOOP_READY", "DEVLOOP_FAILED"))]
    assert len(lines) == 1 and lines[0].startswith("DEVLOOP_READY ")
    record = json.loads(lines[0].split(" ", 1)[1])
    assert record["status"] == str(instance.args.status) and record["seconds"] == 1.5
    assert not instance.stopped.is_set() and instance.failed is None


def test_ci_stops_an_instance_whose_seed_failed(tmp_path, capsys):
    instance = _ci_instance(tmp_path, {"ok": False, "error": "RuntimeError: boom"})
    instance._announce(1.0)
    out = capsys.readouterr().out
    assert "DEVLOOP_FAILED " in out and "DEVLOOP_READY" not in out
    assert "boom" in instance.failed and instance.stopped.is_set()


# ─── seed content ───────────────────────────────────────────────────────────


def test_seeded_repositories_fetch_from_a_local_bare_remote(tmp_path):
    import subprocess

    repo, remote = tmp_path / "workspace" / "dev-sandbox", tmp_path / "remotes" / "dev-sandbox.git"
    seed_module.make_repository(repo, "Dev sandbox", second_branch=True, remote=remote)
    git = ["git", "-C", str(repo)]
    assert subprocess.run([*git, "remote", "get-url", "origin"], capture_output=True, text=True,
                          check=True).stdout.strip() == remote.resolve().as_uri()
    subprocess.run([*git, "fetch", "origin", "--prune"], check=True, capture_output=True)
    upstream = subprocess.run([*git, "rev-parse", "--abbrev-ref", "main@{upstream}"],
                              capture_output=True, text=True, check=True).stdout.strip()
    assert upstream == "origin/main"
    branches = subprocess.run(["git", "-C", str(remote), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True, check=True).stdout.split()
    assert sorted(branches) == ["feature/second", "main"]


def test_seed_ids_are_the_same_on_every_run():
    seed_module._id_counters.clear()
    first = [seed_module.seed_id("chat"), seed_module.seed_id("turn"), seed_module.seed_id("chat")]
    seed_module._id_counters.clear()
    again = [seed_module.seed_id("chat"), seed_module.seed_id("turn"), seed_module.seed_id("chat")]
    assert first == again and len(set(first)) == 3


def test_the_dev_agent_becomes_the_composer_default_for_the_owner(monkeypatch):
    """The preview cookie cannot write preferences; the seed stores the pick for
    the identity the preferences API resolves for an owner (admin) request."""
    from app.core import preference_identity
    from app.db import database

    monkeypatch.setattr(preference_identity, "principal_preference_identity",
                        lambda principal: ("admin", f"owner-of-{principal['kind']}"))
    stored = Mock()
    monkeypatch.setattr(database, "set_user_preference", stored)
    seed_module.make_dev_agent_the_default()
    stored.assert_called_once_with("admin", "owner-of-admin", "composer.profile",
                                   seed_module.DEV_AGENT_ID)


# ─── up --pair ──────────────────────────────────────────────────────────────


def test_a_deferred_seed_writes_nothing_and_runs_on_a_later_start(seed_file, monkeypatch):
    """``pair-b`` waits for B to join A: its first start must not record a seed."""
    from app.db import database

    monkeypatch.setitem(seed_module.SEEDS, "pair-b", AsyncMock(
        side_effect=seed_module.SeedDeferred("worker B seeds once it has joined")))
    asyncio.run(seed_module.seed("pair-b"))
    assert json.loads(seed_file.read_text()) == {
        "ok": True, "seed": "pair-b", "deferred": "worker B seeds once it has joined"}
    assert database.get_system_setting("dev_seed:pair-b") is None
    later = AsyncMock(return_value={"chats": {"b_loose": "b1"}})
    monkeypatch.setitem(seed_module.SEEDS, "pair-b", later)
    asyncio.run(seed_module.seed("pair-b"))
    later.assert_awaited_once()
    assert json.loads(seed_file.read_text())["chats"] == {"b_loose": "b1"}


def test_pair_b_defers_until_enrolled(monkeypatch):
    from app.network import engine

    monkeypatch.setattr(engine, "context", lambda: None)
    with pytest.raises(seed_module.SeedDeferred):
        asyncio.run(seed_module.seed_pair_b())


def test_worker_b_reads_worker_a_remotes_beside_its_data(tmp_path, monkeypatch):
    from app.core.config import settings

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(type(settings), "effective_data_dir", property(lambda self: str(data)))
    assert seed_module._peer_remotes() == {}  # a lone instance: nothing to clone
    (tmp_path / seed_module.PEER_FILE).write_text(json.dumps({"remotes": {"dev-sandbox": "file:///r.git"}}))
    assert seed_module._peer_remotes() == {"dev-sandbox": "file:///r.git"}


def test_worker_b_ids_never_collide_with_worker_a(monkeypatch):
    monkeypatch.setattr(seed_module, "_id_counters", {})
    monkeypatch.setattr(seed_module, "_id_prefix", "")
    a = seed_module.seed_id("chat")
    monkeypatch.setattr(seed_module, "_id_counters", {})
    monkeypatch.setattr(seed_module, "_id_prefix", "pair-b:")
    assert seed_module.seed_id("chat") != a


def test_pair_status_sits_beside_the_single_one(tmp_path):
    single = devloop.default_status_path(tmp_path)
    paired = devloop.default_pair_status_path(tmp_path)
    assert paired.parent == single.parent and paired.name == single.stem + "-pair.json"


def test_a_pair_member_needs_its_launchers_key_and_peer(tmp_path, monkeypatch):
    monkeypatch.delenv("SHUFFLE_DEV_PAIR_KEY", raising=False)
    monkeypatch.setenv("SHUFFLE_DEV_PAIR_PEERS", "http://127.0.0.1:41002")
    with pytest.raises(SystemExit, match="pair key"):
        devloop.pair_member_environment(tmp_path, 41001)
    monkeypatch.setenv("SHUFFLE_DEV_PAIR_KEY", "k" * 64)
    env = devloop.pair_member_environment(tmp_path, 41001)
    assert env == {"SHUFFLE_DEV_PAIR_KEY": "k" * 64, "SHUFFLE_DEV_PAIR_PEERS": "http://127.0.0.1:41002",
                   "CLUSTER_ADVERTISE_URL": "http://127.0.0.1:41001",
                   "SHUFFLE_DEV_OWNER_FILE": str(tmp_path / "owner.json")}
    from app.preview_run import DEV_ENV_KEYS

    assert set(env) <= DEV_ENV_KEYS  # the preview environment accepts every one


def test_offline_switch_holds_and_frees_the_app():
    import signal as signals

    hold, restart = threading.Event(), threading.Event()
    old = devloop.install_offline_switch(hold, restart)
    try:
        os.kill(os.getpid(), devloop.OFFLINE_SIGNAL)
        assert hold.is_set() and restart.is_set()
        restart.clear()
        os.kill(os.getpid(), devloop.ONLINE_SIGNAL)
        assert not hold.is_set() and not restart.is_set()
    finally:
        for sig, handler in old.items():
            signals.signal(sig, handler)


def test_status_and_down_follow_a_pair_status_file(tmp_path, capsys):
    from scripts.devloop import pair

    path = tmp_path / "pair.json"
    path.write_text(json.dumps({"kind": "pair", "pid": 2 ** 22 + 7, "pid_identity": "1"}))
    assert pair.is_pair_status(path)
    assert devloop.main(["down", "--status", str(path)]) == 0
    assert "stale" in capsys.readouterr().out and not path.exists()


def test_pair_refuses_explicit_ports_and_seeds(monkeypatch):
    monkeypatch.setattr(devloop.sys, "platform", "linux")
    with pytest.raises(SystemExit):
        devloop.main(["up", "--pair", "--port", "41000"])
    with pytest.raises(SystemExit):
        devloop.main(["up", "--pair", "--seed", "none"])
