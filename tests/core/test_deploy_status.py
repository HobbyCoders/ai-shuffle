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
    assert old["revision"] is None and "predates" in old["reason"]


def test_deploy_status_never_raises(monkeypatch):
    async def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(ds, "collect_workers", boom)
    report = asyncio.run(ds.deploy_status(pr=1))
    assert report["workers"] == [] and "network down" in report["reason"]
