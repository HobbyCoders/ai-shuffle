"""Shared test setup.

Every test runs with HOME (and USERPROFILE, which is what ``expanduser`` reads
on Windows) pointed at an empty tmp directory. Discovery is filesystem-native
by design — a NativeLoop built with no explicit cwd reads ``~/.claude`` for
settings (CE4) and enabled plugins (CE5), and a plugin's MCP servers would then
really be spawned from a unit test. Tests that need a Claude setup build one
under their own tmp home and re-point HOME themselves; their ``monkeypatch``
runs after this fixture, so it wins.

The cached default skills root and its CLI aliases also point into that home.
Session guards block writes to the real Fluid skills store before they reach
disk and keep the machine's real ``claude`` CLI off PATH (see
``strip_real_claude_dirs``).
"""

import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

# The fluid project root (fluid/tests/conftest.py sits one level below it).
REPO_ROOT = Path(__file__).resolve().parents[1]
# Capture this before any fixture redirects HOME.
_REAL_SKILLS_ROOT = Path.home() / ".fluid" / "skills"


@contextmanager
def guard_skills_writes(root):
    """Fail before a filesystem mutation reaches the protected skills store."""
    # Use the existing os.path helper, not Path(): shell tests mock os.name,
    # which would otherwise select WindowsPath on a POSIX host (or vice versa).
    root = os.path.realpath(root)
    active = True

    def guard(event, args):
        if not active:
            return
        if event == "open":
            if not args[2] & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
                return
            paths = args[:1]
        elif event in {
            "os.mkdir", "os.remove", "os.rmdir", "os.truncate", "os.chmod",
            "os.chown", "os.utime",
        }:
            paths = args[:1]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            paths = args[:2]
        else:
            return
        for path in paths:
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = os.path.realpath(os.fsdecode(path))
                if _is_inside(resolved, (root,)):
                    # Failed is a BaseException: best-effort memory saves catch
                    # Exception, which must not silently swallow this guard.
                    pytest.fail(f"Blocked write to real Fluid skills store: {resolved}")

    sys.addaudithook(guard)
    try:
        yield
    finally:
        # Audit hooks cannot be removed; leave this one inert after the run.
        active = False


@pytest.fixture(autouse=True, scope="session")
def _no_real_skills_writes():
    with guard_skills_writes(_REAL_SKILLS_ROOT):
        yield


# ─── No real ``claude`` CLI in tests ────────────────────────────────────────
# A test that forgets to point the delegated leg at a fake would otherwise run
# the machine's real ``claude`` binary, which spends the developer's Claude
# subscription and executes model-chosen commands. On Windows this happened
# silently: ``shutil.which`` skips an extension-less shim, so the search walked
# on to ``~/.local/bin/claude.exe``. The session guard below removes every PATH
# directory that holds a ``claude`` binary, except directories inside the repo
# or the system temp dir (where tests install their own fakes). A forgotten
# patch then fails with ClaudeCliMissingError instead of billing anyone.
# Set AI_SHUFFLE_ALLOW_REAL_CLAUDE=1 to keep PATH as it is.

_CLAUDE_BINARY_NAMES = ("claude", "claude.exe", "claude.cmd", "claude.bat")


def _path_forms(path: str) -> set:
    forms = set()
    for form in (os.path.abspath(path), os.path.realpath(path)):
        forms.add(os.path.normcase(form))
    return forms


def _is_inside(path: str, roots) -> bool:
    for form in _path_forms(path):
        for root in roots:
            for root_form in _path_forms(str(root)):
                try:
                    if os.path.commonpath([form, root_form]) == root_form:
                        return True
                except ValueError:  # different drives on Windows
                    continue
    return False


def strip_real_claude_dirs(path_value: str, allowed_roots) -> str:
    """``path_value`` without the entries that hold a ``claude`` binary.

    Entries under one of ``allowed_roots`` are kept, as is every entry with no
    ``claude``, ``claude.exe``, ``claude.cmd`` or ``claude.bat`` file in it.
    """
    kept = []
    for entry in path_value.split(os.pathsep):
        if entry and not _is_inside(entry, allowed_roots) and any(
            os.path.isfile(os.path.join(entry, name)) for name in _CLAUDE_BINARY_NAMES
        ):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


@pytest.fixture(autouse=True, scope="session")
def _no_real_claude_on_path():
    """Keep the machine's real ``claude`` CLI off PATH for the whole run."""
    if os.environ.get("AI_SHUFFLE_ALLOW_REAL_CLAUDE") == "1":
        yield
        return
    roots = (REPO_ROOT, tempfile.gettempdir())
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", strip_real_claude_dirs(os.environ.get("PATH", ""), roots))
        yield


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory, monkeypatch):
    from fluid.cli import evolve, replay
    from fluid.codemode import skills

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # HOME alone is too late: collection imports skills.py, which caches this
    # path, and evolve/replay retain their own imported aliases. Give all three
    # the same per-test root; later imports also see the patched source value.
    for module in (skills, evolve, replay):
        monkeypatch.setattr(module, "DEFAULT_SKILLS_ROOT", home / ".fluid" / "skills")
    return home


@pytest.fixture
def requires_symlinks(tmp_path):
    """Skip only link-dependent tests when this host cannot create symlinks."""
    target = tmp_path / "symlink-capability-target"
    target.write_text("probe", encoding="utf-8")
    try:
        (tmp_path / "symlink-capability-link").symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires Developer Mode or elevation")
        raise


@pytest.fixture(params=["plain", "probe's directory", r"backslash\Users"])
def codemode_marker(tmp_path, request):
    """An executable escape payload, including a path requiring Python quoting.

    Use a shell builtin on both platforms, not POSIX-only touch. The generated
    cells must repr() this command separately from shell argument quoting.
    """
    directory = tmp_path / request.param
    directory.mkdir(parents=True)
    marker = directory / "escaped"
    quoted = subprocess.list2cmdline([str(marker)]) if os.name == "nt" else shlex.quote(str(marker))
    return marker, f"echo escaped > {quoted}"


@pytest.fixture(autouse=True)
def _no_builtin_skills(tmp_path_factory, monkeypatch):
    """Keep the package's built-in skills (``fluid/builtin_skills``) out of the
    skill listings tests assert on. Tests of a built-in skill point
    ``builtin_skills_dir`` back at the package folder themselves."""
    empty = tmp_path_factory.mktemp("no-builtin-skills")
    monkeypatch.setattr("fluid.context.instructions.builtin_skills_dir", lambda: empty)
