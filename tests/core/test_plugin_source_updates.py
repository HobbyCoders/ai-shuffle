"""Real git installs and updates, including constant release versions.

Git's per-test URL rewrite supplies disposable repositories without network
access. No clone, checkout, plugin install, or runtime reader is mocked.
"""

import json
import subprocess
from pathlib import Path

import pytest

from app.core.plugin_service import PluginService
from app.core.plugin_models import AutoUpdateSettings
from fluid.context.plugins import load_plugin


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo):
    git(repo, "add", ".")
    return git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Plugin revision")


@pytest.fixture
def sources(tmp_path, monkeypatch):
    home = tmp_path / "home/.fluid"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home.parent))
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: home)
    base = tmp_path / "repos"
    base.mkdir()
    config = tmp_path / "git.config"
    config.write_text(
        f'[url "{base.as_uri()}/"]\n    insteadOf = https://example.test/\n'
        f'[url "{base.as_uri()}/"]\n    insteadOf = https://github.com/example/\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    return PluginService(), base


def setup(sources, kind):
    svc, base = sources
    upstream = base / "upstream.git"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    subdir = "packages/sample" if kind == "subdir" else ""
    plugin_root = upstream / subdir
    write(plugin_root / ".claude-plugin/plugin.json", json.dumps({"name": "sample", "version": "1.0.0"}))
    skill = plugin_root / "skills/sample/SKILL.md"
    write(skill, "---\nname: sample\ndescription: Update test\n---\n\nFirst revision.\n")
    if kind == "marketplace":
        write(upstream / ".claude-plugin/marketplace.json", json.dumps({
            "name": "sample", "plugins": [{"name": "sample", "version": "1.0.0", "source": "./"}],
        }))
    commit(upstream)
    first = git(upstream, "rev-parse", "HEAD")
    if kind == "tag":
        git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.com", "tag", "-a", "v1", "-m", "Pinned release")
    if kind in ("implicit", "marketplace"):
        svc.add_marketplace("https://example.test/upstream.git", "sample")
    else:
        mp = svc.MARKETPLACES_DIR / "sample"
        mp.mkdir(parents=True)
        if kind == "directory":
            # An existing Claude-compatible local-path marketplace registry.
            write(mp / ".claude-plugin/marketplace.json", json.dumps({
                "name": "sample", "plugins": [{"name": "sample", "source": "./plugin"}],
            }))
            import shutil
            shutil.copytree(plugin_root, mp / "plugin", ignore=shutil.ignore_patterns(".git"))
            skill = mp / "plugin/skills/sample/SKILL.md"
            source = {"source": "directory", "path": str(mp)}
        else:
            source = {"source": "url", "url": "https://example.test/upstream.git"}
            if kind == "github":
                source = {"source": "github", "repo": "example/upstream"}
            if kind == "subdir":
                source.update(source="git-subdir", path=subdir)
            if kind == "branch":
                source["ref"] = "main"
            if kind == "tag":
                source["ref"] = "v1"
            if kind == "sha":
                source["sha"] = first
            write(mp / ".claude-plugin/marketplace.json", json.dumps({
                "name": "sample", "plugins": [{"name": "sample", "version": "1.0.0", "source": source}],
            }))
            source = {"source": "directory", "path": str(mp)}
        svc._write_json_file(svc.MARKETPLACES_FILE, {"sample": {
            "installLocation": str(mp), "source": source,
        }})
    info = svc.install_plugin("sample@sample")
    return svc, upstream, skill, info


def runtime_text(svc):
    plugin = load_plugin("sample", "sample", home=svc.PLUGINS_DIR)
    assert plugin and plugin.skills
    return Path(plugin.skills[0].path).read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["implicit", "marketplace", "url", "github", "subdir", "branch", "directory"])
@pytest.mark.parametrize("automatic", [False, True])
def test_constant_version_updates_real_installs_and_agent_content(sources, kind, automatic):
    svc, upstream, skill, info = setup(sources, kind)
    assert "First revision" in runtime_text(svc)
    assert svc.check_for_updates() == []
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    if kind != "directory":
        commit(upstream)
    if automatic:
        svc.save_auto_update_settings(AutoUpdateSettings(
            enabled=True, auto_sync_marketplaces=True, auto_update_plugins=True,
        ))
        # Only a disposable source exists in this service.
        summary = svc.run_auto_update()
        assert summary["errors"] == []
        assert [p["id"] for p in summary["updated_plugins"]] == ["sample@sample"]
    else:
        updates = svc.check_for_updates(refresh_sources=True)
        assert [p.plugin_id for p in updates] == ["sample@sample"]
        svc.update_plugin("sample@sample")
    assert "Second revision" in runtime_text(svc)
    assert svc.check_for_updates() == []
    assert svc.get_enabled_plugins()["sample@sample"] is True


def test_manual_update_refreshes_a_stale_checkout_and_preserves_disabled_state(sources):
    svc, upstream, skill, info = setup(sources, "implicit")
    svc.disable_plugin(info.id)
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    result = svc.update_plugin(info.id)  # No prior Sync or Check.
    assert "Second revision" in (Path(result.install_path) / "skills/sample/SKILL.md").read_text()
    assert svc.get_enabled_plugins()[info.id] is False


@pytest.mark.parametrize("kind", ["tag", "sha"])
def test_pins_do_not_follow_the_default_branch(sources, kind):
    svc, upstream, skill, info = setup(sources, kind)
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    assert svc.check_for_updates(refresh_sources=True) == []
    svc.update_plugin(info.id)
    assert "First revision" in runtime_text(svc)


def test_automatic_update_respects_source_sync_off(sources):
    svc, upstream, skill, info = setup(sources, "implicit")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    commit(upstream)
    svc.save_auto_update_settings(AutoUpdateSettings(
        enabled=True, auto_sync_marketplaces=False, auto_update_plugins=True,
    ))
    assert svc.run_auto_update()["updated_plugins"] == []
    assert "First revision" in runtime_text(svc)

@pytest.mark.parametrize("component_root", ["skills", ".claude/skills", ".agents/skills"])
def test_refreshed_implicit_component_root_cannot_read_outside_checkout(
    sources, tmp_path, requires_symlinks, component_root,
):
    import shutil

    svc, upstream, skill, info = setup(sources, "implicit")
    outside = tmp_path / "outside"
    write(outside / "leak/SKILL.md", "---\nname: leak\ndescription: Outside\n---\nPrivate outside content.\n")
    shutil.rmtree(upstream / "skills")
    link = upstream / component_root
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    commit(upstream)

    assert svc.check_for_updates(refresh_sources=True) == []
    with pytest.raises(ValueError, match="outside"):
        svc.update_plugin(info.id)
    assert "First revision" in runtime_text(svc)
    assert not list(Path(info.install_path).rglob("leak"))
