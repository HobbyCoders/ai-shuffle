"""The update feed's source archive carries runtime paths only."""
from pathlib import Path
import hashlib
import subprocess
import zipfile

import pytest

from scripts import source_archive

FILES = {
    "app/main.py": "print('app')",
    "fluid/pyproject.toml": "[project]",
    "fluid/README.md": "fluid",
    "fluid/fluid/__init__.py": "",
    "fluid/tests/test_x.py": "internal",
    "frontend/package.json": "{}",
    "frontend/src/lib/a.ts": "export {}",
    "frontend/src/lib/a.test.ts": "internal",
    "frontend/tests/e2e.spec.ts": "internal",
    "tools/package.json": "{}",
    "tools/ws_live_gate_probe.py": "internal",
    "browser-mcp/src/index.ts": "export {}",
    "desktop/src/main/index.ts": "export {}",
    "desktop/test/x.test.ts": "internal",
    "docs/content/getting-started/overview.md": "# Overview",
    "docs/reviews/prod-readiness/00-MASTER-PLAN.md": "internal",
    "docs/LOCAL_ACCESS.md": "internal",
    "mockups/a.html": "internal",
    "videos/a.mp4": "internal",
    "scripts/unraid-lab/lab.py": "internal",
    "tests/test_app.py": "internal",
    "android/hub/src/index.ts": "internal",
    ".github/workflows/ci.yml": "internal",
    "requirements.txt": "fastapi",
    "requirements.lock": "fastapi==1 --hash=sha256:" + "0" * 64,
    "requirements-native.lock": "mss==1 --hash=sha256:" + "1" * 64,
    "LICENSE": "Proprietary",
    "README.md": "internal",
}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for name, content in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "fixture")
    return root


def test_archive_holds_only_runtime_paths_under_the_accepted_root(repo, tmp_path):
    revision = git(repo, "rev-parse", "HEAD")
    output = tmp_path / "source.zip"
    result = source_archive.build(revision, output, repo)
    with zipfile.ZipFile(output) as zipped:
        names = {name.split("/", 1)[1] for name in zipped.namelist() if not name.endswith("/")}
        roots = {name.split("/", 1)[0] for name in zipped.namelist()}
    assert roots == {f"ai-shuffle-{revision}"}
    assert names == {
        "app/main.py", "fluid/pyproject.toml", "fluid/README.md", "fluid/fluid/__init__.py",
        "frontend/package.json", "frontend/src/lib/a.ts", "tools/package.json",
        "browser-mcp/src/index.ts", "desktop/src/main/index.ts",
        "docs/content/getting-started/overview.md", "requirements.txt", "requirements.lock",
        "requirements-native.lock", "LICENSE",
    }
    data = output.read_bytes()
    assert result["sha256"] == hashlib.sha256(data).hexdigest() and result["size"] == len(data)
    assert result["files"] == len(names)
    expected = sorted((name, hashlib.sha256(FILES[name].encode()).hexdigest()) for name in names)
    assert result["tree"] == hashlib.sha256(
        b"".join(f"{name}\0{digest}\n".encode() for name, digest in expected)).hexdigest()


def test_archive_is_reproducible_for_a_revision(repo, tmp_path):
    revision = git(repo, "rev-parse", "HEAD")
    first = source_archive.build(revision, tmp_path / "a.zip", repo)
    second = source_archive.build(revision, tmp_path / "b.zip", repo)
    assert first == second


def test_archive_refuses_short_revisions_and_existing_outputs(repo, tmp_path):
    with pytest.raises(ValueError, match="full commit"):
        source_archive.build("main", tmp_path / "a.zip", repo)
    existing = tmp_path / "exists.zip"
    existing.write_text("")
    with pytest.raises(ValueError, match="already exists"):
        source_archive.build(git(repo, "rev-parse", "HEAD"), existing, repo)


def test_real_allow_list_excludes_internal_material():
    spec = source_archive.pathspec()
    for internal in ("docs", "docs/reviews", "mockups", "videos", "scripts", "tests", "android", ".github"):
        assert internal not in spec
    assert "docs/content" in spec and "requirements.lock" in spec and "requirements-native.lock" in spec


def test_archive_refuses_a_revision_without_the_native_lock(repo, tmp_path):
    git(repo, "rm", "-q", "requirements-native.lock")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "drop native lock")
    with pytest.raises(ValueError, match="requirements-native.lock"):
        source_archive.build(git(repo, "rev-parse", "HEAD"), tmp_path / "a.zip", repo)

def test_update_archive_supplies_every_verified_connector_without_repo_access(repo, tmp_path, monkeypatch):
    """Exercise the release archive producer, not a hand-seeded consumer bundle."""
    import json
    import httpx
    from app.core import connector_packages as packages

    sources = packages._SOURCE_DIR
    files = {name for spec in packages.CATALOG["packages"].values() for name in spec["files"]}
    bundled = repo / "app/core/connectors"
    bundled.mkdir(parents=True)
    for name in files | {"catalog.json"}:
        (bundled / name).write_bytes((sources / name).read_bytes())
    git(repo, "add", "app/core/connectors")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "ship connector snapshots")
    revision = git(repo, "rev-parse", "HEAD")
    archive = tmp_path / "application-update.zip"
    source_archive.build(revision, archive, repo)
    unpacked = tmp_path / "buyer-install"
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(unpacked)
    shipped = unpacked / f"ai-shuffle-{revision}" / "app/core/connectors"
    catalog = json.loads((shipped / "catalog.json").read_text())
    monkeypatch.setattr(packages, "CATALOG", catalog)
    monkeypatch.setattr(packages, "_SOURCE_DIR", shipped)
    monkeypatch.setattr(packages, "_prepare_environment", lambda *_: None)
    monkeypatch.setattr(packages, "_validate_worker", lambda *_: None)
    monkeypatch.setattr(packages, "root", lambda: tmp_path / "buyer-runtimes")
    monkeypatch.setattr(httpx.Client, "send", lambda *_a, **_k: pytest.fail("no source download allowed"))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("no GitHub CLI allowed"))
    for variable in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    for kind in catalog["packages"]:
        assert packages.install(kind).connector_type == kind
        assert packages.require_installed(kind) == packages.package_id(kind)
        assert packages.record(kind)["revision"] == catalog["revision"]
