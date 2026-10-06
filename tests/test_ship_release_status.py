"""Pure-function checks for scripts/ci/ship_release_status.py (no git/gh calls)."""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "ship_release_status.py"
_spec = importlib.util.spec_from_file_location("ship_release_status", _PATH)
srs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srs)


def test_latest_final_tag_ignores_prereleases_and_sorts_numerically():
    tags = ["v0.8.9", "v0.8.22", "v0.8.23-rc.1", "v6-phase2", "v0.8.3"]
    assert srs.latest_final_tag(tags) == "v0.8.22"
    assert srs.latest_final_tag([]) is None


def test_next_patch():
    assert srs.next_patch("v0.8.22") == "v0.8.23"
    assert srs.next_patch(None) == "v0.1.0"


def test_frontend_only_changes_need_no_play_build():
    builds = srs.platforms_needing_builds(["frontend/src/a.svelte", "app/core/x.py"])
    assert builds["android"]["needed"] is False
    assert builds["desktop_installer"]["needed"] is False


def test_android_changes_need_play_build():
    builds = srs.platforms_needing_builds(["android/app/build.gradle.kts"])
    assert builds["android"] == {"needed": True, "example_paths": ["android/app/build.gradle.kts"]}


def test_ci_state_uses_newest_run_per_workflow_on_sha():
    runs = [
        {"workflowName": "CI Backend", "status": "completed", "conclusion": "success", "headSha": "abc"},
        {"workflowName": "CI Backend", "status": "completed", "conclusion": "failure", "headSha": "abc"},
        {"workflowName": "CI fluid", "status": "in_progress", "conclusion": None, "headSha": "abc"},
        {"workflowName": "CI JavaScript", "status": "completed", "conclusion": "success", "headSha": "old"},
    ]
    state = srs.ci_state(runs, "abc")
    assert state["CI Backend"] == "success"
    assert state["CI fluid"] == "pending"
    assert state["CI JavaScript"] == "missing"


# -- live smoke gate ------------------------------------------------------------

CANDIDATE = "1234567890abcdef1234567890abcdef12345678"
SMOKE_ROWS = [
    {"id": "x.1", "check": "start", "kind": "script"},
    {"id": "x.2", "check": "stop button", "kind": "browser"},
    {"id": "x.3", "check": "quit", "kind": "owner"},
]


def _smoke_file(base, worker, revision=CANDIDATE, **status):
    statuses = {"x.1": "pass", "x.2": "pass", "x.3": "not_run", **status}
    kinds = {r["id"]: r["kind"] for r in SMOKE_ROWS}
    path = base / revision / f"{worker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"revision": revision, "worker": worker, "outcome": "pass", "rows": [
        {"id": k, "kind": kinds[k], "status": v, "reason": "x" if v != "pass" else None}
        for k, v in statuses.items()]}))


def _gate(tmp_path, workers=("Unraid", "DesktopWorker"), skip=None, revision=CANDIDATE):
    return srs.smoke_gate(revision, list(workers), skip, str(tmp_path), scenario_rows=SMOKE_ROWS)


def test_smoke_gate_pass(tmp_path):
    _smoke_file(tmp_path, "Unraid")
    _smoke_file(tmp_path, "DesktopWorker")
    report = _gate(tmp_path)
    assert report["verdict"] == "pass" and report["blockers"] == []
    assert report["release_notes_lines"] == ["Live smoke passed on Unraid, DesktopWorker at 123456789."]
    assert len(report["owner_todo"]) == 2  # owner rows never block


def test_smoke_gate_failing_row_blocks(tmp_path):
    _smoke_file(tmp_path, "Unraid", **{"x.1": "fail"})
    _smoke_file(tmp_path, "DesktopWorker")
    report = _gate(tmp_path)
    assert report["verdict"] == "block"
    assert report["blockers"] == ["Unraid: x.1 failed: x"]


def test_smoke_gate_unrecorded_browser_row_blocks(tmp_path):
    _smoke_file(tmp_path, "Unraid", **{"x.2": "not_run"})
    report = _gate(tmp_path, workers=["Unraid"])
    assert report["verdict"] == "block" and "x.2 not recorded" in report["blockers"][0]


def test_smoke_gate_missing_file_blocks(tmp_path):
    _smoke_file(tmp_path, "Unraid")
    report = _gate(tmp_path)
    assert report["verdict"] == "block"
    assert report["blockers"][0].startswith("no smoke result for DesktopWorker at 123456789")
    assert _gate(tmp_path / "empty", workers=())["verdict"] == "block"


def test_smoke_gate_wrong_revision_blocks(tmp_path):
    old = "f" * 40
    _smoke_file(tmp_path, "Unraid", revision=old)
    report = _gate(tmp_path, workers=["Unraid"])
    assert report["verdict"] == "block"
    # a pass for an older revision is not a pass for the candidate
    assert _gate(tmp_path, workers=["Unraid"], revision=old)["verdict"] == "pass"
    stale = tmp_path / CANDIDATE / "Unraid.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text((tmp_path / old / "Unraid.json").read_text())
    assert _gate(tmp_path, workers=["Unraid"])["blockers"] == [
        "Unraid's result is for revision fffffffff, not 123456789"]


def test_smoke_gate_skip_is_recorded_for_the_release_notes(tmp_path):
    report = _gate(tmp_path, skip="DesktopWorker is away")
    assert report["verdict"] == "skipped" and report["blockers"] == []
    assert report["would_block"]
    assert report["release_notes_lines"] == ["Live smoke skipped (--skip-smoke): DesktopWorker is away"]
    assert _gate(tmp_path, skip=" ")["release_notes_lines"] == [
        "Live smoke skipped (--skip-smoke): no reason given"]


def test_smoke_gate_without_a_candidate_blocks(tmp_path):
    assert _gate(tmp_path, revision="")["blockers"] == ["no candidate revision to check (got '')"]
    assert _gate(tmp_path, revision="", skip="offline")["verdict"] == "skipped"


@pytest.mark.parametrize("skip, ready, notes", [
    (None, False, []),
    ("away", True, ["Live smoke skipped (--skip-smoke): away"]),
])
def test_collect_folds_the_smoke_gate_into_blockers(tmp_path, monkeypatch, skip, ready, notes):
    def fake_try(*args):
        if args[:2] == ("git", "rev-parse"):
            return CANDIDATE
        if args[:2] == ("git", "tag"):
            return "v0.1.0"
        if args[:2] == ("git", "log"):
            return "abc change"
        return ""
    success = [{"workflowName": n, "status": "completed", "conclusion": "success", "headSha": CANDIDATE}
               for n in srs.REQUIRED_CI]
    monkeypatch.setattr(srs, "_try", fake_try)
    monkeypatch.setattr(srs, "_gh_json", lambda *a: success)
    monkeypatch.setattr(srs, "_variable", lambda *a, **k: None)
    report = srs.collect(["Unraid"], skip, str(tmp_path))
    assert report["ready"] is ready
    assert report["release_notes_lines"] == notes
    if skip is None:
        assert report["blockers"] == [
            f"Live smoke: no smoke result for Unraid at 123456789 ({tmp_path / CANDIDATE / 'Unraid.json'})"]
    text = srs.render(report)
    assert "Live smoke: " + ("skipped (away)" if skip else "block") in text
    if skip:
        assert "release notes must say: Live smoke skipped (--skip-smoke): away" in text
