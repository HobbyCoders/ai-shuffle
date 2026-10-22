"""Only marked, stale, unused scratch can be removed; no live app required."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import scratch_reaper as reaper
from app.core.temp_scratch import MARKER, PREFIXES, process_identity, scratch_directory

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc scratch safety")


@pytest.fixture(autouse=True)
def visible_processes(tmp_path, monkeypatch):
    # Deterministic process view for deletion cases. A separate test below scans
    # the real /proc, including inaccessible container init processes.
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / str(os.getpid())).symlink_to(Path("/proc") / str(os.getpid()), target_is_directory=True)
    original = reaper.sweep
    monkeypatch.setattr(reaper, "sweep", lambda *a, **kw: original(*a, **{"proc": proc, **kw}))


def stale(root, prefix=PREFIXES[0]):
    path = root / (prefix + "fixture")
    path.mkdir(mode=0o700)
    (path / "payload").write_bytes(b"x" * 4096)
    (path / MARKER).write_text(json.dumps({"version": 1, "pid": 99999999, "start": "0",
                                          "created": 1, "uid": os.getuid()}))
    for entry in [*path.iterdir(), path]:
        os.utime(entry, (1, 1))
    return path


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc")
def test_stale_receipted_directory_is_removed(tmp_path):
    path = stale(tmp_path)
    assert reaper.sweep(tmp_path, ttl=1) == 1
    assert not path.exists()


@pytest.mark.parametrize("prefix", ["tmp", "probe-", "cdp-", "ci-", "uwui", "worktree-", "user-"])
def test_generic_prefix_is_never_owned(tmp_path, prefix):
    path = stale(tmp_path, prefix)
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert path.exists()


@pytest.mark.parametrize("kind", ["missing", "invalid", "symlink", "new", "live", "wrong_uid"])
def test_receipt_must_prove_stale_dead_owner(tmp_path, kind):
    path = stale(tmp_path)
    marker = path / MARKER
    if kind == "missing":
        marker.unlink()
    elif kind == "invalid":
        marker.write_text("not json")
    elif kind == "symlink":
        marker.rename(tmp_path / "external")
        marker.symlink_to(tmp_path / "external")
    else:
        receipt = json.loads(marker.read_text())
        if kind == "new":
            receipt["created"] = time.time()
        elif kind == "live":
            receipt.update(pid=os.getpid(), start=process_identity(os.getpid()))
        else:
            receipt["uid"] += 1
        marker.write_text(json.dumps(receipt))
        os.utime(marker, (1, 1))
    assert reaper.sweep(tmp_path, ttl=60) == 0
    assert path.exists()


@pytest.mark.parametrize("reference", ["fd", "cwd"])
def test_open_or_cwd_directory_survives(tmp_path, reference):
    path = stale(tmp_path)
    old = Path.cwd()
    stream = None
    try:
        if reference == "fd":
            stream = (path / "payload").open("rb")
        else:
            os.chdir(path)
        assert reaper.sweep(tmp_path, ttl=1) == 0
        assert path.exists()
    finally:
        os.chdir(old)
        if stream:
            stream.close()
    assert reaper.sweep(tmp_path, ttl=1) == 1


def test_symlink_root_and_nested_links_do_not_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "user-file").write_text("keep")
    linked = tmp_path / (PREFIXES[1] + "link")
    linked.symlink_to(outside, target_is_directory=True)
    path = stale(tmp_path)
    (path / "link").symlink_to(outside, target_is_directory=True)
    os.utime(path / "link", (1, 1), follow_symlinks=False)
    os.utime(path, (1, 1))
    assert reaper.sweep(tmp_path, ttl=1) == 1
    assert linked.is_symlink() and (outside / "user-file").read_text() == "keep"


def test_owned_context_cleans_up_on_error():
    with pytest.raises(RuntimeError), scratch_directory(PREFIXES[0]) as directory:
        assert (Path(directory) / MARKER).is_file()
        raise RuntimeError("fixture")
    assert not Path(directory).exists()


def test_worktrees_and_recently_used_payloads_survive(tmp_path):
    path = stale(tmp_path)
    (path / ".git").write_text("gitdir: never delete")
    os.utime(path / ".git", (1, 1))
    os.utime(path, (1, 1))
    assert reaper.sweep(tmp_path, ttl=1) == 0
    (path / ".git").unlink()
    (path / "payload").write_text("recently used")
    os.utime(path, (1, 1))
    assert reaper.sweep(tmp_path, ttl=1) == 0


def test_oversized_tree_is_skipped(tmp_path, monkeypatch):
    path = stale(tmp_path)
    monkeypatch.setattr(reaper, "MAX_TREE_ENTRIES", 1)
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert path.exists()


def test_process_scan_failure_is_fail_closed(tmp_path):
    path = stale(tmp_path)
    assert reaper.sweep(tmp_path, ttl=1, proc=tmp_path / "no-proc") == 0
    assert path.exists()


def test_process_permission_failure_is_fail_closed(tmp_path, monkeypatch):
    path = stale(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("unreadable process metadata")

    monkeypatch.setattr(reaper, "_references", denied)
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert path.exists()


def test_nonregular_receipt_is_skipped_without_blocking(tmp_path):
    path = stale(tmp_path)
    marker = path / MARKER
    marker.unlink()
    os.mkfifo(marker)
    os.utime(marker, (1, 1))
    os.utime(path, (1, 1))
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert path.exists()


def test_another_live_scratch_owner_survives(tmp_path):
    # Drive the real receipt producer in a separate session-like process. Even
    # artificially aging its entire tree must not make a live lease disposable.
    code = (
        "import time; from app.core.temp_scratch import scratch_directory; "
        "from pathlib import Path\n"
        "with scratch_directory('ai-shuffle-preview-') as directory:\n"
        "    print(directory, flush=True)\n"
        "    time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path)},
    )
    try:
        path = Path(child.stdout.readline().strip())
        assert path.parent == tmp_path
        marker = path / MARKER
        receipt = json.loads(marker.read_text())
        assert receipt["pid"] == child.pid
        receipt["created"] = 1
        marker.write_text(json.dumps(receipt))
        os.utime(marker, (1, 1))
        os.utime(path, (1, 1))
        # Include the other owner's /proc identity in the deterministic view.
        (tmp_path / "proc" / str(child.pid)).symlink_to(Path("/proc") / str(child.pid))
        assert reaper.sweep(tmp_path, ttl=1) == 0
        assert path.exists()
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.parametrize("module, exempt", [("app.main", True), ("other.main", False)])
def test_non_dumpable_sibling(tmp_path, module, exempt):
    package = tmp_path / module.split(".")[0]
    package.mkdir()
    # The stand-in runs with the real entry-point argv and real prctl hardening,
    # and creates its receipt through the production scratch owner helper.
    (package / "__init__.py").write_text(
        f"__path__.append({str(Path(reaper.__file__).parents[1])!r})\n"
        if exempt else "")
    (package / "main.py").write_text(
        "import ctypes, sys\n"
        "from app.core.temp_scratch import scratch_directory\n"
        "assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0\n"
        f"with scratch_directory('ai-shuffle-probe-', root={str(tmp_path)!r}) as directory:\n"
        "    print(directory, flush=True)\n"
        "    sys.stdin.read()\n")
    child = subprocess.Popen(
        [sys.executable, "-m", module], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(Path(reaper.__file__).parents[2])])},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        owned = Path(child.stdout.readline().strip())
        assert owned.parent == tmp_path
        base = Path("/proc") / str(child.pid)
        try:
            with os.scandir(base / "fd"):
                pass
        except PermissionError:
            pass
        else:
            pytest.skip("host permits inspection of non-dumpable siblings")
        proc = tmp_path / "proc"
        (proc / str(child.pid)).symlink_to(base)
        # Even artificially aged app scratch is protected by its live receipt.
        receipt = json.loads((owned / MARKER).read_text())
        receipt["created"] = 1
        (owned / MARKER).write_text(json.dumps(receipt))
        os.utime(owned / MARKER, (1, 1))
        os.utime(owned, (1, 1))
        dead = stale(tmp_path)
        references = reaper._references(proc, time.monotonic() + 5)
        assert (references is not None) == exempt
        assert reaper.sweep(tmp_path, ttl=1) == int(exempt)
        assert owned.exists()
        assert dead.exists() != exempt
    finally:
        child.communicate(timeout=5)
    assert child.returncode == 0
    assert not owned.exists()


def test_real_proc_scan_is_usable_or_identifies_opaque_same_uid(monkeypatch, caplog):
    # Normal app startup makes app/runner processes non-dumpable. Known app
    # instances and init ancestors must not disable a sweep; unknown opaque
    # same-UID processes still fail closed.
    opaque_same_uid = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            status = (process / "status").read_text().splitlines()
            uids = next(line for line in status if line.startswith("Uid:")).split()[1:]
            if str(os.getuid()) not in uids:
                continue
            try:
                with os.scandir(process / "fd") as descriptors:
                    for descriptor in descriptors:
                        os.readlink(descriptor.path)
                os.readlink(process / "cwd")
                os.readlink(process / "root")
            except PermissionError:
                if not reaper._uninspectable_is_unrelated(process, Path("/proc")):
                    opaque_same_uid.append(process.name)
        except (FileNotFoundError, ProcessLookupError):
            continue
    monkeypatch.setattr(reaper, "_disabled_logged", False)
    references = reaper._references(Path("/proc"), time.monotonic() + reaper.SWEEP_SECONDS)
    if opaque_same_uid:
        assert references is None
        assert "potentially relevant process is not inspectable" in caplog.text
    else:
        assert references is not None
        assert str(Path.cwd()) in references


@pytest.mark.parametrize("kind, safe", [("other_uid", True), ("same_uid", False),
                                       ("root", False), ("init", True), ("unrelated_init", False)])
def test_unreadable_process_is_classified_per_process(tmp_path, monkeypatch, caplog, kind, safe):
    proc = tmp_path / "classification-proc"
    proc.mkdir()
    target = proc / "1"
    target.mkdir()
    uid = os.getuid() if kind == "same_uid" else 0
    if kind == "other_uid":
        uid = max(os.getuid(), 1) + 1
    name = "tini" if kind in {"init", "unrelated_init"} else "unknown"
    (target / "status").write_text(f"Name:\t{name}\nUid:\t{uid} {uid} {uid} {uid}\nPPid:\t0\n")
    own = proc / str(os.getpid())
    own.mkdir()
    parent = 1 if kind == "init" else 0
    (own / "status").write_text(f"PPid:\t{parent}\n")
    (own / "fd").mkdir()
    (own / "cwd").symlink_to(tmp_path)
    original = os.scandir

    def scan(path):
        if Path(path) == target / "fd":
            raise PermissionError("uninspectable")
        return original(path)

    monkeypatch.setattr(reaper.os, "scandir", scan)
    monkeypatch.setattr(reaper, "_disabled_logged", False)
    for _ in range(2):
        references = reaper._references(proc, time.monotonic() + 5)
        assert (references is not None) == safe
        if safe:
            assert str(tmp_path) in references
    assert sum("sweep disabled" in message for message in caplog.messages) == int(not safe)


@pytest.mark.parametrize("argv, name, ancestor, safe", [
    (["/venv/bin/python3.12", "-m", "app.main"], "python3.12", False, True),
    (["python", "-s", "-m", "app.main"], "python", False, True),
    (["python", "-m", "app.preview_process"], "python", False, True),
    (["python", "-m", "app.main.extra"], "python", False, False),
    (["python", "-m", "other", "app.main"], "python", False, False),
    (["python", "-c", "import app.main"], "python", False, False),
    (["python", "-m", "app.main", "unexpected"], "python", False, False),
    (["not-python", "-m", "app.main"], "not-python", False, False),
    (["python", "-m", "app.main"], "unknown", False, False),
    (["python", "/source/docker/container_runtime.py", "run"], "python", True, True),
    (["python", "/source/docker/container_runtime.py", "run"], "python", False, False),
    (["python", "/source/docker/container_runtime.py", "dump"], "python", True, False),
    (["python", "/source/other/container_runtime.py", "run"], "python", True, False),
    (["python", "docker/container_runtime.py", "run"], "python", True, False),
])
def test_opaque_application_entry_points(tmp_path, argv, name, ancestor, safe):
    proc = tmp_path / "entry-point-proc"
    target = proc / "1"
    target.mkdir(parents=True)
    uid = os.getuid()
    (target / "status").write_text(f"Name:\t{name}\nUid:\t{uid} {uid} {uid} {uid}\nPPid:\t0\n")
    (target / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")
    own = proc / str(os.getpid())
    own.mkdir()
    (own / "status").write_text(f"PPid:\t{int(ancestor)}\n")
    assert reaper._uninspectable_is_unrelated(target, proc) == safe


@pytest.fixture
def opaque_python_process(tmp_path, monkeypatch):
    proc = tmp_path / "opaque-proc"
    target = proc / "123"
    target.mkdir(parents=True)
    uid = os.getuid()
    (target / "status").write_text(f"Name:\tpython\nUid:\t{uid} {uid} {uid} {uid}\n")
    original = os.scandir

    def scan(path):
        if not isinstance(path, int) and Path(path) == target / "fd":
            raise PermissionError("uninspectable")
        return original(path)

    monkeypatch.setattr(reaper.os, "scandir", scan)
    return proc, target


@pytest.mark.parametrize("module", ["app.main", "app.preview_process"])
def test_opaque_app_keeps_dead_owner_preview_only(tmp_path, opaque_python_process, module):
    proc, target = opaque_python_process
    (target / "cmdline").write_bytes(b"python\0-m\0" + module.encode() + b"\0")
    preview = stale(tmp_path, "ai-shuffle-preview-")
    pytest_tree = stale(tmp_path, "ai-shuffle-pytest-")
    ci_tree = stale(tmp_path, "ai-shuffle-ci-")

    assert reaper.sweep(tmp_path, proc=proc) == 2
    assert preview.exists()
    assert not pytest_tree.exists()
    assert not ci_tree.exists()
    # The exemption is local to this scan, not a permanent preview opt-out.
    assert reaper.sweep(tmp_path, proc=tmp_path / "proc") == 1
    assert not preview.exists()


@pytest.mark.parametrize("error", [ImportError, ModuleNotFoundError])
def test_shim_import_failure_disables_sweep_once(tmp_path, opaque_python_process, monkeypatch, caplog, error):
    import builtins

    proc, target = opaque_python_process
    (target / "cmdline").write_bytes(b"python\0-s\0-c\0shim\0")
    path = stale(tmp_path)
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "docker.agent_isolation":
            raise error("unavailable shim")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(reaper, "_disabled_logged", False)
    for _ in range(2):
        assert reaper.sweep(tmp_path, proc=proc) == 0
        assert path.exists()
    assert sum("sweep disabled" in message for message in caplog.messages) == 1
    assert "potentially relevant process is not inspectable" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_opaque_agent_isolation_shim_requires_exact_code(tmp_path):
    from docker.agent_isolation import APP_SHIM

    target = tmp_path / "shim-proc"
    target.mkdir()
    uid = os.getuid()
    (target / "status").write_text(f"Name:\tpython\nUid:\t{uid} {uid} {uid} {uid}\n")
    cmdline = target / "cmdline"
    for code, safe in [(APP_SHIM, True), (APP_SHIM + "print('other code')\n", False)]:
        cmdline.write_bytes(b"python\0-s\0-c\0" + code.encode() + b"\0")
        assert reaper._uninspectable_is_unrelated(target, tmp_path) == safe


def test_descriptors_have_a_separate_larger_bound(tmp_path, monkeypatch):
    proc = tmp_path / "descriptor-proc"
    descriptors = proc / "1" / "fd"
    descriptors.mkdir(parents=True)
    for index in range(3):
        (descriptors / str(index)).symlink_to(tmp_path)
    monkeypatch.setattr(reaper, "MAX_ENTRIES", 1)
    assert reaper._references(proc, time.monotonic() + 5) == {str(tmp_path)}
    monkeypatch.setattr(reaper, "MAX_DESCRIPTORS", 2)
    assert reaper._references(proc, time.monotonic() + 5) is None


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("kind", ["objects", "commondir", "gitdir"])
def test_separate_git_directories_are_never_scratch(tmp_path, nested, kind):
    path = stale(tmp_path)
    store = path / "nested" / "store" if nested else path
    store.mkdir(parents=True, exist_ok=True)
    if kind == "objects":
        (store / "HEAD").write_text("ref: refs/heads/main\n")
        (store / "objects").mkdir()
        os.utime(store / "objects", (1, 1))
        os.utime(store / "HEAD", (1, 1))
    else:
        (store / kind).write_text("/outside/repository\n")
        os.utime(store / kind, (1, 1))
    for directory in [store, store.parent, path]:
        os.utime(directory, (1, 1))
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert path.exists()


def test_real_separate_git_store_is_preserved(tmp_path):
    path = stale(tmp_path)
    subprocess.run(["git", "init", "--separate-git-dir=" + str(path / "store"),
                    str(tmp_path / "checkout")], check=True, capture_output=True)
    for directory, _, files in os.walk(path):
        for name in files:
            os.utime(Path(directory) / name, (1, 1))
        os.utime(directory, (1, 1))
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert subprocess.run(["git", "-C", str(tmp_path / "checkout"), "status", "--porcelain"],
                          capture_output=True).returncode == 0


def test_nonprivate_scratch_is_not_reaped(tmp_path):
    path = stale(tmp_path)
    path.chmod(0o755)
    assert reaper.sweep(tmp_path, ttl=1) == 0
    assert path.exists()
