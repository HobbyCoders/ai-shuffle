"""DeployStatus: which workers run a change (app/core/deploy_status.py)."""
import asyncio
import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from app.core import deploy_status as ds


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, path, message):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(message)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "ai-shuffle"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    shas = {
        "a": _commit(root, "app/a.py", "first"),
        "b": _commit(root, "app/b.py", "Fix browser tool (#10)"),
        "c": _commit(root, "android/app.kt", "Phone tweak (#11)"),
    }
    _git(root, "update-ref", "refs/remotes/origin/main", shas["c"])
    return root, shas


def _row(label, revision, *, online=True, channel="edge", offered=None, is_self=False):
    return {"device_id": label.lower(), "label": label, "is_self": is_self, "online": online,
            "revision": revision, "app_version": "1.0", "channel": channel, "offered": offered, "reason": None}


@pytest.fixture(autouse=True)
def no_gh():
    with patch("app.core.github_service.github_service._run_gh", side_effect=OSError("no gh")):
        yield


def test_drift_without_a_change(repo):
    root, shas = repo
    report = ds.assess([_row("Unraid", shas["a"], is_self=True), _row("DesktopWorker", shas["c"])],
                       repo=root, refresh=False)
    unraid, gaming = report["workers"]
    assert unraid["behind"] == 2 and unraid["short"] == shas["a"][:9]
    assert "2 behind" in unraid["action"]
    assert unraid["edge_publication_refusal"] is None  # GitHub is unavailable.
    assert gaming["behind"] == 0 and gaming["action"] == "up to date"
    assert report["android"] is None and report["change"] is None
    assert report["summary"] == "Live on: Unraid ✗ · DesktopWorker ✓"


def test_pr_resolves_via_squash_subject_and_reports_containment(repo):
    root, shas = repo
    report = ds.assess([_row("Unraid", shas["a"], offered=shas["c"][:12]), _row("DesktopWorker", shas["b"])],
                       pr=10, repo=root, refresh=False)
    assert report["change"]["commit"] == shas["b"]
    unraid, gaming = report["workers"]
    assert unraid["contains"] is False and unraid["action"] == "update this worker"
    assert gaming["contains"] is True and gaming["action"] == "up to date"
    assert report["android"]["build_needed"] is False
    assert report["summary"] == "Live on: Unraid ✗ · DesktopWorker ✓ · Android: no build needed"


def test_pr_via_gh_merge_commit(repo):
    root, shas = repo
    done = subprocess.CompletedProcess([], 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": shas["c"]}}), "")
    with patch("app.core.github_service.github_service._run_gh", return_value=done):
        report = ds.assess([_row("Unraid", shas["b"])], pr=999, repo=root, refresh=False)
    assert report["change"]["commit"] == shas["c"]
    assert report["android"]["build_needed"] is True
    assert report["summary"].endswith("Android: Play build needed")


def test_unmerged_pr_and_offer_without_the_change(repo):
    root, shas = repo
    open_pr = subprocess.CompletedProcess([], 0, json.dumps({"state": "OPEN", "mergeCommit": None}), "")
    with patch("app.core.github_service.github_service._run_gh", return_value=open_pr):
        report = ds.assess([_row("Unraid", shas["a"])], pr=12, repo=root, refresh=False)
    assert report["change"]["commit"] is None and "open" in report["change"]["reason"]
    # The offered build predates the change: edge has to publish first.
    report = ds.assess([_row("Unraid", shas["a"], offered=shas["b"][:12])], commit=shas["c"], repo=root, refresh=False)
    assert report["workers"][0]["action"].startswith("publish edge first")
    stable = ds.assess([_row("Unraid", shas["a"], channel="stable")], commit=shas["c"], repo=root, refresh=False)
    assert "stable release" in stable["workers"][0]["action"]


def test_degrades_per_worker_without_raising(repo):
    root, shas = repo
    report = ds.assess([_row("Off", shas["a"], online=False), _row("Old", None),
                        _row("Ghost", "deadbeefdeadbeef")], commit=shas["b"], repo=root, refresh=False)
    off, old, ghost = report["workers"]
    assert off["action"] == "offline"
    assert old["revision"] is None and old["action"] == "unknown"
    assert ghost["behind"] is None and "not found" in ghost["reason"]
    assert report["summary"] == "Live on: Off ? · Old ? · Ghost ? · Android: no build needed"
    bad = ds.assess([_row("X", shas["a"])], commit="--all", repo=root, refresh=False)
    assert bad["change"]["commit"] is None and "hex" in bad["change"]["reason"]


def test_no_checkout_gives_a_reason(monkeypatch):
    monkeypatch.setattr(ds, "find_repo", lambda: None)
    report = ds.assess([_row("Unraid", "abcdef123")], pr=1)
    assert report["reason"] and report["change"]["reason"] == report["reason"]
    assert report["workers"][0]["behind"] is None and report["workers"][0]["reason"] == report["reason"]


def _no_fixed_candidates(monkeypatch, tmp_path):
    from app.core.config import settings
    monkeypatch.delenv("SHUFFLE_SOURCE_REPO", raising=False)
    monkeypatch.setattr(ds, "_APP_ROOT", tmp_path / "installed-app")  # no Git data, like the desktop
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspace"))


def test_find_repo_uses_a_registered_project_checkout(monkeypatch, tmp_path, repo):
    # #1815: the desktop's ai-shuffle project lives outside the workspace folder.
    root, _ = repo
    other = tmp_path / "ai-shuffle-plugins"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "remote", "add", "origin", "https://github.com/HobbyCoders/ai-shuffle-plugins.git")
    _git(root, "remote", "add", "origin", "https://github.com/HobbyCoders/ai-shuffle.git")
    _no_fixed_candidates(monkeypatch, tmp_path)
    projects = [{"path": str(tmp_path / "gone")}, {"path": str(other)}, {"path": str(root)}]
    with patch("app.db.database.get_all_projects", return_value=projects):
        assert ds.find_repo() == root.resolve()


def test_find_repo_keeps_the_fixed_candidates_first(monkeypatch, tmp_path, repo):
    root, _ = repo
    _git(root, "remote", "add", "origin", "git@github.com:HobbyCoders/ai-shuffle.git")
    _no_fixed_candidates(monkeypatch, tmp_path)
    monkeypatch.setenv("SHUFFLE_SOURCE_REPO", str(root))
    with patch("app.db.database.get_all_projects", side_effect=AssertionError("not consulted")):
        assert ds.find_repo() == root.resolve()


def test_find_repo_without_a_database_finds_nothing(monkeypatch, tmp_path):
    _no_fixed_candidates(monkeypatch, tmp_path)
    with patch("app.db.database.get_all_projects", side_effect=RuntimeError("no pool")):
        assert ds.find_repo() is None


def test_collect_workers_reads_self_and_peers(monkeypatch):
    listing = {"self_device_id": "me", "runners": [
        {"device_id": "me", "device_name": "Unraid", "is_self": True, "online": True},
        {"device_id": "pc", "device_name": "DesktopWorker", "is_self": False, "online": True, "version": "0.9"},
        {"device_id": "off", "device_name": "Laptop", "is_self": False, "online": False},
        {"device_id": "old", "device_name": "Old", "is_self": False, "online": True},
    ]}
    monkeypatch.setattr("app.network.runners.directory", lambda: listing)
    monkeypatch.setattr("app.core.file_logging.source_revision", lambda: "a" * 40)
    monkeypatch.setattr("app.core.application_updates.status",
                        lambda: {"channel": "edge", "phase": "available", "version": "b" * 12})
    states = {"pc": {"revision": "c" * 40, "app_version": "1.1", "channel": "stable", "phase": "idle"},
              "old": {"phase": "idle"}}
    with patch("app.core.update_workers._peer_status_for", new=AsyncMock(side_effect=lambda d, fresh: states[d])):
        rows = asyncio.run(ds.collect_workers())
    me, pc, off, old = rows
    assert me["label"] == "Unraid" and me["revision"] == "a" * 40 and me["offered"] == "b" * 12
    assert pc["revision"] == "c" * 40 and pc["channel"] == "stable" and pc["app_version"] == "1.1"
    assert off["reason"] == "offline"
    assert old["revision"] is None and old["reason"] == "worker did not report its revision"


def test_deploy_status_never_raises(monkeypatch):
    async def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(ds, "collect_workers", boom)
    report = asyncio.run(ds.deploy_status(pr=1))
    assert report["workers"] == [] and "network down" in report["reason"]


def _gate_api(main, annotations, *, published=False):
    """gh api answers for the edge gate's check runs on ``main`` (#1940)."""
    runs = [{"id": 1, "name": "gate", "app": {"slug": "github-actions"}, "check_suite": {"id": 7},
             "output": {"annotations_count": 2}},
            {"id": 2, "name": "publish", "app": {"slug": "github-actions"}, "check_suite": {"id": 7},
             "conclusion": "success" if published else "skipped"},
            # A newer gate run refused for an unrelated PR completion: platform notes only.
            {"id": 3, "name": "gate", "app": {"slug": "github-actions"}, "check_suite": {"id": 8},
             "output": {"annotations_count": 1}},
            # Another workflow's successful publish job never counts as the edge publication.
            {"id": 4, "name": "publish", "app": {"slug": "github-actions"}, "check_suite": {"id": 9},
             "conclusion": "success"}]
    answers = {
        f"repos/HobbyCoders/ai-shuffle/commits/{main}/check-runs?per_page=100&filter=all": {"check_runs": runs},
        "repos/HobbyCoders/ai-shuffle/check-runs/1/annotations": annotations,
        "repos/HobbyCoders/ai-shuffle/check-runs/3/annotations": [
            {"annotation_level": "warning", "title": "", "message": "Node.js 20 is deprecated."}],
    }
    calls = []

    def run_gh(args, timeout=None):
        calls.append(args)
        assert args[0] == "api"
        return subprocess.CompletedProcess(args, 0, json.dumps(answers[args[1]]), "")
    return run_gh, calls


def test_publish_edge_first_names_the_refused_gate_and_dispatch_commands(repo, monkeypatch):
    root, shas = repo
    monkeypatch.setattr(ds, "_edge_refusals", {})
    message = ("Combined main tree was not tested by PR CI; edge publication needs owner-dispatched CI on main. "
               "CI missing, pending, or unsuccessful: .github/workflows/ci-backend.yml\n"
               "Dispatch on main:\ngh workflow run ci-backend.yml --ref main")
    run_gh, calls = _gate_api(shas["c"], [{"annotation_level": "warning", "title": ds.EDGE_REFUSAL_TITLE,
                                           "message": message}])
    with patch("app.core.github_service.github_service._run_gh", side_effect=run_gh):
        report = ds.assess([_row("Unraid", shas["a"], offered=shas["b"][:12]), _row("DesktopWorker", shas["c"])],
                           commit=shas["c"], repo=root, refresh=False)
        unraid, gaming = report["workers"]
        assert unraid["action"] == (
            "publish edge first (merged after the latest published build). Edge refused "
            f"{shas['c'][:9]}: Combined main tree was not tested by PR CI; edge publication needs "
            "owner-dispatched CI on main. CI missing, pending, or unsuccessful: .github/workflows/ci-backend.yml; "
            "Dispatch on main: gh workflow run ci-backend.yml --ref main")
        assert unraid["edge_publication_refusal"] == {
            "revision": report["origin_main"],
            "reason": message.strip().replace(":\n", ": ").replace("\n", "; "),
        }
        assert gaming["action"] == "up to date"
        assert gaming["edge_publication_refusal"] is None
        # Drift without a change carries the same next step; the lookup is cached.
        drift = ds.assess([_row("Unraid", shas["a"])], repo=root, refresh=False)
        assert drift["workers"][0]["action"].endswith("gh workflow run ci-backend.yml --ref main")
        assert drift["workers"][0]["edge_publication_refusal"] == unraid["edge_publication_refusal"]
    assert len(calls) == 3


@pytest.mark.parametrize("published,annotations", [
    (True, [{"title": "Edge publication refused", "message": "stale refusal"}]),
    (False, [{"title": "", "message": "Node.js 20 is deprecated."}]),
])
def test_publish_edge_first_without_a_refusal_stays_plain(repo, monkeypatch, published, annotations):
    root, shas = repo
    monkeypatch.setattr(ds, "_edge_refusals", {})
    run_gh, _ = _gate_api(shas["c"], annotations, published=published)
    with patch("app.core.github_service.github_service._run_gh", side_effect=run_gh):
        report = ds.assess([_row("Unraid", shas["a"], offered=shas["b"][:12])], commit=shas["c"],
                           repo=root, refresh=False)
    assert report["workers"][0]["action"] == "publish edge first (merged after the latest published build)"
    assert report["workers"][0]["edge_publication_refusal"] is None


@pytest.mark.parametrize("scenario", ["refused", "published", "no refusal", "stable", "current", "offline", "unknown"])
def test_api_serializes_real_current_main_refusal(repo, monkeypatch, scenario):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_admin
    from app.api.settings_updates import updates_router

    root, shas = repo
    monkeypatch.setattr(ds, "_edge_refusals", {})
    monkeypatch.setattr(ds, "find_repo", lambda: root)
    monkeypatch.setattr(ds, "_refresh", lambda repo: None)
    row = _row("Unraid", shas["a"], is_self=True)
    if scenario == "stable":
        row["channel"] = "stable"
    elif scenario == "current":
        row["revision"] = shas["c"]
    elif scenario == "offline":
        row["online"] = False
    elif scenario == "unknown":
        row["revision"] = None
    monkeypatch.setattr(ds, "collect_workers", AsyncMock(return_value=[row]))
    notes = [] if scenario == "no refusal" else [{"title": ds.EDGE_REFUSAL_TITLE, "message": "Dispatch CI on main."}]
    run_gh, calls = _gate_api(shas["c"], notes, published=scenario == "published")
    app = FastAPI()
    app.include_router(updates_router, prefix="/settings")
    app.dependency_overrides[require_admin] = lambda: "fixture owner"
    with patch("app.core.github_service.github_service._run_gh", side_effect=run_gh):
        response = TestClient(app).get("/settings/application-updates/deploy-status")
    assert response.status_code == 200
    report = response.json()
    worker = report["workers"][0]
    assert report["origin_main"] == shas["c"][:9]
    if scenario == "refused":
        assert worker["edge_publication_refusal"] == {"revision": report["origin_main"], "reason": "Dispatch CI on main."}
        assert worker["action"].endswith(f". Edge refused {shas['c'][:9]}: Dispatch CI on main.")
        assert calls[0][1].startswith(f"repos/HobbyCoders/ai-shuffle/commits/{shas['c']}/")
    else:
        assert worker["edge_publication_refusal"] is None
    if scenario in ("stable", "current", "offline", "unknown"):
        assert calls == []


def test_edge_refusal_title_matches_the_gate():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts/ci/edge_publication_gate.py"
    spec = importlib.util.spec_from_file_location("edge_gate_title", path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert gate.REFUSAL_TITLE == ds.EDGE_REFUSAL_TITLE
