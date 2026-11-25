"""Shared helpers for builtin tools."""

from __future__ import annotations

import contextlib
import difflib
import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from fluid.tools import spill
from fluid.tools.child_env import scrub_child_env
from fluid.tools.registry import ToolCtx

_TRUNCATE_LIMIT = 12_000  # chars; keep head + tail, drop (and spill) the middle

# Terminal control sequences: CSI (colours, cursor moves), OSC (title/hyperlink
# strings, terminated by BEL or ST) and the lone two-byte escapes. Test runners
# and package managers emit these even into a pipe on some platforms, and they
# reach the model — and the UI — as gibberish.
ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from captured process output."""
    return ANSI_RE.sub("", text)

# Directories never worth walking for a content search or a file glob: VCS
# metadata, dependency trees, build output, tool caches. Pruning these in place
# is what keeps a repo-wide walk in the thousands of paths instead of hundreds
# of thousands.
PRUNE_DIRS = frozenset({
    ".git",
    ".agents",
    ".cache",
    ".claude",
    ".idea",
    ".next",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
})

# Per-file read ceiling for the pure-Python search path (2MB).
MAX_FILE_BYTES = 2 * 1024 * 1024

# Bytes sampled from the head of a file to decide "is this binary?".
BINARY_SNIFF_BYTES = 8192


def resolve(file_path: str, ctx: ToolCtx) -> Path:
    """Resolve a path against the session cwd (absolute paths pass through)."""
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(ctx.cwd) / p
    return p.resolve()


def truncate(
    text: str,
    limit: int = _TRUNCATE_LIMIT,
    *,
    ctx: ToolCtx | None = None,
    tool: str = "tool",
    head_share: float = 0.5,
    saved_as: str = "full output",
    note: str = "",
) -> str:
    """Head+tail truncation for large tool output (prevents context blowup).

    With ``ctx`` the text is first spilled to the session's scratch folder
    and the marker names the file (see :func:`fluid.tools.spill.cut`).
    """
    return spill.cut(
        text, limit, ctx=ctx, tool=tool, head_share=head_share, saved_as=saved_as, note=note)


@dataclass
class WalkState:
    """Budget + telemetry for :func:`walk_files`.

    ``deadline`` is a ``time.monotonic()`` stamp; when it passes, the walk stops
    and flags ``deadline_hit`` so callers can label their output as partial.
    """

    deadline: float | None = None
    files_seen: int = 0
    deadline_hit: bool = False

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline


def walk_files(
    root: Path, state: WalkState | None = None, *, hidden: bool = True
) -> Iterator[Path]:
    """Yield files under ``root``, pruning heavy directories and symlinks.

    Uses ``os.walk`` (not ``Path.rglob``) for two reasons: the ``dirnames``
    in-place edit prunes whole subtrees before they are ever listed, and
    ``followlinks=False`` keeps the walk out of symlink/junction cycles —
    ``rglob`` only grew a ``recurse_symlinks`` switch in 3.13. With
    ``hidden=False``, dot-named files and directories below ``root`` are
    skipped, as ripgrep skips them by default.
    """
    state = state or WalkState()
    if root.is_file():
        state.files_seen += 1
        yield root
        return
    dirpath, base = "", root
    for parent, name in _walk(root, state, hidden=hidden):
        if parent is not dirpath:
            dirpath, base = parent, Path(parent)
        yield base / name


def _walk(
    root: Path, state: WalkState, *, hidden: bool = True
) -> Iterator[tuple[str, str]]:
    """``(dirpath, filename)`` for every file the pruned walk reaches."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in PRUNE_DIRS and (hidden or not d.startswith("."))
        ]
        if not hidden:
            filenames = [f for f in filenames if not f.startswith(".")]
        if state.expired:
            state.deadline_hit = True
            return
        for name in sorted(filenames):
            if state.expired:
                state.deadline_hit = True
                return
            state.files_seen += 1
            yield dirpath, name


def list_rel_files(
    root: Path, state: WalkState | None = None, rg: str | None = None
) -> Generator[str, None, None]:
    """The files :func:`walk_files` yields, as root-relative POSIX paths.

    This is what a glob is matched against, so the strings are built directly
    rather than through ``Path.relative_to(...).as_posix()``, which costs more
    than the listing itself on a few thousand files. The root itself, when it
    is a file, comes back as ``.``, as ``relative_to`` would give it.

    With ``rg``, a directory is listed by ``rg --files``: ``-uu`` (no ignore
    files, hidden files included) plus the :data:`PRUNE_DIRS` excludes covers
    the same tree as the walk, an order of magnitude faster. Ripgrep without
    ``--follow`` lists no symlinks at all, so :class:`_LinkProbe` adds the
    symlinked files the walk lists. Neither enters a symlinked directory; the
    one remaining difference is that the walk enters Windows junctions and
    ripgrep does not. Following links is not an option, because a link out of
    the tree (to a home or data directory) would turn every listing into a
    walk of that tree. When ripgrep cannot be started, or exits with an error
    having listed nothing, the walk runs instead; a listing that also met an
    error (an unreadable directory) stands, as the walk skips such a
    directory too. The deadline in ``state`` kills ripgrep and the listing
    stops with ``deadline_hit`` set, exactly like the walk.
    """
    state = state or WalkState()
    if root.is_file():
        state.files_seen += 1
        yield "."
        return
    if rg:
        listing = _rg_listing(rg, root, state)
        if listing is not None:
            yield from listing
            return
    yield from _walk_rel(root, state)


def _walk_rel(root: Path, state: WalkState) -> Iterator[str]:
    top = len(os.fspath(root))
    for dirpath, name in _walk(root, state):
        sub = dirpath[top:].lstrip(os.sep)
        if sub and os.sep != "/":
            sub = sub.replace(os.sep, "/")
        yield f"{sub}/{name}" if sub else name


def rg_prune_globs() -> list[str]:
    """``--glob`` arguments that make ripgrep prune exactly :data:`PRUNE_DIRS`.

    A glob with no slash but a trailing one matches a *directory* of that name
    at any depth below the search root, and only a directory, which is what
    :func:`walk_files` prunes (case-sensitively, like the set lookup there).
    Ripgrep lets the last matching glob win, so these go after any glob of the
    caller's that could match the same directory.
    """
    args: list[str] = []
    for name in sorted(PRUNE_DIRS):
        args += ["--glob", f"!{name}/"]
    return args


def _rg_listing(rg: str, root: Path, state: WalkState) -> Iterator[str] | None:
    """``rg --files`` under ``root`` as a stream, or ``None`` if it cannot start."""
    args = [
        rg, "--files", "--no-config", "-uu", "--null", "--path-separator", "/",
        *rg_prune_globs(),
    ]
    try:
        proc = subprocess.Popen(
            args, cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=scrub_child_env(),
        )
    except OSError:
        return None
    return _rg_stream(proc, root, state, _LinkProbe(root))


def _rg_stream(
    proc: subprocess.Popen[bytes], root: Path, state: WalkState, links: _LinkProbe
) -> Iterator[str]:
    timer = None
    if state.deadline is not None:
        def _expire() -> None:
            proc.kill()
            links.close()

        timer = threading.Timer(max(0.0, state.deadline - time.monotonic()), _expire)
        timer.daemon = True
        timer.start()
    assert proc.stdout is not None
    listed = 0
    try:
        pending = b""
        while chunk := proc.stdout.read(65536):
            *names, pending = (pending + chunk).split(b"\0")
            for raw in names:
                if not raw:
                    continue
                if state.expired:
                    state.deadline_hit = True
                    return
                state.files_seen += 1
                listed += 1
                rel = os.fsdecode(raw)
                yield rel[2:] if rel.startswith("./") else rel
        if state.expired:
            state.deadline_hit = True
            return
        # rg: 0 = files listed, 1 = none, >=2 = an error.
        if proc.wait() >= 2 and listed == 0:
            links.close()
            yield from _walk_rel(root, state)
            return
        yield from links.files(state)
    finally:
        if timer is not None:
            timer.cancel()
        links.close()
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()


# ── symlinked files ──────────────────────────────────────────────────────────
#
# ``rg --files`` without ``--follow`` lists no symlinks at all, and following
# them is not an option (one link out of the tree would make every Glob walk
# that tree). The walk lists a symlink as a file whenever it does not resolve
# to a directory, so a symlinked CLAUDE.md or a dangling link shows up there.
# These helpers find exactly those entries, without entering linked
# directories: in a git work tree from the index and the untracked listing
# (git does not follow links either), anywhere else from a scandir pass that
# looks only at entry types.

_GIT_CMDS = (
    # Tracked entries with their modes; 120000 is a symlink.
    ("ls-files", "--stage", "-z", "--recurse-submodules"),
    # Untracked and ignored entries. --directory folds a wholly untracked
    # directory (an ignored build tree, a nested repository) into one
    # "dir/" line, which is scanned below unless it is pruned.
    ("ls-files", "--others", "--directory", "-z"),
)


class _LinkProbe:
    """Finds the symlinked files under ``root`` that ``rg --files`` leaves out.

    The git listings start when the probe is made, so they run while rg does.
    Anything that keeps git from answering (not a work tree, no git, a
    "dubious ownership" refusal) falls back to the scandir pass, which is
    exact but walks every directory.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.procs: list[subprocess.Popen[bytes]] = []
        git = shutil.which("git")
        if git is None:
            return
        try:
            for cmd in _GIT_CMDS:
                self.procs.append(subprocess.Popen(
                    # A repository's fsmonitor setting names a program to run;
                    # listing files is no reason to run it.
                    [git, "-c", "core.fsmonitor=false", *cmd],
                    cwd=str(root), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=scrub_child_env(),
                ))
        except OSError:
            self.close()
            self.procs = []

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()

    def _git_output(self) -> list[bytes] | None:
        if len(self.procs) != len(_GIT_CMDS):
            return None
        outputs = []
        for proc in self.procs:
            out, _ = proc.communicate()
            if proc.returncode != 0:
                return None
            outputs.append(out)
        return outputs

    def files(self, state: WalkState) -> Iterator[str]:
        outputs = self._git_output()
        if outputs is None:
            yield from _scan_links(self.root, "", state)
            return
        staged, others = outputs
        seen: set[str] = set()
        candidates: list[str] = []
        for entry in staged.split(b"\0"):
            meta, sep, name = entry.partition(b"\t")
            if sep and meta.startswith(b"120000 "):
                candidates.append(os.fsdecode(name))
        dirs: list[str] = []
        for entry in others.split(b"\0"):
            if not entry:
                continue
            rel = os.fsdecode(entry)
            if rel.endswith("/") or rel in ("", "."):
                folded = rel.rstrip("/")
                dirs.append("" if folded in ("", ".") else folded.removeprefix("./"))
            else:
                candidates.append(rel)
        for rel in candidates:
            if rel in seen or _pruned(rel.split("/")[:-1]):
                continue
            seen.add(rel)
            if _is_listed_link(os.path.join(self.root, rel)):
                if state.expired:
                    state.deadline_hit = True
                    return
                state.files_seen += 1
                yield rel
        for rel_dir in dirs:
            if rel_dir and _pruned(rel_dir.split("/")):
                continue
            for rel in _scan_links(self.root, rel_dir, state):
                if rel not in seen:
                    seen.add(rel)
                    yield rel
            if state.deadline_hit:
                return


def _pruned(dir_parts: list[str]) -> bool:
    return any(part in PRUNE_DIRS for part in dir_parts)


def _is_listed_link(path: str) -> bool:
    """A symlink the walk would list as a file: one not resolving to a directory."""
    try:
        if not stat.S_ISLNK(os.lstat(path).st_mode):
            return False
    except OSError:
        return False
    return not os.path.isdir(path)


def _scan_links(root: Path, start: str, state: WalkState) -> Iterator[str]:
    """Symlinked files under ``root/start``, found by entry type alone.

    Prunes like the walk and never enters a symlinked directory or a
    junction. Counts what it yields in ``state.files_seen``.
    """
    stack = [start]
    while stack:
        if state.expired:
            state.deadline_hit = True
            return
        rel_dir = stack.pop()
        try:
            entries = os.scandir(os.path.join(root, rel_dir) if rel_dir else root)
        except OSError:
            continue
        with entries:
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    if entry.is_symlink():
                        if not entry.is_dir():
                            state.files_seen += 1
                            yield rel
                    elif (
                        entry.is_dir(follow_symlinks=False)
                        and not entry.is_junction()
                        and entry.name not in PRUNE_DIRS
                    ):
                        stack.append(rel)
                except OSError:
                    continue


def is_binary(path: Path) -> bool:
    """True when the head of ``path`` contains a NUL byte (or cannot be read)."""
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(BINARY_SNIFF_BYTES)
    except OSError:
        return True


class GlobSyntaxError(ValueError):
    """A glob construct the pure-Python glob compiler cannot honour.

    Raised rather than quietly compiling to something that matches nothing:
    a filter that drops every file turns a real hit into a clean "(no
    matches)", which reads as a fact about the tree and is acted on as one.
    The caller's job is to surface it as a hard error naming the construct.
    """


def _close_brace(pat: str, start: int) -> int:
    """Index of the ``}`` closing the group opened at ``start`` (nesting aware)."""
    depth = 0
    for i in range(start, len(pat)):
        if pat[i] == "{":
            depth += 1
        elif pat[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise GlobSyntaxError("unbalanced '{' — the brace group is never closed")


def _split_alternatives(body: str) -> list[str]:
    """Split a brace body on its top-level commas; nested groups stay whole."""
    alts: list[str] = []
    depth = start = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            alts.append(body[start:i])
            start = i + 1
    alts.append(body[start:])
    return alts


def _glob_expr(pat: str) -> str:
    """Translate a glob body (no leading ``!``) into a regex fragment."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        ch = pat[i]
        if ch == "*":
            if pat[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            close = pat.find("]", i + 1)
            if close == -1:
                raise GlobSyntaxError("unterminated '[' — the character class is never closed")
            body = pat[i + 1:close]
            if body.startswith(("!", "^")):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = close + 1
            continue
        if ch == "{":
            close = _close_brace(pat, i)
            # Each alternative is a glob in its own right, so a separator or a
            # nested group inside the braces means what it means outside them.
            alts = _split_alternatives(pat[i + 1:close])
            out.append("(?:" + "|".join(_glob_expr(alt) for alt in alts) + ")")
            i = close + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return "".join(out)


def glob_to_regex(pattern: str, *, case_sensitive: bool | None = None) -> re.Pattern[str]:
    """Compile a glob into a regex matched against a POSIX relative path.

    Supports ``**`` (any depth), ``*``/``?`` (within one segment), ``[...]``
    classes and ``{a,b}`` alternation — the last because ripgrep accepts it, so
    a ``*.{py,ts}`` that worked through Grep's rg branch must not silently match
    nothing through the Python one. Alternatives may contain ``/`` and may nest,
    for the same reason. A pattern with no ``/`` matches the file *name* at any
    depth, so a bare ``*.py`` behaves the way models tend to expect.

    Raises :class:`GlobSyntaxError` for anything it cannot translate faithfully,
    including a leading ``!`` — that is ripgrep's exclusion marker, not a
    literal, so it has to go through :func:`compile_glob`.
    """
    pat = pattern.replace("\\", "/")
    if pat.startswith("!"):
        raise GlobSyntaxError(
            "a leading '!' is ripgrep's exclusion marker, not a literal; "
            "compile it with compile_glob()"
        )
    expr = _glob_expr(pat)
    if "/" not in pat:
        expr = "(?:.*/)?" + expr
    if case_sensitive is None:
        case_sensitive = os.name != "nt"
    try:
        return re.compile(expr + r"\Z", 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:  # pragma: no cover - only reachable via odd classes
        raise GlobSyntaxError(f"glob does not compile: {exc}") from exc


@dataclass(frozen=True)
class GlobFilter:
    """A compiled ``glob`` value carrying ripgrep's leading-``!`` semantics.

    ``rg --glob '!x'`` is an *exclusion*: every file is searched except the ones
    matching ``x``. Translating that as an include filter — which is what a
    plain glob→regex pass does once the ``!`` has been escaped — matches nothing
    at all, and the search comes back as an honest-looking "(no matches)".
    """

    regex: re.Pattern[str]
    negated: bool

    def matches(self, rel_path: str) -> bool:
        """Does a root-relative POSIX path pass this filter?"""
        hit = self.regex.match(rel_path) is not None
        return not hit if self.negated else hit


def compile_glob(pattern: str, *, case_sensitive: bool | None = None) -> GlobFilter:
    """Compile one ripgrep-style ``--glob`` value into a path filter."""
    negated = pattern.startswith("!")
    body = pattern[1:] if negated else pattern
    if not body:
        raise GlobSyntaxError("'!' with nothing to exclude")
    return GlobFilter(
        regex=glob_to_regex(body, case_sensitive=case_sensitive), negated=negated
    )


def detect_newline(text: str) -> str:
    """The text's dominant line ending: CRLF when it has more CRLF than bare LF."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def read_raw_text(path: Path) -> str:
    """The file's UTF-8 text exactly as stored: no newline translation."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


# Bytes sampled from an existing file to pick a whole-file Write's line ending.
NEWLINE_SNIFF_BYTES = 64 * 1024


def sniff_newline(path: Path) -> str:
    """The dominant line ending of the head of an existing file.

    Whole-file Write only needs the style, so it reads a bounded sample rather
    than the whole file (which may be huge, or not UTF-8 at all).
    """
    with path.open("rb") as fh:
        head = fh.read(NEWLINE_SNIFF_BYTES)
    crlf = head.count(b"\r\n")
    return "\r\n" if crlf > head.count(b"\n") - crlf else "\n"


def write_text_with_newline(path: Path, text: str, newline: str) -> None:
    """Write LF-normalized ``text`` using ``newline`` for every line break, with
    no platform translation (so LF files stay LF on Windows)."""
    normalized = text.replace("\r\n", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    write_text_atomic(path, normalized)


# Sibling temp files are ``.fluid-XXXXXXXX.tmp`` whatever the target is
# called, so a basename near the filesystem's 255-byte limit still fits.
_TEMP_PREFIX = ".fluid-"
# Errors that mean "no atomic replace here", not "the save cannot succeed":
# only these fall back to writing in place. Creating the temp file: a
# directory the user cannot create files in. Replacing: on Windows a file
# another process holds open without delete sharing (PermissionError), and a
# single-file bind mount (EBUSY). Anything else (a full disk, a quota, a
# read-only filesystem, a name too long) is raised with the original
# untouched. The temp sits in the target's own directory, so a rename across
# filesystems (EXDEV) is not expected and is raised too.
_TEMP_FALLBACK_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
_REPLACE_FALLBACK_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})
_POSIX_ACL_XATTR = "system.posix_acl_access"
_SELINUX_XATTR = "security.selinux"
# posix_fallocate errors meaning "this filesystem cannot preallocate". EBADF
# too: the descriptor is a validated write-only one, and glibc's emulation for
# filesystems without fallocate (NFSv3) reads a byte, which it may not.
_NO_FALLOCATE_ERRNOS = frozenset({
    errno.EOPNOTSUPP,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EINVAL,
    errno.ENOSYS,
    errno.EBADF,
})


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8, exactly as given, without losing the old file.

    The text is encoded before anything on disk is touched, so text that
    cannot be encoded (a lone surrogate) raises ``UnicodeEncodeError`` with the
    file unchanged. A symlink is written through: its target gets the bytes.
    A new file is created exclusively and removed again if the write fails.
    An existing regular file is replaced by a fully written, fsynced sibling
    temp file carrying its permission bits (and, where allowed, its owner), so
    a full disk or a crash leaves the old contents rather than an empty or
    partial file. A file the user may not open for writing stays refused, as
    before: that is checked with a real (non-truncating) open, since on
    Windows ``os.access`` ignores NTFS ACLs.

    Written in place, without truncating first (see ``_write_in_place``),
    are: a file with other hard links, so every name keeps seeing the same
    contents; a file carrying a POSIX ACL, which a new inode would not inherit
    faithfully; a file whose temp could not be given the same ACL state or
    SELinux label; and a file where the replace is impossible (see
    ``_TEMP_FALLBACK_ERRNOS`` and ``_REPLACE_FALLBACK_ERRNOS``). Anything that
    is not a regular file (a device, a FIFO) is simply written to.
    """
    data = text.encode("utf-8")
    target = Path(os.path.realpath(path))
    try:
        fh = target.open("xb")
    except FileExistsError:
        pass
    else:
        try:
            with fh:
                fh.write(data)
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink()
            raise
        return
    st = target.stat()
    if not stat.S_ISREG(st.st_mode):
        with target.open("wb") as out:
            out.write(data)
        return
    # Raises what the old truncating open raised for a file the user may not
    # write, without truncating or touching its contents.
    os.close(os.open(target, os.O_WRONLY))
    if st.st_nlink > 1 or _has_posix_acl(target):
        _write_in_place(target, data)
        return
    try:
        fd, tmp = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".tmp", dir=target.parent)
    except OSError as exc:
        if exc.errno not in _TEMP_FALLBACK_ERRNOS:
            raise
        _write_in_place(target, data)
        return
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        matched = _match_security_xattrs(tmp, target)
        if matched:
            if hasattr(os, "chown"):
                # Before chmod: a chown by a non-root user clears setuid/setgid.
                # Keeping another user's ownership needs privileges, so a
                # refusal just leaves the file owned by the writer.
                with contextlib.suppress(OSError):
                    os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if not matched:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        _write_in_place(target, data)
        return
    try:
        os.replace(tmp, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        if exc.errno not in _REPLACE_FALLBACK_ERRNOS:
            raise
        _write_in_place(target, data)


def _has_posix_acl(path: Path | str) -> bool:
    """Whether ``path`` carries a POSIX access ACL (Linux xattr).

    Platforms without ``os.listxattr`` and filesystems without xattrs count
    as no ACL. macOS and NTFS ACLs are not xattrs and survive differently;
    they are not detected here.
    """
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        return False
    try:
        return _POSIX_ACL_XATTR in listxattr(path)
    except OSError:
        return False


def _get_xattr(path: Path | str, name: str) -> bytes | None:
    """``name``'s value on ``path``; None when absent or xattrs are unsupported."""
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        return getxattr(path, name)
    except OSError:
        return None


def _match_security_xattrs(tmp: str, target: Path) -> bool:
    """Give the temp the target's ACL state and SELinux label before it replaces it.

    Called only when the target has no POSIX access ACL. The temp may still
    have inherited one from the directory's default ACL; masked by mkstemp's
    0600 it grants nothing, but the chmod to the target's mode would enable
    it, so it is removed first. A differing SELinux label is copied from the
    target. False means the temp could not be made to match: the caller then
    writes the target in place instead of replacing it with the wrong access.
    """
    if _has_posix_acl(tmp):
        try:
            os.removexattr(tmp, _POSIX_ACL_XATTR)
        except OSError:
            return False
    label = _get_xattr(target, _SELINUX_XATTR)
    if label is not None and _get_xattr(tmp, _SELINUX_XATTR) != label:
        try:
            os.setxattr(tmp, _SELINUX_XATTR, label)
        except OSError:
            return False
    return True


def _write_at(fd: int, data: bytes | memoryview, offset: int) -> None:
    """Write all of ``data`` at ``offset`` (no ``os.pwrite`` on Windows)."""
    view = memoryview(data)
    os.lseek(fd, offset, os.SEEK_SET)
    while view:
        view = view[os.write(fd, view):]


def _restore_length(fd: int, old_len: int) -> None:
    """Cut away anything a failed save appended past the old end of file."""
    with contextlib.suppress(OSError):
        if os.fstat(fd).st_size != old_len:
            os.ftruncate(fd, old_len)


def _write_in_place(target: Path, data: bytes) -> None:
    """Overwrite a regular file's contents with ``data`` on the same inode.

    For files that must not be replaced (see ``write_text_atomic``). The file
    is never truncated first: the space the new contents need is reserved
    before any existing byte is overwritten, so a full disk or quota raises
    with the original intact. ``posix_fallocate(fd, 0, len(data))`` reserves
    it where it works; elsewhere (Windows, macOS, filesystems that cannot
    preallocate) the part of ``data`` past the old end is written first and
    fsynced, so space that only runs out at writeback (an async NFS mount)
    fails there too. If the reservation fails, or the first write over the
    old bytes is refused outright (a byte-range lock on Windows), any growth
    is truncated away and the error raised with the original intact. Then
    ``data`` is written from offset 0, fsynced, and the file cut to its new
    length.

    Remaining risk: an I/O error part-way through the overwrite, or a
    copy-on-write filesystem (btrfs, ZFS) that needs new blocks to overwrite
    reserved ones and runs out, can still leave a mix of new and old bytes.
    """
    fd = os.open(target, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        old_len = os.fstat(fd).st_size
        new_len = len(data)
        head_len = new_len  # bytes still to write from offset 0
        try:
            reserved = False
            fallocate = getattr(os, "posix_fallocate", None)
            # Shrinking reserves only what the new contents use: reserving
            # the old length too could hit a quota on a sparse file although
            # the save needs no new blocks.
            if fallocate is not None and new_len > 0:
                try:
                    fallocate(fd, 0, new_len)
                    reserved = True
                except OSError as exc:
                    if exc.errno not in _NO_FALLOCATE_ERRNOS:
                        raise
            if not reserved and new_len > old_len:
                _write_at(fd, memoryview(data)[old_len:], old_len)
                os.fsync(fd)
                head_len = old_len
        except BaseException:
            _restore_length(fd, old_len)
            raise
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _write_at(fd, memoryview(data)[:head_len], 0)
        except BaseException:
            # Refused before a single old byte changed: drop the growth so
            # the file is exactly the original again.
            with contextlib.suppress(OSError):
                if os.lseek(fd, 0, os.SEEK_CUR) == 0:
                    _restore_length(fd, old_len)
            raise
        os.fsync(fd)
        if new_len < old_len:
            os.ftruncate(fd, new_len)
            os.fsync(fd)
    finally:
        os.close(fd)


# ── missing-path diagnostics ─────────────────────────────────────────────────
#
# A bad path is one of the most common tool failures and the most wasteful: the
# bare "file not found: X" costs a Glob or a Grep to recover from. Two shapes
# dominate, and both are recognisable from the string itself — a `path:line:`
# match line pasted straight out of Grep output, and a near-miss spelling in a
# directory that does exist. Naming the fix inline turns a two-call recovery
# into a zero-call one.

_GREP_MATCH_SUFFIX = re.compile(r"^(?P<path>.+?):\d+:")


def _grep_line_note(raw: str) -> str | None:
    """Recognise a Grep result line handed back as if it were a path."""
    match = _GREP_MATCH_SUFFIX.match(raw)
    if match is None:
        return None
    candidate = match.group("path")
    if not candidate or not Path(candidate).exists():
        return None
    return (
        f'this looks like a Grep result line ("path:line:content"); the path '
        f'part is "{candidate}" — pass just that'
    )


def _nearby_note(path: Path) -> str | None:
    """Closest existing names in the parent directory, when there are any."""
    parent = path.parent
    try:
        if not parent.is_dir():
            return f"its parent directory {parent} does not exist either"
        names = [entry.name for entry in islice(parent.iterdir(), 2000)]
    except OSError:
        return None
    close = difflib.get_close_matches(path.name, names, n=3, cutoff=0.6)
    if not close:
        return None
    return "did you mean: " + ", ".join(str(parent / name) for name in close)


def missing_path_note(raw: str, path: Path) -> str:
    """The trailing clause for a not-found diagnostic ("" when nothing helps)."""
    for note in (_grep_line_note(raw), _nearby_note(path)):
        if note:
            return f" — {note}"
    return ""


# ── repeat-work notes ────────────────────────────────────────────────────────
#
# Re-reading a file nothing has touched, and re-running a search already run,
# are the two cheapest calls to make and the two most common ones to waste a
# turn on.


def unchanged_read_note(repeat: bool) -> str:
    """The trailing note for a Read that returns already-seen content.

    The content still comes back in full — the note only says it is already in
    the conversation. Callers must only pass ``repeat=True`` when the earlier
    payload really did reach the model (see ``ToolCtx.read_delivered``).
    """
    if not repeat:
        return ""
    return (
        "\n\n[note: unchanged since your earlier read this session — reuse "
        "what you already learned instead of re-reading]"
    )


def result_digest(text: str, *, unordered: bool = False) -> str:
    """A short hash of a search result, for telling a true repeat from a changed one.

    ``unordered`` hashes the sorted lines, for results whose line order is not
    stable between identical runs (ripgrep walks directories in parallel).
    """
    if unordered:
        text = "\n".join(sorted(text.split("\n")))
    return hashlib.blake2b(
        text.encode("utf-8", "replace"), digest_size=16
    ).hexdigest()


def repeat_search_pointer(text: str) -> str:
    """The result an identical, already-run search gets *instead of* its rows.

    Appending "you already ran this" underneath a second full copy of the rows
    charged for the duplicate and then asked the model not to want it. The
    payload is verbatim-identical to one already in the conversation, so this
    replaces it: the search's own header (so the earlier result is easy to
    find), the size of what is not being repeated, and where it is.
    """
    lines = text.splitlines()
    head = lines[0].strip() if lines else ""
    withheld = max(0, len(lines) - 1)
    plural = "line" if withheld == 1 else "lines"
    return (
        (f"{head}\n" if head else "")
        + f"[identical search already run this session — the same {withheld} "
        f"{plural} of results are above in this conversation and are not "
        "repeated here. Re-read them there; change the pattern, path, or "
        "scope if you need something new.]"
    )
