"""Exercise the hub/byte verification and real git input-comparison producer."""
import hashlib
import io
import json
import subprocess

import pytest

from scripts.ci import windows_installer_policy as policy
from scripts.ci import verify_release_downloads as check


@pytest.fixture
def github():
    return {}


@pytest.fixture
def release(tmp_path, monkeypatch, github):
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
    name = "AI-Shuffle-Desktop-1.0.0-win-x64.exe"
    github.update({
        "release": {"tag_name": "v1.0.0", "draft": False, "assets": [
            {"name": name, "size": len(data), "digest": "sha256:" + row["sha256"]},
            {"name": "SHA256SUMS"}, {"name": "desktop-build.json"}]},
        "SHA256SUMS": (row["sha256"] + "  " + name + "\n").encode(),
        "desktop-build.json": json.dumps({"revision": base}).encode(),
    })
    def fetch(request, timeout):
        assert timeout == 30
        if request.full_url == policy.URL:
            return io.StringIO(json.dumps(payload))
        if request.full_url == policy.RELEASE_API + "v1.0.0":
            return io.StringIO(json.dumps(github["release"]))
        if request.full_url.startswith(policy.RELEASE_DOWNLOAD + "v1.0.0/"):
            return io.BytesIO(github[request.full_url.rsplit("/", 1)[1]])
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
    ("desktop/scripts/build-worker.mjs", "changed installer input"),
    ("desktop/scripts/postgres-binaries.mjs", "changed installer input"),
    ("desktop/scripts/gh-binaries.mjs", "changed installer input"),
    ("desktop/scripts/bundled-symlinks.mjs", "changed installer input"),
    ("desktop/scripts/runtime-contract.mjs", "changed installer input"),
    ("desktop/scripts/stamp-desktop-build.mjs", "changed installer input"),
    ("desktop/scripts/launcher-contract.mjs", "changed installer input"),
    ("desktop/scripts/signing.mjs", "changed installer input"),
    ("desktop/scripts/signing-preflight.mjs", "changed installer input"),
    ("desktop/scripts/verify-packaged-runtime.cjs", "changed installer input"),
    ("desktop/runtime-contract.json", "changed installer input"),
    ("desktop/tsconfig.json", "changed installer input"),
    (".github/workflows/desktop-installer.yml", "changed installer input"),
    ("desktop/vendor/new.whl", "changed installer input"),
    ("requirements.lock", "changed installer input"),
    ("requirements-native.lock", "changed installer input"),
    ("fluid/pyproject.toml", "changed installer input"),
    ("app/core/cloudflared.py", "changed installer input"),
    ("desktop/package.json", '{"devDependencies":{"electron":"43.0.0"}}'),
    ("desktop/package-lock.json", '{"packages":{"node_modules/electron":{"version":"43.0.0","dev":true},"node_modules/production-dep":{"version":"1.0.0"}}}'),
    ("desktop/package-lock.json", '{"packages":{"node_modules/electron":{"version":"42.0.0","dev":true},"node_modules/production-dep":{"version":"2.0.0"}}}'),
])
def test_installer_input_change_needs_build(release, path, data):
    write, commit, *_ = release
    write(path, data)
    revision = commit("v1.0.1")
    decision = policy.decide("v1.0.1", revision)
    assert decision["build"] is True
    assert decision["reason"] == "installer inputs changed"


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


def test_typescript_build_change_needs_windows(release):
    write, commit, _, _, directory = release
    package = json.loads((directory / "desktop/package.json").read_text())
    package["devDependencies"]["typescript"] = "99.0.0"
    write("desktop/package.json", json.dumps(package))
    lock = json.loads((directory / "desktop/package-lock.json").read_text())
    lock["packages"]["node_modules/electron"]["license"] = "MIT"
    lock["packages"]["node_modules/typescript"] = {"version": "99.0.0", "dev": True}
    write("desktop/package-lock.json", json.dumps(lock))
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["build"] is True


@pytest.mark.parametrize("dependency", ["electron", "electron-builder", "typescript", "@types/node", "builder-transitive"])
def test_locked_build_dependency_change_needs_windows(release, dependency):
    write, commit, _, _, directory = release
    lock = json.loads((directory / "desktop/package-lock.json").read_text())
    lock["packages"]["node_modules/" + dependency] = {"version": "99.0.0", "dev": True}
    write("desktop/package-lock.json", json.dumps(lock))
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["reason"] == "installer inputs changed"


def test_package_build_script_change_needs_windows(release):
    write, commit, _, _, directory = release
    package = json.loads((directory / "desktop/package.json").read_text())
    package["scripts"] = {"build": "tsc --new-build-option"}
    write("desktop/package.json", json.dumps(package))
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["reason"] == "installer inputs changed"


def test_lock_descriptive_metadata_does_not_need_windows(release):
    write, commit, _, _, directory = release
    lock = json.loads((directory / "desktop/package-lock.json").read_text())
    lock["packages"]["node_modules/electron"]["license"] = "MIT"
    write("desktop/package-lock.json", json.dumps(lock))
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["build"] is False


@pytest.mark.parametrize("token", [None, "", "test-release-token"])
def test_github_api_authentication_is_not_sent_to_asset_downloads(release, monkeypatch, token):
    _, _, row, _, _ = release
    if token is None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GH_TOKEN", token)
    requests = []
    fetch = policy.urlopen

    def capture(request, timeout):
        requests.append(request)
        return fetch(request, timeout)

    monkeypatch.setattr(policy, "urlopen", capture)
    policy.verify_installer(row)
    api = [request for request in requests if request.full_url.startswith(policy.RELEASE_API)]
    assert len(api) == 1
    assert api[0].get_header("Authorization") == ("Bearer " + token if token else None)
    downloads = [request for request in requests if not request.full_url.startswith(policy.RELEASE_API)]
    assert len(downloads) == 3  # GitHub checksums/build metadata and the hub installer.
    assert all(request.get_header("Authorization") is None for request in downloads)


@pytest.mark.parametrize("api_digest", [True, False])
def test_github_release_assets_match_hub(release, github, api_digest):
    _, commit, row, _, _ = release
    if not api_digest:
        del github["release"]["assets"][0]["digest"]
    policy.verify_installer(row)
    revision = commit("v1.0.1")
    assert policy.decide("v1.0.1", revision)["build"] is False


@pytest.mark.parametrize("fault", ["digest", "size", "checksum", "build-revision", "missing-build-revision",
                                  "missing-exe", "missing-sums", "missing-build", "duplicate-exe",
                                  "draft", "wrong-tag", "unreadable-build"])
def test_unverifiable_github_release_needs_build(release, github, fault):
    _, commit, row, _, _ = release
    assets = github["release"]["assets"]
    if fault == "digest":
        assets[0]["digest"] = "sha256:" + "0" * 64
    elif fault == "size":
        assets[0]["size"] += 1
    elif fault == "checksum":
        github["SHA256SUMS"] = b"0" * 64 + b"  " + assets[0]["name"].encode()
    elif fault in {"build-revision", "missing-build-revision"}:
        github["desktop-build.json"] = json.dumps({"revision": "0" * 40} if fault == "build-revision" else {}).encode()
    elif fault.startswith("missing-"):
        index = {"missing-exe": 0, "missing-sums": 1, "missing-build": 2}[fault]
        assets.pop(index)
    elif fault == "duplicate-exe":
        assets.append(dict(assets[0]))
    elif fault == "draft":
        github["release"]["draft"] = True
    elif fault == "wrong-tag":
        github["release"]["tag_name"] = "v0.9.9"
    elif fault == "unreadable-build":
        github["desktop-build.json"] = b"invalid JSON"
    revision = commit("v1.0.1")
    decision = policy.decide("v1.0.1", revision)
    assert decision["build"] is True
    assert "cannot be verified" in decision["reason"]
    assert decision["previous"] is None


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
