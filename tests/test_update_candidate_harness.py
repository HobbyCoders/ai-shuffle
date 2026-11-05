"""CI-only fixture mounting; no candidate builds or container operations run here."""
import json
import runpy
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import source_archive
from scripts import verify_container_postgres as harness
from scripts.verify_container_postgres import Check


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    build = Mock()
    monkeypatch.setattr(source_archive, "build", build)
    monkeypatch.setattr(harness.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="a" * 40)))
    check = Check("candidate-only", tmp_path)
    # Keep real launch/volume methods so assertions cover the Docker commands,
    # not just arguments to a mocked launch helper. Never contact a daemon.
    check.docker = Mock(return_value=SimpleNamespace(stdout=b"", stderr=b"", returncode=0))
    check.ready = Mock()
    check.stop = Mock()
    check.system_id = Mock(return_value="same-cluster")
    check.sql = Mock(side_effect=["", "retained", "retained"])
    check.passed = Mock()
    return check, build


def mounts(command):
    return [command[index + 1] for index, value in enumerate(command) if value == "--mount"]


def test_candidate_lifecycle_is_built_offline_then_explicitly_installed_and_refreshed(candidate, tmp_path):
    check, build = candidate
    check.updater_lifecycle()
    build.assert_called_once_with("a" * 40, tmp_path / "updater-source.zip")
    initial, preparation, updated = check.containers
    assert check.stop.call_args_list[0].args == (initial,)
    calls = [call.args for call in check.docker.call_args_list]
    prepare = next(call for call in calls if call[-2:] == ("prepare", "/fixture-source.zip"))
    fixture = prepare[-3]
    assert prepare[:6] == ("run", "--name", preparation, "--network", "bridge", "--user")
    assert prepare[6:9] == ("99:100", "--entrypoint", "python")
    assert "DATA_DIR=/data" in prepare and "AI_SHUFFLE_UPDATER_FIXTURE=1" in prepare
    assert fixture == "/updater-candidate-fixture.py"
    fixture_source = (harness.ROOT / "scripts/ci/updater_candidate_fixture.py").resolve(strict=True)
    assert fixture_source.is_file()
    fixture_mount = f"type=bind,src={fixture_source},dst={fixture},readonly"
    assert fixture_mount in mounts(prepare)
    assert f"type=volume,src={check.volumes[0]},dst=/data" in mounts(prepare)
    assert f"type=bind,src={tmp_path / 'updater-source.zip'},dst=/fixture-source.zip,readonly" in mounts(prepare)

    # The same mount belongs to the long-lived lifecycle container, not a
    # temporary docker exec/cp; it is still present after supervisor execve.
    initial_run = next(call for call in calls if call[:4] == ("run", "-d", "--name", initial))
    updated_run = next(call for call in calls if call[:4] == ("run", "-d", "--name", updated))
    assert fixture_mount not in mounts(initial_run)
    assert fixture_mount in mounts(updated_run)
    assert f"type=bind,src={tmp_path / 'updater-ci-public.pem'},dst=/app/docker/application-update-key.pem,readonly" in mounts(updated_run)
    assert "AI_SHUFFLE_UPDATER_FIXTURE=1" in updated_run
    assert "--network" in updated_run and updated_run[updated_run.index("--network") + 1] == "none"
    actions = [call for call in calls if len(call) >= 2 and call[-2] == fixture]
    assert [call[-1] for call in actions] == ["updater-ready", "install", "installed", "refresh-updater", "refreshed", "diagnose"]
    assert all(call[:-2] == ("exec", "--user", "99:100", updated, "python") for call in actions)
    assert check.system_id.call_count == 3
    assert check.sql.call_count == 3
    check.passed.assert_called_once()


def test_candidate_preparation_timeout_is_registered_for_cleanup(candidate):
    check, _ = candidate
    result = check.docker.return_value

    def docker(*args, **kwargs):
        if args[-2:] == ("prepare", "/fixture-source.zip"):
            raise subprocess.TimeoutExpired("docker", 900)
        return result

    check.docker.side_effect = docker
    with pytest.raises(subprocess.TimeoutExpired):
        check.updater_lifecycle()
    initial, preparation = check.containers
    assert check.docker.call_args.args[:2] == ("cp", initial + ":/data/application-updates/build.log")
    check.cleanup()
    calls = [call.args for call in check.docker.call_args_list]
    removed = [call[-1] for call in calls if call[:3] == ("rm", "--force", "--volumes")]
    assert removed == [preparation, initial]
    last_container_removal = max(index for index, call in enumerate(calls) if call[0] == "rm")
    first_volume_removal = next(index for index, call in enumerate(calls) if call[:2] == ("volume", "rm"))
    assert last_container_removal < first_volume_removal


def test_fixture_stays_out_of_packaged_image_and_source_update():
    ignore = (harness.ROOT / ".dockerignore").read_text().splitlines()
    assert "scripts/ci/" in ignore
    dockerfile = (harness.ROOT / "Dockerfile").read_text()
    assert "COPY --chown=root:root scripts/ ./scripts/" in dockerfile
    assert "updater_candidate_fixture" not in dockerfile
    assert not any(path == "scripts" or path.startswith("scripts/") for path in source_archive.RUNTIME_PATHS)


def test_direct_mounted_script_bootstraps_image_imports_without_cwd_or_pythonpath(tmp_path, monkeypatch):
    # Execute the actual __main__ bootstrap from a mount-like location and an
    # unrelated cwd. The unknown action exits before any producing operation.
    mounted = tmp_path / "mount" / "updater-candidate-fixture.py"
    mounted.parent.mkdir()
    mounted.write_bytes((harness.ROOT / "scripts/ci/updater_candidate_fixture.py").read_bytes())
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("AI_SHUFFLE_UPDATER_FIXTURE", "1")
    monkeypatch.setenv("DATA_DIR", "/data")
    monkeypatch.setattr(sys, "path", [str(mounted.parent), *(path for path in sys.path if path != "/app")])
    monkeypatch.setattr(sys, "argv", [str(mounted), "unknown"])
    with pytest.raises(SystemExit, match="Unknown fixture action"):
        runpy.run_path(str(mounted), run_name="__main__")
    assert sys.path[0] == "/app"  # parent of both the packaged app and local docker namespace


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return 1000 + self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 3)


def write_json(path, value):
    # Like the producer, publish via replacement so each generation is distinct.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def test_barrier_waits_for_real_watch_service_before_short_lived_install(tmp_path, monkeypatch):
    from docker import application_updates as updates
    from docker import container_runtime as runtime
    from scripts.ci import updater_candidate_fixture as fixture

    clock = Clock()
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "time", clock)
    monkeypatch.setattr(runtime, "time", clock)
    monkeypatch.setattr(updates, "time", clock)
    image_revision, revision = "a" * 40, "b" * 40
    write_json(tmp_path / "ci-fixture.json", {"image_revision": image_revision, "revision": revision})
    updater = updates.ContainerUpdates(tmp_path, {"revision": image_revision}, b"unused", lambda: True, lambda _: {})
    updater.state = {"pending": revision}  # unit-only input, not a candidate build
    updater.fetch = Mock(side_effect=updates.UpdateError("Offline fixture network"))
    service_times = []

    def tick():
        service_times.append(clock.now)
        updater.service()  # real consumer, including expiry/policy/check publication

    updater.tick = tick
    stop = SimpleNamespace(is_set=lambda: clock.now >= 31, wait=clock.sleep)
    monkeypatch.setattr(runtime, "STOP", stop)
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: True)

    def first_sleep(seconds):
        # Drive the real supervisor loop from cold start on the same fake clock.
        monkeypatch.setattr(clock, "sleep", Clock.sleep.__get__(clock))
        runtime.watch(SimpleNamespace(poll=lambda: None), SimpleNamespace(process=None), None,
                      monitor=monitor, updates=updater)

    monkeypatch.setattr(clock, "sleep", first_sleep)
    fixture.wait_for_service()
    assert monitor.ever_ready
    assert service_times[0] >= runtime.READY_PROBE_INTERVAL == 30
    initial_request = json.loads((tmp_path / "ci-request.json").read_text())
    assert initial_request["action"] == "check"
    assert initial_request["expires_at"] < clock.time()  # boot-gated check expired
    assert not updater.install_requested.is_set()
    assert not (tmp_path / "request.json").exists()
    assert json.loads((tmp_path / "status.json").read_text())["error"] == "Offline fixture network"

    fixture.submit_request("install")
    queued = json.loads((tmp_path / "request.json").read_text())
    assert queued["expires_at"] - clock.time() == 10  # no TTL extension/requeue
    clock.sleep(1)
    updater.service()
    assert updater.install_requested.is_set()
    assert not (tmp_path / "request.json").exists()


@pytest.mark.parametrize("initial,fresh", [
    ({"phase": "ready", "checked_at": 900}, None),  # persisted preparation
    ({"phase": "ready"}, None),  # constructor publication
    ({"phase": "ready"}, {"phase": "ready"}),  # a new constructor-only publication
    ({"phase": "idle", "checked_at": 900}, None),  # old completed check
    ({"phase": "ready", "error": "old error"}, None),
    ({"phase": "ready"}, {"phase": "checking", "checked_at": 1001}),
    ({"phase": "ready"}, {"phase": "downloading", "error": "old error"}),
    ({"phase": "ready"}, {"phase": "idle", "checked_at": 1001, "updater": {"revision": "c" * 40}}),
])
def test_barrier_rejects_stale_unready_or_wrong_resident_publications(tmp_path, monkeypatch, initial, fresh):
    from scripts.ci import updater_candidate_fixture as fixture
    clock = Clock()
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "time", clock)
    submit = Mock()
    monkeypatch.setattr(fixture, "submit_request", submit)
    resident = {"revision": "a" * 40}
    write_json(tmp_path / "ci-fixture.json", {"image_revision": resident["revision"]})
    write_json(tmp_path / "status.json", {"updater": resident, **initial})

    def sleep(seconds):
        clock.now += 30
        if fresh:
            write_json(tmp_path / "status.json", {"updater": resident, **fresh})

    monkeypatch.setattr(clock, "sleep", sleep)
    with pytest.raises(TimeoutError, match="first service pass"):
        fixture.wait_for_service()
    submit.assert_called_once_with("check")


@pytest.mark.parametrize("row,allowed", [(None, True), (("true",), True), (("false",), False)])
def test_diagnostics_capture_actual_files_and_read_only_policy(tmp_path, monkeypatch, capsys, row, allowed):
    import psycopg2
    from app.core.config import settings
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    write_json(tmp_path / "state.json", {"pending": "a" * 40, "active": None, "private": "must-not-copy"})
    write_json(tmp_path / "request.json", {"action": "install", "expires_at": 1000})
    write_json(tmp_path / "status.json", {"phase": "ready", "updater": {"revision": "b" * 40}})
    (tmp_path / "handover.json").write_text("x" * 16385)
    monkeypatch.setattr(type(settings), "get_database_connection", lambda _: {"host": "synthetic", "password": "must-not-copy"})
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = row
    connect = Mock(return_value=connection)
    monkeypatch.setattr(psycopg2, "connect", connect)
    fixture.diagnose()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["files"]["state.json"] == {"pending": "a" * 40, "active": None}
    assert report["files"]["request.json"]["action"] == "install"
    assert report["files"]["status.json"]["updater"]["revision"] == "b" * 40
    assert report["files"]["handover.json"] == {"unavailable": "ValueError"}
    assert report["files"]["ci-fixture.json"] == {"unavailable": "FileNotFoundError"}
    assert report["policy"] == {"allow_updates": allowed}
    assert "must-not-copy" not in output
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in connect.call_args.kwargs["options"]
    cursor.execute.assert_called_once_with("SELECT value FROM system_settings WHERE key = %s", ("allow_updates",))
    connection.close.assert_called_once()


def test_policy_diagnostic_failure_reports_only_exception_type(tmp_path, monkeypatch, capsys):
    import psycopg2
    from app.core.config import settings
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(type(settings), "get_database_connection", lambda _: {})
    monkeypatch.setattr(psycopg2, "connect", Mock(side_effect=RuntimeError("must-not-copy connection details")))
    fixture.diagnose()
    output = capsys.readouterr().out
    assert json.loads(output)["policy"] == {"unavailable": "RuntimeError"}
    assert "must-not-copy" not in output


def test_failed_install_captures_diagnostics_without_masking_failure(candidate, tmp_path):
    check, _ = candidate
    result = check.docker.return_value

    def docker(*args, **kwargs):
        if args[-1] == "installed":
            raise RuntimeError("original install timeout")
        if args[-1] == "diagnose":
            raise subprocess.TimeoutExpired("docker", 20)
        return result

    check.docker.side_effect = docker
    with pytest.raises(RuntimeError, match="original install timeout"):
        check.updater_lifecycle()
    assert json.loads((tmp_path / "updater-lifecycle.json").read_text()) == {"unavailable": "diagnostic process timed out"}


def test_fixture_refuses_to_run_outside_disposable_candidate(monkeypatch):
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.delenv("AI_SHUFFLE_UPDATER_FIXTURE", raising=False)
    monkeypatch.setenv("DATA_DIR", "/not-the-fixture")
    with pytest.raises(SystemExit, match="disposable"):
        fixture.main()
