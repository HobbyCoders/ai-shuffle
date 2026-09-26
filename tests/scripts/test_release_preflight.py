"""Release checks run offline against disposable Git history and API responses."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci import release_preflight as checks

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    # Git for Windows needs these OS inputs; never carry authentication/config.
    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, env=env, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Release test")
    git("config", "user.email", "release@example.invalid")
    source = tmp_path / "app/core/connectors/one.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('pinned source')\n")
    git("add", ".")
    git("commit", "-qm", "Connector source")
    revision = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", revision)
    git("tag", "v0.8.19")
    catalog = {
        "revision": revision,
        "packages": {"one": {"files": {"one.py": hashlib.sha256(source.read_bytes()).hexdigest()}}},
    }
    write_catalog(tmp_path, catalog)
    return tmp_path, git, catalog


def write_catalog(root, catalog):
    identity = {k: v for k, v in catalog.items() if k != "release"}
    catalog["release"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    (root / "app/core/connectors/catalog.json").write_text(json.dumps(catalog))


def test_catalog_verifies_real_committed_bytes(repo):
    root, _, _ = repo
    checks.verify_catalog(root)


def test_matching_checkout_digest_does_not_hide_wrong_pinned_bytes(repo):
    root, _, catalog = repo
    source = root / "app/core/connectors/one.py"
    source.write_text("print('new bytes')\n")
    catalog["packages"]["one"]["files"]["one.py"] = hashlib.sha256(source.read_bytes()).hexdigest()
    write_catalog(root, catalog)
    with pytest.raises(ValueError, match="pinned commit bytes"):
        checks.verify_catalog(root)


def test_catalog_rejects_missing_revision(repo):
    root, _, catalog = repo
    catalog["revision"] = "0" * 40
    write_catalog(root, catalog)
    with pytest.raises(subprocess.CalledProcessError):
        checks.verify_catalog(root)


def test_catalog_change_requires_new_install_identity(repo):
    root, _, catalog = repo
    catalog["dependencies"] = {"default": ["example==1"]}
    (root / "app/core/connectors/catalog.json").write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match="release identity is stale"):
        checks.verify_catalog(root)


def test_remote_catalog_bytes_must_match_the_same_pin(repo, monkeypatch):
    root, _, catalog = repo
    original = subprocess.check_output
    calls = []

    def run(args, **kwargs):
        if args[0] != "gh":
            return original(args, **kwargs)
        calls.append(args)
        return b"wrong remote bytes"

    monkeypatch.setattr(subprocess, "check_output", run)
    with pytest.raises(ValueError, match="GitHub bytes"):
        checks.verify_catalog(root, remote=True)
    assert f"?ref={catalog['revision']}" in calls[0][2]
    assert calls[0][-1] == "Accept: application/vnd.github.raw+json"


def test_tag_must_match_checkout_and_be_on_main(repo):
    root, git, catalog = repo
    assert checks.verify_tag(root, "v0.8.19") == catalog["revision"]
    git("commit", "--allow-empty", "-qm", "Not merged")
    with pytest.raises(ValueError, match="checkout HEAD"):
        checks.verify_tag(root, "v0.8.19")
    git("tag", "v0.8.20")
    with pytest.raises(ValueError, match="not on origin/main"):
        checks.verify_tag(root, "v0.8.20")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
    assert checks.verify_tag(root, "v0.8.20") == git("rev-parse", "HEAD")


@pytest.mark.parametrize("tag", ["main", "v6-phase2", "v1.2.3.4", "v01.2.3", "v1.2.3-"])
def test_invalid_tag_is_not_a_release(repo, tag):
    with pytest.raises(ValueError, match="Expected a release tag"):
        checks.verify_tag(repo[0], tag)


def test_latest_ignores_milestones_and_prereleases(repo):
    root, git, _ = repo
    for tag in ("v6-phase1", "v6-phase2", "v0.9.0-rc.1"):
        git("tag", tag)
    assert checks.newest_final(root, "v0.8.19")
    assert not checks.newest_final(root, "v0.9.0-rc.1")
    git("tag", "v0.8.20")
    assert not checks.newest_final(root, "v0.8.19")


@pytest.mark.parametrize("protected,merged,base,merge_sha,success", [
    (True, True, "main", "release", True),
    (False, True, "main", "release", False),
    (True, False, "main", "release", False),
    (True, True, "other", "release", False),
    (True, True, "main", "other", False),
])
def test_release_requires_protected_main_and_a_merged_pr(monkeypatch, protected, merged, base, merge_sha, success):
    replies = {
        "branches/main": {"protected": protected},
        "commits/release/pulls?per_page=100": [{
            "merged_at": "date" if merged else None, "base": {"ref": base},
            "merge_commit_sha": merge_sha, "head": {"sha": "pr-head"},
        }],
    }
    monkeypatch.setattr(checks, "github", replies.__getitem__)
    if success:
        checks.verify_merged_pr("release")
    else:
        with pytest.raises(ValueError):
            checks.verify_merged_pr("release")


def test_checked_in_version_surfaces_and_tag_agree():
    version = checks.verify_versions(ROOT)
    assert checks.verify_versions(ROOT, f"v{version}") == version
    assert checks.verify_versions(ROOT, f"v{version}-rc.1") == version
    with pytest.raises(ValueError, match="fallback version"):
        checks.verify_versions(ROOT, "v99.0.0")


def test_version_preflight_catches_lock_drift(tmp_path):
    for filename in ("app/__init__.py", "frontend/package.json", "frontend/package-lock.json"):
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / filename, target)
    lockfile = tmp_path / "frontend/package-lock.json"
    lock = json.loads(lockfile.read_text())
    lock["packages"][""]["version"] = "0.0.0"
    lockfile.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="package/lock version drift"):
        checks.verify_versions(tmp_path)
