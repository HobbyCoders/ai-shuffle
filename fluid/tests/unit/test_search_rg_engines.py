"""Glob and Grep give the same answer whichever engine runs (issue #1620).

Glob lists files with ``rg --files`` when ripgrep is installed and with the
Python walk otherwise; Grep searches with rg or with the Python fallback. The
parity tests build one tree and compare the engines' output line for line,
which is what "the engine is an implementation detail" has to mean for a model
that reads these results. They need a real ripgrep and skip without one.

The early-exit tests pin the Grep stream: rg is stopped once the cap is proven
exceeded, without waiting for the rest of its output.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import time
from pathlib import Path

import pytest

from fluid.tools.builtins import _util
from fluid.tools.builtins import glob as glob_mod
from fluid.tools.builtins import grep as grep_mod
from fluid.tools.builtins._util import (
    WalkState,
    list_rel_files,
    rg_prune_globs,
    walk_files,
)
from fluid.tools.registry import ToolCtx
from tests.unit.test_grep_engine_edges import FakeRgProc

RG = shutil.which("rg")
needs_rg = pytest.mark.skipif(RG is None, reason="ripgrep not installed")


def _write(root: Path, rel: str, data: bytes | str = b"needle here\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _parity_tree(root: Path) -> None:
    """Everything the two engines could disagree about, in one tree."""
    # Plain sources, nested and anchored.
    _write(root, "app.py")
    _write(root, "README.md", "needle in the readme\n")
    _write(root, "src/a.py")
    _write(root, "src/b.ts", "const needle = 1;\n")
    _write(root, "src/deep/c.py", "no match on this line\nneedle twice\nneedle\n")
    _write(root, "docs/guide.md", "needle in the docs\n")
    _write(root, "docs/notes.txt", "nothing\n")
    _write(root, "with space/file name.py")
    _write(root, "ünïcode/naïve.py")
    # Hidden files and directories: listed by both (the walk has no hidden rule).
    _write(root, ".hidden.py")
    _write(root, ".config/settings.toml", "needle = true\n")
    # Ignore files are not honoured by Glob (the walk never read them) nor by
    # Grep under no_ignore.
    _write(root, ".gitignore", "*.log\ngenerated/\n")
    _write(root, ".ignore", "src/\n")
    _write(root, "debug.log", "needle in a log\n")
    _write(root, "generated/out.py")
    # Pruned directories, at the root and nested, and a FILE named like one.
    for rel in (
        "node_modules/pkg/index.js", ".git/config", "build/out.py",
        "src/__pycache__/a.cpython-312.pyc", "sub/dist/bundle.js",
        "sub/venv/lib/site.py", "packages/x/target/debug.rs",
    ):
        _write(root, rel)
    _write(root, "sub/build", "needle in a file called build\n")
    # Credential-pattern files: listed by Glob, withheld by Grep.
    _write(root, ".env", "API_TOKEN=needle-secret\n")
    _write(root, "keys/id_rsa", "needle-private-key\n")
    # Skipped by Grep in both engines: binary content and files over 2MB.
    _write(root, "blob.bin", b"needle\0\0\0")
    _write(root, "huge.txt", b"needle\n" + b"x" * (3 * 1024 * 1024))
    # Distinct mtimes so "newest first" has one right answer.
    base = time.time() - 10_000
    for i, path in enumerate(sorted(p for p in root.rglob("*") if p.is_file())):
        os.utime(path, (base + i, base + i))


GLOB_PATTERNS = [
    "*",
    "*.py",
    "**/*.py",
    "src/*.py",
    "src/**/*.py",
    "*.{md,txt}",
    "**/[ab]*.py",
    ".*",
    "**/.env",
    "!{src/**,docs/**}",
    "sub/*",
    "*.nope",
]


@needs_rg
@pytest.mark.parametrize("pattern", GLOB_PATTERNS)
def test_glob_engines_agree(tmp_path, pattern) -> None:
    _parity_tree(tmp_path)
    via_rg = glob_mod._glob(pattern, tmp_path, glob_mod.MAX_LIMIT, rg=RG).text
    via_walk = glob_mod._glob(pattern, tmp_path, glob_mod.MAX_LIMIT, rg=None).text
    assert via_rg == via_walk


@needs_rg
def test_glob_engines_agree_on_the_tree_and_the_cap(tmp_path) -> None:
    _parity_tree(tmp_path)
    listed = glob_mod._glob("*", tmp_path, glob_mod.MAX_LIMIT, rg=RG).text.splitlines()[1:]
    # The prune list holds; a file that merely shares a pruned name does not.
    assert "sub/build" in listed and ".env" in listed and "debug.log" in listed
    assert not any(
        part in {"node_modules", ".git", "build", "__pycache__", "dist", "venv", "target"}
        for line in listed for part in line.split("/")[:-1]
    )
    # The newest-first cap and its note are the same text from both engines.
    capped_rg = glob_mod._glob("**/*.py", tmp_path, 3, rg=RG).text
    capped_walk = glob_mod._glob("**/*.py", tmp_path, 3, rg=None).text
    assert capped_rg == capped_walk
    assert "[showing newest 3 of" in capped_rg


def _walk_rels(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in walk_files(root))


@needs_rg
def test_rg_listing_matches_the_walk(tmp_path) -> None:
    _parity_tree(tmp_path)
    rg_state, walk_state = WalkState(), WalkState()
    via_rg = sorted(list_rel_files(tmp_path, rg_state, RG))
    via_walk = sorted(list_rel_files(tmp_path, walk_state, None))
    assert via_rg == via_walk == _walk_rels(tmp_path)
    assert "ünïcode/naïve.py" in via_rg and "with space/file name.py" in via_rg
    assert rg_state.files_seen == walk_state.files_seen == len(via_rg)
    assert not rg_state.deadline_hit


def test_rel_listing_of_a_file_root_is_dot(tmp_path) -> None:
    target = _write(tmp_path, "one.py")
    for rg in (RG, None):
        assert list(list_rel_files(target, WalkState(), rg)) == ["."]


@needs_rg
def test_glob_ceiling_and_deadline_notes_match(tmp_path, monkeypatch) -> None:
    _parity_tree(tmp_path)
    monkeypatch.setattr(glob_mod, "MAX_LIMIT", 4)
    for rg in (RG, None):
        text = glob_mod._glob("*", tmp_path, 2, rg=rg).text
        assert "[walk stopped: hard ceiling 4 matched files reached]" in text
        assert "[showing newest 2 of 4 matched files" in text

    monkeypatch.setattr(glob_mod, "DEADLINE_S", 0.0)
    for rg in (RG, None):
        text = glob_mod._glob("*", tmp_path, 2, rg=rg).text
        assert text.splitlines()[1] == "(no matches)"
        assert "[search truncated: deadline reached after 0 files]" in text


def test_glob_falls_back_when_rg_cannot_start(tmp_path) -> None:
    _parity_tree(tmp_path)
    missing = str(tmp_path / "no-such-rg")
    assert sorted(list_rel_files(tmp_path, WalkState(), missing)) == _walk_rels(tmp_path)
    assert (
        glob_mod._glob("**/*.py", tmp_path, 50, rg=missing).text
        == glob_mod._glob("**/*.py", tmp_path, 50, rg=None).text
    )


class _ListingProc:
    """A finished ``rg --files`` with canned output, for subprocess.Popen."""

    def __init__(self, stdout: bytes, returncode: int) -> None:
        self.stdout = io.BytesIO(stdout)
        self._code = returncode
        self.killed = False

    def poll(self):
        return self._code

    def wait(self):
        return self._code

    def kill(self) -> None:  # pragma: no cover - only for a live process
        self.killed = True


def test_rg_listing_error_with_nothing_listed_falls_back_to_the_walk(
    tmp_path, monkeypatch
) -> None:
    _write(tmp_path, "src/a.py")
    _write(tmp_path, "b.py")
    monkeypatch.setattr(
        _util.subprocess, "Popen", lambda *a, **k: _ListingProc(b"", 2)
    )
    assert sorted(list_rel_files(tmp_path, WalkState(), "rg")) == ["b.py", "src/a.py"]


def test_rg_listing_error_after_partial_output_keeps_the_partial(
    tmp_path, monkeypatch
) -> None:
    """An unreadable directory makes rg exit 2 after listing the rest; the walk
    skips such a directory silently, so the listing stands."""
    _write(tmp_path, "src/a.py")
    _write(tmp_path, "b.py")
    monkeypatch.setattr(
        _util.subprocess, "Popen", lambda *a, **k: _ListingProc(b"b.py\0", 2)
    )
    assert list(list_rel_files(tmp_path, WalkState(), "rg")) == ["b.py"]


def test_rg_listing_decodes_null_separated_posix_paths(tmp_path, monkeypatch) -> None:
    out = "src/a b.py\0./x.py\0ünï/ç.py\0".encode()
    monkeypatch.setattr(_util.subprocess, "Popen", lambda *a, **k: _ListingProc(out, 0))
    assert list(list_rel_files(tmp_path, WalkState(), "rg")) == [
        "src/a b.py", "x.py", "ünï/ç.py",
    ]


def test_rg_listing_args_prune_like_the_walk(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_popen(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return _ListingProc(b"", 1)

    monkeypatch.setattr(_util.subprocess, "Popen", fake_popen)
    assert list(list_rel_files(tmp_path, WalkState(), "rg")) == []
    args = seen["args"]
    assert args[:2] == ["rg", "--files"]
    assert {"--no-config", "-uu", "--null"} <= set(args)
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    excluded = {args[i + 1] for i, a in enumerate(args) if a == "--glob"}
    assert excluded == {f"!{name}/" for name in _util.PRUNE_DIRS}
    assert rg_prune_globs() == [a for a in args if a == "--glob" or a in excluded]


# ── Grep ─────────────────────────────────────────────────────────────────────


def _body(text: str) -> tuple[list[str], list[str]]:
    """(sorted match lines, notes) from a Grep result, header dropped."""
    lines = text.splitlines()[1:]
    notes = [ln for ln in lines if ln.startswith("[")]
    return sorted(ln for ln in lines if not ln.startswith("[")), notes


async def _grep_both(root: Path, monkeypatch, **input) -> tuple[str, str]:
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: RG)
    ctx = ToolCtx(cwd=str(root))
    via_rg = (await grep_mod.handler({"pattern": "needle", **input}, ctx)).text
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: None)
    ctx = ToolCtx(cwd=str(root))
    via_py = (await grep_mod.handler({"pattern": "needle", **input}, ctx)).text
    assert via_rg.startswith(f"searched via rg under {root}")
    assert via_py.startswith(f"searched via python-fallback under {root}")
    return via_rg, via_py


@needs_rg
@pytest.mark.parametrize("glob", [None, "*.py", "src/**", "!{src/**,docs/**}", "*.{md,toml}"])
async def test_grep_engines_agree_under_no_ignore(tmp_path, monkeypatch, glob) -> None:
    """With no_ignore the Python fallback and rg search the same files, and
    print the same absolute ``path:line:content`` lines."""
    _parity_tree(tmp_path)
    extra = {"glob": glob} if glob else {}
    via_rg, via_py = await _grep_both(tmp_path, monkeypatch, no_ignore=True, **extra)
    assert via_rg.startswith(f"searched via rg under {tmp_path}")
    assert via_py.startswith(f"searched via python-fallback under {tmp_path}")
    assert _body(via_rg) == _body(via_py)
    for line in _body(via_rg)[0]:
        if line != "(no matches)":
            match = grep_mod._MATCH_LINE_RE.match(line)
            assert match is not None, line
            assert Path(match.group(1)).is_absolute(), line
            assert Path(match.group(1)).is_file(), line


@needs_rg
async def test_grep_withholds_credential_hits_in_both_engines(tmp_path, monkeypatch) -> None:
    _parity_tree(tmp_path)
    for text in await _grep_both(tmp_path, monkeypatch, no_ignore=True):
        assert "needle-secret" not in text and "needle-private-key" not in text
        assert "hits in credential-pattern files" in text
        assert f"{tmp_path}{os.sep}app.py:1:needle here" in text
    # Skipped for content reasons by both: binary, over 2MB, pruned dirs.
    for text in await _grep_both(tmp_path, monkeypatch, no_ignore=True):
        for absent in ("blob.bin", "huge.txt", "node_modules", f"build{os.sep}out.py"):
            assert absent not in text


@needs_rg
async def test_grep_rg_prunes_heavy_dirs_only_under_no_ignore(tmp_path) -> None:
    # No .gitignore here: without no_ignore rg's own rules decide (hidden .git
    # is skipped, node_modules is searched), which is unchanged.
    _write(tmp_path, "node_modules/pkg/index.js")
    _write(tmp_path, "src/a.py")
    ctx = ToolCtx(cwd=str(tmp_path))
    plain = (await grep_mod.handler({"pattern": "needle"}, ctx)).text
    assert "index.js" in plain
    ctx = ToolCtx(cwd=str(tmp_path))
    unignored = (await grep_mod.handler({"pattern": "needle", "no_ignore": True}, ctx)).text
    assert "index.js" not in unignored and "a.py" in unignored
    # A path inside a pruned directory is still searchable.
    inside = await grep_mod.handler(
        {"pattern": "needle", "no_ignore": True, "path": str(tmp_path / "node_modules")},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert "index.js:1:needle" in inside.text


@needs_rg
async def test_grep_rg_stops_at_the_cap(tmp_path, monkeypatch) -> None:
    for i in range(60):
        _write(tmp_path, f"f{i:02}.txt", "needle\n" * 20)
    kills: list[object] = []
    real_kill = grep_mod._kill

    def spy(proc) -> None:
        kills.append(proc)
        real_kill(proc)

    monkeypatch.setattr(grep_mod, "_kill", spy)
    out = await grep_mod.handler({"pattern": "needle", "limit": 7}, ToolCtx(cwd=str(tmp_path)))
    assert not out.is_error
    lines, notes = _body(out.text)
    assert len(lines) == 7
    assert notes == [
        "[search truncated: match cap 7 reached — pass a higher limit or narrow the pattern]"
    ]
    assert kills, "rg should have been stopped once the cap was exceeded"


async def test_grep_rg_early_exit_does_not_wait_for_eof(tmp_path, monkeypatch) -> None:
    """The fake rg never closes its output: only stopping at the cap returns."""
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(grep_mod, "RG_TIMEOUT_S", 30.0)
    procs: list[FakeRgProc] = []

    async def fake_exec(*args, **kwargs):
        stdout = b"".join(f"./f{i}.py:1:needle\n".encode() for i in range(5))
        procs.append(FakeRgProc(stdout=stdout, hang=True))
        return procs[-1]

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    out = await asyncio.wait_for(
        grep_mod.handler({"pattern": "needle", "limit": 3}, ToolCtx(cwd=str(tmp_path))), 5
    )
    assert procs[0].killed
    lines, notes = _body(out.text)
    assert len(lines) == 3 and "match cap 3 reached" in notes[0]


async def test_grep_rg_exactly_at_the_limit_has_no_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    procs: list[FakeRgProc] = []

    async def fake_exec(*args, **kwargs):
        stdout = b"".join(f"./f{i}.py:1:needle\n".encode() for i in range(3))
        procs.append(FakeRgProc(stdout=stdout))
        return procs[-1]

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    out = await grep_mod.handler({"pattern": "needle", "limit": 3}, ToolCtx(cwd=str(tmp_path)))
    lines, notes = _body(out.text)
    assert len(lines) == 3 and notes == [] and not procs[0].killed


async def test_grep_rg_reassembles_lines_split_across_reads(tmp_path, monkeypatch) -> None:
    """A long match line spans many 64KB reads and has no trailing newline."""
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    long_line = "./min.js:1:" + "needle," * 30_000

    async def fake_exec(*args, **kwargs):
        return FakeRgProc(stdout=f"./a.py:2:needle\n{long_line}".encode())

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    out = await grep_mod.handler({"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))
    assert f"{tmp_path}{os.sep}a.py:2:needle" in out.text
    assert "min.js:1:needle,needle" in out.text
    assert "chars truncated" in out.text  # the 30KB output bound still applies


async def test_grep_rg_cancel_kills_the_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    procs: list[FakeRgProc] = []

    async def fake_exec(*args, **kwargs):
        procs.append(FakeRgProc(hang=True))
        return procs[-1]

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.ensure_future(
        grep_mod.handler({"pattern": "needle"}, ToolCtx(cwd=str(tmp_path)))
    )
    while not procs:
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs[0].killed


async def test_grep_rg_args_add_prune_globs_only_with_no_ignore(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(grep_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return FakeRgProc(returncode=1)

    monkeypatch.setattr(grep_mod.asyncio, "create_subprocess_exec", fake_exec)
    await grep_mod.handler({"pattern": "a"}, ToolCtx(cwd=str(tmp_path)))
    await grep_mod.handler({"pattern": "b", "no_ignore": True}, ToolCtx(cwd=str(tmp_path)))
    assert "!node_modules/" not in calls[0]
    assert "-uu" in calls[1] and "!node_modules/" in calls[1] and "!.git/" in calls[1]
    # The pattern still comes last, after `--`.
    assert calls[1][-3:] == ["--", "b", "."]
