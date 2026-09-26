"""An unreadable plugin settings/registry file is never overwritten.

``PluginService`` used to read a corrupt, non-UTF-8, non-object or
permission-denied ``settings.json`` / ``installed_plugins.json`` /
``known_marketplaces.json`` as ``{}``, and the next enable/disable/install/
uninstall/marketplace change wrote that back over the whole file. Reads
still tolerate such a file; writes refuse with a 409
(:class:`PluginConfigUnreadable`, the plugin sibling of
``McpConfigUnreadable``) and keep a ``.bak`` of bytes that failed to parse.
"""

import json
import os

import pytest

from app.core.mcp_service import McpConfigUnreadable
from app.core.plugin_models import AutoUpdateSettings, PluginScope
from app.core.plugin_service import PluginConfigUnreadable, PluginService

_BAD_BODIES = ["{broken", b"\xff\xfe{}", "[1, 2]", "null"]
_BAD_IDS = ["corrupt-json", "non-utf8", "list", "null"]


def _write_raw(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


def _baks(path):
    """The timestamped backups ``<name>.<stamp>.bak`` of ``path``, oldest first."""
    return PluginService._backups_of(path)


def _bak(path):
    """The newest backup of ``path`` (a path that does not exist when there is none)."""
    found = _baks(path)
    return found[-1] if found else path.with_name(path.name + ".no-backup")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid_dir = tmp_path / "fluid"
    fluid_dir.mkdir()
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: fluid_dir)
    project = tmp_path / "project"
    project.mkdir()
    return PluginService(project_dir=project)


@pytest.fixture
def unreadable(tmp_path):
    """Make a file permission-denied; skip where permissions are not enforced."""
    changed = []

    def make(path):
        _write_raw(path, json.dumps({"enabledPlugins": {"keep@m": True}}))
        path.chmod(0)
        changed.append(path)
        try:
            path.read_bytes()
        except PermissionError:
            return
        pytest.skip("file permissions are not enforced for this user")

    yield make
    for path in changed:
        path.chmod(0o600)


def test_error_is_a_409_sibling_of_the_mcp_error(tmp_path):
    err = PluginConfigUnreadable(tmp_path / "settings.json")
    assert isinstance(err, McpConfigUnreadable)
    assert err.status_code == 409
    assert err.detail == McpConfigUnreadable(tmp_path / "settings.json").detail


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
@pytest.mark.parametrize("change", ["enable", "disable"])
def test_user_settings_are_never_overwritten(svc, body, change):
    _write_raw(svc.SETTINGS_FILE, body)
    before = svc.SETTINGS_FILE.read_bytes()
    act = svc.enable_plugin if change == "enable" else svc.disable_plugin
    with pytest.raises(PluginConfigUnreadable) as exc:
        act("demo@market")
    assert exc.value.status_code == 409
    assert str(svc.SETTINGS_FILE) in exc.value.detail
    assert svc.SETTINGS_FILE.read_bytes() == before
    # Bytes that failed to parse are kept for the owner, and named.
    assert _bak(svc.SETTINGS_FILE).read_bytes() == before
    assert str(_bak(svc.SETTINGS_FILE)) in exc.value.detail
    # Reads still show empty rather than failing.
    assert svc.get_enabled_plugins() == {}
    assert svc.get_installed_plugins() == []


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
@pytest.mark.parametrize("scope_marker", ["project-local", "project-project"])
def test_project_settings_are_never_overwritten(svc, body, scope_marker):
    scope = PluginScope.LOCAL if scope_marker == "project-local" else PluginScope.PROJECT
    settings_file = svc._get_project_settings_file(scope)
    _write_raw(settings_file, body)
    before = settings_file.read_bytes()
    with pytest.raises(PluginConfigUnreadable):
        svc.enable_project_plugin(f"demo@{scope_marker}")
    with pytest.raises(PluginConfigUnreadable):
        svc.disable_project_plugin(f"demo@{scope_marker}")
    assert settings_file.read_bytes() == before
    assert svc.get_project_enabled_plugins() == {}


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_uninstall_does_not_overwrite_unreadable_settings(svc, body):
    """Delegate read-modify-writes (plugin_install) are stopped at the write."""
    svc.INSTALLED_FILE.write_text(json.dumps(
        {"version": 2, "plugins": {"demo@market": [{"scope": "user"}]}}
    ))
    _write_raw(svc.SETTINGS_FILE, body)
    before = svc.SETTINGS_FILE.read_bytes()
    with pytest.raises(PluginConfigUnreadable):
        svc.uninstall_plugin("demo@market")
    assert svc.SETTINGS_FILE.read_bytes() == before


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_marketplace_registry_writes_are_refused(svc, body):
    """remove_marketplace (plugin_marketplace) must not clobber the installed registry,
    and a marketplace write must not clobber a corrupt known_marketplaces.json."""
    svc.MARKETPLACES_FILE.write_text(json.dumps(
        {"mkt": {"source": {"source": "git", "url": "u"}, "installLocation": ""}}
    ))
    _write_raw(svc.INSTALLED_FILE, body)
    installed_before = svc.INSTALLED_FILE.read_bytes()
    with pytest.raises(PluginConfigUnreadable):
        svc.remove_marketplace("mkt")
    assert svc.INSTALLED_FILE.read_bytes() == installed_before

    _write_raw(svc.MARKETPLACES_FILE, body)
    marketplaces_before = svc.MARKETPLACES_FILE.read_bytes()
    assert svc.get_marketplaces() == []
    # The write add_marketplace performs after cloning.
    with pytest.raises(PluginConfigUnreadable):
        svc._write_json_file(svc.MARKETPLACES_FILE, {"new": {}})
    assert svc.MARKETPLACES_FILE.read_bytes() == marketplaces_before


@pytest.mark.parametrize("target", ["settings", "installed"])
def test_permission_denied_is_refused_without_backup(svc, unreadable, target):
    path = svc.SETTINGS_FILE if target == "settings" else svc.INSTALLED_FILE
    unreadable(path)
    # Reads tolerate it.
    assert svc.get_enabled_plugins() == {}
    assert svc.get_installed_plugins() == []
    with pytest.raises(PluginConfigUnreadable) as exc:
        if target == "settings":
            svc.enable_plugin("demo@market")
        else:
            svc._write_json_file(path, {"version": 2, "plugins": {}})
    assert exc.value.status_code == 409
    assert exc.value.backup is None
    assert _baks(path) == []
    path.chmod(0o600)
    assert json.loads(path.read_text()) == {"enabledPlugins": {"keep@m": True}}


def test_missing_settings_file_is_still_created(svc):
    assert not svc.SETTINGS_FILE.exists()
    assert svc.enable_plugin("demo@market") is True
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {"enabledPlugins": {"demo@market": True}}
    assert svc.disable_plugin("demo@market") is True
    assert svc.get_enabled_plugins() == {"demo@market": False}
    assert _baks(svc.SETTINGS_FILE) == []


def test_missing_project_settings_file_is_still_created(svc):
    settings_file = svc._get_project_settings_file(PluginScope.LOCAL)
    assert not settings_file.exists()
    assert svc.enable_project_plugin("demo@project-local") is True
    assert json.loads(settings_file.read_text()) == {"enabledPlugins": {"demo@project-local": True}}


def test_valid_settings_are_merged_not_replaced(svc):
    svc.SETTINGS_FILE.write_text(json.dumps({"other": 1, "enabledPlugins": {"a@m": True}}))
    assert svc.disable_plugin("b@m") is True
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {
        "other": 1, "enabledPlugins": {"a@m": True, "b@m": False},
    }


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_auto_update_settings_replace_a_corrupt_file_keeping_a_backup(svc, body):
    """The auto-update file is always written whole, so a corrupt one is backed
    up and replaced (else it could never be fixed from the app)."""
    _write_raw(svc.AUTO_UPDATE_FILE, body)
    before = svc.AUTO_UPDATE_FILE.read_bytes()
    assert svc.save_auto_update_settings(AutoUpdateSettings(enabled=True, interval_hours=12)) is True
    assert _bak(svc.AUTO_UPDATE_FILE).read_bytes() == before
    assert json.loads(svc.AUTO_UPDATE_FILE.read_text())["intervalHours"] == 12


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX permissions only")
def test_auto_update_settings_refuse_a_permission_denied_file(svc, unreadable):
    unreadable(svc.AUTO_UPDATE_FILE)
    with pytest.raises(PluginConfigUnreadable):
        svc.save_auto_update_settings(AutoUpdateSettings())



# ── R15-1: the 409 comes before any file is deleted, cloned or copied ───────


def _snapshot(root):
    """Relative path -> bytes for every file under ``root``, ignoring backups."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(".bak")
    }


def _local_marketplace(svc, name="market", plugin="demo"):
    root = svc.MARKETPLACES_DIR / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": name, "plugins": [{"name": plugin, "source": f"./plugins/{plugin}"}]}
    ))
    src = root / "plugins" / plugin
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin, "version": "1.0.0"}))
    (src / "commands").mkdir()
    (src / "commands" / "hello.md").write_text("# hello\n")
    svc.MARKETPLACES_FILE.write_text(json.dumps(
        {name: {"source": {"source": "git", "url": "u"}, "installLocation": str(root)}}
    ))
    return root


def _installed_demo(svc):
    cache = svc.CACHE_DIR / "market" / "demo" / "1"
    cache.mkdir(parents=True)
    (cache / "x").write_text("x")
    svc.INSTALLED_FILE.write_text(json.dumps(
        {"version": 2, "plugins": {"demo@market": [{"scope": "user", "installPath": str(cache)}]}}
    ))
    svc.SETTINGS_FILE.write_text(json.dumps({"enabledPlugins": {"demo@market": True}}))
    return cache


@pytest.mark.parametrize("target", ["settings", "installed"])
def test_uninstall_refused_up_front_leaves_everything_and_can_be_retried(svc, target):
    cache = _installed_demo(svc)
    bad = svc.SETTINGS_FILE if target == "settings" else svc.INSTALLED_FILE
    good_text = bad.read_text()
    bad.write_text("{broken")
    before = _snapshot(svc.FLUID_DIR)
    with pytest.raises(PluginConfigUnreadable):
        svc.uninstall_plugin("demo@market")
    assert cache.exists()
    assert _snapshot(svc.FLUID_DIR) == before
    # Fixed file: the retry finds the plugin still installed and removes it.
    bad.write_text(good_text)
    assert svc.uninstall_plugin("demo@market") is True
    assert not cache.exists()
    assert "demo@market" not in json.loads(svc.INSTALLED_FILE.read_text())["plugins"]
    assert "demo@market" not in svc.get_enabled_plugins()


@pytest.mark.parametrize("target", ["settings", "installed", "marketplaces"])
def test_remove_marketplace_refused_up_front_leaves_everything_and_can_be_retried(svc, tmp_path, target):
    clone = tmp_path / "mkt-clone"
    clone.mkdir()
    (clone / "f").write_text("x")
    svc.MARKETPLACES_FILE.write_text(json.dumps(
        {"market": {"source": {"source": "git", "url": "u"}, "installLocation": str(clone)}}
    ))
    cache = _installed_demo(svc)
    bad = {"settings": svc.SETTINGS_FILE, "installed": svc.INSTALLED_FILE,
           "marketplaces": svc.MARKETPLACES_FILE}[target]
    good_text = bad.read_text()
    bad.write_text("{broken")
    before = _snapshot(svc.FLUID_DIR)
    with pytest.raises(PluginConfigUnreadable):
        svc.remove_marketplace("market")
    assert clone.exists() and cache.exists()
    assert _snapshot(svc.FLUID_DIR) == before
    bad.write_text(good_text)
    assert svc.remove_marketplace("market") is True
    assert not clone.exists() and not cache.exists()
    assert svc.get_marketplaces() == []
    assert json.loads(svc.INSTALLED_FILE.read_text())["plugins"] == {}
    assert "demo@market" not in svc.get_enabled_plugins()


@pytest.mark.parametrize("target", ["settings", "installed"])
def test_install_refused_up_front_copies_nothing_and_can_be_retried(svc, target):
    _local_marketplace(svc)
    bad = svc.SETTINGS_FILE if target == "settings" else svc.INSTALLED_FILE
    bad.write_text("{broken")
    before = _snapshot(svc.FLUID_DIR)
    with pytest.raises(PluginConfigUnreadable):
        svc.install_plugin("demo@market")
    assert not (svc.CACHE_DIR / "market").exists()
    assert _snapshot(svc.FLUID_DIR) == before
    bad.unlink()
    info = svc.install_plugin("demo@market")
    assert info.id == "demo@market" and info.enabled
    assert (svc.CACHE_DIR / "market" / "demo" / "1.0.0" / "commands" / "hello.md").exists()


def test_update_with_corrupt_registry_is_409_not_404(svc):
    _local_marketplace(svc)
    svc.INSTALLED_FILE.write_text("{broken")
    with pytest.raises(PluginConfigUnreadable):
        svc.update_plugin("demo@market")


def test_add_marketplace_refused_before_cloning(svc, monkeypatch):
    svc.MARKETPLACES_FILE.write_text("{broken")

    def no_git(*_a, **_k):
        raise AssertionError("git must not run")

    monkeypatch.setattr("app.core.plugin_marketplace.subprocess.run", no_git)
    with pytest.raises(PluginConfigUnreadable):
        svc.add_marketplace("https://example.invalid/repo.git")
    with pytest.raises(PluginConfigUnreadable):
        svc.sync_marketplace("repo")
    assert not (svc.MARKETPLACES_DIR / "repo").exists()


@pytest.mark.parametrize("scope_marker", ["project-local", "project-project"])
def test_project_uninstall_refused_before_deleting_files(svc, scope_marker):
    scope = PluginScope.LOCAL if scope_marker == "project-local" else PluginScope.PROJECT
    plugin_dir = svc.get_plugins_dir_for_scope(scope) / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "README.md").write_text("demo")
    settings_file = svc._get_project_settings_file(scope)
    _write_raw(settings_file, "{broken")
    with pytest.raises(PluginConfigUnreadable):
        svc.uninstall_project_plugin(f"demo@{scope_marker}")
    assert (plugin_dir / "README.md").read_text() == "demo"
    settings_file.write_text("{}")
    assert svc.uninstall_project_plugin(f"demo@{scope_marker}") is True
    assert not plugin_dir.exists()


def test_project_install_refused_before_replacing_files(svc):
    _local_marketplace(svc)
    plugin_dir = svc.get_plugins_dir_for_scope(PluginScope.LOCAL) / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "mine.md").write_text("local edit")
    settings_file = svc._get_project_settings_file(PluginScope.LOCAL)
    _write_raw(settings_file, "{broken")
    with pytest.raises(PluginConfigUnreadable):
        svc.install_plugin_to_project("demo@market", PluginScope.LOCAL)
    assert (plugin_dir / "mine.md").read_text() == "local edit"


# ── R15-3: backups are timestamped, never overwritten, at most five ────────


def test_a_second_refusal_keeps_the_first_backup(svc):
    original = '{"enabledPlugins": {"a@m": true}  TRUNCATED'
    svc.SETTINGS_FILE.write_text(original)
    with pytest.raises(PluginConfigUnreadable) as first:
        svc.enable_plugin("x@m")
    first_bak = first.value.backup
    assert first_bak.read_text() == original
    assert first_bak.name.startswith("settings.json.") and first_bak.name.endswith(".bak")
    # The owner edits it and it is still invalid; the next write is refused too.
    svc.SETTINGS_FILE.write_text("")
    with pytest.raises(PluginConfigUnreadable) as second:
        svc.disable_plugin("x@m")
    assert second.value.backup != first_bak
    assert first_bak.read_text() == original
    assert second.value.backup.read_text() == ""
    assert str(second.value.backup) in second.value.detail


def test_same_bytes_reuse_the_existing_backup(svc):
    svc.SETTINGS_FILE.write_text("{broken")
    backups = set()
    for _ in range(3):
        with pytest.raises(PluginConfigUnreadable) as exc:
            svc.enable_plugin("x@m")
        backups.add(exc.value.backup)
    assert len(backups) == 1
    assert _baks(svc.SETTINGS_FILE) == list(backups)


def test_a_hand_made_bak_is_never_touched(svc):
    hand = svc.SETTINGS_FILE.with_name("settings.json.bak")
    hand.write_text("owner's copy")
    svc.SETTINGS_FILE.write_text("{broken")
    with pytest.raises(PluginConfigUnreadable) as exc:
        svc.enable_plugin("x@m")
    assert exc.value.backup != hand
    assert hand.read_text() == "owner's copy"


def test_at_most_five_backups_are_kept_per_file(svc):
    for i in range(8):
        svc.SETTINGS_FILE.write_text(f"{{broken {i}")
        with pytest.raises(PluginConfigUnreadable):
            svc.enable_plugin("x@m")
    kept = _baks(svc.SETTINGS_FILE)
    assert [b.read_text() for b in kept] == [f"{{broken {i}" for i in range(3, 8)]
    # Another file's backups are counted separately.
    svc.INSTALLED_FILE.write_text("{broken")
    with pytest.raises(PluginConfigUnreadable):
        svc._write_json_file(svc.INSTALLED_FILE, {})
    assert len(_baks(svc.INSTALLED_FILE)) == 1
    assert len(_baks(svc.SETTINGS_FILE)) == 5


# ─── R16-2: a file caught mid-write after the up-front check ────────────────


def _catch_mid_write(monkeypatch, path):
    """The first text read of ``path`` sees a truncated file (another program
    was mid-write); the bytes on disk stay whole, as when that writer finishes
    before ours. The up-front check reads bytes, so it is not affected."""
    import builtins
    import io

    real_open = builtins.open
    state = {"reads": 0}

    def flaky_open(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and "r" in mode and "b" not in mode:
            state["reads"] += 1
            if state["reads"] == 1:
                return io.StringIO('{"enabledPlugins": {"keep@o')
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("app.core.plugin_service.open", flaky_open, raising=False)
    return state


def _project_demo(svc):
    plugin_dir = svc.get_plugins_dir_for_scope(PluginScope.LOCAL) / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "x").write_text("x")
    return plugin_dir


_MID_WRITE_CASES = [
    ("remove_marketplace", "settings"),
    ("remove_marketplace", "installed"),
    ("uninstall", "settings"),
    ("uninstall", "installed"),
    ("install", "installed"),
    ("update", "settings"),
    ("project_uninstall", "project"),
]


@pytest.mark.parametrize(
    "operation,target", _MID_WRITE_CASES, ids=[f"{o}-{t}" for o, t in _MID_WRITE_CASES]
)
def test_a_file_caught_mid_write_after_the_check_is_not_rewritten(
    svc, tmp_path, monkeypatch, operation, target
):
    """R16-2: the delegates re-read each file after the up-front check. A
    tolerant re-read that caught the file mid-write got ``{}`` and wrote what
    it built from that back, dropping every other key and record. Every read
    that precedes a write now refuses instead."""
    _local_marketplace(svc)
    _installed_demo(svc)
    svc.SETTINGS_FILE.write_text(json.dumps(
        {"enabledPlugins": {"demo@market": True, "keep@other": True},
         "theme": "dark", "env": {"X": "1"}}
    ))
    installed = json.loads(svc.INSTALLED_FILE.read_text())
    installed["plugins"]["keep@other"] = [{"scope": "user", "installPath": "/nowhere"}]
    svc.INSTALLED_FILE.write_text(json.dumps(installed))
    project_settings = svc._get_project_settings_file(PluginScope.LOCAL)
    project_settings.parent.mkdir(parents=True, exist_ok=True)
    project_settings.write_text(json.dumps(
        {"enabledPlugins": {"demo@project-local": True, "keep@project-local": True},
         "theme": "dark"}
    ))
    if operation == "project_uninstall":
        _project_demo(svc)

    path = {"settings": svc.SETTINGS_FILE, "installed": svc.INSTALLED_FILE,
            "project": project_settings}[target]
    good = path.read_bytes()
    state = _catch_mid_write(monkeypatch, path)
    run = {
        "remove_marketplace": lambda: svc.remove_marketplace("market"),
        "uninstall": lambda: svc.uninstall_plugin("demo@market"),
        "install": lambda: svc.install_plugin("demo@market"),
        "update": lambda: svc.update_plugin("demo@market"),
        "project_uninstall": lambda: svc.uninstall_project_plugin("demo@project-local"),
    }[operation]
    with pytest.raises(PluginConfigUnreadable):
        run()
    assert state["reads"] >= 1  # the mid-write read was reached
    assert path.read_bytes() == good
