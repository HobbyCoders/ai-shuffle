"""Candidate checkouts are data, never publisher code or executable dependencies."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("source_reader", ROOT / "scripts/ci/publication_source.py")
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def init(root):
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Publication test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(root / "no-hooks"))


def put(root, name, content):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-qm", "Fixture")
    return git(root, "rev-parse", "HEAD")


def test_two_checkouts_publish_without_executing_candidate_helpers(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    control, candidate, run = tmp_path / "control", tmp_path / "candidate", tmp_path / "run"
    init(control)
    init(candidate)
    run.mkdir()
    # A small real control checkout, not a replacement publisher implementation.
    files = ["desktop/scripts/publish-source.mjs", "desktop/scripts/source-pointer.mjs", "desktop/scripts/trusted-keys.mjs",
             "desktop/scripts/launcher-contract.mjs", "desktop/scripts/release-repository.mjs",
             "scripts/ci/publication_source.py", "scripts/ci/edge_publication_gate.py",
             "scripts/ci/public_cutover.py", "scripts/source_archive.py",
             "android/ci/resolve-version-name.py", "docker/update_contract.py"]
    for name in files:
        put(control, name, (ROOT / name).read_text())
    commit(control)
    sentinel = tmp_path / "CANDIDATE_EXECUTED"
    poison = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\nraise SystemExit(99)\n"
    for name in ["scripts/source_archive.py", "scripts/ci/publication_source.py",
                 "scripts/ci/public_cutover.py", "scripts/ci/edge_publication_gate.py",
                 "scripts/release/sequence_offset.py", "android/ci/resolve-version-name.py",
                 "json.py", "sitecustomize.py"]:
        put(candidate, name, poison)
    put(candidate, "docker/update_contract.py", "PROVIDES = ('base-1', 'source-supervisor')\nREQUIRES = ('base-1',)\nRECOMMENDS = ('source-supervisor',)\n" + poison)
    for name in ["app/data.txt", "fluid/fluid/data.txt", "frontend/data.txt",
                 "requirements.lock", "requirements-native.lock"]:
        put(candidate, name, "candidate data, not control data")
    put(candidate, "desktop/launcher.json", '{"version": 9, "minimum": 7}')
    put(candidate, "release/container-sequence-offset", "0\n")
    commit(candidate)
    git(candidate, "tag", "v1.2.3")
    put(candidate, "app/later.txt", "new candidate revision")
    revision = commit(candidate)
    wrangler = "scripts/source-publisher/node_modules/wrangler/bin/wrangler.js"
    put(candidate, wrangler, f"require('node:fs').writeFileSync({json.dumps(str(sentinel))}, 'node'); process.exit(99);")
    put(candidate, "scripts/source-publisher/package.json", '{"scripts":{"postinstall":"exit 99"}}')
    put(candidate, "scripts/source-publisher/package-lock.json", "candidate lockfile must never be installed")
    put(control, wrangler, (ROOT / "desktop/test/helpers/publish-source-wrangler.cjs").read_text())
    env = {**os.environ, "RUNNER_TEMP": str(run), "SOURCE_CHANNEL": "edge", "GITHUB_SHA": revision,
           "SOURCE_REVISION": revision, "GH_TOKEN": "fixture", "RELEASE_GITHUB_REPO_ID": "42",
           "ACCOUNT_HUB_DATABASE_ID": "11111111-1111-1111-1111-111111111111",
           "CLOUDFLARE_API_TOKEN": "fixture", "PUBLISH_TEST_CASE": "two-checkout",
           "PUBLISH_TEST_CONTROL": str(control), "PUBLISH_TEST_REVISION": revision}
    # Deliberately launch from candidate cwd: every subprocess must override it.
    result = subprocess.run([node, "--import", (ROOT / "desktop/test/helpers/publish-source-preload.mjs").as_uri(),
                             str(control / "desktop/scripts/publish-source.mjs"), "--source-root", str(candidate)],
                            cwd=candidate, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "Published edge" in result.stdout
    assert not sentinel.exists()
    assert (run / "trusted-wrangler-called").read_text() == "yes"
    row = json.loads((run / "record.json").read_text())
    manifest = json.loads(base64.b64decode(json.loads(row["manifest"])["payload"]))
    assert manifest["revision"] == revision
    assert manifest["version"].startswith("1.2.3-dev.1+")
    assert manifest["desktop"] == {"launcher": 7}
    assert manifest["container"]["sequence"] == 2
    assert manifest["container"]["recommends"] == ["source-supervisor"]
    events = json.loads((run / "events.json").read_text())
    assert {"source:version", "source:archive", "source:signing-floor", "source:sequence", "source:contract"} <= set(events)
    # The image verifier also reads literals, never importing the poisoned module.
    import sys
    raw = subprocess.check_output([sys.executable, "-I", str(control / "scripts/ci/publication_source.py"),
                                   "image-record", "--source-root", str(candidate), "--revision", revision],
                                  cwd=control, text=True)
    record = json.loads(raw)
    trusted = reader.load("docker/update_contract.py")
    assert record == {"format": 2, "revision": revision, "sequence": 2,
                      "runtime": trusted.runtime(trusted.LEGACY_PROVIDES),
                      "provides": ["base-1", "source-supervisor"],
                      "source_tree": git(candidate, "rev-parse", f"{revision}^{{tree}}")}
    assert not sentinel.exists()


@pytest.mark.parametrize("declaration", ["REQUIRES = dangerous()", "REQUIRES = ('base-1',)\nREQUIRES = ('other',)", "REQUIRES = 'base-1'"])
def test_contract_reader_refuses_nonliteral_ambiguous_or_invalid_data(monkeypatch, declaration):
    monkeypatch.setattr(reader, "capability_source", lambda *args: declaration + "\nRECOMMENDS = ()")
    with pytest.raises((ValueError, TypeError)):
        reader.contract(Path.cwd(), "a" * 40)

@pytest.mark.parametrize("action", ["contract", "image-record"])
def test_oversized_capability_blob_is_rejected_before_read_or_parse(tmp_path, monkeypatch, action):
    candidate = tmp_path / "candidate"
    init(candidate)
    put(candidate, "docker/update_contract.py", "#" * (reader.MAX_CAPABILITY_BYTES + 1))
    revision = commit(candidate)
    actual = reader.subprocess.check_output
    def bounded_command(argv, **kwargs):
        assert not ("cat-file" in argv and "blob" in argv), "oversized blob must not be read"
        return actual(argv, **kwargs)
    monkeypatch.setattr(reader.subprocess, "check_output", bounded_command)
    with pytest.raises(ValueError, match="256 KiB size limit"):
        reader.read_source(action, candidate, revision)


def test_capability_ast_node_budget_is_enforced_before_literals(monkeypatch):
    # Under the byte limit but many small AST nodes; the tail is deliberately
    # valid so acceptance would be possible without the complexity guard.
    source = b"x = 0\n" * reader.MAX_CAPABILITY_AST_NODES + b"REQUIRES = ('base-1',)\nRECOMMENDS = ()\n"
    assert len(source) < reader.MAX_CAPABILITY_BYTES
    monkeypatch.setattr(reader, "capability_source", lambda *args: source)
    with pytest.raises(ValueError, match="AST node limit"):
        reader.contract(Path.cwd(), "a" * 40)


def test_excessive_capability_nesting_has_clear_refusal(monkeypatch):
    source = "x = " + "[" * 300 + "0" + "]" * 300
    monkeypatch.setattr(reader, "capability_source", lambda *args: source)
    with pytest.raises(ValueError, match="invalid or excessively nested syntax"):
        reader.contract(Path.cwd(), "a" * 40)
