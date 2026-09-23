"""Merge gate decisions against the repository's real workflow filters.

No GitHub credentials or network: the API is an in-memory fake.
"""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
spec = importlib.util.spec_from_file_location("merge_gate", ROOT / "scripts/ci/merge-gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

HEAD = "a" * 40
OLD = "b" * 40
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
BACKEND = ".github/workflows/ci-backend.yml"
FRONTEND = ".github/workflows/ci-frontend.yml"
ANDROID = ".github/workflows/android.yml"


def load(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def repo_workflows():
    return {f".github/workflows/{p.name}": load(p.name) for p in sorted(WORKFLOWS.glob("*.yml"))}


def suites(*files, base="main"):
    return gate.applicable_suites(repo_workflows(), list(files), base)


def run(path, *, sha=HEAD, status="completed", conclusion="success", event="pull_request", at=T0, number=1):
    return {"path": path, "head_sha": sha, "status": status, "conclusion": conclusion, "event": event,
            "created_at": at.isoformat().replace("+00:00", "Z"), "run_number": number, "run_attempt": 1,
            "id": number, "html_url": f"https://example.invalid/runs/{number}"}


def decide(files, runs, labels=(), *, grace_expired=True, **kwargs):
    return gate.decide(suites(*files), runs, HEAD, list(labels), grace_expired=grace_expired, **kwargs)


class ApplicabilityTests(unittest.TestCase):
    def test_glob_semantics_match_github_filters(self):
        cases = [
            ("app/**", "app/api/x.py", True),
            ("app/**", "apps/x.py", False),
            ("run_local.*", "run_local.ps1", True),
            ("run_local.*", "scripts/run_local.ps1", False),
            ("docker-compose*.yml", "docker-compose.dev.yml", True),
            ("app/core/browser*.py", "app/core/browser_state.py", True),
            ("app/core/browser*.py", "app/core/sub/browser.py", False),
            ("**/README.md", "README.md", True),
            ("**/README.md", "docs/a/README.md", True),
            ("Start AI Shuffle.cmd", "Start AI Shuffle.cmd", True),
            ("*.md", "docs/x.md", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(gate.filter_matches([pattern], path), expected)
        self.assertFalse(gate.filter_matches(["docs/**", "!docs/private/**"], "docs/private/a.md"))
        self.assertTrue(gate.filter_matches(["docs/**", "!docs/private/**", "docs/private/keep.md"], "docs/private/keep.md"))

    def test_ordinary_backend_change_selects_backend_only(self):
        self.assertEqual([s.path for s in suites("app/api/sessions.py")], [BACKEND])

    def test_unrelated_path_selects_nothing(self):
        self.assertEqual(suites("docs/notes.md", "README.md"), [])

    def test_full_gated_suites_come_from_the_readiness_step(self):
        gated = {s.path for s in suites("android/app/build.gradle.kts", "desktop/src/main.ts", "browser-mcp/index.js") if s.full_gated}
        self.assertEqual(gated, {ANDROID, ".github/workflows/desktop.yml", ".github/workflows/ci-browser-workspace.yml"})

    def test_branch_and_ignore_filters(self):
        workflows = {"w.yml": {"name": "W", "on": {"pull_request": {"branches": ["release/**"], "paths-ignore": ["docs/**"]}}}}
        self.assertEqual(gate.applicable_suites(workflows, ["app/x.py"], "main"), [])
        self.assertEqual(len(gate.applicable_suites(workflows, ["app/x.py"], "release/1")), 1)
        self.assertEqual(gate.applicable_suites(workflows, ["docs/x.md"], "release/1"), [])
        bare = {"w.yml": {"name": "W", "on": ["push", "pull_request"]}}
        self.assertEqual(gate.applicable_suites(bare, ["anything"], "main")[0].types, gate.DEFAULT_TYPES)


class DecisionTests(unittest.TestCase):
    def test_ordinary_pr_passes_when_its_suites_pass(self):
        verdict = decide(["app/x.py", "frontend/src/a.ts"], [run(BACKEND, number=1), run(FRONTEND, number=2)])
        self.assertEqual(verdict.state, "success")
        self.assertEqual(len(verdict.results), 2)

    def test_ordinary_pr_is_pending_while_a_suite_runs(self):
        verdict = decide(["app/x.py"], [run(BACKEND, status="in_progress", conclusion=None)])
        self.assertEqual(verdict.state, "pending")

    def test_unrelated_path_pr_passes_without_runs(self):
        verdict = decide(["docs/notes.md"], [])
        self.assertEqual(verdict.state, "success")
        self.assertIn("No path-filtered", verdict.description)

    def test_labelled_full_ci_pr(self):
        files = ["android/app/src/main/Foo.kt"]
        self.assertEqual(decide(files, [run(ANDROID)], ["ci:full"]).state, "success")
        # The label is part of the contract, not just the readiness job's result.
        unlabelled = decide(files, [run(ANDROID)])
        self.assertEqual(unlabelled.state, "failure")
        self.assertIn("ci:full", unlabelled.description)
        # Readiness failed on the pre-label run; the newest run for the SHA wins.
        runs = [run(ANDROID, conclusion="failure", at=T0, number=1), run(ANDROID, at=T0 + timedelta(minutes=5), number=2)]
        self.assertEqual(decide(files, runs, ["ci:full"]).state, "success")

    def test_label_change_waits_for_the_new_run(self):
        files = ["android/app/src/main/Foo.kt"]
        old = [run(ANDROID, conclusion="failure", at=T0)]
        labelled_at = T0 + timedelta(minutes=2)
        waiting = decide(files, old, ["ci:full"], grace_expired=False, label_action="labeled", label_changed_at=labelled_at)
        self.assertEqual(waiting.state, "pending")
        self.assertTrue(waiting.waiting_for_runs)
        new = old + [run(ANDROID, status="queued", conclusion=None, at=labelled_at + timedelta(seconds=2), number=2)]
        started = decide(files, new, ["ci:full"], grace_expired=False, label_action="labeled", label_changed_at=labelled_at)
        self.assertEqual(started.state, "pending")
        self.assertFalse(started.waiting_for_runs)
        # A label change does not re-run suites without label triggers.
        backend = decide(["app/x.py"], [run(BACKEND, at=T0)], ["ci:full"], grace_expired=False,
                         label_action="labeled", label_changed_at=labelled_at)
        self.assertEqual(backend.state, "success")

    def test_failed_cancelled_skipped_and_timed_out_runs_block(self):
        for conclusion in ["failure", "cancelled", "skipped", "timed_out", "action_required", "startup_failure", None]:
            with self.subTest(conclusion=conclusion):
                verdict = decide(["app/x.py"], [run(BACKEND, conclusion=conclusion)])
                self.assertEqual(verdict.state, "failure")
                self.assertIn("CI Backend", verdict.description)

    def test_failure_outranks_pending(self):
        verdict = decide(["app/x.py", "frontend/a.ts"], [run(BACKEND, conclusion="failure"), run(FRONTEND, status="in_progress", conclusion=None)])
        self.assertEqual(verdict.state, "failure")

    def test_newer_failure_overrides_older_success(self):
        runs = [run(BACKEND, at=T0, number=1), run(BACKEND, conclusion="failure", at=T0 + timedelta(minutes=1), number=2)]
        self.assertEqual(decide(["app/x.py"], runs).state, "failure")

    def test_stale_sha_after_a_push(self):
        runs = [run(BACKEND, sha=OLD)]
        starting = decide(["app/x.py"], runs, grace_expired=False)
        self.assertEqual(starting.state, "pending")
        stale = decide(["app/x.py"], runs)
        self.assertEqual(stale.state, "failure")
        self.assertIn(OLD[:7], stale.description)

    def test_missing_check(self):
        self.assertEqual(decide(["app/x.py"], [], grace_expired=False).state, "pending")
        missing = decide(["app/x.py"], [])
        self.assertEqual(missing.state, "failure")
        self.assertIn("no run for this revision", missing.description)
        # A run of a different workflow or from an unrelated event is not evidence.
        self.assertEqual(decide(["app/x.py"], [run(FRONTEND), run(BACKEND, event="schedule")]).state, "failure")

    def test_manual_dispatch_for_the_same_sha_counts(self):
        self.assertEqual(decide(["app/x.py"], [run(BACKEND, event="workflow_dispatch")]).state, "success")

    def test_description_fits_a_commit_status(self):
        files = ["app/x.py", "frontend/a.ts", "android/x", "desktop/x", "fluid/x", "Dockerfile"]
        verdict = decide(files, [])
        self.assertLessEqual(len(verdict.description), 140)


class FakeGitHub:
    def __init__(self, *, head=HEAD, labels=(), files=("app/x.py",), runs=(), state="open", mergeable=True, runs_after=None):
        self.pull = {"number": 7, "state": state, "head": {"sha": head}, "base": {"ref": "main"},
                     "merge_commit_sha": "m" * 40, "mergeable": mergeable, "changed_files": len(files),
                     "labels": [{"name": n} for n in labels], "updated_at": "2026-09-22T12:02:00Z"}
        self.files = [{"filename": f} for f in files]
        self.runs = list(runs)
        self.runs_after = runs_after or []
        self.run_queries = 0
        self.statuses = []

    def get(self, path, params=None):
        if path.endswith("/pulls/7"):
            return self.pull
        if "/commits/" in path and path.endswith("/pulls"):
            return [self.pull]
        raise AssertionError(path)

    def paginate(self, path, params=None, key=None):
        if path.endswith("/pulls/7/files"):
            return self.files
        if path.endswith("/actions/runs"):
            self.run_queries += 1
            if self.run_queries > 1:
                self.runs += self.runs_after
                self.runs_after = []
            return [r for r in self.runs if r["head_sha"] == params["head_sha"]]
        raise AssertionError(path)

    def graphql(self, query, variables):
        texts = {p.name: {"text": p.read_text(encoding="utf-8")} for p in WORKFLOWS.glob("*.yml")}
        return {"repository": {
            "commit": {"parents": {"nodes": [{"oid": "c" * 40}, {"oid": HEAD}]}},
            "tree": {"entries": [{"name": n, "type": "blob", "object": o} for n, o in texts.items()]},
        }}

    def set_status(self, repo, sha, state, description, target_url):
        self.statuses.append((sha, state, description))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def pr_event(action="synchronize"):
    return {"action": action, "pull_request": {"number": 7, "head": {"sha": HEAD}, "updated_at": "2026-09-22T12:02:00Z"}}


class OrchestrationTests(unittest.TestCase):
    def execute(self, api, event_name, payload):
        clock = Clock()
        return gate.run(api, "o/r", event_name, payload, sleep=clock.sleep, clock=clock), clock

    def test_pull_request_target_writes_pending_then_the_verdict(self):
        api = FakeGitHub(runs=[run(BACKEND)])
        self.execute(api, "pull_request_target", pr_event())
        self.assertEqual([s[1] for s in api.statuses], ["pending", "success"])
        self.assertTrue(all(s[0] == HEAD for s in api.statuses))

    def test_waits_for_runs_that_have_not_started(self):
        api = FakeGitHub(runs=[], runs_after=[run(BACKEND, status="queued", conclusion=None)])
        _, clock = self.execute(api, "pull_request_target", pr_event())
        self.assertEqual(api.statuses[-1][1], "pending")
        self.assertEqual(api.run_queries, 2)
        self.assertLess(clock.now, gate.GRACE_SECONDS)

    def test_missing_run_fails_after_the_grace_period(self):
        api = FakeGitHub(runs=[])
        _, clock = self.execute(api, "pull_request_target", pr_event())
        self.assertEqual(api.statuses[-1][1], "failure")
        self.assertGreaterEqual(clock.now, gate.GRACE_SECONDS)

    def test_workflow_run_completion_reevaluates_the_head(self):
        api = FakeGitHub(runs=[run(BACKEND)])
        payload = {"workflow_run": {"head_sha": HEAD, "event": "pull_request", "pull_requests": []}}
        self.execute(api, "workflow_run", payload)
        self.assertEqual(api.statuses, [(HEAD, "success", api.statuses[0][2])])

    def test_triggering_run_counts_when_the_run_search_lags(self):
        # The search has not indexed the run that just completed yet.
        api = FakeGitHub(runs=[])
        payload = {"workflow_run": run(BACKEND, number=9) | {"pull_requests": []}}
        self.execute(api, "workflow_run", payload)
        self.assertEqual([s[1] for s in api.statuses], ["success"])

    def test_triggering_run_replaces_its_stale_listing(self):
        api = FakeGitHub(runs=[run(BACKEND, number=9, status="in_progress", conclusion=None)])
        payload = {"workflow_run": run(BACKEND, number=9, conclusion="failure") | {"pull_requests": []}}
        self.execute(api, "workflow_run", payload)
        self.assertEqual([s[1] for s in api.statuses], ["failure"])

    def test_workflow_run_for_an_old_sha_writes_nothing(self):
        api = FakeGitHub(runs=[run(BACKEND, sha=OLD)])
        payload = {"workflow_run": {"head_sha": OLD, "event": "pull_request", "pull_requests": [{"number": 7, "head": {"sha": OLD}}]}}
        self.execute(api, "workflow_run", payload)
        self.assertEqual(api.statuses, [])

    def test_scheduled_runs_are_ignored(self):
        api = FakeGitHub()
        self.execute(api, "workflow_run", {"workflow_run": {"head_sha": HEAD, "event": "schedule", "pull_requests": []}})
        self.assertEqual(api.statuses, [])

    def test_merge_conflict_fails_fast(self):
        api = FakeGitHub(mergeable=False)
        _, clock = self.execute(api, "pull_request_target", pr_event())
        self.assertEqual(api.statuses[-1][1], "failure")
        self.assertEqual(clock.now, 0)

    def test_truncated_file_list_fails_closed(self):
        api = FakeGitHub()
        api.pull["changed_files"] = 3001
        self.execute(api, "pull_request_target", pr_event())
        self.assertEqual(api.statuses[-1][1], "failure")

    def test_renamed_files_count_under_both_names(self):
        api = FakeGitHub(files=())
        api.files = [{"filename": "docs/moved.py", "previous_filename": "app/moved.py"}]
        api.pull["changed_files"] = 1
        self.execute(api, "workflow_run", {"workflow_run": {"head_sha": HEAD, "event": "pull_request", "pull_requests": []}})
        self.assertEqual(api.statuses[-1][1], "failure")
        self.assertIn("CI Backend", api.statuses[-1][2])

    def test_local_replay_never_writes(self):
        api = FakeGitHub(runs=[run(BACKEND)], state="closed")
        self.execute(api, "local", {"pr_number": 7})
        self.assertEqual(api.statuses, [])


class WorkflowContractTests(unittest.TestCase):
    def test_watches_every_pull_request_workflow(self):
        config = load("merge-gate.yml")
        watched = set(config["on"]["workflow_run"]["workflows"])
        expected = {c["name"] for p, c in repo_workflows().items() if gate.pull_request_trigger(c) is not None}
        self.assertEqual(watched, expected)
        self.assertEqual(config["on"]["workflow_run"]["types"], ["completed"])

    def test_gate_is_always_on_cheap_and_never_runs_pr_code(self):
        config = load("merge-gate.yml")
        self.assertIsNone(gate.pull_request_trigger(config), "A pull_request trigger would run the PR's copy of the gate")
        trigger = config["on"]["pull_request_target"]
        self.assertNotIn("paths", trigger)
        self.assertNotIn("branches", trigger)
        self.assertEqual(set(trigger["types"]), {"opened", "synchronize", "reopened", "labeled", "unlabeled"})
        self.assertEqual(config["permissions"], {})
        job = config["jobs"]["evaluate"]
        self.assertEqual(job["runs-on"], "ubuntu-slim")
        # ubuntu-slim caps jobs at 15 minutes; the wait for runs must fit well inside.
        self.assertLessEqual(int(job["timeout-minutes"]), 10)
        self.assertLess(gate.GRACE_SECONDS, int(job["timeout-minutes"]) * 60 / 2)
        self.assertEqual(job["permissions"], {"actions": "read", "contents": "read", "pull-requests": "read", "statuses": "write"})
        self.assertNotEqual(job["name"], gate.CONTEXT)
        checkout = job["steps"][0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(config["concurrency"]["cancel-in-progress"], "false")
        self.assertIn("head_sha", config["concurrency"]["group"])

    def test_policy_workflow_runs_these_tests(self):
        steps = load("ci-artifact-policy.yml")["jobs"]["policy"]["steps"]
        self.assertIn("python scripts/ci/test-merge-gate.py", [s.get("run") for s in steps])


if __name__ == "__main__":
    unittest.main()
