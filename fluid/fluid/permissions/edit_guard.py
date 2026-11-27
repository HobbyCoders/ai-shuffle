"""Which file edits the Accept Edits mode may approve on its own.

Accept Edits means "edit the project's files without asking me". It used to
be decided on the tool's name alone, so a write to ``~/.bashrc``, to
``.git/hooks/pre-commit`` or to the session's own permission settings ran
with no prompt, and each of those runs a program or lifts a prompt later.

:func:`edit_stays_in_project` is the one answer every caller uses (fluid's
own rules, and a host deciding a prompt for the engine). It says yes only
when the file the tool will open

- is inside the session's working folder, or a folder the host added for the
  session (uploads, notes, the harness scratch folder), with links followed;
- is not under a folder that configures a tool or starts programs (``.git``,
  ``.fluid``, ``.claude``, ``.vscode``, ...), is not a shell startup or tool
  settings file (``.bashrc``, ``.gitconfig``, ``.mcp.json``, ...), and is not
  a credential file or under a protected install path;
- is not a second name for a file that also lives somewhere else (a hard
  link).

Everything else is not denied, it asks, exactly as the default mode does.

The answer is the same on Linux, macOS and Windows. Names are compared the
way the most forgiving filesystem reads them: letter case is ignored,
trailing dots and spaces and an NTFS stream suffix are dropped, characters
HFS+ ignores are removed, and an 8.3 short name (``GIT~1``) asks because it
cannot be read back to the long one from here. A name that only looks
protected on one platform asks on all of them.

This is a preflight on the path, not a broker: the tool opens the file
separately afterwards.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from fluid.permissions.rules import _openable, _opened_path

# The input fields the edit-class tools name their file in (Edit, Write,
# MultiEdit: ``file_path``; NotebookEdit: ``notebook_path``).
EDIT_PATH_FIELDS: tuple[str, ...] = ("file_path", "notebook_path")

# A request that names several files (a delegated agent reporting one edit
# over more than one target) lists them all here; each is judged.
EDIT_PATH_LIST_FIELD = "file_paths"

# Folders whose contents configure a tool or run later without a prompt:
# version control (hooks, config), agent and editor settings (permission
# rules, hooks, MCP servers, tasks run on open), and a home folder's key and
# settings stores. Compared after :func:`_plain_name`.
PROTECTED_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    ".fluid", ".claude", ".codex", ".gemini", ".cursor", ".agents",
    ".vscode", ".idea", ".devcontainer", ".husky",
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config", ".local",
})

# Inside these, ``worktrees/<name>/`` is a checkout of the project, judged by
# the rest of its path like any other project file.
_WORKTREE_PARENTS: frozenset[str] = frozenset({".fluid", ".claude"})

# Files a shell, git or an agent reads on start. Globs over the plain name.
PROTECTED_FILE_GLOBS: tuple[str, ...] = (
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".bash_aliases",
    ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout",
    ".profile", ".kshrc", ".cshrc", ".tcshrc", ".login", "config.fish",
    "*profile.ps1",
    ".envrc", ".gitconfig", ".gitmodules", ".gitattributes",
    # Harness configuration fluid loads from a project's top folder.
    "fluid.toml",
    ".mcp.json", ".claude.json", ".ripgreprc", ".curlrc", ".wgetrc",
)

# Code points HFS+ drops when it compares names (git's own list for ``.git``).
_IGNORED_BY_HFS = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")
_SHORT_NAME = re.compile(r"~\d")


def _plain_name(component: str) -> str:
    """``component`` as the most forgiving filesystem reads it."""
    name = unicodedata.normalize("NFC", _IGNORED_BY_HFS.sub("", component))
    name = name.split(":", 1)[0].rstrip(". ")
    return name.casefold()


def _inside(path: str, root: str) -> list[str] | None:
    """The components of ``path`` below ``root``, or None when it is not
    strictly inside."""
    try:
        if os.path.commonpath([os.path.normcase(root), os.path.normcase(path)]) != (
            os.path.normcase(root)
        ):
            return None
        relative = os.path.relpath(path, root)
    except ValueError:  # Another drive, or a mix of absolute and relative.
        return None
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if not parts or parts[0] in (".", ".."):
        return None
    return parts


def _real(folder: Any) -> str | None:
    """A folder's real path, or None when it cannot be one to work in."""
    if isinstance(folder, os.PathLike):  # hosts keep some folders as ``Path``
        folder = os.fspath(folder)
    if not isinstance(folder, str) or not folder.strip():
        return None
    try:
        real = os.path.realpath(os.path.expanduser(folder))
    except (OSError, ValueError):
        return None
    # A filesystem root is not a project: everything would be inside it.
    return None if os.path.dirname(real) == real else real


def _home() -> str | None:
    try:
        home = os.path.expanduser("~")
    except (OSError, RuntimeError):
        return None
    return _real(home) if home and home != "~" else None


def _dirs_allow(plain: list[str]) -> bool:
    """False when one of the folder names is protected. ``<.fluid|.claude>/
    worktrees/<name>/`` is a checkout of the project, so those three names
    are stepped over."""
    index = 0
    while index < len(plain):
        name = plain[index]
        if name in PROTECTED_DIR_NAMES:
            is_worktree = (
                name in _WORKTREE_PARENTS
                and index + 3 < len(plain)
                and plain[index + 1] == "worktrees"
            )
            if not is_worktree:
                return False
            index += 3  # past ``<parent>/worktrees/<name>``
            continue
        index += 1
    return True


def _names_allow(parts: list[str], *, root_is_home: bool) -> bool:
    """False when a component below the root is protected."""
    plain = [_plain_name(part) for part in parts]
    if any(not name or _SHORT_NAME.search(name) for name in plain):
        return False
    # A home folder keeps every tool's settings and programs in dot folders
    # at its top; no list of names keeps up with that.
    if root_is_home and plain[0].startswith("."):
        return False
    if not _dirs_allow(plain):
        return False
    return not any(fnmatchcase(plain[-1], glob) for glob in PROTECTED_FILE_GLOBS)


def _root_allows(root: str) -> bool:
    """False for a working folder that is itself inside a protected folder
    (a session started in ``repo/.vscode`` or ``repo/.git/hooks``): every
    file below it configures a tool. A worktree checkout is project code."""
    parts = [part for part in root.replace("\\", "/").split("/") if part]
    # A trailing ``<parent>/worktrees/<name>`` needs a file below it to be
    # stepped over, so one placeholder stands in for the file.
    return _dirs_allow([_plain_name(part) for part in parts] + ["file"])


def _is_second_name(path: str) -> bool:
    """True for an existing file that has another name somewhere (a hard
    link), which the path cannot place inside the project."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return False
    return os.path.isfile(path) and info.st_nlink > 1


def _spelled_path(value: str, cwd: str) -> str | None:
    """``value`` as an absolute path with no link followed: the names the
    caller wrote, where :func:`_opened_path` gives the file they reach."""
    try:
        path = Path(value)
        if not path.is_absolute():
            path = Path(cwd) / path
        return os.path.abspath(path)
    except (OSError, ValueError):
        return None


def _spelled(folder: Any) -> str | None:
    """A folder as its caller wrote it, absolute, no link followed."""
    if isinstance(folder, os.PathLike):
        folder = os.fspath(folder)
    if not isinstance(folder, str) or not folder.strip():
        return None
    try:
        return os.path.abspath(os.path.expanduser(folder))
    except (OSError, ValueError):
        return None


def _credential_path(path: str) -> bool:
    from fluid.tools.hardening import PROTECTED_WRITE_PREFIXES, is_sensitive_path

    posix = path.replace("\\", "/")
    if is_sensitive_path(posix) or posix.startswith(PROTECTED_WRITE_PREFIXES):
        return True
    return is_sensitive_path("/".join(_plain_name(part) for part in posix.split("/")))


def _path_allowed(
    value: str, cwd: str, roots: Callable[[], Iterable[str]] | Iterable[str] | None
) -> bool:
    opened = _opened_path(value, cwd)
    spelled = _spelled_path(value, cwd)
    if opened is None or spelled is None or not _openable(opened):
        return False
    # Both the file reached and the name written are judged: a link named
    # ``.env`` that points at an ordinary file is still an edit of ``.env``.
    if _credential_path(opened) or _credential_path(spelled):
        return False
    if _is_second_name(opened):
        return False

    home = _home()
    # (real folder, the folder as written, is it the working folder)
    folders: list[tuple[str | None, str | None, bool]] = [(_real(cwd), _spelled(cwd), True)]
    project = folders[0][0]
    if project is None or _inside(opened, project) is None:
        # Only now ask the host for its extra folders: most edits are in the
        # project, and working the extra ones out can cost a lookup.
        folders += [
            (_real(folder), _spelled(folder), False)
            for folder in (roots() if callable(roots) else (roots or ()))
        ]
    for real, written, is_project in folders:
        if real is None:
            continue
        parts = _inside(opened, real)
        if parts is None:
            continue
        # A host adds a folder knowing where it is; the working folder is
        # wherever the session was started. It is judged as it resolves and
        # as it was written: a ``.vscode`` that links to an ordinary folder
        # is still the editor's settings folder.
        if is_project and not all(
            _root_allows(folder) for folder in (real, written) if folder is not None
        ):
            return False
        if not _names_allow(parts, root_is_home=real == home):
            return False
        if spelled == opened:
            return True
        # The spelling took another route (a link on the way). Its names
        # must be allowed too, and it must stay in the same folder.
        for base in (written, real):
            named = _inside(spelled, base) if base is not None else None
            if named is not None:
                return _names_allow(named, root_is_home=real == home)
        return False
    return False


def edit_stays_in_project(
    tool_input: Any,
    cwd: str | None,
    extra_roots: Callable[[], Iterable[str]] | Iterable[str] | None = None,
) -> bool:
    """True when Accept Edits may approve this edit-class call by itself.

    ``tool_input`` is the call's input, ``cwd`` the session's working folder,
    ``extra_roots`` further folders the host lets the session write (or a
    callable returning them, called only for a path outside ``cwd``). False,
    which means "ask", whenever the file cannot be placed: no input, no
    path, no working folder, a path that cannot be reached.
    """
    if isinstance(cwd, os.PathLike):
        cwd = os.fspath(cwd)
    if not isinstance(tool_input, Mapping) or not isinstance(cwd, str) or not cwd.strip():
        return False
    values = [tool_input[key] for key in EDIT_PATH_FIELDS if tool_input.get(key) is not None]
    listed = tool_input.get(EDIT_PATH_LIST_FIELD)
    if listed is not None:
        if not isinstance(listed, list | tuple) or not listed:
            return False
        values += list(listed)
    if not values:
        return False
    for value in values:
        if not isinstance(value, str) or not value or "\x00" in value:
            return False
        # The tool opens the value as given. A caller that trims it first
        # (some delegated CLIs do) must land on an allowed file too.
        spellings = {value, value.strip()}
        if not all(spelling and _path_allowed(spelling, cwd, extra_roots)
                   for spelling in spellings):
            return False
    return True


__all__ = [
    "EDIT_PATH_FIELDS",
    "EDIT_PATH_LIST_FIELD",
    "PROTECTED_DIR_NAMES",
    "PROTECTED_FILE_GLOBS",
    "edit_stays_in_project",
]
