"""
Git Hook Manager

Installs and manages git hooks (post-commit, post-checkout, post-merge,
post-rewrite) in monitored repositories. Hooks send HTTP callbacks to
the backend for immediate state updates, bypassing the watchdog debounce.

Key design:
- POSIX shell scripts (work on Windows via Git Bash)
- Hook chaining: preserves existing user hooks by renaming to .user suffix
- Token-based auth prevents unauthorized callbacks
- Fire-and-forget HTTP POST with 2s timeout (never blocks git operations)
- Hooks are installed when a project starts being watched, removed on unwatch
"""

import hmac
import logging
import os
import platform
import secrets
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# Marker comment to identify hooks managed by AI Shuffle
HOOK_MARKER = "# AI-SHUFFLE-MANAGED-HOOK"
HOOK_VERSION = "v1"

# Hooks we install for event-driven git state detection
MANAGED_HOOKS = ["post-commit", "post-checkout", "post-merge", "post-rewrite"]

# Map hook names to the git state categories they affect
HOOK_CATEGORY_MAP = {
    "post-commit": {"status", "diff_stats", "branches"},
    "post-checkout": {"status", "branches", "diff_stats"},
    "post-merge": {"status", "diff_stats", "branches"},
    "post-rewrite": {"status", "diff_stats"},
}


@dataclass
class HookInstallation:
    """Tracks an installed set of hooks for a repository."""
    repo_path: str
    hooks_dir: str
    installed_hooks: Set[str] = field(default_factory=set)
    chained_hooks: Set[str] = field(default_factory=set)  # Hooks that had user originals


def _get_hooks_dir(working_dir: str, git_dir: str) -> str:
    """Get the hooks directory, respecting core.hooksPath config."""
    try:
        result = subprocess.run(
            ["git", "config", "core.hooksPath"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            hooks_path = result.stdout.strip()
            if not os.path.isabs(hooks_path):
                hooks_path = os.path.join(working_dir, hooks_path)
            return os.path.normpath(hooks_path)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return os.path.join(git_dir, "hooks")


def _generate_hook_script(hook_name: str, port: int, token: str, hooks_dir: str) -> str:
    """Generate a POSIX shell hook script with HTTP callback and chaining."""
    hook_path = os.path.join(hooks_dir, hook_name).replace("\\", "/")

    return f"""#!/bin/sh
{HOOK_MARKER} {HOOK_VERSION}
# Managed by AI Shuffle — do not edit manually.
# Sends a callback to the local backend and chains to any existing user hook.

curl -s -m 2 -X POST "http://127.0.0.1:{port}/api/v1/internal/git-hook" \\
  -H "Content-Type: application/json" \\
  -H "X-Hook-Token: {token}" \\
  -d '{{"hook":"{hook_name}","repo":"'"$(git rev-parse --show-toplevel 2>/dev/null)"'"}}' \\
  >/dev/null 2>&1 || true

# Chain to user's original hook if present
if [ -x "{hook_path}.user" ]; then
  exec "{hook_path}.user" "$@"
fi
"""


def _is_managed_hook(hook_path: str) -> bool:
    """Check if a hook file is one we installed (by checking for marker comment)."""
    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as f:
            # Read first 5 lines to find the marker
            for _ in range(5):
                line = f.readline()
                if HOOK_MARKER in line:
                    return True
        return False
    except (OSError, IOError):
        return False


class GitHookManager:
    """Manages installation and removal of git hooks for monitored repositories."""

    def __init__(self):
        self._installed: Dict[str, HookInstallation] = {}  # repo_path -> install info
        self._hook_token: str = ""
        self._server_port: int = 8000
        self._started: bool = False

    @property
    def hook_token(self) -> str:
        return self._hook_token

    async def start(self, port: int) -> None:
        """Initialize the hook manager. Call during app startup."""
        self._hook_token = secrets.token_hex(16)
        self._server_port = port
        self._started = True
        logger.info("Git hook manager started")

    async def shutdown(self) -> None:
        """Uninstall all hooks and clean up. Call during app shutdown."""
        if not self._started:
            return

        for repo_path in list(self._installed.keys()):
            try:
                self._uninstall_hooks(repo_path)
            except Exception as e:
                logger.warning(f"Failed to uninstall hooks for {repo_path}: {e}")

        self._installed.clear()
        self._started = False
        logger.info("Git hook manager stopped — all hooks uninstalled")

    def install_hooks(self, repo_path: str, git_dir: str) -> bool:
        """Install managed hooks into a repository.

        Chains with existing user hooks by renaming them to .user suffix.

        Args:
            repo_path: The repository's working directory (top-level).
            git_dir: The resolved .git directory path.

        Returns:
            True if hooks were installed (or already installed), False on error.
        """
        if not self._started:
            return False

        # Already installed for this repo
        if repo_path in self._installed:
            return True

        hooks_dir = _get_hooks_dir(repo_path, git_dir)

        # Ensure hooks directory exists
        try:
            os.makedirs(hooks_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create hooks dir {hooks_dir}: {e}")
            return False

        installation = HookInstallation(
            repo_path=repo_path,
            hooks_dir=hooks_dir,
        )

        for hook_name in MANAGED_HOOKS:
            hook_path = os.path.join(hooks_dir, hook_name)
            user_hook_path = hook_path + ".user"

            try:
                # Check if there's an existing hook
                if os.path.exists(hook_path):
                    if _is_managed_hook(hook_path):
                        # Already our hook (e.g., from a previous run that didn't clean up).
                        # Overwrite with fresh script (port/token may have changed).
                        pass
                    else:
                        # User's hook — chain it
                        if os.path.exists(user_hook_path):
                            # .user already exists (from a previous incomplete cleanup).
                            # Don't overwrite it — the .user file is the real original.
                            pass
                        else:
                            os.rename(hook_path, user_hook_path)
                        installation.chained_hooks.add(hook_name)

                # Write our hook script
                script = _generate_hook_script(
                    hook_name=hook_name,
                    port=self._server_port,
                    token=self._hook_token,
                    hooks_dir=hooks_dir,
                )
                with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(script)

                # Make executable on Unix-like systems
                if platform.system() != "Windows":
                    os.chmod(hook_path, os.stat(hook_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

                installation.installed_hooks.add(hook_name)

            except OSError as e:
                logger.warning(f"Failed to install {hook_name} hook in {hooks_dir}: {e}")

        if installation.installed_hooks:
            self._installed[repo_path] = installation
            chained = f" (chained: {', '.join(installation.chained_hooks)})" if installation.chained_hooks else ""
            logger.debug(
                f"Installed git hooks in {hooks_dir}: "
                f"{', '.join(installation.installed_hooks)}{chained}"
            )
            return True

        return False

    def uninstall_hooks(self, repo_path: str) -> None:
        """Uninstall managed hooks from a repository.

        Restores user hooks from .user suffix if they exist.
        """
        self._uninstall_hooks(repo_path)

    def _uninstall_hooks(self, repo_path: str) -> None:
        """Internal uninstall implementation."""
        installation = self._installed.get(repo_path)
        if not installation:
            return

        hooks_dir = installation.hooks_dir

        for hook_name in installation.installed_hooks:
            hook_path = os.path.join(hooks_dir, hook_name)
            user_hook_path = hook_path + ".user"

            try:
                # Only remove if it's our managed hook (safety check)
                if os.path.exists(hook_path) and _is_managed_hook(hook_path):
                    os.remove(hook_path)

                    # Restore user's original hook
                    if os.path.exists(user_hook_path):
                        os.rename(user_hook_path, hook_path)

            except OSError as e:
                logger.warning(f"Failed to uninstall {hook_name} hook from {hooks_dir}: {e}")

        del self._installed[repo_path]
        logger.debug(f"Uninstalled git hooks from {hooks_dir}")

    def verify_hook_token(self, token: str) -> bool:
        """Verify a token from a hook callback (timing-safe comparison)."""
        if not self._hook_token or not token:
            return False
        return hmac.compare_digest(token, self._hook_token)

    def is_installed(self, repo_path: str) -> bool:
        """Check if hooks are currently installed for a repository."""
        return repo_path in self._installed

    def get_installed_repos(self) -> list:
        """Get list of repo paths with installed hooks."""
        return list(self._installed.keys())


# Singleton instance
git_hook_manager = GitHookManager()
