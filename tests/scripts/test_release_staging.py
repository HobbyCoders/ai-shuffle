"""Adversarial whole-release tests: generated keys, local artifacts and fake sinks."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.ci import release_staging as p
from scripts.ci import release_staging_github as gh
from scripts.ci.release_staging_play import Play

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def candidate():
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    files = {}
    def artifact(name):
        data = name.encode()
        row = {"id": len(files) + 1, "name": name, "size": len(data), "digest": "sha256:" + p.sha256(data)}
        files[row["id"]] = ({**row, "state": "uploaded"}, data)
        return row
    assets = [artifact(name) for name in ("AI-Shuffle-Android.apk", "AI-Shuffle-Android.apk.sha256", "android-build.json",
        "app-release.aab", "signing-certificate.pem", "linux-build-provenance.json", "SHA256SUMS-linux", "app.AppImage", "app.deb",
        "app.exe", "desktop-build.json", "SHA256SUMS")]
    archive = artifact("runtime.zip")
    source = {"format": 2, "revision": "a" * 40, "channel": "stable", "version": "1.2.3", "sequence": 500,
              "container": {"sequence": 100}, "sha256": archive["digest"][7:], "size": archive["size"]}
    record = {"kind": p.KIND, "tag": "v1.2.3", "revision": "a" * 40, "tree": "b" * 40,
              "repository": {"name": "example/app", "id": 42}, "release_id": 9, "windows": True,
              "assets": assets, "source": {"manifest": p.sign_record(source, private).decode(), "asset": archive},
              "image_digest": "sha256:" + "c" * 64, "evidence": {name: "success" for name in p.REQUIRED | {"windows"}},
              "play": {"version_code": 51, "asset_id": next(a["id"] for a in assets if a["name"] == "app-release.aab")}}
    state = {key: copy.deepcopy(record[key]) for key in ("tag", "revision", "tree", "repository", "release_id")}
    state.update(newest_tag="v1.2.3", owner=True, merged_pr=True, exact_tree=True, update_reach=True,
                 sources=[], installers=[], installer_floor=10, historical_source_floor=20, historical_image_floor=30)
    return SimpleNamespace(private=private, public=public, record=record, state=state, files=files, source=source)


class Fake:
    def __init__(self, candidate):
        self.c = candidate
        self.public_key = candidate.public
        self.state = copy.deepcopy(candidate.state)
        self.active = {}
        self.writes = []
        self.fail_after = None
        self.fail_before = None
        self.noop = None
        self.image_checked = False
        self.provenance_checked = False

    def current(self, record):
        return copy.deepcopy(self.state)

    def asset(self, asset_id):
        return self.c.files[asset_id]

    def verify_image(self, digest):
        assert digest == self.c.record["image_digest"]
        self.image_checked = True

    def verify_provenance(self, record):
        self.provenance_checked = True

    def matches(self, sink, record):
        return self.active.get(sink) == record

    def write(self, sink, record):
        assert self.image_checked and self.provenance_checked
        if sink == self.fail_before:
            self.fail_before = None
            raise OSError("injected failure before write")
        self.writes.append(sink)
        if sink == self.noop:
            return
        self.active[sink] = copy.deepcopy(record)
        if sink == "source":
            self.state["sources"].append({"revision": record["revision"], "channel": "stable",
                "manifest": record["source"]["manifest"], "sequence": self.c.source["sequence"],
                "asset_id": record["source"]["asset"]["id"], "release_repository_id": record["repository"]["id"],
                "release_repository": record["repository"]["name"], "withdrawn_at": None})
        if sink == self.fail_after:
            self.fail_after = None
            raise OSError("injected lost acknowledgement")


def envelope(c):
    return p.sign_record(c.record, c.private)


@pytest.mark.parametrize("sink", ["source", "installer", "docker", "release"])
@pytest.mark.parametrize("point", ["fail_before", "fail_after"])
def test_partial_activation_then_retry(candidate, sink, point):
    fake = Fake(candidate)
    setattr(fake, point, sink)
    original = envelope(candidate)
    with pytest.raises(OSError):
        p.activate(original, fake)
    p.activate(original, fake)
    assert fake.writes == ["source", "installer", "docker", "release"]
    p.activate(original, fake)
    assert len(fake.writes) == 4
    assert fake.state["sources"][0]["manifest"] == candidate.record["source"]["manifest"]
    assert fake.state["sources"][0]["sequence"] == 500


def test_readback_is_required(candidate):
    fake = Fake(candidate)
    fake.noop = "docker"
    with pytest.raises(ValueError, match="read-back"):
        p.activate(envelope(candidate), fake)
    assert "release" not in fake.writes


@pytest.mark.parametrize("field,value", [("revision", "d" * 40), ("tree", "d" * 40), ("tag", "v1.2.4"),
    ("repository", {"name": "example/app", "id": 99}), ("release_id", 10), ("owner", False), ("merged_pr", False),
    ("exact_tree", False), ("update_reach", False), ("newest_tag", "v1.2.4"), ("installer_floor", 500),
    ("historical_source_floor", 500), ("historical_image_floor", 100)])
def test_current_gate_stale_tag_and_obsolete_version(candidate, field, value):
    fake = Fake(candidate)
    fake.state[field] = value
    with pytest.raises(ValueError):
        p.activate(envelope(candidate), fake)
    assert not fake.writes


@pytest.mark.parametrize("part", ["bytes", "id", "size", "digest", "state"])
def test_asset_substitution(candidate, part):
    fake = Fake(candidate)
    row, data = candidate.files[1]
    if part == "bytes":
        data += b"substituted"
    else:
        row[part] = {"id": 999, "size": 999, "digest": "sha256:" + "0" * 64, "state": "new"}[part]
    candidate.files[1] = row, data
    with pytest.raises(ValueError, match="substituted"):
        p.activate(envelope(candidate), fake)
    assert not fake.writes


def test_record_substitution_fails_signature(candidate):
    signed = json.loads(envelope(candidate))
    import base64
    forged = copy.deepcopy(candidate.record)
    forged["image_digest"] = "sha256:" + "0" * 64
    signed["payload"] = base64.b64encode(p.canonical(forged)).decode()
    with pytest.raises(ValueError, match="signature"):
        p.activate(p.canonical(signed), Fake(candidate))


@pytest.mark.parametrize("evidence", sorted(p.REQUIRED | {"windows"}))
def test_missing_evidence(candidate, evidence):
    del candidate.record["evidence"][evidence]
    with pytest.raises(ValueError, match="evidence"):
        p.activate(envelope(candidate), Fake(candidate))


def test_missing_durable_bundle(candidate):
    candidate.record["assets"] = [a for a in candidate.record["assets"] if a["name"] != "app-release.aab"]
    with pytest.raises(ValueError, match="durable"):
        p.activate(envelope(candidate), Fake(candidate))


@pytest.mark.parametrize("withdrawn,same,sequence", [(1, True, 500), (1, False, 600), (None, False, 600)])
def test_withdrawn_and_historical_floors(candidate, withdrawn, same, sequence):
    fake = Fake(candidate)
    fake.state["sources"] = [{"revision": candidate.record["revision"] if same else "f" * 40,
        "channel": "stable", "withdrawn_at": withdrawn, "sequence": sequence, "manifest": candidate.record["source"]["manifest"]}]
    with pytest.raises(ValueError, match="withdrawn|obsolete"):
        p.activate(envelope(candidate), fake)
    assert not fake.writes


def test_withdrawal_between_partial_attempts_prevents_retry(candidate):
    fake = Fake(candidate)
    fake.fail_before = "docker"
    with pytest.raises(OSError):
        p.activate(envelope(candidate), fake)
    fake.state["sources"][0]["withdrawn_at"] = 1
    with pytest.raises(ValueError, match="withdrawn"):
        p.activate(envelope(candidate), fake)
    assert fake.writes == ["source", "installer"]


def test_play_only_resumes_without_other_writes(candidate):
    fake = Fake(candidate)
    fake.fail_after = "play"
    with pytest.raises(OSError):
        p.activate(envelope(candidate), fake, play_only=True)
    p.activate(envelope(candidate), fake, play_only=True)
    assert fake.writes == ["play"]


def test_upload_never_clobbers_existing_asset(candidate, tmp_path, monkeypatch):
    row, data = candidate.files[1]
    path = tmp_path / row["name"]
    path.write_bytes(data + b"different")
    monkeypatch.setattr(gh, "inventory", lambda _: [row])
    calls = []
    def command(*args, **kw):
        calls.append(args)
        return data
    monkeypatch.setattr(gh, "command", command)
    with pytest.raises(ValueError, match="substituted"):
        gh.upload("v1.2.3", 9, path)
    assert all("upload" not in call for call in calls)


def test_missing_record_is_not_a_legacy_canary(candidate, tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "api", lambda _: {"id": 9})
    monkeypatch.setattr(gh, "inventory", lambda _: [])
    with pytest.raises(ValueError, match="Missing signed staging record"):
        gh.load(SimpleNamespace(tag="v1.2.3", record=tmp_path / gh.RECORD), Fake(candidate))


class PlayService:
    """Edits are isolated snapshots; commit persists even if its response is lost."""
    def __init__(self, candidate):
        self.c = candidate
        self.bundles = []
        self.tracks = []
        self.edits = {}
        self.calls = []
        self.lose_commit = False
        self.count = 0

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if url.endswith("/edits"):
            self.count += 1
            edit = str(self.count)
            self.edits[edit] = copy.deepcopy([self.bundles, self.tracks])
            data = {"id": edit}
        else:
            rest = url.split("/edits/")[1]
            edit = rest.split("/")[0].split(":")[0]
            bundles, tracks = self.edits[edit]
            if method == "DELETE":
                del self.edits[edit]
                data = {}
            elif rest.endswith(":commit"):
                self.bundles, self.tracks = copy.deepcopy([bundles, tracks])
                if self.lose_commit:
                    self.lose_commit = False
                    raise OSError("lost commit response")
                data = {}
            elif rest.endswith(":validate"):
                data = {}
            elif method == "POST" and "uploadType" in rest:
                data = {"versionCode": self.c.record["play"]["version_code"],
                        "sha256": next(a["digest"][7:] for a in self.c.record["assets"] if a["name"] == "app-release.aab")}
                bundles.append(data)
            elif method == "PUT":
                tracks[:] = [kw["json"]]
                data = kw["json"]
            elif rest.endswith("/bundles"):
                data = {"bundles": bundles}
            elif rest.endswith("/tracks"):
                data = {"tracks": tracks}
            else:
                raise AssertionError((method, rest))
        return SimpleNamespace(status_code=200, content=True, json=lambda: data)


@pytest.fixture
def play(candidate, monkeypatch):
    monkeypatch.setenv("PLAY_SIGNING_READY", "true")
    monkeypatch.setenv("ANDROID_WORKERS_READY", "true")
    service = PlayService(candidate)
    return Play(service), service


def test_play_commit_ack_loss_reads_back_without_reupload(candidate, play, tmp_path):
    adapter, service = play
    path = tmp_path / "bundle.aab"
    path.write_bytes(b"fake-bundle")
    assert not adapter.matches(candidate.record)
    service.lose_commit = True
    with pytest.raises(OSError):
        adapter.write(candidate.record, path)
    assert adapter.matches(candidate.record)
    adapter.write(candidate.record, path)
    assert sum("uploadType" in url for _, url in service.calls) == 1
    assert not any("production" in url for _, url in service.calls)


def test_play_refuses_substitution_and_obsolete_version(candidate, play):
    adapter, service = play
    service.bundles = [{"versionCode": 51, "sha256": "0" * 64}]
    with pytest.raises(ValueError, match="different bytes"):
        adapter.matches(candidate.record)
    service.bundles = []
    service.tracks = [{"track": "production", "releases": [{"versionCodes": ["52"], "status": "completed"}]}]
    with pytest.raises(ValueError, match="Obsolete"):
        adapter.matches(candidate.record)


def test_new_workflow_paths_hold_publication_and_never_rebuild_promotion():
    wf = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = wf["jobs"]
    assert wf["concurrency"] == {"group": "stable-release-publish", "cancel-in-progress": "false"}
    assert wf["on"]["workflow_dispatch"]["inputs"]["stage_only"]["default"] == "false"
    assert "!inputs.stage_only" in jobs["create-release"]["if"]
    assert "!inputs.stage_only" in jobs["linux-build"]["if"]
    assert jobs["stage-docker"]["with"]["promote_latest"] == "false"
    assert jobs["stage-android"]["with"]["publish_to_play"] == "false"
    assert jobs["stage-apk"]["with"]["publish_download"] == "false"
    assert jobs["stage-desktop"]["with"]["stage_only"] == "true"
    assert jobs["promotion"]["concurrency"]["group"] == "desktop-source-publish"
    for name in ("promotion", "promotion-play"):
        job = jobs[name]
        assert "preflight" in job["needs"] if isinstance(job["needs"], list) else job["needs"] == "preflight"
        text = json.dumps(job)
        assert "SIGNING_KEY" not in text and "KEYSTORE" not in text
        assert "gradlew" not in text and "build-push-action" not in text
    assert "promotion" in jobs["promotion-play"]["needs"]
    assert any(s.get("uses", "").startswith("actions/attest-build-provenance@") for s in jobs["stage-record"]["steps"])

def test_default_release_graph_is_byte_semantically_unchanged():
    # Canonical YAML digest of the pre-change release workflow on public main.
    # Remove ONLY new opt-in inputs/jobs and guards that are true at defaults.
    wf = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    del wf["on"]["workflow_dispatch"]["inputs"]["stage_only"]
    del wf["on"]["workflow_dispatch"]["inputs"]["promote"]
    for name in list(wf["jobs"]):
        if name.startswith("stage-") or name in {"promotion", "promotion-play"}:
            del wf["jobs"][name]
    jobs = wf["jobs"]
    condition = jobs["create-release"]["if"]
    suffix = " && !inputs.stage_only && inputs.promote == ''"
    assert condition.endswith(suffix)
    jobs["create-release"]["if"] = condition.removesuffix(suffix)
    assert jobs["linux-build"].pop("if") == "${{ !inputs.stage_only && inputs.promote == '' }}"
    steps = jobs["preflight"]["steps"]
    opt_in = next(s for s in steps if s.get("name") == "Validate opt-in two-phase request")
    assert opt_in["if"] == "inputs.stage_only || inputs.promote != ''"
    steps.remove(opt_in)
    assert p.sha256(p.canonical(wf)) == "4734f1ce84ba9444098852f8935b25e1b915c78319819b9f706949ebfec080d7"

