"""Windows file behaviour in plugin management, simulated so it runs anywhere.

Windows refuses to delete a read-only file (Git's object files are read-only),
refuses to open a file while another operation renames over it, and refuses
that rename while a reader has the file open. POSIX does none of these, so
each test switches the behaviour on with the fixtures below and real git
repositories (see ``test_plugin_source_updates``) do the rest.
"""

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from app.core import fs_utils, mcp_service, plugin_service
from app.core.fs_utils import force_rmtree
from app.core.mcp_service import McpConfigUnreadable, McpService
from app.core.plugin_models import PluginScope
from app.core.plugin_service import PluginConfigUnreadable, PluginService
from tests.core.test_plugin_source_updates import commit, runtime_text, setup, sources, write  # noqa: F401


def _windows_error(winerror, path=None):
    exc = PermissionError(13, "Access is denied", path)
    exc.winerror = winerror  # set by the OS on Windows only
    return exc


def _read_only_files(root):
    return [p for p in Path(root).rglob("*") if p.is_file() and not p.stat().st_mode & stat.S_IWRITE]


@pytest.fixture
def windows_deletes(monkeypatch):
    """Return a switch that makes deleting a read-only file fail like Windows (WinError 5)."""
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
        if stat.S_ISREG(mode) and not mode & stat.S_IWRITE:
            raise _windows_error(5, path)
        return real_unlink(path, dir_fd=dir_fd)

    return lambda: monkeypatch.setattr(os, "unlink", unlink)


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(fs_utils, "_sleep", waited.append)
    return waited


@pytest.fixture
def held_file(monkeypatch, sleeps):
    """Return ``hold(path)``: every first attempt to open, read or replace ``path``
    meets a sharing violation, as when it lands inside another operation's replace.
    """
    calls = {}
    real_open, real_read_bytes, real_replace = open, Path.read_bytes, Path.replace

    def hold(path, *, always=False, winerror=32):
        target = Path(path)

        def blocked(candidate):
            if Path(candidate) != target:
                return False
            calls[target] = calls.get(target, 0) + 1
            return always or calls[target] % 2 == 1

        def fake_open(file, *args, **kwargs):
            if blocked(file):
                raise _windows_error(winerror, str(file))
            return real_open(file, *args, **kwargs)

        def fake_read_bytes(self):
            if blocked(self):
                raise _windows_error(winerror, str(self))
            return real_read_bytes(self)

        def fake_replace(self, destination):
            if blocked(destination):
                raise _windows_error(5, str(destination))
            return real_replace(self, destination)

        monkeypatch.setattr(plugin_service, "open", fake_open, raising=False)
        monkeypatch.setattr(mcp_service, "open", fake_open, raising=False)
        monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
        monkeypatch.setattr(Path, "replace", fake_replace)
        return lambda: calls.get(target, 0)

    return hold


def _backups(path):
    return sorted(path.parent.glob(path.name + ".*.bak"))


# ── #1482: read-only Git files under plugin paths ───────────────────────────


def test_simulated_windows_refuses_plain_rmtree_of_read_only_files(tmp_path, windows_deletes):
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree/object").write_text("x")
    (tmp_path / "tree/object").chmod(0o444)
    windows_deletes()
    with pytest.raises(PermissionError):
        shutil.rmtree(tmp_path / "tree")
    force_rmtree(tmp_path / "tree")
    assert not (tmp_path / "tree").exists()


def test_force_rmtree_reports_or_skips_an_entry_it_cannot_remove(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub/locked").write_text("in use")
    (tree / "other").write_text("x")
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if os.fsdecode(path).endswith("locked"):
            raise _windows_error(32, path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(PermissionError):
        force_rmtree(tree)
    force_rmtree(tree, ignore_errors=True)
    assert (tree / "sub/locked").exists() and not (tree / "other").exists()


@pytest.fixture
def project_sources(sources, tmp_path):
    _, base = sources
    project = tmp_path / "proj"
    project.mkdir()
    return PluginService(project_dir=project), base


def test_project_plugin_from_git_reinstalls_and_uninstalls_on_windows(project_sources, windows_deletes):
    svc, *_ = setup(project_sources, "url")
    first = svc.install_plugin_to_project("sample@sample", PluginScope.PROJECT)
    path = Path(first.install_path)
    assert _read_only_files(path / ".git"), "the clone should keep Git's read-only objects"
    windows_deletes()

    again = svc.install_plugin_to_project("sample@sample", PluginScope.PROJECT)
    assert Path(again.install_path) == path and (path / ".git").is_dir()
    assert svc.get_project_enabled_plugins() == {"sample@project-project": True}

    assert svc.uninstall_project_plugin("sample@project-project")
    assert not path.exists()
    assert svc.get_project_enabled_plugins() == {}


def test_user_plugin_from_git_uninstall_removes_its_clone_on_windows(sources, windows_deletes):
    svc, _, _, info = setup(sources, "url")
    path = Path(svc.get_installation_records(info.id)[0]["installPath"])
    assert _read_only_files(path)
    windows_deletes()
    assert svc.uninstall_plugin(info.id)
    assert not path.exists()


def test_git_subdir_install_removes_its_temporary_clone_on_windows(sources, windows_deletes, tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    windows_deletes()
    svc, *_ = setup(sources, "subdir")
    assert "First revision" in runtime_text(svc)
    assert list(temp.glob("plugin-clone-*")) == []


def test_marketplace_removal_removes_its_clone_on_windows(sources, windows_deletes):
    svc, _, _, info = setup(sources, "marketplace")
    location = Path(svc._read_json_file(svc.MARKETPLACES_FILE, {})["sample"]["installLocation"])
    assert _read_only_files(location / ".git")
    windows_deletes()
    assert svc.remove_marketplace("sample") is True
    assert not location.exists() and not (svc.CACHE_DIR / "sample").exists()
    assert "sample" not in svc._read_json_file(svc.MARKETPLACES_FILE, {})


def test_marketplace_reclone_replaces_a_folder_with_read_only_files_on_windows(sources, windows_deletes):
    svc, *_ = setup(sources, "marketplace")
    location = Path(svc._read_json_file(svc.MARKETPLACES_FILE, {})["sample"]["installLocation"])
    force_rmtree(location / ".git")  # plain rmtree fails on Windows on read-only objects
    stale = location / "stale.txt"
    stale.write_text("left over")
    stale.chmod(0o444)
    windows_deletes()
    assert svc.sync_marketplace("sample")
    assert (location / ".git").is_dir() and not stale.exists()


# ── #1488: a registry read or write overlapping another operation's replace ──


def test_update_and_sync_wait_out_a_registry_replace_without_409_or_backup(sources, held_file, sleeps):
    svc, upstream, skill, info = setup(sources, "marketplace")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    attempts = held_file(svc.MARKETPLACES_FILE)

    assert svc.sync_marketplace("sample")
    svc.update_plugin(info.id)

    assert attempts() > 2 and sleeps, "the simulated hold was never met"
    assert "Second revision" in runtime_text(svc)
    assert _backups(svc.MARKETPLACES_FILE) == []


def test_registry_write_waits_out_a_reader_holding_the_file(sources, held_file):
    svc, _ = sources
    svc._write_json_file(svc.MARKETPLACES_FILE, {"a": {}})
    attempts = held_file(svc.MARKETPLACES_FILE)
    assert svc._write_json_file(svc.MARKETPLACES_FILE, {"b": {}}) is True
    assert attempts() >= 2
    assert json.loads(svc.MARKETPLACES_FILE.read_text()) == {"b": {}}


def test_a_hold_that_never_clears_is_refused_after_a_bounded_wait_without_backup(sources, held_file, sleeps):
    svc, _ = sources
    svc._write_json_file(svc.SETTINGS_FILE, {"enabledPlugins": {}})
    held_file(svc.SETTINGS_FILE, always=True)
    with pytest.raises(PluginConfigUnreadable):
        svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
    assert 0 < sum(sleeps) < 1.5
    assert _backups(svc.SETTINGS_FILE) == []


def test_a_failed_open_never_backs_up_bytes_that_read_fine(sources, held_file):
    """The issue's needless .bak: the open fails, the backup read succeeds."""
    svc, _ = sources
    svc._write_json_file(svc.SETTINGS_FILE, {"enabledPlugins": {}})
    held_file(svc.SETTINGS_FILE, winerror=None)  # a real denial: not retried
    with pytest.raises(PluginConfigUnreadable) as exc:
        svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
    assert exc.value.backup is None and _backups(svc.SETTINGS_FILE) == []


def test_a_genuinely_corrupt_registry_is_still_refused_with_a_backup(sources, held_file):
    svc, _ = sources
    svc.MARKETPLACES_FILE.parent.mkdir(parents=True, exist_ok=True)
    svc.MARKETPLACES_FILE.write_text("{not json")
    held_file(svc.MARKETPLACES_FILE)
    with pytest.raises(PluginConfigUnreadable) as exc:
        svc._check_files_replaceable(svc.MARKETPLACES_FILE)
    assert exc.value.backup is not None and exc.value.backup.read_text() == "{not json"
    with pytest.raises(PluginConfigUnreadable):
        svc._read_json_file(svc.MARKETPLACES_FILE, {}, for_update=True)
    assert svc.MARKETPLACES_FILE.read_text() == "{not json"


def test_mcp_config_read_and_write_wait_out_a_replace(tmp_path, held_file):
    svc = McpService(project_dir=tmp_path)
    config = tmp_path / ".mcp.json"
    config.write_text('{"mcpServers": {}}')
    held_file(config)
    assert svc._read_json_file(config, {}, for_update=True) == {"mcpServers": {}}
    assert svc._write_json_file(config, {"mcpServers": {"a": {}}}) is True
    assert json.loads(config.read_text()) == {"mcpServers": {"a": {}}}
    held_file(config, always=True, winerror=None)
    with pytest.raises(McpConfigUnreadable):
        svc._read_json_file(config, {}, for_update=True)


def test_install_settings_preservation_waits_out_a_replace(sources, held_file):
    """Reading, comparing and restoring settings.json around a failed install."""
    from app.core.plugin_install import _preserve_install_settings

    svc, _ = sources
    svc._write_json_file(svc.SETTINGS_FILE, {"enabledPlugins": {"a@b": True}})
    attempts = held_file(svc.SETTINGS_FILE)
    with pytest.raises(RuntimeError, match="activation failed"):
        with _preserve_install_settings(svc):
            svc.SETTINGS_FILE.write_text('{"enabledPlugins": {}}')
            raise RuntimeError("activation failed")
    assert attempts() >= 6, "read, compare and restore should each meet the hold"
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {"enabledPlugins": {"a@b": True}}
    assert list(svc.SETTINGS_FILE.parent.glob(".plugin-settings-restore-*")) == []


def test_seed_clone_cleanup_removes_read_only_git_files_on_windows(tmp_path, windows_deletes):
    from app.core.checkout_manager import _rmtree_quiet

    seed = tmp_path / ".seed-x"
    (seed / ".git/objects/ab").mkdir(parents=True)
    (seed / ".git/objects/ab/cdef").write_text("blob")
    (seed / ".git/objects/ab/cdef").chmod(0o444)
    windows_deletes()
    _rmtree_quiet(str(seed))
    assert not seed.exists()


# ── The backoff never sleeps on the event loop ──────────────────────────────


def test_the_retry_never_sleeps_on_an_event_loop_thread(sleeps):
    import asyncio

    attempts = []

    def held():
        attempts.append(1)
        raise _windows_error(32)

    async def on_loop():
        with pytest.raises(PermissionError):
            fs_utils.retry_sharing_violation(held)

    asyncio.run(on_loop())
    assert len(attempts) == 1 and sleeps == []  # the one attempt it always made

    with pytest.raises(PermissionError):
        fs_utils.retry_sharing_violation(held)
    assert len(attempts) == 1 + len(fs_utils._SHARING_RETRY_DELAYS) + 1 and len(sleeps) == len(fs_utils._SHARING_RETRY_DELAYS)


def test_plugin_and_mcp_handlers_run_one_at_a_time_on_one_thread_off_the_loop(monkeypatch):
    import asyncio
    import threading
    import time

    from app.api import mcp as mcp_api
    from app.api import plugins as plugins_api
    from app.api import project_plugins as project_api

    running, overlaps, threads = [0], [], set()

    def work():
        threads.add(threading.current_thread())
        running[0] += 1
        overlaps.append(running[0])
        time.sleep(0.05)
        running[0] -= 1

    class Service:
        def get_all_servers_with_builtins(self):
            work()
            return []

        def sync_marketplace(self, marketplace_id):
            work()

        def get_project_installed_plugins(self):
            work()
            return []

    monkeypatch.setattr(mcp_api, "get_mcp_service", lambda *a, **k: Service())
    monkeypatch.setattr(plugins_api, "get_plugin_service", lambda *a, **k: Service())
    monkeypatch.setattr(project_api, "validate_project_access", lambda request, project_id: {})
    monkeypatch.setattr(project_api, "_get_project_plugin_service", lambda project: Service())

    async def main():
        await asyncio.gather(
            mcp_api.list_global_mcp_servers(request=None, token="t"),
            plugins_api.sync_marketplace("m", token="t"),
            plugins_api.sync_marketplace("n", token="t"),
            project_api.list_project_plugins("p", request=None, token="t"),
        )
        return threading.current_thread()

    loop_thread = asyncio.run(main())
    assert len(overlaps) == 4 and max(overlaps) == 1
    assert len(threads) == 1 and loop_thread not in threads


def test_cold_service_initialisation_runs_off_the_event_loop(sources, tmp_path, monkeypatch):
    import asyncio
    import threading

    from app.api import plugins as plugins_api
    from app.api import project_plugins as project_api

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(plugin_service, "_plugin_services", {})  # nothing cached yet
    real_ensure = plugin_service._mp_mod.ensure_default_source
    initialised = []

    def ensure_default_source(svc):
        initialised.append((svc.project_dir, threading.current_thread()))
        return real_ensure(svc)

    monkeypatch.setattr(plugin_service._mp_mod, "ensure_default_source", ensure_default_source)
    monkeypatch.setattr(project_api, "validate_project_access", lambda request, project_id: {})
    monkeypatch.setattr(project_api, "get_project_path_from_dict", lambda project_dict: project)

    async def main():
        listed = await project_api.list_project_plugins("p", request=None, token="t")
        enabled = await plugins_api.get_enabled_plugins(token="t")
        return threading.current_thread(), listed, enabled

    loop_thread, listed, enabled = asyncio.run(main())
    assert listed == [] and enabled.enabled_plugins == {}
    assert [path for path, _ in initialised] == [project, None]
    assert all(thread is not loop_thread for _, thread in initialised)


def test_a_project_plugin_list_never_races_its_uninstall(project_sources, monkeypatch):
    import asyncio
    import time

    from app.api import project_plugins as project_api
    from app.core import plugin_install

    svc, *_ = setup(project_sources, "directory")
    svc.install_plugin_to_project("sample@sample", PluginScope.LOCAL)
    real_rmtree = plugin_install._force_rmtree

    def slow_rmtree(*args, **kwargs):
        time.sleep(0.3)  # a list scheduled meanwhile must not see a half-removed folder
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(plugin_install, "_force_rmtree", slow_rmtree)
    monkeypatch.setattr(project_api, "validate_project_access", lambda request, project_id: {})
    monkeypatch.setattr(project_api, "_get_project_plugin_service", lambda project: svc)

    async def main():
        uninstall = asyncio.ensure_future(
            project_api.uninstall_project_plugin("p", "sample@project-local", request=None, token="t"))
        await asyncio.sleep(0.05)
        listed = await project_api.list_project_plugins("p", request=None, token="t")
        await uninstall
        return listed

    assert asyncio.run(main()) == []
    assert svc.get_project_enabled_plugins() == {}
