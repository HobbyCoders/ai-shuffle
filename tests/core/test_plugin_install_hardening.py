"""Plugin install hardening: traversal, symlinks, git argument injection, sha
pins, downgrade-free updates, uninstall cleanup, scope isolation, manifest
validation and hooks.json loading.

Hermetic: every PluginService lives in tmp_path, marketplaces are plain
directories, and git is a fake that records its argv (no network, no clone).
Tests named ``*_guard_bites`` re-run a scenario with the guard monkeypatched off
and assert the unsafe outcome, proving the safe-path assertions depend on it.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.core import plugin_install, plugin_updates
from app.core.plugin_models import PluginScope
from app.core.plugin_service import PluginService

symlinks = pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                              reason="needs POSIX symlinks")

PINNED = "a" * 40
TIP = "b" * 40


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid = tmp_path / "fluid"
    fluid.mkdir()
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: fluid)
    project = tmp_path / "proj"
    project.mkdir()
    service = PluginService(project_dir=project)
    # Marketplace dirs here are not git repos; never shell out for HEAD.
    monkeypatch.setattr(service, "_get_marketplace_head_version", lambda _mp: None)
    return service


def _marketplace(svc, name="mp", plugins=(), metadata=None, raw=None) -> Path:
    root = svc.MARKETPLACES_DIR / name
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = raw if raw is not None else {"name": name, "plugins": list(plugins)}
    if metadata is not None:
        manifest["metadata"] = metadata
    (root / ".claude-plugin" / "marketplace.json").write_text(
        manifest if isinstance(manifest, str) else json.dumps(manifest))
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    data[name] = {"source": {"source": "git", "url": f"https://example.invalid/{name}.git"},
                  "installLocation": str(root)}
    svc._write_json_file(svc.MARKETPLACES_FILE, data)
    return root


def _plugin_dir(root: Path, rel: str, version="1.2.0", files=None) -> Path:
    d = root / rel
    (d / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": d.name}
    if version is not None:
        manifest["version"] = version
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    (d / "commands").mkdir(exist_ok=True)
    (d / "commands" / "hello.md").write_text("# hello\n")
    for name, content in (files or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content)
    return d


def _tree(root: Path) -> dict:
    """Relative path -> file bytes for everything under root (no link following)."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = p.read_bytes() if not p.is_symlink() else b"<link>"
    return out


def _installed(svc) -> dict:
    return svc._read_json_file(svc.INSTALLED_FILE, {}).get("plugins", {})


class FakeGit:
    """Stands in for subprocess.run in plugin_install: records argv, emulates
    clone / rev-parse / fetch / checkout on a directory."""

    def __init__(self, populate=None, head=TIP, fetch_ok=True):
        self.calls = []
        self.populate = populate or (lambda dest: _plugin_dir(dest.parent, dest.name, "2.0.0"))
        self.heads = {}
        self.default_head = head
        self.fetch_ok = fetch_ok

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[-1])
            dest.mkdir(parents=True, exist_ok=True)
            self.populate(dest)
            self.heads[str(dest)] = self.default_head
            return ok
        if cmd[0] == "git" and cmd[1] == "-C":
            repo, verb = cmd[2], cmd[3]
            if verb == "rev-parse":
                return subprocess.CompletedProcess(cmd, 0, self.heads.get(repo, "") + "\n", "")
            if verb == "fetch":
                if not self.fetch_ok:
                    return subprocess.CompletedProcess(cmd, 128, "", "not our ref")
                self.fetched = cmd[-1]
                return ok
            if verb == "checkout":
                self.heads[repo] = self.fetched
                return ok
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def fake_git(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(plugin_install.subprocess, "run", git)
    return git


# ── path traversal ──────────────────────────────────────────────────────────


def _traversal_version_install(svc, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    mp = _marketplace(svc, plugins=[{"name": "evil", "source": "./plugins/evil"}])
    # cache/mp/evil/<version> -> tmp_path/victim
    _plugin_dir(mp, "plugins/evil", version="../../../../../victim")
    try:
        svc.install_plugin("evil@mp")
        return victim, None
    except ValueError as e:
        return victim, e


def test_manifest_version_with_traversal_is_rejected(svc, tmp_path):
    victim, err = _traversal_version_install(svc, tmp_path)
    assert err is not None and "version" in str(err)
    assert (victim / "keep.txt").read_text() == "precious"
    assert not (victim / "commands").exists()
    assert "evil@mp" not in _installed(svc)


def test_manifest_version_traversal_guard_bites(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_install, "_safe_path_component", lambda v, _w: v)
    victim, err = _traversal_version_install(svc, tmp_path)
    # Without the guard the install deletes and overwrites a directory
    # outside the plugin cache.
    assert err is None
    assert not (victim / "keep.txt").exists()
    assert (victim / "commands" / "hello.md").exists()


@pytest.mark.parametrize("version", ["..", ".", "", "a/b", "a\\b", 7])
def test_unsafe_entry_versions_rejected_before_fetch(svc, fake_git, version):
    _marketplace(svc, plugins=[{"name": "x", "version": version,
                                "source": {"source": "github", "repo": "o/x"}}])
    with pytest.raises(ValueError):
        svc.install_plugin("x@mp")
    assert fake_git.calls == []
    assert not (svc.CACHE_DIR / "mp").exists()


def test_cloned_manifest_version_traversal_is_rejected_and_cleaned(svc, tmp_path, monkeypatch):
    git = FakeGit(populate=lambda dest: _plugin_dir(dest.parent, dest.name, "../../../../../escaped"))
    monkeypatch.setattr(plugin_install.subprocess, "run", git)
    _marketplace(svc, plugins=[{"name": "x", "version": "1.0.1",
                                "source": {"source": "github", "repo": "o/x"}}])
    with pytest.raises(ValueError):
        svc.install_plugin("x@mp")
    assert not (tmp_path / "escaped").exists()
    assert not (svc.CACHE_DIR / "mp" / "x" / "1.0.1").exists()
    assert "x@mp" not in _installed(svc)


@pytest.mark.parametrize("source,metadata", [
    ("./plugins/../../../outside", None),
    ("./ok", {"pluginRoot": "../../.."}),
])
def test_relative_source_cannot_escape_marketplace(svc, tmp_path, source, metadata):
    outside = svc.MARKETPLACES_DIR.parent / "outside"
    _plugin_dir(outside.parent, "outside", files={"secret.txt": "s3cret"})
    # pluginRoot "../../.." from marketplaces/mp lands at fluid/ -> fluid/ok
    _plugin_dir(svc.FLUID_DIR, "ok", files={"secret.txt": "s3cret"})
    (_marketplace(svc, plugins=[{"name": "p", "source": source}], metadata=metadata)
     / "plugins").mkdir()

    with pytest.raises(ValueError, match="not found"):
        svc.install_plugin("p@mp")
    assert not any(b"s3cret" in v for v in _tree(svc.CACHE_DIR).values())
    # Listing does not resolve it to the outside directory either.
    listed = {p.id: p for p in svc.get_available_plugins(marketplace="mp")}
    assert listed["p@mp"].marketplace_path != str(outside)


def test_relative_source_escape_guard_bites(svc, monkeypatch):
    outside = svc.MARKETPLACES_DIR.parent / "outside"
    _plugin_dir(outside.parent, "outside", files={"secret.txt": "s3cret"})
    (_marketplace(svc, plugins=[{"name": "p", "source": "./plugins/../../../outside"}])
     / "plugins").mkdir()
    monkeypatch.setattr(plugin_install, "_is_within", lambda *_a: True)
    svc.install_plugin("p@mp")
    assert any(b"s3cret" in v for v in _tree(svc.CACHE_DIR).values())


def test_project_install_rejects_traversal_name(svc, tmp_path):
    sibling = svc.project_dir / ".fluid" / "keep"
    sibling.mkdir(parents=True)
    (sibling / "f").write_text("x")
    mp = _marketplace(svc, plugins=[{"name": "../keep", "source": "./plugins/p"}])
    _plugin_dir(mp, "plugins/p")
    with pytest.raises(ValueError, match="Invalid plugin name"):
        svc.install_plugin_to_project("../keep@mp", PluginScope.LOCAL)
    assert (sibling / "f").read_text() == "x"


@pytest.mark.parametrize("bad_id", ["@project-local", ".@project-local", "..@project-project",
                                    "a/b@project-local"])
def test_project_uninstall_never_removes_scope_dir(svc, bad_id):
    for scope in (PluginScope.LOCAL, PluginScope.PROJECT):
        d = svc.ensure_scope_directory(scope) / "other"
        d.mkdir()
        (d / "f").write_text("x")
    with pytest.raises(ValueError):
        svc.uninstall_project_plugin(bad_id)
    assert (svc.LOCAL_PLUGINS_DIR / "other" / "f").exists()
    assert (svc.PROJECT_PLUGINS_DIR / "other" / "f").exists()


def test_git_subdir_path_cannot_escape_clone(svc, tmp_path, monkeypatch):
    git = FakeGit(populate=lambda dest: (dest / "sub").mkdir())
    monkeypatch.setattr(plugin_install.subprocess, "run", git)
    dest = tmp_path / "dest"
    with pytest.raises(ValueError, match="not found"):
        plugin_install._clone_git_subdir_source(
            svc, {"source": "git-subdir", "url": "o/r", "path": "../../"}, dest)
    assert not dest.exists()


@pytest.mark.parametrize("url, cloned", [
    ("ssh://git.example.com/team/repo", "ssh://git.example.com/team/repo.git"),
    ("ssh://git@git.example.com:2222/team/repo.git", "ssh://git@git.example.com:2222/team/repo.git"),
    ("git@github.com:o/r.git", "git@github.com:o/r.git"),
    ("https://gitlab.example.com/o/r", "https://gitlab.example.com/o/r.git"),
    ("o/r", "https://github.com/o/r.git"),
])
def test_git_subdir_keeps_each_url_form(svc, tmp_path, monkeypatch, url, cloned):
    """R6-7: an ssh:// URL used to be rewritten into https://github.com/ssh://..."""
    git = FakeGit(populate=lambda dest: _plugin_dir(dest, "sub", "1.0.0"))
    monkeypatch.setattr(plugin_install.subprocess, "run", git)
    plugin_install._clone_git_subdir_source(
        svc, {"source": "git-subdir", "url": url, "path": "sub"}, tmp_path / "dest")
    clone = git.calls[0]
    assert clone[:2] == ["git", "clone"]
    assert clone[-2] == cloned


# ── symlinks ────────────────────────────────────────────────────────────────


def _symlink_plugin(svc, tmp_path):
    secret_dir = tmp_path / "home" / ".ssh"
    secret_dir.mkdir(parents=True)
    (secret_dir / "id_rsa").write_text("PRIVATE KEY")
    mp = _marketplace(svc, plugins=[{"name": "s", "source": "./plugins/s"}])
    pdir = _plugin_dir(mp, "plugins/s", files={"skills/a/SKILL.md": "---\nname: a\n---\n"})
    os.symlink(secret_dir / "id_rsa", pdir / "skills" / "a" / "leak.md")
    os.symlink(secret_dir, pdir / "ssh")
    # In-repo link (shared file) and a loop back to an ancestor.
    (mp / "shared.md").write_text("shared")
    os.symlink(mp / "shared.md", pdir / "commands" / "shared.md")
    os.symlink("..", pdir / "commands" / "loop")
    return svc.install_plugin("s@mp")


@symlinks
def test_symlinks_outside_repo_are_not_copied(svc, tmp_path):
    info = _symlink_plugin(svc, tmp_path)
    tree = _tree(Path(info.install_path))
    assert not any(b"PRIVATE KEY" in v for v in tree.values())
    assert "ssh/id_rsa" not in tree
    # In-repo links still materialize as real files; the loop is cut.
    shared = Path(info.install_path) / "commands" / "shared.md"
    assert shared.read_text() == "shared" and not shared.is_symlink()
    assert len(tree) < 20


@symlinks
def test_symlink_guard_bites(svc, tmp_path, monkeypatch):
    real = plugin_install._is_within
    # Disable only the "outside the repo" check; keep loop protection working.
    monkeypatch.setattr(plugin_install, "_is_within",
                        lambda p, root: True if p.is_symlink() else real(p, root))
    info = _symlink_plugin(svc, tmp_path)
    assert any(b"PRIVATE KEY" in v for v in _tree(Path(info.install_path)).values())


@symlinks
def test_implicit_plugin_skips_symlinked_skill_outside_repo(svc, tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "SKILL.md").write_text("---\nname: leak\n---\nTOKEN=abc")
    root = svc.MARKETPLACES_DIR / "repo"
    (root / "skills" / "ok").mkdir(parents=True)
    (root / "skills" / "ok" / "SKILL.md").write_text("---\nname: ok\n---\n")
    os.symlink(secret, root / "skills" / "leak")
    data = {"repo": {"source": {"source": "git", "url": "https://x/repo.git"},
                     "installLocation": str(root)}}
    svc._write_json_file(svc.MARKETPLACES_FILE, data)

    info = svc.install_plugin("repo@repo")
    install = Path(info.install_path)
    assert (install / "skills" / "ok" / "SKILL.md").is_file()
    assert not (install / "skills" / "leak").exists()


@symlinks
def test_cloned_symlinks_escaping_the_plugin_are_pruned(svc, tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN")

    def populate(dest):
        _plugin_dir(dest.parent, dest.name, "2.0.0")
        os.symlink(secret, dest / "commands" / "leak.md")
        os.symlink("hello.md", dest / "commands" / "alias.md")

    monkeypatch.setattr(plugin_install.subprocess, "run", FakeGit(populate=populate))
    _marketplace(svc, plugins=[{"name": "x", "source": {"source": "github", "repo": "o/x"}}])
    info = svc.install_plugin("x@mp")
    cmds = Path(info.install_path) / "commands"
    assert not (cmds / "leak.md").exists() and not (cmds / "leak.md").is_symlink()
    assert (cmds / "alias.md").read_text() == "# hello\n"
    assert secret.read_text() == "TOKEN"


# ── git argument injection / transports ─────────────────────────────────────


@pytest.mark.parametrize("url", [
    "--upload-pack=touch /tmp/pwned",
    "-c core.sshCommand=evil",
    "ext::sh -c touch% /tmp/pwned",
    "file:///etc",
    "/home/user/private-repo",
])
def test_untrusted_clone_urls_never_reach_git(svc, fake_git, url):
    _marketplace(svc, plugins=[{"name": "x", "source": {"source": "url", "url": url}}])
    with pytest.raises(ValueError, match="Unsupported plugin source URL"):
        svc.install_plugin("x@mp")
    assert fake_git.calls == []
    assert "x@mp" not in _installed(svc)


def test_clone_url_guard_bites(svc, fake_git, monkeypatch):
    monkeypatch.setattr(plugin_install, "_check_clone_url", lambda _u: None)
    _marketplace(svc, plugins=[{"name": "x", "source": {"source": "url",
                                                        "url": "--upload-pack=touch /tmp/pwned"}}])
    svc.install_plugin("x@mp")
    clone = fake_git.calls[0]
    # Even with the URL check off, "--" keeps git from parsing it as an option.
    assert clone[clone.index("--upload-pack=touch /tmp/pwned") - 1] == "--"


TOKEN = "ghp_secretToken123"
TOKEN_URL = f"https://{TOKEN}@example.com/o/x.git"


@pytest.mark.parametrize("source", [
    {"source": "url", "url": TOKEN_URL},
    {"source": "git-subdir", "url": TOKEN_URL, "path": "plugins/x"},
])
def test_clone_error_redacts_url_credentials_and_is_capped(svc, monkeypatch, source):
    stderr = (f"Cloning into 'x'...\nfatal: unable to access '{TOKEN_URL}/': "
              "The requested URL returned error: 403\n" + "remote: noise\n" * 400)

    def run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 128, "", stderr)

    monkeypatch.setattr(plugin_install.subprocess, "run", run)
    _marketplace(svc, plugins=[{"name": "x", "source": source}])
    with pytest.raises(RuntimeError, match="Failed to clone") as exc:
        svc.install_plugin("x@mp")
    message = str(exc.value)
    assert TOKEN not in message
    assert "https://***@example.com/o/x.git" in message
    assert len(message) < 600
    assert "x@mp" not in _installed(svc)


def test_rejected_clone_url_error_redacts_url_credentials(svc, fake_git):
    _marketplace(svc, plugins=[{"name": "x", "source": {
        "source": "url", "url": f"file://{TOKEN}@host/repo"}}])
    with pytest.raises(ValueError, match="Unsupported plugin source URL") as exc:
        svc.install_plugin("x@mp")
    assert TOKEN not in str(exc.value)
    assert fake_git.calls == []


def test_clone_passes_ref_and_separates_url(svc, fake_git):
    _marketplace(svc, plugins=[{"name": "x", "source": {"source": "github", "repo": "o/x",
                                                        "ref": "v2"}}])
    info = svc.install_plugin("x@mp")
    clone = fake_git.calls[0]
    assert clone[:4] == ["git", "clone", "--depth", "1"]
    assert clone[clone.index("--branch") + 1] == "v2"
    assert clone[-3:-1] == ["--", "https://github.com/o/x.git"]
    assert info.version == "2.0.0"
    assert Path(info.install_path) == svc.CACHE_DIR / "mp" / "x" / "2.0.0"


# ── sha pinning ─────────────────────────────────────────────────────────────


def test_pinned_sha_is_fetched_and_verified(svc, fake_git):
    _marketplace(svc, plugins=[{"name": "x", "source": {"source": "github", "repo": "o/x",
                                                        "sha": PINNED}}])
    svc.install_plugin("x@mp")
    verbs = [c[3] for c in fake_git.calls if c[1] == "-C"]
    assert verbs == ["rev-parse", "fetch", "checkout", "rev-parse"]
    assert fake_git.fetched == PINNED
    assert _installed(svc)["x@mp"][0]["sourceSha"] == PINNED[:7]


def test_pinned_sha_mismatch_fails_closed(svc, monkeypatch):
    git = FakeGit(fetch_ok=False)
    monkeypatch.setattr(plugin_install.subprocess, "run", git)
    _marketplace(svc, plugins=[{"name": "x", "version": "1.0.0",
                                "source": {"source": "github", "repo": "o/x", "sha": PINNED}}])
    with pytest.raises(RuntimeError, match="pinned sha"):
        svc.install_plugin("x@mp")
    assert not (svc.CACHE_DIR / "mp" / "x" / "1.0.0").exists()
    assert "x@mp" not in _installed(svc)
    assert "x@mp" not in svc.get_enabled_plugins()


def test_pinned_sha_already_at_head_skips_fetch(svc, monkeypatch):
    git = FakeGit(head=PINNED)
    monkeypatch.setattr(plugin_install.subprocess, "run", git)
    _marketplace(svc, plugins=[{"name": "x", "source": {"source": "github", "repo": "o/x",
                                                        "sha": PINNED[:12]}}])
    svc.install_plugin("x@mp")
    assert [c[3] for c in git.calls if c[1] == "-C"] == ["rev-parse"]


@pytest.mark.parametrize("sha", ["--upload-pack=x", "zzzzzzz", "abc", 123])
def test_malformed_sha_is_rejected(tmp_path, sha):
    with pytest.raises(ValueError, match="sha"):
        plugin_install._checkout_pinned_sha(tmp_path, sha)


# ── updates ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("installed,available,expected", [
    ("1.2.0", "1.1.9", False),
    ("1.2.0", "1.2.0", False),
    ("1.2", "1.2.0", False),
    ("v1.2.0", "1.10.0", True),
    ("1.2.0", "2.0", True),
    ("abc1234", "def5678", True),      # hashes cannot be ordered
    ("1234567", "1234560", True),      # all-digit short hash is not a release
    ("1.0.0-beta", "1.0.0", True),
])
def test_is_update_orders_release_versions(installed, available, expected):
    assert plugin_updates._is_update(installed, available) is expected


def _installed_local(svc, installed_version, available_version):
    mp = _marketplace(svc, plugins=[{"name": "p", "source": "./plugins/p"}])
    _plugin_dir(mp, "plugins/p", version=installed_version)
    svc.install_plugin("p@mp")
    _plugin_dir(mp, "plugins/p", version=available_version)  # marketplace now says
    return mp


def _run_auto_update(svc):
    svc.save_auto_update_settings(plugin_updates.AutoUpdateSettings(
        enabled=True, interval_hours=24, auto_sync_marketplaces=False,
        auto_update_plugins=True))
    return svc.run_auto_update()


def test_auto_update_never_downgrades(svc):
    _installed_local(svc, "1.5.0", "1.4.0")
    summary = _run_auto_update(svc)
    assert summary["updates_available"] == [] and summary["updated_plugins"] == []
    rec = _installed(svc)["p@mp"][0]
    assert rec["version"] == "1.5.0"
    assert Path(rec["installPath"]).name == "1.5.0"
    assert not (svc.CACHE_DIR / "mp" / "p" / "1.4.0").exists()
    assert svc.get_auto_update_settings().last_check


def test_downgrade_guard_bites(svc, monkeypatch):
    monkeypatch.setattr(plugin_updates, "_is_update", lambda i, a: i != a)
    _installed_local(svc, "1.5.0", "1.4.0")
    _run_auto_update(svc)
    assert _installed(svc)["p@mp"][0]["version"] == "1.4.0"


def test_auto_update_applies_newer_version_and_keeps_disabled_state(svc):
    _installed_local(svc, "1.0.0", "1.1.0")
    svc.disable_plugin("p@mp")
    summary = _run_auto_update(svc)
    assert [u["plugin_id"] for u in summary["updates_available"]] == ["p@mp"]
    assert _installed(svc)["p@mp"][0]["version"] == "1.1.0"
    assert svc.get_enabled_plugins()["p@mp"] is False


def test_auto_update_respects_update_plugins_off(svc):
    _installed_local(svc, "1.0.0", "1.1.0")
    svc.save_auto_update_settings(plugin_updates.AutoUpdateSettings(
        enabled=True, interval_hours=24, auto_sync_marketplaces=False, auto_update_plugins=False))
    summary = svc.run_auto_update()
    assert len(summary["updates_available"]) == 1
    assert _installed(svc)["p@mp"][0]["version"] == "1.0.0"


def test_external_entry_with_lower_version_is_not_an_update(svc, fake_git):
    _marketplace(svc, plugins=[{"name": "x", "version": "3.0.0",
                                "source": {"source": "github", "repo": "o/x"}}])
    svc.install_plugin("x@mp")  # cloned manifest says 2.0.0 -> recorded as 2.0.0
    assert _installed(svc)["x@mp"][0]["version"] == "2.0.0"
    assert [u.available_version for u in svc.check_for_updates()] == ["3.0.0"]
    _marketplace(svc, plugins=[{"name": "x", "version": "1.9.0",
                                "source": {"source": "github", "repo": "o/x"}}])
    assert svc.check_for_updates() == []


def test_external_pinned_sha_unchanged_is_not_an_update(svc, fake_git):
    src = {"source": "github", "repo": "o/x", "sha": PINNED}
    _marketplace(svc, plugins=[{"name": "x", "source": src}])
    svc.install_plugin("x@mp")
    assert svc.check_for_updates() == []
    _marketplace(svc, plugins=[{"name": "x", "source": {**src, "sha": "c" * 40}}])
    assert [u.available_version for u in svc.check_for_updates()] == ["c" * 7]


# ── uninstall cleanup ───────────────────────────────────────────────────────


def test_uninstall_removes_files_hooks_mcp_and_records_only(svc, tmp_path):
    from app.core.hook_service import load_plugin_hooks
    from app.core.plugin_service import get_hooks_from_plugins

    mp = _marketplace(svc, plugins=[{"name": "p", "source": "./plugins/p"},
                                    {"name": "q", "source": "./plugins/q"}])
    hooks = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/h.sh"}]}]}}
    _plugin_dir(mp, "plugins/p", files={"hooks/hooks.json": json.dumps(hooks), "h.sh": "echo",
                                        ".mcp.json": json.dumps({"mcpServers": {"s": {"command": "x"}}})})
    _plugin_dir(mp, "plugins/q")
    user_mcp = tmp_path / "user.mcp.json"
    user_mcp.write_text("{}")
    p = svc.install_plugin("p@mp")
    q = svc.install_plugin("q@mp")
    assert os.access(Path(p.install_path) / "h.sh", os.X_OK)
    assert load_plugin_hooks([p.install_path])["PreToolUse"]
    marketplace_before = _tree(mp)

    assert svc.uninstall_plugin("p@mp") is True

    assert not Path(p.install_path).exists()
    assert "p@mp" not in _installed(svc) and "p@mp" not in svc.get_enabled_plugins()
    assert load_plugin_hooks([p.install_path]) == {}
    assert not any(h["plugin_id"] == "p@mp" for h in get_hooks_from_plugins(svc.get_installed_plugins()))
    # Nothing else touched: sibling plugin, marketplace source, unrelated config.
    assert Path(q.install_path, "commands", "hello.md").exists()
    assert svc.get_enabled_plugins()["q@mp"] is True
    assert _tree(mp) == marketplace_before
    assert user_mcp.read_text() == "{}"


def _plant_record(svc, record):
    data = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
    data.setdefault("plugins", {})["z@mp"] = [record]
    svc._write_json_file(svc.INSTALLED_FILE, data)


def test_uninstall_record_without_install_path_keeps_cwd(svc, tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "app.py").write_text("important")
    monkeypatch.chdir(cwd)
    _plant_record(svc, {"scope": "user", "version": "1"})
    svc.uninstall_plugin("z@mp")
    assert (cwd / "app.py").read_text() == "important"
    assert "z@mp" not in _installed(svc)


def test_uninstall_record_outside_cache_keeps_files(svc, tmp_path):
    outside = tmp_path / "my-dev-plugin"
    outside.mkdir()
    (outside / "work.md").write_text("wip")
    _plant_record(svc, {"scope": "user", "installPath": str(outside)})
    svc.uninstall_plugin("z@mp")
    assert (outside / "work.md").read_text() == "wip"
    # installPath equal to the cache root itself is refused too.
    (svc.CACHE_DIR / "other").mkdir()
    _plant_record(svc, {"scope": "user", "installPath": str(svc.CACHE_DIR)})
    svc.uninstall_plugin("z@mp")
    assert (svc.CACHE_DIR / "other").is_dir()


def test_uninstall_outside_cache_guard_bites(svc, tmp_path, monkeypatch):
    outside = tmp_path / "my-dev-plugin"
    outside.mkdir()
    _plant_record(svc, {"scope": "user", "installPath": str(outside)})
    # Treat everything as inside the cache (and the cache as inside nothing).
    monkeypatch.setattr(plugin_install, "_is_within", lambda p, root: root == svc.CACHE_DIR)
    svc.uninstall_plugin("z@mp")
    assert not outside.exists()


def test_remove_marketplace_removes_its_cached_plugins_only(svc):
    mp = _marketplace(svc, plugins=[{"name": "p", "source": "./plugins/p"}])
    _plugin_dir(mp, "plugins/p")
    other = _marketplace(svc, name="other", plugins=[{"name": "p", "source": "./plugins/p"}])
    _plugin_dir(other, "plugins/p")
    svc.install_plugin("p@mp")
    kept = svc.install_plugin("p@other")

    svc.remove_marketplace("mp")

    assert not (svc.CACHE_DIR / "mp").exists()
    assert not mp.exists()
    assert set(_installed(svc)) == {"p@other"}
    assert Path(kept.install_path, "commands", "hello.md").exists()


# ── scope isolation ─────────────────────────────────────────────────────────


def test_project_scopes_are_isolated(svc):
    mp = _marketplace(svc, plugins=[{"name": "p", "source": "./plugins/p"}])
    _plugin_dir(mp, "plugins/p")
    local = svc.install_plugin_to_project("p@mp", PluginScope.LOCAL)
    proj = svc.install_plugin_to_project("p@mp", PluginScope.PROJECT)

    assert local.id == "p@project-local" and proj.id == "p@project-project"
    assert Path(local.install_path) == svc.project_dir / ".fluid" / "plugins" / "p"
    assert Path(proj.install_path) == svc.project_dir / "plugins" / "p"
    local_settings = json.loads((svc.project_dir / ".fluid" / "settings.local.json").read_text())
    proj_settings = json.loads((svc.project_dir / ".fluid" / "settings.json").read_text())
    assert local_settings["enabledPlugins"] == {"p@project-local": True}
    assert proj_settings["enabledPlugins"] == {"p@project-project": True}
    # Nothing leaks into user scope.
    assert _installed(svc) == {} and svc.get_enabled_plugins() == {}

    svc.uninstall_project_plugin("p@project-local")
    assert not Path(local.install_path).exists()
    assert Path(proj.install_path, "commands", "hello.md").exists()
    assert svc.get_project_enabled_plugins() == {"p@project-project": True}


def test_user_install_does_not_touch_project(svc):
    mp = _marketplace(svc, plugins=[{"name": "p", "source": "./plugins/p"}])
    _plugin_dir(mp, "plugins/p")
    info = svc.install_plugin("p@mp")
    assert Path(info.install_path).is_relative_to(svc.CACHE_DIR)
    assert list(svc.project_dir.iterdir()) == []
    assert svc.get_project_installed_plugins() == []


# ── manifest validation ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    "[1, 2]",
    '{"plugins": {"p": {}}}',
    "not json",
    '{"plugins": ["p", null, 3]}',
])
def test_malformed_marketplace_manifest_is_ignored(svc, raw):
    _marketplace(svc, name="bad", raw=raw)
    good = _marketplace(svc, name="good", plugins=[{"name": "g", "source": "./plugins/g"}])
    _plugin_dir(good, "plugins/g")
    ids = {p.id for p in svc.get_available_plugins()}
    assert "g@good" in ids
    assert not any(i.endswith("@bad") for i in ids)
    with pytest.raises(ValueError, match="not found"):
        svc.install_plugin("p@bad")


def test_malformed_entries_are_skipped_not_fatal(svc):
    mp = _marketplace(svc, plugins=[
        "string-entry",
        {"name": None},
        {"name": 5, "source": "./plugins/p"},
        {"name": "../escape", "source": "./plugins/p"},
        {"name": "nodesc", "description": None, "source": "./plugins/p"},
        {"name": "ok", "description": "d" * 500, "source": "./plugins/p"},
    ])
    _plugin_dir(mp, "plugins/p")
    listed = {p.id: p for p in svc.get_available_plugins(marketplace="mp")}
    assert set(listed) == {"nodesc@mp", "ok@mp"}
    assert len(listed["ok@mp"].description) == 200


def test_non_string_entry_version_rejected(svc):
    mp = _marketplace(svc, plugins=[{"name": "p", "version": 2, "source": "./plugins/p"}])
    _plugin_dir(mp, "plugins/p", version=None)
    with pytest.raises(ValueError, match="version"):
        svc.install_plugin("p@mp")
    assert _installed(svc) == {}


def test_corrupt_plugin_json_falls_back_to_entry_version(svc):
    mp = _marketplace(svc, plugins=[{"name": "p", "version": "0.3.0", "source": "./plugins/p"}])
    d = _plugin_dir(mp, "plugins/p")
    (d / ".claude-plugin" / "plugin.json").write_text("{nope")
    info = svc.install_plugin("p@mp")
    assert info.version == "0.3.0"


@pytest.mark.parametrize("source", [{"source": "npm", "package": "x"}, "https://x/y", 42])
def test_unsupported_sources_rejected(svc, fake_git, source):
    _marketplace(svc, plugins=[{"name": "p", "source": source}])
    with pytest.raises(ValueError):
        svc.install_plugin("p@mp")
    assert fake_git.calls == [] and _installed(svc) == {}


def test_invalid_plugin_id_shapes(svc):
    for bad in ("noat", "a/b@mp", "p@../mp", "p@"):
        with pytest.raises(ValueError):
            svc.install_plugin(bad)


# ── hooks.json loading ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_installed_hooks_json_with_invalid_regex_and_unknown_event(svc, monkeypatch):
    from app.core import hook_service

    hooks = {"hooks": {
        "PreToolUse": [
            {"matcher": "Bash(", "hooks": [{"type": "command", "command": "bad"}]},
            {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "good"}]},
        ],
        "NotARealEvent": [{"hooks": [{"type": "command", "command": "ghost"}]}],
    }}
    mp = _marketplace(svc, plugins=[{"name": "h", "source": "./plugins/h"},
                                    {"name": "broken", "source": "./plugins/broken"}])
    _plugin_dir(mp, "plugins/h", files={"hooks/hooks.json": json.dumps(hooks)})
    _plugin_dir(mp, "plugins/broken", files={"hooks/hooks.json": '{"hooks": ["x"]}'})
    h = svc.install_plugin("h@mp")
    broken = svc.install_plugin("broken@mp")

    loaded = hook_service.load_plugin_hooks([broken.install_path, h.install_path])
    assert [d["command"] for d in loaded["PreToolUse"]] == ["bad", "good"]
    assert "NotARealEvent" in loaded  # carried, never dispatched by the SDK bridge

    ran = []

    async def _execute(hook_def, *_a, **_k):
        ran.append(hook_def["command"])

    monkeypatch.setattr(hook_service, "execute_plugin_hook", _execute)
    handler = hook_service.create_plugin_hook_handler("PreToolUse", loaded["PreToolUse"], h.install_path)
    await handler({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, None, {})
    assert ran == ["good"]

    # The discovery listing survives an unparsable hooks.json in another plugin.
    from app.core.plugin_service import get_hooks_from_plugins
    events = {(x["plugin_id"], x["event"]) for x in get_hooks_from_plugins(svc.get_installed_plugins())}
    assert ("h@mp", "PreToolUse") in events
