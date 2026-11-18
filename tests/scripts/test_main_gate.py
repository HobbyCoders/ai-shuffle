"""The CI gate judges the run from its start, so its verdict never queues."""
import copy
import http.client
import importlib.util
import io
import json
import re
from pathlib import Path
import urllib.error

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("main_gate", ROOT / "scripts/ci/main_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PLAN = gate.Plan()
ci_plan = gate.ci_plan
FIXTURE = json.loads((ROOT / "tests/scripts/fixtures/main_suite_jobs.json").read_text())["jobs"]
ALL_PROVEN = ("backend", "fluid", "windows", "browser-workspace", "container", "identity", "desktop-source",
              "javascript-checks")
SELF = {"id": 999, "name": "CI", "status": "in_progress", "conclusion": None}
# The journey shards of the fixture run, in shard order (the count changes with the workflow).
SHARDS = sorted((job["name"] for job in FIXTURE if job["name"].startswith("javascript / UI journeys (")),
                key=lambda name: int(name.split("(")[1].split("/")[0]))
JOURNEYS_TEMPLATE = next(slot.full for slot in PLAN.slots if slot.key == "journeys")


def lookup(**found):
    return {"result": "success", "outputs": dict(found)}


def listing():
    return copy.deepcopy(FIXTURE)


def named(jobs, name):
    return next(job for job in jobs if job["name"] == name)


def skip_area(jobs, area):
    """What GitHub lists for a called workflow the lookup skipped: one job, named after the call."""
    kept = [job for job in jobs if not job["name"].startswith(f"{area} / ")]
    return kept + [{"id": 900 + len(kept), "name": area, "status": "completed", "conclusion": "skipped"}]


def selection(jobs, shards, step=gate.SELECTED[("javascript", "journeys")][2]):
    """Set what the journey selection job names as its choice (None: no such step)."""
    plan = named(jobs, "javascript / Select UI journeys")
    plan["steps"] = [{"name": "Select the journeys and their shards", "status": "completed",
                      "conclusion": "success"}]
    if shards is not None:
        plan["steps"].append({"name": step + json.dumps(shards), "status": "completed", "conclusion": "success"})
    return jobs


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


# --- What the gate expects ---------------------------------------------------

def test_a_real_green_run_is_complete_and_passes():
    jobs = listing() + [SELF]
    assert gate.missing(PLAN, gate.judged(PLAN, jobs)) == []
    # Every listed job of the real run belongs to exactly one job definition.
    for job in gate.judged(PLAN, jobs):
        assert PLAN.slot_of(job["name"]) is not None, job["name"]
    evidence = gate.verdict(PLAN, jobs, lookup())
    assert evidence["backend"] == evidence["windows"] == "true"
    assert json.loads(evidence["backend-needs"]) == {
        name: {"result": "success"} for name in ("backend-shard", "backend-contracts", "backend-security")}


def test_areas_match_the_aggregate_inventory_and_the_called_verdict_jobs():
    assert set(PLAN.areas) == set(gate.REQUIRED)
    jobs = workflow("ci.yml")["jobs"]
    # The verdict jobs CI skips are the pull-request required checks;
    # their names stay as they are, and only CI asks them to step aside.
    names = {"backend": "Backend tests", "javascript": "JavaScript package checks",
             "windows": "Windows desktop", "browser-workspace": "Real browser workspace"}
    for area, key in gate.CALLER_GATED.items():
        called = workflow(jobs[area]["uses"].rsplit("/", 1)[1])
        assert called["jobs"][key]["name"] == names[area]
        assert called["jobs"][key]["if"].startswith("always()")
        assert called["jobs"][key]["if"].endswith(" && inputs.gated_by_caller != true")
        assert called["on"]["workflow_call"]["inputs"]["gated_by_caller"]["default"] == "false"
        assert jobs[area]["with"]["gated_by_caller"] == "true"
    assert [area for area, job in jobs.items() if "gated_by_caller" in (job.get("with") or {})] == [
        area for area in jobs if area in gate.CALLER_GATED]


def test_most_specific_definition_wins():
    assert PLAN.slot_of("windows / Windows desktop shard (2/3)").key == "desktop"
    assert PLAN.slot_of("windows / Windows desktop").key == "desktop-gate"
    assert PLAN.slot_of("windows / Windows ${{ matrix.part }}").key == "suite"
    assert PLAN.slot_of(SHARDS[6]).key == "journeys"
    assert PLAN.slot_of("javascript / record-journeys / Record the pass").key == "record-journeys"
    assert PLAN.is_advisory("source-ui / build") and PLAN.is_advisory("Plan what this run has to check")
    for area in gate.REQUIRED:
        assert not PLAN.is_advisory(area) and not PLAN.is_advisory(f"{area} / anything")


def test_a_job_github_has_not_created_yet_is_waited_for():
    jobs = [job for job in listing() if "UI journeys (" not in job["name"]]
    assert gate.missing(PLAN, jobs) == SHARDS
    # The whole called workflow not listed yet (its call waits on the lookup).
    jobs = [job for job in listing() if not job["name"].startswith("fluid / ")]
    assert "fluid / fluid unit tests" in gate.missing(PLAN, jobs)


# --- Verdict rules -----------------------------------------------------------

@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure", None])
def test_any_required_job_that_did_not_pass_fails_the_verdict(conclusion):
    for name in ("backend / backend-shard 3", SHARDS[4], "update-reach",
                 "windows / Selected tests on Windows", "desktop / Typecheck and unit tests"):
        jobs = listing()
        named(jobs, name)["conclusion"] = conclusion
        with pytest.raises(ValueError):
            gate.verdict(PLAN, jobs, lookup())


def test_advisory_jobs_never_decide():
    jobs = listing()
    named(jobs, "source-ui / build")["conclusion"] = "failure"
    named(jobs, "Plan what this run has to check")["conclusion"] = "failure"
    gate.verdict(PLAN, jobs, {"result": "failure", "outputs": {}})


def test_a_proven_skip_passes_and_an_unproven_skip_fails():
    for area in ("backend", "fluid", "windows", "browser-workspace", "container", "identity", "desktop-source"):
        jobs = skip_area(listing(), area)
        evidence = gate.verdict(PLAN, jobs, lookup(**{area: "true"}))
        if area in ("backend", "windows"):
            assert evidence[area] == "false"  # nothing ran, so no receipts to check
        for found in ({"result": "success", "outputs": {area: "false"}},
                      {"result": "success", "outputs": {}},
                      {"result": "failure", "outputs": {}},
                      {"result": "success", "outputs": {"javascript-checks": "true"}}):
            with pytest.raises(ValueError):
                gate.verdict(PLAN, jobs, found)
    # An area main never skips cannot be skipped by any lookup answer.
    for area in ("javascript", "policy", "desktop"):
        with pytest.raises(ValueError):
            gate.verdict(PLAN, skip_area(listing(), area), lookup(**{a: "true" for a in ALL_PROVEN}))
    for area in ("android-and-docs", "supply-chain", "update-reach"):
        jobs = listing()
        named(jobs, area)["conclusion"] = "skipped"
        with pytest.raises(ValueError):
            gate.verdict(PLAN, jobs, lookup(**{a: "true" for a in ALL_PROVEN}))


def javascript_checks():
    return [job for job in listing() if job["name"].startswith("javascript / ")
            and PLAN.slot_of(job["name"]).key in ("frontend", "contracts", "build", "typechecks", "mobile",
                                                  "tools", "browser-mcp", "visual")]


def test_javascript_checks_skip_only_on_their_record():
    jobs = listing()
    for job in javascript_checks():
        named(jobs, job["name"])["conclusion"] = "skipped"
    gate.verdict(PLAN, jobs, lookup(**{"javascript-checks": "true"}))
    with pytest.raises(ValueError):
        gate.verdict(PLAN, jobs, lookup())
    # One check skipped without its record.
    jobs = listing()
    named(jobs, "javascript / Mobile tests")["conclusion"] = "skipped"
    with pytest.raises(ValueError):
        gate.verdict(PLAN, jobs, lookup())
    # With the record the checks must not have run at all.
    with pytest.raises(ValueError):
        gate.verdict(PLAN, listing(), lookup(**{"javascript-checks": "true"}))


def test_journeys_skip_only_when_the_selection_chose_none():
    # A successful selection is proof of an empty choice only while this is
    # the journeys' sole condition.
    assert workflow("ci-frontend.yml")["jobs"]["journeys"]["if"] == "needs.journey-plan.outputs.shards != '[]'"
    jobs =[job for job in listing() if "UI journeys (" not in job["name"]]
    selection(jobs, [])
    jobs.append({"id": 800, "name": JOURNEYS_TEMPLATE, "status": "completed",
                 "conclusion": "skipped"})
    gate.verdict(PLAN, jobs, lookup())
    named(jobs, "javascript / Select UI journeys")["conclusion"] = "skipped"
    with pytest.raises(ValueError):
        gate.verdict(PLAN, jobs, lookup())


def test_inner_rules_match_the_pull_request_verdict_jobs():
    # ci-frontend.yml's own rule, branch by branch.
    expected = workflow("ci-frontend.yml")["jobs"]["javascript"]["needs"]
    ok = {name: {"result": "success"} for name in expected}
    gate.check_javascript(ok, expected, False)
    for name in expected:
        for result in ("failure", "cancelled", "skipped", None):
            broken = dict(ok)
            if result is None:
                del broken[name]
            else:
                broken[name] = {"result": result}
            if name == "journeys" and result == "skipped":
                gate.check_javascript(broken, expected, False)
                continue
            with pytest.raises(ValueError):
                gate.check_javascript(broken, expected, False)
    for area in ("browser-workspace", "windows"):
        key = gate.CALLER_GATED[area]
        needs = workflow(workflow("ci.yml")["jobs"][area]["uses"].rsplit("/", 1)[1])["jobs"][key]["needs"]
        good = {name: {"result": "success"} for name in needs}
        gate.check_every_branch(area, good, needs)
        for name in needs:
            for result in ("failure", "cancelled", "skipped"):
                with pytest.raises(ValueError):
                    gate.check_every_branch(area, {**good, name: {"result": result}}, needs)
            with pytest.raises(ValueError):
                gate.check_every_branch(area, {k: v for k, v in good.items() if k != name}, needs)


# --- Watching the run --------------------------------------------------------

class Run:
    """A fake Actions API: each GET returns the next listing (the last one repeats)."""

    def __init__(self, *listings, errors=0, error=None):
        self.listings, self.errors, self.calls, self.slept = list(listings), errors, [], []
        self.error = error or urllib.error.URLError("unreachable")

    def __call__(self, method, path, token):
        self.calls.append((method, path))
        if method == "POST":
            return {}
        if self.errors:
            self.errors -= 1
            raise self.error
        jobs = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
        return {"total_count": len(jobs), "jobs": jobs}

    def sleep(self, seconds):
        self.slept.append(seconds)

    @property
    def cancelled(self):
        return ("POST", "/repos/o/r/actions/runs/7/cancel") in self.calls


def watch(run, clock=None, summary=None):
    out = io.StringIO()
    ticks = iter(range(0, 10**6, gate.POLL_SECONDS))
    jobs = gate.watch(PLAN, "o/r", 7, 2, "t", call=run, sleep=run.sleep, out=out,
                      clock=clock or (lambda: next(ticks)), summary_path=summary)
    return jobs, out.getvalue()


def running():
    jobs = listing()
    for job in jobs:
        if "UI journeys (" in job["name"]:
            job.update(status="in_progress", conclusion=None)
    return jobs + [SELF]


def test_the_verdict_comes_the_moment_the_last_job_finishes():
    done = listing() + [SELF]
    run = Run(running(), running(), done)
    jobs, _ = watch(run)
    assert not run.cancelled
    # Two polls while running, then the finished listing confirmed once, a few seconds later.
    assert run.slept == [gate.POLL_SECONDS, gate.POLL_SECONDS, gate.CONFIRM_SECONDS]
    assert all("/attempts/2/jobs" in path for method, path in run.calls)
    gate.verdict(PLAN, jobs, lookup())


def test_a_required_job_that_fails_after_the_gate_started_cancels_the_run(tmp_path):
    late = running()
    named(late, SHARDS[4]).update(status="completed", conclusion="failure")
    run = Run(running(), late)
    summary = tmp_path / "summary.md"
    with pytest.raises(SystemExit) as stopped:
        watch(run, summary=str(summary))
    assert stopped.value.code == 1
    assert run.cancelled
    assert SHARDS[4] in summary.read_text()


def test_a_cancelled_run_is_never_green():
    cancelled = running()
    for job in cancelled:
        if job["status"] != "completed" and job["name"] != "CI":
            job.update(status="completed", conclusion="cancelled")
    run = Run(running(), cancelled)
    with pytest.raises(SystemExit):
        watch(run)
    # Even a listing that somehow reached the verdict fails on a cancelled job.
    with pytest.raises(ValueError):
        gate.verdict(PLAN, cancelled, lookup())


def test_the_gate_never_judges_or_cancels_for_its_own_job():
    done = listing() + [{**SELF, "status": "completed", "conclusion": "failure"}]
    run = Run(done)
    jobs, _ = watch(run)
    assert not run.cancelled and all(job["name"] != "CI" for job in jobs)


def test_advisory_failures_do_not_cancel():
    done = listing() + [SELF]
    named(done, "source-ui / build")["conclusion"] = "failure"
    run = Run(done)
    watch(run)
    assert not run.cancelled


def test_a_late_matrix_is_waited_for_then_judged():
    early = [job for job in listing() if "UI journeys (" not in job["name"]] + [SELF]
    run = Run(early, early, early, running(), listing() + [SELF])
    watch(run)
    assert run.slept.count(gate.POLL_SECONDS) >= 3


def test_a_listing_that_changes_while_confirming_is_read_again():
    done = listing() + [SELF]
    grown = done + [{"id": 777, "name": "desktop / Package Windows shell", "status": "queued", "conclusion": None}]
    run = Run(done, grown, done, done)
    watch(run)
    assert run.slept == [gate.CONFIRM_SECONDS, gate.POLL_SECONDS, gate.CONFIRM_SECONDS]


def test_an_expected_job_that_never_appears_fails_instead_of_hanging():
    early = [job for job in listing() if "UI journeys (" not in job["name"]] + [SELF]
    run = Run(early)
    out = io.StringIO()
    ticks = iter(range(0, 10**6, gate.POLL_SECONDS))
    with pytest.raises(SystemExit):
        gate.watch(PLAN, "o/r", 7, 1, "t", call=run, sleep=run.sleep, out=out, clock=lambda: next(ticks))
    assert "UI journeys" in out.getvalue() and not run.cancelled


# What a read of GitHub's API raised on real runs, or can: no answer, a timeout,
# a reset connection, a body cut short, a body that is not the JSON expected.
UNREADABLE = [
    urllib.error.URLError("unreachable"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
    http.client.IncompleteRead(b"", 225289),
    http.client.RemoteDisconnected("closed without response"),
    json.JSONDecodeError("cut short", "{", 1),
]


@pytest.mark.parametrize("error", UNREADABLE, ids=lambda error: type(error).__name__)
def test_an_unreadable_run_is_not_proven(error):
    run = Run(listing(), errors=gate.MAX_FAILED_READS, error=error)
    with pytest.raises(SystemExit):
        watch(run)
    # A few failed reads are retried.
    run = Run(listing() + [SELF], errors=3, error=error)
    jobs, out = watch(run)
    assert jobs and out.count("could not read the run's jobs") == 3


@pytest.mark.parametrize("error", UNREADABLE, ids=lambda error: type(error).__name__)
def test_settling_survives_an_unreadable_poll(error):
    run = Run(listing() + [SELF], errors=2, error=error)
    ticks = iter(range(0, 10**6, gate.POLL_SECONDS))
    assert gate.settle(PLAN, "o/r", 7, 1, "t", run, run.sleep, lambda: next(ticks), io.StringIO())


def test_main_writes_the_evidence_flags(tmp_path):
    output = tmp_path / "output"
    run = Run(listing() + [SELF])
    environ = {"RESULTS": json.dumps({gate.LOOKUP: lookup()}), "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7",
               "GITHUB_RUN_ATTEMPT": "1", "GH_TOKEN": "t", "GITHUB_OUTPUT": str(output)}
    assert gate.main(environ, call=run, sleep=run.sleep, out=io.StringIO()) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["backend"] == lines["windows"] == "true"
    assert json.loads(lines["backend-needs"])["backend-shard"] == {"result": "success"}
    # An unproven skip fails the run. The areas that did finish still have their
    # receipts checked, so their pass can be recorded.
    output.unlink()
    run = Run(skip_area(listing(), "fluid") + [SELF])
    assert gate.main(environ, call=run, sleep=run.sleep, out=io.StringIO()) == 1
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["backend"] == lines["windows"] == "true"
    assert not [key for key in lines if key.startswith("pass-")]


# --- Recording what passed -----------------------------------------------------

RECORDS = {area: f"pass-{area}-{'a' * 40}" for area in ci_plan.AREAS}


def planned(**found):
    return lookup(records=json.dumps(RECORDS), **found)


def test_a_green_run_names_a_record_for_every_area_it_ran():
    assert gate.passes(PLAN, listing() + [SELF], planned()) == RECORDS
    # An area the plan skipped ran nothing: its record already exists.
    jobs = skip_area(listing(), "fluid") + [SELF]
    found = gate.passes(PLAN, jobs, planned(fluid="true"))
    assert found == {area: record for area, record in RECORDS.items() if area != "fluid"}
    # Without the plan's names nothing can be recorded.
    assert gate.passes(PLAN, listing(), lookup()) == {}
    assert gate.passes(PLAN, listing(), lookup(records="not json")) == {}
    assert gate.passes(PLAN, listing(), lookup(records="[]")) == {}
    assert gate.passes(PLAN, listing(), {"result": "failure"}) == {}


@pytest.mark.parametrize(("name", "lost"), [
    ("backend / backend-shard 3", {"backend"}),
    ("backend / backend-security", {"backend"}),
    ("fluid / fluid unit tests", {"fluid"}),
    ("windows / Windows desktop shard (2/3)", {"windows"}),
    ("javascript / Frontend and mobile production build", {"javascript-checks"}),
    ("javascript / Frontend tests (3/4)", {"javascript-checks"}),
    ("android / Build and test Android app", {"android"}),
    ("android / Android emulator verification (API 34)", {"android"}),
    ("android-minified / Minified releaseSmoke (API 36)", {"android-minified"}),
    # Not part of any record: these have no files of their own.
    ("update-reach", set()), ("supply-chain", set()),
])
@pytest.mark.parametrize("state", [("completed", "failure"), ("completed", "cancelled"), ("in_progress", None)])
def test_an_area_is_recorded_on_its_own_whatever_another_area_did(name, lost, state):
    jobs = listing() + [SELF]
    named(jobs, name)["status"], named(jobs, name)["conclusion"] = state
    assert gate.passes(PLAN, jobs, planned()) == {area: record for area, record in RECORDS.items() if area not in lost}


def test_a_failed_journey_does_not_lose_the_record_of_the_other_javascript_checks():
    """The journeys keep their own record (scripts/ci/proven.py)."""
    jobs = listing() + [SELF]
    named(jobs, SHARDS[3])["conclusion"] = "failure"
    assert gate.passes(PLAN, jobs, planned()) == RECORDS
    # Still running, the JavaScript workflow is not finished: nothing of it is recorded yet.
    named(jobs, SHARDS[3])["status"], named(jobs, SHARDS[3])["conclusion"] = "in_progress", None
    assert gate.passes(PLAN, jobs, planned()) == {
        area: record for area, record in RECORDS.items() if area != "javascript-checks"}


def test_an_area_with_a_job_not_listed_yet_is_not_recorded():
    """A job waiting on another is listed only once that one finished."""
    for name, area in (("android / Android emulator verification (API 34)", "android"),
                       ("fluid / delegated tool-output contract (2.1.308)", "fluid"),
                       ("windows / Windows desktop", "windows"),
                       ("javascript / JavaScript package checks", "javascript-checks")):
        jobs = [job for job in listing() if job["name"] != name]
        assert area not in gate.passes(PLAN, jobs, planned())
    # A whole area whose call has not started lists nothing.
    jobs = [job for job in listing() if not job["name"].startswith("container / ")]
    assert "container" not in gate.passes(PLAN, jobs, planned())


def test_the_emulator_its_own_workflow_lets_fail_does_not_lose_the_record():
    advisory = "android / Android emulator verification (API 36)"
    assert gate.TOLERATED == {advisory}
    matrix = workflow("android.yml")["jobs"]["android-ui"]["strategy"]["matrix"]["include"]
    assert [item["api"] for item in matrix if item["advisory"] == "true"] == ["36"]
    assert workflow("android.yml")["jobs"]["android-ui"]["continue-on-error"] == "${{ matrix.advisory }}"
    jobs = listing() + [SELF]
    named(jobs, advisory)["conclusion"] = "failure"
    assert gate.passes(PLAN, jobs, planned()) == RECORDS
    gate.verdict(PLAN, jobs, planned())
    # Still running, it holds both back.
    named(jobs, advisory)["status"], named(jobs, advisory)["conclusion"] = "in_progress", None
    assert "android" not in gate.passes(PLAN, jobs, planned())
    with pytest.raises(ValueError):
        gate.verdict(PLAN, jobs, planned())


def test_the_javascript_checks_are_recorded_only_when_every_one_of_them_ran_and_passed():
    jobs = listing() + [SELF]
    for job in jobs:
        if job["name"].startswith("javascript / ") and "UI journeys" not in job["name"] \
                and job["name"] not in ("javascript / Select UI journeys", "javascript / JavaScript package checks",
                                        "javascript / record-journeys"):
            job["conclusion"] = "skipped"
    assert "javascript-checks" not in gate.passes(PLAN, jobs, planned())


def test_main_names_the_records_only_for_this_repositorys_own_runs(tmp_path):
    output = tmp_path / "output"
    environ = {"RESULTS": json.dumps({gate.LOOKUP: planned()}), "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7",
               "GITHUB_RUN_ATTEMPT": "1", "GH_TOKEN": "t", "GITHUB_OUTPUT": str(output),
               "GITHUB_EVENT_NAME": "pull_request", "RECORD": "true"}

    def written(jobs, **changes):
        output.write_text("")
        run = Run(jobs)
        code = gate.main({**environ, **changes}, call=run, sleep=run.sleep, out=io.StringIO())
        lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return code, {key[5:]: value for key, value in lines.items() if key.startswith("pass-")}, run

    # update-reach runs on main only.
    green = [job if job["name"] != "update-reach" else {**job, "conclusion": "skipped"} for job in listing()] + [SELF]
    assert written(green)[:2] == (0, RECORDS)
    # A fork's pull request records nothing: its records would never count.
    assert written(green, RECORD="false")[:2] == (0, {})
    # A pull request's run is watched to its end after a failure: every failure
    # shows in one round and the areas that passed are recorded.
    red = copy.deepcopy(green)
    named(red, "fluid / fluid unit tests")["conclusion"] = "failure"
    code, found, run = written(red)
    assert code == 1 and not run.cancelled
    assert found == {area: record for area, record in RECORDS.items() if area != "fluid"}
    # On main the first failure still stops the run; what had finished is recorded.
    with pytest.raises(SystemExit):
        written(red, GITHUB_EVENT_NAME="push")
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["pass-backend"] == RECORDS["backend"] and "pass-fluid" not in lines


# --- Every matrix member ------------------------------------------------------

def test_static_matrix_members_come_from_the_workflows():
    members = {(slot.area, slot.key): slot.members for slot in PLAN.slots if slot.matrix}
    frontend = workflow("ci-frontend.yml")["jobs"]["frontend"]
    assert members[("javascript", "frontend")] == [
        "javascript / " + frontend["name"].replace("${{ matrix.shard }}", value)
        for value in frontend["strategy"]["matrix"]["shard"]]
    assert members[("identity", "identity")] == ["identity / Instance identity (windows-latest)"]
    assert members[("javascript", "journeys")] is None  # chosen by the selection job
    for slot in PLAN.slots:
        if slot.matrix and slot.members is not None and slot.key not in ("suite",):
            listed = {job["name"] for job in FIXTURE}
            assert set(slot.members) <= listed, slot.full


@pytest.mark.parametrize("name", ["javascript / Frontend tests (4/4)", "fluid / delegated tool-output contract (2.1.308)",
                                  "browser-workspace / Browser workspace (panel-demand)",
                                  "windows / Windows desktop shard (2/3)", "backend / backend-shard 3"])
def test_one_absent_member_of_a_static_matrix_is_waited_for(name):
    jobs = [job for job in listing() if job["name"] != name]
    assert gate.missing(PLAN, jobs) == [name]
    with pytest.raises(ValueError):
        gate.verdict(PLAN, jobs, lookup())


def test_a_partial_journey_matrix_is_waited_for_and_a_late_failing_member_is_read():
    # Only the first shard listed, seen twice: not complete, so the gate keeps reading.
    first = [job for job in listing() if job["name"] not in SHARDS[1:]] + [SELF]
    assert gate.missing(PLAN, first) == SHARDS[1:]
    late = listing() + [SELF]
    named(late, SHARDS[4])["conclusion"] = "failure"
    run = Run(first, first, late)
    with pytest.raises(SystemExit):
        watch(run)
    assert run.cancelled
    with pytest.raises(ValueError):
        gate.verdict(PLAN, first, lookup())


def test_the_selection_decides_which_journey_shards_are_expected():
    chosen = [int(name.split("(")[1].split("/")[0]) for name in (SHARDS[1], SHARDS[4])]
    jobs = selection([job for job in listing() if job["name"] not in SHARDS or job["name"] in (SHARDS[1], SHARDS[4])],
                     chosen)
    assert gate.missing(PLAN, jobs) == []
    gate.verdict(PLAN, jobs, lookup())
    # One chosen shard absent: missing.
    jobs = [job for job in jobs if job["name"] != SHARDS[4]]
    assert gate.missing(PLAN, jobs) == [SHARDS[4]]


@pytest.mark.parametrize("change", ["no step", "not json", "not a list", "two steps", "selection failed",
                                    "selection running"])
def test_an_unreadable_selection_is_never_complete(change):
    jobs = listing()
    if change == "no step":
        selection(jobs, None)
    elif change == "not json":
        named(jobs, "javascript / Select UI journeys")["steps"][-1]["name"] += "]"
    elif change == "not a list":
        selection(jobs, {"1": 1})
    elif change == "two steps":
        plan = named(jobs, "javascript / Select UI journeys")
        plan["steps"].append(dict(plan["steps"][-1]))
    elif change == "selection failed":
        named(jobs, "javascript / Select UI journeys")["conclusion"] = "failure"
    else:
        named(jobs, "javascript / Select UI journeys").update(status="in_progress", conclusion=None)
    assert f"{JOURNEYS_TEMPLATE} (its selection is not readable yet)" in gate.missing(PLAN, jobs)
    with pytest.raises(ValueError):
        gate.verdict(PLAN, jobs, lookup())
    if change in ("no step", "not json", "not a list", "two steps"):
        # Every job finished and the choice still cannot be read: fail, do not hang.
        run = Run(jobs + [SELF])
        with pytest.raises(SystemExit):
            watch(run)
        assert not run.cancelled


def test_the_gate_expects_exactly_the_journey_shards_the_workflow_names():
    # The shard count lives in the planner and in the selection command; the
    # gate derives the shard names from the job's name template. All three and
    # the recorded listing must agree, or main waits for shards that never appear.
    hosted = int(re.search(r"^HOSTED_SHARDS = (\d+)$", (ROOT / "scripts/devloop/ci.py").read_text(),
                           re.MULTILINE)[1])
    jobs = workflow("ci-frontend.yml")["jobs"]
    select = next(step for step in jobs["journey-plan"]["steps"] if step.get("id") == "select")
    assert f"--shards {hosted} " in select["run"]
    assert jobs["journeys"]["name"] == f"UI journeys (${{{{ matrix.shard }}}}/{hosted})"
    expected = [f"javascript / UI journeys ({shard}/{hosted})" for shard in range(1, hosted + 1)]
    assert SHARDS == expected
    without = [job for job in listing() if job["name"] not in SHARDS]
    assert gate.missing(PLAN, selection(without, list(range(1, hosted + 1)))) == expected
    # Shards named for another count are not the selected shards: every real one stays missing.
    stale = [{"id": 800 + shard, "name": f"javascript / UI journeys ({shard}/16)", "status": "completed",
              "conclusion": "success"} for shard in range(1, 17)]
    assert gate.missing(PLAN, selection(without + stale, list(range(1, hosted + 1)))) == expected


def test_the_selection_job_names_its_choice_in_a_step():
    _, chooser, prefix = gate.SELECTED[("javascript", "journeys")]
    jobs = workflow("ci-frontend.yml")["jobs"]
    steps = jobs[chooser]["steps"]
    select = next(index for index, step in enumerate(steps) if step.get("id") == "select")
    named_step = [step for step in steps if step.get("name", "").startswith(prefix)]
    assert named_step == [steps[select + 1]]
    assert named_step[0]["name"] == prefix + "${{ steps.select.outputs.shards }}"
    assert jobs["journeys"]["strategy"]["matrix"]["shard"] == gate.SELECTED[("javascript", "journeys")][0]


def test_a_matrix_the_gate_cannot_read_fails_the_plan(tmp_path):
    source = ROOT / ".github/workflows"
    for case, old, new in (
            ("computed", "inputs.main_suite == true) && '[\"windows-latest\"]'",
             "inputs.main_suite == true) && '[\"windows-latest\", \"ubuntu-24.04\"]'"),
            ("two keys", "chrome-mode: [external, panel-demand, direct-page-demand]",
             "chrome-mode: [external, panel-demand, direct-page-demand]\n        size: [small, large]")):
        target = tmp_path / case
        target.mkdir()
        for path in source.glob("*.yml"):
            (target / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        changed = [path for path in target.glob("*.yml") if old in path.read_text(encoding="utf-8")]
        assert len(changed) == 1, case
        changed[0].write_text(changed[0].read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        with pytest.raises(ValueError):
            gate.Plan(target)


# --- Re-runs ------------------------------------------------------------------

def copied(jobs):
    """A re-run attempt lists each job it does not run again as a completed copy with a new id."""
    return [{**job, "id": job["id"] + 10_000} for job in jobs]


def test_a_rerun_of_only_the_gate_judges_the_copied_jobs():
    attempt = copied(listing()) + [SELF]
    run = Run(attempt)
    jobs, _ = watch(run)
    assert all("/attempts/2/jobs" in path for method, path in run.calls)
    assert run.slept == [gate.CONFIRM_SECONDS] and not run.cancelled
    gate.verdict(PLAN, jobs, lookup())


def test_a_rerun_of_failed_jobs_waits_for_them_and_what_they_start():
    rest = [job for job in copied(listing()) if job["name"] not in ("backend / backend-shard 2", "backend / Backend tests")]
    shard = {"id": 50, "name": "backend / backend-shard 2", "status": "queued", "conclusion": None}
    record = {"id": 51, "name": "backend / Backend tests", "status": "completed", "conclusion": "skipped"}
    queued = rest + [shard, SELF]
    passed = rest + [{**shard, "status": "completed", "conclusion": "success"}, SELF]
    run = Run(queued, passed, passed + [record])
    jobs, _ = watch(run)
    # Not done while the shard ran, nor once it passed but the job after it was not listed yet.
    assert run.slept == [gate.POLL_SECONDS, gate.POLL_SECONDS, gate.CONFIRM_SECONDS]
    gate.verdict(PLAN, jobs, lookup())
    # The re-run shard failing again stops the run, whatever the first attempt said.
    again = rest + [{**shard, "status": "completed", "conclusion": "failure"}, SELF]
    run = Run(queued, again)
    with pytest.raises(SystemExit):
        watch(run)
    assert run.cancelled


# --- Listing and reporting ----------------------------------------------------

def test_a_failure_on_the_second_page_is_read():
    jobs = listing() + [{"id": 5000 + n, "name": f"desktop / Typecheck and unit tests", "status": "completed",
                         "conclusion": "success"} for n in range(60)] + [SELF]
    named(jobs, "update-reach")["conclusion"] = "failure"
    jobs.append(jobs.pop(jobs.index(named(jobs, "update-reach"))))
    assert len(jobs) > 100

    calls = []

    def paged(method, path, token):
        calls.append((method, path))
        if method == "POST":
            return {}
        page = int(path.rsplit("page=", 1)[1])
        return {"total_count": len(jobs), "jobs": jobs[(page - 1) * 100:page * 100]}

    assert len(gate.jobs_of("o/r", 7, 1, "t", paged)) == len(jobs)
    with pytest.raises(SystemExit):
        gate.watch(PLAN, "o/r", 7, 1, "t", call=paged, sleep=lambda s: None, out=io.StringIO(),
                   clock=lambda: 0)
    assert ("POST", "/repos/o/r/actions/runs/7/cancel") in calls
    assert any(path.endswith("page=2") for method, path in calls)


def test_reporting_waits_for_cancelled_shards_to_upload():
    # A shard fails; another is still running its upload of a recovered case.
    failing = running()
    named(failing, SHARDS[4]).update(status="completed", conclusion="failure")
    uploading = copy.deepcopy(failing)
    for job in uploading:
        if job["status"] != "completed" and job["name"] != "CI":
            job.update(status="completed", conclusion="cancelled")
    run = Run(failing, failing, uploading)
    out = io.StringIO()
    ticks = iter(range(0, 10**6, gate.POLL_SECONDS))
    environ = {"RESULTS": "{}", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1",
               "GH_TOKEN": "t"}
    assert gate.main(environ, call=run, sleep=run.sleep, out=out, argv=["settle"], clock=lambda: next(ticks)) == 0
    # Read until the uploading shard stopped, never cancelling anything itself.
    assert len([call for call in run.calls if call[0] == "GET"]) == 3 and not run.cancelled
    # It gives up (with a warning) rather than holding the job forever.
    run = Run(failing)
    ticks = iter(range(0, 10**6, gate.POLL_SECONDS))
    assert not gate.settle(PLAN, "o/r", 7, 1, "t", run, run.sleep, lambda: next(ticks), out)
    assert 0 < len(run.slept) <= gate.SETTLE_SECONDS // gate.POLL_SECONDS
    assert "had not stopped" in out.getvalue()


def test_the_wait_runs_in_its_own_step_before_reporting():
    steps = workflow("ci.yml")["jobs"]["ci"]["steps"]
    names = [step.get("name") for step in steps]
    settle = steps[names.index("Wait for cancelled jobs to stop")]
    report = steps[names.index("Open or update flaky journey issues")]
    assert names.index("Wait for cancelled jobs to stop") == names.index("Open or update flaky journey issues") - 1
    assert settle["run"] == "python scripts/ci/main_gate.py settle"
    assert settle["if"] == report["if"] and settle["if"].startswith("always() && ")
    assert settle["continue-on-error"] == "true"
    assert int(settle["timeout-minutes"]) * 60 > gate.SETTLE_SECONDS + gate.POLL_SECONDS


def test_waiting_and_reporting_fit_before_github_stops_the_cancelled_gate():
    # GitHub force-stops a cancelled job 5 minutes after the cancel; both
    # steps, at their step limits, must end well before that (a minute spare).
    assert gate.CANCELLED_JOB_LIMIT_SECONDS == 300
    steps = {step.get("name"): step for step in workflow("ci.yml")["jobs"]["ci"]["steps"]}
    settle = int(steps["Wait for cancelled jobs to stop"]["timeout-minutes"]) * 60
    report = int(steps["Open or update flaky journey issues"]["timeout-minutes"]) * 60
    assert settle + report <= gate.CANCELLED_JOB_LIMIT_SECONDS - 60
    assert gate.SETTLE_SECONDS + gate.POLL_SECONDS < settle
