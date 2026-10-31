"""Config folders for extra Claude subscription sign-ins, on disk.

The stock ``claude`` CLI keeps everything in one config folder: the worker's
``$CLAUDE_CONFIG_DIR`` when set (the desktop app sets it), else ``~/.claude``.
That folder is the *default* sign-in and is never moved, linked or deleted
here.

Each extra saved sign-in gets its own folder, ``<data dir>/claude-logins/<id>``,
and every ``claude`` spawn for it gets ``CLAUDE_CONFIG_DIR=<that folder>``. The
CLI writes that sign-in's identity and credentials there itself; AI Shuffle
never opens, copies or stores them. Identity is only ever read by running
``claude auth status`` against the folder.

Shared state is linked back to the default folder, so transcripts, settings and
customizations are shared instead of forked: :data:`SHARED_DIRS` and
:data:`SHARED_FILES`. A missing shared directory is created in the default
folder first (empty), so a write made while another sign-in is active still
lands in the shared place.

- Linux and other POSIX: symlinks for folders and files.
- Windows: directory junctions for folders (no admin rights needed). Files use
  a symlink when Windows allows one (Developer Mode or admin); otherwise the
  file is not linked and :func:`ensure` reports it, so the account keeps its own
  copy of that file instead of a link or hard link that could silently diverge
  when the CLI rewrites it.
- macOS: not offered (:func:`platform_support`), because Claude Code keeps the
  sign-in in the macOS Keychain and per-folder isolation there is unverified.

Removal (:func:`remove`) only accepts a folder directly under the
``claude-logins`` root, removes links and junctions themselves without ever
following them, and refuses the default folder.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT_NAME = "claude-logins"
# Folders shared with the default sign-in. ``projects`` (transcripts) is
# required: a sign-in whose transcripts cannot be shared is not usable.
REQUIRED_DIRS = ("projects",)
SHARED_DIRS = (
    "projects", "agents", "commands", "skills", "plugins", "output-styles", "rules",
    "todos", "plans", "file-history", "session-env",
)
# Files shared when they exist in the default folder (``settings.json`` always:
# an empty one is created so a settings change made on any sign-in is shared).
SHARED_FILES = ("settings.json", "settings.local.json", "CLAUDE.md", "keybindings.json")
ALWAYS_FILES = ("settings.json",)

_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class HomeError(RuntimeError):
    """A sign-in folder could not be prepared or removed safely."""


def platform_support() -> tuple[bool, Optional[str]]:
    """Whether this worker can keep more than one Claude sign-in, and why not."""
    if sys.platform == "darwin":
        return False, ("Saving more than one Claude sign-in is available on Linux and Windows "
                       "workers. On this Mac, Claude Code uses the sign-in from `claude auth login`.")
    return True, None


def default_dir() -> Path:
    """The CLI's own folder: the worker's ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def root() -> Path:
    from app.core.config import settings

    return Path(settings.effective_data_dir) / ROOT_NAME


def valid_id(account_id: object) -> bool:
    return isinstance(account_id, str) and bool(_ID_RE.match(account_id))


def account_dir(account_id: str) -> Path:
    """``<root>/<id>``; refuses anything that is not an app-made id."""
    if not valid_id(account_id):
        raise HomeError("Not a saved Claude sign-in folder.")
    return root() / account_id


def is_link(path: Path) -> bool:
    """A symlink or a Windows junction (never followed by this module)."""
    return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def _same(link: Path, target: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(link)) == os.path.normcase(os.path.realpath(target))
    except OSError:
        return False


def _unlink_link(path: Path) -> None:
    """Remove a link or junction itself; the folder it points at is untouched."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        if sys.platform != "win32":
            raise
        # A Windows junction or directory symlink: rmdir removes the link
        # entry only (it never recurses and fails on a real non-empty folder).
        os.rmdir(path)


def _make_dir_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _link_dir(home: Path, name: str, base: Path) -> bool:
    target = base / name
    link = home / name
    target.mkdir(parents=True, exist_ok=True)
    if is_link(link):
        if _same(link, target):
            return True
        _unlink_link(link)
    elif link.exists():
        try:
            link.rmdir()  # an empty folder the CLI made before linking
        except OSError:
            logger.warning("Claude sign-in folder %s has its own %s; it stays unshared", home, name)
            return False
    _make_dir_link(link, target)
    return True


def _link_file(home: Path, name: str, base: Path) -> bool:
    target = base / name
    link = home / name
    if not target.exists() and not target.is_symlink():
        if name not in ALWAYS_FILES:
            return True  # nothing to share yet; linked once it exists
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "x", encoding="utf-8") as handle:
                handle.write("{}\n")
        except FileExistsError:
            pass
    if is_link(link):
        if _same(link, target):
            return True
        _unlink_link(link)
    elif link.exists():
        logger.warning("Claude sign-in folder %s has its own %s; it stays unshared", home, name)
        return False
    try:
        os.symlink(target, link)
    except OSError:
        if sys.platform != "win32":
            raise
        # Windows without Developer Mode or admin rights cannot make a file
        # symlink. A copy or hard link would silently diverge when the CLI
        # rewrites the file, so the account keeps its own file instead.
        return False
    return True


def ensure(account_id: str, *, create: bool = True) -> dict:
    """Create the folder (if ``create``) and (re)link shared state. Idempotent.

    Returns ``{"exists": bool, "shared": {name: bool}}``. Raises
    :class:`HomeError` when the folder or its required links cannot be made.
    """
    home = account_dir(account_id)
    base = default_dir()
    if _same(home, base):
        raise HomeError("A saved sign-in folder cannot be the Claude Code folder itself.")
    if is_link(home):
        raise HomeError("The saved Claude sign-in folder is a link; remove it and sign in again.")
    if not home.is_dir():
        if not create:
            return {"exists": False, "shared": {}}
        home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(home, stat.S_IRWXU)
        except OSError:
            pass
    shared: dict[str, bool] = {}
    try:
        for name in SHARED_DIRS:
            shared[name] = _link_dir(home, name, base)
        for name in SHARED_FILES:
            shared[name] = _link_file(home, name, base)
    except OSError as exc:
        raise HomeError(f"Could not link the shared Claude Code folders ({type(exc).__name__}).") from None
    if not all(shared.get(name) for name in REQUIRED_DIRS):
        raise HomeError("Transcripts could not be shared with this sign-in folder.")
    return {"exists": True, "shared": shared}


def intact(account_id: str) -> bool:
    """The folder exists and its transcripts link points at the shared folder."""
    try:
        home = account_dir(account_id)
    except HomeError:
        return False
    if not home.is_dir() or is_link(home):
        return False
    base = default_dir()
    return all(is_link(home / name) and _same(home / name, base / name) for name in REQUIRED_DIRS)


def _remove_tree(path: Path) -> None:
    if is_link(path):
        _unlink_link(path)
        return
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        try:
            os.unlink(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(path)
        return
    with os.scandir(path) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        _remove_tree(child)
    os.rmdir(path)


def remove(account_id: str) -> None:
    """Delete an app-made sign-in folder without following any link in it."""
    home = account_dir(account_id)
    parent = root()
    if home.parent != parent or _same(home, default_dir()):
        raise HomeError("Refusing to remove a folder AI Shuffle did not create.")
    if is_link(parent):
        raise HomeError("Refusing to remove through a linked sign-in root.")
    if not os.path.lexists(home):
        return
    _remove_tree(home)


def env_for(account_id: Optional[str]) -> dict[str, str]:
    """The overlay a ``claude`` spawn needs for this sign-in.

    ``None`` is the default sign-in: nothing is set, so the CLI uses the
    worker's own ``$CLAUDE_CONFIG_DIR`` or ``~/.claude`` exactly as before
    (setting it to ``~/.claude`` explicitly would move the CLI's global config
    file, so it is deliberately left alone).
    """
    if account_id is None:
        return {}
    return {"CLAUDE_CONFIG_DIR": str(account_dir(account_id))}
