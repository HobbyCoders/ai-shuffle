"""Exercise CI cost gates without app dependencies, credentials or paid runners."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEVANT = "github.event_name != 'pull_request' || (github.event.action != 'labeled' && github.event.action != 'unlabeled') || github.event.label.name == 'ci:full'"
NOOP = "github.event_name == 'pull_request' && (github.event.action == 'labeled' || github.event.action == 'unlabeled') && github.event.label.name != 'ci:full'"
FULL = "github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'ci:full')"
EXPENSIVE = {
    "ci-browser-workspace.yml": ["workspace"],
    "android.yml": ["android-ui"],
    "desktop.yml": ["package", "windows-preview"],
}
REQUIRED = {
    "ci-browser-workspace.yml": ["smoke", "workspace"],
    "android.yml": ["changes", "account-hub", "build", "android-ui"],
    "desktop.yml": ["check", "package", "windows-preview"],
}
SCRIPT = ROOT / "scripts/ci/require-full-ci.py"
spec = importlib.util.spec_from_file_location("readiness", SCRIPT)
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class CostPolicyTests(unittest.TestCase):
    def test_expensive_jobs_and_readiness_share_the_same_request(self):
        for name, expensive in EXPENSIVE.items():
            with self.subTest(workflow=name):
                jobs = workflow(name)["jobs"]
                for key in expensive:
                    self.assertEqual(jobs[key]["if"], FULL)
                gate = jobs["readiness"]
                self.assertEqual(gate["needs"], REQUIRED[name])
                self.assertEqual(gate["if"], "${{ !cancelled() && (" + RELEVANT + ") }}")
                self.assertEqual(gate["runs-on"], "ubuntu-latest")
                step = gate["steps"][-1]
                expected_env = {
                    "CI_FULL_REQUESTED": "${{ " + FULL + " }}",
                    "CI_REQUIRED_RESULTS": "${{ toJSON(needs) }}",
                    "CI_DEFERRABLE_JOBS": json.dumps(expensive),
                }
                if name == "android.yml":
                    expected_env["CI_ANDROID_APK_REQUIRED"] = "${{ needs.changes.outputs.apk }}"
                self.assertEqual(step["env"], expected_env)
                self.assertEqual(step["run"], "python3 scripts/ci/require-full-ci.py")
                self.assertNotIn("continue-on-error", gate)

    def test_label_gate_truth_table_executes(self):
        # GitHub's label projection is the only non-JS syntax in this expression.
        expression = FULL.replace("labels.*.name", "labels.map(label => label.name)")
        cases = []
        for event, labels in itertools.product(
            ["pull_request", "workflow_dispatch", "workflow_call"],
            [[], [{"name": "bug"}], [{"name": "ci:full"}], [{"name": "ci:full-other"}]],
        ):
            github = {"event_name": event, "event": {}}
            if event == "pull_request":
                github["event"]["pull_request"] = {"labels": labels}
            cases.append({"github": github, "expected": event != "pull_request" or any(label["name"] == "ci:full" for label in labels)})
        runner = "const {expr,cases}=JSON.parse(require('fs').readFileSync(0,'utf8')); const fn=new Function('github','contains','return '+expr); for(const c of cases) require('assert').strictEqual(fn(c.github,(xs,x)=>xs.includes(x)),c.expected);"
        subprocess.run(["node", "-e", runner], input=json.dumps({"expr": expression, "cases": cases}), text=True, check=True)

    def test_new_commits_and_label_removal_recheck_readiness(self):
        for name in EXPENSIVE:
            config = workflow(name)
            self.assertEqual(set(config["on"]["pull_request"]["types"]), {"opened", "synchronize", "reopened", "labeled", "unlabeled"})
            self.assertIn("scripts/ci/require-full-ci.py", config["on"]["pull_request"]["paths"])
            self.assertIn("github.ref", config["concurrency"]["group"])
            self.assertIn(NOOP + " && github.run_id", config["concurrency"]["group"])
            self.assertEqual(config["run-name"], "${{ github.workflow }}${{ " + NOOP + " && ' (unrelated label event)' || '' }}")
            self.assertIn(config["concurrency"]["cancel-in-progress"], ["true", "${{ github.event_name == 'pull_request' }}"])

    def test_cheap_checks_do_not_require_the_label(self):
        for name, keys in {
            "ci-browser-workspace.yml": ["smoke"],
            "android.yml": ["account-hub", "changes"],
            "desktop.yml": ["check"],
        }.items():
            jobs = workflow(name)["jobs"]
            for key in keys:
                self.assertEqual(jobs[key]["if"], RELEVANT)
                self.assertLessEqual(int(jobs[key]["timeout-minutes"]), 20)

    def test_android_consumers_and_staging_are_gated_together(self):
        jobs = workflow("android.yml")["jobs"]
        self.assertEqual([row["api"] for row in jobs["android-ui"]["strategy"]["matrix"]["include"]], ["34", "36"])
        steps = {step.get("name"): step for step in jobs["build"]["steps"]}
        for name in ["Build Android test APK", "Upload instrumentation APKs"]:
            self.assertEqual(steps[name]["if"], FULL)
        self.assertIn("assembleDebug", steps["Build and verify debug APK"]["run"])
        self.assertEqual(jobs["publish"]["needs"], ["account-hub", "build", "android-ui"])
        self.assertIn("inputs.publish_download", jobs["publish"]["if"])
        self.assertIn("github.event_name != 'pull_request'", jobs["publish"]["if"])


    def test_irrelevant_labels_do_not_cancel_or_start_validation(self):
        cases = []
        for event, action, label in itertools.product(
            ["pull_request", "workflow_dispatch", "workflow_call"],
            ["opened", "synchronize", "reopened", "labeled", "unlabeled"],
            ["ci:full", "bug", "ci:full-other"],
        ):
            irrelevant = event == "pull_request" and action in {"labeled", "unlabeled"} and label != "ci:full"
            github = {"event_name": event, "event": {"action": action, "label": {"name": label}}}
            cases.append({"github": github, "expected": not irrelevant})
        runner = "const {expr,cases}=JSON.parse(require('fs').readFileSync(0,'utf8')); const fn=new Function('github','return '+expr); for(const c of cases) require('assert').strictEqual(fn(c.github),c.expected);"
        subprocess.run(["node", "-e", runner], input=json.dumps({"expr": RELEVANT, "cases": cases}), text=True, check=True)
        for name in EXPENSIVE:
            config = workflow(name)
            group = config["concurrency"]["group"]
            for job in config["jobs"].values():
                self.assertIn(NOOP + " && ' (unrelated label event)'", job["name"])
            # The same real group handles ci:full additions AND removals; each
            # irrelevant event has its own group, so cannot evict pending work.
            self.assertIn(NOOP + " && github.run_id || 'validation'", group)

    def test_android_apk_classification_and_readiness(self):
        spec = importlib.util.spec_from_file_location("android_inputs", ROOT / "scripts/ci/android-inputs.py")
        inputs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inputs)
        def check(*names):
            return inputs.apk_required([[{"filename": name} for name in names]])
        for path in ["android/hub/src/index.ts", "android/site/package-lock.json", "android/docs/testing.md", "docs/content/setup.md"]:
            self.assertFalse(check(path))
        for path in ["android/app/src/Main.kt", "android/build.gradle.kts", "android/gradle.properties", "android/ci/test-mobile-apk.py", "android/scripts/sync-offline-demo.sh", "mobile/src/App.svelte", "frontend/src/lib/api.ts", ".github/workflows/android.yml", "scripts/ci/android-inputs.py", "android/new-build-input/data"]:
            self.assertTrue(check(path))
            self.assertTrue(check("android/site/src/index.ts", path))
        self.assertTrue(inputs.apk_required([[{"filename": "android/site/old.kt", "previous_filename": "android/app/src/old.kt", "status": "renamed"}]]))
        self.assertTrue(inputs.apk_required([[{"filename": "android/app/src/old.kt", "status": "removed"}]]))
        self.assertTrue(inputs.apk_required([[]]))
        for pages in [None, [], [None], [[{}]], [[{"filename": "android/site/x", "status": "renamed"}]]]:
            with self.assertRaises(ValueError):
                inputs.apk_required(pages)
        for pr in [None, {}, {"head": None}, {"head": {"sha": "new"}, "changed_files": 1}, {"head": {"sha": "head"}, "changed_files": 2}]:
            with self.assertRaises(ValueError):
                inputs.apk_required([[{"filename": "android/site/x"}]], pr, "head")
        results = {"changes": {"result": "success"}, "account-hub": {"result": "success"}, "build": {"result": "skipped"}, "android-ui": {"result": "skipped"}}
        for requested in ["true", "false"]:
            self.assertIsNone(readiness.readiness_error(requested, results, ("android-ui",), "false"))
            for apk in ["true", "", "invalid", None]:
                self.assertIsNotNone(readiness.readiness_error(requested, results, ("android-ui",), apk))
            for job in results:
                for state in ["failure", "cancelled", "pending"]:
                    bad = dict(results, **{job: {"result": state}})
                    self.assertIsNotNone(readiness.readiness_error(requested, bad, ("android-ui",), "false"))
        jobs = workflow("android.yml")["jobs"]
        self.assertEqual(jobs["build"]["needs"], "changes")
        self.assertEqual(jobs["build"]["if"], "needs.changes.outputs.apk == 'true'")
        self.assertEqual(jobs["changes"]["outputs"]["apk"], "${{ steps.inputs.outputs.apk }}")
        command = jobs["changes"]["steps"][-1]["run"]
        self.assertIn('if [ "$EVENT_NAME" != pull_request ]', command)
        self.assertIn("echo 'apk=true'", command)
        self.assertIn("--paginate --slurp", command)
        callers = []
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            config = workflow(path.name)
            for job in config.get("jobs", {}).values():
                if job.get("uses") == "./.github/workflows/android.yml":
                    callers.append(path.name)
                    self.assertEqual(job["permissions"]["pull-requests"], "read")
                    self.assertEqual(job["permissions"]["contents"], "write")
                    self.assertEqual(job["permissions"]["actions"], "read")
        self.assertEqual(callers, ["platform-installer.yml"])

    def test_no_workflow_runs_on_a_schedule(self):
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                config = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
                self.assertNotIn("schedule", config["on"])
                self.assertNotIn("canary-gate", config.get("jobs", {}))

    def test_ios_macos_job_is_path_filtered_unsigned_and_bounded(self):
        config = workflow("ios.yml")
        self.assertEqual(set(config["on"]), {"pull_request", "workflow_dispatch"})
        self.assertEqual(config["on"]["pull_request"]["paths"], ["ios/**", ".github/workflows/ios.yml"])
        self.assertIsNone(config["on"]["workflow_dispatch"] or None)
        self.assertEqual(config["permissions"], {"contents": "read"})
        self.assertEqual(config["concurrency"]["cancel-in-progress"], "${{ github.event_name == 'pull_request' }}")
        jobs = config["jobs"]
        self.assertEqual(list(jobs), ["simulator"])
        job = jobs["simulator"]
        self.assertEqual(job["runs-on"], "blacksmith-6vcpu-macos-latest")
        self.assertLessEqual(int(job["timeout-minutes"]), 30)
        text = (ROOT / ".github/workflows/ios.yml").read_text(encoding="utf-8")
        # Simulator-only CI: no signing material, no uploads, no publishing.
        for forbidden in ["secrets.", "environment:", "upload-artifact", "altool", "-exportArchive", "archive "]:
            self.assertNotIn(forbidden, text)
        self.assertIn("CODE_SIGNING_ALLOWED=NO", text)

    def test_costly_single_purpose_suites_are_manual_only(self):
        # Releases re-run the container check in docker-publish.yml; Linux
        # identity coverage stays in CI Backend.
        for name in ["container-postgres.yml", "ci-instance-identity.yml"]:
            with self.subTest(workflow=name):
                self.assertEqual(set(workflow(name)["on"]), {"workflow_dispatch"})

    def test_benchmark_defaults_and_worker_count_match(self):
        backend = workflow("ci-backend.yml")
        javascript = workflow("ci-frontend.yml")
        self.assertIn("inputs.runner_cpus || '2'", backend["run-name"])
        self.assertIn("inputs.runner_cpus || '4'", javascript["run-name"])
        self.assertEqual(backend["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "2")
        self.assertEqual(javascript["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "4")
        self.assertEqual(backend["jobs"]["backend"]["env"]["PYTEST_WORKERS"], "${{ inputs.runner_cpus == '4' && '4' || '2' }}")
        for config, key, timeout in [(backend, "backend", "35"), (javascript, "frontend", "20")]:
            self.assertEqual(config["jobs"][key]["timeout-minutes"], timeout)
            self.assertIn("inputs.runner_cpus", config["concurrency"]["group"])
        self.assertEqual(backend["jobs"]["backend"]["runs-on"], "${{ inputs.runner_cpus == '4' && 'blacksmith-4vcpu-ubuntu-2404' || 'blacksmith-2vcpu-ubuntu-2404' }}")
        self.assertEqual(javascript["jobs"]["frontend"]["runs-on"], "${{ inputs.runner_cpus == '2' && 'blacksmith-2vcpu-ubuntu-2404' || 'blacksmith-4vcpu-ubuntu-2404' }}")
        command = next(step["run"] for step in backend["jobs"]["backend"]["steps"] if step.get("name") == "Run fast test subset")
        self.assertIn('-n "$PYTEST_WORKERS"', command)

    def test_browser_diagnostics_are_failure_only_and_short_lived(self):
        steps = workflow("ci-browser-workspace.yml")["jobs"]["workspace"]["steps"]
        upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact"))
        self.assertEqual(upload["if"], "failure()")
        self.assertEqual(upload["with"]["retention-days"], "3")

    def test_readiness_rejects_every_non_success_state(self):
        for state in ["skipped", "failure", "cancelled", "pending", None]:
            for requested in ["true", "false", None]:
                for deferrable in [(), ("full",)]:
                    if state == "skipped" and requested != "true" and deferrable:
                        continue
                    with self.subTest(state=state, requested=requested, deferrable=deferrable):
                        results = {"smoke": {"result": "success"}, "full": {"result": state}}
                        self.assertIsNotNone(readiness.readiness_error(requested, results, deferrable))
        for results in [None, {}, [], {"full": None}, {"full": {}}, {"full": "success"}]:
            self.assertIsNotNone(readiness.readiness_error("true", results))
            self.assertIsNotNone(readiness.readiness_error("false", results, ("full",)))

    def test_unlabelled_prs_pass_with_deferred_platform_jobs_only(self):
        deferred = {"smoke": {"result": "success"}, "full": {"result": "skipped"}}
        self.assertIsNone(readiness.readiness_error("false", deferred, ("full",)))
        # A skipped fast job is never acceptable, even without the label.
        self.assertIsNotNone(readiness.readiness_error("false", {"smoke": {"result": "skipped"}, "full": {"result": "skipped"}}, ("full",)))
        # With the label, a deferred job must have run and passed.
        self.assertIsNotNone(readiness.readiness_error("true", deferred, ("full",)))
        passed = {key: {"result": "success"} for key in ["smoke", "full"]}
        for requested in ["true", "false"]:
            self.assertIsNone(readiness.readiness_error(requested, passed, ("full",)))

    def test_actual_readiness_entrypoint_and_summary(self):
        cases = [("false", '{"full":{"result":"skipped"}}', 0, "ci:full"),
                 ("false", '{"fast":{"result":"skipped"}}', 1, "did not pass"),
                 ("true", '{"full":{"result":"skipped"}}', 1, "did not pass"),
                 ("true", '{"full":{"result":"failure"}}', 1, "did not pass"),
                 ("true", '{"full":{"result":"success"}}', 0, "passed"),
                 ("true", "malformed", 1, "missing")]
        with tempfile.TemporaryDirectory() as temporary:
            for index, (requested, results, expected, message) in enumerate(cases):
                summary = Path(temporary) / str(index)
                env = dict(os.environ, CI_FULL_REQUESTED=requested, CI_REQUIRED_RESULTS=results,
                           CI_DEFERRABLE_JOBS='["full"]', GITHUB_STEP_SUMMARY=str(summary))
                result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn(message, result.stdout)
                self.assertIn(message, summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
