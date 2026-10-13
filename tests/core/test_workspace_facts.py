"""The "# Workspace facts" bootstrap card (round-9 audit, item 2).

Twelve-plus sessions each re-discovered the same environment facts by probing:
node_modules missing in a fresh worktree, which interpreter has pytest, that
the agent browser lives on another host, that the app's own UI rejects
proxied requests, that vite.config.ts does not exist. The card states the ones
that are TRUE for this worker, once per session, and never varies per turn.
"""
from __future__ import annotations

import sys

import pytest

from app.core import environment as env


@pytest.fixture(autouse=True)
def _fresh_pytest_probe(monkeypatch):
    """Each test pins the interpreter answer explicitly; never probe PATH."""
    monkeypatch.setattr(env, "_PYTEST_PROBE", {})
    monkeypatch.setattr(env, "_interpreter_has_pytest", lambda exe: False)
    import shutil
    original = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "pytest" else original(name))


def _shuffle_checkout(tmp_path, *, node_modules=False, vite_ts=False):
    root = tmp_path / "ai-shuffle"
    (root / "app").mkdir(parents=True)
    (root / "app" / "preview.py").write_text("# preview")
    (root / "fluid").mkdir()
    (root / "tests").mkdir()
    (root / "tests" / "pg_shim.py").write_text("# shim")
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text("{}")
    (root / "frontend" / "package-lock.json").write_text("{}")
    (root / "frontend" / "vite.config.js").write_text("export default {}")
    if vite_ts:
        (root / "frontend" / "vite.config.ts").write_text("export default {}")
    if node_modules:
        (root / "frontend" / "node_modules" / ".bin").mkdir(parents=True)
    return root


def test_card_names_the_facts_that_are_true_here(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "_PYTEST_PROBE", {"value": sys.executable})
    root = _shuffle_checkout(tmp_path)
    card = env.workspace_facts_card(
        str(root), browser_host="10.0.0.30", app_port=8000,
        preview_url="http://10.0.0.30:8002/api/v1/auth/preview-access?token=t&next=/",
    )
    assert card.startswith(env.WORKSPACE_FACTS_HEADER)
    # node_modules missing → the npm ci instruction, with the junction rule.
    assert "node_modules MISSING in frontend" in card and "npm ci" in card
    assert "never junction" in card
    # pytest interpreter + the PYTHONPATH=fluid invocation.
    assert sys.executable in card and "PYTHONPATH=fluid" in card
    # The browser lives elsewhere: bind 0.0.0.0, use the LAN address.
    assert "0.0.0.0" in card and "http://10.0.0.30:<port>" in card
    # The app's own guard and the one-time preview URL.
    assert ":8000" in card and "rejects proxied/forwarded" in card
    assert "preview-access?token=t" in card
    # The link is on the preview-only port; the owner's port is named as refused.
    assert "http://10.0.0.30:8002/" in card and "refused :8000" in card
    # Branch preview command, the vite config name, the pg_shim caveat.
    assert "python -m app.preview stamp" in card and "AI_SHUFFLE_PREVIEW=1" in card
    assert "frontend/vite.config.js" in card and "no vite.config.ts" in card
    assert "pg_shim" in card and "SET" in card


def test_card_drops_facts_that_are_not_true(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "_PYTEST_PROBE", {"value": None})
    root = _shuffle_checkout(tmp_path, node_modules=True, vite_ts=True)
    card = env.workspace_facts_card(str(root), browser_host=None, app_port=8000)
    assert "node_modules installed in frontend" in card
    assert "MISSING" not in card
    assert "no interpreter or executable with pytest was verified" in card
    assert "no vite.config.ts" not in card
    assert "preview-access" not in card  # no URL was minted
    assert "cannot open the running UI" in card
    assert "LAN address" in card  # browser rule still stated without a host
    assert "may run on a different host" in card


def test_generic_repo_gets_only_generic_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "_PYTEST_PROBE", {"value": sys.executable})
    root = tmp_path / "other"
    root.mkdir()
    (root / "package.json").write_text("{}")
    card = env.workspace_facts_card(str(root), browser_host="172.17.0.9", app_port=8000)
    assert "node_modules MISSING in ." in card
    assert "PYTHONPATH=fluid" not in card
    assert "app.preview" not in card and "pg_shim" not in card
    assert "http://172.17.0.9:<port>" in card


def test_card_is_byte_stable_and_bounded(tmp_path, monkeypatch):
    """It lives in the cached prompt prefix: two renders in one session must
    be identical, and it must stay under the ~600-token ceiling."""
    monkeypatch.setattr(env, "_PYTEST_PROBE", {"value": sys.executable})
    root = _shuffle_checkout(tmp_path)
    kwargs = dict(browser_host="10.0.0.30", app_port=8000,
                  preview_url="http://10.0.0.30:8002/api/v1/auth/preview-access?token=t&next=/")
    first = env.workspace_facts_card(str(root), **kwargs)
    second = env.workspace_facts_card(str(root), **kwargs)
    assert first == second
    assert len(first) <= env.WORKSPACE_FACTS_MAX_CHARS


def test_pytest_probe_runs_once_per_process(monkeypatch):
    calls = []

    def _probe(exe):
        calls.append(exe)
        return True

    monkeypatch.setattr(env, "_interpreter_has_pytest", _probe)
    monkeypatch.setattr(env, "_PYTEST_PROBE", {})
    first = env.pytest_interpreter()
    second = env.pytest_interpreter()
    assert first == second
    # Either this interpreter has pytest (no subprocess at all) or PATH was
    # probed exactly once; never once per call.
    assert len(calls) <= 1 or first == sys.executable
    calls_after_first = len(calls)
    env.pytest_interpreter()
    assert len(calls) == calls_after_first


def test_card_never_raises(monkeypatch):
    monkeypatch.setattr(env, "_node_dependency_facts", lambda cwd: (_ for _ in ()).throw(OSError("boom")))
    card = env.workspace_facts_card("/nowhere", browser_host="10.0.0.1", app_port=8000)
    assert card.startswith(env.WORKSPACE_FACTS_HEADER)
