"""The `Main suite` verdict, given the moment the last required job finishes.

Every job that starts after other jobs finish waits for a free runner again, and
runners are shared with every open pull request. The old closing chain (each
called workflow's verdict job, then the reporter, then the gate) could wait 15
minutes to run 20 seconds of checks. This gate starts with the run instead,
keeps its runner, and reads the run's own jobs through the Actions API:

- It knows which jobs to expect from the checked-in workflow files, so a job
  GitHub has not created yet (a matrix chosen by an earlier job) is waited
  for, never mistaken for "nothing left".
- The first required job that fails, is cancelled or times out ends the run:
  the gate names it, cancels the run (it can no longer publish) and fails.
- When every expected job has finished it applies the same rules as before:
  `main_suite.aggregate` for the areas, with a skip accepted only where the
  pass-record lookup answered "true", and the rules of the called workflows'
  own verdict jobs, which do not run when main-suite calls them.

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
from ci.main_suite import LOOKUP, REQUIRED, aggregate  # noqa: E402

API = "https://api.github.com"
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_KEY = "main-suite"
GATE = "Main suite"
# Jobs that cannot fail the run: the optional UI bundle cache and the
# pass-record lookup (a failed lookup only means every area runs).
ADVISORY = ("source-ui", LOOKUP)
# Called workflows whose own verdict job main-suite skips (input
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

    def __init__(self, area, key, template, matrix=False, nested=False):
        self.area, self.key, self.template = area, key, template
        prefix = "" if key is None else re.escape(area) + " / "
        self.regex = re.compile(prefix + _name_pattern(template, matrix) + (r"(?: / .+)?" if nested else ""))
        self.full = template if key is None else f"{area} / {template}"
        self.weight = len(EXPRESSION.sub("", template))


class Plan:
    """What main-suite.yml and the workflows it calls define, read from the checkout."""

    def __init__(self, workflows=WORKFLOWS):
        workflows = Path(workflows)
        main = _load(workflows / "main-suite.yml")["jobs"]
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
                self.slots.append(Slot(area, None, job.get("name") or area, bool(_matrix(job))))
                continue
            if not uses.startswith("./.github/workflows/"):
                raise ValueError(f"{area} calls a workflow outside this repository")
            self.reusable.add(area)
            called = _load(workflows / uses.rsplit("/", 1)[1])["jobs"]
            self.called[area] = called
            for key, inner in called.items():
                self.slots.append(Slot(area, key, inner.get("name") or key, bool(_matrix(inner)),
                                       nested="uses" in inner))
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
    """The listed jobs that decide the run: not this gate and not advisory."""
    return [job for job in jobs if job.get("name") != GATE and not plan.is_advisory(job.get("name", ""))]


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


def missing(plan, jobs):
    """Expected job definitions that have no listed job yet."""
    skipped, placed = layout(plan, jobs)
    return [slot.full for slot in plan.slots if slot.area not in skipped and slot not in placed]


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


def verdict(plan, jobs, lookup):
    """Raise ValueError unless the finished run passes. Return the evidence to check next."""
    jobs = judged(plan, jobs)
    bad = failed(jobs)
    if bad:
        raise ValueError("failed: " + ", ".join(job["name"] for job in bad))
    results = area_results(plan, jobs, lookup)
    aggregate(results)
    proven = (lookup.get("outputs") or {}).get("javascript-checks") == "true"
    evidence = {"backend": "false", "windows": "false"}
    for area in CALLER_GATED:
        if results[area]["result"] != "success":
            continue  # a skip the lookup proved: nothing of it ran
        needs, expected = inner_needs(plan, jobs, area)
        expected = [expected] if isinstance(expected, str) else list(expected)
        if area == "javascript":
            check_javascript(needs, expected, proven)
        else:
            check_every_branch(area, needs, expected)
        if area == "backend":
            evidence["backend"] = "true"
            evidence["backend-needs"] = json.dumps(needs, sort_keys=True)
        elif area == "windows":
            evidence["windows"] = "true"
    return evidence


def report(bad, out=sys.stdout, summary_path=None):
    lines = [f"- {job['name']}: {job.get('conclusion')} {job.get('html_url', '')}".rstrip() for job in bad]
    print("::error title=Main suite stopped early::" + "; ".join(job["name"] for job in bad), file=out)
    print("\n".join(lines), file=out, flush=True)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("### Stopped on the first failure\n\nThese jobs did not pass, so the rest of the run was cancelled:\n\n")
            handle.write("\n".join(lines) + "\n")


def watch(plan, repository, run, attempt, token, call=request, sleep=time.sleep, clock=time.monotonic,
          out=sys.stdout, summary_path=None):
    """Poll until the run is decided. Return the judged jobs, or raise SystemExit(1)."""
    unreadable, stalled_since, confirming = 0, None, None
    while True:
        try:
            jobs = judged(plan, jobs_of(repository, run, attempt, token, call))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
            unreadable += 1
            print(f"could not read the run's jobs ({error})", file=out, flush=True)
            if unreadable >= MAX_FAILED_READS:
                print("::error title=Main suite::The run's jobs could not be read; it is not proven.", file=out)
                raise SystemExit(1)
            sleep(POLL_SECONDS)
            continue
        unreadable = 0
        bad = failed(jobs)
        if bad:
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
                print("::error title=Main suite::Expected jobs never appeared: " + ", ".join(absent), file=out)
                raise SystemExit(1)
        else:
            stalled_since = None
        confirming = None
        sleep(POLL_SECONDS)


def main(environ=os.environ, call=request, sleep=time.sleep, out=sys.stdout):
    plan = Plan()
    lookup = json.loads(environ["RESULTS"]).get(LOOKUP) or {}
    jobs = watch(plan, environ["GITHUB_REPOSITORY"], environ["GITHUB_RUN_ID"],
                 environ.get("GITHUB_RUN_ATTEMPT", "1"), environ["GH_TOKEN"], call=call, sleep=sleep, out=out,
                 summary_path=environ.get("GITHUB_STEP_SUMMARY"))
    try:
        evidence = verdict(plan, jobs, lookup)
    except ValueError as error:
        print(f"::error title=Main suite::{error}", file=out)
        return 1
    if environ.get("GITHUB_OUTPUT"):
        with open(environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key, value in evidence.items():
                handle.write(f"{key}={value}\n")
    print("Every hosted full-suite job succeeded, or a pull request passed it on these exact files.", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
