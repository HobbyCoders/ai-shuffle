"""Only marked, stale, unused scratch can be removed; no live app required."""
import json
import os
import time
from pathlib import Path

import pytest

from app.core import scratch_reaper as reaper
from app.core.temp_scratch import MARKER, PREFIXES, process_identity, scratch_directory

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc scratch safety")


@pytest.fixture(autouse=True)
def visible_processes(tmp_path, monkeypatch):
    # Real descriptors and cwd of this process; deterministic proc view excludes
    # unrelated inaccessible processes. A production scan fails closed on those.
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / str(os.getpid())).symlink_to(Path("/proc") / str(os.getpid()), target_is_directory=True)
    original = reaper.sweep
    monkeypatch.setattr(reaper, "sweep", lambda *a, **kw: original(*a, **{"proc": proc, **kw}))


def stale(root, prefix=PREFIXES[0]):
    path = root / (prefix + "fixture")
    path.mkdir()
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
