"""Disposable, offline workflow smoke for the agent isolation runner.

Run only in a throwaway project directory, never the application's data directory.
No downloads, credentials, global configuration or application database are used.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, timeout=30)


def main():
    with tempfile.TemporaryDirectory(prefix="isolation-workflow-", dir=Path.cwd()) as directory:
        root = Path(directory)
        repo = root / "project"
        repo.mkdir()
        run("git", "init", "--initial-branch=main", str(repo), cwd=root)
        run("git", "config", "user.name", "Isolation Fixture", cwd=repo)
        run("git", "config", "user.email", "fixture@example.invalid", cwd=repo)
        (repo / "value.txt").write_text("before\n")
        run("git", "add", "value.txt", cwd=repo)
        run("git", "-c", "commit.gpgsign=false", "commit", "-m", "fixture", cwd=repo)
        worktree = root / "branch"
        run("git", "worktree", "add", "-b", "fixture-branch", str(worktree), cwd=repo)
        (worktree / "value.txt").write_text("after\n")
        assert (repo / "value.txt").read_text() == "before\n"
        run("git", "diff", "--check", cwd=worktree)
        (worktree / "package.json").write_text(json.dumps({
            "private": True, "scripts": {"verify": "node -e \"require('fs').writeFileSync('npm-ok', 'ok')\""}
        }))
        run("npm", "--offline", "--ignore-scripts", "run", "verify", cwd=worktree)
        assert (worktree / "npm-ok").read_text() == "ok"
        (worktree / "test_fixture.py").write_text(
            "from pathlib import Path\ndef test_edit():\n"
            "    assert Path('value.txt').read_text() == 'after\\n'\n"
        )
        run(sys.executable, "-m", "pytest", "test_fixture.py", "-q", "-p", "no:cacheprovider", cwd=worktree)
    print("ISOLATION_WORKFLOW_OK", flush=True)


if __name__ == "__main__":
    main()
