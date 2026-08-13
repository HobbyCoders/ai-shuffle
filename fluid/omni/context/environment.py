"""Environment-context gathering for the prompt compiler.

Claude Code injects an environment block (cwd, platform, OS, date, git state)
into its system prompt; omni gathers the same facts itself so every consumer
gets that context without assembling it app-side. All gathering is best-effort:
a missing git binary, a non-repo cwd, or a slow subprocess degrades to omitting
that line, never to an exception.
"""

from __future__ import annotations

import os
import platform as _platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_GIT_TIMEOUT = 3.0  # seconds; env gathering must never stall a session start


def _run_git(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _os_version() -> str:
    try:
        if sys.platform == "darwin":
            return f"macOS {_platform.mac_ver()[0]}"
        return f"{_platform.system()} {_platform.release()}"
    except Exception:
        return _platform.system() or "unknown"


def git_context(cwd: str) -> str | None:
    """Branch, dirty-state summary, and recent commits for ``cwd``'s repo.

    Returns ``None`` when ``cwd`` is not inside a git work tree (or git is
    unavailable), so callers can omit the section entirely.
    """
    inside = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return None
    lines: list[str] = []
    branch = _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        lines.append(f"Current branch: {branch}")
    status = _run_git(cwd, "status", "--porcelain")
    if status is not None:
        changed = len(status.splitlines())
        lines.append(
            "Working tree: clean" if changed == 0 else f"Working tree: {changed} changed path(s)"
        )
    recent = _run_git(cwd, "log", "--oneline", "-5")
    if recent:
        lines.append("Recent commits:\n" + recent)
    if not lines:
        return None
    return "\n".join(lines)


def environment_block(cwd: str | os.PathLike[str] = ".") -> str:
    """The compiled prompt's environment section (always renders).

    Facts that cannot be gathered are omitted line-by-line; the block itself is
    always non-empty (cwd + date at minimum).
    """
    cwd_str = str(Path(cwd).resolve()) if cwd else str(Path.cwd())
    lines = [
        "# Environment",
        f"Working directory: {cwd_str}",
        f"Platform: {sys.platform}",
        f"OS: {_os_version()}",
        f"Today's date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
    ]
    git = git_context(cwd_str)
    if git is not None:
        lines.append("Git repository: yes")
        lines.append(git)
    else:
        lines.append("Git repository: no")
    return "\n".join(lines)
