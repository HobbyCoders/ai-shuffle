"""The CI entry point, with real summary files but no browser/database required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.devloop import ci


def case(id="copy@desktop-fast", status="fail", error="first error"):
    return {"id": id, "status": status, "ok": status != "fail", "ms": 11_170,
            "error": error if status == "fail" else None, "anomalies": [],
            "knownBug": "known bug #123" if status == "known-bug" else None}


@pytest.fixture
def driver(tmp_path, monkeypatch):
    calls, instances = [], []
    out = tmp_path / "out"
    summary = tmp_path / "job.md"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    # CI must clear this inherited developer filter, not run a partial shard.
    monkeypatch.setenv("JOURNEY_ONLY", "a developer's substring")
    files = ["flow.journey.mjs", "other.journey.mjs"]
    monkeypatch.setattr(ci, "plan", lambda _: [files, [], [], []])

    class Instance:
        def __init__(self, *args):
            instances.append(self)
            self.status = tmp_path / "status.json"
            self.stopped = False

        def wait_ready(self, timeout):
            return None

        def alive(self):
            return True

        def down(self):
            self.stopped = True

    monkeypatch.setattr(ci, "DevInstance", Instance)

    def run(first, second=None, *, opt=True):
        def command(argv, env, log, timeout):
            assert not instances[0].stopped  # same live instance on both attempts
            attempt = 2 if "attempt-2" in str(log) else 1
            name = Path(argv[1]).name
            selected = [a.removeprefix("--case=") for a in argv[2:]]
            calls.append((attempt, name, selected, timeout, env.copy()))
            responses = first if attempt == 1 else second
            rows = responses.get(name, [case("untouched@phone-slow", "pass")])
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(f"attempt {attempt} process error\n")
            if rows is None:  # process died before writing a summary
                return 2, 1.0
            journey = name.removesuffix(".journey.mjs")
            directory = Path(env["JOURNEY_OUT"]) / journey
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "summary.json").write_text(json.dumps({
                "journey": journey, "cases": rows, "finishedAt": "2026-11-02T00:00:00Z"}))
            for row in rows:
                verdict = "KNOWN BUG #123" if row["status"] == "known-bug" else row["status"].upper()
                (directory / f"{row['id']}.txt").write_text(
                    f"# {row['id']}  {verdict}  (11170ms)\n"
                    f"attempt {attempt}: {row['error'] or row['status']}\n")
            return (1 if any(not r["ok"] for r in rows) else 0), 11.17

        monkeypatch.setattr(ci, "run_command", command)
        argv = ["journeys", "--shard", "1/4", "--out", str(out)]
        if opt:
            argv.append("--retry-failures")
        code = ci.main(argv)
        return code

    return run, calls, out, summary, outputs, instances


def test_retries_only_failed_cases_once_and_preserves_both_errors(driver, capsys):
    run, calls, out, summary, outputs, instances = driver
    code = run({"flow.journey.mjs": [case(), case("other@phone-slow", "pass"),
                                    case("known@phone-fast", "known-bug")]},
               {"flow.journey.mjs": [case(error="second error")]})
    assert code == 1
    assert [(a, n, ids) for a, n, ids, *_ in calls] == [
        (1, "flow.journey.mjs", []), (1, "other.journey.mjs", []),
        (2, "flow.journey.mjs", ["copy@desktop-fast"])]
    assert all("JOURNEY_ONLY" not in env for *_, env in calls)
    assert len(instances) == 1 and instances[0].stopped
    text = summary.read_text()
    assert "attempt 1: first error" in text and "attempt 2: second error" in text
    assert "failed both attempts" in text
    assert "::warning" not in capsys.readouterr().out
    assert outputs.read_text() == "retried=true\n"
    for attempt, error in [(1, "first error"), (2, "second error")]:
        report = out / f"attempt-{attempt}/journeys/flow/copy@desktop-fast.txt"
        assert error in report.read_text()


def test_pass_on_retry_warns_and_lists_case_in_job_summary(driver, capsys):
    run, calls, out, summary, *_ = driver
    assert run({"flow.journey.mjs": [case()]},
               {"flow.journey.mjs": [case(status="pass")]}) == 0
    stdout = capsys.readouterr().out
    assert "::warning title=UI journey passed on retry::flow / copy@desktop-fast" in stdout
    assert "### Passed on retry (flaky journeys)" in summary.read_text()
    assert "`flow` / `copy@desktop-fast`" in summary.read_text()
    receipt = json.loads((out / "summary.json").read_text())
    row = next(r for r in receipt["rows"] if r["case"] == "copy@desktop-fast")
    assert row["status"] == "passed-on-retry"
    assert [a["status"] for a in row["attempts"]] == ["fail", "pass"]
    assert (out / "attempt-1/journeys/flow/copy@desktop-fast.txt").exists()
    assert (out / "attempt-2/journeys/flow/copy@desktop-fast.txt").exists()


@pytest.mark.parametrize("count, retries", [(3, 1), (4, 0)])
def test_small_failure_threshold(driver, count, retries):
    run, calls, _, summary, *_ = driver
    first = [case(f"case-{i}@phone-slow") for i in range(count)]
    second = [case(r["id"], "pass") for r in first]
    assert run({"flow.journey.mjs": first}, {"flow.journey.mjs": second}) == (0 if retries else 1)
    assert len([c for c in calls if c[0] == 2]) == retries
    if not retries:
        assert "Retry skipped" in summary.read_text() and "4" in summary.read_text()


def test_threshold_is_across_the_shard_not_per_journey(driver):
    run, calls, *_ = driver
    rows = [case(f"case-{i}@phone-slow") for i in range(2)]
    assert run({"flow.journey.mjs": rows, "other.journey.mjs": rows}, {}) == 1
    assert all(c[0] == 1 for c in calls)


@pytest.mark.parametrize("second, expected", [(None, 1), ([case(status="pass")], 0),
                                             ([case(error="retry case error")], 1)])
def test_crashed_journey_is_retried_as_a_whole_file(driver, second, expected):
    run, calls, _, summary, *_ = driver
    assert run({"flow.journey.mjs": None}, {"flow.journey.mjs": second}) == expected
    assert calls[-1][0:3] == (2, "flow.journey.mjs", [])
    text = summary.read_text()
    assert "attempt 1 process error" in text
    if expected:
        assert "attempt 2 process error" in text or "retry case error" in text


def test_known_bug_verdict_is_not_retried(driver, capsys):
    run, calls, _, summary, outputs, *_ = driver
    assert run({"flow.journey.mjs": [case(status="known-bug")]}, {}) == 0
    assert len(calls) == 2
    assert "known-bug" in summary.read_text()
    assert "::warning" not in capsys.readouterr().out
    assert not outputs.exists()


@pytest.mark.parametrize("rows", [[], [case("different@phone-fast", "pass")],
                                  [case(status="pass"), case("extra@phone-slow", "pass")]])
def test_retry_must_prove_exactly_the_requested_cases_ran(driver, rows):
    run, _, _, summary, *_ = driver
    assert run({"flow.journey.mjs": [case()]}, {"flow.journey.mjs": rows}) == 1
    assert "summary" in summary.read_text().lower()


def test_no_implicit_retries_without_opt_in(driver):
    run, calls, *_ = driver
    assert run({"flow.journey.mjs": [case()]}, {}, opt=False) == 1
    assert len(calls) == 2 and all(c[0] == 1 for c in calls)


def test_retry_budget_and_step_teardown_reserve(driver, monkeypatch):
    from scripts.devloop import journey_retry as retry
    run, calls, *_ = driver
    clock = iter([0, 1300, 1300, 1300])
    monkeypatch.setattr(retry.time, "monotonic", lambda: next(clock, 1300))
    assert run({"flow.journey.mjs": [case()]}, {"flow.journey.mjs": [case(status="pass")]}) == 0
    # 25 minutes minus 180 seconds reserved for down(), minus elapsed time.
    assert calls[-1][3] == 20


def test_no_step_headroom_skips_retry(driver, monkeypatch):
    from scripts.devloop import journey_retry as retry
    run, calls, _, summary, *_ = driver
    clock = iter([0, 1321])
    monkeypatch.setattr(retry.time, "monotonic", lambda: next(clock, 1321))
    assert run({"flow.journey.mjs": [case()]}, {}) == 1
    assert len(calls) == 2
    assert "Retry skipped" in summary.read_text()


def test_warning_escapes_workflow_commands(driver, capsys):
    run, *_ = driver
    id = "copy%\n::error::injected@desktop-fast"
    assert run({"flow.journey.mjs": [case(id)]},
               {"flow.journey.mjs": [case(id, "pass")]}) == 0
    warnings = [s for s in capsys.readouterr().out.splitlines() if s.startswith("::warning")]
    assert len(warnings) == 1 and "copy%25%0A::error::injected" in warnings[0]



def test_retry_time_budget_is_shared_across_journeys(driver, monkeypatch):
    from scripts.devloop import journey_retry as retry
    run, calls, *_ = driver
    clock = iter([0, 10, 10, 189])
    monkeypatch.setattr(retry.time, "monotonic", lambda: next(clock, 189))
    first = {name: [case()] for name in ("flow.journey.mjs", "other.journey.mjs")}
    second = {name: [case(status="pass")] for name in first}
    assert run(first, second) == 0
    assert [c[3] for c in calls if c[0] == 2] == [180, 1]


def test_old_attempt_output_is_never_reused_or_overwritten(driver):
    run, calls, out, *_ = driver
    evidence = out / "attempt-1/journeys/flow/summary.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("previous evidence")
    with pytest.raises(FileExistsError):
        run({"flow.journey.mjs": [case()]}, {})
    assert not calls and evidence.read_text() == "previous evidence"


def test_known_bug_on_retry_keeps_the_runner_verdict(driver):
    run, _, out, *_ = driver
    assert run({"flow.journey.mjs": [case()]},
               {"flow.journey.mjs": [case(status="known-bug")]}) == 0
    rows = json.loads((out / "summary.json").read_text())["rows"]
    assert rows[0]["attempts"][1]["status"] == "known-bug"
