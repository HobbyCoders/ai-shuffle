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
    assert "python -m app.preview run --port 18080" in card
    assert "--host 0.0.0.0 --browser-host <worker-IP>" in card
    assert "printed access" in card and "named port requires its token" in card
    assert "Do not daemonize" in card and "TTL expires" in card
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


# ─── Fitting the ceiling: whole facts only ───────────────────────────────────
#
# A real session on a container worker got a card cut mid-sentence at
# "- Frontend: the Vite config is `…": the old code sliced the text at the
# ceiling. These pin the realistic worst case (AI Shuffle checkout, node
# dependencies missing in some packages, a long interpreter path, a remote
# browser host and a preview link with a ~150-character token) and the
# whole-line rule for any ceiling.

_SAMPLE_TOKEN = "v1." + "Q" * 48 + ".1700000000." + "0" * 64
_SAMPLE_TOKEN += "z" * (150 - len(_SAMPLE_TOKEN))
_WORST_URL = f"http://10.0.0.30:8002/api/v1/auth/preview-access?token={_SAMPLE_TOKEN}&next=/"
_LONG_PYTHON = "/opt/app-updates/versions/" + "0" * 40 + "/venv/bin/python"


def _worst_case_checkout(tmp_path):
    root = _shuffle_checkout(tmp_path)
    for pkg in ("browser-mcp", "tools", "desktop"):
        (root / pkg).mkdir()
        (root / pkg / "package.json").write_text("{}")
        (root / pkg / "package-lock.json").write_text("{}")
    (root / "tools" / "node_modules" / ".bin").mkdir(parents=True)
    (root / "mobile").mkdir()
    (root / "mobile" / "package.json").write_text("{}")
    return root


def _render_worst(root, monkeypatch, ceiling=None):
    monkeypatch.setattr(env, "_PYTEST_PROBE", {"value": _LONG_PYTHON})
    if ceiling is not None:
        monkeypatch.setattr(env, "WORKSPACE_FACTS_MAX_CHARS", ceiling)
    return env.workspace_facts_card(
        str(root), browser_host="10.0.0.30", app_port=8000, preview_url=_WORST_URL,
    )


def _fact_lines(card):
    """The card's lines after the header and the baseline sentence."""
    return card.split("\n")[2:]


def test_worst_case_card_fits_whole_without_dropping_anything(tmp_path, monkeypatch):
    root = _worst_case_checkout(tmp_path)
    card = _render_worst(root, monkeypatch)
    assert len(card) <= env.WORKSPACE_FACTS_MAX_CHARS
    assert "Left out" not in card and not card.endswith("…")
    lines = _fact_lines(card)
    assert all(line.startswith("- ") for line in lines)
    labels = [line[2:].split(":", 1)[0] for line in lines]
    assert labels == ["Node", "Mobile", "Node", "Python", "Agent browser", "This app",
                      "Branch UI preview", "Frontend", "Tests"]
    link_line = next(line for line in lines if line.startswith("- This app:"))
    assert _WORST_URL in link_line
    # The interpreter path appears once, inside the copyable command.
    assert card.count(_LONG_PYTHON) == 1
    assert f"PYTHONPATH=fluid {_LONG_PYTHON} -m pytest" in card


@pytest.mark.parametrize("ceiling", range(250, 2500, 41))
def test_over_ceiling_card_drops_whole_facts_never_part_of_one(tmp_path, monkeypatch, ceiling):
    root = _worst_case_checkout(tmp_path)
    full = _fact_lines(_render_worst(root, monkeypatch, ceiling=10**6))
    card = _render_worst(root, monkeypatch, ceiling=ceiling)
    assert len(card) <= ceiling
    assert card.startswith(env.WORKSPACE_FACTS_HEADER + "\n")
    lines = _fact_lines(card)
    kept = [line for line in lines if line.startswith("- ")]
    # Every kept line is one of the produced facts, whole, in the same order.
    assert kept == [line for line in full if line in kept]
    assert set(kept) <= set(full)
    rest = lines[len(kept):]
    if len(kept) < len(full):
        assert len(rest) == 1 and rest[0].startswith("Left out to fit the size limit")
        assert lines[-1] == rest[0]
    else:
        assert rest == []
    # The link is whole or absent: never a cut token.
    if _SAMPLE_TOKEN[:20] in card:
        assert _WORST_URL in card


def test_over_ceiling_card_leaves_out_lowest_ranked_facts_first(tmp_path, monkeypatch):
    root = _worst_case_checkout(tmp_path)
    full = _render_worst(root, monkeypatch, ceiling=10**6)
    branch = next(line for line in _fact_lines(full) if line.startswith("- Branch UI preview:"))
    # Room for everything but the branch-preview recipe: the lower-ranked
    # facts go first, and the link, node, browser and Python facts stay.
    card = _render_worst(root, monkeypatch, ceiling=len(full) - len(branch) + 40)
    assert _WORST_URL in card
    for kept in ("- Node: node_modules MISSING", "- Mobile:", "- Agent browser:", "- Python:"):
        assert kept in card
    assert "- Branch UI preview:" not in card
    note = card.split("\n")[-1]
    assert note.startswith("Left out to fit the size limit")
    assert "Branch UI preview" in note


def test_link_line_is_never_partial_even_when_it_cannot_fit(tmp_path, monkeypatch):
    root = _worst_case_checkout(tmp_path)
    card = _render_worst(root, monkeypatch, ceiling=500)
    assert len(card) <= 500
    assert "token=" not in card and "preview-access" not in card
    assert "This app" in card.split("\n")[-1]  # named as left out
