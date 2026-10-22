"""Path precedence, hostile diffs and real git metadata must fail closed."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.ci import changed_paths as cp

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path,area", [
    ("README.md", None), ("CONTRIBUTING.md", None), ("CHANGELOG.md", None), ("SECURITY.md", None),
    ("docs/a.md", None), ("docs/image.svg", None), ("docs/a.rst", None),
    ("frontend/src/lib/docs/content/a.md", None),
    ("frontend/src/page.svelte", "frontend"), ("mobile/a.ts", "frontend"),
    (".github/workflows/ci-backend.yml", "workflows"), (".github/workflows/a.yaml", "workflows"),
    ("browser-mcp/a.ts", "browser"), ("tools/a.js", "tools"), ("desktop/a.mjs", "desktop"),
    ("app/README.md", "all"), ("fluid/tests/README.md", "all"), ("docker/a.md", "all"),
    ("tests/a.md", "all"), ("scripts/a.md", "all"), ("requirements-dev.txt", "all"),
    ("requirements-voice.lock", "all"), ("requirements-anything", "all"),
    ("Dockerfile", "all"), ("Dockerfile.dev", "all"), ("docker-compose.yml", "all"),
    ("compose.test.yaml", "all"), ("pyproject.toml", "all"), ("pytest.ini", "all"),
    ("setup.cfg", "all"), (".python-version", "all"), (".gitleaks.toml", "all"),
    ("docs/install/a.md", "all"), ("docs/unknown.json", "all"), ("docs/a.py", "all"),
    (".github/actions/a.yml", "all"), (".github/CODEOWNERS", "all"), ("new-root/a.md", "all"),
    ("package.json", "all"), ("../docs/a.md", "all"), ("/docs/a.md", "all"),
    ("docs//a.md", "all"), ("docs/./a.md", "all"), ("docs/../a.md", "all"),
])
def test_priority(path, area):
    result = cp.classify([path])
    assert {a for a in cp.AREAS if result[f"full_{a}"]} == (set(cp.AREAS) if area == "all" else {area} if area else set())


@pytest.mark.parametrize("paths,areas", [
    (["docs/a.md", "frontend/a.ts"], {"frontend"}),
    (["README.md", ".github/workflows/a.yml"], {"workflows"}),
    (["frontend/a.ts", ".github/workflows/a.yml", "tools/a.ts"], {"frontend", "workflows", "tools"}),
    (["desktop/a.mjs", "browser-mcp/a.ts"], {"desktop", "browser"}),
    (["docs/a.md", "app/a.md"], set(cp.AREAS)),
    (["frontend/a.ts", "tests/a.py"], set(cp.AREAS)),
    (["docs/a.md", "unknown/a.md"], set(cp.AREAS)),
    ([], set(cp.AREAS)),
])
def test_mixed_union(paths, areas):
    result = cp.classify(paths)
    assert {a for a in cp.AREAS if result[f"full_{a}"]} == areas


@pytest.mark.parametrize("data", [b"", b"M\0docs/a.md", b"M\0", b"R100\0a\0b\0", b"T\0a\0", b"Q\0a\0", b"M\0\xff\0"])
def test_malformed_diff(data):
    with pytest.raises((ValueError, UnicodeError)):
        cp.parse_diff(data)


def test_nul_diff_handles_rename_sides_and_unusual_names():
    assert cp.parse_diff(b"D\0app/old.md\0A\0docs/new name\n.md\0") == [
        ("D", "app/old.md"), ("A", "docs/new name\n.md")]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, stderr=subprocess.PIPE).decode().strip()
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "CI Test")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_text("initial")
    git("add", ".")
    git("commit", "-qm", "base")
    return tmp_path, git, git("rev-parse", "HEAD")


def commit(git):
    git("add", "-A")
    git("commit", "-qm", "change")
    return git("rev-parse", "HEAD")


def test_real_git_docs_delete(repo):
    path, git, base = repo
    (path / "docs/a.md").unlink()
    assert cp.classify_diff(base, commit(git))["full_backend"] is False


def test_real_git_rename_sensitive_to_docs(repo):
    path, git, _ = repo
    (path / "app").mkdir()
    (path / "app/a.md").write_text("sensitive")
    base = commit(git)
    git("mv", "app/a.md", "docs/moved.md")
    result = cp.classify_diff(base, commit(git))
    assert result["full_backend"] is True
    assert result["paths"] == ["app/a.md", "docs/moved.md"]


@pytest.mark.parametrize("kind", ["symlink", "executable", "submodule", "mode-change"])
def test_real_git_unsafe_modes(repo, kind):
    path, git, base = repo
    if kind == "symlink":
        (path / "docs/link.md").symlink_to("a.md")
    elif kind in {"executable", "mode-change"}:
        target = path / ("docs/a.md" if kind == "mode-change" else "docs/exec.md")
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    else:
        git("update-index", "--add", "--cacheinfo", f"160000,{base},docs/submodule.md")
        git("commit", "-qm", "submodule")
        assert cp.classify_diff(base, git("rev-parse", "HEAD"))["full_backend"] is True
        return
    assert cp.classify_diff(base, commit(git))["full_backend"] is True


def test_missing_history_and_empty_diff(repo):
    _, _, base = repo
    for before, after in [(None, base), ("x", base), ("a" * 40, base), (base, base)]:
        assert cp.classify_diff(before, after)["full_backend"] is True


def healthy_gate(full=True):
    needs = {name: {"result": "success"} for name in
             ("changes", "backend-shard", "backend-contracts", "backend-security")}
    needs["changes"]["outputs"] = {"full_backend": str(full).lower()}
    receipts = [{"shard": i, "head": "head", "count": 2, "total": 8,
                 "collection": "collection", "partition": "partition", "workers": 2} for i in range(1, 5)]
    if not full:
        for job in ("backend-shard", "backend-contracts"):
            needs[job]["result"] = "skipped"
        receipts = []
    return needs, receipts


@pytest.mark.parametrize("full", [True, False])
def test_gate_accepts_only_explicit_coverage(full):
    needs, receipts = healthy_gate(full)
    cp.check_gate(needs, receipts, "head")


@pytest.mark.parametrize("job", ["changes", "backend-shard", "backend-contracts", "backend-security"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", None])
def test_fault_injected_gate_blocks_unsuccessful_jobs(job, result):
    needs, receipts = healthy_gate()
    needs[job]["result"] = result
    with pytest.raises(ValueError):
        cp.check_gate(needs, receipts, "head")


@pytest.mark.parametrize("fault", ["missing-job", "missing-shard", "duplicate-shard", "empty-shard", "wrong-head", "wrong-total", "wrong-collection", "wrong-partition", "missing-worker", "unknown-output"])
def test_fault_injected_gate_blocks_incomplete_evidence(fault):
    needs, receipts = healthy_gate()
    if fault == "missing-job":
        del needs["backend-shard"]
    elif fault == "missing-shard":
        receipts.pop()
    elif fault == "duplicate-shard":
        receipts[0]["shard"] = 2
    elif fault == "unknown-output":
        needs["changes"]["outputs"]["full_backend"] = "maybe"
    else:
        key, value = {"empty-shard": ("count", 0), "wrong-head": ("head", "other"),
                      "wrong-total": ("total", 9), "wrong-collection": ("collection", "other"),
                      "wrong-partition": ("partition", "other"), "missing-worker": ("workers", 1)}[fault]
        receipts[0][key] = value
    with pytest.raises(ValueError):
        cp.check_gate(needs, receipts, "head")


@pytest.mark.parametrize("full", [True, False])
def test_gate_cli_exits_nonzero_on_cancelled_shard(tmp_path, full):
    needs, _ = healthy_gate(full)
    needs["backend-shard"]["result"] = "cancelled"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/changed_paths.py"),
                             "--gate", "--head", "head", "--receipts", str(tmp_path)],
                            env={**os.environ, "BACKEND_NEEDS": json.dumps(needs)}, capture_output=True)
    assert result.returncode != 0
    assert b"cancelled" in result.stderr
