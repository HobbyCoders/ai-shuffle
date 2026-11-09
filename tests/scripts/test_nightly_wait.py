"""nightly_wait follows a commit into a published nightly, never one run."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location("nightly_wait", Path(__file__).resolve().parents[2] / "scripts/ci/nightly_wait.py")
waiter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waiter)

OLD, MINE, NEWER, TIP = "0" * 40, "a" * 40, "b" * 40, "c" * 40
ORDER = [OLD, MINE, NEWER, TIP]
NOW = 1_800_000_000.0


def pointer(revision, version="1.2.3-dev.4"):
    payload = base64.b64encode(json.dumps({"channel": "nightly", "revision": revision, "version": version}).encode()).decode()
    return lambda: json.dumps({"payload": payload})


def run(run_id, sha, status="completed", conclusion=None, age=0, event="push", branch="main"):
    stamp = waiter.time.strftime("%Y-%m-%dT%H:%M:%SZ", waiter.time.gmtime(NOW - age))
    return {"id": run_id, "head_sha": sha, "status": status, "conclusion": conclusion, "event": event,
            "head_branch": branch, "updated_at": stamp, "html_url": f"https://example.test/runs/{run_id}"}


class Api:
    """Linear main history OLD < MINE < NEWER < TIP with scripted run lists."""

    def __init__(self, runs=(), publishers=(), hidden=()):
        self.runs = list(runs)
        self.publishers = list(publishers)
        self.hidden = list(hidden)  # real runs GitHub's newest-100 page would not show
        self.calls = []

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if endpoint.startswith("compare/"):
            base, head = endpoint[len("compare/"):].split("...")
            distance = ORDER.index(TIP if head == "main" else head) - ORDER.index(base)
            return {"status": "identical" if distance == 0 else "ahead" if distance > 0 else "behind"}
        if endpoint.startswith("actions/runs/"):
            run_id = int(endpoint.split("/")[2])
            return {"total_count": next(item.get("jobs", 0) for item in self.runs if item["id"] == run_id)}
        workflow, _, query = endpoint.partition("/runs?")
        status = dict(item.split("=") for item in query.split("&"))["status"]
        if workflow == "actions/workflows/main-suite.yml":
            pool = self.runs + (self.hidden if status != "completed" else [])
        elif workflow == "actions/workflows/desktop-source.yml":
            pool = self.publishers
        else:
            raise AssertionError(endpoint)
        return {"workflow_runs": [item for item in pool if item["status"] == status]}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def wait(api, fetch, **options):
    clock, said = Clock(), []
    code = waiter.wait(api, MINE, fetch=fetch, clock=clock, sleep=clock.sleep, say=said.append, **options)
    return code, said, clock


@pytest.mark.parametrize("revision", [MINE, NEWER, TIP])
def test_a_nightly_at_or_after_the_commit_is_published(revision):
    code, said, _ = wait(Api(), pointer(revision))
    assert code == 0
    assert said == [f"Published: nightly 1.2.3-dev.4 ({revision[:9]}) contains {MINE[:9]}."]


def test_a_replaced_waiting_run_is_not_an_event():
    """The commit's own run was replaced ("cancelled"); a newer run covers it."""
    api = Api([run(3, TIP, status="in_progress"), run(2, NEWER, conclusion="cancelled"), run(1, MINE, conclusion="cancelled")])
    polls = []

    def fetch():
        polls.append(1)
        if len(polls) == 3:
            api.runs[0] = run(3, TIP, conclusion="success")
        return pointer(TIP if len(polls) >= 5 else OLD)()

    code, said, _ = wait(api, fetch)
    assert code == 0
    assert not any("cancel" in line.lower() for line in said)
    assert [line.split(".")[0] for line in said] == [
        "Not in a nightly yet (live: 1", f"main-suite passed on {TIP[:9]}; waiting for the publisher", "Published: nightly 1"]


def test_unchanged_state_is_reported_once():
    api = Api([run(3, TIP, status="queued")])
    code, said, clock = wait(api, pointer(OLD), timeout=600, interval=60)
    assert code == 2
    assert len(said) == 2 and said[-1].startswith("Timed out")
    assert clock.now - NOW == 600


def test_a_failed_newest_run_with_nothing_waiting_blocks_after_settling():
    api = Api([run(3, TIP, conclusion="failure"), run(1, MINE, conclusion="cancelled")])
    code, said, clock = wait(api, pointer(OLD))
    assert code == 1
    assert "ended failure" in said[0] and "runs/3" in said[0]
    assert said[-1].startswith("Blocked")
    assert clock.now - NOW == waiter.SETTLE_SECONDS


def test_a_rerun_of_the_failed_run_resumes_the_wait():
    api = Api([run(3, TIP, conclusion="failure")])
    polls = []

    def fetch():
        polls.append(1)
        if len(polls) == 2:
            api.runs[0] = run(3, TIP, status="in_progress")
        if len(polls) == 12:
            return pointer(TIP)()
        return pointer(OLD)()

    code, said, _ = wait(api, fetch)
    assert code == 0 and not any(line.startswith("Blocked") for line in said)


def test_a_run_that_has_not_registered_yet_gets_time_to_appear():
    api = Api()
    polls = []

    def fetch():
        polls.append(1)
        if len(polls) == 3:
            api.runs.append(run(1, MINE, status="queued"))
        return pointer(OLD)()

    code, said, _ = wait(api, fetch, timeout=900)
    assert code == 2
    assert said[0].startswith("No main-suite run in flight covers") and "queued" in said[1]


def test_a_pass_the_publisher_never_ships_is_reported():
    api = Api([run(1, MINE, conclusion="success", age=waiter.PUBLISH_SECONDS + 60)])
    code, said, _ = wait(api, pointer(OLD))
    assert code == 1
    assert "no nightly" in said[0] and "desktop-source.yml" in said[0]


def test_only_real_main_runs_on_covering_commits_count():
    api = Api([run(9, OLD, status="in_progress"), run(8, TIP, status="in_progress", event="pull_request"),
               run(7, TIP, status="in_progress", branch="feature"), run(6, TIP, conclusion="failure")])
    assert waiter.suite_state(api, MINE, now=NOW)[0] == "failed"
    # A re-run or dispatch creates a newer run on the same commit; the newest wins.
    api.runs.append(run(10, TIP, status="queued", event="workflow_dispatch"))
    assert waiter.suite_state(api, MINE, now=NOW)[0] == "testing"


def test_once_reports_and_returns_without_sleeping():
    code, said, clock = wait(Api([run(1, MINE, status="in_progress")]), pointer(OLD), once=True)
    assert (code, len(said), clock.now) == (3, 1, NOW)


def test_brief_read_outages_do_not_end_the_wait():
    api = Api([run(1, MINE, status="in_progress")])
    polls = []

    def fetch():
        polls.append(1)
        if len(polls) in (2, 3):
            raise subprocess.TimeoutExpired("gh", 20)
        return pointer(MINE if len(polls) == 5 else OLD)()

    code, said, _ = wait(api, fetch)
    assert code == 0 and sum("retrying" in line for line in said) == 2

    def down():
        raise subprocess.TimeoutExpired("gh", 20)

    with pytest.raises(subprocess.TimeoutExpired):
        wait(api, down)


def test_pointer_must_be_nightly():
    payload = base64.b64encode(json.dumps({"channel": "stable", "revision": MINE}).encode()).decode()
    with pytest.raises(ValueError, match="nightly pointer"):
        waiter.published(lambda: json.dumps({"payload": payload}))


def test_a_merge_landing_mid_poll_is_never_reported_as_a_failure():
    """Coverage is asked of each run's own head, so a brand-new head still counts."""
    api = Api([run(2, TIP, status="queued"), run(1, MINE, conclusion="cancelled")])
    assert waiter.suite_state(api, MINE, now=NOW)[0] == "testing"
    # Even with nothing registered yet, a replaced run is not called a failure.
    state, detail = waiter.suite_state(Api([run(1, MINE, conclusion="cancelled")]), MINE, now=NOW)
    assert state == "none" and "cancel" not in detail.lower() and "fail" not in detail.lower()
    # A real failure under a replaced run is still the thing to report.
    api = Api([run(2, TIP, conclusion="cancelled"), run(1, NEWER, conclusion="failure")])
    state, detail = waiter.suite_state(api, MINE, now=NOW)
    assert state == "failed" and "runs/1" in detail and "ended failure" in detail


def test_a_run_stopped_on_its_first_failure_is_reported_not_mistaken_for_a_replaced_run():
    """Both end "cancelled"; only the stopped run started jobs."""
    stopped = dict(run(2, TIP, conclusion="cancelled"), jobs=64)
    state, detail = waiter.suite_state(Api([stopped, run(1, MINE, conclusion="cancelled")]), MINE, now=NOW)
    assert state == "failed" and "runs/2" in detail and "stopped after a job failed" in detail


def test_an_active_rerun_behind_a_page_of_newer_failures_still_counts():
    api = Api([run(9, TIP, conclusion="failure")], hidden=[run(1, NEWER, status="in_progress")])
    state, detail = waiter.suite_state(api, MINE, now=NOW)
    assert state == "testing" and NEWER[:9] in detail


@pytest.mark.parametrize("runs", [[run(3, TIP, conclusion="failure")], [],
                                  [run(1, MINE, conclusion="success", age=waiter.PUBLISH_SECONDS + 60)]])
def test_an_in_flight_publisher_keeps_the_wait_alive(runs):
    """A schedule, owner dispatch or owner local pass can still ship the commit."""
    api = Api(runs, publishers=[run(5, TIP, status="in_progress", event="schedule")])
    state, detail = waiter.suite_state(api, MINE, now=NOW)
    assert state == "publishing" and "runs/5" in detail
    api.publishers[0] = run(5, TIP, conclusion="success", event="schedule")
    assert waiter.suite_state(api, MINE, now=NOW)[0] in ("failed", "none", "unpublished")


def test_run_times_are_utc_in_any_local_zone(monkeypatch):
    if not hasattr(waiter.time, "tzset"):
        pytest.skip("tzset is POSIX-only")
    monkeypatch.setenv("TZ", "America/New_York")
    waiter.time.tzset()
    try:
        assert waiter._epoch("2026-07-01T12:00:00Z") == 1782907200
        assert waiter._epoch("2026-01-01T12:00:00Z") == 1767268800
    finally:
        monkeypatch.undo()
        waiter.time.tzset()


def test_ancestry_is_asked_once_per_revision():
    api = Api([run(3, TIP, status="queued")])
    wait(api, pointer(OLD), timeout=600, interval=60)
    compares = [call for call in api.calls if call.startswith("compare/")]
    assert sorted(compares) == sorted(set(compares))


def test_resolve_accepts_a_merged_pr_or_a_short_sha():
    pulls = {"pulls/7": {"merged": True, "merge_commit_sha": MINE, "base": {"ref": "main"}},
             "pulls/8": {"merged": False, "merge_commit_sha": None, "base": {"ref": "main"}},
             "pulls/9": {"merged": True, "merge_commit_sha": MINE, "base": {"ref": "release"}},
             "commits/aaaaaaa": {"sha": MINE}, f"compare/{MINE}...main": {"status": "ahead"},
             "commits/ddddddd": {"sha": "d" * 40}, f"compare/{'d' * 40}...main": {"status": "diverged"}}
    assert waiter.resolve(pulls.__getitem__, pr=7) == MINE
    assert waiter.resolve(pulls.__getitem__, "aaaaaaa") == MINE
    for options in ({"pr": 8}, {"pr": 9}, {"commit": "main"}, {"commit": "ddddddd"}):
        with pytest.raises(ValueError):
            waiter.resolve(pulls.__getitem__, **options)


def test_main_requires_exactly_one_target(capsys):
    for argv in ([], [MINE, "--pr", "7"], [MINE, "--interval-seconds", "1"]):
        with pytest.raises(SystemExit):
            waiter.main(argv)
    capsys.readouterr()
