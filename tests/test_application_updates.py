import base64
import hashlib
import io
import json
import stat
import zipfile

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core import application_updates as policy
from docker.application_updates import ContainerUpdates, UpdateError, build_environment, extract_source, verify_manifest

A, B, C = "a" * 40, "b" * 40, "c" * 40
RUNTIME = "d" * 64


@pytest.fixture
def feed(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    responses, requests = {}, []
    def publish(revision=B, sequence=2, runtime=RUNTIME, entries=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in (entries or {f"ai-shuffle-{revision}/source.txt": "content"}).items():
                archive.writestr(name, value)
        archive = output.getvalue()
        payload = json.dumps({"format": 2, "revision": revision, "sequence": sequence,
                              "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
                              "container": {"sequence": sequence, "runtime": runtime}}).encode()
        envelope = json.dumps({"payload": base64.b64encode(payload).decode(),
                               "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
        responses["/v1/desktop/manifest"] = envelope
        responses["/v1/desktop/sources/" + revision] = archive
        return envelope
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=responses[request.url.path])
    client = httpx.Client
    monkeypatch.setattr("docker.application_updates.httpx.Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    enabled = [True]
    image = {"revision": A, "sequence": 1, "runtime": RUNTIME}
    updater = ContainerUpdates(tmp_path / "updates", image, public, lambda: enabled[0], lambda action: {})
    def build(directory, revision):
        assert (directory / "source/source.txt").read_text() == "content"
        for name in ("venv/bin/python", "source/app/static/index.html", "complete"):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(revision)
    monkeypatch.setattr(updater, "build", build)
    return updater, publish, enabled, requests, responses


def test_signed_download_stages_then_explicit_restart_and_health_commit(feed):
    updater, publish, enabled, requests, _ = feed
    publish()
    updater.check()
    assert len(requests) == 1 and not updater.state.get("pending")
    updater.check(download=True)
    assert updater.state == {"pending": B}
    assert updater.launch({})[1] is None, "ordinary container restart must not activate staging"
    command, cwd, env = updater.launch({"PYTHONPATH": "wrong", "PATH": "/usr/bin"}, activate=True)
    assert command[0].endswith("versions/" + B + "/venv/bin/python")
    assert "PYTHONPATH" not in env and env["GIT_SHA"] == B
    assert not updater.state.get("booting"), "validation must not mark old worker as new"
    updater.activating()
    updater.healthy()
    assert updater.state == {"active": B}
    offline = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: False, lambda _: {})
    assert offline.launch({})[1] == cwd
    assert all("authorization" not in request.headers for request in requests)


def test_disabled_before_download_and_after_staging_blocks_activation(feed):
    updater, publish, enabled, requests, _ = feed
    publish()
    enabled[0] = False
    with pytest.raises(UpdateError, match="disabled"):
        updater.check(download=True)
    assert requests == []
    enabled[0] = True
    updater.check(download=True)
    enabled[0] = False
    with pytest.raises(UpdateError, match="disabled"):
        updater.launch({}, activate=True)
    assert not updater.state.get("booting")


def test_policy_disabled_during_build_never_stages(feed, monkeypatch):
    updater, publish, enabled, _, _ = feed
    publish()
    monkeypatch.setattr(updater, "build", lambda *_: enabled.__setitem__(0, False))
    with pytest.raises(UpdateError, match="disabled"):
        updater.check(download=True)
    assert updater.state == {} and not updater.directory(B).exists()


def test_runtime_mismatch_requires_image_and_older_source_is_ignored(feed):
    updater, publish, _, requests, _ = feed
    publish(runtime="e" * 64)
    updater.check(download=True)
    assert json.loads((updater.root / "status.json").read_text())["phase"] == "image-required"
    assert len(requests) == 1
    publish(sequence=1)
    updater.check(download=True)
    assert updater.state == {}


def test_cleanup_retains_active_previous_staged_and_failed_revisions(feed):
    updater, _, _, _, _ = feed
    for revision in (A, B, C, "e" * 40, "f" * 40):
        updater.directory(revision).mkdir(parents=True)
    updater.state = {"active": A, "previous": B, "pending": C, "failed": ["e" * 40]}
    updater.prune()
    assert updater.directory(A).exists() and updater.directory(B).exists()
    assert updater.directory(C).exists() and updater.directory("e" * 40).exists()
    assert not updater.directory("f" * 40).exists()


def test_new_image_discards_obsolete_pending_and_never_downgrades(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    newer = ContainerUpdates(updater.root, dict(updater.image, revision=C, sequence=3), updater.public_key, lambda: True, lambda _: {})
    assert newer.state == {} and newer.launch({})[1] is None
    with pytest.raises(UpdateError, match="No application update"):
        newer.launch({}, activate=True)


def test_failed_first_boot_never_rolls_back_and_newer_milestone_can_recover(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, dict(updater.image, revision=B, sequence=2), updater.public_key, lambda: True, lambda _: {})
    newer = ContainerUpdates(updater.root, dict(updater.image, revision=C, sequence=3), updater.public_key, lambda: True, lambda _: {})
    assert newer.state == {} and newer.launch({})[1] is None


def test_bad_signature_and_checksum_rejected_before_build(feed):
    updater, publish, _, _, responses = feed
    signed = publish()
    envelope = json.loads(signed)
    envelope["signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(UpdateError, match="unsigned"):
        verify_manifest(json.dumps(envelope).encode(), updater.public_key)
    responses["/v1/desktop/sources/" + B] = b"corrupt"
    with pytest.raises(UpdateError, match="checksum"):
        updater.check(download=True)
    assert updater.state == {} and not updater.directory(B).exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", f"ai-shuffle-{B}/../escape", f"ai-shuffle-{B}/a\\b", f"ai-shuffle-{B}/C:drive"])
def test_archive_rejects_traversal_before_writes(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(name, "bad")
    with pytest.raises(UpdateError):
        extract_source(archive, tmp_path / "source", B)
    assert not (tmp_path / "source").exists()


def test_archive_rejects_links(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        info = zipfile.ZipInfo(f"ai-shuffle-{B}/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, "/etc/passwd")
    with pytest.raises(UpdateError):
        extract_source(archive, tmp_path / "source", B)


def test_build_environment_never_inherits_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("NODE_OPTIONS", "secret")
    env = build_environment(tmp_path)
    assert all("secret" not in value for value in env.values())
    assert env["PIP_CONFIG_FILE"] == "/dev/null"


@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("false", False), ("invalid", False)])
def test_default_enabled_policy_and_desktop_mirror(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: value)
    mirror = tmp_path / "allow-updates.json"
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(mirror))
    assert policy.mirror_policy() is expected
    assert json.loads(mirror.read_text()) == {"allow_updates": expected}


def test_account_policy_is_shared():
    from app.network.policy import is_shared_setting_key
    assert is_shared_setting_key(policy.KEY)
