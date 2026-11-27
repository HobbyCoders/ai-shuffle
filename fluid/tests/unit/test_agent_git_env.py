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
    child_env._GIT_ANSWERS.clear()
    yield calls, answer
    child_env._GIT_ANSWERS.clear()


def _asked() -> bool:
    """The answer where the permission engine lets git be asked."""
    with child_env.launch_context(None, may_ask_git=True):
        return child_env.git_hardening_supported()


def _rated(command: str) -> str:
    from fluid.tools.builtins.bash_risk import classify

    with child_env.launch_context(None, may_ask_git=True):
        return classify({"command": command})


def test_the_probe_asks_git_once_through_the_agent_shell(git_probe) -> None:
    from fluid.tools.shell import resolve_shell

    calls, _ = git_probe
    assert _asked() is True
    assert _asked() is True
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
    _, current = git_probe
    current.update(answer)
    assert _asked() is False
    assert _rated("git status") == "write"
    assert _rated("ls") == "safe"


def test_a_current_git_keeps_its_reads_unprompted(git_probe) -> None:
    assert _rated("git status") == "safe"


def test_git_is_not_started_unless_the_engine_allows_it(git_probe) -> None:
    """Outside the permission engine (code mode's own look at a call's risk),
    nothing is started: the answer is the one already known, or no."""
    from fluid.tools.builtins.bash_risk import classify

    calls, _ = git_probe
    assert child_env.git_hardening_supported() is False
    assert classify({"command": "git status"}) == "write"
    assert calls == []
    assert _asked() is True
    # Known now, so the same question costs nothing and starts nothing.
    assert child_env.git_hardening_supported() is True
    assert classify({"command": "git status"}) == "safe"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("policy", "rule_decision"),
    [
        ("on_write", "deny"),
        ("on_write", "ask"),
        ("on_write", "allow"),
        ("always_ask", None),
        ("never_ask", None),
    ],
)
def test_git_is_not_started_for_a_call_the_rating_does_not_decide(
    git_probe, policy: str, rule_decision: str | None
) -> None:
    """A rule or the policy already settles these calls. Whatever git is
    first on the worker's PATH, a folder the agent can write to included, it
    is not started to rate a call that is denied or put to a person."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule
    from fluid.tools.builtins.bash import SPEC as BASH_SPEC
    from fluid.tools.builtins.monitor import SPEC as MONITOR_SPEC

    calls, _ = git_probe
    rules = (
        [Rule(tool="*", decision=rule_decision, input_globs={"command": "git status"})]
        if rule_decision else []
    )
    expected = rule_decision or {"always_ask": "ask", "never_ask": "allow"}[policy]
    for spec in (BASH_SPEC, MONITOR_SPEC):
        verdict = PermissionEngine(policy, rules=list(rules)).decide(
            spec, {"command": "git status"})
        assert verdict.decision == expected
    assert calls == []


def test_a_git_installed_later_is_asked_again(
    git_probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer belongs to the git file a PATH search finds. One that
    appears earlier on PATH afterwards is another git."""
    calls, current = git_probe
    first = tmp_path / "first"
    later = tmp_path / "later"
    for folder in (first, later):
        folder.mkdir()
    name = "git.exe" if os.name == "nt" else "git"
    (first / name).write_text("one")
    (first / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{later}{os.pathsep}{first}")
    assert _asked() is True
    current.update({"stdout": "git version 2.30.2\n"})
    assert _asked() is True
    assert len(calls) == 1

    (later / name).write_text("another")
    (later / name).chmod(0o755)
    assert child_env.git_hardening_supported() is False  # not known, not asked
    assert _asked() is False
    assert len(calls) == 2


def test_the_probe_never_gets_a_sessions_settings(git_probe) -> None:
    """The check asks the worker's own git only: a session's ``PATH`` is not
    in what it starts."""
    calls, _ = git_probe
    seen: list[dict[str, str]] = []
    real_run = child_env.subprocess.run

    def run(argv, **kwargs):
        seen.append(dict(kwargs["env"]))
        return real_run(argv, **kwargs)

    child_env.subprocess.run = run  # the fixture's monkeypatch restores it
    assert _asked() is True
    overlay = {"PATH": "/opt/old-git/bin", "GIT_EXEC_PATH": "/opt/old-git/libexec"}
    with child_env.launch_context(overlay, may_ask_git=True):
        assert child_env.git_hardening_supported() is True
    assert len(calls) == 1
    assert seen[0].get("PATH") == child_env.scrub_child_env().get("PATH")
    assert seen[0].get("GIT_EXEC_PATH") != "/opt/old-git/libexec"
    # The git settings are part of what a shell gets, so of what is asked.
    assert _config(seen[0])[-len(AGENT_GIT_CONFIG):] == list(AGENT_GIT_CONFIG)


def test_the_probe_starts_the_way_agent_commands_do(git_probe, monkeypatch) -> None:
    """Under Docker agent isolation every agent process goes through the
    spawn runner; so does the question."""
    from fluid.tools import agent_runner
    from fluid.tools.shell import resolve_shell

    calls, _ = git_probe
    wrapped: list[list[str]] = []

    def wrap(argv, cwd=None, env=None, **_):
        wrapped.append(list(argv))
        return ["runner", "--", *argv], "/home/agent", env

    monkeypatch.setattr(agent_runner, "wrap", wrap)
    assert _asked() is True
    assert wrapped == [[*resolve_shell().argv_prefix, "git --version"]]
    assert calls == [["runner", "--", *resolve_shell().argv_prefix, "git --version"]]


@pytest.mark.parametrize(
    "name",
    [
        "PATH", "Path", "PATHEXT", "BASH_ENV", "ENV", "ZDOTDIR", "HOME", "USERPROFILE",
        "XDG_CONFIG_HOME", "GIT_EXEC_PATH", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
        "GIT_PAGER", "BASH_FUNC_git%%", "LD_PRELOAD", "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES", "MSYS2_PATH_TYPE",
    ],
)
def test_settings_that_choose_the_programs_leave_nothing_read_only(git_probe, name: str) -> None:
    """A session whose settings say where programs are found, or what a shell
    or git runs first, is not running the programs the check knows. Nothing
    is started to find out; every command of that session asks."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as BASH_SPEC
    from fluid.tools.builtins.monitor import SPEC as MONITOR_SPEC

    calls, _ = git_probe
    engine = PermissionEngine("on_write")
    overlay = {name: "/somewhere/else"}
    for spec in (BASH_SPEC, MONITOR_SPEC):
        for command in ("git status", "ls", "cat notes.txt"):
            call = {"command": command}
            assert engine.decide(spec, call, env=overlay).decision == "ask"
            # The overlay does not outlive the call it was given for.
            assert engine.decide(spec, call).decision == "allow"
    assert all(argv[-1] == "git --version" for argv in calls) and len(calls) == 1


def test_other_settings_keep_reads_unprompted(git_probe) -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.bash import SPEC as BASH_SPEC

    engine = PermissionEngine("on_write")
    overlay = {"NODE_ENV": "test", "DEBUG": "1", "EDITOR": "vim", "PATHFINDER": "x"}
    for command in ("git status", "ls"):
        assert engine.decide(BASH_SPEC, {"command": command}, env=overlay).decision == "allow"


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


def _recording_git(folder: Path) -> tuple[dict[str, str], Path]:
    """A settings ``env`` that puts a stand-in git first on PATH, and the file
    the stand-in writes each time it is started."""
    folder.mkdir()
    ran = folder / "ran.log"
    script = folder / "git"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{ran}'\necho 'git version 2.30.2'\n")
    script.chmod(0o755)
    return {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}, ran


@needs_git
@pytest.mark.skipif(os.name == "nt", reason="the stand-in git is a POSIX script")
def test_a_denied_git_command_starts_nothing_the_session_chose(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    """A rule denies ``git status`` and the session's settings put another git
    first on PATH. Rating the call comes before the rule, and it must not
    start that git, not even to ask its version."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule
    from fluid.tools.builtins.bash import SPEC as BASH_SPEC
    from fluid.tools.builtins.monitor import SPEC as MONITOR_SPEC

    overlay, ran = _recording_git(tmp_path / "other-git")
    deny = Rule(tool="*", decision="deny", input_globs={"command": "git status"})
    child_env._GIT_ANSWERS.clear()
    try:
        for spec in (BASH_SPEC, MONITOR_SPEC):
            call = {"command": "git status"}
            denied = PermissionEngine("on_write", rules=[deny]).decide(
                spec, call, cwd=str(tmp_path), env=overlay)
            assert denied.decision == "deny" and denied.source == "rule"
            # With no rule the call asks, and still nothing was started.
            asked = PermissionEngine("on_write").decide(
                spec, call, cwd=str(tmp_path), env=overlay)
            assert asked.decision == "ask"
        assert not ran.exists()
        # Without those settings the worker's own git answers.
        assert PermissionEngine("on_write").decide(
            BASH_SPEC, {"command": "git status"}, cwd=str(tmp_path)).decision == "allow"
        assert not ran.exists()
    finally:
        child_env._GIT_ANSWERS.clear()


@needs_git
@pytest.mark.skipif(os.name == "nt", reason="the stand-in git is a POSIX script")
@pytest.mark.parametrize("decision", ["deny", "ask"])
def test_a_git_in_a_folder_the_agent_can_write_is_not_started_for_a_ruled_call(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    """No settings this time: the worker's own PATH starts with a folder the
    agent can write to (``~/.local/bin`` in the Docker image), and a git was
    installed there in an earlier, approved step. A rule denies ``git status``
    or sends it to a person; rating the call starts nothing."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule
    from fluid.tools.builtins.bash import SPEC as BASH_SPEC
    from fluid.tools.builtins.monitor import SPEC as MONITOR_SPEC

    overlay, ran = _recording_git(tmp_path / "local-bin")
    monkeypatch.setenv("PATH", overlay["PATH"])
    rule = Rule(tool="*", decision=decision, input_globs={"command": "git status"})
    child_env._GIT_ANSWERS.clear()
    try:
        for spec in (BASH_SPEC, MONITOR_SPEC):
            for policy in ("on_write", "always_ask"):
                verdict = PermissionEngine(policy, rules=[rule]).decide(
                    spec, {"command": "git status"}, cwd=str(tmp_path))
                assert verdict.decision == decision
        # A policy that asks about everything starts nothing either.
        assert PermissionEngine("always_ask").decide(
            BASH_SPEC, {"command": "git status"}, cwd=str(tmp_path)).decision == "ask"
        assert not ran.exists()
        # Where read-only commands run on their own, that git is the worker's
        # git: it is asked, it is too old, and the command goes to a person.
        assert PermissionEngine("on_write").decide(
            BASH_SPEC, {"command": "git status"}, cwd=str(tmp_path)).decision == "ask"
        assert ran.read_text().strip() == "--version"
    finally:
        child_env._GIT_ANSWERS.clear()


@needs_git
@pytest.mark.skipif(os.name == "nt", reason="the stand-in git is a POSIX script")
def test_the_bash_tool_runs_the_git_the_sessions_settings_name(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    """Why such a session asks: once a command is approved, its shell does
    start with the settings ``env``, so the git it runs is the session's."""
    import asyncio

    overlay, ran = _recording_git(tmp_path / "other-git")
    ctx = ToolCtx(cwd=str(tmp_path))
    ctx.extra["settings_env"] = overlay
    outcome = asyncio.run(bash_handler({"command": "git --version"}, ctx))
    assert "git version 2.30.2" in outcome.text
    assert ran.read_text().strip() == "--version"
