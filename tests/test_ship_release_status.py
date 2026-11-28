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


def test_release_tags_reads_the_remote_and_ignores_local_only_tags(monkeypatch):
    def fake_try(*args):
        if args[:3] == ("git", "ls-remote", "--tags"):
            assert args[3] == "public"
            return ("t1\trefs/tags/v0.9.1\nc1\trefs/tags/v0.9.1^{}\n"
                    "b2\trefs/tags/android-latest\nc3\trefs/tags/v0.9.2")
        if args[:2] == ("git", "tag"):
            return "v5.0.0\nv0.9.1"
        return None
    monkeypatch.setattr(srs, "_try", fake_try)
    tags = srs.release_tags("public", "public/main")
    # Annotated tags resolve to their peeled commit, not the tag object.
    assert tags == {"v0.9.1": "c1", "android-latest": "b2", "v0.9.2": "c3"}
    assert srs.latest_final_tag(tags) == "v0.9.2"


def test_release_tags_offline_reads_only_tags_on_the_public_history(monkeypatch):
    calls = []

    def fake_try(*args):
        calls.append(args)
        return "v0.9.1" if args[:2] == ("git", "tag") else None
    monkeypatch.setattr(srs, "_try", fake_try)
    assert srs.release_tags("public", "public/main") == {"v0.9.1": None}
    # The archive's tags live in the same clone; only tags merged into public main count.
    assert ("git", "tag", "--list", "--merged", "public/main") in calls


def _shared_checkout(tmp_path):
    """A clone whose origin is the private archive and whose ``public`` is the public repo."""
    import subprocess

    def git(cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    def repo(name, tag, subject):
        path = tmp_path / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "user.email", "fixture@example.invalid")
        git(path, "config", "user.name", "fixture")
        git(path, "config", "commit.gpgsign", "false")
        (path / "a.txt").write_text(name)
        git(path, "add", ".")
        git(path, "commit", "-qm", "root")
        git(path, "tag", "-a", tag, "-m", tag)
        (path / "b.txt").write_text(subject)
        git(path, "add", ".")
        git(path, "commit", "-qm", subject)
        return path

    private = repo("private", "v0.9.6", "archive change")
    public = repo("public", "v1.0.0", "public change")
    shared = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", str(private), str(shared)], check=True)
    git(shared, "remote", "add", "public", str(public))
    return shared, git(public, "rev-parse", "HEAD"), git, public


def test_collect_reads_public_main_in_a_checkout_whose_origin_is_the_archive(tmp_path, monkeypatch):
    shared, public_head, git, public = _shared_checkout(tmp_path)
    import subprocess

    def run(*args):
        return subprocess.check_output(args, cwd=shared, text=True, stderr=subprocess.DEVNULL).strip()
    monkeypatch.setattr(srs, "_run", run)
    monkeypatch.setattr(srs, "public_main_ref", lambda: "public/main")
    monkeypatch.setattr(srs, "_gh_json", lambda *a: [])
    monkeypatch.setattr(srs, "_variable", lambda *a, **k: None)
    monkeypatch.setattr(srs, "main_ci", lambda head: {"state": "success", "url": "u"})
    report = srs.collect(["ServerWorker"], "away", str(tmp_path / "smoke"))
    assert report["main_ref"] == "public/main"
    assert report["remote"] == {"name": "public", "url": str(public)}
    assert report["main_head"] == public_head
    assert report["last_release"]["tag"] == "v1.0.0"
    assert report["last_release"]["sha"] == git(public, "rev-parse", "v1.0.0^{commit}")
    assert report["commit_subjects"][0].endswith("public change")
    assert report["suggested_tag"] == "v1.0.1"
    assert "Candidate: public/main (remote 'public' = HobbyCoders/ai-shuffle)" in srs.render(report)


def test_public_main_ref_uses_local_ci_public_resolution(monkeypatch):
    seen = {}

    def resolve(*, public_only=False):
        seen["public_only"] = public_only
        return "public/main"
    monkeypatch.setattr(srs.local_ci, "resolve_base", resolve)
    assert srs.public_main_ref() == "public/main" and seen == {"public_only": True}


def test_collect_without_a_public_remote_blocks(tmp_path, monkeypatch):
    def fail():
        raise srs.local_ci.Failure("No remote points to HobbyCoders/ai-shuffle")
    monkeypatch.setattr(srs, "public_main_ref", fail)
    monkeypatch.setattr(srs, "_try", lambda *a: None)
    monkeypatch.setattr(srs, "_gh_json", lambda *a: [])
    monkeypatch.setattr(srs, "_variable", lambda *a, **k: None)
    report = srs.collect(["ServerWorker"], "away", str(tmp_path))
    assert not report["ready"] and report["main_ref"] is None
    assert report["blockers"][0] == "No remote points to HobbyCoders/ai-shuffle"


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


GREEN = {"state": "success", "url": "https://github.com/HobbyCoders/ai-shuffle/actions/runs/1/attempts/1"}


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
    {"id": "x.2", "check": "stop button", "kind": "browser", "needs": ["install"]},
    {"id": "x.3", "check": "quit", "kind": "owner", "needs": ["device"]},
]
JOURNEY_ROW = {"id": "x.5", "check": "send", "kind": "journey", "journeys": ["chat-send-stream"]}


def _smoke_file(base, worker, revision=CANDIDATE, **status):
    statuses = {"x.1": "pass", "x.2": "pass", "x.3": "not_run", **status}
    kinds = {r["id"]: r["kind"] for r in SMOKE_ROWS}
    path = base / revision / f"{worker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"revision": revision, "worker": worker, "outcome": "pass", "rows": [
        {"id": k, "kind": kinds[k], "status": v, "reason": "x" if v != "pass" else None}
        for k, v in statuses.items()]}))


def _gate(tmp_path, workers=("ServerWorker", "DesktopWorker"), skip=None, revision=CANDIDATE):
    return srs.smoke_gate(revision, list(workers), skip, str(tmp_path), scenario_rows=SMOKE_ROWS)


def test_smoke_gate_pass(tmp_path):
    _smoke_file(tmp_path, "ServerWorker")
    _smoke_file(tmp_path, "DesktopWorker")
    report = _gate(tmp_path)
    assert report["verdict"] == "pass" and report["blockers"] == []
    assert report["release_notes_lines"] == ["Live smoke passed on ServerWorker, DesktopWorker at 123456789."]
    assert len(report["owner_todo"]) == 2  # owner rows never block


def test_smoke_gate_failing_row_blocks(tmp_path):
    _smoke_file(tmp_path, "ServerWorker", **{"x.1": "fail"})
    _smoke_file(tmp_path, "DesktopWorker")
    report = _gate(tmp_path)
    assert report["verdict"] == "block"
    assert report["blockers"] == ["ServerWorker: x.1 failed: x"]


def test_smoke_gate_unrecorded_browser_row_blocks(tmp_path):
    _smoke_file(tmp_path, "ServerWorker", **{"x.2": "not_run"})
    report = _gate(tmp_path, workers=["ServerWorker"])
    assert report["verdict"] == "block" and "x.2 not recorded" in report["blockers"][0]


def test_smoke_gate_missing_file_blocks(tmp_path):
    _smoke_file(tmp_path, "ServerWorker")
    report = _gate(tmp_path)
    assert report["verdict"] == "block"
    assert report["blockers"][0].startswith("no smoke result for DesktopWorker at 123456789")
    assert _gate(tmp_path / "empty", workers=())["verdict"] == "block"


def test_smoke_gate_wrong_revision_blocks(tmp_path):
    old = "f" * 40
    _smoke_file(tmp_path, "ServerWorker", revision=old)
    report = _gate(tmp_path, workers=["ServerWorker"])
    assert report["verdict"] == "block"
    # a pass for an older revision is not a pass for the candidate
    assert _gate(tmp_path, workers=["ServerWorker"], revision=old)["verdict"] == "pass"
    stale = tmp_path / CANDIDATE / "ServerWorker.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text((tmp_path / old / "ServerWorker.json").read_text())
    assert _gate(tmp_path, workers=["ServerWorker"])["blockers"] == [
        "ServerWorker's result is for revision fffffffff, not 123456789"]


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


def _fake_collect(monkeypatch, ci=GREEN, runs=()):
    def fake_try(*args):
        if args[:2] == ("git", "rev-parse"):
            return "abc" if args[-1].startswith("abc") else CANDIDATE
        if args[:2] == ("git", "ls-remote"):
            return "abc\trefs/tags/v0.1.0"
        if args[:2] == ("git", "log"):
            return "abc change"
        return ""
    runs = [{"workflowName": srs.EDGE_WORKFLOW, "status": "completed", "conclusion": "success",
             "headSha": CANDIDATE}, *runs]
    monkeypatch.setattr(srs, "_try", fake_try)
    monkeypatch.setattr(srs, "_gh_json", lambda *a: [] if a[0] == "api" else runs)
    monkeypatch.setattr(srs, "main_ci", lambda head: ci)
    monkeypatch.setattr(srs, "_variable", lambda *a, **k: None)
    monkeypatch.setattr(srs, "public_main_ref", lambda: "public/main")
    # The real catalog has journey rows; their CI evidence is stubbed green here.
    monkeypatch.setattr(srs, "journey_ci", lambda revision: {"state": "success", "url": "https://ci/run"})


@pytest.mark.parametrize("skip, ready, notes", [
    (None, False, []),
    ("away", True, ["Live smoke skipped (--skip-smoke): away"]),
])
def test_collect_folds_the_smoke_gate_into_blockers(tmp_path, monkeypatch, skip, ready, notes):
    _fake_collect(monkeypatch)
    report = srs.collect(["ServerWorker"], skip, str(tmp_path))
    assert report["edge_published"] is True
    assert report["ready"] is ready
    assert report["release_notes_lines"] == notes
    if skip is None:
        assert report["blockers"] == [
            f"Live smoke: no smoke result for ServerWorker at 123456789 ({tmp_path / CANDIDATE / 'ServerWorker.json'})"]
    text = srs.render(report)
    assert "Live smoke: " + ("skipped (away)" if skip else "block") in text
    if skip:
        assert "release notes must say: Live smoke skipped (--skip-smoke): away" in text


@pytest.mark.parametrize("state, url, tail", [
    ("pending", "https://ci/run/4", " (https://ci/run/4). Wait for it to finish, or tag a main commit"),
    ("cancelled", "https://ci/run/5", " (https://ci/run/5). Tag a main commit"),
    ("missing", None, ". Tag a main commit"),
])
def test_collect_waits_for_main_ci_on_the_candidate(tmp_path, monkeypatch, state, url, tail):
    _fake_collect(monkeypatch, ci={"state": state, "url": url})
    report = srs.collect(["ServerWorker"], "away", str(tmp_path))
    assert not report["ready"]
    assert report["blockers"] == [f"Main CI on 123456789 is {state}{tail} whose CI run passed."]
    assert f"Main CI on candidate: {state}" in srs.render(report)


def _ci_run(sha, conclusion="success", event="push", run_id=1):
    return {"workflowName": "CI", "status": "completed", "conclusion": conclusion, "headSha": sha,
            "event": event, "databaseId": run_id}


def test_a_red_candidate_names_the_newest_green_unreleased_main_commit(tmp_path, monkeypatch):
    older, released = "0" * 40, "e" * 40
    _fake_collect(monkeypatch, ci={"state": "cancelled", "url": None}, runs=[
        _ci_run(CANDIDATE, "cancelled"), _ci_run("f" * 40, event="pull_request"), _ci_run(older)])
    real_try = srs._try
    walked = []

    def fake_try(*a):
        if a[:3] == ("git", "rev-list", "--first-parent"):
            walked.append(a[-1])
            return "\n".join([CANDIDATE, "f" * 40, older])
        return real_try(*a)
    monkeypatch.setattr(srs, "_try", fake_try)
    blocker = srs.collect(["ServerWorker"], "away", str(tmp_path))["blockers"][0]
    assert blocker.endswith("The newest main commit with a green CI run is 000000000: rerun with --candidate 000000000.")
    assert walked == [f"abc..{CANDIDATE}"]  # only commits after the last release tag
    assert srs.newest_green_main([_ci_run(released)], CANDIDATE, None) is None


def test_a_newer_red_run_on_a_commit_outweighs_its_older_green_one(monkeypatch):
    monkeypatch.setattr(srs, "_try", lambda *a: "\n".join([CANDIDATE, "0" * 40]))
    runs = [_ci_run(CANDIDATE, run_id=1), _ci_run(CANDIDATE, "failure", "workflow_dispatch", run_id=2),
            _ci_run("0" * 40, run_id=3)]
    assert srs.newest_green_main(runs, CANDIDATE, "abc") == "0" * 40


def test_green_main_ci_is_the_only_suite_gate(tmp_path, monkeypatch):
    _fake_collect(monkeypatch)
    report = srs.collect(["ServerWorker"], "away", str(tmp_path))
    assert report["ready"], report["blockers"]
    assert report["main_ci"] == GREEN
    assert f"Main CI on candidate: success ({GREEN['url']})" in srs.render(report)


def _run(run_id, status="completed", conclusion="success", event="push"):
    return {"id": run_id, "status": status, "conclusion": conclusion, "event": event,
            "html_url": f"https://ci/run/{run_id}"}


@pytest.mark.parametrize("runs, expected", [
    ([], {"state": "missing", "url": None}),
    ([_run(3, "in_progress", None)], {"state": "pending", "url": "https://ci/run/3"}),
    ([_run(3), _run(4, conclusion="cancelled")], {"state": "cancelled", "url": "https://ci/run/4"}),
    ([_run(3, event="pull_request")], {"state": "missing", "url": None}),
    ([_run(3)], {"state": "not verified", "url": "https://ci/run/3"}),
])
def test_main_ci_explains_a_missing_pass_from_the_newest_main_run(monkeypatch, runs, expected):
    from scripts.ci import edge_publication_gate as edge
    monkeypatch.setattr(edge, "suite_status", lambda api, revision: None)
    monkeypatch.setattr(srs, "_gh_json", lambda *a: {"workflow_runs": runs})
    assert srs.main_ci(CANDIDATE) == expected


def test_main_ci_reports_the_verified_pass(monkeypatch):
    from scripts.ci import edge_publication_gate as edge
    asked = []
    monkeypatch.setattr(edge, "suite_status", lambda api, revision: asked.append(revision) or {
        "state": "success", "target_url": GREEN["url"]})
    monkeypatch.setattr(srs, "_gh_json", lambda *a: pytest.fail("not consulted"))
    assert srs.main_ci(CANDIDATE) == GREEN and asked == [CANDIDATE]


def test_skip_smoke_rows_excuses_not_run_rows_into_the_release_notes(tmp_path, monkeypatch):
    rows = SMOKE_ROWS + [{"id": "x.4", "check": "peer", "kind": "script", "two_worker": True}]
    _smoke_file(tmp_path, "ServerWorker-preview")
    path = tmp_path / CANDIDATE / "ServerWorker-preview.json"
    data = json.loads(path.read_text())
    data["rows"].append({"id": "x.4", "kind": "script", "status": "not_run", "reason": "worker offline"})
    data["preview"] = {"commit": CANDIDATE, "verified_at_end": True}
    path.write_text(json.dumps(data))
    blocked = srs.smoke_gate(CANDIDATE, None, None, str(tmp_path), scenario_rows=rows)
    assert blocked["verdict"] == "block"
    report = srs.smoke_gate(CANDIDATE, None, None, str(tmp_path), scenario_rows=rows,
                            skip_rows=[["two_worker", "no second worker for the preview"]])
    assert report["verdict"] == "pass"
    assert report["release_notes_lines"] == [
        "Live smoke passed on ServerWorker-preview (isolated preview instance of the candidate) at 123456789.",
        "Live smoke rows not run (--skip-smoke-rows two_worker): x.4 on ServerWorker-preview. "
        "Reason: no second worker for the preview"]


def test_smoke_gate_counts_the_journeys_ci_pass_for_journey_rows(tmp_path, monkeypatch):
    asked = []
    state = {"state": "failure", "url": "https://ci/run/9"}
    monkeypatch.setattr(srs, "journey_ci", lambda revision: asked.append(revision) or state)
    _smoke_file(tmp_path, "ServerWorker")
    rows = SMOKE_ROWS + [JOURNEY_ROW]
    blocked = srs.smoke_gate(CANDIDATE, ["ServerWorker"], None, str(tmp_path), scenario_rows=rows)
    assert asked == [CANDIDATE] and blocked["verdict"] == "block"
    assert blocked["blockers"][0].startswith("UI journeys at 123456789 are failure (https://ci/run/9)")
    state = {"state": "success", "url": "https://ci/run/10"}
    report = srs.smoke_gate(CANDIDATE, ["ServerWorker"], None, str(tmp_path), scenario_rows=rows)
    assert report["verdict"] == "pass"
    assert report["release_notes_lines"][-1] == (
        "UI journeys passed in CI at 123456789 (https://ci/run/10), standing in for 1 live smoke rows.")
    # No journey rows, or no candidate: GitHub is not asked.
    srs.smoke_gate(CANDIDATE, ["ServerWorker"], None, str(tmp_path), scenario_rows=SMOKE_ROWS)
    srs.smoke_gate("", ["ServerWorker"], None, str(tmp_path), scenario_rows=rows)
    assert asked == [CANDIDATE, CANDIDATE]


def test_journey_ci_reads_main_suite_through_gh(monkeypatch):
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if "head_sha=" in args[1]:
            return {"workflow_runs": [{"id": 5, "head_sha": CANDIDATE, "head_branch": "main", "html_url": "u"}]}
        return {"jobs": [{"name": f"javascript / UI journeys ({k}/2)", "status": "completed",
                          "conclusion": "success"} for k in (1, 2)]}
    monkeypatch.setattr(srs, "_gh_json", fake_gh)
    assert srs.journey_ci(CANDIDATE) == {"state": "success", "url": "u"}
    assert "actions/workflows/ci.yml/runs" in calls[0][1] and "/runs/5/jobs" in calls[1][1]


def test_collect_blocks_and_renders_when_the_journeys_are_not_green(tmp_path, monkeypatch):
    _fake_collect(monkeypatch)
    monkeypatch.setattr(srs, "journey_ci", lambda revision: {"state": "pending", "url": "https://ci/run/3"})
    report = srs.collect(["ServerWorker"], None, str(tmp_path))
    assert any(b.startswith("Live smoke: UI journeys at 123456789 are pending") for b in report["blockers"])
    assert "UI journeys in CI: pending https://ci/run/3 (" in srs.render(report)


def test_main_passes_skip_smoke_rows_through(monkeypatch, capsys):
    seen = {}

    def fake_collect(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {"ready": True}
    monkeypatch.setattr(srs, "collect", fake_collect)
    monkeypatch.setattr(srs, "render", lambda report: "ok")
    assert srs.main(["--skip-smoke-rows", "two_worker", "peer away", "--skip-smoke-rows", "x.2", "browser"]) == 0
    assert seen["args"][-1] == [["two_worker", "peer away"], ["x.2", "browser"]]
    assert seen["kwargs"] == {"candidate": None}
    assert srs.main(["--candidate", "abc123"]) == 0
    assert seen["kwargs"] == {"candidate": "abc123"}


TIP = "fedcba9876543210fedcba9876543210fedcba98"


def _fake_pinned(monkeypatch, green, history, seen=None):
    seen = {} if seen is None else seen

    def fake_try(*args):
        if args[:2] in (("git", "log"), ("git", "diff")):
            seen.setdefault(args[1], []).append(args[-1])
        if args[:3] == ("git", "rev-parse", "--verify"):
            ref = args[-1].removesuffix("^{commit}")
            return {CANDIDATE[:9]: CANDIDATE, CANDIDATE: CANDIDATE, "abc": "abc"}.get(ref)
        if args[:2] == ("git", "rev-parse"):
            return TIP
        if args[:3] == ("git", "rev-list", "--first-parent"):
            return "\n".join(history)
        if args[:2] == ("git", "ls-remote"):
            return "abc\trefs/tags/v0.1.0"
        if args[:2] == ("git", "log"):
            return "abc change"
        if args[:2] == ("git", "diff"):
            return "desktop/src/main.rs" if args[-1].endswith(TIP) else "frontend/src/app.ts"
        return ""

    monkeypatch.setattr(srs, "_try", fake_try)
    monkeypatch.setattr(srs, "_gh_json", lambda *a: [])
    monkeypatch.setattr(srs, "main_ci", lambda head: GREEN if head in green else {"state": "missing", "url": None})
    monkeypatch.setattr(srs, "_variable", lambda *a, **k: None)
    monkeypatch.setattr(srs, "public_main_ref", lambda: "public/main")


def test_a_pinned_candidate_reads_its_own_gates_after_main_moved(tmp_path, monkeypatch):
    # Main CI and smoke passed on CANDIDATE; another session then merged TIP. Pinning keeps them.
    seen = {}
    _fake_pinned(monkeypatch, {CANDIDATE}, [TIP, CANDIDATE, "abc"], seen)
    smoked = []
    real_gate = srs.smoke_gate
    monkeypatch.setattr(srs, "smoke_gate", lambda revision, *a, **k: smoked.append(revision) or real_gate(
        revision, *a, **k))
    unpinned = srs.collect(["ServerWorker"], "away", str(tmp_path))
    assert unpinned["main_head"] == TIP and not unpinned["ready"]
    seen.clear()
    report = srs.collect(["ServerWorker"], "away", str(tmp_path), candidate=CANDIDATE[:9])
    assert report["main_head"] == CANDIDATE and report["main_tip"] == TIP and report["pinned"]
    assert report["ready"], report["blockers"]
    # Commits, changed platforms and smoke are all read for the pinned revision, not the tip.
    assert seen == {"log": [f"abc..{CANDIDATE}"], "diff": [f"abc..{CANDIDATE}"]}
    assert smoked[-1] == CANDIDATE
    assert not report["platform_builds"]["desktop_installer"]["needed"]
    text = srs.render(report)
    assert f"{CANDIDATE[:9]} pinned; main is at {TIP[:9]}" in text
    assert "does not block a pinned release" in text


def test_a_pinned_candidate_without_green_ci_blocks(tmp_path, monkeypatch):
    _fake_pinned(monkeypatch, {TIP}, [TIP, CANDIDATE, "abc"])
    report = srs.collect(["ServerWorker"], "away", str(tmp_path), candidate=CANDIDATE)
    assert report["blockers"] == [
        f"Main CI on pinned {CANDIDATE[:9]} is missing. Tag a main commit whose CI run passed."]


def test_a_pinned_candidate_off_main_history_blocks(tmp_path, monkeypatch):
    # CANDIDATE stands for a PR-branch commit: reachable through a merge's second parent,
    # but not on main's first-parent history.
    _fake_pinned(monkeypatch, {CANDIDATE}, [TIP, "abc"])
    report = srs.collect(["ServerWorker"], "away", str(tmp_path), candidate=CANDIDATE)
    assert not report["ready"]
    assert report["blockers"][0].startswith(f"Pinned candidate {CANDIDATE[:9]} is not on public main")


def test_an_unknown_pinned_candidate_blocks(tmp_path, monkeypatch):
    _fake_pinned(monkeypatch, set(), [TIP])
    report = srs.collect(["ServerWorker"], "away", str(tmp_path), candidate="nope")
    assert report["blockers"][0].startswith("Pinned candidate nope is not a commit")
