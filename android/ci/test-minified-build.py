#!/usr/bin/env python3
"""SDK-free contracts for the minified release smoke (no secrets or emulator)."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / "android/ci"
spec = importlib.util.spec_from_file_location("minified", CI / "check-minified-build.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class MinifiedBuildTests(unittest.TestCase):
    def mapping(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.txt"
            path.write_text(text, encoding="utf-8")
            return checker.check_mapping(path)

    def test_real_obfuscation_is_required(self):
        self.assertEqual([("com.hobbycoders.aishuffle.model.Server", "a.b")], self.mapping(
            "com.hobbycoders.aishuffle.ui.ChatActivity -> com.hobbycoders.aishuffle.ui.ChatActivity:\n"
            "com.hobbycoders.aishuffle.model.Server -> a.b:\n    int value -> a\n"))

    def test_missing_empty_identity_and_fixture_only_mappings_fail(self):
        for text in ("", "com.hobbycoders.aishuffle.model.Server -> com.hobbycoders.aishuffle.model.Server:\n",
                     "com.hobbycoders.aishuffle.ci.Fixture -> a.b:\n", "okhttp3.Client -> a.b:\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.mapping(text)
        with self.assertRaises(FileNotFoundError):
            checker.check_mapping(CI / "no-such-mapping.txt")

    def test_smoke_is_a_separate_non_debuggable_release_variant(self):
        gradle = (ROOT / "android/app/build.gradle.kts").read_text()
        self.assertIn("isMinifyEnabled = true", gradle)
        self.assertIn("isShrinkResources = true", gradle)
        self.assertIn('initWith(getByName("release"))', gradle)
        self.assertIn('testBuildType = "releaseSmoke"', gradle)
        self.assertNotIn("isDebuggable = true", gradle)
        self.assertNotIn("testProguardFiles", gradle)
        rules = (ROOT / "android/app/proguard-rules.pro").read_text()
        self.assertNotIn("-dontobfuscate", rules)
        self.assertNotIn("-dontoptimize", rules)
        self.assertNotIn("-dontwarn **", rules)
        self.assertNotIn("-keep class com.hobbycoders.aishuffle.**", rules)
        manifest = ET.parse(ROOT / "android/app/src/releaseSmoke/AndroidManifest.xml").getroot()
        self.assertEqual("true", manifest.find("application").get("{http://schemas.android.com/apk/res/android}testOnly"))
        main = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text()
        self.assertNotIn("ReleaseSmokeFixtureActivity", main)

    def test_workflow_cannot_publish_and_tests_both_apis(self):
        workflow = yaml.load((ROOT / ".github/workflows/ci-android-minified.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual({"pull_request", "workflow_dispatch", "workflow_call"}, set(workflow["on"]))
        self.assertEqual({"contents": "read"}, workflow["permissions"])
        self.assertEqual({"build", "smoke"}, set(workflow["jobs"]))
        build, smoke = workflow["jobs"]["build"], workflow["jobs"]["smoke"]
        for job in (build, smoke):
            self.assertEqual("ubuntu-latest", job["runs-on"])
        self.assertEqual({"34", "36"}, {leg["api"] for leg in smoke["strategy"]["matrix"]["include"]})
        self.assertNotIn("continue-on-error", smoke)
        command = next(step["run"] for step in build["steps"] if step.get("name", "").startswith("Build production"))
        for required in ("bundleRelease", "assembleReleaseSmokeAndroidTest", "-PplayStoreBuild=true", "-PminifiedSmoke=true", "check-minified-build.py"):
            self.assertIn(required, command)
        source = (ROOT / ".github/workflows/ci-android-minified.yml").read_text()
        self.assertNotIn("secrets.", source)
        runner = (CI / "run-minified-smoke.sh").read_text()
        for required in ("set -euo pipefail", "releaseSmoke/app-releaseSmoke.apk", "releaseSmoke/app-releaseSmoke-androidTest.apk", "check-instrumentation.py", "300s", "exit \"$status\""):
            self.assertIn(required, runner)
        test = (ROOT / "android/app/src/releaseSmokeTest/java/com/hobbycoders/aishuffle/MinifiedReleaseSmokeTest.kt").read_text()
        self.assertNotIn("import com.hobbycoders.aishuffle.", test)
        self.assertNotIn("getDeclaredField", test)
        self.assertIn("shuffleShell.postMessage", test)
        self.assertIn("shuffleFileSave.postMessage", test)


if __name__ == "__main__":
    unittest.main()
