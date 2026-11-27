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
    ".g\u200cit/config",  # HFS+ ignores this code point
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


def test_the_sessions_scratch_folder_is_the_one_its_host_names(project: Path) -> None:
    """The scratch folder is the one the session's owner resolves and passes
    in. It is never read off the path being edited: that made every
    session's scratch folder editable by every other session."""
    home = project.parent / "home"
    state = home / ".fluid" / "projects" / "-work-project"
    scratch = state / "scratch" / "ses_1"
    mine = {"extra_roots": [str(scratch)]}
    assert _ok(project, str(scratch / "brief.md"), **mine)
    assert _ok(project, str(scratch / "evidence" / "run.log"), **mine)
    # Another session's scratch folder, and this one's with nothing passed.
    assert not _ok(project, str(state / "scratch" / "ses_2" / "brief.md"), **mine)
    assert not _ok(project, str(scratch / "brief.md"))
    # Only the scratch folder: the rest of the harness's state asks.
    assert not _ok(project, str(state / "memory" / "facts.md"), **mine)
    assert not _ok(project, str(state / "scratch"), **mine)
    assert not _ok(project, str(home / ".fluid" / "settings.json"), **mine)
    assert not _ok(project, str(scratch / ".git" / "config"), **mine)
    # A scratch folder kept somewhere else entirely (FLUID_PROJECTS_DIR).
    elsewhere = project.parent / "state" / "proj" / "scratch" / "ses_1"
    assert _ok(project, str(elsewhere / "brief.md"), extra_roots=[str(elsewhere)])


def _symlink(link: Path, target: Path, *, folder: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=folder)
    except (OSError, NotImplementedError):  # Windows without the privilege
        pytest.skip("this account cannot create links")


@pytest.mark.parametrize("name", [".env", ".gitconfig", ".bashrc", "fluid.toml", ".mcp.json"])
def test_a_protected_name_that_links_to_an_ordinary_file_asks(project: Path, name: str) -> None:
    """Following the link gives ``config/local.txt``, an ordinary project
    file. The name the call wrote is still the protected one."""
    target = project / "config" / "local.txt"
    target.parent.mkdir(exist_ok=True)
    target.write_text("x")
    _symlink(project / name, target)
    assert _ok(project, "config/local.txt")
    assert not _ok(project, name)
    assert not _ok(project, str(project / name))


def test_a_protected_folder_that_links_to_an_ordinary_one_asks(project: Path) -> None:
    plain = project / "tooling"
    plain.mkdir()
    (plain / "settings.json").write_text("{}")
    _symlink(project / ".vscode", plain, folder=True)
    assert not _ok(project, ".vscode/settings.json")
    # The same file by its ordinary name is still the editor's settings.
    assert not _ok(project, "tooling/settings.json")


def test_an_ordinary_link_inside_the_project_is_still_approved(project: Path) -> None:
    (project / "docs").mkdir()
    (project / "docs" / "guide.md").write_text("x")
    _symlink(project / "manual", project / "docs", folder=True)
    assert _ok(project, "manual/guide.md")


@pytest.mark.parametrize("inside", [".vscode", ".git/hooks", ".claude", ".fluid", ".devcontainer"])
def test_a_session_started_inside_a_protected_folder_asks(project: Path, inside: str) -> None:
    """The protected name is in the working folder's own path, above every
    file the session names."""
    folder = project / inside
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "pre-commit", "notes/x.md"):
        assert not edit_stays_in_project({"file_path": name}, str(folder))
        assert not edit_stays_in_project({"file_path": str(folder / name)}, str(folder))


@pytest.mark.parametrize("name", [".vscode", ".claude", ".devcontainer"])
def test_a_session_started_in_a_protected_folder_that_is_a_link_asks(
    project: Path, name: str
) -> None:
    """The working folder resolves to an ordinary folder, so its real path
    has no protected name; the name the session was started under does."""
    (project / "plain").mkdir()
    _symlink(project / name, project / "plain", folder=True)
    started = str(project / name)
    assert not edit_stays_in_project({"file_path": "settings.json"}, started)
    assert not edit_stays_in_project({"file_path": str(project / name / "settings.json")}, started)
    assert not edit_stays_in_project({"file_path": str(project / "plain" / "settings.json")}, started)


@pytest.mark.parametrize("name", [".vscode", ".fluid", ".husky", ".devcontainer"])
def test_a_folder_a_protected_name_links_to_asks_under_any_name(project: Path, name: str) -> None:
    """``.vscode -> editor-config``: a file in ``editor-config`` is the
    editor's settings whichever name the call or the session used, and no
    name on the route says so."""
    target = project / "editor-config"
    (target / "deep").mkdir(parents=True)
    _symlink(project / name, target, folder=True)
    # From the project, by the ordinary name.
    assert not _ok(project, "editor-config/settings.json")
    assert not _ok(project, "editor-config/deep/tasks.json")
    assert not _ok(project, str(target / "settings.json"))
    # Started inside the folder itself, where the shell's physical working
    # folder never shows the link's name.
    assert not edit_stays_in_project({"file_path": "settings.json"}, str(target))
    assert not edit_stays_in_project({"file_path": "deep/tasks.json"}, str(target / "deep"))
    # The rest of the project is untouched by the link.
    assert _ok(project, "src/app.py")
    assert _ok(project, "README.md")


@pytest.mark.parametrize("name", [".vscode", ".claude", ".husky"])
def test_a_protected_name_that_links_deeper_into_the_project_asks(project: Path, name: str) -> None:
    """``.vscode -> tooling/editor``: the link is not beside the folder it
    points at, it is in a folder above it."""
    target = project / "tooling" / "editor"
    (target / "deep").mkdir(parents=True)
    _symlink(project / name, target, folder=True)
    assert not _ok(project, "tooling/editor/settings.json")
    assert not _ok(project, "tooling/editor/deep/tasks.json")
    assert not _ok(project, str(target / "settings.json"))
    # Started inside the target, in a folder below it and in the one above.
    assert not edit_stays_in_project({"file_path": "settings.json"}, str(target))
    assert not edit_stays_in_project({"file_path": "tasks.json"}, str(target / "deep"))
    assert not edit_stays_in_project({"file_path": "editor/settings.json"}, str(project / "tooling"))
    # What the link does not lead to is still project code.
    assert _ok(project, "tooling/build.py")
    assert _ok(project, "src/app.py")
    assert edit_stays_in_project({"file_path": "build.py"}, str(project / "tooling"))


def test_a_protected_name_that_links_to_its_own_folder_asks(project: Path) -> None:
    """``.vscode -> .``: every file of the folder is also under ``.vscode``."""
    (project / "pkg").mkdir()
    _symlink(project / "pkg" / ".vscode", project / "pkg", folder=True)
    assert not _ok(project, "pkg/settings.json")
    assert not _ok(project, "pkg/sub/tasks.json")
    assert _ok(project, "src/app.py")


@pytest.mark.parametrize(("link", "real"), [(".claude", ".fluid"), (".fluid", ".claude")])
def test_a_worktree_reached_through_a_linked_settings_folder_is_project_code(
    project: Path, link: str, real: str
) -> None:
    """``.claude -> .fluid``: a checkout under ``.fluid/worktrees/<name>`` is
    one under ``.claude/worktrees/<name>`` too, and both are project code."""
    checkout = project / real / "worktrees" / "feature"
    (checkout / "src").mkdir(parents=True)
    _symlink(project / link, project / real, folder=True)
    for started in (checkout, project / link / "worktrees" / "feature"):
        assert edit_stays_in_project({"file_path": "src/app.py"}, str(started))
        assert edit_stays_in_project({"file_path": "README.md"}, str(started))
        assert not edit_stays_in_project({"file_path": ".vscode/tasks.json"}, str(started))
    assert _ok(project, f"{real}/worktrees/feature/src/app.py")
    assert _ok(project, f"{link}/worktrees/feature/src/app.py")
    # The settings folder itself still asks under both names.
    assert not _ok(project, f"{real}/settings.json")
    assert not _ok(project, f"{link}/settings.json")
    assert not _ok(project, f"{real}/worktrees/notes.md")


def test_a_linked_folder_is_recognised_when_its_path_is_spelled_differently(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a volume that ignores letter case the link can resolve to a
    spelling the edited path does not share. The folder is the same one."""
    target = project / "tooling"
    target.mkdir()
    link = project / ".vscode"
    _symlink(link, target, folder=True)
    resolve = os.path.realpath

    def other_spelling(path, *args, **kwargs):
        if os.fspath(path) == str(link):
            return str(project / "TOOLING")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", other_spelling)
    assert not _ok(project, "tooling/settings.json")
    assert _ok(project, "src/app.py")


def test_a_dangling_protected_link_asks_for_the_folder_it_names(project: Path) -> None:
    """The link's folder does not exist yet: Write would create it, and the
    file would then sit under the protected name."""
    _symlink(project / ".vscode", project / "later", folder=True)
    assert not _ok(project, "later/tasks.json")
    assert _ok(project, "other/tasks.json")


def test_a_protected_link_to_another_folder_leaves_its_neighbours_alone(project: Path) -> None:
    (project / "a").mkdir()
    (project / "b").mkdir()
    _symlink(project / ".vscode", project / "a", folder=True)
    assert not _ok(project, "a/x.json")
    assert _ok(project, "b/x.json")


def test_a_link_inside_a_host_added_folder_asks(project: Path, tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    (uploads / "cfg").mkdir(parents=True)
    (uploads / "plain").mkdir()
    _symlink(uploads / ".vscode", uploads / "cfg", folder=True)
    roots = [str(uploads)]
    assert not edit_stays_in_project(
        {"file_path": str(uploads / "cfg" / "tasks.json")}, str(project), roots)
    assert edit_stays_in_project(
        {"file_path": str(uploads / "plain" / "notes.md")}, str(project), roots)


@pytest.mark.parametrize("parent", [".fluid", ".claude"])
def test_a_session_started_in_a_worktree_checkout_edits_its_code(project: Path, parent: str) -> None:
    checkout = project / parent / "worktrees" / "feat-x"
    (checkout / "src").mkdir(parents=True)
    assert edit_stays_in_project({"file_path": "src/app.py"}, str(checkout))
    assert edit_stays_in_project({"file_path": "README.md"}, str(checkout))
    assert not edit_stays_in_project({"file_path": ".git/config"}, str(checkout))
    assert not edit_stays_in_project({"file_path": ".vscode/tasks.json"}, str(checkout))
    # The worktrees folder itself is not a checkout.
    assert not edit_stays_in_project(
        {"file_path": "x.py"}, str(project / parent / "worktrees"))


@pytest.mark.parametrize("name", ["fluid.toml", "FLUID.TOML", "sub/fluid.toml", "fluid.toml."])
def test_the_top_level_harness_config_asks(project: Path, name: str) -> None:
    """``fluid.toml`` is harness configuration the CLI loads from a project
    that has no ``.fluid/config.toml``."""
    assert not _ok(project, name)


def test_every_file_a_request_lists_is_judged(project: Path) -> None:
    """A delegated agent can report one edit over several files. Approval
    of the first is not approval of the rest."""
    inside, other = "src/app.py", "src/util.py"
    outside = str(project.parent / "elsewhere" / "x.py")
    cwd = str(project)
    assert edit_stays_in_project({"file_path": inside, "file_paths": [inside, other]}, cwd)
    assert edit_stays_in_project({"file_paths": [inside, other]}, cwd)
    assert not edit_stays_in_project({"file_path": inside, "file_paths": [inside, outside]}, cwd)
    assert not edit_stays_in_project(
        {"file_path": inside, "file_paths": [inside, ".git/hooks/pre-commit"]}, cwd)
    # A list that cannot be read as paths asks.
    for listed in ([], "src/app.py", [inside, None], [inside, 3], [inside, ""], {"a": 1}):
        assert not edit_stays_in_project({"file_path": inside, "file_paths": listed}, cwd)


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
