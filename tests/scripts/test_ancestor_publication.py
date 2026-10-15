"""Source publication follows first-parent order, not attestation finish time."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ancestor_gate", ROOT / "scripts/ci/edge_publication_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    def commit(name):
        (tmp_path / name).write_text(name)
        git("add", name)
        git("commit", "-qm", name)
        return git("rev-parse", "HEAD")
    base = commit("base")
    first = commit("first")
    tip = commit("tip")
    git("remote", "add", "origin", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    state = {"published": base, "statuses": {first: "success"}, "hook": lambda revision: None}
    def api(endpoint):
        if endpoint == "branches/main":
            return {"protected": True, "commit": {"sha": git("rev-parse", "main")}}
        revision = endpoint.split("/")[1]
        state["hook"](revision)
        value = state["statuses"].get(revision)
        return [] if value is None else [{"context": gate.CONTEXT, "state": value,
                                         "creator": {"id": gate.OWNER_ID}}]
    monkeypatch.setattr(gate, "published_edge_revision", lambda: state["published"])
    return state, api, git, commit, base, first, tip


def test_attested_ancestor_after_main_moved_publishes(lane):
    state, api, _, _, _, first, _ = lane
    assert gate.publication_readiness(api, first)[0]
    assert gate.newest_attested(api, gate.main_history(api))[0] == first


@pytest.mark.parametrize("which", ["same", "older"])
def test_published_or_older_revision_refused(lane, which):
    state, api, _, _, _, first, tip = lane
    state["published"] = first if which == "same" else tip
    ready, reason = gate.publication_readiness(api, first)
    assert not ready and "older" in reason


def test_side_branch_even_when_merged_is_not_first_parent(lane):
    state, api, git, commit, base, _, _ = lane
    git("switch", "-c", "side", base)
    side = commit("side")
    git("switch", "main")
    git("merge", "--no-ff", "side", "-m", "Merge side")
    state["statuses"][side] = "success"
    ready, reason = gate.publication_readiness(api, side)
    assert not ready and "first-parent" in reason


def test_two_attestations_finish_out_of_order(lane):
    state, api, _, _, _, first, tip = lane
    state["statuses"] = {first: "pending", tip: "success"}
    assert gate.publication_readiness(api, tip)[0]
    # The older run finishes last; completion time must not decide the winner.
    state["statuses"][first] = "success"
    ready, reason = gate.publication_readiness(api, first)
    assert not ready and "Newer attested" in reason
    state["published"] = tip
    assert not gate.publication_readiness(api, first)[0]


def test_new_attestation_during_gate_is_rechecked(lane):
    state, api, _, _, _, first, tip = lane
    def complete_newer(revision):
        if revision == first:
            state["statuses"][tip] = "success"
    state["hook"] = complete_newer
    assert not gate.publication_readiness(api, first)[0]


def test_unattested_main_movement_during_gate_does_not_waste_pass(lane):
    state, api, _, commit, _, first, _ = lane
    def merge(revision):
        if revision == first:
            state["hook"] = lambda revision: None
            commit("later")
    state["hook"] = merge
    assert gate.publication_readiness(api, first)[0]


def test_publication_during_gate_refuses_stale_baseline(lane):
    state, api, _, _, _, first, tip = lane
    state["hook"] = lambda revision: state.update(published=tip)
    assert not gate.publication_readiness(api, first)[0]


def test_unknown_published_history_fails_closed(lane, monkeypatch):
    state, api, _, _, _, first, _ = lane
    state["published"] = "f" * 40
    with pytest.raises((ValueError, FileNotFoundError)):
        gate.publication_readiness(api, first)


@pytest.mark.parametrize("args", [["--select"], ["--json"]])
def test_cli_selects_and_rechecks_exact_ancestor(lane, monkeypatch, tmp_path, capsys, args):
    import json
    state, api, git, _, _, first, tip = lane
    event, output = tmp_path / "event.json", tmp_path / "output"
    event.write_text(json.dumps({"schedule": "0 6 * * *"}))
    for name, value in {"GITHUB_REPOSITORY": gate.REPOSITORY, "GITHUB_REF": "refs/heads/main",
                        "GITHUB_EVENT_NAME": "schedule", "GITHUB_SHA": tip,
                        "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(gate, "GitHub", lambda: api)
    if args == ["--json"]:
        git("switch", "--detach", first)
        monkeypatch.setenv("SOURCE_REVISION", first)
    monkeypatch.chdir(ROOT)  # control cwd must not change which Git tree is checked
    gate.main([*args, "--source-root", str(tmp_path)])
    result = capsys.readouterr().out
    if args == ["--select"]:
        assert output.read_text() == f"ready=true\nrevision={first}\n"
    else:
        assert json.loads(result) == {"ready": True, "revision": first, "published": state["published"],
                                      "reason": f"Local full suite passed on {first[:9]}: "}


def test_cli_refuses_checkout_other_than_selected_ancestor(lane, monkeypatch, tmp_path):
    import json
    _, _, _, _, _, first, tip = lane
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"schedule": "0 6 * * *"}))
    for name, value in {"GITHUB_REPOSITORY": gate.REPOSITORY, "GITHUB_REF": "refs/heads/main",
                        "GITHUB_EVENT_NAME": "schedule", "GITHUB_SHA": tip,
                        "SOURCE_REVISION": first, "GITHUB_EVENT_PATH": str(event)}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="exact source revision"):
        gate.main(["--json"])


@pytest.mark.parametrize("status", ["pending", "failure", "error"])
def test_ancestor_needs_its_own_success(lane, status):
    state, api, _, _, _, first, _ = lane
    state["statuses"][first] = status
    assert not gate.publication_readiness(api, first)[0]


def test_candidate_scan_is_bounded():
    calls = []
    assert gate.newest_attested(lambda endpoint: calls.append(endpoint) or [], [str(i) for i in range(101)]) == (None, None)
    assert len(calls) == 100

@pytest.mark.parametrize("race", [False, True])
def test_actual_publisher_sql_never_overwrites_a_newer_revision(tmp_path, race):
    import base64
    import json
    import os
    import shutil
    import sqlite3

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to drive the real source publisher")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_SHA": "a" * 40,
           "SOURCE_REVISION": "a" * 40, "GH_TOKEN": "fixture", "RELEASE_GITHUB_REPO_ID": "42",
           "ACCOUNT_HUB_DATABASE_ID": "11111111-1111-1111-1111-111111111111",
           "PUBLISH_TEST_CASE": "new", "SOURCE_CHANNEL": "edge"}
    subprocess.run([node, "--import", (ROOT / "desktop/test/helpers/publish-source-preload.mjs").as_uri(),
                    str(ROOT / "desktop/scripts/publish-source.mjs")],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    sql = (tmp_path / "insert.sql").read_text()
    manifest = json.loads(json.loads((tmp_path / "record.json").read_text())["manifest"])
    sequence = json.loads(base64.b64decode(manifest["payload"]))["sequence"]
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE source_releases (revision TEXT, channel TEXT, version TEXT, "
                   "asset_id INTEGER, size INTEGER, manifest TEXT, sequence INTEGER, "
                   "release_repository TEXT, release_repository_id INTEGER, UNIQUE(revision,channel))")
        db.execute("INSERT INTO source_releases(revision,channel,sequence) VALUES (?,'edge',1)", ("b" * 40,))
        if race:
            # Newer source landed after our gate/database read, but our older
            # source's signing clock is higher. Sequence alone is unsafe.
            db.execute("INSERT INTO source_releases(revision,channel,sequence) VALUES (?,'edge',?)",
                       ("c" * 40, sequence - 1))
        db.execute(sql)
        latest = db.execute("SELECT revision FROM source_releases WHERE channel='edge' ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        assert latest == ("c" if race else "a") * 40
        assert db.execute("SELECT COUNT(*) FROM source_releases WHERE revision=?", ("a" * 40,)).fetchone()[0] == (0 if race else 1)
