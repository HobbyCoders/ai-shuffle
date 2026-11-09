"""Real-service regressions for shared state, failed activation commit and mode identity."""
import copy
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from tests.core.test_plugin_source_updates import runtime_text, setup, sources, write  # noqa: F401


def test_unused_marketplace_removal_cannot_revert_another_update(sources, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    unused = svc.MARKETPLACES_DIR / "unused"
    unused.mkdir()
    registry = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    registry["unused"] = {"source": {"source": "directory"}, "installLocation": str(unused)}
    svc._write_json_file(svc.MARKETPLACES_FILE, registry)
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    paused, resume, done = threading.Event(), threading.Event(), threading.Event()
    read = svc._read_json_file
    errors = []

    def pause_registry(path, *args, **kwargs):
        result = read(path, *args, **kwargs)
        if path == svc.INSTALLED_FILE and threading.current_thread().name == "remove":
            paused.set()
            assert resume.wait(5)
        return result

    def remove():
        try:
            svc.remove_marketplace("unused")
        except Exception as exc:
            errors.append(exc)

    def update():
        try:
            svc.update_plugin(info.id)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(svc, "_read_json_file", pause_registry)
    a, b = threading.Thread(target=remove, name="remove"), threading.Thread(target=update)
    a.start()
    try:
        assert paused.wait(5)
        b.start()
        done.wait(.3)
    finally:
        resume.set()
        a.join(5)
        if b.ident is not None:
            b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert not errors
    assert "Second revision" in runtime_text(svc)
    assert Path(svc.get_installation_records(info.id)[0]["installPath"]).is_dir()


@pytest.mark.parametrize("previous", [None, False, True])
@pytest.mark.parametrize("desired", [False, True])
def test_failed_install_commit_preserves_prior_enablement(sources, monkeypatch, previous, desired):
    svc, _, _, info = setup(sources, "directory")
    if previous is None:
        svc.uninstall_plugin(info.id)
    elif previous:
        svc.enable_plugin(info.id)
    else:
        svc.disable_plugin(info.id)
    settings = copy.deepcopy(svc._read_json_file(svc.SETTINGS_FILE, {}))
    installed = copy.deepcopy(svc._read_json_file(svc.INSTALLED_FILE, {}))
    save = svc._write_json_file

    def refuse_record(path, data):
        return False if path == svc.INSTALLED_FILE else save(path, data)

    monkeypatch.setattr(svc, "_write_json_file", refuse_record)
    with pytest.raises(OSError, match="Could not register"):
        svc.install_plugin(info.id, enable=desired)
    assert svc._read_json_file(svc.SETTINGS_FILE, {}) == settings
    assert svc._read_json_file(svc.INSTALLED_FILE, {}) == installed


@pytest.mark.skipif(os.name == "nt", reason="Windows does not preserve POSIX executable modes")
def test_mode_only_update_and_cache_repair(sources):
    svc, _, skill, info = setup(sources, "directory")
    tool = skill.parents[2] / "bin/tool"
    write(tool, "fixture executable\n")
    tool.chmod(0o644)
    before = svc.update_plugin(info.id)
    assert stat.S_IMODE(Path(before.install_path, "bin/tool").stat().st_mode) == 0o644
    tool.chmod(0o755)
    report = svc.get_update_report(refresh_sources=True)
    assert [item.plugin_id for item in report["updates"]] == [info.id]
    updated = svc.update_plugin(info.id)
    assert updated.install_path != before.install_path
    cached = Path(updated.install_path, "bin/tool")
    assert stat.S_IMODE(cached.stat().st_mode) == 0o755
    cached.chmod(0o644)
    repaired = svc.update_plugin(info.id)
    assert repaired.install_path != updated.install_path
    assert stat.S_IMODE(Path(repaired.install_path, "bin/tool").stat().st_mode) == 0o755


def test_failed_settings_restore_keeps_explicit_prior_copy(sources, monkeypatch):
    svc, _, _, info = setup(sources, "directory")
    svc.disable_plugin(info.id)
    prior = svc.SETTINGS_FILE.read_bytes()
    record = svc.INSTALLED_FILE.read_bytes()
    save, replace = svc._write_json_file, Path.replace

    def refuse_record(path, data):
        return False if path == svc.INSTALLED_FILE else save(path, data)

    def refuse_restore(path, destination):
        if path.name.startswith(".plugin-settings-restore-"):
            raise PermissionError("Injected restoration sharing violation")
        return replace(path, destination)

    monkeypatch.setattr(svc, "_write_json_file", refuse_record)
    monkeypatch.setattr(Path, "replace", refuse_restore)
    with pytest.raises(OSError, match="Could not restore plugin settings"):
        svc.install_plugin(info.id, enable=True)
    backups = list(svc.SETTINGS_FILE.parent.glob(".plugin-settings-restore-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == prior
    assert svc.INSTALLED_FILE.read_bytes() == record
    # A real restoration refusal is not reported as success or hidden: the
    # old bytes survive for recovery, even though the active file could not be restored.
    assert svc.get_enabled_plugins()[info.id] is True


def test_legacy_byte_only_revision_is_not_reclassified_as_corrupt(sources):
    import hashlib
    import shutil

    svc, _, _, info = setup(sources, "directory")
    current = Path(info.install_path)
    digest = hashlib.sha256()
    for path in sorted(current.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(current).as_posix().encode() + b"\0")
            digest.update(str(path.stat().st_size).encode() + b"\0")
            digest.update(path.read_bytes())
    key = hashlib.sha256(info.version.encode() + b"\0" + digest.hexdigest().encode()).hexdigest()
    legacy = current.parent / (".revision-" + info.version + "-" + key)
    shutil.copytree(current, legacy)
    data = svc._read_json_file(svc.INSTALLED_FILE, {})
    data["plugins"][info.id][0]["installPath"] = str(legacy)
    svc._write_json_file(svc.INSTALLED_FILE, data)
    inode = legacy.stat().st_ino
    assert not svc.get_update_report(refresh_sources=True)["updates"]
    updated = svc.update_plugin(info.id)
    assert Path(updated.install_path) == legacy
    assert legacy.stat().st_ino == inode
    assert "First revision" in runtime_text(svc)


def test_windows_mode_policy_has_stable_keys_and_repeat_checks(sources, monkeypatch):
    from app.core import plugin_update_content

    monkeypatch.setattr(plugin_update_content, "_EXECUTABLE_MASK", 0)
    svc, _, skill, info = setup(sources, "directory")
    tool = skill.parents[2] / "bin/tool"
    write(tool, "portable fixture\n")
    tool.chmod(0o644)
    first = svc.update_plugin(info.id)
    tool.chmod(0o755)
    assert not svc.get_update_report(refresh_sources=True)["updates"]
    assert svc.update_plugin(info.id).install_path == first.install_path
    # Relative content identity is independent of the worker's root directory.
    import shutil
    other = svc.PLUGINS_DIR / "another-worker-tree"
    shutil.copytree(Path(first.install_path), other)
    Path(other, "bin/tool").chmod(0o644)
    assert plugin_update_content._tree_digest(other) == plugin_update_content._tree_digest(Path(first.install_path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable repair requires POSIX modes")
def test_shell_transform_is_identical_for_publish_and_comparison(sources):
    svc, _, skill, info = setup(sources, "directory")
    script = skill.parents[2] / "bin/tool.sh"
    write(script, "#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    installed = svc.update_plugin(info.id)
    assert Path(installed.install_path, "bin/tool.sh").stat().st_mode & 0o111 == 0o111
    assert not svc.get_update_report(refresh_sources=True)["updates"]
    Path(installed.install_path, "bin/tool.sh").chmod(0o644)
    assert svc.get_update_report(refresh_sources=True)["updates"]
    repaired = svc.update_plugin(info.id)
    assert repaired.install_path != installed.install_path
    assert Path(repaired.install_path, "bin/tool.sh").stat().st_mode & 0o111 == 0o111


@pytest.mark.parametrize("kill_at", ["settings", "records", "cache"])
def test_killed_removal_is_retryable_and_updates_cannot_resurrect_it(sources, kill_at):
    from tests.core.test_plugin_revision_publication import _child

    svc, _, _, info = setup(sources, "directory")
    record = svc._read_json_file(svc.INSTALLED_FILE, {})
    code = """
from app.core import plugin_marketplace
save = svc._write_json_file
remove = plugin_marketplace.shutil.rmtree
phase = sys.argv[2]
def interrupt_write(path, data):
    if phase == 'settings' and path == svc.SETTINGS_FILE:
        os._exit(73)
    if phase == 'records' and path == svc.INSTALLED_FILE:
        os._exit(73)
    return save(path, data)
def interrupt_remove(*args, **kwargs):
    os._exit(73)
svc._write_json_file = interrupt_write
if phase == 'cache':
    plugin_marketplace.shutil.rmtree = interrupt_remove
svc.remove_marketplace('sample')
"""
    child = _child(svc, code, kill_at)
    try:
        out, err = child.communicate(timeout=15)
        assert child.returncode == 73, (out, err)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert svc._read_json_file(svc.MARKETPLACES_FILE, {})["sample"]["removing"]
    if kill_at in ("settings", "records"):
        assert svc._read_json_file(svc.INSTALLED_FILE, {}) == record
        assert Path(info.install_path).is_dir()
        assert "First revision" in runtime_text(svc)
    else:
        assert not svc.get_installation_records(info.id)
    with pytest.raises(ValueError, match="removal is incomplete"):
        svc.update_plugin(info.id)
    with pytest.raises(ValueError, match="removal is incomplete"):
        svc.sync_marketplace("sample")
    assert svc.remove_marketplace("sample")
    assert "sample" not in svc._read_json_file(svc.MARKETPLACES_FILE, {})
    assert not svc.get_installation_records(info.id)
    assert not (svc.CACHE_DIR / "sample").exists()
    assert info.id not in svc.get_enabled_plugins()
    with pytest.raises(ValueError):
        svc.update_plugin(info.id)
    assert not svc.get_installation_records(info.id)
    assert not (svc.CACHE_DIR / "sample").exists()


def test_settings_import_participates_in_shared_state_and_respects_removal(sources, tmp_path, monkeypatch):
    from fluid.context import plugin_state
    from fluid.context.settings_import import apply_settings_import, preview_settings_import

    svc, _ = sources
    external = tmp_path / "external-home"
    write(external / ".claude/plugins/cache/mp/tool/1.0.0/.claude-plugin/plugin.json", '{"name":"tool"}')
    write(external / ".claude/settings.json", '{"enabledPlugins":{"tool@mp":true}}')
    preview = preview_settings_import(sources=("claude",), source_home=external, destination_home=svc.FLUID_DIR.parent)
    assert any(item.kind == "plugin_enablement" and item.status == "ready" for item in preview.items)
    svc._write_json_file(svc.MARKETPLACES_FILE, {"mp": {"removing": True}})
    with pytest.raises(ValueError, match="removal is incomplete"):
        apply_settings_import(preview)
    assert not svc.SETTINGS_FILE.exists()
    svc._write_json_file(svc.MARKETPLACES_FILE, {})
    from contextlib import contextmanager
    lock, observed = plugin_state.plugin_state_lock, []

    @contextmanager
    def observe(plugins_dir):
        observed.append(Path(plugins_dir))
        with lock(plugins_dir):
            yield

    monkeypatch.setattr(plugin_state, "plugin_state_lock", observe)
    result = apply_settings_import(preview)
    assert observed == [svc.PLUGINS_DIR]
    assert any(item.kind == "plugin_enablement" and item.status == "imported" for item in result.items)
    assert svc.get_enabled_plugins() == {"tool@mp": True}


def test_unfetched_default_partial_removal_does_not_block_other_sources(sources, monkeypatch):
    from app.core import plugin_marketplace

    svc, _, _, healthy = setup(sources, "directory")
    default_id = plugin_marketplace.DEFAULT_SOURCE_ID
    plugin_marketplace.ensure_default_source(svc)
    assert not (svc.MARKETPLACES_DIR / default_id).exists()
    save = svc._write_json_file
    with monkeypatch.context() as failure:
        failure.setattr(svc, "_write_json_file", lambda path, data:
                        False if path == svc.SETTINGS_FILE else save(path, data))
        assert svc.remove_marketplace(default_id) is False

    pending = svc._read_json_file(svc.MARKETPLACES_FILE, {})[default_id]
    assert pending["fetchOnDiscovery"] is True
    assert pending["removing"] is True
    assert not pending.get("lastUpdated") and not pending.get("syncError")
    assert [plugin.id for plugin in svc.get_available_plugins(marketplace="sample")] == [healthy.id]
    assert default_id in {source.id for source in svc.get_marketplaces()}
    assert svc._read_json_file(svc.MARKETPLACES_FILE, {})[default_id] == pending
    assert not (svc.MARKETPLACES_DIR / default_id).exists()

    assert svc.remove_marketplace(default_id) is True
    plugin_marketplace.ensure_default_source(svc)
    assert default_id not in svc._read_json_file(svc.MARKETPLACES_FILE, {})
    assert [plugin.id for plugin in svc.get_available_plugins(marketplace="sample")] == [healthy.id]


def test_partial_default_registration_is_not_readded_after_explicit_removal(sources):
    from app.core import plugin_marketplace

    svc, _ = sources
    folder = svc.MARKETPLACES_DIR / plugin_marketplace.DEFAULT_SOURCE_ID
    folder.mkdir(parents=True)
    svc._write_json_file(svc.MARKETPLACES_FILE, {
        plugin_marketplace.DEFAULT_SOURCE_ID: {"installLocation": str(folder)}
    })
    # Startup died after registration but before its applied marker.
    assert not (svc.PLUGINS_DIR / "source_defaults.json").exists()
    assert svc.remove_marketplace(plugin_marketplace.DEFAULT_SOURCE_ID)
    plugin_marketplace.ensure_default_source(svc)
    assert not svc._read_json_file(svc.MARKETPLACES_FILE, {})


def test_remove_many_plugins_and_update_all_finish_without_resurrection(sources, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    root = svc.MARKETPLACES_DIR / "sample"
    manifest_path = root / ".claude-plugin/marketplace.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["plugins"].append({"name": "second", "source": "./second"})
    write(manifest_path, json.dumps(manifest))
    write(root / "second/.claude-plugin/plugin.json", '{"name":"second","version":"1.0.0"}')
    second = root / "second/skills/second/SKILL.md"
    write(second, "---\nname: second\ndescription: Fixture\n---\nFirst revision")
    svc.install_plugin("second@sample")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    write(second, second.read_text().replace("First revision", "Second revision"))
    assert len(svc.check_for_updates(refresh_sources=True)) == 2
    paused, resume, removed = threading.Event(), threading.Event(), threading.Event()
    save, outcomes = svc._write_json_file, {}

    def pause_commit(path, data):
        if path == svc.INSTALLED_FILE and threading.current_thread().name == "all" and not paused.is_set():
            paused.set()
            assert resume.wait(10)
        return save(path, data)

    def update_all():
        try:
            outcomes["all"] = svc.update_all_plugins()
        except Exception as exc:
            outcomes["all"] = exc

    def remove():
        try:
            outcomes["remove"] = svc.remove_marketplace("sample")
        except Exception as exc:
            outcomes["remove"] = exc
        finally:
            removed.set()

    monkeypatch.setattr(svc, "_write_json_file", pause_commit)
    a, b = threading.Thread(target=update_all, name="all"), threading.Thread(target=remove)
    a.start()
    try:
        assert paused.wait(10)
        b.start()
        assert not removed.wait(.2), "Removal did not wait for the in-flight update"
    finally:
        resume.set()
        a.join(10)
        if b.ident is not None:
            b.join(10)
    assert not a.is_alive() and not b.is_alive()
    assert isinstance(outcomes["all"], list) and outcomes["remove"] is True
    assert not any(pid.endswith("@sample") for pid in svc._read_json_file(svc.INSTALLED_FILE, {}).get("plugins", {}))
    assert not any(pid.endswith("@sample") for pid in svc.get_enabled_plugins())
    assert not (svc.CACHE_DIR / "sample").exists()


def test_remove_sharing_violation_stays_closed_until_retry(sources, monkeypatch):
    from app.core import plugin_marketplace

    svc, _, _, info = setup(sources, "directory")
    remove = plugin_marketplace.shutil.rmtree

    def refuse_cache(path, *args, **kwargs):
        if Path(path) == svc.CACHE_DIR / "sample":
            return  # Match rmtree(ignore_errors=True) under a sharing violation.
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(plugin_marketplace.shutil, "rmtree", refuse_cache)
        assert svc.remove_marketplace("sample") is False
    assert svc._read_json_file(svc.MARKETPLACES_FILE, {})["sample"]["removing"]
    assert not svc.get_installation_records(info.id)
    assert info.id not in svc.get_enabled_plugins()
    with pytest.raises(ValueError, match="removal is incomplete"):
        svc.install_plugin(info.id)
    assert svc.remove_marketplace("sample")
    assert not (svc.CACHE_DIR / "sample").exists()


def test_import_never_creates_lock_through_a_destination_symlink(sources, tmp_path, requires_symlinks):
    from fluid.context.settings_import import apply_settings_import, preview_settings_import

    svc, _ = sources
    external = tmp_path / "external"
    write(external / ".claude/plugins/cache/mp/tool/1.0.0/.claude-plugin/plugin.json", '{"name":"tool"}')
    write(external / ".claude/settings.json", '{"enabledPlugins":{"tool@mp":true}}')
    preview = preview_settings_import(sources=("claude",), source_home=external, destination_home=svc.FLUID_DIR.parent)
    outside = tmp_path / "outside"
    outside.mkdir()
    (svc.PLUGINS_DIR / ".locks").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="state path has a symlink"):
        apply_settings_import(preview)
    assert list(outside.iterdir()) == []
    assert not svc.SETTINGS_FILE.exists()




