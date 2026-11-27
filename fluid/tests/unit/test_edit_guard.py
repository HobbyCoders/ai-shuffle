"""Which edits Accept Edits approves by itself (``permissions.edit_guard``).

The mode used to approve an edit-class tool on its name: a write to
``~/.bashrc``, ``.git/hooks/pre-commit`` or the session's own settings ran
with no prompt. These pin the path rule: inside the project and not a file
that configures a tool, starts programs or holds credentials. Every name is
judged the way the most forgiving filesystem reads it, so the answer is the
same on Linux, macOS and Windows.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fluid.permissions.edit_guard import edit_stays_in_project
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.modes import ACCEPT_EDITS_TOOLS, mode_rules
from fluid.tools.registry import ToolSpec


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project folder beside a home folder, both real."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    folder = tmp_path / "project"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "app.py").write_text("x = 1\n")
    return folder


def _ok(project: Path, path: str, **kwargs) -> bool:
    return edit_stays_in_project({"file_path": path}, str(project), **kwargs)


# ─── the project's own files ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "src/app.py",
    "README.md",
    "new/folder/file.txt",  # Write creates it
    "./src/../src/app.py",
    ".github/workflows/ci.yml",
    ".gitignore",
    ".env.example",  # a template: no values in it
    "docs/.hidden/notes.md",
])
def test_a_file_of_the_project_is_approved(project: Path, path: str) -> None:
    assert _ok(project, path)
    assert _ok(project, str(project / path))


def test_the_notebook_field_is_read_too(project: Path) -> None:
    assert edit_stays_in_project({"notebook_path": "a.ipynb"}, str(project))
    assert not edit_stays_in_project({"notebook_path": "../a.ipynb"}, str(project))
    # Every path the call names has to pass.
    assert not edit_stays_in_project(
        {"file_path": "a.py", "notebook_path": ".git/config"}, str(project)
    )


# ─── outside the project ─────────────────────────────────────────────────────


def test_a_file_outside_the_project_asks(project: Path) -> None:
    home = project.parent / "home"
    for path in (
        "../other/file.py",
        "src/../../other/file.py",
        str(home / ".bashrc"),
        str(home / "notes.txt"),
        "/etc/hosts",
        str(project),  # the folder itself
        str(project.parent),
    ):
        assert not _ok(project, path), path
    # The file tools do not expand ``~``, so this is a folder named ``~``
    # inside the project; the startup file's name asks wherever it is.
    assert not _ok(project, "~/.bashrc")


def test_a_link_out_of_the_project_asks(project: Path) -> None:
    outside = project.parent / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("x")
    try:
        (project / "link.txt").symlink_to(outside / "target.txt")
        (project / "linked").symlink_to(outside, target_is_directory=True)
        (project / "to-git").symlink_to(project / ".git", target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    assert not _ok(project, "link.txt")
    assert not _ok(project, "linked/new.txt")
    # A link whose target is a protected folder inside the project.
    assert not _ok(project, "to-git/config")


def test_a_second_name_for_a_file_elsewhere_asks(project: Path) -> None:
    """A hard link shares the file with a name the path does not show."""
    outside = project.parent / "outside.txt"
    outside.write_text("x")
    try:
        os.link(outside, project / "inside.txt")
    except OSError:
        pytest.skip("this filesystem has no hard links")
    assert not _ok(project, "inside.txt")


# ─── files that configure a tool, start programs or hold credentials ─────────


@pytest.mark.parametrize("path", [
    ".git/hooks/pre-commit",
    ".git/config",
    "sub/.git/config",
    ".hg/hgrc",
    ".fluid/config.toml",
    ".fluid/settings.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/config.toml",
    ".vscode/tasks.json",
    ".idea/workspace.xml",
    ".devcontainer/devcontainer.json",
    ".husky/pre-commit",
    ".mcp.json",
    ".envrc",
    ".bashrc",
    ".zshenv",
    ".gitconfig",
    ".gitmodules",
    ".gitattributes",
    "sub/.gitattributes",
    "Microsoft.PowerShell_profile.ps1",
    ".env",
    ".env.local",
    "deploy/id_rsa",
    "certs/server.pem",
    ".npmrc",
])
def test_a_protected_file_asks(project: Path, path: str) -> None:
    assert not _ok(project, path)
    assert not _ok(project, str(project / path))


@pytest.mark.parametrize("path", [
    ".GIT/config",  # Windows, and a default macOS volume
    ".Git/hooks/pre-commit",
    ".git./config",  # Windows drops a trailing dot
    ".git /config",  # and a trailing space
    ".git::$INDEX_ALLOCATION/config",  # NTFS stream syntax
    "GIT~1/config",  # an 8.3 short name
    ".g‌it/config",  # HFS+ ignores this code point
    ".CLAUDE/settings.json",
    ".Bashrc",
    ".ENV",
    ".MCP.JSON",
    ".vscode./tasks.json",
])
def test_another_spelling_of_a_protected_name_asks(project: Path, path: str) -> None:
    """Names that open the protected file on some platform ask on all."""
    assert not _ok(project, path)


def test_a_path_that_only_passes_before_it_is_trimmed_asks(project: Path) -> None:
    """Some callers trim the value before opening it."""
    assert not _ok(project, " .bashrc")
    assert not _ok(project, ".git/config ")


def test_a_checkout_under_the_agents_worktree_folder_is_project_code(project: Path) -> None:
    assert _ok(project, ".fluid/worktrees/feat-x/src/app.py")
    assert _ok(project, ".claude/worktrees/feat-x/README.md")
    # Its own protected files stay protected.
    assert not _ok(project, ".fluid/worktrees/feat-x/.git/config")
    assert not _ok(project, ".fluid/worktrees/feat-x/.claude/settings.json")
    assert not _ok(project, ".fluid/worktrees/feat-x/.env")
    # And the folder's own entries are not a checkout's files.
    assert not _ok(project, ".fluid/worktrees/feat-x")
    assert not _ok(project, ".fluid/worktrees/hook.sh")
    assert not _ok(project, ".fluid/other/worktrees/x/a.py")


# ─── when the file cannot be placed ──────────────────────────────────────────


@pytest.mark.parametrize("tool_input", [
    None, "src/app.py", {}, {"file_path": ""}, {"file_path": None},
    {"file_path": 3}, {"file_path": ["a.py"]}, {"file_path": "a\x00.py"},
    {"path": "src/app.py"},  # not a field an edit tool names its file in
])
def test_an_input_without_a_usable_path_asks(project: Path, tool_input) -> None:
    assert not edit_stays_in_project(tool_input, str(project))


@pytest.mark.parametrize("cwd", [None, "", "   ", 3])
def test_an_unknown_working_folder_asks(project: Path, cwd) -> None:
    assert not edit_stays_in_project({"file_path": str(project / "src" / "app.py")}, cwd)


def test_a_filesystem_root_is_not_a_project(project: Path) -> None:
    root = os.path.abspath(os.sep)
    assert not edit_stays_in_project({"file_path": str(project / "src" / "app.py")}, root)


# ─── folders the host added, and the session's scratch folder ────────────────


def test_a_folder_the_host_added_counts_as_the_projects(project: Path) -> None:
    uploads = project.parent / "uploads"
    uploads.mkdir()
    target = str(uploads / "notes.md")
    assert not _ok(project, target)
    assert _ok(project, target, extra_roots=[str(uploads)])
    assert _ok(project, target, extra_roots=lambda: [str(uploads)])
    # The same names are protected there.
    assert not _ok(project, str(uploads / ".git" / "config"), extra_roots=[str(uploads)])
    assert not _ok(project, str(uploads / ".env"), extra_roots=[str(uploads)])
    # A host may hold its folders as ``Path`` objects.
    assert _ok(project, target, extra_roots=[uploads])
    assert edit_stays_in_project({"file_path": "src/app.py"}, project)
    # Junk in the list is skipped, not trusted.
    assert not _ok(project, target, extra_roots=[None, "", 3, os.path.abspath(os.sep)])


def test_the_extra_folders_are_only_asked_for_outside_the_project(project: Path) -> None:
    calls: list[int] = []

    def roots():
        calls.append(1)
        return []

    assert _ok(project, "src/app.py", extra_roots=roots)
    assert calls == []
    assert not _ok(project, "/etc/hosts", extra_roots=roots)
    assert calls == [1]


def test_the_sessions_scratch_folder_is_its_own(project: Path) -> None:
    home = project.parent / "home"
    scratch = home / ".fluid" / "projects" / "-work-project" / "scratch" / "ses_1"
    assert _ok(project, str(scratch / "brief.md"))
    assert _ok(project, str(scratch / "evidence" / "run.log"))
    # Only the scratch folder: the rest of the harness's state asks.
    state = home / ".fluid" / "projects" / "-work-project"
    assert not _ok(project, str(state / "memory" / "facts.md"))
    assert not _ok(project, str(state / "scratch"))
    assert not _ok(project, str(home / ".fluid" / "settings.json"))
    assert not _ok(project, str(scratch / ".git" / "config"))


def test_a_session_working_in_the_home_folder_does_not_get_its_dot_folders(
    project: Path,
) -> None:
    home = project.parent / "home"
    assert edit_stays_in_project({"file_path": "notes.txt"}, str(home))
    assert edit_stays_in_project({"file_path": "code/app.py"}, str(home))
    for path in (".bashrc", ".ssh/authorized_keys", ".config/fish/config.fish",
                 ".local/bin/git", ".anything/at-all", ".Cache/x"):
        assert not edit_stays_in_project({"file_path": path}, str(home)), path


# ─── through the engine, as the mode installs it ─────────────────────────────


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", input_schema={}, risk="write")


def _field(name: str) -> str:
    return "notebook_path" if name == "NotebookEdit" else "file_path"


@pytest.mark.parametrize("name", sorted(ACCEPT_EDITS_TOOLS))
def test_the_mode_approves_a_project_edit_and_asks_for_the_rest(
    project: Path, name: str
) -> None:
    engine = PermissionEngine("on_write")
    engine.set_mode_rules(mode_rules("acceptEdits"))
    cwd = str(project)
    home = project.parent / "home"

    def verdict(path: str) -> str:
        return engine.evaluate(_spec(name), {_field(name): path}, cwd=cwd)

    assert verdict("src/app.py") == "allow"
    # What the name-only rule let through.
    assert verdict(str(home / ".bashrc")) == "ask"
    assert verdict(".git/hooks/pre-commit") == "ask"
    assert verdict(".claude/settings.json") == "ask"
    assert verdict(".fluid/config.toml") == "ask"
    assert verdict("../elsewhere.txt") == "ask"
    # No working folder, no approval.
    assert engine.evaluate(_spec(name), {_field(name): "src/app.py"}) == "ask"
