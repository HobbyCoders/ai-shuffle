#!/usr/bin/env python3
"""Safety contracts for the private Play release workflow (no Google credentials)."""
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/android-play.yml"


class PlayWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves YAML's `on` key instead of interpreting it as True.
        cls.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        cls.jobs = cls.workflow["jobs"]

    def test_only_manual_main_or_called_tag_can_release(self):
        self.assertEqual(set(self.workflow["on"]), {"pull_request", "workflow_dispatch", "workflow_call"})
        for name in ("bundle", "publish"):
            condition = self.jobs[name]["if"]
            self.assertIn("github.event_name == 'workflow_dispatch'", condition)
            self.assertIn("github.ref == 'refs/heads/main'", condition)
            self.assertIn("github.event_name == 'push'", condition)
            self.assertIn("startsWith(github.ref, 'refs/tags/v')", condition)
            self.assertIn("inputs.release_tag != ''", condition)
        for event in ("workflow_dispatch", "workflow_call"):
            inputs = self.workflow["on"][event]["inputs"]
            self.assertEqual(inputs["publish_to_play"]["default"], "false")
            self.assertEqual(inputs["workers_ready"]["default"], "false")
        self.assertEqual(self.workflow["on"]["workflow_call"]["inputs"]["release_tag"]["required"], "true")
        self.assertIn("inputs.publish_to_play", self.jobs["publish"]["if"])

    def test_publish_has_no_production_or_track_input(self):
        upload = next(step for step in self.jobs["publish"]["steps"]
                      if step.get("uses", "").startswith("r0adkll/"))
        self.assertRegex(upload["uses"], r"@[a-f0-9]{40}$")
        self.assertEqual(upload["with"]["tracks"], "internal")
        self.assertEqual(upload["with"]["status"], "completed")
        self.assertEqual(upload["with"]["packageName"], "com.hobbycoders.aishuffle")
        self.assertEqual(upload["with"]["releaseFiles"], "play-bundle/app-release.aab")
        self.assertEqual(self.jobs["publish"]["environment"], "google-play-internal")
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")

    def test_readiness_guard_executes_fail_closed(self):
        guard = self.jobs["publish"]["steps"][0]["run"]
        ready = {"PLAY_SIGNING_READY": "true", "ANDROID_WORKERS_READY": "true",
                 "WORKERS_CONFIRMED": "true", "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": "dummy-test-value"}
        result = subprocess.run(["/bin/bash", "-e", "-c", guard], env=ready, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ready:
            with self.subTest(missing=name):
                env = {**ready, name: ""}
                result = subprocess.run(["/bin/bash", "-e", "-c", guard], env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"dummy-test-value", result.stdout + result.stderr)

    def test_missing_signing_secret_is_rejected(self):
        guard = next(step["run"] for step in self.jobs["bundle"]["steps"]
                     if step.get("name") == "Require existing release signing secrets")
        keys = ("KEYSTORE_BASE64", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD")
        for missing in keys:
            env = {name: "dummy-secret" for name in keys if name != missing}
            result = subprocess.run(["/bin/bash", "-e", "-c", guard], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"dummy-secret", result.stdout + result.stderr)

    def test_bundle_is_verified_and_only_public_artifacts_exported(self):
        steps = self.jobs["bundle"]["steps"]
        build = next(step["run"] for step in steps if step.get("name") == "Test and build signed bundle")
        self.assertIn("bundleRelease", build)
        self.assertIn("testDebugUnitTest lintDebug", build)
        self.assertIn("verify-mobile-apk.py --app-bundle", build)
        self.assertIn("jarsigner -verify -strict", build)
        artifacts = next(step["with"] for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(artifacts["path"].splitlines(), [
            "android/app/build/outputs/bundle/release/app-release.aab",
            "android/app/build/outputs/bundle/release/signing-certificate.pem",
        ])
        cleanup = steps[-1]
        self.assertEqual(cleanup["if"], "always()")
        self.assertIn('rm -f "$RUNNER_TEMP/play-release.jks"', cleanup["run"])
        self.assertEqual(self.jobs["bundle"]["needs"], "checks")
        self.assertEqual(self.jobs["publish"]["needs"], "bundle")

    def test_release_bundle_uses_immutable_tag_and_matching_version(self):
        steps = self.jobs["bundle"]["steps"]
        checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"],
                         "${{ inputs.release_tag && format('refs/tags/{0}', inputs.release_tag) || github.sha }}")
        version = next(step for step in steps if step.get("name") == "Set release version name")
        self.assertEqual(version["env"]["RELEASE_TAG"], "${{ inputs.release_tag }}")
        self.assertIn('ANDROID_VERSION_NAME=${RELEASE_TAG#v}', version["run"])

    def test_all_inline_shell_parses(self):
        for job in self.jobs.values():
            for step in job["steps"]:
                if "run" in step:
                    with self.subTest(step=step.get("name", step["run"])):
                        result = subprocess.run(["/bin/bash", "-n"], input=step["run"], text=True, capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
