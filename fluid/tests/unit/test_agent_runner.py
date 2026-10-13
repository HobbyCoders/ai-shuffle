"""The agent spawn runner (Docker agent isolation, #1957).

``wrap`` must change nothing while the mode is off; the runner itself must
refuse a bad environment, working directory or argument list before running
anything, and must leave no inherited descriptor or privilege behind.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from fluid.tools import agent_runner, child_env

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the runner is Linux only")
ON = {agent_runner.FLAG: "on", agent_runner.ROOTS: "/srv/work:/tmp"}


def test_wrap_off_returns_the_same_objects():
    argv, env = ["sh", "-c", "true"], {"DATABASE_URL": "kept when off"}
    out = agent_runner.wrap(argv, "/somewhere", env, environ={})
    assert out[0] is argv and out[1] == "/somewhere" and out[2] is env
    out = agent_runner.wrap(argv, None, None, environ={agent_runner.FLAG: "off"})
    assert out[0] is argv and out[1] is None and out[2] is None


def test_wrap_on_prefixes_the_runner_and_drops_secrets():
    env = {"PATH": os.defpath, "HOME": "/home/x", "DATABASE_URL": "postgresql://x", "PGPASSWORD": "p",
           "PGID": "100", "SHUFFLE_AGENT_ROOTS": "/x", "OWNER_TOKEN": "the owner's own"}
    argv, cwd, out = agent_runner.wrap(["sh", "-c", "echo hi"], "/srv/work", env, environ=ON)
    assert argv == [sys.executable, "-I", "-S", agent_runner.RUNNER, "--root", "/srv/work",
                    "--root", "/tmp", "--", "sh", "-c", "echo hi"]
    assert cwd == "/srv/work"
    assert out == {"PATH": os.defpath, "HOME": "/home/x", "PGID": "100", "OWNER_TOKEN": "the owner's own"}
    assert "DATABASE_URL" in env  # the caller's mapping is not modified


def test_wrap_on_defaults_the_working_directory_to_home():
    _argv, cwd, _env = agent_runner.wrap(["sh"], None, {"PATH": os.defpath}, environ=ON)
    assert cwd == pwd.getpwuid(os.getuid()).pw_dir


def test_wrap_on_inherits_the_process_environment_when_env_is_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://never")
    monkeypatch.setenv("AGENT_RUNNER_PROBE", "1")
    _argv, _cwd, env = agent_runner.wrap(["sh"], "/tmp", None, environ={**os.environ, **ON})
    assert env["AGENT_RUNNER_PROBE"] == "1" and "DATABASE_URL" not in env
    assert not any(k.startswith("SHUFFLE_AGENT_") for k in env)


def test_wrap_on_raises_file_not_found_for_a_missing_command():
    with pytest.raises(FileNotFoundError):
        agent_runner.wrap(["no-such-command-xyz"], "/tmp", {"PATH": os.defpath}, environ=ON)
    with pytest.raises(FileNotFoundError):
        agent_runner.wrap([], "/tmp", {"PATH": os.defpath}, environ=ON)


def test_strip_recovers_the_command():
    argv, _cwd, _env = agent_runner.wrap(["sh", "-c", "x -- y"], "/tmp", {"PATH": os.defpath}, environ=ON)
    assert agent_runner.strip(argv) == ["sh", "-c", "x -- y"]
    assert agent_runner.strip(["sh", "-c", "agent_runner.py"]) == ["sh", "-c", "agent_runner.py"]
    assert agent_runner.strip(["python", "-I", "-S", "/x/agent_runner.py", "--root"]) == [
        "python", "-I", "-S", "/x/agent_runner.py", "--root"]


@pytest.mark.parametrize("name", [
    "DATABASE_URL", "database_url", "DSN", "MY_DSN", "PGPASSWORD", "PGHOST", "PGID", "pgid", "POSTGRES_PASSWORD",
    "DATABASE_MODE", "OWNER_PASSWORD", "LICENSE_KEY", "ADMIN_PASSWORD", "ADMIN_USERNAME", "SECRET_KEY",
    "CLUSTER_SECRET", "JWT_SECRET", "SESSION_SECRET", "AUTH_SECRET", "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN",
    "SHUFFLE_DESKTOP_ATTACH_TOKEN", "SHUFFLE_DESKTOP_LISTENER_TOKEN", "PATH", "HOME", "GITHUB_TOKEN",
    "OPENAI_API_KEY", "PUID", "PG", "PGX", "POSTGRESQL", "DATABASES", "TOKEN_DSN", "LANG",
])
def test_secret_names_match_child_env(name):
    assert agent_runner.is_app_secret(name) == child_env.is_app_secret(name)


def test_secret_list_is_a_copy_of_child_env():
    assert agent_runner._APP_SECRET_NAMES == child_env._DENY_NAMES
    assert agent_runner._APP_SECRET_PREFIX.pattern == child_env._DENY_PREFIX_RE.pattern


def _run(args, tmp_path, env=None, **kwargs):
    return subprocess.run([sys.executable, "-I", "-S", agent_runner.RUNNER, *args], cwd=tmp_path,
                          env={"PATH": os.defpath} if env is None else env,
                          capture_output=True, text=True, timeout=30, **kwargs)


@linux
@pytest.mark.parametrize("args", [
    ["--", "true"],                       # no approved roots
    ["--root", "/", "--"],                # no command
    ["--root"],                           # dangling option
    ["--bogus", "/", "--", "true"],      # unexpected argument
    ["--root", "/nonexistent-agent-root", "--", "true"],  # cwd outside the roots
    ["--root", "relative", "--", "true"],  # relative roots never match
])
def test_runner_refuses_bad_arguments(tmp_path, args):
    result = _run(args, tmp_path)
    assert result.returncode == agent_runner.REFUSED
    assert "agent runner refused" in result.stderr


@linux
@pytest.mark.parametrize("name", ["DATABASE_URL", "PGPASSWORD", "OWNER_PASSWORD", "x_dsn"])
def test_runner_refuses_an_app_secret_in_the_environment(tmp_path, name):
    result = _run(["--root", str(tmp_path), "--", "true"], tmp_path, {"PATH": os.defpath, name: "secret"})
    assert result.returncode == agent_runner.REFUSED
    assert name in result.stderr and "secret" not in result.stderr.replace(name, "")


@linux
def test_runner_refuses_a_symlinked_way_out(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    result = _run(["--root", str(root), "--", "true"], root / "escape")
    assert result.returncode == agent_runner.REFUSED


def _extra_groups():
    return bool(set(os.getgroups()) - {os.getgid()})


@linux
def test_runner_runs_the_command_without_privileges_or_descriptors(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        script = (f"grep NoNewPrivs /proc/self/status; test -e /proc/self/fd/{write_fd} && echo fd-open"
                  " || echo fd-closed; env")
        env = {"PATH": os.defpath, "SHUFFLE_AGENT_ROOTS": "/x", "KEEP": "1"}
        result = _run(["--root", str(tmp_path), "--", "sh", "-c", script], tmp_path, env, pass_fds=(write_fd,))
    finally:
        os.close(read_fd)
        os.close(write_fd)
    if os.getuid() == 0:
        assert result.returncode == agent_runner.REFUSED and "root" in result.stderr
        return
    if _extra_groups():
        # The app runs with no supplementary groups; any left here would leak.
        assert result.returncode == agent_runner.REFUSED and "supplementary groups" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    assert "NoNewPrivs:\t1" in result.stdout
    assert "fd-closed" in result.stdout
    assert "KEEP=1" in result.stdout and "SHUFFLE_AGENT_ROOTS" not in result.stdout


@linux
def test_runner_reports_a_missing_command(tmp_path):
    if os.getuid() == 0 or _extra_groups():
        pytest.skip("the runner refuses before exec for root or with supplementary groups")
    result = _run(["--root", str(tmp_path), "--", "no-such-command-xyz"], tmp_path)
    assert result.returncode == 127
    assert "no-such-command-xyz" in result.stderr


def test_runner_is_standard_library_only():
    # It runs with -I -S, before anything of the app's can be trusted.
    source = Path(agent_runner.RUNNER).read_text()
    assert "from fluid" not in source and "import fluid" not in source
