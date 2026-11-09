"""Round-three reproductions: mutable sources, release ordering, state and repair."""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from app.core.plugin_install import _check_update_version, _copy_tree_resolving_symlinks
from app.core.plugin_models import AutoUpdateSettings
from app.core.plugin_updates import _compare_versions
from tests.core.test_plugin_update_review_regressions import catalog, release
from tests.core.test_plugin_source_updates import commit, runtime_text, setup, sources, write  # noqa: F401


def test_swap_during_directory_enumeration_cannot_copy_outside(tmp_path, monkeypatch, requires_symlinks):
    checkout = tmp_path / "checkout"
    victim = checkout / "skills/sample"
    write(victim / "SKILL.md", "Inside")
    outside = tmp_path / "outside"
    write(outside / "SKILL.md", "Outside secret fixture")
    identity = victim.stat().st_ino
    attempted = False
    original_iterdir, original_scandir = Path.iterdir, os.scandir

    def swap(path):
        nonlocal attempted
        matches = os.fstat(path).st_ino == identity if isinstance(path, int) else Path(path) == victim
        if matches and not attempted:
            attempted = True
            try:
                shutil.rmtree(victim)
                victim.symlink_to(outside, target_is_directory=True)
            except PermissionError:  # Windows pins disallow the substitution.
                pass

    def iterdir(path):
        swap(path)
        return original_iterdir(path)

    def scandir(path):
        swap(path)
        return original_scandir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(os, "scandir", scandir)
    destination = tmp_path / "installed"
    try:
        _copy_tree_resolving_symlinks(checkout / "skills", destination, root=checkout)
    except (OSError, ValueError):
        pass  # Refusal is safe; installing outside data is not.
    assert attempted
    assert all("Outside secret" not in p.read_text() for p in destination.rglob("SKILL.md"))


def test_uncoordinated_git_sync_can_overlap_install_without_copying_outside(sources, tmp_path, monkeypatch, requires_symlinks):
    svc, upstream, skill, info = setup(sources, "implicit")
    outside = tmp_path / "outside"
    write(outside / "SKILL.md", "Outside secret fixture")
    shutil.rmtree(skill.parent)
    skill.parent.symlink_to(outside, target_is_directory=True)
    commit(upstream)
    marketplace = Path(svc._read_json_file(svc.MARKETPLACES_FILE, {})["sample"]["installLocation"])
    victim = marketplace / "skills/sample"
    identity = victim.stat().st_ino
    attempted, completed, errors = [], [], []
    original_iterdir, original_scandir = Path.iterdir, os.scandir

    def sync():
        try:
            # Cooperating service sync now shares the source gate. Keep this
            # adversarial confinement test independent of that gate: a git CLI
            # outside the service can still change the checkout during copying.
            subprocess.run(["git", "-C", str(marketplace), "pull", "--ff-only"],
                           check=True, capture_output=True)
            completed.append(True)
        except Exception as exc:
            errors.append(exc)

    def overlap(path):
        matches = os.fstat(path).st_ino == identity if isinstance(path, int) else Path(path) == victim
        if matches and not attempted:
            attempted.append(True)
            worker = threading.Thread(target=sync, daemon=True)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive(), "External Git checkout edit did not finish"
            assert completed and not errors
            if os.name != "nt":
                assert victim.is_symlink()

    def iterdir(path):
        overlap(path)
        return original_iterdir(path)

    def scandir(path):
        overlap(path)
        return original_scandir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(os, "scandir", scandir)
    try:
        svc.update_plugin(info.id, refresh_source=False)
    except (OSError, ValueError):
        pass  # Refusing the now-changing source is safe.
    assert attempted and completed
    assert all("Outside secret" not in p.read_text() for p in svc.CACHE_DIR.rglob("SKILL.md"))


@pytest.mark.parametrize("installed,candidate,lower", [
    ("2.0.0", "1.0.0-rc.1", True),
    ("2.0.0+build.5", "1.0.0", True),
    ("v2.0", "2.0.0", False),
    ("2.0", "2.0.0", False),
])
def test_canonical_release_downgrade_guard(installed, candidate, lower):
    if lower:
        with pytest.raises(ValueError, match="downgrade"):
            _check_update_version(installed, candidate)
    else:
        _check_update_version(installed, candidate)


def test_transient_external_check_keeps_last_known_update(sources, monkeypatch):
    svc, upstream, skill, info = setup(sources, "url")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    expected = svc.check_for_updates(refresh_sources=True)
    assert len(expected) == 1
    def unavailable(source):
        raise RuntimeError("Disposable source unavailable")
    monkeypatch.setattr("app.core.plugin_update_content._remote_revision", unavailable)
    assert svc.check_for_updates(refresh_sources=True) == expected
    assert svc.check_for_updates() == expected
    report = svc.get_update_report()
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][0]["checked_at"]
    assert "source" in report["checks"][0]["error"]


@pytest.mark.parametrize("operation", ["check", "all", "automatic"])
def test_missing_installed_tree_is_offered_and_repaired(sources, operation):
    svc, upstream, skill, info = setup(sources, "directory")
    candidate = skill.parents[2]
    write(candidate / ".claude-plugin/plugin.json", '{"name":"sample","version":"1.1.0"}')
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    shutil.rmtree(info.install_path)
    updates = svc.check_for_updates(refresh_sources=True)
    assert len(updates) == 1 and updates[0].available_version == "1.1.0"
    if operation == "all":
        assert len(svc.update_all_plugins()) == 1
    elif operation == "automatic":
        svc.save_auto_update_settings(AutoUpdateSettings(enabled=True))
        assert len(svc.run_auto_update()["updated_plugins"]) == 1
    else:
        svc.update_plugin(info.id)
    assert "Second revision" in runtime_text(svc)


@pytest.mark.parametrize("installed,candidate,lower", [
    ("2.0.0", "1.0.0-rc.1", True),
    ("2.0.0+build.5", "1.0.0", True),
    ("v2.0", "2.0.0", False),
    ("2.0", "2.0.0", False),
])
def test_changed_pin_uses_same_release_order_in_discovery_and_install(sources, installed, candidate, lower):
    svc, upstream, skill, info = setup(sources, "sha")
    installed_pin = release(upstream, installed)
    catalog(svc, lambda entry: entry["source"].update(sha=installed_pin))
    svc.update_plugin(info.id)
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    candidate_pin = release(upstream, candidate)
    catalog(svc, lambda entry: entry["source"].update(sha=candidate_pin))
    updates = svc.check_for_updates(refresh_sources=True)
    if lower:
        assert updates == []
        with pytest.raises(ValueError, match="downgrade"):
            svc.update_plugin(info.id)
        assert "First revision" in runtime_text(svc)
    else:
        assert len(updates) == 1
        svc.update_plugin(info.id)
        assert "Second revision" in runtime_text(svc)


@pytest.mark.parametrize("installed,candidate,order", [
    ("1.0.0-alpha.1", "1.0.0-alpha.2", 1),
    ("1.0.0-alpha.2", "1.0.0-alpha.10", 1),
    ("1.0.0-alpha.10", "1.0.0-beta", 1),
    ("1.0.0-beta", "1.0.0", 1),
    ("1.0.0", "1.0.0-rc.1", -1),
    ("v2.0.0+build.1", "2.0+build.2", 0),
    ("0123456", "1234567", None),
    ("abcdef0", "abcdef1", None),
])
def test_single_release_comparison(installed, candidate, order):
    assert _compare_versions(installed, candidate) == order


@pytest.mark.parametrize("kind", ["directory", "url", "sha", "implicit"])
def test_same_release_missing_tree_is_repairable_even_at_unchanged_pin(sources, kind):
    svc, upstream, skill, info = setup(sources, kind)
    shutil.rmtree(info.install_path)
    updates = svc.check_for_updates(refresh_sources=True)
    assert len(updates) == 1 and "repair" in updates[0].available_version
    assert len(svc.update_all_plugins()) == 1
    assert "First revision" in runtime_text(svc)
    assert svc.check_for_updates(refresh_sources=True) == []


def test_report_distinguishes_unchecked_current_and_failed_without_hiding_healthy(sources, monkeypatch):
    svc, upstream, skill, info = setup(sources, "url")
    assert svc.get_update_report()["checks"][0]["status"] == "unchecked"
    assert svc.get_update_report(refresh_sources=True)["checks"][0]["status"] == "current"
    path = svc.MARKETPLACES_DIR / "sample"
    manifest = path / ".claude-plugin/marketplace.json"
    data = json.loads(manifest.read_text())
    data["plugins"].append({"name": "healthy", "source": "./healthy"})
    write(manifest, json.dumps(data))
    write(path / "healthy/plugin.json", '{"name":"healthy","version":"1.0.0"}')
    svc.install_plugin("healthy@sample")
    write(path / "healthy/plugin.json", '{"name":"healthy","version":"1.1.0"}')
    def unavailable(source):
        raise RuntimeError("Disposable source unavailable")
    monkeypatch.setattr("app.core.plugin_update_content._remote_revision", unavailable)
    report = svc.get_update_report(refresh_sources=True)
    assert [update.plugin_id for update in report["updates"]] == ["healthy@sample"]
    assert {check["plugin_id"]: check["status"] for check in report["checks"]} == {
        "sample@sample": "failed", "healthy@sample": "update",
    }
    assert svc.get_update_report()["checks"][0]["status"] == "failed"


def test_failed_marketplace_sync_is_not_reported_as_successful_current(sources, monkeypatch):
    svc, upstream, skill, info = setup(sources, "implicit")
    svc.get_update_report(refresh_sources=True)
    def unavailable(*args):
        raise RuntimeError("Disposable source unavailable")
    monkeypatch.setattr(svc, "sync_marketplace", unavailable)
    report = svc.get_update_report(refresh_sources=True)
    assert report["checks"][0]["status"] == "failed"
    assert svc.get_update_report()["checks"][0]["status"] == "failed"


@pytest.mark.parametrize("kind", ["url", "sha"])
def test_catalog_fallback_label_update_is_not_lost_to_git_fast_path(sources, kind):
    svc, upstream, skill, info = setup(sources, kind)
    write(upstream / ".claude-plugin/plugin.json", '{"name":"sample"}')
    commit(upstream)
    from tests.core.test_plugin_source_updates import git
    if kind == "sha":
        revision = git(upstream, "rev-parse", "HEAD")
        catalog(svc, lambda entry: entry["source"].update(sha=revision))
    svc.update_plugin(info.id)
    catalog(svc, lambda entry: entry.update(version="1.1.0"))
    updates = svc.check_for_updates(refresh_sources=True)
    assert len(updates) == 1 and updates[0].available_version == "1.1.0"
    assert svc.update_plugin(info.id).version == "1.1.0"



def test_missing_cache_cannot_be_called_current_when_only_candidate_is_older(sources):
    svc, upstream, skill, info = setup(sources, "directory")
    write(Path(info.install_path) / ".claude-plugin/plugin.json", '{"name":"sample","version":"2.0.0"}')
    record = svc._read_json_file(svc.INSTALLED_FILE, {})
    record["plugins"][info.id][0]["version"] = "2.0.0"
    svc._write_json_file(svc.INSTALLED_FILE, record)
    shutil.rmtree(info.install_path)
    report = svc.get_update_report(refresh_sources=True)
    assert report["updates"] == []
    assert report["checks"][0]["status"] == "failed"


def test_empty_implicit_upstream_is_a_failed_check_not_successfully_current(sources):
    svc, upstream, skill, info = setup(sources, "implicit")
    shutil.rmtree(skill.parent.parent)
    write(upstream / "README.md", "No plugin components")
    commit(upstream)
    report = svc.get_update_report(refresh_sources=True)
    assert report["updates"] == []
    assert report["checks"][0]["status"] == "failed"
