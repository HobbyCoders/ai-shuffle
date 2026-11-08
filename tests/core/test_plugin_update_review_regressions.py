"""Review regressions using real installs, git candidates and isolated homes."""

import json
from pathlib import Path

import pytest

from app.core.plugin_models import AutoUpdateSettings
from app.core.plugin_service import PluginService
from tests.core.test_plugin_source_updates import (  # noqa: F401 — fixture shared with the git matrix
    commit, git, runtime_text, setup, sources, write,
)


def test_copy_starting_directory_cannot_escape_checkout(tmp_path, requires_symlinks):
    from app.core.plugin_install import _copy_tree_resolving_symlinks

    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside"
    write(outside / "leak/SKILL.md", "Outside content")
    (root / "skills").symlink_to(outside, target_is_directory=True)
    destination = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="outside"):
        _copy_tree_resolving_symlinks(root / "skills", destination, root=root)
    assert not destination.exists()


def catalog(svc, change):
    path = svc.MARKETPLACES_DIR / "sample/.claude-plugin/marketplace.json"
    data = json.loads(path.read_text())
    change(data["plugins"][0])
    write(path, json.dumps(data))


def release(upstream, version):
    write(upstream / ".claude-plugin/plugin.json", json.dumps({"name": "sample", "version": version}))
    commit(upstream)
    return git(upstream, "rev-parse", "HEAD")


@pytest.mark.parametrize("catalog_version", ["1.0.0", "3.0.0"])
def test_manifest_is_canonical_despite_a_stale_catalog(sources, catalog_version):
    svc, upstream, skill, info = setup(sources, "url")
    release(upstream, "2.0.0")
    svc.update_plugin(info.id)
    catalog(svc, lambda entry: entry.update(version=catalog_version))
    assert svc.check_for_updates(refresh_sources=True) == []
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    updates = svc.check_for_updates(refresh_sources=True)
    assert len(updates) == 1 and updates[0].available_version.startswith("2.0.0+")
    svc.update_plugin(info.id)
    assert "Second revision" in runtime_text(svc)


@pytest.mark.parametrize("entry_version", [None, "3.0.0"])
def test_changed_sha_pin_cannot_offer_or_apply_an_older_manifest(sources, entry_version):
    svc, upstream, skill, info = setup(sources, "sha")
    first = git(upstream, "rev-parse", "HEAD")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    newer = release(upstream, "2.0.0")
    catalog(svc, lambda entry: entry["source"].update(sha=newer))
    svc.update_plugin(info.id)
    catalog(svc, lambda entry: entry["source"].update(sha=first))
    catalog(svc, lambda entry: entry.update(version=entry_version))
    assert svc.check_for_updates(refresh_sources=True) == []
    svc.save_auto_update_settings(AutoUpdateSettings(enabled=True))
    assert svc.run_auto_update()["updated_plugins"] == []
    with pytest.raises(ValueError, match="downgrade"):
        svc.update_plugin(info.id)
    assert "Second revision" in runtime_text(svc)
    assert svc.get_installed_plugins()[0].version == "2.0.0"


def test_implicit_update_does_not_adopt_new_catalog_hooks_or_mcp(sources):
    svc, upstream, skill, info = setup(sources, "implicit")
    write(upstream / ".claude-plugin/marketplace.json", json.dumps({
        "name": "sample", "plugins": [{"name": "sample", "source": "./"}],
    }))
    write(upstream / "hooks/hooks.json", '{"hooks": {"Stop": []}}')
    write(upstream / ".mcp.json", '{"mcpServers": {"new": {"command": "not-executed"}}}')
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    svc.save_auto_update_settings(AutoUpdateSettings(enabled=True))
    summary = svc.run_auto_update()
    assert summary["errors"] == []
    assert [p["id"] for p in summary["updated_plugins"]] == [info.id]
    record = svc._read_json_file(svc.INSTALLED_FILE, {})["plugins"][info.id][0]
    assert record["implicit"] is True
    installed = Path(record["installPath"])
    assert not (installed / "hooks").exists() and not (installed / ".mcp.json").exists()
    assert "Second revision" in runtime_text(svc)
    plugin = svc.get_installed_plugins()[0]
    assert plugin.enabled and not plugin.has_hooks and not plugin.has_mcp
    # A deliberate fresh owner install, unlike an update, may adopt the catalog.
    svc.install_plugin(info.id)
    assert (Path(svc.get_installed_plugins()[0].install_path) / ".mcp.json").is_file()


def test_passive_external_checks_are_cached_and_never_reach_git(sources, monkeypatch):
    svc, upstream, skill, info = setup(sources, "url")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    import app.core.plugin_update_content as content
    with monkeypatch.context() as patch:
        patch.setattr(content, "_remote_revision", lambda source: pytest.fail("passive remote probe"))
        patch.setattr(svc, "_clone_plugin_source", lambda *args: pytest.fail("passive clone"))
        patch.setattr(svc, "sync_marketplace", lambda *args: pytest.fail("passive sync"))
        assert svc.check_for_updates() == []
    expected = svc.check_for_updates(refresh_sources=True)
    assert len(expected) == 1
    monkeypatch.setattr(content, "_remote_revision", lambda source: pytest.fail("cached remote probe"))
    monkeypatch.setattr(PluginService, "_clone_plugin_source", lambda *args: pytest.fail("cached clone"))
    # Cache survives service reconstruction; disabled automatic checking does not
    # turn an authenticated GET into a network operation.
    svc.save_auto_update_settings(AutoUpdateSettings(enabled=False))
    assert PluginService().check_for_updates() == expected
    svc.save_auto_update_settings(AutoUpdateSettings(
        enabled=True, auto_sync_marketplaces=False, auto_update_plugins=False,
    ))
    summary = svc.run_auto_update()
    assert summary["updates_available"] == [update.to_dict() for update in expected]
    assert summary["updated_plugins"] == [] and summary["errors"] == []


def test_large_implicit_tree_updates_without_authored_approval_limits(sources):
    svc, upstream, skill, info = setup(sources, "implicit")
    for i in range(2001):
        write(upstream / f"skills/sample/support/file-{i}.txt", f"support {i}")
    commit(upstream)
    svc.update_plugin(info.id)
    assert svc.check_for_updates(refresh_sources=True) == []
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    svc.save_auto_update_settings(AutoUpdateSettings(enabled=True))
    summary = svc.run_auto_update()
    assert summary["errors"] == [] and len(summary["updated_plugins"]) == 1
    assert "Second revision" in runtime_text(svc)


def test_one_unavailable_external_source_does_not_drop_healthy_updates(sources, monkeypatch):
    svc, upstream, skill, info = setup(sources, "url")
    mp = svc.MARKETPLACES_DIR / "sample"
    manifest = mp / ".claude-plugin/marketplace.json"
    data = json.loads(manifest.read_text())
    data["plugins"].append({"name": "healthy", "source": "./healthy"})
    write(manifest, json.dumps(data))
    write(mp / "healthy/plugin.json", '{"name": "healthy", "version": "1.0.0"}')
    svc.install_plugin("healthy@sample")
    write(mp / "healthy/plugin.json", '{"name": "healthy", "version": "1.1.0"}')
    import app.core.plugin_update_content as content
    def unavailable(source):
        raise RuntimeError("disposable source unavailable")
    monkeypatch.setattr(content, "_remote_revision", unavailable)
    assert [u.plugin_id for u in svc.check_for_updates(refresh_sources=True)] == ["healthy@sample"]
    svc.save_auto_update_settings(AutoUpdateSettings(enabled=True))
    summary = svc.run_auto_update()
    assert [p["id"] for p in summary["updated_plugins"]] == ["healthy@sample"]
