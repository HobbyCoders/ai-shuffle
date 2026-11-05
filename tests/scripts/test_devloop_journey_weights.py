"""Hosted runtime planning and the log-to-weights producer (no app or browser)."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from scripts.devloop import ci


def _weights(tmp_path, seconds, per_case=10.0):
    path = tmp_path / "weights.json"
    path.write_text(json.dumps({"seconds": seconds, "seconds_per_case": per_case}))
    return path


@pytest.mark.parametrize("shards", [1, 4, 8, 30])
def test_runtime_partition_is_deterministic_and_exact(shards):
    weights = {f"{i}.journey.mjs": (i % 5 + 1) * 0.25 for i in range(25)}
    buckets = ci.partition(weights, shards)
    assert ci.partition(dict(reversed(list(weights.items()))), shards) == buckets
    flat = [name for bucket in buckets for name in bucket]
    assert sorted(flat) == sorted(weights)
    assert len(flat) == len(set(flat))
    assert len(buckets) == shards


def test_checked_in_nine_shard_plan_leaves_retry_headroom():
    weights = ci.estimated_seconds()
    buckets = ci.plan(9)
    assert len(buckets) == 9
    assert sorted(name for bucket in buckets for name in bucket) == sorted(weights)
    # Reserve 30 seconds for instance startup (7-12s in hosted run 42035121047):
    # first pass by ~19 minutes, then the full 180s retry before the 22m cutoff.
    loads = [sum(weights[name] for name in bucket) for bucket in buckets]
    assert max(loads) <= 19 * 60 - 30, loads


def test_plan_uses_runtime_instead_of_case_counts(tmp_path, monkeypatch):
    seconds = {"many-short.journey.mjs": 10, "few-long.journey.mjs": 90,
               "other-long.journey.mjs": 80}
    for name in seconds:
        (tmp_path / name).touch()
    path = _weights(tmp_path, seconds)

    def no_counts(path):
        pytest.fail(f"measured journeys must not use case count: {path}")

    monkeypatch.setattr(ci, "case_count", no_counts)
    buckets = ci.plan(2, tmp_path, path)
    assert buckets == [["few-long.journey.mjs"], ["many-short.journey.mjs", "other-long.journey.mjs"]]
    counts = {"many-short.journey.mjs": 100, "few-long.journey.mjs": 2,
              "other-long.journey.mjs": 1}
    assert ci.partition(counts, 2) != buckets


def test_unknown_journey_uses_case_count_times_per_case_median(tmp_path, monkeypatch):
    for name in ("known.journey.mjs", "new.journey.mjs"):
        (tmp_path / name).touch()
    path = _weights(tmp_path, {"known.journey.mjs": 90, "removed.journey.mjs": 999}, per_case=7.5)
    counted = []

    def count(path):
        counted.append(path.name)
        return 4

    monkeypatch.setattr(ci, "case_count", count)
    assert ci.estimated_seconds(tmp_path, path) == {"known.journey.mjs": 90, "new.journey.mjs": 30}
    assert counted == ["new.journey.mjs"]


def test_case_count_lists_all_cases_without_an_inherited_filter(tmp_path, monkeypatch):
    path = tmp_path / "a.journey.mjs"
    monkeypatch.setenv("JOURNEY_ONLY", "copy@desktop-fast")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return ci.subprocess.CompletedProcess(command, 0, stdout="a@desktop-fast\nb@phone-slow\n")

    monkeypatch.setattr(ci.subprocess, "run", run)
    assert ci.case_count(path, strict=True) == 2
    assert calls == [["node", str(path), "--list", "--only="]]


@pytest.mark.parametrize("returncode,stdout", [(1, "a\n"), (0, ""), (0, "a\na\n")])
def test_strict_case_count_refuses_unreadable_lists(tmp_path, monkeypatch, returncode, stdout):
    monkeypatch.setattr(ci.subprocess, "run", lambda command, **kwargs:
                        ci.subprocess.CompletedProcess(command, returncode, stdout=stdout))
    path = tmp_path / "a.journey.mjs"
    with pytest.raises(ValueError, match=r"Cannot read case count for a\.journey\.mjs"):
        ci.case_count(path, strict=True)
    assert ci.case_count(path) == 1


@pytest.mark.parametrize("error", [OSError("node missing"), ci.subprocess.TimeoutExpired("node", 60)])
def test_strict_case_count_refuses_listing_errors(tmp_path, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(ci.subprocess, "run", fail)
    path = tmp_path / "a.journey.mjs"
    with pytest.raises(ValueError, match=r"Cannot read case count for a\.journey\.mjs"):
        ci.case_count(path, strict=True)
    assert ci.case_count(path) == 1


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "slow"])
def test_invalid_runtime_measurements_are_refused(tmp_path, bad):
    (tmp_path / "a.journey.mjs").touch()
    with pytest.raises(ValueError, match="positive finite seconds"):
        ci.estimated_seconds(tmp_path, _weights(tmp_path, {"a.journey.mjs": bad}))
    with pytest.raises(ValueError, match="positive finite seconds"):
        ci.estimated_seconds(tmp_path, _weights(tmp_path, {}, per_case=bad))


def test_plan_stdout_stays_json_and_loads_reach_log_and_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(ci, "estimated_seconds", lambda *args: {"a.journey.mjs": 90, "b.journey.mjs": 30})
    assert ci.main(["plan", "--shards", "3"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [["a.journey.mjs"], ["b.journey.mjs"], []]
    for index, minutes in enumerate([1.5, 0.5, 0.0], 1):
        assert f"planned shard {index}/3: {minutes:.1f} estimated minutes" in captured.err
        assert f"| {index}/3 | {1 if index < 3 else 0} | {minutes:.1f} |" in summary.read_text()
    assert "excludes dev-instance setup and teardown" in summary.read_text()


# Frozen main 8ea52f8d case counts + PR #929's 16 short open-unloaded-chat cases.
# Seconds are hosted medians from runs 41071513091, 41082119694 and 41093871590,
# not today's weights file: refreshing estimates must not erase this regression.
REGRESSION_929 = {
    "chat-send-stream": (20, 185.95),
    "cleared-chat": (8, 69.98),
    "compaction": (20, 382.49),
    "first-visit": (1, 6.35),
    "mirrored-chat": (24, 269.46),
    "new-worktree-session": (28, 307.51),
    "open-unloaded-chat": (78, 319.41),
    "parked-timer": (4, 127.06),
    "phone-composer": (12, 79.99),
    "provider-account-switch": (4, 22.18),
    "provider-errors": (16, 322.30),
    "question-tray": (20, 334.12),
    "queue-while-busy": (8, 169.90),
    "reload-midstream": (8, 167.36),
    "send-during-init": (4, 55.05),
    "side-panels": (24, 189.19),
    "stop-midstream": (4, 54.06),
    "switch-chats": (12, 123.05),
    "tool-cards": (8, 107.02),
    "transcript-boundary": (8, 32.54),
    "transcript-reader": (32, 461.66),
    "type-at-boot": (4, 39.66),
    "voice-call": (10, 171.40),
    "voice-provider-endings": (6, 73.25),
    "worker-machine-settings": (2, 5.52),
}


def test_929_short_cases_cannot_pack_the_four_long_journeys_together():
    counts = {f"{stem}.journey.mjs": cases for stem, (cases, _) in REGRESSION_929.items()}
    seconds = {f"{stem}.journey.mjs": runtime for stem, (_, runtime) in REGRESSION_929.items()}
    long_four = {f"{stem}.journey.mjs" for stem in
                 ("compaction", "transcript-reader", "switch-chats", "reload-midstream")}
    old = ci.partition(counts, 4)
    assert any(long_four <= set(bucket) for bucket in old)
    new = ci.partition(seconds, 4)
    assert not any(long_four <= set(bucket) for bucket in new)
    heaviest = {"compaction.journey.mjs", "transcript-reader.journey.mjs"}
    assert not any(heaviest <= set(bucket) for bucket in new)
    old_load = max(sum(seconds[name] for name in bucket) for bucket in old)
    new_load = max(sum(seconds[name] for name in bucket) for bucket in new)
    assert new_load < 0.8 * old_load


def _log(stem, seconds, cases, passed=None, known_bugs=0):
    start = datetime(2026, 10, 30, tzinfo=timezone.utc)
    end = start + timedelta(seconds=seconds)
    known = f", {known_bugs} known bug" if known_bugs else ""
    return (f"{start.isoformat()} [ci] running {stem}.journey.mjs\n"
            f"{end.isoformat()} {stem}: {cases if passed is None else passed}/{cases} passed{known}; "
            f"timelines in /tmp/journeys/{stem}\n")


def _archive(log):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1_javascript _ UI journeys (1_4).txt", log)
        # Real GitHub archives also include copies of each step: never count twice.
        archive.writestr("javascript _ UI journeys (1_4)/3_Run journeys.txt", log)
        archive.writestr("2_Unrelated job.txt", log)
    return buffer.getvalue()


@pytest.fixture
def refresh_inventory(tmp_path, monkeypatch):
    directory = tmp_path / "journeys"
    directory.mkdir()
    for stem in ("a", "b"):
        (directory / f"{stem}.journey.mjs").touch()
    monkeypatch.setattr(ci, "JOURNEYS", directory)
    monkeypatch.setattr(ci, "case_count", lambda path, *, strict=False: {
        "a.journey.mjs": 3, "b.journey.mjs": 2,
    }[path.name])


def _run_state(command, *, attempt=1, conclusion="success"):
    state = {"status": "completed", "conclusion": conclusion, "attempt": attempt}
    # Emulate gh: fields not requested by the implementation are not returned.
    return json.dumps({field: state[field] for field in command[-1].split(",")})


def test_log_parser_times_each_journey_including_last_without_teardown():
    log = (_log("compaction", 30, 3) + _log("transcript-reader", 90, 6)
           + "2026-10-30T00:10:00.1234567Z [devloop] stopping (signal 15)\n"
           + "2026-10-30T00:11:00.1234567Z [ci] shard 1/4: passed; 9/9 cases passed\n")
    assert ci.parse_journey_log(log) == [("compaction.journey.mjs", 30, 3),
                                         ("transcript-reader.journey.mjs", 90, 6)]


@pytest.mark.parametrize("passed,cases,known_bugs", [(44, 46, 2), (1, 2, 1), (0, 2, 2)])
def test_log_parser_times_completed_journeys_with_known_bugs(passed, cases, known_bugs):
    log = _log("sidebar-truth", 650, cases, passed=passed, known_bugs=known_bugs)
    assert ci.parse_journey_log(log) == [("sidebar-truth.journey.mjs", 650, cases)]


@pytest.mark.parametrize("log", [
    "2026-10-30T00:00:00.1234567Z [ci] running cut.journey.mjs\n",
    ("2026-10-30T00:00:00Z [ci] running cut.journey.mjs\n"
     "2026-10-30T00:01:00Z [ci] running next.journey.mjs\n"),
    _log("failed", 30, 3, passed=2),
    _log("overcounted-known-bugs", 650, 46, passed=45, known_bugs=2),
    _log("failed-with-known-bugs", 650, 46, passed=43, known_bugs=2),
    _log("empty", 30, 0),
    _log("backwards", -1, 3),
])
def test_incomplete_or_failed_journeys_cannot_become_weights(log):
    with pytest.raises(ValueError):
        ci.parse_journey_log(log)


def test_refresh_produces_medians_from_green_archived_job_logs(tmp_path, monkeypatch, refresh_inventory):
    archives = {10: _archive(_log("a", 30, 3) + _log("b", 10, 2, passed=1, known_bugs=1)),
                20: _archive(_log("a", 50, 3) + _log("b", 30, 2, passed=1, known_bugs=1))}
    calls = []

    def gh(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command)
        assert command[:2] == ["gh", "api"]
        assert command[2].startswith("repos/owner/repo/actions/runs/")
        return archives[int(command[2].split("/")[-2])]

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    path = tmp_path / "weights.json"
    ci.refresh_weights([20, 10, 20], path)
    data = json.loads(path.read_text())
    assert data["seconds"] == {"a.journey.mjs": 40, "b.journey.mjs": 20}
    assert data["seconds_per_case"] == 12.5
    assert data["source_runs"] == [10, 20]
    assert "refresh-weights" in data["_comment"]
    assert len(calls) == 5
    first = path.read_bytes()
    ci.refresh_weights([10, 20], path)
    assert path.read_bytes() == first


@pytest.mark.parametrize("failure", ["red", "no-samples", "incomplete", "duplicate"])
def test_bad_refresh_leaves_existing_measurements_untouched(
        tmp_path, monkeypatch, refresh_inventory, failure):
    path = tmp_path / "weights.json"
    path.write_text("unchanged")

    def gh(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command, conclusion="failure" if failure == "red" else "success")
        if failure == "no-samples":
            return _archive("no journeys\n")
        if failure == "incomplete":
            return _archive("2026-10-30T00:00:00Z [ci] running cut.journey.mjs\n")
        return _archive(_log("a", 30, 3) * 2)

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError):
        ci.refresh_weights([10], path)
    assert path.read_text() == "unchanged"


def test_refresh_refuses_unreadable_case_counts_before_network_or_writing(tmp_path, monkeypatch):
    directory = tmp_path / "journeys"
    directory.mkdir()
    (directory / "a.journey.mjs").touch()
    monkeypatch.setattr(ci, "JOURNEYS", directory)
    monkeypatch.setattr(ci.subprocess, "run", lambda command, **kwargs:
                        ci.subprocess.CompletedProcess(command, 1, stdout=""))
    monkeypatch.setattr(ci.subprocess, "check_output", lambda *args, **kwargs:
                        pytest.fail("unreadable counts must be refused before fetching runs"))
    path = _weights(tmp_path, {"a.journey.mjs": 123})
    original = path.read_bytes()
    with pytest.raises(ValueError, match=r"Cannot read case count for a\.journey\.mjs"):
        ci.refresh_weights([10], path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("measured_cases", [1, 4])
def test_refresh_refuses_changed_case_count_after_a_complete_run(
        tmp_path, monkeypatch, refresh_inventory, measured_cases):
    path = _weights(tmp_path, {"unchanged.journey.mjs": 123})
    original = path.read_bytes()

    def gh(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command)
        cases = 3 if command[2].endswith("/10/logs") else measured_cases
        return _archive(_log("a", 30, cases) + _log("b", 10, 2))

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError, match=rf"Run 20.*a\.journey\.mjs.*measured {measured_cases}.*checkout 3"):
        ci.refresh_weights([10, 20], path)
    assert path.read_bytes() == original


def test_refresh_cli_dispatches_run_ids(monkeypatch):
    refreshed = []
    monkeypatch.setattr(ci, "refresh_weights", lambda ids: refreshed.extend(ids))
    assert ci.main(["refresh-weights", "20", "10"]) == 0
    assert refreshed == [20, 10]


@pytest.mark.parametrize("crashed", [False, True])
def test_refresh_refuses_retried_green_run_with_clear_reason_without_writing(
        tmp_path, monkeypatch, refresh_inventory, crashed):
    path = _weights(tmp_path, {"unchanged.journey.mjs": 123})
    original = path.read_bytes()
    first = ("2026-10-30T00:00:00Z [ci] running a.journey.mjs\n" if crashed
             else _log("a", 30, 3, passed=2))
    selection = "(whole journey)" if crashed else "copy@desktop-fast"
    retried = (first + f"2026-10-30T00:01:00Z [ci] retrying a.journey.mjs: {selection} (budget 180s)\n"
               + "2026-10-30T00:01:30Z a: 1/1 passed; timelines in /tmp/attempt-2/a\n"
               + "2026-10-30T00:01:30Z ::warning title=UI journey passed on retry::a recovered\n")

    def gh(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command)
        # Validate a clean run first too: no partial refresh may be written.
        return _archive(_log("a", 30, 3) + _log("b", 10, 2)
                        if command[2].endswith("/10/logs") else retried)

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError, match="Run 20 contained retries.*cannot be used for runtime weights"):
        ci.refresh_weights([10, 20], path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("attempt", [2, 3])
def test_refresh_refuses_reruns_before_downloading_partial_logs(
        tmp_path, monkeypatch, refresh_inventory, attempt):
    path = _weights(tmp_path, {"a.journey.mjs": 123, "b.journey.mjs": 456})
    original = path.read_bytes()
    calls = []

    def gh(command, **kwargs):
        calls.append(command)
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command, attempt=attempt)
        return _archive(_log("a", 30, 3))  # Latest-attempt archive hides prior jobs/retries.

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError, match=rf"Run 10.*attempt {attempt}.*not 1.*re-run"):
        ci.refresh_weights([10], path)
    assert path.read_bytes() == original
    assert not any(command[:2] == ["gh", "api"] for command in calls)


@pytest.mark.parametrize("stems", [("a",), ("a", "c")])
def test_refresh_refuses_partial_or_wrong_inventory_even_after_a_complete_run(
        tmp_path, monkeypatch, refresh_inventory, stems):
    # The existing weights may already be partial: expected coverage comes from files.
    path = _weights(tmp_path, {"a.journey.mjs": 123})
    original = path.read_bytes()

    def gh(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command)
        selected = ("a", "b") if command[2].endswith("/10/logs") else stems
        return _archive("".join(_log(stem, 30, 3 if stem == "a" else 2) for stem in selected))

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError, match=r"Run 20.*journey coverage.*missing.*b\.journey\.mjs") as error:
        ci.refresh_weights([10, 20], path)
    if "c" in stems:
        assert "unexpected" in str(error.value) and "c.journey.mjs" in str(error.value)
    assert path.read_bytes() == original


def test_refresh_must_not_merge_partial_runs_into_full_coverage(tmp_path, monkeypatch, refresh_inventory):
    path = _weights(tmp_path, {"a.journey.mjs": 123, "b.journey.mjs": 456})
    original = path.read_bytes()

    def gh(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command)
        return _archive(_log("a" if command[2].endswith("/10/logs") else "b", 30, 3))

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError, match=r"Run 10.*journey coverage.*missing.*b\.journey\.mjs"):
        ci.refresh_weights([10, 20], path)
    assert path.read_bytes() == original


def test_refresh_detects_retry_after_unterminated_output_line(tmp_path, monkeypatch, refresh_inventory):
    path = _weights(tmp_path, {"a.journey.mjs": 123, "b.journey.mjs": 456})
    original = path.read_bytes()
    log = ("2026-10-30T00:00:00Z [ci] running a.journey.mjs\n"
           "2026-10-30T00:01:00Z partial output[ci] retrying a.journey.mjs: (whole journey) (budget 180s)\n"
           "2026-10-30T00:02:00Z a: 1/1 passed; timelines in /tmp/attempt-2/a\n"
           + _log("b", 10, 2))

    def gh(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return "owner/repo\n"
        if command[:3] == ["gh", "run", "view"]:
            return _run_state(command)
        return _archive(log)

    monkeypatch.setattr(ci.subprocess, "check_output", gh)
    with pytest.raises(ValueError, match="Run 10 contained retries.*cannot be used for runtime weights"):
        ci.refresh_weights([10], path)
    assert path.read_bytes() == original
