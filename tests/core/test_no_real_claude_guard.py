"""No test may reach the machine's real ``claude`` CLI.

Two layers: the session guard in tests/conftest.py strips PATH directories
that hold a ``claude`` binary, and each fixture that drives the delegated leg
points the adapter's resolver at its fake. Both are checked here without
running any CLI.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import app.core.provider.harness_adapter as ha
from tests import conftest
from tests.core import test_compliance_section5 as compliance
from tests.core import test_delegated_scripted_app as delegated
from tests.core import test_native_scripted_provider_app as native


def _dir_with(tmp_path, name, filename):
    folder = tmp_path / name
    folder.mkdir()
    (folder / filename).write_text("dummy\n", encoding="utf-8")
    return folder


def test_a_claude_dir_outside_the_allowed_roots_is_stripped(tmp_path):
    real = _dir_with(tmp_path, "local-bin", "claude.exe")
    shim = _dir_with(tmp_path, "npm", "claude.cmd")
    posix = _dir_with(tmp_path, "usr-bin", "claude")
    other = _dir_with(tmp_path, "tools", "git.exe")
    path = os.pathsep.join(str(d) for d in (other, real, shim, posix))

    # tmp_path lives in the system temp dir, so only the repo is allowed here.
    stripped = conftest.strip_real_claude_dirs(path, (conftest.REPO_ROOT,))

    assert stripped.split(os.pathsep) == [str(other)]


def test_claude_dirs_inside_an_allowed_root_are_kept(tmp_path):
    fake = _dir_with(tmp_path, "bin", "claude.exe")
    path = os.pathsep.join([str(fake), ""])

    assert conftest.strip_real_claude_dirs(path, (tmp_path,)) == path


def test_the_session_guard_left_no_real_claude_on_path():
    if os.environ.get("AI_SHUFFLE_ALLOW_REAL_CLAUDE") == "1":
        return
    roots = (conftest.REPO_ROOT, tempfile.gettempdir())
    for name in ("claude", "claude.exe", "claude.cmd", "claude.bat"):
        found = shutil.which(name)
        assert found is None or conftest._is_inside(found, roots), found


def test_the_delegated_scripted_fixture_runs_its_fake(monkeypatch):
    delegated._use_fake_cli(monkeypatch)

    assert ha._resolve_claude_cmd() == [sys.executable, str(delegated.FAKE)]


def test_the_native_parity_fixture_runs_its_fake(monkeypatch):
    native._install_fake_cli(monkeypatch)

    assert ha._resolve_claude_cmd() == [sys.executable, str(native.FAKE_CLI)]


def test_the_compliance_fixture_runs_its_recorder(monkeypatch, tmp_path):
    cli = compliance._install_fake_cli(tmp_path, monkeypatch)

    if os.name == "nt":
        assert ha._resolve_claude_cmd() == [sys.executable, str(cli["recorder"])]
    else:
        # POSIX keeps the real PATH search and it lands on the fake wrapper.
        assert ha._resolve_claude_cmd() == [str(cli["script"])]
