"""The Main suite gate judges the run from its start, so its verdict never queues."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("main_gate", ROOT / "scripts/ci/main_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PLAN = gate.Plan()
FIXTURE = json.loads((ROOT / "tests/scripts/fixtures/main_suite_jobs.json").read_text())["jobs"]
ALL_PROVEN = ("backend", "fluid", "windows", "browser-workspace", "container", "identity", "desktop-source",
              "javascript-checks")
SELF = {"id": 999, "name": "Main suite", "status": "in_progress", "conclusion": None}


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
    jobs = workflow("main-suite.yml")["jobs"]
    # The verdict jobs main-suite skips are the pull-request required checks;
    # their names stay as they are, and only main-suite asks them to step aside.
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
    assert PLAN.slot_of("javascript / UI journeys (7/16)").key == "journeys"
    assert PLAN.slot_of("backend / record / Record the pass").key == "record"
    assert PLAN.is_advisory("source-ui / build") and PLAN.is_advisory("Find what a pull request already proved")
    for area in gate.REQUIRED:
        assert not PLAN.is_advisory(area) and not PLAN.is_advisory(f"{area} / anything")


def test_a_job_github_has_not_created_yet_is_waited_for():
    jobs = [job for job in listing() if "UI journeys (" not in job["name"]]
    assert gate.missing(PLAN, jobs) == ["javascript / UI journeys (${{ matrix.shard }}/16)"]
    # The whole called workflow not listed yet (its call waits on the lookup).
    jobs = [job for job in listing() if not job["name"].startswith("fluid / ")]
    assert "fluid / fluid unit tests" in gate.missing(PLAN, jobs)


# --- Verdict rules -----------------------------------------------------------

@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure", None])
def test_any_required_job_that_did_not_pass_fails_the_verdict(conclusion):
    for name in ("backend / backend-shard 3", "javascript / UI journeys (5/16)", "update-reach",
                 "windows / Selected tests on Windows", "desktop / Typecheck and unit tests"):
        jobs = listing()
        named(jobs, name)["conclusion"] = conclusion
        with pytest.raises(ValueError):
            gate.verdict(PLAN, jobs, lookup())


def test_advisory_jobs_never_decide():
    jobs = listing()
    named(jobs, "source-ui / build")["conclusion"] = "failure"
    named(jobs, "Find what a pull request already proved")["conclusion"] = "failure"
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
    jobs.append({"id": 800, "name": "javascript / UI journeys (${{ matrix.shard }}/16)", "status": "completed",
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
        needs = workflow(workflow("main-suite.yml")["jobs"][area]["uses"].rsplit("/", 1)[1])["jobs"][key]["needs"]
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

    def __init__(self, *listings, errors=0):
        self.listings, self.errors, self.calls, self.slept = list(listings), errors, [], []

    def __call__(self, method, path, token):
        self.calls.append((method, path))
        if method == "POST":
            return {}
        if self.errors:
            self.errors -= 1
            raise urllib.error.URLError("unreachable")
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
    named(late, "javascript / UI journeys (5/16)").update(status="completed", conclusion="failure")
    run = Run(running(), late)
    summary = tmp_path / "summary.md"
    with pytest.raises(SystemExit) as stopped:
        watch(run, summary=str(summary))
    assert stopped.value.code == 1
    assert run.cancelled
    assert "javascript / UI journeys (5/16)" in summary.read_text()


def test_a_cancelled_run_is_never_green():
    cancelled = running()
    for job in cancelled:
        if job["status"] != "completed" and job["name"] != "Main suite":
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
    assert not run.cancelled and all(job["name"] != "Main suite" for job in jobs)


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


def test_an_unreadable_run_is_not_proven():
    run = Run(listing(), errors=gate.MAX_FAILED_READS)
    with pytest.raises(SystemExit):
        watch(run)
    # A few failed reads are retried.
    run = Run(listing() + [SELF], errors=3)
    watch(run)


def test_main_writes_the_evidence_flags(tmp_path):
    output = tmp_path / "output"
    run = Run(listing() + [SELF])
    environ = {"RESULTS": json.dumps({"proven": lookup()}), "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7",
               "GITHUB_RUN_ATTEMPT": "1", "GH_TOKEN": "t", "GITHUB_OUTPUT": str(output)}
    assert gate.main(environ, call=run, sleep=run.sleep, out=io.StringIO()) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["backend"] == lines["windows"] == "true"
    assert json.loads(lines["backend-needs"])["backend-shard"] == {"result": "success"}
    # An unproven skip fails, with no evidence flags written.
    output.unlink()
    run = Run(skip_area(listing(), "fluid") + [SELF])
    assert gate.main(environ, call=run, sleep=run.sleep, out=io.StringIO()) == 1
    assert not output.exists()
