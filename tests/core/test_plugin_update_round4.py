"""Real-service regressions for alias roots, transactional cache replacement and sync failures."""
import json
from pathlib import Path

import pytest

from app.core import plugin_install
from app.core.plugin_models import AutoUpdateSettings
from tests.core.test_plugin_source_updates import commit, runtime_text, setup, sources, write  # noqa: F401


@pytest.mark.parametrize("operation", ["check", "update", "refuse"])
def test_registered_alias_root_can_check_and_update(sources, tmp_path, monkeypatch, requires_symlinks, operation):
    svc, upstream, skill, info = setup(sources, "directory")
    registry = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    target = Path(registry["sample"]["installLocation"])
    alias = tmp_path / "source-alias"
    alias.symlink_to(target, target_is_directory=True)
    registry["sample"]["installLocation"] = str(alias)
    registry["sample"]["source"]["path"] = str(alias)
    svc._write_json_file(svc.MARKETPLACES_FILE, registry)
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    if operation == "check":
        report = svc.get_update_report(refresh_sources=True)
        assert report["checks"][0]["status"] == "update"
        assert len(report["updates"]) == 1
    elif operation == "refuse":
        installed = svc._read_json_file(svc.INSTALLED_FILE, {})
        def refuse_copy(src, dst, *args, **kwargs):
            write(Path(dst) / "partial", "Incomplete replacement")
            raise OSError("Forced copy failure through alias")
        monkeypatch.setattr(plugin_install, "_copy_tree_resolving_symlinks", refuse_copy)
        with pytest.raises(OSError, match="Forced copy"):
            svc.update_plugin(info.id)
        assert "First revision" in runtime_text(svc)
        assert svc._read_json_file(svc.INSTALLED_FILE, {}) == installed
    else:
        try:
            svc.update_plugin(info.id)
        except Exception:
            assert "First revision" in runtime_text(svc), "Refusal destroyed installed content"
            raise
        assert "Second revision" in runtime_text(svc)


@pytest.mark.parametrize("kind", ["directory", "implicit", "url"])
@pytest.mark.parametrize("failure", ["materialize", "publish", "record", "record_false", "activate"])
def test_any_failed_replacement_preserves_previous_cache(sources, monkeypatch, kind, failure):
    svc, upstream, skill, info = setup(sources, kind)
    installed = svc._read_json_file(svc.INSTALLED_FILE, {})
    before = Path(info.install_path, "skills/sample/SKILL.md").read_bytes()
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    if kind != "directory":
        commit(upstream)
    if failure == "materialize":
        def refuse_copy(src, dst, *args, **kwargs):
            write(Path(dst) / "partial", "Incomplete replacement")
            raise OSError("Forced materialization failure")
        monkeypatch.setattr(plugin_install, "_copy_tree_resolving_symlinks", refuse_copy)
        if kind == "url":
            monkeypatch.setattr(plugin_install.shutil, "move", refuse_copy)
    elif failure == "publish":
        original = Path.replace
        def refuse_publish(path, destination):
            if path.name == "candidate" and Path(destination) == Path(info.install_path):
                raise OSError("Forced publication failure")
            return original(path, destination)
        monkeypatch.setattr(Path, "replace", refuse_publish)
    elif failure == "activate":
        def refuse_activation(*args):
            raise OSError("Forced activation failure")
        monkeypatch.setattr(svc, "enable_plugin", refuse_activation)
    else:
        original = svc._write_json_file
        def refuse_record(path, data):
            if path == svc.INSTALLED_FILE:
                if failure == "record_false":
                    return False
                raise OSError("Forced record failure")
            return original(path, data)
        monkeypatch.setattr(svc, "_write_json_file", refuse_record)
    # Keep the version/cache path constant for the implicit case as well.
    monkeypatch.setattr(svc, "_get_marketplace_head_version", lambda _: info.version)
    with pytest.raises(OSError, match="Forced|Could not register"):
        svc.update_plugin(info.id)
    assert Path(info.install_path, "skills/sample/SKILL.md").read_bytes() == before
    assert svc._read_json_file(svc.INSTALLED_FILE, {}) == installed
    assert not Path(info.install_path, "partial").exists()


@pytest.mark.parametrize("pending_update", [False, True])
@pytest.mark.parametrize("sync_failure", ["raises", "returns_false"])
def test_scheduled_failed_sync_preserves_last_known_report(sources, monkeypatch, pending_update, sync_failure):
    svc, upstream, skill, info = setup(sources, "implicit")
    if pending_update:
        write(skill, skill.read_text().replace("First revision", "Second revision"))
        commit(upstream)
    successful = svc.get_update_report(refresh_sources=True)
    last_success = successful["checks"][0]["checked_at"]
    expected = [update.to_dict() for update in successful["updates"]]
    def unavailable(*args):
        if sync_failure == "returns_false":
            return False
        raise RuntimeError("Disposable source unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(svc, "sync_marketplace", unavailable)
        explicit = svc.get_update_report(refresh_sources=True)
        assert explicit["checks"][0]["status"] == "failed"
        svc.save_auto_update_settings(AutoUpdateSettings(
            enabled=True, auto_sync_marketplaces=True, auto_update_plugins=True,
        ))
        summary = svc.run_auto_update()
        assert summary["synced_marketplaces"] == {"sample": False}
        passive = svc.get_update_report()
        assert passive["checks"][0]["status"] == "failed"
        assert passive["checks"][0]["checked_at"] == last_success
        assert [update.to_dict() for update in passive["updates"]] == expected
        assert summary["updates_available"] == expected
        assert summary["updated_plugins"] == []
        assert summary["errors"]
        assert "First revision" in runtime_text(svc)
    recovered = svc.run_auto_update()
    assert recovered["synced_marketplaces"] == {"sample": True}
    assert len(recovered["updated_plugins"]) == int(pending_update)
    assert svc.get_update_report(refresh_sources=True)["checks"][0]["status"] == "current"


@pytest.mark.parametrize("kind", ["directory", "implicit", "url"])
def test_activation_failure_restores_record_across_changed_cache_paths(sources, monkeypatch, kind):
    svc, upstream, skill, info = setup(sources, kind)
    installed = svc._read_json_file(svc.INSTALLED_FILE, {})
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    manifest = skill.parents[2] / ".claude-plugin/plugin.json"
    write(manifest, '{"name":"sample","version":"1.1.0"}')
    if kind != "directory":
        commit(upstream)
    def refuse_activation(*args):
        raise OSError("Forced activation failure")
    monkeypatch.setattr(svc, "enable_plugin", refuse_activation)
    with pytest.raises(OSError, match="Forced activation"):
        svc.update_plugin(info.id)
    assert svc._read_json_file(svc.INSTALLED_FILE, {}) == installed
    assert "First revision" in runtime_text(svc)


def test_copy_pins_canonical_root_even_if_alias_is_retargeted(tmp_path, monkeypatch, requires_symlinks):
    import os
    real, outside, alias = tmp_path / "real", tmp_path / "outside", tmp_path / "alias"
    write(real / "SKILL.md", "Inside checkout")
    write(outside / "SKILL.md", "Outside secret fixture")
    alias.symlink_to(real, target_is_directory=True)
    identity = real.stat().st_ino
    original, swapped = os.scandir, []
    def retarget(path):
        inode = os.fstat(path).st_ino if isinstance(path, int) else Path(path).stat().st_ino
        if inode == identity and not swapped:
            alias.unlink()
            alias.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original(path)
    monkeypatch.setattr(os, "scandir", retarget)
    destination = tmp_path / "installed"
    plugin_install._copy_tree_resolving_symlinks(alias, destination, root=alias)
    assert swapped
    assert (destination / "SKILL.md").read_text() == "Inside checkout"


def test_starting_directory_escape_is_refused_even_with_alias_root(tmp_path, requires_symlinks):
    real, outside, alias = tmp_path / "real", tmp_path / "outside", tmp_path / "alias"
    write(real / "inside/SKILL.md", "Inside checkout")
    write(outside / "SKILL.md", "Outside secret fixture")
    alias.symlink_to(real, target_is_directory=True)
    (real / "skills").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        plugin_install._copy_tree_resolving_symlinks(alias / "skills", tmp_path / "installed", root=alias)
    assert not (tmp_path / "installed").exists()


def test_scheduled_source_failure_does_not_suppress_healthy_install(sources, monkeypatch):
    svc, upstream, skill, info = setup(sources, "implicit")
    healthy = svc.MARKETPLACES_DIR / "healthy"
    write(healthy / ".claude-plugin/marketplace.json", json.dumps({
        "name": "healthy", "plugins": [{"name": "healthy", "source": "./plugin"}],
    }))
    write(healthy / "plugin/.claude-plugin/plugin.json", '{"name":"healthy","version":"1.0.0"}')
    write(healthy / "plugin/skills/healthy/SKILL.md", "---\nname: healthy\ndescription: Healthy fixture\n---\nFirst revision")
    registry = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    registry["healthy"] = {"installLocation": str(healthy), "source": {"source": "directory", "path": str(healthy)}}
    svc._write_json_file(svc.MARKETPLACES_FILE, registry)
    healthy_info = svc.install_plugin("healthy@healthy")
    write(healthy / "plugin/skills/healthy/SKILL.md", "---\nname: healthy\ndescription: Healthy fixture\n---\nSecond revision")
    sync = svc.sync_marketplace
    def partially_unavailable(marketplace_id):
        if marketplace_id == "sample":
            raise RuntimeError("Disposable source unavailable")
        return sync(marketplace_id)
    monkeypatch.setattr(svc, "sync_marketplace", partially_unavailable)
    svc.save_auto_update_settings(AutoUpdateSettings(
        enabled=True, auto_sync_marketplaces=True, auto_update_plugins=True,
    ))
    summary = svc.run_auto_update()
    assert summary["synced_marketplaces"] == {"sample": False, "healthy": True}
    assert [plugin["id"] for plugin in summary["updated_plugins"]] == [healthy_info.id]
    assert svc.get_update_report()["checks"][0]["status"] == "failed"
    assert "Second revision" in Path(healthy_info.install_path, "skills/healthy/SKILL.md").read_text()
