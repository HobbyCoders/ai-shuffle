"""The existing candidate-container CI path owns the real lifecycle probe."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.verify_container_postgres import Check


def test_candidate_lifecycle_is_built_offline_then_explicitly_installed_and_refreshed(tmp_path, monkeypatch):
    from scripts import source_archive
    from scripts import verify_container_postgres as harness
    build = Mock()
    monkeypatch.setattr(source_archive, "build", build)
    monkeypatch.setattr(harness.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="a" * 40)))
    check = Mock(spec=Check)
    check.artifacts, check.image = tmp_path, "candidate-only"
    check.volume.return_value = "disposable-volume"
    check.launch.side_effect = ["initial-container", "updated-container"]
    check.system_id.return_value = "same-cluster"
    check.sql.side_effect = ["", "retained", "retained"]
    Check.updater_lifecycle(check)
    build.assert_called_once_with("a" * 40, tmp_path / "updater-source.zip")
    assert check.stop.call_args_list[0].args == ("initial-container",)
    calls = [call.args for call in check.docker.call_args_list]
    prepare = next(call for call in calls if call[0] == "run")
    assert prepare[:4] == ("run", "--rm", "--network", "bridge")
    assert prepare[-3:] == ("/app/scripts/ci/updater_candidate_fixture.py", "prepare", "/fixture-source.zip")
    assert "type=volume,src=disposable-volume,dst=/data" in prepare
    actions = [call[-1] for call in calls if call[:2] == ("exec", "--user")]
    assert actions[:4] == ["install", "installed", "refresh-updater", "refreshed"]
    check.passed.assert_called_once()


def test_fixture_refuses_to_run_outside_disposable_candidate(monkeypatch):
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.delenv("AI_SHUFFLE_UPDATER_FIXTURE", raising=False)
    monkeypatch.setenv("DATA_DIR", "/not-the-fixture")
    with pytest.raises(SystemExit, match="disposable"):
        fixture.main()
