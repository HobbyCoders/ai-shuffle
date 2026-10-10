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


def test_release_tags_reads_origin_and_ignores_local_only_tags(monkeypatch):
    def fake_try(*args):
        if args[:2] == ("git", "ls-remote"):
            return "a1\trefs/tags/v0.9.1\nb2\trefs/tags/android-latest"
        if args[:2] == ("git", "tag"):
            return "v5.0.0\nv0.9.1"
        return None
    monkeypatch.setattr(srs, "_try", fake_try)
    assert srs.release_tags() == ["v0.9.1", "android-latest"]
    assert srs.latest_final_tag(srs.release_tags()) == "v0.9.1"


def test_release_tags_falls_back_to_local_tags_offline(monkeypatch):
    monkeypatch.setattr(srs, "_try", lambda *a: "v0.9.1" if a[:2] == ("git", "tag") else None)
    assert srs.release_tags() == ["v0.9.1"]


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


def _status(context, state="success", creator=srs.OWNER_ID):
    return {"context": context, "state": state, "creator": {"id": int(creator)}}


def test_local_state_uses_the_newest_owner_status_per_suite():
    statuses = [
        _status("local/full-suite", creator="7"),  # a stranger's newer success never counts
        _status("local/full-suite", "failure"),
        _status("local/full-suite"),
        _status("local/windows-suite", "pending"),
    ]
    assert srs.local_state(statuses) == {"local/full-suite": "failure", "local/windows-suite": "pending"}
    assert srs.local_state([]) == {"local/full-suite": "missing", "local/windows-suite": "missing"}


def test_edge_state_uses_the_newest_run_on_sha():
    runs = [
        {"workflowName": srs.EDGE_WORKFLOW, "status": "completed", "conclusion": "success", "headSha": "abc"},
        {"workflowName": srs.EDGE_WORKFLOW, "status": "completed", "conclusion": "failure", "headSha": "abc"},
    ]
    assert srs.edge_state(runs, "abc") == "success"
    assert srs.edge_state(runs, "old") == "missing"
    assert srs.edge_state([{**runs[0], "status": "in_progress"}], "abc") == "pending"


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


def _fake_collect(monkeypatch, statuses):
    def fake_try(*args):
        if args[:2] == ("git", "rev-parse"):
            return CANDIDATE
        if args[:2] == ("git", "ls-remote"):
            return "abc\trefs/tags/v0.1.0"
        if args[:2] == ("git", "log"):
            return "abc change"
        return ""
    runs = [{"workflowName": srs.EDGE_WORKFLOW, "status": "completed", "conclusion": "success",
             "headSha": CANDIDATE}]
    monkeypatch.setattr(srs, "_try", fake_try)
    monkeypatch.setattr(srs, "_gh_json", lambda *a: statuses if a[0] == "api" else runs)
    monkeypatch.setattr(srs, "_variable", lambda *a, **k: None)


@pytest.mark.parametrize("skip, ready, notes", [
    (None, False, []),
    ("away", True, ["Live smoke skipped (--skip-smoke): away"]),
])
def test_collect_folds_the_smoke_gate_into_blockers(tmp_path, monkeypatch, skip, ready, notes):
    _fake_collect(monkeypatch, [_status(c) for c in srs.REQUIRED_LOCAL])
    report = srs.collect(["Unraid"], skip, str(tmp_path))
    assert report["edge_published"] is True
    assert report["ready"] is ready
    assert report["release_notes_lines"] == notes
    if skip is None:
        assert report["blockers"] == [
            f"Live smoke: no smoke result for Unraid at 123456789 ({tmp_path / CANDIDATE / 'Unraid.json'})"]
    text = srs.render(report)
    assert "Live smoke: " + ("skipped (away)" if skip else "block") in text
    if skip:
        assert "release notes must say: Live smoke skipped (--skip-smoke): away" in text


def test_collect_waits_for_both_local_suites(tmp_path, monkeypatch):
    _fake_collect(monkeypatch, [_status("local/full-suite")])
    report = srs.collect(["Unraid"], "away", str(tmp_path))
    assert not report["ready"]
    assert len(report["blockers"]) == 1
    assert "local/windows-suite" in report["blockers"][0] and "DesktopWorker" in report["blockers"][0]
    assert "local/windows-suite=missing" in srs.render(report)


def test_skip_windows_releases_with_a_note(tmp_path, monkeypatch):
    _fake_collect(monkeypatch, [_status("local/full-suite")])
    report = srs.collect(["Unraid"], "away", str(tmp_path), skip_windows="DesktopWorker off")
    assert report["ready"]
    assert "Windows full suite not run on this release (--skip-windows): DesktopWorker off" in report["release_notes_lines"]


def test_linux_suite_cannot_be_skipped(tmp_path, monkeypatch):
    _fake_collect(monkeypatch, [_status("local/windows-suite")])
    report = srs.collect(["Unraid"], "away", str(tmp_path), skip_windows="x")
    assert not report["ready"]
    assert report["blockers"][0].startswith("local/full-suite")
