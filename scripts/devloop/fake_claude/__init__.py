"""A fake ``claude`` CLI for the dev loop, so a dev instance can run Claude Code chats.

The app runs a Claude Code chat through the delegated ``claude`` CLI it finds on
PATH (``harness_adapter._resolve_claude_cmd``), and it checks the sign-in with
``claude auth status``. :func:`install` writes a ``claude`` shim into the dev
instance's own throwaway directory; :func:`instance_path` puts that directory
first on the PATH the instance's app gets and drops every other directory that
holds a ``claude``, so the real CLI can never be found there, even if the shim
went missing. The shim runs :mod:`scripts.devloop.fake_claude.cli`, which
answers ``--version`` and ``auth status`` (signed in as an obviously fake
account) and plays stream-json turns picked by ``[script:NAME]`` in the
prompt. It holds no credential and makes no network calls; the only socket it
opens is the app's own loopback bridge (the Claude Code mod's compaction route).
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
SCENARIOS_DIR = PACKAGE / "scenarios"
#: How Claude Code prints each kind of compaction (the replay fixtures).
COMPACTION_DIR = PACKAGE / "compaction"
CLI = PACKAGE / "cli.py"
#: The shim's second line. The seed checks it before it creates the Claude
#: Code agent, so that agent is only ever seeded beside the fake.
MARKER = "# ai-shuffle devloop fake claude CLI: never the real one, no network, no credential"
#: The account ``claude auth status`` reports. ``.invalid`` is reserved
#: (RFC 2606): no real address or organization can have it.
FAKE_EMAIL = "fake-claude@devloop.invalid"
CLI_NAMES = ("claude", "claude.exe", "claude.cmd", "claude.bat")


def install(directory: Path, *, python: str | None = None) -> Path:
    """Write the ``claude`` shim under ``directory`` and return its bin dir.

    ``directory`` is the dev instance's scratch directory: the fake keeps its
    transcripts only under it (``FAKE_CLAUDE_ROOT``) and logs every run to
    ``fake-claude.jsonl`` there. Deleted with the instance.
    """
    directory = Path(directory).resolve()
    bin_dir = directory / "fake-claude-bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    shim = bin_dir / "claude"
    lines = [
        "#!/bin/sh",
        MARKER,
        f"FAKE_CLAUDE_ROOT={shlex.quote(str(directory))}",
        f"FAKE_CLAUDE_LOG={shlex.quote(str(log_path(directory)))}",
        f"FAKE_CLAUDE_STARTUP_MS={shlex.quote(os.environ.get('DEVLOOP_CLAUDE_STARTUP_MS', '0'))}",
        "export FAKE_CLAUDE_ROOT FAKE_CLAUDE_LOG FAKE_CLAUDE_STARTUP_MS",
        # -I: no PYTHON* variables, no user site, not the caller's directory.
        f"exec {shlex.quote(python or sys.executable)} -I {shlex.quote(str(CLI))} \"$@\"",
    ]
    shim.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shim.chmod(stat.S_IRWXU)
    return bin_dir


def cover_home(home: Path, bin_dir: Path) -> Path | None:
    """Link ``home/.local/bin/claude`` to the shim and return it.

    App startup puts ``~/.local/bin`` first on PATH, and that is where the
    app's Claude Code installer writes. With the fake there too, the app
    reports Claude Code as installed and nothing in the instance's own HOME
    can come before the fake. Only a HOME inside the instance's scratch
    directory (``bin_dir``'s parent) is touched; None otherwise.
    """
    home, scratch = Path(home).resolve(), Path(bin_dir).resolve().parent
    if not home.is_relative_to(scratch):
        return None
    link = home / ".local" / "bin" / "claude"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(link):
        link.symlink_to(Path(bin_dir).resolve() / "claude")
    return link


def log_path(directory: Path) -> Path:
    return Path(directory) / "fake-claude.jsonl"


def is_fake(path: str | os.PathLike) -> bool:
    """Whether ``path`` is a shim :func:`install` wrote."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(512).decode("utf-8", "replace")
    except OSError:
        return False
    return head.splitlines()[1:2] == [MARKER]


def _holds_cli(entry: str) -> bool:
    return any(os.path.lexists(os.path.join(entry, name)) for name in CLI_NAMES)


def _mirror(entry: str, target: Path) -> Path:
    """``target``: links to everything in ``entry`` except a ``claude``."""
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        names = os.listdir(entry)
    except OSError:
        names = []
    for name in names:
        if name.lower() in CLI_NAMES or name.lower().startswith("claude."):
            continue
        link = target / name
        if not os.path.lexists(link):
            link.symlink_to(os.path.join(os.path.abspath(entry), name))
    return target


def instance_path(path: str, bin_dir: Path) -> str:
    """``path`` with ``bin_dir`` first and no other directory holding a ``claude``.

    A directory that holds one (``~/.local/bin`` for the native installer, an
    npm prefix that also holds ``node``) is replaced by a mirror of its other
    entries under ``bin_dir``'s parent, so the instance keeps every other
    program and a lost shim makes the app report "Claude Code is not
    installed" instead of reaching a real CLI.
    """
    mirrors = Path(bin_dir).parent / "fake-claude-path"
    entries = []
    for index, entry in enumerate(path.split(os.pathsep)):
        if not entry or os.path.abspath(entry) == os.path.abspath(bin_dir):
            continue
        entries.append(str(_mirror(entry, mirrors / str(index))) if _holds_cli(entry) else entry)
    return os.pathsep.join([str(bin_dir), *entries])
