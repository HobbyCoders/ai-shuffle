"""Scratch belongs to one invocation, including its failed/parallel children."""
import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import local_ci

ROOT = Path(__file__).resolve().parents[2]


def test_retention_is_explicit():
    config = configparser.ConfigParser()
    config.read(ROOT / "pytest.ini")
    assert config["pytest"].get("tmp_path_retention_count") == "0"
    assert config["pytest"].get("tmp_path_retention_policy") == "none"


@pytest.mark.parametrize("failure", [False, True])
def test_local_ci_removes_child_scratch(tmp_path, failure):
    record = tmp_path / "record"
    code = ("import os, pathlib, tempfile; "
            "p=pathlib.Path(tempfile.mkdtemp()); (p/'payload').write_bytes(b'x'*1048576); "
            f"pathlib.Path({str(record)!r}).write_text(str(p)); "
            f"raise SystemExit({int(failure)})")
    if failure:
        with pytest.raises(local_ci.Failure):
            local_ci.run([sys.executable, "-c", code])
    else:
        local_ci.run([sys.executable, "-c", code])
    directory = Path(record.read_text())
    try:
        assert not directory.exists()
    finally:
        # This test created it, including on the unfixed baseline.
        import shutil
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(os.name != "posix", reason="Unix socket path limit")
def test_local_ci_pytest_scratch_does_not_nest_socket_paths(tmp_path):
    suite = tmp_path / "test_child_socket.py"
    record = tmp_path / "socket-path"
    suite.write_text(
        "import socket, pathlib\n"
        "def test_real_no_unix_socket_reaches_a_host_listener(tmp_path):\n"
        "    folder = tmp_path / 'grant'\n"
        "    folder.mkdir()\n"
        "    path = folder / 'stream.sock'\n"
        "    with socket.socket(socket.AF_UNIX) as stream:\n"
        "        stream.bind(str(path))\n"
        f"    pathlib.Path({str(record)!r}).write_text(str(path))\n"
    )
    local_ci.run([sys.executable, "-m", "pytest", "-p", "scripts.ci.test_scratch",
                  str(suite), "-n", "2", "-q"], env={"PYTHONPATH": str(ROOT)}, timeout=60)
    path = Path(record.read_text())
    assert "ai-shuffle-ci-" not in str(path)
    assert not path.exists()


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_pytest_owns_and_removes_basetemp(tmp_path, parallel, failure):
    suite = tmp_path / "test_child.py"
    record = tmp_path / "record"
    suite.write_text("def test_payload(tmp_path):\n"
                     "    (tmp_path/'payload').write_bytes(b'x'*1048576)\n"
                     f"    __import__('pathlib').Path({str(record)!r}).write_text(str(tmp_path))\n"
                     f"    assert {not failure!r}\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    command = [sys.executable, "-m", "pytest", "-p", "scripts.ci.test_scratch",
               str(suite), "-q"]
    if parallel:
        command += ["-n", "2"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == int(failure), result.stdout + result.stderr
    directory = Path(record.read_text())
    assert not directory.exists()
