"""Default-branch cache seeding contracts; no APK fetch or emulator in these tests."""
from pathlib import Path
import subprocess
import unittest

import yaml
import ci_webview as provider

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/android-webview-cache.yml"


class CacheSeedWorkflowTest(unittest.TestCase):
    def workflow(self):
        self.assertTrue(WORKFLOW.is_file(), "No default-branch WebView cache seeder; PR-scoped caches cannot serve other PRs")
        return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    def test_seed_is_default_branch_only(self):
        config = self.workflow()
        self.assertEqual({"push", "schedule", "workflow_dispatch"}, set(config["on"]))
        self.assertEqual(config["on"]["push"], {"branches": ["main"], "paths": [
            "android/ci/ci_webview.py", ".github/workflows/android-webview-cache.yml",
        ]})
        self.assertEqual({"seed"}, set(config["jobs"]))
        self.assertEqual(config["jobs"]["seed"]["if"],
                         "github.repository == 'HobbyCoders/ai-shuffle' && github.ref == 'refs/heads/main'")

    def test_refresh_interval_is_shorter_than_seven_day_eviction(self):
        # Twice weekly leaves headroom for schedule delays without daily work.
        self.assertEqual([{"cron": "23 4 * * 1,4"}], self.workflow()["on"]["schedule"])

    def test_only_one_small_standard_runner_without_emulator_or_publishing(self):
        config = self.workflow()
        self.assertEqual({"contents": "read"}, config["permissions"])
        self.assertEqual({"group": "android-webview-cache-main", "cancel-in-progress": "false"}, config["concurrency"])
        job = config["jobs"]["seed"]
        self.assertEqual("ubuntu-latest", job["runs-on"])
        self.assertEqual("15", job["timeout-minutes"])
        self.assertNotIn("strategy", job)
        self.assertNotIn("continue-on-error", job)
        self.assertEqual({"actions/checkout@v4", "actions/setup-python@v5", "actions/cache@v4"},
                         {step["uses"] for step in job["steps"] if "uses" in step})
        for step in job["steps"]:
            self.assertNotIn("continue-on-error", step)
            for forbidden in ("secrets.", "sdkmanager", "emulator", "adb ", "gh release", "upload-artifact"):
                self.assertNotIn(forbidden, step.get("run", "") + step.get("uses", ""))

    def test_seed_and_both_consumers_use_identical_cache_version_inputs(self):
        self.workflow()
        for name, job in (("android-webview-cache.yml", "seed"), ("android.yml", "android-ui"),
                          ("ci-android-minified.yml", "smoke")):
            with self.subTest(workflow=name):
                config = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
                caches = [step for step in config["jobs"][job]["steps"]
                          if step.get("uses", "").startswith("actions/cache@")]
                self.assertEqual(1, len(caches))
                self.assertEqual("actions/cache@v4", caches[0]["uses"])
                self.assertEqual({"path": "${{ runner.temp }}/ci-webview.apk",
                                  "key": "aosp-webview-x86_64-" + provider.SHA256}, caches[0]["with"])

    def test_size_and_digest_verification_runs_even_on_a_cache_hit(self):
        steps = self.workflow()["jobs"]["seed"]["steps"]
        cache = next(step for step in steps if step.get("uses") == "actions/cache@v4")
        verifiers = [step for step in steps
                     if step.get("run") == 'python3 android/ci/ci_webview.py "$RUNNER_TEMP/ci-webview.apk"']
        self.assertEqual(1, len(verifiers))
        self.assertNotIn("if", cache)
        self.assertNotIn("if", verifiers[0])
        self.assertLess(steps.index(cache), steps.index(verifiers[0]))
        # actions/cache saves only after a successful job; corrupt bytes cannot
        # be published when this shared size/SHA verifier fails.
        self.assertEqual("actions/checkout@v4", steps[0]["uses"])

    def test_inline_shell_parses(self):
        for step in self.workflow()["jobs"]["seed"]["steps"]:
            if "run" in step:
                result = subprocess.run(["/bin/bash", "-n"], input=step["run"], text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
