"""Windows regression: an orphan Git must not outlive its preview job."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil
import pytest

from app import preview_process


def test_entry_refuses_non_preview(monkeypatch):
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    with pytest.raises(RuntimeError, match="only for source previews"):
        preview_process.main()


def test_entry_contains_before_importing_worker(monkeypatch):
    events = []
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    monkeypatch.setattr(preview_process, "contain_windows_preview", lambda: events.append("contain"))
    monkeypatch.setattr(preview_process.runpy, "run_module", lambda *a, **k: events.append((a, k)))
    preview_process.main()
    assert events == ["contain", (("app.main",), {"run_name": "__main__"})]


def test_entry_does_not_start_worker_if_containment_fails(monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    def refused():
        raise RuntimeError("job assignment refused")
    monkeypatch.setattr(preview_process, "contain_windows_preview", refused)
    monkeypatch.setattr(preview_process.runpy, "run_module", lambda *a, **k: pytest.fail("worker started"))
    with pytest.raises(RuntimeError, match="job assignment refused"):
        preview_process.main()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects and directory locks")
@pytest.mark.parametrize("contained,forced", [(False, False), (True, False), (True, True)])
def test_orphan_git_profile_lock(tmp_path, contained, forced):
    git = shutil.which("git")
    assert git, "Git is required for the Windows preview lifecycle regression"
    profile = tmp_path / "profile"
    profile.mkdir()
    sleeper = tmp_path / "hold.py"
    sleeper.write_text("import time; time.sleep(120)")
    # Git's alias shell holds the private cwd without touching a real repository.
    alias = f'alias.hold=!"{Path(sys.executable).as_posix()}" "{sleeper.as_posix()}"'
    intermediate = (
        "import subprocess,psutil,json; "
        f"p=subprocess.Popen({[git, '-c', alias, 'hold']!r}, cwd={str(profile)!r}, "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(json.dumps({'pid':p.pid,'created':psutil.Process(p.pid).create_time()}),flush=True)"
    )
    root_code = (
        "import subprocess,sys; "
        + ("from app.preview_process import contain_windows_preview; contain_windows_preview(); " if contained else "")
        + f"subprocess.run([sys.executable,'-c',{intermediate!r}],check=True); "
        "sys.stdin.readline()"
    )
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo) + os.pathsep + str(repo / "fluid")}
    root = subprocess.Popen([sys.executable, "-c", root_code], cwd=tmp_path, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # A sibling is deliberately outside the job; it must survive both exit paths.
    sibling = subprocess.Popen([sys.executable, str(sleeper)], cwd=tmp_path)
    owned = []
    try:
        info = json.loads(root.stdout.readline())
        orphan = psutil.Process(info["pid"])
        assert orphan.create_time() == info["created"]
        owned = [orphan]
        deadline = time.monotonic() + 10
        while not orphan.children(recursive=True) and time.monotonic() < deadline:
            time.sleep(0.02)
        owned += orphan.children(recursive=True)
        assert len(owned) > 1, "real Git alias did not start"
        assert orphan.ppid() != root.pid
        assert not psutil.pid_exists(orphan.ppid()), "intermediate parent must have exited"
        with pytest.raises(PermissionError):
            profile.rmdir()
        if forced:
            root.kill()
        else:
            root.stdin.write("stop\n")
            root.stdin.flush()
        root.wait(timeout=10)
        assert sibling.poll() is None, "unowned sibling was terminated"
        if contained:
            gone, alive = psutil.wait_procs(owned, timeout=10)
            assert not alive, f"owned descendants survived: {[p.pid for p in alive]}"
            profile.rmdir()  # Real Windows lock release, not just a PID assertion.
        else:
            assert orphan.is_running(), "baseline did not reproduce the orphan"
            with pytest.raises(PermissionError):
                profile.rmdir()
    finally:
        if root.poll() is None:
            root.kill()
        root.communicate(timeout=10)
        # psutil Process.kill checks the captured start time against PID reuse.
        for process in reversed(owned):
            if process.is_running():
                process.kill()
        psutil.wait_procs(owned, timeout=10)
        sibling.kill()
        sibling.wait(timeout=10)
