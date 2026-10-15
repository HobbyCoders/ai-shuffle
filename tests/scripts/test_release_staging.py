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
                "release_repository": record["repository"]["name"], "withdrawn_at": None,
                "version": self.c.source["version"], "size": record["source"]["asset"]["size"]})
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
        "channel": "stable", "version": "1.2.3", "withdrawn_at": withdrawn, "sequence": sequence, "manifest": candidate.record["source"]["manifest"]}]
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


def test_real_stage_producer_round_trip_uses_immutable_uploads(candidate, tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    for name in ("linux", "apk", "play", "source"):
        (artifacts / name).mkdir(parents=True)
    linux = []
    for name in ("app.AppImage", "app.deb"):
        data = name.encode()
        (artifacts / "linux" / name).write_bytes(data)
        linux.append({"name": name, "bytes": len(data), "sha256": p.sha256(data)})
    (artifacts / "linux" / "linux-build-provenance.json").write_text(json.dumps({
        "sourceCommit": candidate.record["revision"], "version": "1.2.3", "artifacts": linux}))
    (artifacts / "linux" / "SHA256SUMS").write_text("\n".join(f"{a['sha256']}  {a['name']}" for a in linux) + "\n")
    (artifacts / "apk" / "AI-Shuffle-Android.apk").write_bytes(b"apk")
    (artifacts / "apk" / "AI-Shuffle-Android.apk.sha256").write_text(p.sha256(b"apk") + "  AI-Shuffle-Android.apk\n")
    (artifacts / "apk" / "android-build.json").write_text(json.dumps({"commit": candidate.record["revision"], "versionName": "1.2.3"}))
    (artifacts / "play" / "app-release.aab").write_bytes(b"bundle")
    (artifacts / "play" / "signing-certificate.pem").write_bytes(b"public certificate")
    (artifacts / "source" / "source-evidence.json").write_text(json.dumps(candidate.record["source"]))
    remote = {candidate.record["source"]["asset"]["id"]: candidate.files[candidate.record["source"]["asset"]["id"]]}
    release_assets = []
    calls = []
    def api(route):
        if route == "releases/tags/v1.2.3":
            return {"id": 9, "draft": True}
        if route.startswith("releases/assets/"):
            return remote[int(route.split("/")[-1])][0]
        raise AssertionError(route)
    def command(*args, **kw):
        calls.append(args)
        if args[:3] == ("gh", "release", "upload"):
            path = Path(args[4])
            data = path.read_bytes()
            row = {"id": 200 + len(release_assets), "name": path.name, "size": len(data), "digest": "sha256:" + p.sha256(data), "state": "uploaded"}
            release_assets.append(row)
            remote[row["id"]] = row, data
            return b""
        if args[:2] == ("gh", "api"):
            return remote[int(args[2].split("/")[-1])][1]
        raise AssertionError(args)
    monkeypatch.setattr(gh, "gates", lambda _: copy.deepcopy(candidate.state))
    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(gh, "inventory", lambda _: release_assets)
    monkeypatch.setattr(gh, "command", command)
    monkeypatch.setattr(gh, "container_sequence", lambda *args: 100)
    monkeypatch.setenv("DESKTOP_APPLICATION_SIGNING_KEY", candidate.private.decode())
    monkeypatch.setenv("STAGING_JOB_RESULTS", json.dumps({**{k: "success" for k in p.REQUIRED}, "windows": "skipped"}))
    backend = gh.Backend(tmp_path / "work")
    backend.public_key = candidate.public
    monkeypatch.setattr(backend, "current", lambda _: copy.deepcopy(candidate.state))
    monkeypatch.setattr(backend, "verify_image", lambda _: None)
    args = SimpleNamespace(tag="v1.2.3", artifacts=artifacts, windows=False, image_digest=candidate.record["image_digest"],
                           play_code=51, record=tmp_path / gh.RECORD)
    gh.stage(args, backend)
    signed = args.record.read_bytes()
    record = p.verify_signed(signed, candidate.public)
    assert record["source"]["manifest"] == candidate.record["source"]["manifest"]
    assert len(record["assets"]) == 9
    assert all("--clobber" not in call for call in calls)
    assert all(call[:2] in {("gh", "api"), ("gh", "release")} for call in calls)
    candidate.record = record
    candidate.files = remote
    fake = Fake(candidate)
    p.activate(signed, fake)
    p.activate(signed, fake)
    assert fake.writes == ["source", "docker", "release"]


def test_d1_executes_only_trusted_control_helper(candidate, tmp_path, monkeypatch):
    control, untrusted = tmp_path / "control", tmp_path / "candidate"
    relative = "scripts/source-publisher/node_modules/wrangler/bin/wrangler.js"
    for directory in (control, untrusted):
        (directory / relative).parent.mkdir(parents=True)
    (untrusted / relative).write_text("throw Error('CANDIDATE HELPER EXECUTED');")
    (control / relative).write_text("if(process.env.DESKTOP_APPLICATION_SIGNING_KEY || process.env.GH_TOKEN) throw Error('credential leaked'); console.log(JSON.stringify([{results:[{trusted:true}]}]));")
    key = control / "desktop/build/application-update-key.pem"
    key.parent.mkdir(parents=True)
    key.write_bytes(candidate.public)
    monkeypatch.setattr(gh, "CONTROL", control)
    monkeypatch.setattr(gh, "ROOT", untrusted)
    monkeypatch.chdir(untrusted)
    monkeypatch.setenv("ACCOUNT_HUB_DATABASE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("DESKTOP_APPLICATION_SIGNING_KEY", "test-only-do-not-inherit")
    monkeypatch.setenv("GH_TOKEN", "test-only-do-not-inherit")
    assert gh.Backend(tmp_path / "output").sql("SELECT 1;") == [{"trusted": True}]


def test_promoter_passes_verified_baseline_to_shared_cas(candidate, tmp_path, monkeypatch):
    backend = gh.Backend(tmp_path / "work")
    backend.public_key = candidate.public
    fake = Fake(candidate)
    monkeypatch.setattr(backend, "current", fake.current)
    calls = []
    monkeypatch.setattr(gh, "command", lambda *args, **kwargs: calls.append((args, kwargs)))
    backend.write("source", candidate.record)
    args, kwargs = calls[0]
    assert args == ("node", str(gh.CONTROL / "scripts/ci/activate_source.mjs"))
    assert json.loads(kwargs["data"])["expectedRevision"] is None
    assert json.loads(kwargs["data"])["signed"] == candidate.record["source"]["manifest"]
    assert "DESKTOP_APPLICATION_SIGNING_KEY" not in kwargs["env"]


def test_promote_workflow_uses_control_checkout_and_isolated_python():
    wf = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    for name in ("stage-begin", "stage-record", "promotion", "promotion-play"):
        job = wf["jobs"][name]
        checkouts = [s["with"] for s in job["steps"] if s.get("uses") == "actions/checkout@v4"]
        assert checkouts[0]["path"] == "release-control" and checkouts[0]["ref"] == "main"
        assert checkouts[1]["path"] == "release-candidate"
        assert job["defaults"]["run"]["working-directory"] == "release-control"
        assert all("python -I scripts/ci/release_staging_github.py" in s["run"]
                   for s in job["steps"] if "release_staging_github.py" in s.get("run", ""))


def test_candidate_python_helper_cannot_run_or_receive_secrets(tmp_path, monkeypatch):
    control, untrusted = tmp_path / "control", tmp_path / "candidate"
    relative = "scripts/ci/publication_source.py"
    for directory in (control, untrusted):
        (directory / relative).parent.mkdir(parents=True)
    (untrusted / relative).write_text("raise RuntimeError('CANDIDATE HELPER EXECUTED')")
    (control / relative).write_text("import json, os, sys\nassert sys.flags.isolated\nassert not any(k in os.environ for k in ['DESKTOP_APPLICATION_SIGNING_KEY','CLOUDFLARE_API_TOKEN','GH_TOKEN'])\nprint(json.dumps({'trusted': True, 'args': sys.argv[1:]}))\n")
    monkeypatch.setattr(gh, "CONTROL", control)
    monkeypatch.setattr(gh, "ROOT", untrusted)
    monkeypatch.chdir(untrusted)
    for key in ("DESKTOP_APPLICATION_SIGNING_KEY", "CLOUDFLARE_API_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(key, "test-only-do-not-inherit")
    result = gh.source_data("image-record", "a" * 40)
    assert result == {"trusted": True, "args": ["image-record", "--source-root", str(untrusted), "--revision", "a" * 40]}


@pytest.mark.parametrize("substituted", [False, True])
def test_stopped_image_matches_trusted_candidate_descriptor(candidate, tmp_path, monkeypatch, substituted):
    backend = gh.Backend(tmp_path / "work")
    expected = {"revision": candidate.record["revision"], "source_tree": candidate.record["tree"], "sequence": 100}
    monkeypatch.setattr(gh, "source_data", lambda action, revision: expected)
    monkeypatch.setattr(gh, "git", lambda *args: candidate.record["revision"])
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if args[:4] == ("docker", "buildx", "imagetools", "inspect"):
            return candidate.record["image_digest"].encode()
        if args[:2] == ("docker", "create"):
            return b"stopped-container"
        if args[:2] == ("docker", "cp"):
            observed = {**expected, "source_tree": "d" * 40} if substituted else expected
            Path(args[3]).write_text(json.dumps(observed))
        return b""
    monkeypatch.setattr(gh, "command", command)
    if substituted:
        with pytest.raises(ValueError, match="exact candidate"):
            backend.verify_image(candidate.record["image_digest"])
    else:
        backend.verify_image(candidate.record["image_digest"])
    assert calls[-1] == ("docker", "rm", "stopped-container")
    assert not any("run" in call or "start" in call or "build" in call for call in calls)


def test_obsolete_source_version_cannot_be_resigned_forward(candidate):
    fake = Fake(candidate)
    fake.state["sources"] = [{"channel": "stable", "version": "2.0.0"}]
    with pytest.raises(ValueError, match="Obsolete version"):
        p.activate(envelope(candidate), fake)
    assert not fake.writes


def test_release_inline_shell_is_valid():
    import subprocess
    wf = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    for name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step:
                result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
                assert result.returncode == 0, (name, step.get("name"), result.stderr)


@pytest.fixture
def owner_gate_evidence(candidate, monkeypatch):
    """Keep real gates()/owner_status(); fake only GitHub and unrelated gates."""
    from scripts.ci import edge_publication_gate as edge
    revision = candidate.record["revision"]
    success = {"context": edge.CONTEXT, "creator": {"id": edge.OWNER_ID}, "state": "success"}
    evidence = SimpleNamespace(rows=[success], calls=[], error=None)
    def api(route):
        evidence.calls.append(route)
        assert route == f"commits/{revision}/statuses?per_page=100&page=1"
        if evidence.error:
            raise evidence.error
        return evidence.rows
    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(gh, "owner_gate", lambda _: candidate.record["repository"])
    monkeypatch.setattr(gh, "command", lambda *args, **kwargs: b"")
    monkeypatch.setattr(gh, "git", lambda *args: f"{revision}\trefs/tags/v1.2.3" if args[0] == "ls-remote" else candidate.record["tree"])
    monkeypatch.setattr(gh.preflight, "verify_tag", lambda *args: revision)
    for name in ("verify_versions", "verify_catalog", "verify_merged_pr"):
        monkeypatch.setattr(gh.preflight, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(gh.preflight, "newest_final", lambda *args: True)
    monkeypatch.setattr(edge, "update_reach_readiness", lambda *args, **kwargs: (True, "fixture"))
    return evidence


@pytest.mark.parametrize("case", ["missing", "failure", "foreign", "pending", "api-error", "superseded"])
@pytest.mark.parametrize("phase", ["stage", "promote"])
def test_latest_owner_full_suite_is_required(candidate, owner_gate_evidence, case, phase):
    evidence = owner_gate_evidence
    success = copy.deepcopy(evidence.rows[0])
    if case == "missing":
        evidence.rows = []
    elif case == "api-error":
        evidence.error = ValueError("GitHub status API unavailable")
    elif case == "foreign":
        evidence.rows = [{**success, "creator": {"id": "not-the-owner"}}]
    elif case == "superseded":
        evidence.rows = [{**success, "state": "failure"}, success]
    else:
        evidence.rows = [{**success, "state": case}]
    fake = Fake(candidate)
    original_current = fake.current
    def current(record):
        return {**original_current(record), **gh.gates(record["tag"])}
    fake.current = current
    with pytest.raises(ValueError, match="attestation|API unavailable"):
        if phase == "stage":
            # stage() must refuse before even looking for candidate artifacts.
            gh.stage(SimpleNamespace(tag="v1.2.3"), fake)
        else:
            p.activate(envelope(candidate), fake)
    assert len(evidence.calls) == 1
    assert not fake.writes


def test_owner_gate_ignores_foreign_status_and_checks_exact_revision(owner_gate_evidence):
    evidence = owner_gate_evidence
    success = evidence.rows[0]
    evidence.rows = [{**success, "state": "failure", "creator": {"id": "not-the-owner"}}, success]
    assert gh.gates("v1.2.3")["owner"] is True
    assert evidence.calls == [f"commits/{'a' * 40}/statuses?per_page=100&page=1"]


@pytest.mark.parametrize("revoke_after", ["verification", "source"])
def test_owner_attestation_is_rechecked_immediately_before_activation(candidate, owner_gate_evidence, revoke_after):
    evidence = owner_gate_evidence
    success = copy.deepcopy(evidence.rows[0])
    fake = Fake(candidate)
    original_current, original_provenance, original_write = fake.current, fake.verify_provenance, fake.write
    def revoke():
        evidence.rows = [{**success, "state": "failure"}, success]
    def current(record):
        return {**original_current(record), **gh.gates(record["tag"])}
    def provenance(record):
        original_provenance(record)
        if revoke_after == "verification":
            revoke()
    def write(sink, record):
        original_write(sink, record)
        if sink == revoke_after:
            revoke()
    fake.current, fake.verify_provenance, fake.write = current, provenance, write
    with pytest.raises(ValueError, match="attestation"):
        p.activate(envelope(candidate), fake)
    assert fake.writes == ([] if revoke_after == "verification" else ["source"])
    # Retrying the signed candidate cannot reuse its formerly successful status.
    with pytest.raises(ValueError, match="attestation"):
        p.activate(envelope(candidate), fake)
    assert fake.writes == ([] if revoke_after == "verification" else ["source"])
    assert len(evidence.calls) >= 3


def test_release_publication_credentials_are_step_scoped():
    wf = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    forbidden = {"GH_TOKEN", "GITHUB_TOKEN", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                 "DESKTOP_APPLICATION_SIGNING_KEY", "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"}
    def credential_free(env):
        assert not forbidden.intersection(env)
        assert "secrets." not in json.dumps(env)
        assert "github.token" not in json.dumps(env)
    credential_free(wf.get("env", {}))
    for job in wf["jobs"].values():
        credential_free(job.get("env", {}))
        for step in job.get("steps", []):
            run = step.get("run", "")
            setup = step.get("uses", "").startswith(("actions/setup-", "docker/setup-"))
            if setup or "pip install" in run or "npm ci" in run:
                credential_free({**wf.get("env", {}), **job.get("env", {}), **step.get("env", {})})
                assert "secrets." not in json.dumps(step)
                assert "github.token" not in json.dumps(step)
    for name in ("stage-begin", "stage-record", "promotion", "promotion-play"):
        job = wf["jobs"][name]
        assert job["permissions"]["statuses"] == "read"
        for step in job["steps"]:
            if "release_staging_github.py" in step.get("run", ""):
                assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
                if name != "stage-begin":
                    assert step["env"]["CLOUDFLARE_API_TOKEN"] == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
            if step.get("uses") == "actions/checkout@v4":
                assert step["with"]["persist-credentials"] == "false"
