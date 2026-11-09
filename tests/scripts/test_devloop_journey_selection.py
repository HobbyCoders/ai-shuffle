"""Which UI journeys a workflow run needs (no app or browser)."""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts.devloop import ci

NAMES = sorted(path.name for path in ci.journey_files())


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   cwd=repo, check=True, capture_output=True)


def _merge_checkout(tmp_path):
    """A repository whose HEAD is the merge of a branch into its base, like a PR run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "kept.txt").write_text("base\n")
    (repo / "old name.txt").write_text("renamed later\n" * 20)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "added.txt").write_text("new\n")
    _git(repo, "mv", "old name.txt", "new name.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "base-only.txt").write_text("landed on main meanwhile\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main moved")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    return repo


def test_pull_request_files_are_the_merge_commits_own_changes(tmp_path):
    repo = _merge_checkout(tmp_path)
    # Not what main gained meanwhile; a rename lists the old and the new path.
    assert ci.pull_request_files(repo) == ["added.txt", "new name.txt", "old name.txt"]


def test_unreadable_merge_diff_is_reported_as_unknown(tmp_path):
    repo = _merge_checkout(tmp_path)
    _git(repo, "checkout", "-q", "feature")  # an ordinary one-parent commit
    assert ci.pull_request_files(repo) is None
    assert ci.pull_request_files(tmp_path / "missing") is None
    empty = tmp_path / "not-a-repository"
    empty.mkdir()
    assert ci.pull_request_files(empty) is None


@pytest.mark.parametrize("event", [None, "push", "workflow_dispatch", "workflow_call", "schedule"])
def test_only_a_pull_request_is_narrowed(event, monkeypatch):
    monkeypatch.setattr(ci, "pull_request_files", lambda root=ci.ROOT: pytest.fail("must not read a diff"))
    only, why = ci.selection_for_event(event)
    assert only is None and why.startswith("every journey")


def test_a_pull_request_falls_back_to_every_journey_when_unsure(monkeypatch):
    monkeypatch.setattr(ci, "pull_request_files", lambda root=ci.ROOT: None)
    assert ci.selection_for_event("pull_request")[0] is None
    monkeypatch.setattr(ci, "pull_request_files", lambda root=ci.ROOT: ["docs/readme.md"])
    monkeypatch.setattr(ci, "select_journeys", lambda files, previous_map=None: None)
    assert ci.selection_for_event("pull_request")[0] is None


def test_real_selector_narrows_covers_and_widens():
    # A docs or unmapped backend change needs no journey.
    assert ci.select_journeys(["docs/ci/pr-checks.md", "README.md"]) == []
    # A journey file selects itself.
    assert ci.select_journeys([f"frontend/tests/journeys/{NAMES[0]}"]) == [NAMES[0]]
    # The harness, the selection itself and the workflow run everything (None).
    for path in ("frontend/tests/journeys/runner.mjs", "frontend/tests/journeys/select.mjs",
                 "scripts/devloop/ci.py",
                 ".github/workflows/ci-frontend.yml", "frontend/src/app.css"):
        assert ci.select_journeys([path]) is None, path
    # A UI source file no rule covers runs everything; its unit test alone does not.
    assert ci.select_journeys(["frontend/src/lib/__no_rule_covers_this__.ts"]) is None
    assert ci.select_journeys(["frontend/src/lib/__no_rule_covers_this__.test.ts"]) == []


def test_an_edited_map_is_judged_against_the_base_copy_and_never_drops_a_journey(tmp_path, monkeypatch):
    current = json.loads((ci.ROOT / ci.JOURNEY_MAP).read_text())
    text = json.dumps(current)
    probe = "frontend/src/lib/__no_rule_covers_this__.ts"
    # Without the base's copy an edited map still runs everything.
    assert ci.select_journeys([ci.JOURNEY_MAP]) is None
    # With it, the edit alone needs nothing and other files select as usual.
    assert ci.select_journeys([ci.JOURNEY_MAP, "docs/readme.md"], previous_map=text) == []
    assert ci.select_journeys([ci.JOURNEY_MAP, f"frontend/tests/journeys/{NAMES[2]}"], previous_map=text) == [NAMES[2]]
    assert ci.select_journeys([ci.JOURNEY_MAP, probe], previous_map=text) is None
    # A base map that names journeys for a file still applies if this change's map dropped the rule.
    stricter = {**current, "rules": [*current["rules"], {
        "why": "base-only rule", "globs": ["docs/readme.md"],
        "journeys": [NAMES[4].removesuffix(ci.SUFFIX)]}]}
    assert ci.select_journeys([ci.JOURNEY_MAP, "docs/readme.md"], previous_map=json.dumps(stricter)) == [NAMES[4]]
    stricter["all"] = [*current["all"], {"why": "base-only all rule", "globs": ["docs/readme.md"]}]
    assert ci.select_journeys([ci.JOURNEY_MAP, "docs/readme.md"], previous_map=json.dumps(stricter)) is None
    # The workflow passes the base copy only when the map changed, and all when it cannot be read.
    seen = []
    monkeypatch.setattr(ci, "select_journeys", lambda files, previous_map=None: seen.append(previous_map) or [])
    monkeypatch.setattr(ci, "base_journey_map", lambda root=ci.ROOT: "BASE")
    monkeypatch.setattr(ci, "pull_request_files", lambda root=ci.ROOT: ["docs/readme.md"])
    ci.selection_for_event("pull_request")
    monkeypatch.setattr(ci, "pull_request_files", lambda root=ci.ROOT: [ci.JOURNEY_MAP])
    ci.selection_for_event("pull_request")
    assert seen == [None, "BASE"]


def test_base_map_is_read_from_the_merge_commits_first_parent(tmp_path):
    repo = _merge_checkout(tmp_path)
    assert ci.base_journey_map(repo) is None  # the base has no map
    target = repo / ci.JOURNEY_MAP
    target.parent.mkdir(parents=True)
    _git(repo, "checkout", "-q", "-b", "with-map", "HEAD^1")
    target.write_text('{"all": [], "rules": []}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base map")
    _git(repo, "checkout", "-q", "-b", "edits-map")
    target.write_text('{"all": [], "rules": [], "edited": true}')
    _git(repo, "commit", "-q", "-am", "edit map")
    _git(repo, "checkout", "-q", "with-map")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "edits-map")
    assert json.loads(ci.base_journey_map(repo)) == {"all": [], "rules": []}
    _git(repo, "checkout", "-q", "-b", "broken", "HEAD^1")
    target.write_text("not json")
    _git(repo, "commit", "-q", "-am", "broken base map")
    _git(repo, "checkout", "-q", "-b", "fixes")
    target.write_text('{"all": [], "rules": []}')
    _git(repo, "commit", "-q", "-am", "fix")
    _git(repo, "checkout", "-q", "broken")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "fixes")
    assert ci.base_journey_map(repo) is None


@pytest.mark.parametrize("answer", [
    "not json", "{}", json.dumps({"all": True, "journeys": []}),
    json.dumps({"all": False, "journeys": ["__unknown_journey__"]}),
    json.dumps({"all": False, "journeys": "chat"}), json.dumps({"journeys": []}),
])
def test_an_unusable_selector_answer_runs_every_journey(answer, tmp_path):
    selector = tmp_path / "selector.mjs"
    selector.write_text(f"console.log({json.dumps(answer)});\n")
    assert ci.select_journeys(["docs/readme.md"], selector) is None
    failing = tmp_path / "failing.mjs"
    failing.write_text('console.log(JSON.stringify({ all: false, journeys: [] })); process.exit(2);\n')
    assert ci.select_journeys(["docs/readme.md"], failing) is None
    assert ci.select_journeys(["docs/readme.md"], tmp_path / "missing.mjs") is None


def test_only_values_round_trip_and_reject_unknown_names():
    assert ci.parse_only(None) is None and ci.parse_only("*") is None
    assert ci.parse_only("") == []
    short = NAMES[1].removesuffix(ci.SUFFIX)
    assert ci.parse_only(f"{short}, {NAMES[0]},{short}") == sorted({NAMES[0], NAMES[1]})
    with pytest.raises(ValueError, match="__unknown_journey__"):
        ci.parse_only("__unknown_journey__")


def test_a_selection_is_split_exactly_and_the_shards_repeat_it():
    only = [NAMES[0], NAMES[3], NAMES[5]]
    buckets = ci.plan(ci.HOSTED_SHARDS, only=only)
    assert len(buckets) == ci.HOSTED_SHARDS
    assert sorted(name for bucket in buckets for name in bucket) == sorted(only)
    assert buckets == ci.plan(ci.HOSTED_SHARDS, only=list(reversed(only)))  # each shard recomputes it
    assert ci.plan(ci.HOSTED_SHARDS, only=[]) == [[] for _ in range(ci.HOSTED_SHARDS)]


@pytest.mark.parametrize(("only", "expected_only", "count"), [
    (None, "*", ci.HOSTED_SHARDS), ([], "", 0), ([NAMES[0], NAMES[3]], None, 2),
])
def test_matrix_prints_exactly_the_two_step_outputs(only, expected_only, count, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(ci, "selection_for_event", lambda event: (only, "test"))
    assert ci.main(["matrix"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == ["only", "shards"]
    shown, shards = lines[0].split("=", 1)[1], json.loads(lines[1].split("=", 1)[1])
    if expected_only is None:
        expected_only = ",".join(name.removesuffix(ci.SUFFIX) for name in only)
    assert shown == expected_only
    assert ci.parse_only(shown) == only
    assert len(shards) == count and " " not in lines[1]
    # The shards a job starts for are exactly those the same selection gives a file.
    buckets = ci.plan(ci.HOSTED_SHARDS, only=only)
    assert shards == [index for index, bucket in enumerate(buckets, 1) if bucket]
