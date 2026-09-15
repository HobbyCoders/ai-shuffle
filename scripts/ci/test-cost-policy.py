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
FULL = "github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'ci:full')"
EXPENSIVE = {
    "ci-browser-workspace.yml": ["workspace"],
    "android.yml": ["android-ui"],
    "desktop.yml": ["package", "windows-preview"],
}
REQUIRED = {
    "ci-browser-workspace.yml": ["smoke", "workspace"],
    "android.yml": ["account-hub", "build", "android-ui"],
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
                self.assertEqual(gate["if"], "${{ !cancelled() }}")
                self.assertEqual(gate["runs-on"], "ubuntu-latest")
                step = gate["steps"][-1]
                self.assertEqual(step["env"], {
                    "CI_FULL_REQUESTED": "${{ " + FULL + " }}",
                    "CI_REQUIRED_RESULTS": "${{ toJSON(needs) }}",
                })
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
            self.assertNotIn("github.event.action", config["concurrency"]["group"])
            self.assertIn(config["concurrency"]["cancel-in-progress"], ["true", "${{ github.event_name == 'pull_request' }}"])

    def test_cheap_checks_do_not_require_the_label(self):
        for name, keys in {
            "ci-browser-workspace.yml": ["smoke"],
            "android.yml": ["account-hub", "build"],
            "desktop.yml": ["check"],
        }.items():
            jobs = workflow(name)["jobs"]
            for key in keys:
                self.assertNotIn("if", jobs[key])
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

    def test_daily_canaries_still_skip_unchanged_main(self):
        for name in ["ci-backend.yml", "ci-frontend.yml"]:
            config = workflow(name)
            self.assertEqual(config["on"]["schedule"], [{"cron": "7 6 * * *"}])
            self.assertIn('if [ "$last" = "$GITHUB_SHA" ]', config["jobs"]["canary-gate"]["steps"][0]["run"])

    def test_benchmark_defaults_and_worker_count_match(self):
        backend = workflow("ci-backend.yml")
        javascript = workflow("ci-frontend.yml")
        self.assertIn("inputs.runner_cpus || '2'", backend["run-name"])
        self.assertIn("inputs.runner_cpus || '4'", javascript["run-name"])
        self.assertEqual(backend["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "2")
        self.assertEqual(javascript["on"]["workflow_dispatch"]["inputs"]["runner_cpus"]["default"], "4")
        self.assertEqual(backend["jobs"]["backend"]["env"]["PYTEST_WORKERS"], "${{ inputs.runner_cpus == '4' && '4' || '2' }}")
        for config, key in [(backend, "backend"), (javascript, "frontend")]:
            self.assertEqual(config["jobs"][key]["timeout-minutes"], "20")
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
                with self.subTest(state=state, requested=requested):
                    self.assertIsNotNone(readiness.readiness_error(requested, {"smoke": {"result": "success"}, "full": {"result": state}}))
        for results in [None, {}, [], {"full": None}, {"full": {}}, {"full": "success"}]:
            self.assertIsNotNone(readiness.readiness_error("true", results))

    def test_success_does_not_override_removed_label(self):
        results = {key: {"result": "success"} for key in ["smoke", "full"]}
        self.assertIsNone(readiness.readiness_error("true", results))
        self.assertIsNotNone(readiness.readiness_error("false", results))

    def test_actual_readiness_entrypoint_and_summary(self):
        cases = [("false", '{"full":{"result":"skipped"}}', 1, "ci:full"),
                 ("true", '{"full":{"result":"failure"}}', 1, "did not pass"),
                 ("true", '{"full":{"result":"success"}}', 0, "passed"),
                 ("true", "malformed", 1, "missing")]
        with tempfile.TemporaryDirectory() as temporary:
            for index, (requested, results, expected, message) in enumerate(cases):
                summary = Path(temporary) / str(index)
                env = dict(os.environ, CI_FULL_REQUESTED=requested, CI_REQUIRED_RESULTS=results, GITHUB_STEP_SUMMARY=str(summary))
                result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn(message, result.stdout)
                self.assertIn(message, summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
