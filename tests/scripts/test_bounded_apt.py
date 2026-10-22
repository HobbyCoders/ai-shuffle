"""Real process/record-lock regression for bounded privileged CI installs."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/bounded_apt.py"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux apt process groups")


def load_helper():
    spec = importlib.util.spec_from_file_location("bounded_apt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_apt(tmp_path):
    def executable(name, body):
        path = tmp_path / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    executable("apt-get", r'''
        import fcntl, os, pathlib, subprocess, sys, time
        root = pathlib.Path(__file__).parent
        config = pathlib.Path(os.environ["APT_CONFIG"]).read_text()
        assert 'APT::Get::Upgrade "false";' in config
        assert 'Dir::Etc::sourceparts "-";' in config
        log = root / "events"
        with log.open("a") as stream:
            stream.write("apt\n")
        if (root / "mode").read_text() == "fail":
            sys.exit(100)
        if log.read_text().count("apt\n") == 1 or (root / "mode").read_text() == "always-timeout":
            holder = subprocess.Popen([sys.executable, str(root / "holder")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "pid").write_text(str(holder.pid))
            while not (root / "ready").exists():
                time.sleep(0.01)
            time.sleep(60)
        with (root / "lock").open("r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert log.read_text() == "apt\nrepair\napt\n"
    ''')
    executable("holder", r'''
        import fcntl, pathlib, signal, time
        root = pathlib.Path(__file__).parent
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with (root / "lock").open("r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX)
            (root / "ready").touch()
            time.sleep(60)
    ''')
    executable("dpkg", r'''
        import fcntl, pathlib, sys
        assert sys.argv[1:] == ["--configure", "-a"]
        root = pathlib.Path(__file__).parent
        with (root / "lock").open("r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (root / "events").open("a") as log:
            log.write("repair\n")
        sys.exit(1 if (root / "repair-fails").exists() else 0)
    ''')
    # Exercise the unmodified supervisor, shortening only its two constants and
    # redirecting real POSIX lock probes to the fixture's lock. No sudo/root.
    helper = load_helper()
    source = SCRIPT.read_text().replace("ATTEMPT_SECONDS = 300", "ATTEMPT_SECONDS = 1")
    source = source.replace("KILL_AFTER_SECONDS = 10", "KILL_AFTER_SECONDS = 0.2")
    for lock in helper.LOCKS:
        source = source.replace(lock, str(tmp_path / "lock"))
    (tmp_path / "bounded_apt.py").write_text(source)
    (tmp_path / "lock").touch()
    (tmp_path / "mode").write_text("timeout")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    try:
        yield tmp_path, env
    finally:
        pid_file = tmp_path / "pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def run_install(root, env):
    return subprocess.run([sys.executable, str(root / "bounded_apt.py"), "--root", "apt-get"],
                          env=env, text=True, capture_output=True, timeout=10)


def test_timeout_kills_term_resistant_lock_holder_before_repair_and_retry(fake_apt):
    root, env = fake_apt
    result = run_install(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "events").read_text() == "apt\nrepair\napt\n"
    assert "attempt 1 failed (exit 124" in result.stdout


def test_two_failures_fail_closed_without_dpkg_repair(fake_apt):
    root, env = fake_apt
    (root / "mode").write_text("fail")
    result = run_install(root, env)
    assert result.returncode == 1
    assert (root / "events").read_text() == "apt\napt\n"
    assert "failed after two bounded attempts" in result.stdout


def test_both_timeouts_fail_closed_and_release_locks(fake_apt):
    import fcntl

    root, env = fake_apt
    (root / "mode").write_text("always-timeout")
    result = run_install(root, env)
    assert result.returncode == 1
    assert (root / "events").read_text() == "apt\nrepair\napt\n"
    with (root / "lock").open("r+") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_waits_for_an_external_package_manager_without_killing_it(fake_apt):
    root, env = fake_apt
    (root / "mode").write_text("fail")
    code = (
        "import fcntl, sys, time; "
        "lock = open(sys.argv[1], 'r+'); "
        "fcntl.lockf(lock, fcntl.LOCK_EX); "
        "print('locked', flush=True); time.sleep(0.5)"
    )
    with subprocess.Popen([sys.executable, "-c", code, str(root / "lock")],
                          stdout=subprocess.PIPE, text=True) as holder:
        assert holder.stdout.readline().strip() == "locked"
        start = time.monotonic()
        result = run_install(root, env)
        assert time.monotonic() - start >= 0.4
        assert result.returncode == 1
        assert holder.wait(timeout=2) == 0
    assert (root / "events").read_text() == "apt\napt\n"


def test_failed_repair_prevents_the_retry_install(fake_apt):
    root, env = fake_apt
    (root / "repair-fails").touch()
    result = run_install(root, env)
    assert result.returncode == 1
    assert (root / "events").read_text() == "apt\nrepair\n"


def test_elevation_includes_supervisor_and_preserves_runner_cache(monkeypatch):
    helper = load_helper()
    class ExecCalled(Exception):
        pass

    def execute(program, args):
        assert program == "sudo"
        assert args[:3] == ["sudo", "-n", "env"]
        assert f"HOME={os.environ['HOME']}" in args
        assert f"PATH={os.environ['PATH']}" in args
        assert args[-3:] == ["--root", "apt-get", "update"]
        raise ExecCalled

    monkeypatch.setattr(helper.os, "execvp", execute)
    with pytest.raises(ExecCalled):
        helper.main(["apt-get", "update"])
    assert helper.ATTEMPT_SECONDS == 300
    assert helper.KILL_AFTER_SECONDS == 10


def test_apt_config_reuses_installed_packages_without_upgrades(tmp_path):
    helper = load_helper()
    config = tmp_path / "apt.conf"
    config.write_text(helper.APT_OPTIONS)
    # Exercise apt itself, without root, network or modifying the database.
    # libc6 is already part of both the Ubuntu CI and Debian-based test images.
    result = subprocess.run(["apt-get", "--simulate", "install", "libc6"],
                            env={**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C"},
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipping libc6, it is already installed and upgrade is not set" in result.stdout
    assert "0 upgraded, 0 newly installed" in result.stdout
    assert '--allow-unauthenticated' not in helper.APT_OPTIONS
    assert 'trusted' not in helper.APT_OPTIONS
    assert 'Acquire::Languages "none"' in helper.APT_OPTIONS


@pytest.mark.parametrize("workflow,job,step_name", [
    ("ci-frontend.yml", "browser-mcp", "Prepare sandboxed Chromium"),
    ("ci-fluid.yml", "unit", "Install ripgrep"),
    ("ci-browser-workspace.yml", "workspace", "Install isolated Chromium"),
])
def test_every_apt_install_uses_shared_bounded_supervisor(workflow, job, step_name):
    data = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)
    step = next(step for step in data["jobs"][job]["steps"] if step.get("name") == step_name)
    assert "scripts/ci/bounded_apt.py" in step["run"]
    assert step["timeout-minutes"] == "13"
    assert "continue-on-error" not in step
    if workflow != "ci-fluid.yml":
        assert "bounded_apt.py npx --no-install playwright-core install-deps chromium" in step["run"]
        assert "timeout --kill-after=10s 90s npx --no-install playwright-core install chromium" in step["run"]
        assert "sudo env" not in step["run"]
        assert "--with-deps" not in step["run"]
