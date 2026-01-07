"""
Platform detection and OS-specific path handling for local mode.

This module provides cross-platform support for running AI Shuffle outside of Docker,
detecting the deployment mode (Docker vs Local) and providing appropriate paths.
"""

import os
import sys
from pathlib import Path
from enum import Enum
from typing import Optional


class DeploymentMode(Enum):
    """Deployment mode for the application"""
    DOCKER = "docker"
    LOCAL = "local"


def detect_deployment_mode() -> DeploymentMode:
    """
    Detect whether we're running in Docker or locally.

    Detection methods (in order of priority):
    1. LOCAL_MODE environment variable (explicit override)
    2. Check for /.dockerenv file (Docker indicator)
    3. Check if running as 'appuser' (Docker user)
    4. Check for /data directory (Docker volume mount)
    """
    # Explicit override via environment variable
    local_mode_env = os.environ.get("LOCAL_MODE", "").lower()
    if local_mode_env in ("true", "1", "yes"):
        return DeploymentMode.LOCAL
    if local_mode_env in ("false", "0", "no"):
        return DeploymentMode.DOCKER

    # Check for Docker indicators
    if Path("/.dockerenv").exists():
        return DeploymentMode.DOCKER

    # Check if we're the Docker user
    if os.environ.get("USER") == "appuser" or os.environ.get("HOME") == "/home/appuser":
        return DeploymentMode.DOCKER

    # Check for Docker volume mounts
    if Path("/data").exists() and Path("/workspace").exists() and Path("/app").exists():
        return DeploymentMode.DOCKER

    # Default to local mode if no Docker indicators found
    return DeploymentMode.LOCAL


def get_app_data_dir() -> Path:
    """
    Get the application data directory based on the current platform.

    Returns:
        - Windows: %APPDATA%/ai-shuffle
        - macOS: ~/Library/Application Support/ai-shuffle
        - Linux: ~/.local/share/ai-shuffle
        - Docker: /data
    """
    if detect_deployment_mode() == DeploymentMode.DOCKER:
        return Path("/data")

    if sys.platform == "win32":
        # Windows: Use USERPROFILE directly to avoid Microsoft Store Python virtualization
        # Microsoft Store Python virtualizes APPDATA and LOCALAPPDATA paths,
        # but direct USERPROFILE access is not virtualized
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return Path(userprofile) / ".ai-shuffle"
        return Path.home() / ".ai-shuffle"

    elif sys.platform == "darwin":
        # macOS: Use Application Support
        return Path.home() / "Library" / "Application Support" / "ai-shuffle"

    else:
        # Linux and other Unix-like systems
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "ai-shuffle"
        return Path.home() / ".local" / "share" / "ai-shuffle"


def get_default_workspace_dir() -> Path:
    """
    Get the default workspace directory based on the current platform.

    Returns:
        - Windows: %USERPROFILE%/Documents/ai-shuffle-workspace
        - macOS/Linux: ~/ai-shuffle-workspace
        - Docker: /workspace
    """
    if detect_deployment_mode() == DeploymentMode.DOCKER:
        return Path("/workspace")

    if sys.platform == "win32":
        # Windows: Use Documents folder
        docs = os.environ.get("USERPROFILE")
        if docs:
            return Path(docs) / "Documents" / "ai-shuffle-workspace"
        return Path.home() / "Documents" / "ai-shuffle-workspace"

    else:
        # macOS/Linux: Use home directory
        return Path.home() / "ai-shuffle-workspace"


def get_claude_credentials_dir() -> Path:
    """
    Get the Claude credentials directory.

    Returns:
        Path to the .claude directory in user's home
    """
    return Path.home() / ".claude"


def get_claude_executable() -> Optional[str]:
    """
    Find the Claude CLI executable.

    Returns:
        Path to claude executable, or None if not found
    """
    import shutil

    # Try common names
    for name in ["claude", "claude.exe", "claude-code"]:
        path = shutil.which(name)
        if path:
            return path

    # Check common installation locations
    possible_paths = []

    if sys.platform == "win32":
        # Windows npm global install locations
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            possible_paths.extend([
                Path(appdata) / "npm" / "claude.cmd",
                Path(appdata) / "npm" / "claude",
            ])
        # Also check Program Files
        possible_paths.extend([
            Path("C:/Program Files/nodejs/claude.cmd"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        ])
    else:
        # Unix-like systems
        possible_paths.extend([
            Path.home() / ".npm-global" / "bin" / "claude",
            Path("/usr/local/bin/claude"),
            Path("/usr/bin/claude"),
            Path.home() / ".local" / "bin" / "claude",
        ])

    for path in possible_paths:
        if path.exists():
            return str(path)

    return None


def get_gh_executable() -> Optional[str]:
    """
    Find the GitHub CLI executable.

    Returns:
        Path to gh executable, or None if not found
    """
    import shutil

    path = shutil.which("gh")
    if path:
        return path

    # Check common installation locations
    if sys.platform == "win32":
        possible_paths = [
            Path("C:/Program Files/GitHub CLI/gh.exe"),
            Path("C:/Program Files (x86)/GitHub CLI/gh.exe"),
        ]
    else:
        possible_paths = [
            Path("/usr/local/bin/gh"),
            Path("/usr/bin/gh"),
            Path.home() / ".local" / "bin" / "gh",
        ]

    for path in possible_paths:
        if path.exists():
            return str(path)

    return None


def is_local_mode() -> bool:
    """Quick check if running in local mode"""
    return detect_deployment_mode() == DeploymentMode.LOCAL


def is_docker_mode() -> bool:
    """Quick check if running in Docker mode"""
    return detect_deployment_mode() == DeploymentMode.DOCKER


def get_platform_info() -> dict:
    """
    Get comprehensive platform information for diagnostics.

    Returns:
        Dictionary with platform details
    """
    return {
        "platform": sys.platform,
        "python_version": sys.version,
        "deployment_mode": detect_deployment_mode().value,
        "app_data_dir": str(get_app_data_dir()),
        "workspace_dir": str(get_default_workspace_dir()),
        "claude_credentials_dir": str(get_claude_credentials_dir()),
        "claude_executable": get_claude_executable(),
        "gh_executable": get_gh_executable(),
        "home_dir": str(Path.home()),
        "cwd": str(Path.cwd()),
    }
