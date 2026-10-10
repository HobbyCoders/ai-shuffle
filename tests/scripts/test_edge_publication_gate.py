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
