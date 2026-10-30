"""Real child-boundary regressions; probe only a synthetic, credential-free env."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE_ENTRY = b"REVIEW_TRANSCRIPT_FAKE_SECRET=fixture-only"


def child_stats():
    return {"pid": os.getpid(), "secret_inherited": "REVIEW_TRANSCRIPT_FAKE_SECRET" in os.environ,
            "api_modules": [name for name in sys.modules if name.startswith("app.api")],
            "main_imported": "app.main" in sys.modules}


def blocked(marker):
    Path(marker).touch()
    time.sleep(30)


def gil_blocked(marker):
    # Deliberately unrelated to canonical reminder parsing: that parser's
    # independent linear-time fix must not make this ownership test vacuous.
    expression = re.compile(r"(a+)+$")
    Path(marker).touch()
    expression.fullmatch("a" * 32 + "!")  # C regex holds the GIL for many seconds.
    raise AssertionError("GIL-holding fixture unexpectedly completed")


PARENT = r'''
import asyncio, importlib.util, json, os, sys, time
import psutil
from app.core.transcript_views.bridge import ReducerBridge
from app.core.transcript_views.child_process import _job_object
from tests.test_transcript_projection_child_boundary import child_stats, blocked, gil_blocked

async def main():
    # Exactly the production spawn hazard: multiprocessing reconstructs this
    # entrypoint even though the preparation operation needs no application.
    sys.modules["__main__"].__spec__ = importlib.util.find_spec("app.main")
    bridge = ReducerBridge()
    start = time.perf_counter()
    stats = await bridge.prepare(child_stats)
    stats["cold_seconds"] = time.perf_counter() - start
    await bridge.request("plan", carry=None, frames=[])
    stats["node"] = bridge._process.pid
    _job_object()  # Also verify the portable helper lookup on Linux, without Win32 calls.
    stats["no_tool_registry"] = "fluid.tools.registry" not in sys.modules
    # Inspect actual helpers rather than multiprocessing's private attributes,
    # so the assertion covers every extra process on Linux and Windows.
    stats["tracker"] = next((child.pid for child in psutil.Process().children()
                             if child.pid not in {stats["pid"], stats["node"]}), None)
    if sys.argv[1] in ("blocked", "gil-blocked"):
        operation = gil_blocked if sys.argv[1] == "gil-blocked" else blocked
        asyncio.create_task(bridge.prepare(operation, sys.argv[2]))
        while not os.path.exists(sys.argv[2]):
            await asyncio.sleep(.005)
        if sys.argv[1] == "gil-blocked":
            await asyncio.sleep(.2)  # Ensure the child entered C before killing its parent.
    print(json.dumps(stats), flush=True)
    # The supervisor kills this parent without giving close()/finally a turn.
    sys.stdin.read(1)
    await bridge.close()

asyncio.run(main())
'''


def parent_environment(tmp_path):
    # Windows environ keys enumerate as uppercase but lookups are case-
    # insensitive. Preserve mandatory SystemRoot, not just its display case.
    environment = {key: os.environ[key]
                   for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP") if key in os.environ}
    environment.update(TESTING="1", AI_SHUFFLE_ENV_FILE=os.devnull,
                       PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "fluid"))),
                       DATA_DIR=str(tmp_path / "data"), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                       REVIEW_TRANSCRIPT_FAKE_SECRET="fixture-only")
    return environment


async def parent(tmp_path, mode="idle"):
    environment = parent_environment(tmp_path)
    # Files, not inherited output pipes: the pre-fix orphan helper retains
    # stdout/stderr, so awaiting their EOF would hide the regression in a hang.
    output = tmp_path / "parent-output"
    with output.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", PARENT, mode, str(tmp_path / "entered"), cwd=ROOT,
            env=environment, stdin=asyncio.subprocess.PIPE, stdout=stream, stderr=stream)
    descendants = []
    try:
        async def ready():
            nonlocal descendants
            while True:
                text = output.read_text()
                for line in text.splitlines():
                    if line.startswith('{'):
                        return json.loads(line)
                assert process.returncode is None, text
                try:
                    descendants = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    await process.wait()
                    raise AssertionError(output.read_text()) from None
                await asyncio.sleep(.01)
        return process, await asyncio.wait_for(ready(), 15)
    except BaseException:
        for child in descendants:
            if child.is_running():
                child.kill()
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


def alive(pid):
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def cleanup(process, stats):
    # Only the parent/children created by this fixture, never a shared worker.
    for key in ("pid", "node", "tracker"):
        pid = stats.get(key)
        if pid and alive(pid):
            psutil.Process(pid).kill()
    if process.returncode is None:
        process.kill()
    await process.wait()


@pytest.mark.asyncio
async def test_lightweight_cold_child_does_not_reconstruct_production_main(tmp_path):
    process, stats = await parent(tmp_path)
    try:
        assert not stats["main_imported"]
        assert stats["api_modules"] == []
        assert stats["no_tool_registry"]
        assert stats["tracker"] is None
        # Structural import assertions remain portable; this Linux cold budget
        # also catches accidental site/application startup outside app.api.
        if sys.platform == "linux":
            assert stats["cold_seconds"] < 2.0, stats["cold_seconds"]
        print(f"preparation cold call: {stats['cold_seconds']:.3f}s; no application imports")
    finally:
        await cleanup(process, stats)


@pytest.mark.asyncio
async def test_same_uid_sibling_cannot_find_fake_credential_in_either_child(tmp_path):
    process, stats = await parent(tmp_path)
    try:
        assert not stats["secret_inherited"]
        assert stats["tracker"] is None  # No hidden third credential boundary.
        if sys.platform == "linux":
            assert psutil.Process(process.pid).uids().real == os.getuid()
            for key in ("pid", "node"):
                assert psutil.Process(stats[key]).uids().real == os.getuid()
                try:
                    contents = Path(f"/proc/{stats[key]}/environ").read_bytes()
                except PermissionError:
                    continue
                # Never print an environment, including on assertion failure.
                visible = FAKE_ENTRY in contents.split(b"\0")
                assert not visible, f"synthetic credential readable from {key}"
    finally:
        await cleanup(process, stats)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["idle", "blocked", "gil-blocked"])
async def test_abrupt_parent_death_stops_all_owned_children(tmp_path, mode):
    process, stats = await parent(tmp_path, mode)
    try:
        assert alive(stats["pid"]) and alive(stats["node"])
        process.kill()
        await process.wait()
        deadline = time.monotonic() + 3
        pids = [stats[key] for key in ("pid", "node", "tracker") if stats.get(key)]
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert not any(alive(pid) for pid in pids), {pid: alive(pid) for pid in pids}
    finally:
        await cleanup(process, stats)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death exec guard")
@pytest.mark.asyncio
async def test_linux_guard_rejects_a_parent_lost_before_it_is_armed(tmp_path):
    marker = tmp_path / "must-not-execute"
    script = "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-S", str(ROOT / "app/core/transcript_views/linux_child.py"),
        str(os.getpid() + 1), sys.executable, "-I", "-c", script,
        env=parent_environment(tmp_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        assert await asyncio.wait_for(process.wait(), 3) == -signal.SIGKILL
        assert not marker.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kill-on-close job")
@pytest.mark.asyncio
async def test_windows_job_failure_kills_the_unowned_child(monkeypatch):
    from types import SimpleNamespace

    from app.core.transcript_views import child_process

    pids = []

    def cannot_assign(pid, *additional_pids):
        pids.extend((pid, *additional_pids))
        return None

    monkeypatch.setattr(child_process, "_job_object", lambda: SimpleNamespace(
        assign_kill_on_close_job=cannot_assign))
    with pytest.raises(OSError, match="projection_parent_ownership_failed"):
        await child_process.launch(sys.executable, "-I", str(ROOT / "app/core/transcript_views/preparation_child.py"),
                                   limit=4096, runtime="python")
    assert pids and not alive(pids[0])


def test_job_helper_also_resolves_root_only_source_namespace(monkeypatch):
    from types import SimpleNamespace

    from app.core.transcript_views import child_process

    child_process._job_object.cache_clear()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(child_process.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=None))
            module = child_process._job_object()
        assert Path(module.__file__) == ROOT / "fluid/fluid/tools/job_object.py"
        assert callable(module.assign_kill_on_close_job)
    finally:
        child_process._job_object.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize("admission", ["go", "eof", "invalid"])
async def test_windows_startup_gates_do_not_execute_or_consume_protocol(tmp_path, runtime, admission):
    from app.core.transcript_views.child_process import _windows_command

    marker = tmp_path / "entry"
    target = tmp_path / ("protocol.py" if runtime == "python" else "protocol.cjs")
    if runtime == "python":
        target.write_text("from pathlib import Path; import sys; Path(" + repr(str(marker)) +
                          ").touch(); assert sys.stdin.buffer.read(3) == b'rpc'; print('ready')")
        command = (sys.executable, "-I", str(target))
    else:
        target.write_text("if(require.main!==module)process.exit(72);const fs=require('node:fs');fs.writeFileSync(" + json.dumps(str(marker)) +
                          ",'');const p=Buffer.alloc(3);let n=0;while(n<3)n+=fs.readSync(0,p,n,3-n,null);"
                          "if(p.toString()!=='rpc')process.exit(71);console.log('ready');")
        node = shutil.which("node")
        assert node, "Node is a required projection runtime"
        command = (node, "--max-old-space-size=192", str(target))
    process = await asyncio.create_subprocess_exec(
        *_windows_command(command, runtime), env=parent_environment(tmp_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        runtime_pid = int.from_bytes(await asyncio.wait_for(process.stdout.readexactly(4), 5), "big")
        assert runtime_pid > 0 and alive(runtime_pid)
        await asyncio.sleep(.15)
        assert process.returncode is None and not marker.exists()
        if admission == "go":
            process.stdin.write(b"Grpc")  # One write tests that only the gate byte is consumed.
            await process.stdin.drain()
        elif admission == "invalid":
            process.stdin.write(b"Xrpc")
            await process.stdin.drain()
        else:
            process.stdin.close()
        output, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == (0 if admission == "go" else 70), error.decode(errors="replace")
        assert marker.exists() == (admission == "go")
        assert output.strip() == (b"ready" if admission == "go" else b"")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,runtime_pid", [
    ("success", 202), ("same", 101), ("first-failure", 202), ("second-failure", 202),
    ("exception", 202), ("drain", 202), ("cancelled", 202), ("invalid", 0),
])
async def test_windows_gate_admission_owns_every_pid_and_closes_once(monkeypatch, mode, runtime_pid):
    from types import SimpleNamespace

    from app.core.transcript_views import child_process

    calls, handles = [], []

    class Input:
        def write(self, data):
            assert data == b"G"
            assert [pid for op, pid in calls if op == "assign"] == list(dict.fromkeys((101, runtime_pid)))
            calls.append(("write", data))

        async def drain(self):
            if mode == "drain":
                raise OSError("synthetic broken gate pipe")
            if mode == "cancelled":
                raise asyncio.CancelledError

        def close(self):
            calls.append(("close-input", None))

    class Output:
        async def readexactly(self, amount):
            assert amount == 4
            return runtime_pid.to_bytes(4, "big")

    def assign(pid):
        calls.append(("assign", pid))
        if (mode == "first-failure" and pid == 101) or (mode == "second-failure" and pid == 202):
            return None
        if mode == "exception" and pid == 202:
            raise OSError("synthetic second assignment error")
        handle = pid + 1000
        handles.append(handle)
        return handle

    async def wait():
        return 0

    process = SimpleNamespace(pid=101, stdin=Input(), stdout=Output(), returncode=0, wait=wait)
    monkeypatch.setattr(child_process, "_job_object", lambda: SimpleNamespace(
        assign_kill_on_close_job=assign, close_job=lambda handle: calls.append(("close-job", handle))))
    if mode in ("success", "same"):
        await child_process._admit_windows(process)
        await child_process.stop(process)
    else:
        error = asyncio.CancelledError if mode == "cancelled" else OSError
        with pytest.raises(error):
            await child_process._admit_windows(process)
    assert [handle for op, handle in calls if op == "close-job"] == handles
    assert any(op == "write" for op, value in calls) == (mode in ("success", "same", "drain", "cancelled"))
    await child_process.stop(process)
    assert [handle for op, handle in calls if op == "close-job"] == handles


@pytest.fixture(scope="module")
def startup_python(tmp_path_factory):
    # A fresh private venv, not the worker's environment: its installed .pth hook
    # creates a real descendant before the script's first line, unless -S gates it.
    import venv

    directory = tmp_path_factory.mktemp("projection-startup-python")
    venv.EnvBuilder(with_pip=False).create(directory)
    packages = directory / ("Lib/site-packages" if os.name == "nt" else
                            f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    packages.mkdir(parents=True, exist_ok=True)
    hook = r"""
import os, subprocess, sys
from pathlib import Path
if Path(sys.argv[-1]).name == "startup_python.py":
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", "import time;time.sleep(30)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
    marker = Path(sys.argv[-1]).parent / "grandchild-pid"
    marker.with_suffix(".tmp").write_text(str(child.pid))
    marker.with_suffix(".tmp").replace(marker)
"""
    (packages / "projection_startup_fixture.py").write_text(hook)
    (packages / "projection_startup_fixture.pth").write_text("import projection_startup_fixture\n")
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.mark.asyncio
async def test_python_gate_defers_installed_site_hook_until_admission(tmp_path, startup_python):
    from app.core.transcript_views.child_process import _windows_command

    target, entry, marker = (tmp_path / name for name in ("startup_python.py", "entry", "grandchild-pid"))
    target.write_text("from pathlib import Path;Path(" + repr(str(entry)) + ").touch();print('ready')")
    process = await asyncio.create_subprocess_exec(
        *_windows_command((str(startup_python), "-I", str(target)), "python"),
        env=parent_environment(tmp_path), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        runtime_pid = int.from_bytes(await asyncio.wait_for(process.stdout.readexactly(4), 5), "big")
        assert runtime_pid > 0 and alive(runtime_pid)
        await asyncio.sleep(.15)
        assert process.returncode is None and not entry.exists() and not marker.exists()
        process.stdin.write(b"G")
        await process.stdin.drain()
        output, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode(errors="replace")
        assert output.strip() == b"ready" and entry.exists() and marker.exists()
        assert alive(int(marker.read_text())), "the installed startup hook must actually have run"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if marker.exists() and alive(int(marker.read_text())):
            descendant = psutil.Process(int(marker.read_text()))
            # A private Windows venv Popen PID can itself be a redirector.
            for member in reversed([descendant, *descendant.children(recursive=True)]):
                try:
                    member.kill()
                except psutil.NoSuchProcess:
                    pass


STARTUP_PARENT = r'''
import asyncio, json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / "fluid")]
from app.core.transcript_views import child_process
runtime, executable, target, mode = sys.argv[2:]
marker = Path(target).parent / "grandchild-pid"
entry = Path(target).parent / "entry"
seen = {}

async def main():
    # The supervising test attaches its externally held outer job before B.
    assert os.read(0, 1) == b"B"
    jobs = child_process._job_object()
    original = jobs.assign_kill_on_close_job
    original_admit = child_process._admit_windows
    current = None
    async def admit(process):
        nonlocal current
        current = process
        await original_admit(process)
    child_process._admit_windows = admit
    def assign(pid):
        seen["pid"] = current.pid
        seen["runtime_pid"] = current._projection_runtime_pid
        if runtime == "python":
            # EnvBuilder on Windows uses a real PE redirector. Keep this a
            # two-PID regression rather than silently testing the base exe.
            assert seen["pid"] != seen["runtime_pid"], "private venv must exercise runtime redirection"
        if mode == "launch-death":
            print(json.dumps(seen), flush=True)
            os.read(0, 1)  # The test kills us in this pre-assignment window.
            raise AssertionError("launch-death parent unexpectedly released")
        # Enlarge the creation/assignment window. An ungated script/site hook
        # can create an escaping descendant here, before retroactive assignment.
        time.sleep(.4)
        fail = mode == "failure" or (mode == "partial-failure" and pid == seen["runtime_pid"])
        job = None if fail else original(pid)
        if mode == "assigned-death" and pid == seen["runtime_pid"]:
            assert job is not None, "cannot attach launch-death fixture"
            print(json.dumps(seen), flush=True)
            os.read(0, 1)  # Attached, but no go byte has been returned/written.
            raise AssertionError("assigned-death parent unexpectedly released")
        return job
    jobs.assign_kill_on_close_job = assign
    command = (executable, "-I", target) if runtime == "python" else (
        executable, "--max-old-space-size=192", target)
    child = None
    try:
        try:
            child = await child_process.launch(*command, limit=4096, runtime=runtime)
        except OSError:
            seen["failed"] = True
        else:
            deadline = time.monotonic() + 5
            while not (marker.exists() and entry.exists()):
                assert child.returncode is None, "startup child exited"
                assert time.monotonic() < deadline, "startup descendant not observed"
                await asyncio.sleep(.01)
            seen["grandchild"] = int(marker.read_text())
            seen["failed"] = False
        print(json.dumps(seen), flush=True)
        os.read(0, 1)
    finally:
        await child_process.stop(child)

asyncio.run(main())
'''


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows startup/Job ownership")
@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize("mode", ["success", "failure", "partial-failure", "launch-death", "assigned-death", "nested"])
async def test_windows_owns_startup_descendants_and_launch_window(tmp_path, startup_python, runtime, mode):
    from app.core.transcript_views.child_process import _job_object

    target = tmp_path / ("startup_python.py" if runtime == "python" else "startup_node.cjs")
    grandchild = tmp_path / "grandchild-pid"
    entry = tmp_path / "entry"
    if runtime == "python":
        # The .pth site hook, not this entrypoint, creates the descendant.
        target.write_text("from pathlib import Path;import time;Path(" + repr(str(entry)) +
                          ").touch();time.sleep(30)")
        executable = str(startup_python)
    else:
        target.write_text("if(require.main!==module)process.exit(72);const fs=require('node:fs');const child=require('node:child_process').spawn("
                          "process.execPath,['-e','setTimeout(()=>{},30000)'],{stdio:'ignore',windowsHide:true});"
                          "const marker=" + json.dumps(str(grandchild)) + ";fs.writeFileSync(marker+'.tmp',"
                          "String(child.pid));fs.renameSync(marker+'.tmp',marker);fs.writeFileSync(" +
                          json.dumps(str(entry)) + ",'');setInterval(()=>{},1000);")
        executable = shutil.which("node")
        assert executable, "Node is a required projection runtime"
    output = tmp_path / "startup-parent-output"
    with output.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-S", "-c", STARTUP_PARENT, str(ROOT), runtime, executable, str(target), mode,
            cwd=ROOT, env=parent_environment(tmp_path), stdin=asyncio.subprocess.PIPE, stdout=stream, stderr=stream)
    outer, owned = None, set()
    try:
        if mode == "nested":
            # Keep the outer handle in THIS supervisor, not the killed parent:
            # outer-job closure must not mask a failed inner ownership boundary.
            outer = _job_object().assign_kill_on_close_job(process.pid)
            assert outer is not None, "cannot create explicit nested-job fixture"
        process.stdin.write(b"B")
        await process.stdin.drain()

        async def ready():
            while True:
                text = output.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("{"):
                        return json.loads(line)
                assert process.returncode is None, text
                await asyncio.sleep(.01)
        stats = await asyncio.wait_for(ready(), 15)
        owned.update((stats["pid"], stats["runtime_pid"]))
        if mode in ("success", "nested"):
            assert not stats["failed"] and grandchild.exists() and entry.exists(), stats
            owned.add(stats["grandchild"])
            assert all(alive(pid) for pid in owned), stats
        elif mode in ("failure", "partial-failure"):
            assert stats["failed"] and not alive(stats["pid"]), stats
            assert not grandchild.exists() and not entry.exists()
        process.kill()
        await process.wait()
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in owned) and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert not any(alive(pid) for pid in owned), stats
        if mode in ("launch-death", "assigned-death"):
            assert not grandchild.exists() and not entry.exists()
    finally:
        if grandchild.exists():
            owned.add(int(grandchild.read_text()))
        if process.returncode is None:
            try:
                owned.update(child.pid for child in psutil.Process(process.pid).children(recursive=True))
            except psutil.NoSuchProcess:
                pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()
        # These are solely this fixture's known descendants; never shared workers.
        for pid in owned:
            if alive(pid):
                psutil.Process(pid).kill()
        if outer is not None:
            _job_object().close_job(outer)
