"""File-tool I/O safety and result-size bounds.

Edit and Write never truncate a file before its new bytes are known to be
writable (an in-place save reserves the space first); Edit leaves the line endings of untouched lines alone; Read and Grep
hold a bounded amount of a file however long its lines are; the Grep fallback
skips hidden files as ripgrep does; an MCP server's paginated tools/list is
followed to the end (within bounds); and MCP tool aliases stay within the
provider's 64-character limit without two tools sharing one name.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest
from fluid.tools.builtins import _util
from fluid.tools.builtins import edit as edit_mod
from fluid.tools.builtins import grep as grep_mod
from fluid.tools.builtins import read as read_mod
from fluid.tools.builtins import write as write_mod
from fluid.tools.mcp import client as client_mod
from fluid.tools.mcp.client import McpClient
from fluid.tools.registry import API_TOOL_NAME_MAX, ToolCtx, api_tool_name

from tests.unit.test_grep_engine_edges import FakeRgProc

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")


def _ctx(path: Path) -> ToolCtx:
    ctx = ToolCtx(cwd=str(path.parent))
    ctx.read_files.add(str(path))
    return ctx


async def _edit(path: Path, **kw):
    return await edit_mod.handler({"file_path": str(path), **kw}, _ctx(path))


async def _write(path: Path, content: str):
    return await write_mod.handler({"file_path": str(path), "content": content}, _ctx(path))


# ── (a) writes never lose the old file ───────────────────────────────────────


async def test_unencodable_edit_and_write_leave_the_file_intact(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"keep me\n")
    outcome = await _edit(target, old_string="keep", new_string="bad \ud800")
    assert outcome.is_error and "unchanged" in outcome.text
    assert target.read_bytes() == b"keep me\n"
    outcome = await _write(target, "bad \ud800\n")
    assert outcome.is_error and "nothing was written" in outcome.text
    assert target.read_bytes() == b"keep me\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]  # no temp left


async def test_unencodable_write_creates_no_new_file(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    outcome = await write_mod.handler(
        {"file_path": str(target), "content": "\udfff"}, ToolCtx(cwd=str(tmp_path))
    )
    assert outcome.is_error and not target.exists()


def test_failed_write_keeps_the_original(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"original\n")

    def boom(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_util.os, "fsync", boom)
    with pytest.raises(OSError):
        _util.write_text_atomic(target, "replacement\n")
    assert target.read_bytes() == b"original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


@posix_only
def test_replace_keeps_mode_and_writes_through_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.sh"
    real.write_bytes(b"old\n")
    real.chmod(0o751)
    link = tmp_path / "link.sh"
    link.symlink_to(real)
    _util.write_text_atomic(link, "new\n")
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert real.read_bytes() == b"new\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o751


def test_replace_refused_falls_back_to_writing_in_place(tmp_path: Path, monkeypatch) -> None:
    """Windows refuses os.replace onto a file another process holds open."""
    target = tmp_path / "held.txt"
    target.write_bytes(b"old\n")

    def refuse(_src, _dst) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(_util.os, "replace", refuse)
    _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["held.txt"]


@posix_only
def test_long_basename_still_saves_atomically(tmp_path: Path, monkeypatch) -> None:
    """A 250-byte name fits the 255-byte limit; its temp name must too."""
    target = tmp_path / ("n" * 246 + ".txt")
    target.write_bytes(b"old\n")
    before = target.stat().st_ino
    prefixes: list[str] = []
    real_mkstemp = _util.tempfile.mkstemp

    def spy(*a, **kw):
        prefixes.append(kw.get("prefix", ""))
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(_util.tempfile, "mkstemp", spy)
    _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"new\n"
    assert target.stat().st_ino != before  # replaced, not written in place
    assert prefixes and len(prefixes[0]) < 16
    assert sorted(p.name for p in tmp_path.iterdir()) == [target.name]


@pytest.mark.parametrize(
    "code", [errno.ENOSPC, errno.EROFS, errno.ENAMETOOLONG], ids=errno.errorcode.get
)
async def test_temp_create_failure_leaves_the_original_intact(
    tmp_path: Path, monkeypatch, code: int
) -> None:
    """A full disk (or read-only fs) must not fall back to truncating the file."""
    target = tmp_path / "a.txt"
    target.write_bytes(b"original\r\nbytes\n")

    def full(*_a, **_kw):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(_util.tempfile, "mkstemp", full)
    outcome = await _write(target, "replacement\n")
    assert outcome.is_error
    assert target.read_bytes() == b"original\r\nbytes\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


def test_unexpected_replace_failure_raises_and_removes_the_temp(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"original\n")

    def broken(_src, _dst) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(_util.os, "replace", broken)
    with pytest.raises(OSError):
        _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


async def test_file_that_cannot_be_opened_for_writing_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """os.access is not trusted (it ignores NTFS ACLs): a real open decides."""
    target = tmp_path / "locked.txt"
    target.write_bytes(b"locked\n")
    real_open = _util.os.open
    locked = os.path.realpath(target)

    def guarded(path, flags, *a, **kw):
        if os.fspath(path) == locked and flags & (os.O_WRONLY | os.O_RDWR):
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(_util.os, "open", guarded)
    monkeypatch.setattr(_util.os, "access", lambda *_a, **_kw: True)
    outcome = await _write(target, "new\n")
    assert outcome.is_error
    assert target.read_bytes() == b"locked\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["locked.txt"]


async def test_edit_of_a_hard_linked_file_updates_every_name(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"value = 1\n")
    other = tmp_path / "b.txt"
    try:
        os.link(target, other)
    except OSError as exc:  # a filesystem without hard links
        pytest.skip(f"no hard links here: {exc}")
    outcome = await _edit(target, old_string="1", new_string="2")
    assert not outcome.is_error, outcome.text
    assert target.read_bytes() == other.read_bytes() == b"value = 2\n"
    assert os.path.samefile(target, other)


@posix_only
def test_file_with_a_posix_acl_is_written_in_place(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "shared.txt"
    target.write_bytes(b"old\n")
    before = target.stat().st_ino
    monkeypatch.setattr(
        _util.os, "listxattr", lambda _p: ["system.posix_acl_access"], raising=False
    )
    _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"new\n"
    assert target.stat().st_ino == before  # same inode: the ACL stays as it was


def _busy(_src, _dst) -> None:
    """A single-file bind mount: Linux refuses the rename onto it."""
    raise OSError(errno.EBUSY, "Device or resource busy")


async def test_in_place_save_that_cannot_reserve_space_leaves_the_original(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "mounted.txt"
    target.write_bytes(b"original\r\nbytes\n")

    def full(fd: int, _offset: int, length: int) -> None:
        os.ftruncate(fd, length)  # a partial preallocation that grew the file
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(_util.os, "posix_fallocate", full, raising=False)
    outcome = await _write(target, "a much longer replacement than before\n")
    assert outcome.is_error
    assert target.read_bytes() == b"original\r\nbytes\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mounted.txt"]


@pytest.mark.parametrize(
    "new", ["short\n", "a good deal longer than the old text\n", ""],
    ids=["shrink", "grow", "empty"],
)
def test_busy_replace_overwrites_in_place_without_truncating(
    tmp_path: Path, monkeypatch, new: str
) -> None:
    old = b"the old contents here\n"
    target = tmp_path / "mounted.txt"
    target.write_bytes(old)
    before = target.stat().st_ino
    reserved: list[tuple[int, int]] = []

    def fallocate(_fd: int, offset: int, length: int) -> None:
        reserved.append((offset, length))

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(_util.os, "posix_fallocate", fallocate, raising=False)
    _util.write_text_atomic(target, new)
    assert target.read_bytes() == new.encode()
    assert target.stat().st_ino == before
    # Only what the new contents use: shrinking a sparse file under a quota
    # needs no new blocks, and an empty file needs no reservation at all.
    assert reserved == ([(0, len(new))] if new else [])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mounted.txt"]


def _no_fallocate(*_a) -> None:
    raise OSError(errno.EOPNOTSUPP, "Operation not supported")


def test_in_place_growth_is_fsynced_before_any_old_byte_changes(
    tmp_path: Path, monkeypatch
) -> None:
    """Delayed writeback (async NFS): ENOSPC first shows at fsync of the tail."""
    old = b"0123456789\n"
    target = tmp_path / "held.txt"
    target.write_bytes(old)
    held = target.stat().st_ino
    real_fsync = _util.os.fsync

    def late_enospc(fd: int) -> None:
        if os.fstat(fd).st_ino == held:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(_util.os, "posix_fallocate", _no_fallocate, raising=False)
    monkeypatch.setattr(_util.os, "fsync", late_enospc)
    with pytest.raises(OSError):
        _util.write_text_atomic(target, "y" * 40)
    assert target.read_bytes() == old


@pytest.mark.parametrize("fallocate_works", [False, True], ids=["tail-first", "fallocate"])
def test_refused_first_overwrite_drops_the_growth(
    tmp_path: Path, monkeypatch, fallocate_works: bool
) -> None:
    """A byte-range lock on the old contents refuses the head write outright."""
    old = b"0123456789\n"
    target = tmp_path / "locked.txt"
    target.write_bytes(old)
    real_write_at = _util._write_at

    def extends(fd: int, _offset: int, length: int) -> None:
        if length > os.fstat(fd).st_size:
            os.ftruncate(fd, length)  # posix_fallocate grows the file

    def locked_head(fd: int, data, offset: int) -> None:
        if offset == 0:
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        real_write_at(fd, data, offset)

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(
        _util.os, "posix_fallocate", extends if fallocate_works else _no_fallocate,
        raising=False,
    )
    monkeypatch.setattr(_util, "_write_at", locked_head)
    with pytest.raises(PermissionError):
        _util.write_text_atomic(target, "y" * 40)
    assert target.read_bytes() == old


def test_fallocate_ebadf_takes_the_tail_first_path(tmp_path: Path, monkeypatch) -> None:
    """glibc's emulation (NFSv3) reads a byte, which a write-only fd may not."""
    old = b"0123456789\n"
    target = tmp_path / "nfs.txt"
    target.write_bytes(old)
    writes: list[int] = []
    real_write_at = _util._write_at

    def emulated(*_a) -> None:
        raise OSError(errno.EBADF, "Bad file descriptor")

    def spy(fd: int, data, offset: int) -> None:
        writes.append(offset)
        real_write_at(fd, data, offset)

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(_util.os, "posix_fallocate", emulated, raising=False)
    monkeypatch.setattr(_util, "_write_at", spy)
    _util.write_text_atomic(target, "z" * 30)
    assert target.read_bytes() == b"z" * 30
    assert writes == [len(old), 0]


@pytest.mark.parametrize("new", ["tiny\n", "x" * 50 + "\n"])
def test_in_place_save_without_fallocate_writes_the_growth_first(
    tmp_path: Path, monkeypatch, new: str
) -> None:
    old = b"0123456789abcdef\n"
    target = tmp_path / "held.txt"
    target.write_bytes(old)
    writes: list[tuple[int, bytes]] = []
    real_write_at = _util._write_at

    def unsupported(*_a) -> None:
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    def spy(fd: int, data, offset: int) -> None:
        writes.append((offset, bytes(data)))
        real_write_at(fd, data, offset)

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(_util.os, "posix_fallocate", unsupported, raising=False)
    monkeypatch.setattr(_util, "_write_at", spy)
    _util.write_text_atomic(target, new)
    data = new.encode()
    assert target.read_bytes() == data
    if len(data) > len(old):
        assert writes == [(len(old), data[len(old):]), (0, data[: len(old)])]
    else:
        assert writes == [(0, data)]


def test_in_place_growth_that_hits_a_full_disk_restores_the_length(
    tmp_path: Path, monkeypatch
) -> None:
    old = b"0123456789\n"
    target = tmp_path / "held.txt"
    target.write_bytes(old)
    real_write_at = _util._write_at

    def unsupported(*_a) -> None:
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    def fills_up(fd: int, data, offset: int) -> None:
        real_write_at(fd, bytes(data)[:3], offset)  # some of the tail lands
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_util.os, "replace", _busy)
    monkeypatch.setattr(_util.os, "posix_fallocate", unsupported, raising=False)
    monkeypatch.setattr(_util, "_write_at", fills_up)
    with pytest.raises(OSError):
        _util.write_text_atomic(target, "y" * 40)
    assert target.read_bytes() == old


def test_cross_device_replace_raises_instead_of_writing_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    """The temp is a sibling, so EXDEV is unexpected: keep the original."""
    target = tmp_path / "a.txt"
    target.write_bytes(b"original\n")

    def cross(_src, _dst) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(_util.os, "replace", cross)
    with pytest.raises(OSError):
        _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]


def _is_temp(path) -> bool:
    return Path(path).name.startswith(_util._TEMP_PREFIX)


@posix_only
@pytest.mark.parametrize("strip_fails", [False, True], ids=["stripped", "in-place"])
def test_acl_inherited_by_the_temp_is_stripped_before_chmod(
    tmp_path: Path, monkeypatch, strip_fails: bool
) -> None:
    """A directory default ACL must not reach a target that had no ACL."""
    target = tmp_path / "plain.txt"
    target.write_bytes(b"old\n")
    target.chmod(0o660)
    before = target.stat().st_ino
    removed: list[tuple[str, str]] = []
    real_chmod = _util.os.chmod

    def listxattr(path) -> list[str]:
        acl_left = _is_temp(path) and not removed
        return ["system.posix_acl_access"] if acl_left else []

    def removexattr(path, name) -> None:
        if strip_fails:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        removed.append((Path(path).name, name))

    def chmod(path, mode, **kw) -> None:
        assert removed or not _is_temp(path), "chmod before the ACL was removed"
        real_chmod(path, mode, **kw)

    monkeypatch.setattr(_util.os, "listxattr", listxattr, raising=False)
    monkeypatch.setattr(_util.os, "removexattr", removexattr, raising=False)
    monkeypatch.setattr(_util.os, "getxattr", lambda *_a, **_k: b"", raising=False)
    monkeypatch.setattr(_util.os, "chmod", chmod)
    _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o660
    assert sorted(p.name for p in tmp_path.iterdir()) == ["plain.txt"]
    if strip_fails:
        assert target.stat().st_ino == before  # written in place, never replaced
    else:
        assert target.stat().st_ino != before
        assert [name for _, name in removed] == ["system.posix_acl_access"]
        assert all(_is_temp(path) for path, _ in removed)


@posix_only
@pytest.mark.parametrize("relabel_fails", [False, True], ids=["copied", "in-place"])
def test_selinux_label_of_the_target_is_copied_to_the_temp(
    tmp_path: Path, monkeypatch, relabel_fails: bool
) -> None:
    target = tmp_path / "labelled.txt"
    target.write_bytes(b"old\n")
    before = target.stat().st_ino
    target_label = b"system_u:object_r:httpd_sys_content_t:s0\x00"
    labels: dict[str, bytes] = {}

    def getxattr(path, name, **_kw) -> bytes:
        assert name == "security.selinux"
        if not _is_temp(path):
            return target_label
        return labels.get(os.fspath(path), b"unconfined_u:object_r:user_tmp_t:s0\x00")

    def setxattr(path, name, value, *_a, **_kw) -> None:
        if relabel_fails:
            raise PermissionError(errno.EACCES, "Permission denied")
        assert _is_temp(path) and name == "security.selinux"
        labels[os.fspath(path)] = value

    monkeypatch.setattr(_util.os, "listxattr", lambda *_a, **_k: [], raising=False)
    monkeypatch.setattr(_util.os, "getxattr", getxattr, raising=False)
    monkeypatch.setattr(_util.os, "setxattr", setxattr, raising=False)
    _util.write_text_atomic(target, "new\n")
    assert target.read_bytes() == b"new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["labelled.txt"]
    if relabel_fails:
        assert target.stat().st_ino == before
    else:
        assert target.stat().st_ino != before
        assert list(labels.values()) == [target_label]


# ── (b) Edit leaves untouched line endings alone ─────────────────────────────


async def test_edit_on_mixed_endings_changes_only_the_replaced_bytes(tmp_path: Path) -> None:
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\nfour\r\nfive\n")
    outcome = await _edit(target, old_string="three\nfour", new_string="THREE\nFOUR\nextra")
    assert not outcome.is_error, outcome.text
    # The replaced text held one LF break, so the new breaks are LF; every
    # other byte, the CRLF after "four" included, is what it was.
    assert target.read_bytes() == b"one\r\ntwo\r\nTHREE\nFOUR\nextra\r\nfive\n"


async def test_single_line_edit_takes_its_lines_ending(tmp_path: Path) -> None:
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"a\nb\r\nc\n")
    outcome = await _edit(target, old_string="b", new_string="b1\nb2")
    assert not outcome.is_error, outcome.text
    assert target.read_bytes() == b"a\nb1\r\nb2\r\nc\n"


async def test_indent_fallback_on_mixed_endings(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_bytes(b"x = 1\r\nif x:\n    y = 2\n    z = 3\nw = 4\r\n")
    outcome = await _edit(target, old_string="y = 2\nz = 3", new_string="y = 20\nz = 30")
    assert not outcome.is_error, outcome.text
    assert target.read_bytes() == b"x = 1\r\nif x:\n    y = 20\n    z = 30\nw = 4\r\n"


# ── (c) Read holds a bounded slice of a giant line, off the event loop ───────


async def test_read_of_a_giant_single_line_stays_bounded(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "big.json"
    target.write_bytes(b'{"k": "' + b"x" * (16 * 1024 * 1024) + b'"}\n' + b"tail\n")
    threads: list[object] = []
    real_to_thread = asyncio.to_thread

    async def spy(fn, *a, **kw):
        threads.append(fn)
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(read_mod.asyncio, "to_thread", spy)
    tracemalloc.start()
    try:
        outcome = await read_mod.handler(
            {"file_path": str(target), "limit": 1}, _ctx(target)
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert read_mod._read_text in threads
    assert "[line truncated]" in outcome.text and "offset=2" in outcome.text
    assert peak < 4 * 1024 * 1024, peak  # was > 16MB: the whole line in memory


def test_read_skips_a_giant_line_before_the_offset(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_bytes(b"y" * (3 * 1024 * 1024) + b"\nsecond\nthird")
    text, more, count = read_mod._read_text(target, 2, None)
    assert text == "2\tsecond\n3\tthird" and not more and count == 2


# ── (d) Grep bounds retained bytes, not just matches ─────────────────────────


async def test_rg_stops_at_the_output_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(grep_mod, "MAX_OUTPUT_CHARS", 10_000)
    rows = b"".join(
        b"./f%d.js\x001:" % i + b"needle" * 1000 + b"\n" for i in range(50)
    )
    procs: list[FakeRgProc] = []

    async def fake_exec(*args, **kwargs):
        procs.append(FakeRgProc(stdout=rows, hang=True))
        return procs[-1]

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    out = await grep_mod.handler({"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))
    assert not out.is_error
    assert "[search truncated: output budget of 10000 characters reached" in out.text
    assert out.text.count("[line truncated]") == 4  # 2,000-char lines, 10,000 budget
    assert procs[0].killed


def test_fallback_clips_lines_and_stops_at_the_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep_mod, "MAX_OUTPUT_CHARS", 5_000)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("needle" * 1000 + "\n")
    out = grep_mod._pure_python_grep("needle", tmp_path, None)
    assert "[search truncated: output budget of 5000 characters reached" in out.text
    assert out.text.count("[line truncated]") == 2


async def test_rg_line_reader_keeps_at_most_max_bytes_per_line() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"abcdefghijklmnop\nshort\n" + b"x" * 30)
    stream.feed_data(b"yyyy\ntail")
    stream.feed_eof()
    lines = [line async for line in grep_mod._byte_lines(stream, max_bytes=10)]
    assert lines == [b"abcdefghij", b"short", b"x" * 10, b"tail"]


async def test_rg_line_reader_holds_a_bounded_amount_across_many_reads() -> None:
    """One endless line arriving in many chunks: what is held stays under the cap."""
    held: list[int] = []

    class _Stream:
        def __init__(self) -> None:
            self.chunks = [b"z" * 65536] * 20 + [b"z\nnext\n"]
            self.reader = None

        async def read(self, _n: int) -> bytes:
            pending = self.reader.ag_frame.f_locals["pending"]
            held.append(sum(len(part) for part in pending))
            return self.chunks.pop(0) if self.chunks else b""

    stream = _Stream()
    stream.reader = grep_mod._byte_lines(stream, max_bytes=1000)
    lines = [line async for line in stream.reader]
    assert len(held) == 22 and max(held) <= 1000
    assert lines == [b"z" * 1000, b"next"]


# ── (e) the Grep fallback skips hidden files unless no_ignore ────────────────


def test_fallback_skips_hidden_files_and_says_ignore_files_are_not_read(
    tmp_path: Path,
) -> None:
    (tmp_path / "shown.py").write_text("needle\n")
    (tmp_path / ".hidden.py").write_text("needle\n")
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "s.toml").write_text("needle\n")
    default = grep_mod._pure_python_grep("needle", tmp_path, None).text
    assert "shown.py" in default and ".hidden" not in default and ".config" not in default
    assert ".gitignore not applied" in default.splitlines()[0]
    everything = grep_mod._pure_python_grep("needle", tmp_path, None, no_ignore=True).text
    assert ".hidden.py" in everything and "s.toml" in everything
    assert ".gitignore" not in everything.splitlines()[0]


# ── (f) MCP tools/list follows nextCursor within bounds ──────────────────────


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description="", inputSchema={"type": "object"})


class _PagedSession:
    """``pages`` maps a cursor (None = first) to (tool names, next cursor)."""

    def __init__(self, pages, fail_on: str | None = None) -> None:
        self.pages = pages
        self.fail_on = fail_on
        self.cursors: list[str | None] = []

    async def list_tools(self, cursor=None, *, params=None):
        cursor = params.cursor if params is not None else cursor
        self.cursors.append(cursor)
        if cursor is not None and cursor == self.fail_on:
            raise RuntimeError("boom")
        names, nxt = self.pages[cursor]
        return SimpleNamespace(tools=[_tool(n) for n in names], nextCursor=nxt, ttlMs=60_000)


async def test_mcp_list_collects_every_page() -> None:
    client = McpClient("srv", {})
    session = _PagedSession({None: (["a"], "p2"), "p2": (["b"], "p3"), "p3": (["c"], None)})
    tools = await client._do_list(session)
    assert [t.name for t in tools] == ["a", "b", "c"]
    assert session.cursors == [None, "p2", "p3"]
    assert client.catalog_incomplete is None
    assert client._cache is not None and client._cache.expires_at is not None


async def test_mcp_list_reports_a_partial_catalog_and_does_not_cache_it() -> None:
    client = McpClient("srv", {})
    session = _PagedSession({None: (["a"], "p2"), "p2": (["b"], "p3")}, fail_on="p3")
    tools = await client._do_list(session)
    assert [t.name for t in tools] == ["a", "b"]
    assert "page 3 failed" in (client.catalog_incomplete or "")
    assert client._cache is not None and client._cache.expires_at is None


async def test_mcp_list_stops_at_the_page_cap_and_on_a_repeated_cursor(monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "MAX_LIST_PAGES", 3)
    endless = {None: (["t0"], "c1")}
    endless.update({f"c{i}": ([f"t{i}"], f"c{i + 1}") for i in range(1, 10)})
    client = McpClient("srv", {})
    tools = await client._do_list(_PagedSession(endless))
    assert len(tools) == 3 and "3-page cap" in (client.catalog_incomplete or "")

    looping = _PagedSession({None: (["a"], "x"), "x": (["b"], "x")})
    tools = await McpClient("srv", {})._do_list(looping)
    assert [t.name for t in tools] == ["a", "b"] and looping.cursors == [None, "x"]


async def test_mcp_list_first_page_stall_fails_at_the_deadline() -> None:
    class _Stalled:
        async def list_tools(self, *a, **kw):
            await asyncio.Event().wait()

    client = McpClient("srv", {"call_timeout": 0.05})
    with pytest.raises(TimeoutError, match="tools/list within 0.05 s"):
        await asyncio.wait_for(client._do_list(_Stalled()), 5)
    assert client._cache is None


# ── (g) MCP aliases: at most 64 characters, one name per tool ────────────────


def test_alias_fits_even_with_a_long_server_name() -> None:
    name = api_tool_name("s" * 80, "tool", lambda _n: False)
    assert len(name) == API_TOOL_NAME_MAX
    assert name == api_tool_name("s" * 80, "tool", lambda _n: False)  # deterministic


def test_servers_that_normalise_alike_get_distinct_long_aliases() -> None:
    registered: dict[str, tuple[str, str]] = {}

    def register(server: str, tool: str) -> str:
        name = api_tool_name(
            server, tool, lambda c: c in registered and registered[c] != (server, tool)
        )
        assert len(name) <= API_TOOL_NAME_MAX
        registered[name] = (server, tool)
        return name

    tool = "t" * 70
    names = {register(server, tool) for server in ("a.b", "a b", "a+b")}
    assert len(names) == 3 and len(registered) == 3
    # Re-registering the same tool maps back to the name it already has.
    assert register("a b", tool) in names and len(registered) == 3
    short = {register(server, "x") for server in ("c.d", "c d", "c+d")}
    assert len(short) == 3
