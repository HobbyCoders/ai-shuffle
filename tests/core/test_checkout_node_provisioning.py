"""Node provisioning for a fresh worktree (audit P13).

`git worktree add` never brings node_modules across — it is gitignored — so a
new checkout could not build until someone reinstalled a tree that already
existed one directory over. Models kept reinventing an ad-hoc symlink to work
around it, and one Monitor run failed outright on the missing `.svelte-kit`.

These tests cover the DECISION logic on real tmp directories. `npx` is always
stubbed: the suite must not depend on a node toolchain being installed.
"""

import json
import os

import pytest

import app.core.checkout_manager as cm

# Captured before the autouse stubs below replace them, so the tests that
# exercise the real helpers can still reach them.
_REAL_SYNC = cm._run_svelte_kit_sync
_REAL_NPM_INSTALL = cm._run_npm_install


@pytest.fixture(autouse=True)
def no_real_npx(monkeypatch):
    """Record svelte-kit sync calls instead of shelling out to npx."""
    calls = []

    def fake_sync(package_dir):
        calls.append(str(package_dir))
        return True

    monkeypatch.setattr(cm, "_run_svelte_kit_sync", fake_sync)
    return calls


@pytest.fixture(autouse=True)
def no_real_npm(monkeypatch):
    """Record npm installs instead of running one. Creates node_modules."""
    calls = []

    def fake_install(package_dir):
        calls.append(str(package_dir))
        (package_dir / "node_modules").mkdir(exist_ok=True)
        return True, "npm ci --no-audit --no-fund"

    monkeypatch.setattr(cm, "_run_npm_install", fake_install)
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

    assert summary == {
        "linked": [],
        "installed": [],
        "synced": [],
        "skipped": [],
        "errors": [],
    }
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


# --- freshness: link only what the branch's lockfile actually wants ---------
#
# Linking a source tree the branch has outgrown is worse than linking nothing:
# audited sessions lost minutes to "Cannot optimize dependency" and phantom
# svelte-check errors that read like code bugs. These cover the decision.


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _lockfile(**versions):
    packages = {"": {"name": "app"}}
    for name, version in versions.items():
        packages[f"node_modules/{name}"] = {"version": version}
    return {"lockfileVersion": 3, "packages": packages}


def _seed_pair(tmp_path, *, source_lock=None, installed=None, dest_lock=None):
    """A source checkout with an install, and a worktree, both at ``frontend/``."""
    source = tmp_path / "src"
    dest = tmp_path / "wt"
    _mk_package(source, "frontend", node_modules=True)
    _mk_package(dest, "frontend")
    if source_lock is not None:
        _write_json(source / "frontend" / "package-lock.json", source_lock)
    if installed is not None:
        _write_json(
            source / "frontend" / "node_modules" / ".package-lock.json", installed
        )
    if dest_lock is not None:
        _write_json(dest / "frontend" / "package-lock.json", dest_lock)
    return source, dest


def test_links_when_the_installed_tree_satisfies_the_branch_lockfile(
    tmp_path, no_real_npm
):
    source, dest = _seed_pair(
        tmp_path,
        installed=_lockfile(tailwindcss="4.0.0"),
        dest_lock=_lockfile(tailwindcss="4.0.0"),
    )

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["frontend"]
    assert summary["installed"] == []
    assert no_real_npm == []


def test_installs_when_the_branch_wants_another_version(tmp_path, no_real_npm):
    """The Tailwind 3-vs-4 case: linking here breaks every frontend command."""
    source, dest = _seed_pair(
        tmp_path,
        installed=_lockfile(tailwindcss="3.4.1"),
        dest_lock=_lockfile(tailwindcss="4.0.0"),
    )

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == []
    assert no_real_npm == [str(dest / "frontend")]
    assert len(summary["installed"]) == 1
    assert summary["installed"][0].startswith("frontend: npm ci")
    assert "tailwindcss" in summary["installed"][0]
    assert not summary["errors"]
    # Installed in the worktree's own dir, not linked out to the source.
    assert not (dest / "frontend" / "node_modules").is_symlink()


def test_installs_when_the_branch_adds_a_package_the_source_lacks(
    tmp_path, no_real_npm
):
    """A branch-only dependency (the missing @tailwindcss/vite case)."""
    source, dest = _seed_pair(
        tmp_path,
        installed=_lockfile(tailwindcss="4.0.0"),
        dest_lock=_lockfile(tailwindcss="4.0.0", vite_plugin="4.0.0"),
    )

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == []
    assert no_real_npm == [str(dest / "frontend")]


def test_links_when_there_is_no_lockfile_to_compare(tmp_path, no_real_npm):
    """Unknowable is not a reason to spend minutes installing."""
    source, dest = _seed_pair(tmp_path, installed=_lockfile(tailwindcss="3.4.1"))

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["frontend"]
    assert no_real_npm == []


def test_falls_back_to_comparing_lockfiles_without_an_install_record(
    tmp_path, no_real_npm
):
    """npm 6, or a hand-assembled tree: the two lockfiles still decide."""
    source, dest = _seed_pair(
        tmp_path,
        source_lock=_lockfile(tailwindcss="4.0.0"),
        dest_lock=_lockfile(tailwindcss="4.0.0"),
    )
    assert cm.provision_node_modules(str(source), str(dest))["linked"] == ["frontend"]
    assert no_real_npm == []

    source2, dest2 = _seed_pair(
        tmp_path / "second",
        source_lock=_lockfile(tailwindcss="3.4.1"),
        dest_lock=_lockfile(tailwindcss="4.0.0"),
    )
    summary = cm.provision_node_modules(str(source2), str(dest2))
    assert summary["linked"] == []
    assert no_real_npm == [str(dest2 / "frontend")]


def test_optional_and_platform_gated_packages_do_not_force_an_install(
    tmp_path, no_real_npm
):
    """npm leaves these out on purpose; demanding them makes every tree stale."""
    dest_lock = _lockfile(tailwindcss="4.0.0")
    dest_lock["packages"]["node_modules/fsevents"] = {
        "version": "2.3.3",
        "optional": True,
        "os": ["darwin"],
    }
    source, dest = _seed_pair(
        tmp_path, installed=_lockfile(tailwindcss="4.0.0"), dest_lock=dest_lock
    )

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == ["frontend"]
    assert no_real_npm == []


def test_a_failed_install_is_recorded_and_stops_sync(tmp_path, monkeypatch):
    """Better an obviously empty node_modules than a quietly wrong one."""
    source, dest = _seed_pair(
        tmp_path,
        installed=_lockfile(tailwindcss="3.4.1"),
        dest_lock=_lockfile(tailwindcss="4.0.0"),
    )
    (dest / "frontend" / "svelte.config.js").write_text(
        "export default {};", encoding="utf-8"
    )

    monkeypatch.setattr(
        cm, "_run_npm_install", lambda d: (False, "npm ci failed (rc=1): boom")
    )
    sync_calls = []
    monkeypatch.setattr(
        cm, "_run_svelte_kit_sync", lambda d: sync_calls.append(str(d)) or True
    )

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["linked"] == []
    assert summary["installed"] == []
    assert any("install failed" in e for e in summary["errors"])
    assert sync_calls == []
    assert not (dest / "frontend" / "node_modules").exists()


def test_svelte_sync_still_runs_after_an_install(tmp_path, no_real_npm, no_real_npx):
    source, dest = _seed_pair(
        tmp_path,
        installed=_lockfile(tailwindcss="3.4.1"),
        dest_lock=_lockfile(tailwindcss="4.0.0"),
    )
    (dest / "frontend" / "svelte.config.js").write_text(
        "export default {};", encoding="utf-8"
    )

    summary = cm.provision_node_modules(str(source), str(dest))

    assert summary["synced"] == ["frontend"]
    assert no_real_npx == [str(dest / "frontend")]


# --- the installer itself ---------------------------------------------------


def test_install_uses_npm_ci_with_a_lockfile_and_npm_install_without(
    tmp_path, monkeypatch
):
    """`npm ci` is how CI and the Dockerfile install this repo; it needs a lockfile."""
    seen = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        seen.append((command, kwargs.get("cwd")))
        return _Result()

    monkeypatch.setattr(cm.subprocess, "run", fake_run)

    with_lock = tmp_path / "with"
    with_lock.mkdir()
    (with_lock / "package-lock.json").write_text("{}", encoding="utf-8")
    ok, detail = _REAL_NPM_INSTALL(with_lock)
    assert ok and detail.startswith("npm ci")
    assert seen[-1][0][:2] == ["npm", "ci"]
    assert seen[-1][1] == str(with_lock)

    without = tmp_path / "without"
    without.mkdir()
    ok, detail = _REAL_NPM_INSTALL(without)
    assert ok and detail.startswith("npm install")
    assert seen[-1][0][:2] == ["npm", "install"]


def test_install_helper_never_raises_when_npm_is_missing(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("npm not found")

    monkeypatch.setattr(cm.subprocess, "run", boom)
    ok, detail = _REAL_NPM_INSTALL(tmp_path)
    assert ok is False
    assert "could not run" in detail


def test_install_helper_reports_a_nonzero_exit(tmp_path, monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "ERESOLVE could not resolve"

    monkeypatch.setattr(cm.subprocess, "run", lambda *a, **kw: _Result())
    ok, detail = _REAL_NPM_INSTALL(tmp_path)
    assert ok is False
    assert "rc=1" in detail
    assert "ERESOLVE" in detail
