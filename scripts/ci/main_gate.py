"""The `CI` verdict, given the moment the last required job finishes.

Every job that starts after other jobs finish waits for a free runner again, and
runners are shared with every open pull request. The old closing chain (each
called workflow's verdict job, then the reporter, then the gate) could wait 15
minutes to run 20 seconds of checks. This gate starts with the run instead,
keeps its runner, and reads the run's own jobs through the Actions API:

- It knows which jobs to expect from the checked-in workflow files, so a job
  GitHub has not created yet (a matrix chosen by an earlier job) is waited
  for, never mistaken for "nothing left".
- On main the first required job that fails, is cancelled or times out ends
  the run: the gate names it, cancels the run (it can no longer publish) and
  fails. A pull request's run goes on to the end, so one round shows every
  failure and every area that did pass is recorded.
- When every expected job has finished it applies `main_suite.aggregate` for
  the areas, with a skip accepted only where the plan found a recorded pass,
  and the rules of the called workflows' own verdict jobs, which do not run
  when ci.yml calls them.
- It names the areas that passed in this run (`pass-<area>` outputs), so the
  workflow can record them for later runs (scripts/ci/plan.py), also when
  another area failed.

It never judges its own job, and a run it cannot read is a failure, not a pass.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ci import plan as ci_plan  # noqa: E402
from ci.main_suite import LOOKUP, REQUIRED, aggregate  # noqa: E402

API = "https://api.github.com"
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = "ci.yml"
GATE_KEY = "ci"
GATE = ci_plan.GATE
# Jobs that cannot fail the run: the optional UI bundle cache and the
# pass-record lookup (a failed lookup only means every area runs).
ADVISORY = ("source-ui", LOOKUP)
# Matrix members whose own workflow lets them fail (`continue-on-error`).
# GitHub lists such a job as failed although the run goes on; here it counts
# as passed once it has finished.
TOLERATED = frozenset(name for area in ci_plan.AREAS.values() for name in area.get("advisory", ()))
# Called workflows whose own verdict job ci.yml skips (input
# `gated_by_caller`), mapped to that job. This gate applies its rules instead.
CALLER_GATED = {
    "backend": "backend",
    "javascript": "javascript",
    "windows": "desktop-gate",
    "browser-workspace": "browser-workspace",
}
PASSED = ("success", "skipped")
POLL_SECONDS = 20
CONFIRM_SECONDS = 5
# Every listed job finished yet an expected one never appeared: fail, naming it.
STALL_SECONDS = 300
# Consecutive unreadable polls before the gate gives up (about ten minutes).
MAX_FAILED_READS = 30
# After cancelling, how long to wait for the cancelled jobs to stop. GitHub
# force-stops a cancelled job 5 minutes after the cancel, the gate included:
# this wait and the reporting step after it must both fit well inside that.
SETTLE_SECONDS = 90
CANCELLED_JOB_LIMIT_SECONDS = 300
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def request(method, path, token, urlopen=urllib.request.urlopen):
    call = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(call, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def jobs_of(repository, run, attempt, token, call=request):
    found, page = [], 1
    while True:
        data = call("GET", f"/repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}", token)
        batch = data.get("jobs")
        if not isinstance(batch, list):
            raise ValueError("unexpected jobs response")
        found.extend(batch)
        if not batch or len(found) >= data.get("total_count", 0):
            return found
        page += 1


def _load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _name_pattern(template, matrix):
    """A regular expression for the names GitHub gives a job's runs.

    A name with expressions matches any value there, the literal template
    included (a skipped matrix job keeps it). A matrix job whose name has none
    gets its values appended in brackets.
    """
    if "${{" in template:
        return "(?:.+)".join(re.escape(part) for part in EXPRESSION.split(template))
    return re.escape(template) + (r"(?: \(.+\))?" if matrix else "")


class Slot:
    """One job definition of the run, and the listed jobs GitHub made of it."""

    def __init__(self, area, key, template, matrix=None, nested=False, values=None):
        self.area, self.key, self.template = area, key, template
        prefix = "" if key is None else re.escape(area) + " / "
        self.regex = re.compile(prefix + _name_pattern(template, matrix) + (r"(?: / .+)?" if nested else ""))
        self.full = template if key is None else f"{area} / {template}"
        self.weight = len(EXPRESSION.sub("", template))
        self.matrix = bool(matrix)
        # Every member a matrix job must list; None when an earlier job chooses them.
        self.members = None if values is None else [self.member(value) for value in values]

    def member(self, value):
        """The name GitHub gives this matrix job's run for one value."""
        if "${{" in self.template:
            name = EXPRESSION.sub(str(value), self.template)
        else:
            name = f"{self.template} ({value})"
        return name if self.key is None else f"{self.area} / {name}"


MATRIX_KEY = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


def _matrix_values(area, key, job, inputs):
    """The values of a one-key matrix, or None when a job of this run chooses them.

    A matrix given as an `include` list is read through the one matrix key the
    job's name uses. A shape this cannot read fails here, so the gate is never
    left to guess which members exist.
    """
    matrix = _matrix(job)
    if not matrix:
        return None
    if not isinstance(matrix, dict) or len(matrix) != 1:
        raise ValueError(f"{area} / {key}: only a one-key matrix or an include list is understood")
    (values,) = matrix.values()
    if "include" in matrix:
        template = job.get("name") or ""
        named = MATRIX_KEY.findall(template)
        if (len(named) != 1 or len(EXPRESSION.findall(template)) != 1 or not isinstance(values, list)
                or not all(isinstance(entry, dict) and named[0] in entry for entry in values)):
            raise ValueError(f"{area} / {key}: an include matrix must be named by one of its keys")
        return [entry[named[0]] for entry in values]
    if isinstance(values, list):
        return values
    if (area, key) in RESOLVED:
        expression, resolve = RESOLVED[(area, key)]
        if values != expression:
            raise ValueError(f"{area} / {key}: matrix changed; update RESOLVED in main_gate.py")
        return resolve(inputs)
    if (area, key) in SELECTED:
        if values != SELECTED[(area, key)][0]:
            raise ValueError(f"{area} / {key}: matrix changed; update SELECTED in main_gate.py")
        return None
    raise ValueError(f"{area} / {key}: a computed matrix main_gate.py does not know")


# Computed matrices whose members follow from ci.yml's inputs alone.
RESOLVED = {
    ("identity", "identity"): (
        "${{ fromJSON((github.event_name == 'pull_request' || inputs.main_suite == true) && "
        "'[\"windows-latest\"]' || '[\"windows-latest\", \"macos-latest\"]') }}",
        lambda inputs: ["windows-latest"] if inputs.get("main_suite") is True else ["windows-latest", "macos-latest"],
    ),
}
# Matrices an earlier job of the run chooses: (matrix, choosing job, step-name prefix).
# The choosing job names a step after its choice, so the run's job list holds it.
SELECTED = {
    ("javascript", "journeys"): (
        "${{ fromJSON(needs.journey-plan.outputs.shards) }}", "journey-plan", "Selected UI journey shards: "),
}


class Plan:
    """What ci.yml and the workflows it calls define, read from the checkout."""

    def __init__(self, workflows=WORKFLOWS):
        workflows = Path(workflows)
        main = _load(workflows / WORKFLOW)["jobs"]
        self.reusable, self.slots, self.advisory, self.called = set(), [], [], {}
        for area, job in main.items():
            if area == GATE_KEY:
                continue
            uses = job.get("uses", "")
            if area in ADVISORY:
                self.advisory.append(Slot(area, None, area if uses else job.get("name") or area,
                                          nested=bool(uses)))
                continue
            if not uses:
                self.slots.append(Slot(area, None, job.get("name") or area, _matrix(job),
                                       values=_matrix_values(area, None, job, {})))
                continue
            if not uses.startswith("./.github/workflows/"):
                raise ValueError(f"{area} calls a workflow outside this repository")
            self.reusable.add(area)
            called = _load(workflows / uses.rsplit("/", 1)[1])["jobs"]
            self.called[area] = called
            inputs = job.get("with") or {}
            for key, inner in called.items():
                self.slots.append(Slot(area, key, inner.get("name") or key, _matrix(inner),
                                       nested="uses" in inner, values=_matrix_values(area, key, inner, inputs)))
        self.areas = [area for area in main if area != GATE_KEY and area not in ADVISORY]

    def is_advisory(self, name):
        return any(slot.regex.fullmatch(name) for slot in self.advisory)

    def slot_of(self, name):
        """The one definition a listed name belongs to: exact template first, then the most specific."""
        matches = [slot for slot in self.slots if slot.regex.fullmatch(name)]
        exact = [slot for slot in matches if slot.full == name]
        if exact:
            return exact[0]
        return max(matches, key=lambda slot: slot.weight, default=None)


def _matrix(job):
    return (job.get("strategy") or {}).get("matrix")


def judged(plan, jobs):
    """The listed jobs that decide the run: not this gate and not advisory.

    A finished job its own workflow lets fail is given as passed.
    """
    found = []
    for job in jobs:
        name = job.get("name", "")
        if name == GATE or plan.is_advisory(name):
            continue
        if name in TOLERATED and job.get("status") == "completed" and job.get("conclusion") not in PASSED:
            job = {**job, "conclusion": "success"}
        found.append(job)
    return found


def failed(jobs):
    return [job for job in jobs if job.get("status") == "completed" and job.get("conclusion") not in PASSED]


def layout(plan, jobs):
    """(skipped areas, {slot: [jobs]}) for the judged jobs."""
    skipped = {job["name"] for job in jobs
               if job.get("name") in plan.reusable and job.get("conclusion") == "skipped"}
    placed = {}
    for job in jobs:
        slot = plan.slot_of(job.get("name", ""))
        if slot is not None:
            placed.setdefault(slot, []).append(job)
    return skipped, placed


def selected_members(plan, slot, placed):
    """The members an earlier job chose for this matrix, or None while unknown.

    The choosing job must have finished and succeeded, and name exactly one
    step after its choice: a JSON list of the values.
    """
    _, chooser, prefix = SELECTED[(slot.area, slot.key)]
    runs = [job for other, found in placed.items()
            if other.area == slot.area and other.key == chooser for job in found]
    if len(runs) != 1 or runs[0].get("status") != "completed" or runs[0].get("conclusion") != "success":
        return None
    names = [step.get("name", "") for step in runs[0].get("steps") or []
             if step.get("name", "").startswith(prefix)]
    if len(names) != 1:
        return None
    try:
        values = json.loads(names[0][len(prefix):])
    except ValueError:
        return None
    if not isinstance(values, list) or not all(isinstance(value, (int, str)) for value in values):
        return None
    return [slot.member(value) for value in values]


def missing(plan, jobs):
    """Expected jobs that are not listed yet, each matrix member on its own.

    A matrix job skipped as a whole is listed once under its literal template
    name. A matrix an earlier job chooses counts as missing until that job's
    choice can be read, so a run whose choice cannot be read is never complete.
    """
    skipped, placed = layout(plan, jobs)
    absent = []
    for slot in plan.slots:
        if slot.area in skipped:
            continue
        listed = {job.get("name") for job in placed.get(slot, [])}
        if not slot.matrix:
            if not listed:
                absent.append(slot.full)
            continue
        if slot.full in listed:
            continue  # the whole matrix was skipped
        members = slot.members if slot.members is not None else selected_members(plan, slot, placed)
        if members is None:
            absent.append(f"{slot.full} (its selection is not readable yet)")
            continue
        absent.extend(name for name in members if name not in listed)
    return absent


def result_of(listed):
    """The result GitHub would give `needs.<job>.result` for these runs of one job."""
    if not listed:
        return None
    conclusions = {job.get("conclusion") for job in listed}
    if conclusions == {"skipped"}:
        return "skipped"
    return "success" if conclusions <= set(PASSED) else "failure"


def area_results(plan, jobs, lookup):
    skipped, placed = layout(plan, jobs)
    results = {LOOKUP: lookup}
    for area in plan.areas:
        if area in skipped:
            results[area] = {"result": "skipped"}
        elif area in plan.reusable:
            listed = [job for slot, found in placed.items() if slot.area == area for job in found]
            results[area] = {"result": result_of(listed)}
        else:
            (slot,) = [slot for slot in plan.slots if slot.area == area]
            results[area] = {"result": result_of(placed.get(slot, []))}
    return results


def inner_needs(plan, jobs, area):
    """`needs` of the area's own verdict job, rebuilt from the listed jobs."""
    _, placed = layout(plan, jobs)
    needs = plan.called[area][CALLER_GATED[area]]["needs"]
    found = {}
    for key in [needs] if isinstance(needs, str) else needs:
        listed = [job for slot, runs in placed.items() if slot.area == area and slot.key == key for job in runs]
        if listed:
            found[key] = {"result": result_of(listed)}
    return found, needs


def check_javascript(needs, expected, checks_proven):
    """ci-frontend.yml's verdict: journeys skip only when none were selected,
    the other checks only on a pull request's record for these files."""
    if set(needs) != set(expected):
        raise ValueError("JavaScript checks: missing branch")
    plan = needs["journey-plan"]["result"]
    for name in expected:
        if name == "journeys":
            # The journeys job runs whenever the selection succeeded and chose
            # any; skipped after a successful selection means it chose none.
            allowed = {"success", "skipped"} if plan == "success" else {"success"}
        else:
            allowed = {"skipped" if name != "journey-plan" and checks_proven else "success"}
        if needs[name]["result"] not in allowed:
            raise ValueError(f"JavaScript checks: {name} {needs[name]['result']}")


def check_every_branch(area, needs, expected):
    if set(needs) != set(expected) or any(needs[name]["result"] != "success" for name in expected):
        raise ValueError(f"{area}: a required job failed, was cancelled, skipped or is missing")


def check_inner(plan, jobs, area, checks_proven):
    """Apply the rules of the area's own verdict job. Return its rebuilt `needs`."""
    needs, expected = inner_needs(plan, jobs, area)
    expected = [expected] if isinstance(expected, str) else list(expected)
    if area == "javascript":
        check_javascript(needs, expected, checks_proven)
    else:
        check_every_branch(area, needs, expected)
    return needs


def verdict(plan, jobs, lookup, event=""):
    """Raise ValueError unless the finished run passes. Return the evidence to check next."""
    jobs = judged(plan, jobs)
    bad = failed(jobs)
    if bad:
        raise ValueError("failed: " + ", ".join(job["name"] for job in bad))
    unfinished = [job["name"] for job in jobs if job.get("status") != "completed"]
    absent = missing(plan, jobs)
    if unfinished or absent:
        raise ValueError("not finished: " + ", ".join(unfinished + absent))
    results = area_results(plan, jobs, lookup)
    aggregate(results, event)
    proven = (lookup.get("outputs") or {}).get("javascript-checks") == "true"
    for area in CALLER_GATED:
        if results[area]["result"] == "success":  # else a skip the plan proved: nothing of it ran
            check_inner(plan, jobs, area, proven)
    return evidence_of(plan, jobs)


def complete(plan, jobs, area):
    """True when every job the called workflow defines is listed and finished.

    A job waiting on another is not listed until that one finishes, so a
    run read part-way can show only an area's first, passed jobs.
    """
    if any(name == area or name.startswith(f"{area} / ") for name in missing(plan, jobs)):
        return False
    return all(job.get("status") == "completed" for job in jobs if job.get("name", "").startswith(f"{area} / "))


def evidence_of(plan, jobs):
    """Which receipts the workflow has to check next: an area's, once all its jobs succeeded."""
    jobs = judged(plan, jobs)
    evidence = {"backend": "false", "windows": "false"}
    for area in ("backend", "windows"):
        try:
            if not complete(plan, jobs, area):
                continue
            needs = check_inner(plan, jobs, area, False)
        except (ValueError, KeyError):
            continue
        evidence[area] = "true"
        if area == "backend":
            evidence["backend-needs"] = json.dumps(needs, sort_keys=True)
    return evidence


def passed(plan, jobs, area):
    """True when an area of scripts/ci/plan.py passed in these judged jobs.

    An area counts on its own, whatever another area did: every job its
    workflow defines is listed and finished, none failed, at least one ran,
    and its own verdict job's rules hold. scripts/ci/plan.py applies the same
    rule to the run behind a record before it skips the area.
    """
    caller = ci_plan.AREAS[area]["jobs"][0]
    try:
        if not complete(plan, jobs, caller) or not ci_plan.area_passed(area, jobs):
            return False
        if caller == "javascript":
            needs, _ = inner_needs(plan, jobs, caller)
            return all(needs.get(name, {}).get("result") == "success"
                       for name in plan.called[caller][CALLER_GATED[caller]]["needs"]
                       if name not in ("journey-plan", "journeys"))
        if caller in CALLER_GATED:
            check_inner(plan, jobs, caller, False)
    except (ValueError, KeyError):
        return False
    return True


def passes(plan, jobs, lookup):
    """{area: record name} for the areas of scripts/ci/plan.py that passed in this run.

    An area the plan skipped ran nothing and is not recorded again.
    """
    jobs = judged(plan, jobs)
    outputs = lookup.get("outputs") or {}
    try:
        records = json.loads(outputs.get("records") or "{}")
    except ValueError:
        return {}
    found = {}
    for area in ci_plan.AREAS:
        record = records.get(area) if isinstance(records, dict) else None
        if isinstance(record, str) and record and outputs.get(area) != "true" and passed(plan, jobs, area):
            found[area] = record
    return found


def report(bad, out=sys.stdout, summary_path=None, stopped=True):
    lines = [f"- {job['name']}: {job.get('conclusion')} {job.get('html_url', '')}".rstrip() for job in bad]
    title = "CI stopped early" if stopped else "CI failed"
    print(f"::error title={title}::" + "; ".join(job["name"] for job in bad), file=out)
    print("\n".join(lines), file=out, flush=True)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            if stopped:
                handle.write("### Stopped on the first failure\n\nThese jobs did not pass, so the rest of the run was cancelled:\n\n")
            else:
                handle.write("### Failed\n\nThese jobs did not pass. Areas that did pass are recorded and will not run again on the same files:\n\n")
            handle.write("\n".join(lines) + "\n")


def settle(plan, repository, run, attempt, token, call, sleep, clock, out):
    """Wait (bounded) for every judged job to stop. Its own step, after the verdict.

    Cancelled jobs still run their `always()` steps: a journey shard uploads
    the receipt of a case it recovered. The reporting step reads those
    artifacts once, so it must not start while a shard is still uploading. The
    cancel also stops the verdict step itself, which is why this is a separate
    `always()` step. After a verdict every job has finished: it returns at once.
    """
    started = clock()
    while clock() - started < SETTLE_SECONDS:
        try:
            jobs = judged(plan, jobs_of(repository, run, attempt, token, call))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            print(f"could not read the run's jobs ({error})", file=out, flush=True)
        else:
            if all(job.get("status") == "completed" for job in jobs):
                return True
        sleep(POLL_SECONDS)
    print("::warning title=CI::Some cancelled jobs had not stopped; journey reporting may miss them.",
          file=out, flush=True)
    return False


def watch(plan, repository, run, attempt, token, call=request, sleep=time.sleep, clock=time.monotonic,
          out=sys.stdout, summary_path=None, stop_early=True, seen=None):
    """Poll until the run is decided. Return the judged jobs, or raise SystemExit(1).

    With ``stop_early`` the first failure cancels the run. Without it the run
    is watched to its end and the jobs are returned for the verdict to fail.
    ``seen`` receives the jobs of the last readable poll, also on an early exit.

    Only the current attempt is read. "Re-run failed jobs" or "Re-run job"
    lists every job it does not run again in the new attempt, as a completed
    copy (checked on real runs), so the attempt is the whole run. Reading an
    older attempt instead could let an old success stand for a job the new
    attempt has not created yet; a job that never appears fails the gate.
    """
    unreadable, stalled_since, confirming = 0, None, None
    while True:
        try:
            jobs = judged(plan, jobs_of(repository, run, attempt, token, call))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            unreadable += 1
            print(f"could not read the run's jobs ({error})", file=out, flush=True)
            if unreadable >= MAX_FAILED_READS:
                print("::error title=CI::The run's jobs could not be read; it is not proven.", file=out)
                raise SystemExit(1)
            sleep(POLL_SECONDS)
            continue
        unreadable = 0
        if seen is not None:
            seen[:] = jobs
        bad = failed(jobs)
        if bad and stop_early:
            report(bad, out, summary_path)
            try:
                call("POST", f"/repos/{repository}/actions/runs/{run}/cancel", token)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
                # A run already cancelling answers 409; it fails either way.
                print(f"could not cancel the run ({error})", file=out, flush=True)
            raise SystemExit(1)
        if all(job.get("status") == "completed" for job in jobs):
            absent = missing(plan, jobs)
            if not absent:
                # Seen twice, a few seconds apart, with the same jobs: nothing is
                # still being created from a job that just finished.
                current = sorted(job.get("id", 0) for job in jobs)
                if confirming == current:
                    return jobs
                confirming = current
                sleep(CONFIRM_SECONDS)
                continue
            stalled_since = clock() if stalled_since is None else stalled_since
            if clock() - stalled_since >= STALL_SECONDS:
                print("::error title=CI::Expected jobs never appeared: " + ", ".join(absent), file=out)
                raise SystemExit(1)
        else:
            stalled_since = None
        confirming = None
        sleep(POLL_SECONDS)


def write_outputs(environ, values):
    if environ.get("GITHUB_OUTPUT"):
        with open(environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main(environ=os.environ, call=request, sleep=time.sleep, out=sys.stdout, argv=None, clock=time.monotonic):
    plan = Plan()
    argv = argv or []
    if argv == ["settle"]:
        settle(plan, environ["GITHUB_REPOSITORY"], environ["GITHUB_RUN_ID"], environ.get("GITHUB_RUN_ATTEMPT", "1"),
               environ["GH_TOKEN"], call, sleep, clock, out)
        return 0
    if argv:
        print("usage: main_gate.py [settle]", file=out)
        return 2
    lookup = json.loads(environ["RESULTS"]).get(LOOKUP) or {}
    event = environ.get("GITHUB_EVENT_NAME", "")
    seen = []
    try:
        # A pull request's run goes on after a failure: one round shows every
        # failure, and the areas that pass are recorded.
        jobs = watch(plan, environ["GITHUB_REPOSITORY"], environ["GITHUB_RUN_ID"],
                     environ.get("GITHUB_RUN_ATTEMPT", "1"), environ["GH_TOKEN"], call=call, sleep=sleep, out=out,
                     summary_path=environ.get("GITHUB_STEP_SUMMARY"), stop_early=event != "pull_request", seen=seen)
    finally:
        # Also after an early stop: what did pass need not run again. RECORD is
        # "true" except for a fork's pull request, whose records never count.
        try:
            values = dict(evidence_of(plan, seen))
            if environ.get("RECORD") == "true":
                values.update({f"pass-{area}": record for area, record in passes(plan, seen, lookup).items()})
            write_outputs(environ, values)
        except Exception as error:  # noqa: BLE001 - recording is a shortcut, never the verdict
            print(f"could not name the areas that passed ({error})", file=out, flush=True)
    try:
        verdict(plan, jobs, lookup, event)
    except ValueError as error:
        bad = failed(judged(plan, jobs))
        if bad:
            report(bad, out, environ.get("GITHUB_STEP_SUMMARY"), stopped=False)
        print(f"::error title=CI::{error}", file=out)
        return 1
    print("Every required area succeeded, or already passed on exactly the files it reads.", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(argv=sys.argv[1:]))
