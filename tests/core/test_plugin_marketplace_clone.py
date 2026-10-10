"""Marketplace clones: the URL is never parsed as a git option or a remote
helper, and URL credentials never reach the error text.

Hermetic: git is a fake that records its argv (no network, no clone).
"""

import json
import subprocess
from pathlib import Path

import pytest

from app.core import plugin_marketplace
from app.core.plugin_service import PluginService


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid = tmp_path / "fluid"
    fluid.mkdir()
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: fluid)
    project = tmp_path / "proj"
    project.mkdir()
    return PluginService(project_dir=project)


@pytest.fixture
def fake_git(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", run)
    return calls


def _broken_marketplace(svc, url):
    """A registered marketplace whose directory is not a git repo (forces a re-clone)."""
    root = svc.MARKETPLACES_DIR / "mp"
    root.mkdir(parents=True)
    data = {"mp": {"source": {"source": "git", "url": url}, "installLocation": str(root)}}
    svc._write_json_file(svc.MARKETPLACES_FILE, data)
    return root


def _url_after_separator(cmd):
    assert cmd[:2] == ["git", "clone"]
    return cmd[cmd.index("--") + 1]


def test_add_marketplace_separates_the_url(svc, fake_git):
    plugin_marketplace.add_marketplace(svc, "https://example.com/org/mp.git")

    assert len(fake_git) == 1
    assert _url_after_separator(fake_git[0]) == "https://example.com/org/mp.git"


def test_sync_reclone_separates_the_url(svc, fake_git):
    _broken_marketplace(svc, "https://example.com/org/mp.git")

    plugin_marketplace.sync_marketplace(svc, "mp")

    assert len(fake_git) == 1
    assert _url_after_separator(fake_git[0]) == "https://example.com/org/mp.git"


def test_sync_clones_a_marketplace_whose_folder_is_missing(svc, fake_git):
    root = svc.MARKETPLACES_DIR / "mp"
    data = {"mp": {"source": {"source": "git", "url": "https://example.com/org/mp.git"},
                   "installLocation": str(root)}}
    svc._write_json_file(svc.MARKETPLACES_FILE, data)
    assert not root.exists()

    assert plugin_marketplace.sync_marketplace(svc, "mp") is True

    assert len(fake_git) == 1
    assert _url_after_separator(fake_git[0]) == "https://example.com/org/mp.git"
    assert fake_git[0][-1] == str(root)


def test_sync_missing_folder_without_source_url_raises(svc, fake_git):
    root = svc.MARKETPLACES_DIR / "mp"
    data = {"mp": {"source": {"source": "git"}, "installLocation": str(root)}}
    svc._write_json_file(svc.MARKETPLACES_FILE, data)

    with pytest.raises(RuntimeError, match="Marketplace directory not found"):
        plugin_marketplace.sync_marketplace(svc, "mp")
    assert fake_git == []


@pytest.mark.parametrize("stored", ["--upload-pack=touch /tmp/pwn", "ext::sh -c id", "file:///etc"])
def test_sync_reclone_never_clones_an_option_or_helper_url(svc, fake_git, stored):
    """A stored URL outside the allowed transports is read as GitHub shorthand."""
    _broken_marketplace(svc, stored)

    plugin_marketplace.sync_marketplace(svc, "mp")

    assert _url_after_separator(fake_git[0]).startswith("https://github.com/")


@pytest.mark.parametrize("op", ["add", "sync"])
def test_clone_error_text_has_no_url_credentials(svc, monkeypatch, op):
    url = "https://alice:s3cr3t@example.com/org/mp.git"

    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            128, cmd, stderr=f"fatal: unable to access '{url}': 403\n".encode()
        )

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", run)
    with pytest.raises((RuntimeError, ValueError)) as exc:
        if op == "add":
            # The marketplace URL pattern refuses userinfo; the refusal must not echo it.
            plugin_marketplace.add_marketplace(svc, url)
        else:
            _broken_marketplace(svc, url)
            plugin_marketplace.sync_marketplace(svc, "mp")
    assert "s3cr3t" not in str(exc.value)
    assert "https://***@example.com" in str(exc.value)


@pytest.fixture
def first_boot(svc, monkeypatch):
    from app.core import plugin_service

    monkeypatch.setattr(plugin_service, "_plugin_services", {})
    for module in ("app.core.plugin_models", "app.core.connector_packages"):
        monkeypatch.setattr(f"{module}._get_fluid_dir", lambda: svc.FLUID_DIR)
    monkeypatch.setattr(PluginService, "_get_marketplace_head_version", lambda *_: None)
    return plugin_service.get_plugin_service


def test_default_source_registered_once_on_first_use(first_boot, svc, fake_git):
    service = first_boot()
    sources = service.get_marketplaces()
    assert [(m.id, m.url, m.display_name) for m in sources] == [(
        plugin_marketplace.DEFAULT_SOURCE_ID, plugin_marketplace.DEFAULT_SOURCE_URL,
        "Anthropic official plugins",
    )]
    assert fake_git == []  # Boot does not wait on the network.
    assert first_boot() is service
    assert (svc.PLUGINS_DIR / "source_defaults.json").is_file()
    assert sources[0].error  # Not downloaded yet, not an empty catalog.


def test_upgrade_preserves_existing_sources(first_boot, svc):
    _broken_marketplace(svc, "https://example.com/team.git")
    service = first_boot()
    assert {m.id for m in service.get_marketplaces()} == {"mp", plugin_marketplace.DEFAULT_SOURCE_ID}


def test_upgrade_does_not_duplicate_source_under_another_name(first_boot, svc):
    _broken_marketplace(svc, plugin_marketplace.DEFAULT_SOURCE_URL.removesuffix(".git"))
    assert [m.id for m in first_boot().get_marketplaces()] == ["mp"]


def test_removed_default_stays_removed_after_restart(first_boot, svc, monkeypatch):
    first_boot().remove_marketplace(plugin_marketplace.DEFAULT_SOURCE_ID)
    monkeypatch.setattr("app.core.plugin_service._plugin_services", {})
    assert first_boot().get_marketplaces() == []
    assert first_boot().get_available_plugins()  # Built-in connectors remain.
    assert first_boot().get_marketplaces() == []


def test_unreadable_defaults_marker_is_not_overwritten(first_boot, svc):
    from app.core.plugin_service import PluginConfigUnreadable

    marker = svc.PLUGINS_DIR / "source_defaults.json"
    marker.write_text("broken", encoding="utf-8")
    with pytest.raises(PluginConfigUnreadable):
        first_boot()
    assert marker.read_text() == "broken"
    assert not svc.MARKETPLACES_FILE.exists()


def _clone_catalog(cmd, **kwargs):
    assert cmd[:2] == ["git", "clone"]
    target = Path(cmd[-1])
    assert target.is_absolute()
    manifest = target / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"plugins": [
        {"name": "design", "source": "./plugins/design", "description": "Design helpers"},
    ]}))
    skill = target / "plugins" / "design" / "skills" / "design" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: design\ndescription: Design helpers\n---\nDesign.\n")
    return subprocess.CompletedProcess(cmd, 0, b"", b"")


def test_discovery_fetches_default_and_tools_can_find_it(first_boot, monkeypatch):
    from app.mcp.plugin_tools import find_plugins

    calls = []

    def clone(cmd, **kwargs):
        calls.append(cmd)
        return _clone_catalog(cmd, **kwargs)

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", clone)
    service = first_boot()
    assert "design@claude-plugins-official (available)" in find_plugins("Design")
    assert service.get_marketplaces()[0].error is None
    service.get_available_plugins()
    assert len(calls) == 1
    assert _url_after_separator(calls[0]) == plugin_marketplace.DEFAULT_SOURCE_URL
    # The producer fetched the package, not a pre-seeded available-plugin row.
    plugin = service.install_plugin("design@claude-plugins-official", enable=False)
    assert plugin.has_skills
    assert not plugin.enabled


def test_unreachable_source_is_reported_then_retry_recovers(first_boot, monkeypatch):
    from app.mcp.plugin_tools import find_plugins

    calls = []

    def fail(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(128, cmd, stderr=b"offline secret-not-for-display")

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", fail)
    service = first_boot()
    text = find_plugins("design")
    assert "search is incomplete" in text
    assert "claude-plugins-official is unavailable" in text
    assert "Retry Sync" in text
    assert "secret-not-for-display" not in text
    assert "Build one" not in text
    assert service.get_marketplaces()[0].error
    service.get_available_plugins()
    assert find_plugins("design") == text
    assert len(calls) == 1  # Reopening Explore/FindPlugins does not retry offline.

    def recover(cmd, **kwargs):
        calls.append(cmd)
        return _clone_catalog(cmd, **kwargs)

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", recover)
    service.sync_marketplace(plugin_marketplace.DEFAULT_SOURCE_ID)
    assert len(calls) == 2  # The owner's explicit Sync retries and clears the error.
    assert "design@claude-plugins-official (available)" in find_plugins("design")
    assert len(calls) == 2
    assert service.get_marketplaces()[0].error is None


def test_failed_sync_keeps_cached_plugins_and_reports_incomplete_search(first_boot, monkeypatch):
    from app.mcp.plugin_tools import find_plugins

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", _clone_catalog)
    service = first_boot()
    service.get_available_plugins()
    source = service.MARKETPLACES_DIR / plugin_marketplace.DEFAULT_SOURCE_ID
    (source / ".git").mkdir()

    def fail(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(plugin_marketplace.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="timed out"):
        service.sync_marketplace(plugin_marketplace.DEFAULT_SOURCE_ID)
    text = find_plugins("design")
    assert "design@claude-plugins-official (available)" in text
    assert "Search incomplete" in text
