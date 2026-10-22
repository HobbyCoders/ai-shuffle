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
    suite.write_text("import json\n"
                     "from pathlib import Path\n"
                     "def test_payload(tmp_path, tmp_path_factory, pytestconfig):\n"
                     "    base = tmp_path_factory.getbasetemp()\n"
                     "    owner = base.parent\n"
                     "    if hasattr(pytestconfig, 'workerinput'):\n"
                     "        owner = owner.parent\n"
                     "    assert owner.name.startswith('ai-shuffle-pytest-')\n"
                     "    if Path('/proc/self/stat').exists():\n"
                     "        receipt = json.loads((owner / '.ai-shuffle-scratch-owner').read_text())\n"
                     "        assert receipt['version'] == 1\n"
                     "        assert receipt['pid'] > 0\n"
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
    owner = directory.parents[2 if parallel else 1]
    assert owner.name.startswith("ai-shuffle-pytest-")
    assert not owner.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM cleanup")
@pytest.mark.parametrize("runner", ["local_ci", "pytest", "pytest-xdist"])
def test_sigterm_unwinds_owned_scratch(tmp_path, runner):
    import signal
    import time

    record = tmp_path / "ready"
    second = tmp_path / "second-started"
    if runner == "local_ci":
        child_code = ("import os, pathlib, time; "
                      f"pathlib.Path({str(record)!r}).write_text(os.environ['TMPDIR']); "
                      "time.sleep(60)")
        code = ("import sys; from scripts.local_ci import run; "
                f"run([sys.executable, '-c', {child_code!r}])")
        command = [sys.executable, "-c", code]
    else:
        suite = tmp_path / "test_sleep.py"
        suite.write_text("import pathlib, time\n"
                         "def test_sleep(pytestconfig, tmp_path_factory):\n"
                         "    owner = tmp_path_factory.getbasetemp().parent\n"
                         "    if hasattr(pytestconfig, 'workerinput'):\n"
                         "        owner = owner.parent\n"
                         "    if pathlib.Path('/proc/self/stat').exists():\n"
                         "        assert (owner / '.ai-shuffle-scratch-owner').is_file()\n"
                         f"    pathlib.Path({str(record)!r}).write_text(str(owner))\n"
                         "    time.sleep(60)\n"
                         "def test_second():\n"
                         f"    pathlib.Path({str(second)!r}).touch()\n")
        command = [sys.executable, "-m", "pytest", "-p", "scripts.ci.test_scratch", str(suite), "-q"]
        if runner == "pytest-xdist":
            # Keep both tests on one worker so the second is queued, not already
            # running independently when the controller receives SIGTERM.
            command += ["-n", "2", "--dist", "loadscope"]
    child = subprocess.Popen(command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 20
        while not record.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert record.exists(), "child never reached owned scratch"
        directory = Path(record.read_text())
        assert directory.is_dir()
        child.send_signal(signal.SIGTERM)
        output, _ = child.communicate(timeout=10)
        assert child.returncode == 128 + signal.SIGTERM, output
        assert not second.exists(), output
        assert not directory.exists(), output
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


def test_sigterm_cleanup_preserves_staged_handler():
    import signal
    from app.core.temp_scratch import cleanup_on_sigterm
    from scripts.local_ci_stages import _install_stop_signals, _restore_signals

    previous = _install_stop_signals()
    try:
        staged = signal.getsignal(signal.SIGTERM)
        with cleanup_on_sigterm():
            assert signal.getsignal(signal.SIGTERM) is staged
        assert signal.getsignal(signal.SIGTERM) is staged
    finally:
        _restore_signals(previous)


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("from_fluid", [False, True])
def test_fluid_pytest_owns_scratch(tmp_path, explicit, from_fluid):
    """Exercise Fluid's real config boundary, without injecting our scratch plugin."""
    import json

    record = tmp_path / "record"
    observer = tmp_path / "scratch_observer.py"
    observer.write_text(
        "import json, pathlib\n"
        "def pytest_sessionstart(session):\n"
        "    config = session.config\n"
        "    base = pathlib.Path(config.option.basetemp)\n"
        "    marker = base.parent / '.ai-shuffle-scratch-owner'\n"
        "    receipt = json.loads(marker.read_text()) if marker.is_file() else None\n"
        f"    pathlib.Path({str(record)!r}).write_text(json.dumps([str(base), receipt]))\n"
    )
    cwd = ROOT / "fluid" if from_fluid else ROOT
    test = ROOT / "fluid/tests/unit/test_workflow_proposals.py"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(ROOT / "fluid")])}
    # local_ci and hosted jobs clear addopts; scratch must still be configured.
    command = [sys.executable, "-m", "pytest", "-p", "scratch_observer", str(test),
               "-o", "addopts=", "-q"]
    requested = tmp_path / "caller-owned"
    if explicit:
        command += ["--basetemp", str(requested)]
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    base, receipt = json.loads(record.read_text())
    base = Path(base)
    if explicit:
        assert base == requested
        assert receipt is None
        assert base.is_dir()
    else:
        assert base.name == "pytest"
        assert base.parent.name.startswith("ai-shuffle-pytest-")
        if Path("/proc/self/stat").exists():
            assert receipt["version"] == 1
            assert receipt["pid"] != os.getpid()
            assert receipt["uid"] == os.getuid()
        assert not base.parent.exists()


@pytest.mark.parametrize("missing", ["scripts", "scripts.ci"])
def test_fluid_pytest_without_app_scripts(tmp_path, missing):
    """A Fluid-only installation must not require the app's scratch plugin."""
    code = (
        "import importlib.abc, sys, pytest\n"
        "class WithoutAppScripts(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        f"        if fullname == {missing!r}:\n"
        "            raise ModuleNotFoundError('app scripts unavailable', name=fullname)\n"
        "sys.meta_path.insert(0, WithoutAppScripts())\n"
        "result = pytest.main(['tests/unit/test_workflow_proposals.py', '-o', 'addopts=',\n"
        f"                      '--basetemp', {str(tmp_path / 'standalone-base')!r}, '-q'])\n"
        "assert 'scripts.ci.test_scratch' not in sys.modules\n"
        "raise SystemExit(result)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT / "fluid",
        env={**os.environ, "PYTHONPATH": str(ROOT / "fluid")},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "21 passed" in result.stdout


@pytest.mark.parametrize("parallel", [False, True])
def test_explicit_basetemp_is_caller_owned(tmp_path, parallel):
    suite = tmp_path / "test_explicit.py"
    record = tmp_path / "record"
    requested = tmp_path / "caller-owned"
    suite.write_text("def test_payload(tmp_path):\n"
                     f"    __import__('pathlib').Path({str(record)!r}).write_text(str(tmp_path))\n")
    command = [sys.executable, "-m", "pytest", "-p", "scripts.ci.test_scratch",
               str(suite), "--basetemp", str(requested), "-q"]
    if parallel:
        command += ["-n", "2"]
    result = subprocess.run(
        command,
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(record.read_text()).is_relative_to(requested)
    assert requested.is_dir()
    assert not (requested.parent / ".ai-shuffle-scratch-owner").exists()
