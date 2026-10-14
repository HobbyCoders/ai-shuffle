"""Edge publication waits for the owner's local full-suite pass on main."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("edge_gate", ROOT / "scripts/ci/edge_publication_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
REAL_PUBLISHED_EDGE_REVISION = gate.published_edge_revision

REVISION = "a" * 40
OTHER = "b" * 40


def status(state="success", context=gate.CONTEXT, creator=gate.OWNER_ID, description="backend, fluid passed"):
    return {"state": state, "context": context, "creator": {"id": int(creator)}, "description": description}


class FakeGitHub:
    def __init__(self, statuses=(), protected=True, heads=(REVISION, REVISION)):
        self.statuses = list(statuses)
        self.protected = protected
        self.heads = list(heads)
        self.calls = []

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if endpoint == "branches/main":
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            return {"protected": self.protected, "commit": {"sha": head}}
        if endpoint.startswith(f"commits/{REVISION}/statuses"):
            page = int(endpoint.rsplit("page=", 1)[1])
            return self.statuses[(page - 1) * 100:page * 100]
        raise AssertionError(endpoint)


def test_owner_success_on_current_main_publishes():
    ready, reason = gate.publication_readiness(FakeGitHub([status()]), REVISION)
    assert ready, reason
    assert "backend, fluid passed" in reason


@pytest.mark.parametrize("statuses", [
    [],
    [status(creator="7")],
    [status(context="ci/other")],
    [status(state="pending")],
    [status(state="failure")],
    [status(state="error")],
])
def test_missing_foreign_or_unfinished_pass_refuses(statuses):
    ready, reason = gate.publication_readiness(FakeGitHub(statuses), REVISION)
    assert not ready
    assert reason.startswith(gate.MAIN_SUITE_REFUSAL)
    assert gate.COMMAND in reason


def test_newest_owner_status_wins():
    # GitHub lists newest first: a later failure beats an earlier success.
    ready, _ = gate.publication_readiness(FakeGitHub([status(state="failure"), status()]), REVISION)
    assert not ready
    # A later success recovers from an earlier failure.
    ready, _ = gate.publication_readiness(FakeGitHub([status(), status(state="failure")]), REVISION)
    assert ready
    # A stranger's newer success never hides the owner's failure.
    ready, _ = gate.publication_readiness(FakeGitHub([status(creator="7"), status(state="failure")]), REVISION)
    assert not ready


def test_owner_status_on_a_later_page_is_found():
    statuses = [status(creator="7")] * 100 + [status()]
    assert gate.publication_readiness(FakeGitHub(statuses), REVISION)[0]


def test_unbounded_status_history_fails_closed():
    with pytest.raises(ValueError):
        gate.publication_readiness(FakeGitHub([status(creator="7")] * 1000), REVISION)


def test_unprotected_or_moved_main_refuses():
    assert not gate.publication_readiness(FakeGitHub([status()], protected=False), REVISION)[0]
    assert not gate.publication_readiness(FakeGitHub([status()], heads=[OTHER]), REVISION)[0]
    # main moving while the statuses are read is caught by the recheck.
    ready, reason = gate.publication_readiness(FakeGitHub([status()], heads=[REVISION, OTHER]), REVISION)
    assert not ready and "moved during verification" in reason


def env(**changes):
    values = {"GITHUB_REPOSITORY": gate.REPOSITORY, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule",
              "GITHUB_SHA": REVISION, "GITHUB_ACTOR_ID": gate.OWNER_ID, "GITHUB_ACTOR": "example-maintainer",
              "GITHUB_TRIGGERING_ACTOR": "example-maintainer"}
    values.update(changes)
    return values


def status_event(**changes):
    event = {"schedule": "0 6 * * *", "context": gate.CONTEXT, "state": "success", "sha": REVISION, "sender": {"id": int(gate.OWNER_ID)}}
    event.update(changes)
    return event


@pytest.mark.parametrize("kind", ["schedule", "workflow_dispatch"])
@pytest.mark.parametrize("passed", [False, True])
def test_trigger_cannot_add_workflow_requirements(kind, passed):
    # #2202: dispatching suites used to turn a same-tree PR into a combined
    # tree and demand more Actions runs. Only the local attestation counts now,
    # regardless of what triggered publication; no PR/tree/run reads are needed.
    assert gate.event_allowed(env(GITHUB_EVENT_NAME=kind), status_event())
    api = FakeGitHub([status()] if passed else [])
    ready, reason = gate.publication_readiness(api, REVISION)
    assert ready is passed
    if not passed:
        assert gate.COMMAND in reason
    assert set(api.calls) == {"branches/main", f"commits/{REVISION}/statuses?per_page=100&page=1"}


def test_event_predicate():
    assert gate.event_allowed(env(), status_event())
    assert not gate.event_allowed(env(GITHUB_EVENT_NAME="push"), {})
    assert gate.event_allowed(env(GITHUB_EVENT_NAME="workflow_dispatch"), {})
    assert not gate.event_allowed(env(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_ACTOR_ID="7"), {})
    assert not gate.event_allowed(env(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_TRIGGERING_ACTOR="x"), {})
    assert not gate.event_allowed(env(), {"schedule": "0 7 * * *"})
    for name in ["pull_request", "workflow_run", "status", "push"]:
        assert not gate.event_allowed(env(GITHUB_EVENT_NAME=name), status_event())
    assert not gate.event_allowed(env(GITHUB_REF="refs/heads/feature"), status_event())
    assert not gate.event_allowed(env(GITHUB_REPOSITORY="someone/ai-shuffle"), status_event())


@pytest.mark.parametrize("statuses,ready", [([status()], True), ([], False)])
def test_entrypoint_writes_output_and_annotates_refusal(statuses, ready, monkeypatch, tmp_path, capsys):
    event_path, output, summary = tmp_path / "event.json", tmp_path / "out", tmp_path / "summary"
    event_path.write_text(json.dumps(status_event()), encoding="utf-8")
    for key, value in env(GITHUB_EVENT_PATH=str(event_path), GITHUB_OUTPUT=str(output),
                          GITHUB_STEP_SUMMARY=str(summary)).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(gate, "GitHub", lambda: FakeGitHub(statuses))
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: REVISION + "\n")
    gate.main()
    assert output.read_text() == f"ready={str(ready).lower()}\n"
    printed = capsys.readouterr().out
    assert ("::warning title=Edge publication refused::" in printed) is (not ready)
    assert summary.read_text(encoding="utf-8")


def test_entrypoint_refuses_unauthorized_events(monkeypatch, tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"schedule": "0 7 * * *"}), encoding="utf-8")
    for key, value in env(GITHUB_EVENT_PATH=str(event_path)).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        gate.main()


def test_annotation_escapes_workflow_command_data():
    assert gate.warning_annotation("50%\nnext\r") == "::warning title=Edge publication refused::50%25%0Anext%0D"


def test_workflow_wiring_matches_the_gate():
    config = yaml.load((ROOT / ".github/workflows/desktop-source.yml").read_text(encoding="utf-8"),
                       Loader=yaml.BaseLoader)
    condition = config["jobs"]["gate"]["if"]
    assert "github.event_name == 'schedule'" in condition
    assert f"github.actor_id == '{gate.OWNER_ID}'" in condition
    assert set(config["on"]) == {"schedule", "workflow_dispatch"}
    assert config["on"]["schedule"] == [{"cron": "0 6 * * *"}]
    assert config["env"]["SOURCE_CHANNEL"] == "nightly"
    steps = config["jobs"]["gate"]["steps"]
    assert steps[1]["run"] == "python3 scripts/ci/nightly_changed.py"
    assert steps[-1]["if"] == "steps.changed.outputs.changed == 'true'"
    text = (ROOT / "scripts/local_ci.py").read_text(encoding="utf-8")
    assert f'ATTEST_CONTEXT = "{gate.CONTEXT}"' in text
    assert "local_ci.py full --attest" in gate.COMMAND


@pytest.fixture
def reach_history(tmp_path, monkeypatch):
    """Real commits/trees: exercise classification, not a pre-seeded failing group."""
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Reach test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / "disabled-hooks"))
    def commit(path, text):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git("add", ".")
        git("commit", "-qm", "Reach fixture\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>")
        return git("rev-parse", "HEAD")
    base = commit("desktop/src/main/managed-entry.ts", "export const version = 1;")
    reach = gate.load_update_reach()
    original_git, original_reader = reach.git, reach.reader
    monkeypatch.setattr(reach, "git", lambda *a, **kw: original_git(*a, root=tmp_path))
    monkeypatch.setattr(reach, "reader", lambda rev: original_reader(rev, root=tmp_path))
    monkeypatch.setattr(gate, "load_update_reach", lambda: reach)
    monkeypatch.setattr(gate, "published_edge_revision", lambda: base)
    return base, commit


def merged_pr(sha, labels=(), number=42):
    return {"number": number, "merged_at": "2026-10-11T00:00:00Z",
            "base": {"ref": "main", "repo": {"full_name": gate.REPOSITORY}},
            "merge_commit_sha": sha, "labels": [{"name": name} for name in labels]}


@pytest.mark.parametrize("labels,accepted", [([], False), (["update-reach:windows-launcher"], True),
                                            (["update-reach:docker-image"], False)])
def test_main_reach_requires_label_on_merged_pr(reach_history, labels, accepted):
    base, commit = reach_history
    head = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        return [merged_pr(head, labels)]
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert ready is accepted
    assert calls == [f"commits/{head}/pulls?per_page=100"]
    if not accepted:
        assert "PR #42" in reason and "update-reach:windows-launcher" in reason
        assert "OUT OF REACH" in reason


def test_in_reach_main_needs_no_pr_api(reach_history):
    base, commit = reach_history
    head = commit("app/example.py", "version = 2")
    def unavailable(endpoint):
        raise AssertionError("In-reach changes must not query gh")
    ready, reason = gate.update_reach_readiness(head, api=unavailable)
    assert ready, reason


@pytest.mark.parametrize("evidence", ["unavailable", "missing", "unrelated", "unmerged", "wrong-base", "foreign-repo", "incomplete"])
def test_out_of_reach_missing_or_unrelated_evidence_refuses(reach_history, evidence):
    base, commit = reach_history
    head = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
    def api(endpoint):
        pr = merged_pr(head, ["update-reach:windows-launcher"])
        if evidence == "unavailable":
            raise subprocess.CalledProcessError(1, "gh")
        if evidence == "missing":
            return []
        if evidence == "unrelated":
            pr["merge_commit_sha"] = base
        if evidence == "unmerged":
            pr["merged_at"] = None
        if evidence == "wrong-base":
            pr["base"]["ref"] = "other"
        if evidence == "foreign-repo":
            pr["base"]["repo"]["full_name"] = "someone/ai-shuffle"
        return [pr] * (100 if evidence == "incomplete" else 1)
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert not ready
    assert "refused" in reason


def test_each_unpublished_main_change_needs_its_own_acceptance(reach_history):
    base, commit = reach_history
    first = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
    second = commit("desktop/src/main/managed-entry.ts", "export const version = 3;")
    def api(endpoint):
        sha = endpoint.split("/")[1]
        return [merged_pr(sha, ["update-reach:windows-launcher"] if sha == second else [],
                          number=42 if sha == first else 43)]
    ready, reason = gate.update_reach_readiness(second, api=api)
    assert not ready and "PR #42" in reason
    assert "PR #43" not in reason


def test_nonancestor_edge_pointer_refuses(reach_history):
    base, commit = reach_history
    head = commit("app/example.py", "version = 2")
    assert not gate.update_reach_readiness(head, base="f" * 40)[0]


@pytest.fixture(autouse=True)
def isolated_edge_cache(tmp_path, monkeypatch):
    path = tmp_path / "cache" / "edge.json"
    monkeypatch.setattr(gate, "edge_cache_path", lambda: path)
    return path


@pytest.fixture
def signed_feed(monkeypatch):
    import base64
    import httpx

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(serialization, "load_pem_public_key", lambda raw: key.public_key())
    response = {"raw": b"", "offline": False}
    def fetch(request):
        assert (request.method, str(request.url)) == ("GET", "https://hobbycoders.com/v1/desktop/manifest/edge")
        if response["offline"]:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, content=response["raw"])
    client = httpx.Client
    def make_client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 20}
        return client(transport=httpx.MockTransport(fetch), **kwargs)
    monkeypatch.setattr(httpx, "Client", make_client)
    def publish(revision=REVISION, *, changes=None, omit=()):
        manifest = {"format": 2, "revision": revision, "channel": "edge",
                    "sha256": "c" * 64, "size": 1, "sequence": 1,
                    "container": {"sequence": 1, "runtime": "d" * 64},
                    "tree": "e" * 64, "version": "0.9.0"}
        manifest.update(changes or {})
        for field in omit:
            manifest.pop(field)
        payload = json.dumps(manifest).encode()
        response["raw"] = json.dumps({"payload": base64.b64encode(payload).decode(),
                                      "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
        return response["raw"]
    return publish, response


@pytest.mark.parametrize("signed_channel", ["edge", "nightly"])
@pytest.mark.parametrize("fault", [None, "signature", "channel", "revision", "oversize",
                                   "missing-fields", "size", "container", "tree"])
def test_published_pointer_uses_complete_updater_verification(signed_feed, isolated_edge_cache, fault, signed_channel):
    publish, response = signed_feed
    changes = {"channel": "stable" if fault == "channel" else signed_channel}
    if fault in ("size", "container", "tree"):
        changes[fault] = {"size": 0, "container": {}, "tree": "invalid"}[fault]
    publish("invalid" if fault == "revision" else REVISION, changes=changes,
            omit=("format", "size", "sha256", "sequence", "container") if fault == "missing-fields" else ())
    if fault == "signature":
        import base64
        envelope = json.loads(response["raw"])
        envelope["payload"] = base64.b64encode(base64.b64decode(envelope["payload"]) + b" ").decode()
        response["raw"] = json.dumps(envelope).encode()
    if fault == "oversize":
        response["raw"] = b"x" * 32769
    if fault:
        with pytest.raises(Exception) as caught:
            gate.published_edge_revision()
        assert type(caught.value).__name__ == ("ValueError" if fault in ("channel", "oversize") else "UpdateError")
        assert not isolated_edge_cache.exists()
    else:
        assert gate.published_edge_revision() == REVISION
        assert isolated_edge_cache.read_bytes() == response["raw"]
        assert gate.cached_edge_revision() == REVISION


def test_cached_manifest_is_reverified_not_a_bare_sha(signed_feed, isolated_edge_cache):
    publish, response = signed_feed
    publish()
    gate.published_edge_revision()
    isolated_edge_cache.write_text(json.dumps({"revision": OTHER}))
    with pytest.raises(Exception) as caught:
        gate.cached_edge_revision()
    assert type(caught.value).__name__ == "UpdateError"


@pytest.mark.parametrize("cached", [False, True])
def test_local_tag_cannot_skip_unpublished_launcher_change(reach_history, signed_feed, monkeypatch, cached):
    base, commit = reach_history
    publish, response = signed_feed
    monkeypatch.setattr(gate, "published_edge_revision", REAL_PUBLISHED_EDGE_REVISION)
    if cached:
        publish(base)
        assert gate.published_edge_revision() == base
    launcher = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
    gate.load_update_reach().git("tag", "v999.0.0", launcher)
    head = commit("app/example.py", "version = 2")
    response["offline"] = True
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        return [merged_pr(launcher)]
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert not ready and "PR #42" in reason and "update-reach:windows-launcher" in reason
    assert calls == [f"commits/{launcher}/pulls?per_page=100"]


@pytest.mark.parametrize("cached", [False, True])
def test_network_outage_does_not_block_in_reach_history(reach_history, signed_feed, monkeypatch, cached):
    base, commit = reach_history
    publish, response = signed_feed
    monkeypatch.setattr(gate, "published_edge_revision", REAL_PUBLISHED_EDGE_REVISION)
    if cached:
        # This unreachable change is already published: its labels are irrelevant
        # during a later feed + GitHub outage, even if a local release tag predates it.
        gate.load_update_reach().git("tag", "v0.0.1", base)
        published = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
        publish(published)
        assert gate.published_edge_revision() == published
    head = commit("app/example.py", "version = 2")
    response["offline"] = True
    def unavailable(endpoint):
        raise AssertionError("in-reach changes must not query GitHub")
    ready, reason = gate.update_reach_readiness(head, api=unavailable)
    assert ready, reason
    assert "WARNING: published edge pointer unavailable" in reason
    assert ("reverified cached signed publication" if cached else "no verified cached publication") in reason


def test_cold_cache_depth_one_clone_cannot_skip_launcher_change(reach_history, signed_feed,
                                                               monkeypatch, tmp_path):
    base, commit = reach_history
    head = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
    source = Path(gate.load_update_reach().git("rev-parse", "--show-toplevel"))
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--quiet", "--depth=1", source.as_uri(), str(shallow)], check=True)
    reach = gate.load_update_reach()
    def git(*args, **kwargs):
        return subprocess.check_output(["git", *args], cwd=shallow, text=True).strip()
    monkeypatch.setattr(reach, "git", git)
    monkeypatch.setattr(gate, "published_edge_revision", REAL_PUBLISHED_EDGE_REVISION)
    signed_feed[1]["offline"] = True
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        return [merged_pr(head)]
    assert git("rev-parse", "--is-shallow-repository") == "true"
    assert git("rev-list", "--first-parent", head) == head  # truncated, not bootstrap
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert not ready
    assert "incomplete Git history" in reason
    assert "git fetch --unshallow origin" in reason
    assert calls == []
    # The prescribed recovery exposes the unpublished launcher change again.
    git("fetch", "--unshallow", "origin")
    assert git("rev-parse", "--is-shallow-repository") == "false"
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert not ready and "PR #42" in reason and "update-reach:windows-launcher" in reason
    assert calls == [f"commits/{head}/pulls?per_page=100"]


def test_filtered_clone_cannot_treat_unavailable_blobs_as_absent(reach_history, monkeypatch, tmp_path):
    base, commit = reach_history
    path = "desktop/src/main/managed-entry.ts"
    unlabeled = commit(path, "export const version = 2;")
    head = commit(path, "export const version = 3;")
    source_reach = gate.load_update_reach()
    source = Path(source_reach.git("rev-parse", "--show-toplevel"))
    source_reach.git("config", "uploadpack.allowFilter", "true")
    filtered = tmp_path / "filtered"
    subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
                    source.as_uri(), str(filtered)], check=True)
    def git(*args, **kwargs):
        return subprocess.check_output(["git", *args], cwd=filtered, text=True).strip()
    assert git("rev-parse", "--is-shallow-repository") == "false"
    historical_blob = source_reach.git("rev-parse", f"{unlabeled}:{path}")
    assert "?" + historical_blob in git("rev-list", "--objects", "--all", "--missing=print").splitlines()
    # Only the latest labeled change's blob is materialized before the outage.
    assert "version = 3" in git("show", f"{head}:{path}")
    git("config", "remote.origin.url", str(tmp_path / "unavailable-promisor"))
    failed_fetch = subprocess.run(["git", "show", f"{unlabeled}:{path}"], cwd=filtered, capture_output=True)
    assert failed_fetch.returncode != 0

    spec = importlib.util.spec_from_file_location("filtered_reach", ROOT / "scripts/ci/update_reach.py")
    reach = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reach)
    original_reader = reach.reader
    monkeypatch.setattr(reach, "git", git)
    monkeypatch.setattr(reach, "reader", lambda rev: original_reader(rev, root=filtered))
    monkeypatch.setattr(gate, "load_update_reach", lambda: reach)
    # Tree evidence still distinguishes absent files from unavailable content.
    assert reach.reader(base)("genuinely-absent.py") is None
    assert b"version = 3" in reach.reader(head)(path)
    with pytest.raises(RuntimeError, match="Cannot read historical update-reach content"):
        reach.reader(unlabeled)(path)
    with pytest.raises(RuntimeError, match="Cannot read historical update-reach content"):
        reach.reader("f" * 40)(path)
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        sha = endpoint.split("/")[1]
        return [merged_pr(sha, ["update-reach:windows-launcher"] if sha == head else [],
                          number=43 if sha == head else 42)]
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert not ready and "could not verify" in reason
    assert calls == []  # classification refuses before it can silently skip a PR

    # Restoring the promisor makes lazy reads work, but never accepts the
    # earlier unlabeled change on the strength of the latest PR's label.
    git("config", "remote.origin.url", source.as_uri())
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert not ready and "PR #42" in reason and "update-reach:windows-launcher" in reason
    assert "PR #43" not in reason
    assert calls == [f"commits/{unlabeled}/pulls?per_page=100", f"commits/{head}/pulls?per_page=100"]


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_outage_still_requires_labels(reach_history, signed_feed, monkeypatch, accepted, cached):
    base, commit = reach_history
    publish, response = signed_feed
    monkeypatch.setattr(gate, "published_edge_revision", REAL_PUBLISHED_EDGE_REVISION)
    if cached:
        publish(base)
        gate.published_edge_revision()
    head = commit("desktop/src/main/managed-entry.ts", "export const version = 2;")
    response["offline"] = True
    ready, reason = gate.update_reach_readiness(head, api=lambda endpoint:
        [merged_pr(head, ["update-reach:windows-launcher"] if accepted else [])])
    assert ready is accepted
    if not accepted:
        assert "PR #42" in reason and "update-reach:windows-launcher" in reason
