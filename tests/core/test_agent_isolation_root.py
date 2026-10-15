"""Docker agent isolation (#1957) with real accounts: an agent shell gets
"Permission denied" on every protected DATA_DIR entry.

Needs root (it creates files for, and drops to, two throwaway uids), so it is
skipped otherwise; CI runs it with sudo. Everything lives in a fresh /tmp
directory and is removed afterwards.

The migration runs for real (``enable`` as root over a DATA_DIR the agent
owns, as an install with the mode off has it). Then a helper process takes the
app's ids under the split, real and effective PUID with the service account
saved, and runs the Bash tool handler and the Monitor shell spawner through
the spawn runner exactly as the app does.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# CI sets this so a broken sudo setup fails instead of skipping.
REQUIRED = os.environ.get("AGENT_ISOLATION_ROOT_REQUIRED") == "1"
pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="agent isolation is Linux (Docker) only"),
    pytest.mark.skipif(not REQUIRED and (not hasattr(os, "geteuid") or os.geteuid() != 0),
                       reason="needs root to create a second account's files and drop to throwaway uids; "
                              "CI runs it with sudo"),
]

REPO = Path(__file__).resolve().parents[2]
PUID, PGID, SUID, SGID = 64101, 64101, 64102, 64102
# The six entries of #1957's done-when; application-updates is protected too.
FILES = ("managed-postgres.json", "database-mode.json", "preview-access.key")
DIRS = {".credential-keys": "key", ".managed-connection": "key", "postgres": "password"}

HELPER = r'''
import asyncio, json, os, sys, time
repo, data, work, runner, probe = sys.argv[1:6]
puid, pgid, suid, sgid = map(int, sys.argv[6:10])
sys.path[:0] = [repo, os.path.join(repo, "fluid")]
os.environ.update(SHUFFLE_AGENT_ISOLATION="on", SHUFFLE_AGENT_ROOTS=work + ":/tmp")

# Everything imports as root first: the checkout need not be readable by the
# throwaway uids. The runner is a world-readable copy, as in the image.
from fluid.tools import agent_runner
agent_runner.RUNNER = runner
import fluid.tools.shell
from fluid.tools.builtins.bash import handler
from fluid.tools.registry import ToolCtx
from app.core import config, private_storage, process_ledger, process_tree
from app.mcp import builtin_tools
process_ledger.record_process = lambda *a, **k: False  # its own unit tests cover the runner

os.setgroups([])
os.setresgid(pgid, pgid, sgid)
os.setresuid(puid, puid, suid)
assert os.getresuid() == (puid, puid, suid) and os.getresgid() == (pgid, pgid, sgid)

async def bash(command):
    outcome = await handler({"command": command}, ToolCtx(cwd=work))
    return outcome.text

async def monitor(command):
    task = await builtin_tools._spawn_background(command, work, None)
    await asyncio.wait_for(task._reader_task, 30)
    builtin_tools._background_tasks.pop(task.task_id, None)
    return "\n".join(task.lines)

async def wait_pid(path):
    for _ in range(300):
        if os.path.exists(path) and open(path).read():
            return int(open(path).read())
        await asyncio.sleep(0.1)
    raise AssertionError("probe never started")

async def dead(pid):
    for _ in range(150):
        if not process_tree.pid_alive(pid):
            return True
        await asyncio.sleep(0.1)
    return False

async def main():
    out = {"bash": {}, "monitor": {}}
    for path in json.loads(sys.argv[10]):
        command = ("ls " if os.path.isdir(path) else "cat ") + path
        out["bash"][path] = await bash(command)
        out["monitor"][path] = await monitor(command)
    out["control_bash"] = await bash("cat " + os.path.join(data, "notes", "todo.md"))
    out["control_monitor"] = await monitor("cat " + os.path.join(data, "notes", "todo.md"))
    if os.path.exists(os.path.join(os.path.dirname(runner), "workload.py")):
        import shlex
        command = shlex.join([sys.executable, os.path.join(os.path.dirname(runner), "workload.py")])
        out["workflow_bash"] = await bash(command)
        out["workflow_monitor"] = await monitor(command)
    out["status"] = await bash("grep -E '^(Uid|Gid|Groups|NoNewPrivs):' /proc/self/status")
    with private_storage.service_files():
        out["app_reads"] = open(os.path.join(data, "managed-postgres.json")).read()

    # Cancellation reaches the agent's grandchild through the runner.
    pid_file = os.path.join(work, "bash.pid")
    task = asyncio.create_task(handler({"command": f'"{sys.executable}" "{probe}" "{pid_file}"'}, ToolCtx(cwd=work)))
    pid = await wait_pid(pid_file)
    out["bash_child_uids"] = [line for line in open(f"/proc/{pid}/status") if line.startswith("Uid:")][0]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    out["bash_cancel_killed"] = await dead(pid)

    # So does a Monitor stop (process-tree kill).
    pid_file = os.path.join(work, "monitor.pid")
    shell = await builtin_tools._spawn_background(f'"{sys.executable}" "{probe}" "{pid_file}"', work, None)
    pid = await wait_pid(pid_file)
    out["monitor_survivors"] = await process_tree.kill_process_tree(shell.process, own_process_group=True)
    out["monitor_kill_killed"] = await dead(pid)
    builtin_tools._background_tasks.pop(shell.task_id, None)
    print("RESULT " + json.dumps(out))

asyncio.run(main())
'''


def _load_isolation():
    spec = importlib.util.spec_from_file_location("shuffle_agent_isolation_root", REPO / "docker" / "agent_isolation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture():
    base = Path(tempfile.mkdtemp(prefix="agent-isolation-", dir="/tmp"))
    try:
        base.chmod(0o755)
        data, work, home = base / "data", base / "work", base / "home"
        for directory in (data, work, home):
            directory.mkdir()
        for name in FILES:
            (data / name).write_text('{"secret": "fixture"}\n')
        for name, inner in DIRS.items():
            (data / name).mkdir(mode=0o700)
            (data / name / inner).write_text("fixture\n")
        (data / "notes").mkdir()
        (data / "notes" / "todo.md").write_text("agent-readable\n")
        for path in (data, *data.rglob("*"), work, home):
            os.chown(path, PUID, PGID, follow_symlinks=False)
            path.chmod(0o700 if path.is_dir() else 0o600)
        (data / "notes" / "todo.md").chmod(0o644)
        runner = base / "agent_runner.py"
        shutil.copyfile(REPO / "fluid" / "fluid" / "tools" / "agent_runner.py", runner)
        probe = base / "probe.py"
        probe.write_text("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n")
        helper = base / "helper.py"
        helper.write_text(HELPER)
        for path in (runner, probe, helper):
            path.chmod(0o644)
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _as_agent(argv):
    return subprocess.run(argv, user=PUID, group=PGID, extra_groups=[], capture_output=True, text=True, timeout=30)


def _protected(data):
    return [data / name for name in FILES] + [data / name for name in DIRS] + \
        [data / name / inner for name, inner in DIRS.items()]


@pytest.mark.parametrize("workflows", [False, True], ids=["security", "workflows"])
def test_agent_shells_are_denied_every_protected_entry(fixture, workflows):
    if workflows:
        for command in ("git", "npm", "node"):
            if not shutil.which(command):
                pytest.skip(f"root workflow smoke requires {command}")
        workload = fixture / "workload.py"
        shutil.copyfile(REPO / "scripts/ci/agent_isolation_workload.py", workload)
        workload.chmod(0o644)
    iso = _load_isolation()
    ident = iso.Identity(PUID, PGID, SUID, SGID)
    data = fixture / "data"
    before = {p: os.lstat(p) for p in [data, *data.rglob("*")]}

    iso.enable(data, ident, log=lambda _m: None)
    for path in _protected(data):
        assert os.lstat(path).st_uid == SUID, path
    assert os.lstat(data / "notes").st_uid == PUID

    paths = [str(p) for p in _protected(data)]
    result = subprocess.run(
        [sys.executable, str(fixture / "helper.py"), str(REPO), str(data), str(fixture / "work"),
         str(fixture / "agent_runner.py"), str(fixture / "probe.py"),
         str(PUID), str(PGID), str(SUID), str(SGID), json.dumps(paths)],
        cwd=fixture, capture_output=True, text=True, timeout=300,
        env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(fixture / "home"),
             "DATA_DIR": str(fixture / "work" / "appdata"), "LANG": "C.UTF-8",
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, result.stdout[-4000:] + result.stderr[-4000:]
    out = json.loads(lines[-1][len("RESULT "):])

    for path in paths:
        assert "Permission denied" in out["bash"][path], (path, out["bash"][path])
        assert "Permission denied" in out["monitor"][path], (path, out["monitor"][path])
        assert "fixture" not in out["bash"][path] and "fixture" not in out["monitor"][path]
    assert "agent-readable" in out["control_bash"] and "agent-readable" in out["control_monitor"]
    if (fixture / "workload.py").exists():
        for tool in ("bash", "monitor"):
            assert "ISOLATION_WORKFLOW_OK" in out[f"workflow_{tool}"], out[f"workflow_{tool}"]
    status = out["status"]
    assert f"Uid:\t{PUID}\t{PUID}\t{PUID}\t{PUID}" in status
    assert f"Gid:\t{PGID}\t{PGID}\t{PGID}\t{PGID}" in status
    assert "NoNewPrivs:\t1" in status
    assert any(line.startswith("Groups:") and not line[len("Groups:"):].strip() for line in status.splitlines())
    assert out["app_reads"] == '{"secret": "fixture"}\n'
    assert out["bash_child_uids"].split()[1:] == [str(PUID)] * 4
    assert out["bash_cancel_killed"] is True
    assert out["monitor_survivors"] == [] and out["monitor_kill_killed"] is True

    # Outside the app as well: a plain agent-account process is refused.
    for path in paths:
        command = ["ls", path] if os.path.isdir(path) else ["cat", path]
        assert "Permission denied" in _as_agent(command).stderr, path

    # Rolling back hands everything to the agent account as it was.
    iso.disable(data, ident, log=lambda _m: None)
    for path, info in before.items():
        after = os.lstat(path)
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), path
    assert not (data / iso.RECORD_NAME).exists()
    assert _as_agent(["cat", str(data / "managed-postgres.json")]).returncode == 0


def test_runner_drops_a_kept_service_id(fixture):
    """The runner itself settles every id, even when the exec it came
    through had not (a process started with the service account saved)."""
    script = (
        "import os, subprocess, sys\n"
        f"os.setgroups([]); os.setresgid({PGID}, {SGID}, {SGID}); os.setresuid({PUID}, {SUID}, {SUID})\n"
        "env = {'PATH': os.defpath}\n"
        "out = subprocess.run([sys.executable, '-I', '-S', sys.argv[1], '--root', '/tmp', '--',\n"
        "                      'grep', '-E', '^(Uid|Gid|Groups):', '/proc/self/status'],\n"
        "                     cwd='/tmp', env=env, capture_output=True, text=True)\n"
        "print(out.returncode); print(out.stdout); print(out.stderr)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(fixture / "agent_runner.py")],
                            capture_output=True, text=True, timeout=60)
    assert result.stdout.startswith("0\n"), result.stdout + result.stderr
    assert f"Uid:\t{PUID}\t{PUID}\t{PUID}\t{PUID}" in result.stdout
    assert f"Gid:\t{PGID}\t{PGID}\t{PGID}\t{PGID}" in result.stdout
