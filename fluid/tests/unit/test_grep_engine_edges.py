"""Grep engine edges with a fake ripgrep (timeouts, errors, credential-file
withholding, single-file targets, the global cap) and the pure-Python
fallback's own validation and bounds; plus the shared walk/glob helpers."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from fluid.tools.builtins import _util
from fluid.tools.builtins import grep as grep_mod
from fluid.tools.builtins._util import (
    GlobSyntaxError,
    WalkState,
    compile_glob,
    glob_to_regex,
    is_binary,
    missing_path_note,
    walk_files,
)
from fluid.tools.registry import ToolCtx


class FakeRgProc:
    """Stands in for an rg subprocess: canned stdout and stderr streams, an exit
    status, and a record of being killed. ``hang`` keeps both streams open and
    the process alive until it is killed. Build it inside a running loop."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self._exited = asyncio.Event()
        if not hang:
            self._exit(returncode)

    def _exit(self, code: int) -> None:
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self._exit(-9)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


_Proc = FakeRgProc


@pytest.fixture
def fake_rg(monkeypatch):
    state: dict = {"calls": []}

    async def fake_exec(*args, **kwargs):
        state["calls"].append((list(args), kwargs))
        return state["proc"]

    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    return state


async def _grep(tmp_path: Path, **input):
    return await grep_mod.handler({"pattern": "needle", **input}, ToolCtx(cwd=str(tmp_path)))


async def test_rg_timeout_kills_and_reports(tmp_path, fake_rg, monkeypatch) -> None:
    fake_rg["proc"] = proc = _Proc(hang=True)
    monkeypatch.setattr(grep_mod, "RG_TIMEOUT_S", 0.01)
    outcome = await _grep(tmp_path)
    assert outcome.is_error and "search timed out after 0.01s" in outcome.text
    assert proc.killed


async def test_rg_error_exit_is_an_error_with_stderr(tmp_path, fake_rg) -> None:
    fake_rg["proc"] = _Proc(returncode=2, stderr=b"regex parse error: unclosed group")
    outcome = await _grep(tmp_path, pattern="(")
    assert outcome.is_error and "rg error: regex parse error" in outcome.text
    fake_rg["proc"] = _Proc(returncode=2)
    assert "rg exited 2" in (await _grep(tmp_path)).text


async def test_rg_withholds_credential_file_hits_and_caps_globally(tmp_path, fake_rg) -> None:
    # The credential hit arrives before the cap: the stream stops at the cap,
    # so a hit after it is never read and never counted.
    lines = ["./.env:1:needle=secret"] + [f"./src/f{i}.py:{i}:needle" for i in range(5)]
    fake_rg["proc"] = _Proc(stdout=("\n".join(lines) + "\n").encode())
    outcome = await _grep(tmp_path, limit=3)
    assert "needle=secret" not in outcome.text
    assert "hits in credential-pattern files" in outcome.text
    assert "[search truncated: match cap 3 reached" in outcome.text
    assert f"{tmp_path}{os.sep}src/f0.py:0:needle" in outcome.text


async def test_rg_single_file_target_runs_in_its_parent(tmp_path, fake_rg) -> None:
    target = tmp_path / "one.py"
    target.write_text("needle\n")
    fake_rg["proc"] = _Proc(stdout=b"one.py:1:needle\n")
    outcome = await _grep(tmp_path, path=str(target), glob="*.py", no_ignore=True)
    args, kwargs = fake_rg["calls"][0]
    assert kwargs["cwd"] == str(tmp_path) and args[-1] == "one.py"
    assert "-uu" in args and "--glob" in args
    assert f"{tmp_path / 'one.py'}:1:needle" in outcome.text


async def test_repeat_search_returns_a_pointer(tmp_path, fake_rg) -> None:
    fake_rg["proc"] = _Proc(stdout=b"./a.py:1:needle\n")
    ctx = ToolCtx(cwd=str(tmp_path))
    first = await grep_mod.handler({"pattern": "needle"}, ctx)
    second = await grep_mod.handler({"pattern": "needle"}, ctx)
    assert "a.py:1:needle" in first.text
    assert second.text != first.text and "a.py:1:needle" not in second.text


# ─── pure-Python fallback ────────────────────────────────────────────────────


@pytest.fixture
def no_rg(monkeypatch):
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)


async def test_fallback_invalid_pattern_and_bad_glob(tmp_path, no_rg) -> None:
    outcome = await _grep(tmp_path, pattern="(")
    assert outcome.is_error and "invalid pattern" in outcome.text
    outcome = await _grep(tmp_path, glob="*.{py")
    assert outcome.is_error and "unsupported glob" in outcome.text


async def test_fallback_withholds_credential_files_and_skips_big_or_binary(
    tmp_path, no_rg, monkeypatch
) -> None:
    (tmp_path / ".env").write_text("needle=secret\n")
    (tmp_path / "bin.dat").write_bytes(b"needle\0\0")
    (tmp_path / "big.txt").write_text("needle\n" * 10)
    (tmp_path / "ok.txt").write_text("needle\n")
    monkeypatch.setattr(grep_mod, "MAX_FILE_BYTES", 20)
    outcome = await _grep(tmp_path)
    assert "ok.txt:1:needle" in outcome.text
    assert "secret" not in outcome.text and "bin.dat" not in outcome.text
    assert "big.txt" not in outcome.text
    assert "hits in credential-pattern files" in outcome.text


async def test_fallback_cap_and_deadline_notes(tmp_path, no_rg, monkeypatch) -> None:
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("needle\nneedle\n")
    outcome = await _grep(tmp_path, limit=3)
    assert "match cap 3 reached" in outcome.text

    monkeypatch.setattr(grep_mod, "FALLBACK_DEADLINE_S", -1.0)
    outcome = await _grep(tmp_path)
    assert "deadline reached after 0 files" in outcome.text


def test_glob_hit_outside_root_uses_the_name() -> None:
    flt = compile_glob("*.py")
    assert grep_mod._glob_hit(flt, Path("/elsewhere/x.py"), Path("/root"))


def test_search_file_bounds(tmp_path) -> None:
    import re

    rx = re.compile("n")
    f = tmp_path / "a.txt"
    f.write_text("n\nn\nn\n")
    assert list(grep_mod._search_file(f, rx, 0)) == []
    assert len(list(grep_mod._search_file(f, rx, 2))) == 2
    assert list(grep_mod._search_file(tmp_path / "missing", rx, 5)) == []


# ─── walk / glob helpers ─────────────────────────────────────────────────────


def test_walk_files_single_file_and_deadline(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    state = WalkState()
    assert list(walk_files(f, state)) == [f] and state.files_seen == 1

    (tmp_path / "b.txt").write_text("y")
    expired = WalkState(deadline=time.monotonic() - 1)
    assert list(walk_files(tmp_path, expired)) == [] and expired.deadline_hit


def test_walk_files_deadline_mid_directory(tmp_path, monkeypatch) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name)
    ticks = iter([0.0, 0.0, 0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(_util.time, "monotonic", lambda: next(ticks, 100.0))
    state = WalkState(deadline=50.0)
    seen = list(walk_files(tmp_path, state))
    assert 0 < len(seen) < 3 and state.deadline_hit


def test_is_binary_unreadable_counts_as_binary(tmp_path) -> None:
    assert is_binary(tmp_path / "missing") is True


@pytest.mark.parametrize("pattern", ["[abc", "{a,b", "!"])
def test_glob_syntax_errors(pattern) -> None:
    with pytest.raises(GlobSyntaxError):
        compile_glob(pattern)


def test_glob_leading_bang_needs_compile_glob_and_classes_negate() -> None:
    with pytest.raises(GlobSyntaxError):
        glob_to_regex("!x")
    rx = glob_to_regex("file[!0-9].txt", case_sensitive=True)
    assert rx.match("filea.txt") and not rx.match("file1.txt")
    assert glob_to_regex("F?.PY", case_sensitive=False).match("src/fa.py")
    assert compile_glob("!vendor/**").matches("src/a.py")
    assert not compile_glob("!vendor/**").matches("vendor/x.py")


def test_missing_path_note_variants(tmp_path) -> None:
    real = tmp_path / "real.py"
    real.write_text("x")
    note = missing_path_note(f"{real}:12:x = 1", tmp_path / "nope")
    assert "looks like a Grep result line" in note
    assert "parent directory" in missing_path_note("", tmp_path / "no" / "such")
    assert "did you mean" in missing_path_note("", tmp_path / "reel.py")
