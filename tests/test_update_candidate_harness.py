"""CI-only fixture mounting; no candidate builds or container operations run here."""
import runpy
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

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
    assert [call[-1] for call in actions] == ["install", "installed", "refresh-updater", "refreshed"]
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


def test_fixture_refuses_to_run_outside_disposable_candidate(monkeypatch):
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.delenv("AI_SHUFFLE_UPDATER_FIXTURE", raising=False)
    monkeypatch.setenv("DATA_DIR", "/not-the-fixture")
    with pytest.raises(SystemExit, match="disposable"):
        fixture.main()
