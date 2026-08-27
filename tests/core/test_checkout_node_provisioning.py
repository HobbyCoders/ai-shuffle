"""Node provisioning for a fresh worktree (audit P13).

`git worktree add` never brings node_modules across — it is gitignored — so a
new checkout could not build until someone reinstalled a tree that already
existed one directory over. Models kept reinventing an ad-hoc symlink to work
around it, and one Monitor run failed outright on the missing `.svelte-kit`.

These tests cover the DECISION logic on real tmp directories. `npx` is always
stubbed: the suite must not depend on a node toolchain being installed.
"""

import os

import pytest

import app.core.checkout_manager as cm

# Captured before the autouse stub below replaces it, so the one test that
# exercises the real helper can still reach it.
_REAL_SYNC = cm._run_svelte_kit_sync


@pytest.fixture(autouse=True)
def no_real_npx(monkeypatch):
    """Record svelte-kit sync calls instead of shelling out to npx."""
    calls = []

    def fake_sync(package_dir):
        calls.append(str(package_dir))
        return True

    monkeypatch.setattr(cm, "_run_svelte_kit_sync", fake_sync)
    return calls


def _mk_package(root, rel, *, node_modules=False, svelte=False):
    """Create a package dir under ``root`` and return it."""
    path = root if rel == "." else root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    if node_modules:
        mods = path / "node_modules"
        mods.mkdir(exist_ok=True)
        (mods / "marker.txt").write_text("installed", encoding="utf-8")
    if svelte:
        (path / "svelte.config.js").write_text("export default {};", encoding="utf-8")
    return path


# ─── the link decision ───────────────────────────────────────────────────────


def test_links_root_node_modules_into_the_worktree(tmp_path):
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, ".", node_modules=True)
    _mk_package(dest, ".")

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["<root>"]
    assert not summary["errors"]
    assert (dest / "node_modules" / "marker.txt").read_text() == "installed"


def test_links_a_subdirectory_package(tmp_path):
    """The app commonly lives in frontend/, not at the repo root."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, "frontend", node_modules=True)
    _mk_package(dest, "frontend")

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["frontend"]
    assert (dest / "frontend" / "node_modules" / "marker.txt").exists()


def test_skips_when_the_worktree_has_no_package_json(tmp_path):
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    (source / "node_modules").mkdir(parents=True)
    dest.mkdir()

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary == {"linked": [], "synced": [], "skipped": [], "errors": []}
    assert not (dest / "node_modules").exists()


def test_skips_when_the_source_has_no_node_modules(tmp_path):
    """Nothing installed upstream — there is nothing to link, and that is fine."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, ".")
    _mk_package(dest, ".")

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == []
    assert summary["skipped"] == ["<root>: source has no node_modules"]
    assert not summary["errors"]


def test_skips_when_the_worktree_already_has_node_modules(tmp_path):
    """Never clobber a real install that is already there."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, ".", node_modules=True)
    _mk_package(dest, ".", node_modules=True)

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == []
    assert summary["skipped"] == ["<root>: already present"]
    # The worktree's own copy is untouched, not replaced by a link.
    assert not (dest / "node_modules").is_symlink()


def test_ignores_dot_dirs_and_nested_node_modules(tmp_path):
    """The scan is one level deep and skips the places a package.json is noise."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, "frontend", node_modules=True)
    _mk_package(dest, "frontend")
    hidden = dest / ".cache"
    hidden.mkdir()
    (hidden / "package.json").write_text("{}", encoding="utf-8")
    nested = dest / "node_modules" / "dep"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text("{}", encoding="utf-8")

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["frontend"]


# ─── svelte-kit sync ─────────────────────────────────────────────────────────


def test_runs_svelte_kit_sync_when_a_svelte_config_exists(tmp_path, no_real_npx):
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, "frontend", node_modules=True)
    _mk_package(dest, "frontend", svelte=True)

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["synced"] == ["frontend"]
    assert no_real_npx == [str(dest / "frontend")]


def test_does_not_run_sync_without_a_svelte_config(tmp_path, no_real_npx):
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, ".", node_modules=True)
    _mk_package(dest, ".")

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["synced"] == []
    assert no_real_npx == []


def test_does_not_run_sync_when_the_link_failed(tmp_path, monkeypatch, no_real_npx):
    """Sync cannot work without modules, so it is not attempted."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, ".", node_modules=True)
    _mk_package(dest, ".", svelte=True)

    def boom(src, dst):
        raise OSError("no permission to link")

    monkeypatch.setattr(cm, "_link_directory", boom)

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == []
    assert any("link failed" in e for e in summary["errors"])
    assert no_real_npx == []


def test_a_failed_sync_is_recorded_but_not_fatal(tmp_path, monkeypatch):
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, ".", node_modules=True)
    _mk_package(dest, ".", svelte=True)

    monkeypatch.setattr(cm, "_run_svelte_kit_sync", lambda d: False)

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["<root>"]
    assert summary["synced"] == []
    assert any("svelte-kit sync failed" in e for e in summary["errors"])


def test_sync_helper_never_raises_when_npx_is_missing(tmp_path, monkeypatch):
    """The real helper swallows a missing toolchain rather than propagating."""
    def boom(*a, **kw):
        raise FileNotFoundError("npx not found")

    monkeypatch.setattr(cm.subprocess, "run", boom)
    assert _REAL_SYNC(tmp_path) is False


# ─── resilience ──────────────────────────────────────────────────────────────


def test_link_failure_is_isolated_to_one_package(tmp_path, monkeypatch):
    """One bad package must not stop the others from being provisioned."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, "alpha", node_modules=True)
    _mk_package(source, "beta", node_modules=True)
    _mk_package(dest, "alpha")
    _mk_package(dest, "beta")

    real_link = cm._link_directory

    def flaky(src, dst):
        if "alpha" in str(dst):
            raise OSError("nope")
        return real_link(src, dst)

    monkeypatch.setattr(cm, "_link_directory", flaky)

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["beta"]
    assert any("alpha" in e for e in summary["errors"])


def test_never_raises_on_a_missing_destination(tmp_path):
    summary = cm.provision_node_modules(
        str(tmp_path / "src"), str(tmp_path / "does-not-exist")
    )
    assert summary["linked"] == []


def test_link_directory_makes_the_source_readable_through_dest(tmp_path):
    """The real linker, on whichever mechanism this platform provides."""
    source = tmp_path / "real"
    source.mkdir()
    (source / "pkg.txt").write_text("hello", encoding="utf-8")
    dest = tmp_path / "link"

    cm._link_directory(source, dest)

    assert (dest / "pkg.txt").read_text(encoding="utf-8") == "hello"
    assert dest.is_dir()
    if os.name != "nt":
        assert dest.is_symlink()
