"""Hermetic, real-git repository builders for the git integration tests.

Every repository lives under the test's ``tmp_path``. The git CLI is run with
the user's global and system configuration switched off, so a developer's
``~/.gitconfig`` (credential helpers, hooks, signing, default branch) can never
leak into a test or make a network operation prompt for credentials.
"""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterator, Optional

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}

# Modules that bind ``_native_reader`` at import time. Switching the backend
# means patching every one of them.
_NATIVE_READER_MODULES = (
    "app.core.git_service",
    "app.core.git_branch_ops",
    "app.core.git_diff_ops",
    "app.core.git_worktree_ops",
)


def hermetic_git_env(monkeypatch, tmp_path: Path) -> None:
    """Isolate git from the machine's global/system config for this test."""
    empty_config = tmp_path / ".gitconfig-empty"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for var in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)


def git(cwd, *args: str, check: bool = True) -> str:
    """Run git in *cwd* and return stripped stdout."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def init_repo(
    path: Path,
    files: Optional[Dict[str, object]] = None,
    branch: str = "main",
    commit: bool = True,
) -> Path:
    """Create a repository at *path*, optionally with an initial commit."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.name", GIT_IDENTITY["GIT_AUTHOR_NAME"])
    git(path, "config", "user.email", GIT_IDENTITY["GIT_AUTHOR_EMAIL"])
    git(path, "config", "commit.gpgsign", "false")
    if commit:
        for rel, content in (files or {"README.md": "hello\n"}).items():
            write(path / rel, content)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "initial")
    return path


def commit_file(repo: Path, rel: str, content, message: str) -> str:
    write(repo / rel, content)
    git(repo, "add", "--", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_origin(tmp_path: Path, repo: Path, name: str = "origin.git") -> Path:
    """Create a bare repo, add it as ``origin`` of *repo* and push main to it."""
    bare = tmp_path / name
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    return bare


def clone(tmp_path: Path, bare: Path, name: str) -> Path:
    target = tmp_path / name
    git(tmp_path, "clone", "-q", str(bare), str(target))
    git(target, "config", "user.name", GIT_IDENTITY["GIT_AUTHOR_NAME"])
    git(target, "config", "user.email", GIT_IDENTITY["GIT_AUTHOR_EMAIL"])
    return target


def use_backend(monkeypatch, backend: str) -> None:
    """Route GitService reads through pygit2 (``native``) or the git CLI (``cli``)."""
    import importlib

    from app.core import git_cache

    if backend == "native":
        from app.core.git_native import GitNativeReader

        reader = GitNativeReader()
    elif backend == "cli":
        reader = None
    else:  # pragma: no cover - programming error in a test
        raise ValueError(backend)
    for name in _NATIVE_READER_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "_native_reader", reader)
    git_cache._git_cache.invalidate_prefix("")


@contextmanager
def auth_required_http_remote() -> Iterator[str]:
    """Serve a localhost HTTP 'remote' that answers every request with 401.

    Git's smart-HTTP client then needs credentials; with prompting disabled
    and no helper configured the operation fails fast instead of hanging.
    """

    class _Handler(BaseHTTPRequestHandler):
        def _deny(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _deny
        do_POST = _deny

        def log_message(self, *args):  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/repo.git"
    finally:
        server.shutdown()
        server.server_close()


def non_utf8_name_supported(directory: Path) -> bool:
    """Whether the filesystem accepts a non-UTF-8 byte in a file name."""
    probe = os.path.join(os.fsencode(directory), b"probe\xff")
    try:
        with open(probe, "wb"):
            pass
        os.remove(probe)
        return True
    except (OSError, UnicodeError):
        return False
