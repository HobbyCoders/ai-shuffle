"""Git settings every agent shell gets, and what they stop.

``git status`` is rated read-only, so it runs without a prompt. Git itself
runs the program a repository's config names (``core.fsmonitor``), and a
folder of plain files is a repository to git, so a clone can ship one. The
tests plant such a folder and check, with the real ``git``, that the program
runs without the settings and does not run with them, then that the Bash tool
applies them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fluid.tools import child_env
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.child_env import (
    AGENT_GIT_CONFIG,
    claude_cli_env_with_overlay,
    git_version_supports_hardening,
    harden_git_env,
)
from fluid.tools.registry import ToolCtx


def _config(env: dict[str, str]) -> list[tuple[str, str]]:
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


def test_settings_are_appended_after_the_ones_already_there() -> None:
    env = harden_git_env({
        "PATH": "/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "Sam",
    })
    assert _config(env) == [("user.name", "Sam"), *AGENT_GIT_CONFIG]
    assert env["PATH"] == "/bin"


def test_applying_the_settings_twice_adds_nothing() -> None:
    once = harden_git_env({})
    assert _config(once) == list(AGENT_GIT_CONFIG)
    assert harden_git_env(dict(once)) == once


def test_an_inherited_entry_cannot_switch_a_setting_back() -> None:
    """Git uses the last entry for a key, so ours go last."""
    env = harden_git_env({
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "SAFE.bareRepository",
        "GIT_CONFIG_VALUE_0": "all",
        "GIT_CONFIG_KEY_1": "core.fsmonitor",
        "GIT_CONFIG_VALUE_1": "false",
    })
    assert _config(env) == [
        ("SAFE.bareRepository", "all"),
        ("core.fsmonitor", "false"),
        ("safe.bareRepository", "explicit"),
    ]


def test_a_malformed_count_is_left_for_git_to_reject() -> None:
    env = {"GIT_CONFIG_COUNT": "two"}
    assert harden_git_env(env) == {"GIT_CONFIG_COUNT": "two"}


def test_the_delegated_cli_inherits_the_settings() -> None:
    env = claude_cli_env_with_overlay(None, {"PATH": "/bin"})
    assert _config(env) == list(AGENT_GIT_CONFIG)


# ─── a git too old for the settings ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("output", "supported"),
    [
        ("git version 2.47.3\n", True),
        ("git version 2.38.0\n", True),
        ("git version 2.47.0.windows.1\n", True),
        ("git version 2.39.5 (Apple Git-154)\n", True),
        ("git version 3.0.0\n", True),
        ("git version 2.37.7\n", False),  # GIT_CONFIG_COUNT, no safe.bareRepository
        ("git version 2.25.1\n", False),  # neither
        ("git version 1.8.3.1\n", False),
        ("", False),
        ("bash: git: command not found\n", False),
    ],
)
def test_which_git_versions_honour_the_settings(output: str, supported: bool) -> None:
    assert git_version_supports_hardening(output) is supported


@pytest.fixture
def git_probe(monkeypatch: pytest.MonkeyPatch):
    """Replace the ``git --version`` call; returns the list of commands run."""
    calls: list[list[str]] = []
    answer: dict[str, object] = {"stdout": "git version 2.47.3\n", "returncode": 0}

    def run(argv, **kwargs):
        calls.append(list(argv))
        if answer.get("raise"):
            raise answer["raise"]  # type: ignore[misc]
        return subprocess.CompletedProcess(argv, answer["returncode"], answer["stdout"], "")

    monkeypatch.setattr(child_env.subprocess, "run", run)
    child_env.git_hardening_supported.cache_clear()
    yield calls, answer
    child_env.git_hardening_supported.cache_clear()


def test_the_probe_asks_git_once_through_the_agent_shell(git_probe) -> None:
    from fluid.tools.shell import resolve_shell

    calls, _ = git_probe
    assert child_env.git_hardening_supported() is True
    assert child_env.git_hardening_supported() is True
    assert calls == [[*resolve_shell().argv_prefix, "git --version"]]


@pytest.mark.parametrize(
    "answer",
    [
        {"stdout": "git version 2.30.2\n", "returncode": 0},
        {"stdout": "", "returncode": 127},
        {"stdout": "git version 2.47.3\n", "returncode": 1},
        {"raise": FileNotFoundError("sh")},
        {"raise": subprocess.TimeoutExpired("git", 10)},
    ],
)
def test_an_old_missing_or_silent_git_is_not_trusted(git_probe, answer) -> None:
    from fluid.tools.builtins.bash_risk import classify

    _, current = git_probe
    current.update(answer)
    assert child_env.git_hardening_supported() is False
    assert classify({"command": "git status"}) == "write"
    assert classify({"command": "ls"}) == "safe"


def test_a_current_git_keeps_its_reads_unprompted(git_probe) -> None:
    from fluid.tools.builtins.bash_risk import classify

    assert classify({"command": "git status"}) == "safe"


# ─── against the real git ────────────────────────────────────────────────────


def _git_version() -> tuple[int, int]:
    git = shutil.which("git")
    if git is None:
        return (0, 0)
    out = subprocess.run([git, "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


needs_git = pytest.mark.skipif(
    _git_version() < (2, 38), reason="safe.bareRepository needs git 2.38 or newer"
)


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An environment with no user or system git config, also set on the
    process so the Bash tool's child inherits it."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    home = tmp_path / "home"
    home.mkdir()
    values = {
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "Sam", "GIT_AUTHOR_EMAIL": "sam@example.com",
        "GIT_COMMITTER_NAME": "Sam", "GIT_COMMITTER_EMAIL": "sam@example.com",
    }
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
        monkeypatch.delenv(name)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return dict(os.environ)


def _git(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)


def _clone_with_planted_repository(root: Path, env: dict[str, str]) -> tuple[Path, Path]:
    """A normal checkout holding ``planted/``: plain files git reads as a
    repository whose config names a program. Returns it and the file that
    program creates."""
    repo = root / "repo"
    repo.mkdir()
    assert _git(env, repo, "init", "-q").returncode == 0
    assert _git(env, repo, "commit", "-q", "--allow-empty", "-m", "start").returncode == 0
    marker = root / "program-ran"
    planted = repo / "planted"
    (planted / "objects").mkdir(parents=True)
    (planted / "refs" / "heads").mkdir(parents=True)
    shutil.copy(repo / ".git" / "HEAD", planted / "HEAD")
    (planted / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tbare = false\n"
        f"\tworktree = {repo.as_posix()}\n"
        f"\tfsmonitor = \"touch '{marker.as_posix()}'; false\"\n"
    )
    return repo, marker


@needs_git
def test_a_planted_repository_runs_its_program_without_the_settings(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    """The baseline: this is what ``git -C planted status`` did unprompted."""
    repo, marker = _clone_with_planted_repository(tmp_path, git_env)
    _git(git_env, repo, "-C", "planted", "status")
    assert marker.exists()


@needs_git
def test_a_planted_repository_is_refused_with_the_settings(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    repo, marker = _clone_with_planted_repository(tmp_path, git_env)
    hardened = harden_git_env(dict(git_env))
    for args in (("-C", "planted", "status"), ("-C", "planted", "log", "-1")):
        result = _git(hardened, repo, *args)
        assert result.returncode != 0
        assert "safe.bareRepository" in result.stderr
    # Standing in the folder is the same as naming it.
    assert _git(hardened, repo / "planted", "status").returncode != 0
    assert not marker.exists()


@needs_git
def test_a_checkouts_own_config_cannot_name_a_status_program(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    repo, _ = _clone_with_planted_repository(tmp_path, git_env)
    marker = tmp_path / "checkout-program-ran"
    command = f"touch '{marker.as_posix()}'; false"
    assert _git(git_env, repo, "config", "core.fsmonitor", command).returncode == 0
    _git(git_env, repo, "status")
    assert marker.exists()  # the baseline
    marker.unlink()

    result = _git(harden_git_env(dict(git_env)), repo, "status", "--short")
    assert result.returncode == 0
    assert not marker.exists()


@needs_git
def test_checkouts_and_linked_worktrees_still_work(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    repo, _ = _clone_with_planted_repository(tmp_path, git_env)
    linked = tmp_path / "linked"
    assert _git(git_env, repo, "worktree", "add", "-q", str(linked), "-b", "side").returncode == 0
    (repo / "src").mkdir()
    hardened = harden_git_env(dict(git_env))
    for cwd in (repo, repo / "src", linked):
        result = _git(hardened, cwd, "status", "--short", "--branch")
        assert result.returncode == 0, result.stderr
    assert _git(hardened, tmp_path, "-C", str(linked), "log", "-1", "--oneline").returncode == 0


@needs_git
async def test_the_bash_tool_applies_the_settings(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    """Through the tool the model calls, not the helper."""
    repo, marker = _clone_with_planted_repository(tmp_path, git_env)
    await bash_handler({"command": "git -C planted status"}, ToolCtx(cwd=str(repo)))
    assert not marker.exists()
    # And with the planted folder as the command's working directory.
    await bash_handler({"command": "git status"}, ToolCtx(cwd=str(repo / "planted")))
    assert not marker.exists()
