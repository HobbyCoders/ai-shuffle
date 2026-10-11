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
    values = {"GITHUB_REPOSITORY": gate.REPOSITORY, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "status",
              "GITHUB_SHA": REVISION, "GITHUB_ACTOR_ID": gate.OWNER_ID, "GITHUB_ACTOR": "Quickkill0",
              "GITHUB_TRIGGERING_ACTOR": "Quickkill0"}
    values.update(changes)
    return values


def status_event(**changes):
    event = {"context": gate.CONTEXT, "state": "success", "sha": REVISION, "sender": {"id": int(gate.OWNER_ID)}}
    event.update(changes)
    return event


@pytest.mark.parametrize("kind", ["push", "workflow_dispatch", "status"])
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
    assert gate.event_allowed(env(GITHUB_EVENT_NAME="push"), {})
    assert not gate.event_allowed(env(GITHUB_EVENT_NAME="push"), {"deleted": True})
    assert gate.event_allowed(env(GITHUB_EVENT_NAME="workflow_dispatch"), {})
    assert not gate.event_allowed(env(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_ACTOR_ID="7"), {})
    assert not gate.event_allowed(env(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_TRIGGERING_ACTOR="x"), {})
    for change in [{"context": "ci/other"}, {"state": "pending"}, {"sha": OTHER}, {"sender": {"id": 7}}, {"sender": None}]:
        assert not gate.event_allowed(env(), status_event(**change)), change
    for name in ["pull_request", "workflow_run", "schedule"]:
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
    event_path.write_text(json.dumps(status_event(state="failure")), encoding="utf-8")
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
    assert f"github.event.context == '{gate.CONTEXT}'" in condition
    assert f"github.event.sender.id == {gate.OWNER_ID}" in condition
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


def test_unverifiable_edge_pointer_refuses(reach_history, monkeypatch):
    base, commit = reach_history
    head = commit("app/example.py", "version = 2")
    def unavailable():
        raise OSError("offline")
    monkeypatch.setattr(gate, "published_edge_revision", unavailable)
    assert not gate.update_reach_readiness(head)[0]
    assert not gate.update_reach_readiness(head, base="f" * 40)[0]


@pytest.mark.parametrize("out_of_reach,accepted", [(False, True), (True, False), (True, True)])
def test_edge_outage_uses_release_baseline_and_still_requires_labels(reach_history, monkeypatch,
                                                                   out_of_reach, accepted):
    base, commit = reach_history
    gate.load_update_reach().git("tag", "v0.0.1", base)
    path = "desktop/src/main/managed-entry.ts" if out_of_reach else "app/example.py"
    head = commit(path, "version = 2")
    def unavailable():
        raise OSError("offline")
    monkeypatch.setattr(gate, "published_edge_revision", unavailable)
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        return [merged_pr(head, ["update-reach:windows-launcher"] if accepted else [])]
    ready, reason = gate.update_reach_readiness(head, api=api)
    assert ready is accepted
    assert "WARNING: published edge pointer unavailable" in reason
    assert "latest final release" in reason
    assert len(calls) == (1 if out_of_reach else 0)
    if not accepted:
        assert "PR #42" in reason and "update-reach:windows-launcher" in reason


@pytest.mark.parametrize("fault", [None, "signature", "channel", "revision", "oversize"])
def test_published_pointer_is_signature_verified(monkeypatch, fault):
    import base64
    import httpx

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    manifest = {"revision": "invalid" if fault == "revision" else REVISION,
                "channel": "stable" if fault == "channel" else "edge"}
    payload = json.dumps(manifest).encode()
    signature = key.sign(payload)
    if fault == "signature":
        payload += b" "
    raw = json.dumps({"payload": base64.b64encode(payload).decode(),
                      "signature": base64.b64encode(signature).decode()}).encode()
    if fault == "oversize":
        raw = b"x" * 32769
    calls = []
    def fetch(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, content=raw)
    client = httpx.Client
    def make_client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 20}
        return client(transport=httpx.MockTransport(fetch), **kwargs)
    monkeypatch.setattr(httpx, "Client", make_client)
    monkeypatch.setattr(serialization, "load_pem_public_key", lambda raw: key.public_key())
    if fault:
        from cryptography.exceptions import InvalidSignature
        with pytest.raises((ValueError, InvalidSignature)):
            gate.published_edge_revision()
    else:
        assert gate.published_edge_revision() == REVISION
    assert calls == [("GET", "https://hobbycoders.com/v1/desktop/manifest/edge")]
