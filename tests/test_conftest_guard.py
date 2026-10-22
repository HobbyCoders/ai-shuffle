"""Temp git discovery cannot reach a checkout above an explicit basetemp."""
import os
import subprocess

from tests.conftest import _isolate_test_git_discovery


def test_git_ceiling_stops_discovery_above_basetemp(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    base = checkout / "caller-basetemp"
    folder = base / "test-folder"
    folder.mkdir(parents=True)
    inherited = str(tmp_path / "existing-ceiling")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", inherited)

    _isolate_test_git_discovery.__wrapped__(monkeypatch, folder)

    assert os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep) == [inherited, str(base)]
    result = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
