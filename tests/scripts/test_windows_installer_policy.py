"""Exercise the hub/byte verification and real git input-comparison producer."""
import hashlib
import io
import json
import subprocess

import pytest

from scripts.ci import windows_installer_policy as policy
from scripts.ci import verify_release_downloads as check


@pytest.fixture
def release(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Release test")
    git("config", "user.email", "test@example.invalid")
    def write(path, value):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    write("desktop/launcher.json", '{"version":1,"minimum":0}')
    write("desktop/src/main/managed-entry.ts", 'import "./runtime";')
    write("desktop/src/main/runtime.ts", "export const ready = true;")
    write("desktop/package.json", '{"version":"1.0.0","devDependencies":{"electron":"42.0.0"}}')
    write("desktop/package-lock.json", json.dumps({"version": "1.0.0", "packages": {
        "": {"version": "1.0.0", "devDependencies": {"electron": "42.0.0"}},
        "node_modules/electron": {"version": "42.0.0", "dev": True},
        "node_modules/production-dep": {"version": "1.0.0"}}}))
    def commit(tag):
        git("add", ".")
        git("commit", "-qm", tag, "--allow-empty")
        git("tag", tag)
        return git("rev-parse", "HEAD")
    base = commit("v1.0.0")
    original_reader = policy.reach.reader
    monkeypatch.setattr(policy.reach, "git", git)
    monkeypatch.setattr(policy.reach, "reader", lambda rev: original_reader(rev, root=tmp_path))
    data = b"verified Windows installer"
    row = {"version": "1.0.0", "revision": base, "size": len(data),
           "sha256": hashlib.sha256(data).hexdigest(), "url": policy.INSTALLER_URL}
    payload = {"platforms": {"win": row}}
    def fetch(request, timeout):
        assert timeout == 30
        if request.full_url == policy.URL:
            return io.StringIO(json.dumps(payload))
        assert request.full_url == policy.INSTALLER_URL
        return io.BytesIO(data)
    monkeypatch.setattr(policy, "urlopen", fetch)
    return write, commit, row, payload, tmp_path


def test_two_consecutive_stable_versions_without_installer_changes_skip(release):
    write, commit, row, _, _ = release
    for version in ("1.0.1", "1.0.2"):
        # Real release producers bump both package metadata surfaces.
        write("desktop/package.json", json.dumps({"version": version, "devDependencies": {"electron": "42.0.0"}}))
        write("desktop/package-lock.json", json.dumps({"version": version, "packages": {
            "": {"version": version, "devDependencies": {"electron": "42.0.0"}},
            "node_modules/electron": {"version": "42.0.0", "dev": True},
            "node_modules/production-dep": {"version": "1.0.0"}}}))
        write("app/example.py", "# in-app update " + version)
        revision = commit("v" + version)
        decision = policy.decide("v" + version, revision)
        assert decision == {"build": False, "reason": "installer inputs unchanged", "previous": row}
        payload = {"platforms": {"win": row, **{p: {"version": version, "revision": revision} for p in ("linux", "android")}}}
        check.verify(payload, "v" + version, revision, windows_policy=decision, windows_result="skipped")


@pytest.mark.parametrize("path,data", [
    ("desktop/src/main/runtime.ts", "export const ready = false;"),
    ("desktop/launcher.json", '{"version":2,"minimum":0}'),
    ("desktop/build/new/nested.ico", "icon bytes"),
    ("desktop/electron-builder.yml", "appId: changed"),
    ("desktop/package.json", '{"devDependencies":{"electron":"43.0.0"}}'),
    ("desktop/package-lock.json", '{"packages":{"node_modules/electron":{"version":"43.0.0","dev":true},"node_modules/production-dep":{"version":"1.0.0"}}}'),
    ("desktop/package-lock.json", '{"packages":{"node_modules/electron":{"version":"42.0.0","dev":true},"node_modules/production-dep":{"version":"2.0.0"}}}'),
])
def test_installer_input_change_needs_build(release, path, data):
    write, commit, *_ = release
    write(path, data)
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["build"] is True


@pytest.mark.parametrize("failure", ["unreadable", "absent", "checksum", "revision", "unknown-revision", "size", "malformed"])
def test_unverifiable_hub_needs_build(release, monkeypatch, failure):
    _, commit, row, payload, _ = release
    revision = commit("v1.0.1")
    if failure == "unreadable":
        def fail(*args, **kwargs):
            raise OSError("private HTTP body")
        monkeypatch.setattr(policy, "urlopen", fail)
    elif failure == "absent":
        payload["platforms"]["win"] = None
    elif failure == "checksum":
        row["sha256"] = "0" * 64
    elif failure == "revision":
        row["revision"] = "0" * 40
    elif failure == "unknown-revision":
        monkeypatch.setattr(policy, "changed_inputs", lambda *args: (_ for _ in ()).throw(RuntimeError("missing history")))
    elif failure == "size":
        row["size"] += 1
    else:
        payload["platforms"] = None
    decision = policy.decide("v1.0.1", revision)
    assert decision["build"] is True
    assert "cannot be verified" in decision["reason"]
    assert "private HTTP body" not in decision["reason"]


def test_non_installer_build_metadata_does_not_need_windows(release):
    write, commit, _, _, directory = release
    package = json.loads((directory / "desktop/package.json").read_text())
    package["devDependencies"]["typescript"] = "99.0.0"
    write("desktop/package.json", json.dumps(package))
    lock = json.loads((directory / "desktop/package-lock.json").read_text())
    lock["packages"]["node_modules/electron"]["license"] = "MIT"
    lock["packages"]["node_modules/typescript"] = {"version": "99.0.0", "dev": True}
    write("desktop/package-lock.json", json.dumps(lock))
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["build"] is False


def test_forced_build_does_not_need_hub(release, monkeypatch):
    _, commit, *_ = release
    revision = commit("v1.0.1")
    monkeypatch.setattr(policy, "downloads", lambda: pytest.fail("forced build must not read hub"))
    assert policy.decide("v1.0.1", revision, force=True)["build"] is True


@pytest.mark.parametrize("tag", ["v0.9.9", "v1.1.0-rc.1"])
def test_older_patch_and_prerelease_skip_even_if_forced(release, monkeypatch, tag):
    _, commit, *_ = release
    revision = commit(tag)
    monkeypatch.setattr(policy, "downloads", lambda: pytest.fail("ineligible tag must not read hub"))
    assert policy.decide(tag, revision, force=True)["build"] is False


@pytest.mark.parametrize("fault", ["pointer", "checksum", "inputs", "required", "failed-job"])
def test_skipped_verification_fails_closed_and_names_win(release, fault):
    write, commit, row, _, _ = release
    revision = commit("v1.0.1")
    decision = policy.decide("v1.0.1", revision)
    previous = dict(row)
    if fault == "pointer":
        previous["version"] = "0.9.9"
    elif fault == "checksum":
        previous["sha256"] = "0" * 64
        decision["previous"] = previous
    elif fault == "inputs":
        write("desktop/src/main/runtime.ts", "changed")
        revision = commit("v1.0.2")
    elif fault == "required":
        decision["build"] = True
    payload = {"platforms": {"win": previous, **{p: {"version": "1.0.1", "revision": revision} for p in ("linux", "android")}}}
    with pytest.raises(ValueError, match="win:"):
        check.verify(payload, "v1.0.1", revision, windows_policy=decision,
                     windows_result="failure" if fault == "failed-job" else "skipped")


@pytest.mark.parametrize("platform", ["linux", "android"])
def test_skipped_windows_does_not_relax_other_platforms(release, platform):
    _, commit, row, _, _ = release
    revision = commit("v1.0.1")
    decision = policy.decide("v1.0.1", revision)
    payload = {"platforms": {"win": row, **{p: {"version": "1.0.1", "revision": revision} for p in ("linux", "android")}}}
    payload["platforms"][platform]["revision"] = row["revision"]
    with pytest.raises(ValueError, match=platform + ":"):
        check.verify(payload, "v1.0.1", revision, windows_policy=decision, windows_result="skipped")


def test_dispatch_cli_emits_verified_decision_and_snapshot(release, monkeypatch):
    _, commit, row, _, directory = release
    revision = commit("v1.0.1")
    output = directory / "github-output"
    monkeypatch.setattr("sys.argv", ["policy", "--tag", "v1.0.1", "--revision", revision, "--github-output", str(output)])
    assert policy.main() == 0
    lines = output.read_text().splitlines()
    assert lines[0] == "build=false"
    decision = json.loads(lines[1].removeprefix("policy="))
    assert decision["previous"] == row
    monkeypatch.setattr("sys.argv", ["policy", "--tag", "v1.0.1", "--revision", revision, "--force", "--github-output", str(output)])
    assert policy.main() == 0
    assert output.read_text().splitlines()[2] == "build=true"


@pytest.mark.parametrize("result", ["success", "promoted"])
def test_built_verification_requires_new_tag(release, result):
    _, commit, old, _, _ = release
    revision = commit("v1.0.1")
    decision = {"build": True, "previous": None}
    payload = {"platforms": {p: {"version": "1.0.1", "revision": revision} for p in check.WORKFLOWS}}
    check.verify(payload, "v1.0.1", revision, windows_policy=decision, windows_result=result)
    payload["platforms"]["win"] = old
    with pytest.raises(ValueError, match="win:"):
        check.verify(payload, "v1.0.1", revision, windows_policy=decision, windows_result=result)
