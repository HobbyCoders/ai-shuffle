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
    """Linear main history OLD < MINE < NEWER < TIP with a scripted run list."""

    def __init__(self, runs=()):
        self.runs = list(runs)

    def __call__(self, endpoint):
        if endpoint.startswith("compare/"):
            base, head = endpoint[len("compare/"):].split("...")
            head = TIP if head == "main" else head
            distance = ORDER.index(head) - ORDER.index(base)
            status = "identical" if distance == 0 else "ahead" if distance > 0 else "behind"
            return {"status": status, "commits": [{"sha": sha} for sha in ORDER[ORDER.index(base) + 1:ORDER.index(head) + 1]]}
        if endpoint.startswith("actions/workflows/main-suite.yml/runs"):
            return {"workflow_runs": self.runs}
        raise AssertionError(endpoint)


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
    assert said[0].startswith("No main-suite run covers") and "queued" in said[1]


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


def test_commit_must_be_on_main_and_pointer_must_be_nightly():
    with pytest.raises(ValueError, match="not on main"):
        waiter.suite_state(BehindApi(), MINE, now=NOW)
    payload = base64.b64encode(json.dumps({"channel": "stable", "revision": MINE}).encode()).decode()
    with pytest.raises(ValueError, match="nightly pointer"):
        waiter.published(lambda: json.dumps({"payload": payload}))


class BehindApi:
    def __call__(self, endpoint):
        return {"status": "diverged", "commits": []}


def test_resolve_accepts_a_merged_pr_or_a_short_sha():
    pulls = {"pulls/7": {"merged": True, "merge_commit_sha": MINE, "base": {"ref": "main"}},
             "pulls/8": {"merged": False, "merge_commit_sha": None, "base": {"ref": "main"}},
             "pulls/9": {"merged": True, "merge_commit_sha": MINE, "base": {"ref": "release"}},
             "commits/aaaaaaa": {"sha": MINE}}
    assert waiter.resolve(pulls.__getitem__, pr=7) == MINE
    assert waiter.resolve(pulls.__getitem__, "aaaaaaa") == MINE
    for options in ({"pr": 8}, {"pr": 9}, {"commit": "main"}):
        with pytest.raises(ValueError):
            waiter.resolve(pulls.__getitem__, **options)


def test_main_requires_exactly_one_target(capsys):
    for argv in ([], [MINE, "--pr", "7"], [MINE, "--interval-seconds", "1"]):
        with pytest.raises(SystemExit):
            waiter.main(argv)
    capsys.readouterr()
