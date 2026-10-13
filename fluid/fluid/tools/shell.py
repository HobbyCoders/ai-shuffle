"""Which shell runs Bash and Monitor commands.

Models write POSIX shell far more reliably than cmd.exe, and the read-only
classifier in ``bash_risk`` parses POSIX syntax. Claude Code runs its Bash tool
through Git Bash on Windows for the same reason, so fluid does too: when Git
Bash is installed, commands go through ``bash.exe -c``; only when it is missing
do they fall back to ``cmd.exe``. On POSIX the user's ``$SHELL`` is used when it
is a Bourne-style shell, otherwise ``/bin/sh``.

:func:`resolve_shell` is cached for the process; :func:`describe` renders the
``Shell:`` line of the environment block so the prompt always names the shell
that actually runs, with the syntax the model should write for it.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ENV_OVERRIDE = "FLUID_SHELL"

_POSIX_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ash", "ksh"}


@dataclass(frozen=True)
class ShellInfo:
    """How to launch a command string: ``argv_prefix + [command]``."""

    name: str  # "posix" | "git-bash" | "cmd"
    path: str
    argv_prefix: tuple[str, ...]

    @property
    def posix(self) -> bool:
        return self.name != "cmd"


def _is_wsl_bash(candidate: str) -> bool:
    """``System32\\bash.exe`` is the WSL launcher, not a shell for this host."""
    parts = [p.lower() for p in Path(candidate).parts]
    return "system32" in parts


def _windows_candidates() -> list[str]:
    env = os.environ
    found: list[str] = []
    override = env.get(ENV_OVERRIDE)
    if override:
        found.append(override)
    for root_var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = env.get(root_var)
        if root:
            found.append(os.path.join(root, "Git", "bin", "bash.exe"))
    local = env.get("LOCALAPPDATA")
    if local:
        found.append(os.path.join(local, "Programs", "Git", "bin", "bash.exe"))
    on_path = shutil.which("bash.exe") or shutil.which("bash")
    if on_path:
        found.append(on_path)
    return found


def _resolve_windows() -> ShellInfo:
    for candidate in _windows_candidates():
        if _is_wsl_bash(candidate):
            continue
        if os.path.isfile(candidate):
            return ShellInfo(name="git-bash", path=candidate, argv_prefix=(candidate, "-c"))
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return ShellInfo(name="cmd", path=comspec, argv_prefix=(comspec, "/d", "/s", "/c"))


def _resolve_posix() -> ShellInfo:
    user_shell = os.environ.get("SHELL") or ""
    if (
        user_shell
        and os.path.basename(user_shell) in _POSIX_SHELL_NAMES
        and os.path.isfile(user_shell)
    ):
        path = user_shell
    else:
        path = "/bin/sh"
    return ShellInfo(name="posix", path=path, argv_prefix=(path, "-c"))


@lru_cache(maxsize=1)
def resolve_shell() -> ShellInfo:
    """The shell the Bash and Monitor tools run commands through (cached)."""
    if sys.platform == "win32" or os.name == "nt":
        return _resolve_windows()
    return _resolve_posix()


def describe(info: ShellInfo | None = None) -> str:
    """The text after ``Shell:`` in the environment block."""
    info = info or resolve_shell()
    if info.name == "git-bash":
        return (
            f"Git Bash ({info.path}): the Bash tool runs POSIX sh syntax; "
            "Windows paths with backslashes must be quoted or written with "
            "forward slashes"
        )
    if info.name == "cmd":
        return (
            f"cmd.exe ({info.path}): the Bash tool uses Windows cmd syntax, not "
            "PowerShell or POSIX shell syntax"
        )
    return f"{info.path} (the Bash tool uses POSIX shell syntax)"
