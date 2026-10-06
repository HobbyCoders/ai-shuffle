"""Marketplace clones: the URL is never parsed as a git option or a remote
helper, and URL credentials never reach the error text.

Hermetic: git is a fake that records its argv (no network, no clone).
"""

import subprocess

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
