#!/usr/bin/env python3
"""SDK-free contracts for hermetic Robolectric SDK resolution."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class RobolectricDependenciesTests(unittest.TestCase):
    def test_every_unit_test_uses_verified_local_jars(self):
        app = (ROOT / "android/app/build.gradle.kts").read_text()
        self.assertIn('apply(from = "../ci/robolectric.gradle.kts")', app)
        script = (ROOT / "android/ci/robolectric.gradle.kts").read_text()
        self.assertIn('tasks.withType<Test>().configureEach', script)
        self.assertIn('dependsOn(prepareRobolectricDependencies)', script)
        self.assertIn('systemProperty("robolectric.offline", "true")', script)
        self.assertIn('systemProperty("robolectric.dependency.dir", robolectricDependencyDir', script)
        self.assertIn('isTransitive = false', script)
        self.assertIn('configurations.detachedConfiguration', script)
        self.assertIn('robolectricJars.flatMap { it.resolve() }', script)
        self.assertIn('java.security.MessageDigest.getInstance("SHA-256")', script)
        self.assertIn('check(actual == expected)', script)
        self.assertIn('TestExceptionFormat.FULL', script)
        self.assertIn('showExceptions = true', script)
        self.assertIn('showCauses = true', script)
        # Validation must happen on cache hits too, not just on downloads.
        self.assertNotIn('outputs.', script)
        self.assertLess(script.index('check(actual == expected)'), script.index('copy {'))

    def test_pins_cover_all_test_sdks_and_the_compile_sdk(self):
        script = (ROOT / "android/ci/robolectric.gradle.kts").read_text()
        pins = dict(re.findall(r'"([^" ]+:[^" ]+)" to "([a-f0-9]{64})"', script))
        versions = {28: "9-robolectric-4913185-2-i7", 34: "14-robolectric-10818077-i7",
                    35: "15-robolectric-13954326-i7", 36: "16-robolectric-13921718-i7"}
        app = (ROOT / "android/app/build.gradle.kts").read_text()
        sdks = {int(re.search(r'compileSdk = (\d+)', app)[1])}
        for path in (ROOT / "android/app/src/test").rglob("*.kt"):
            for values in re.findall(r'sdk\s*=\s*\[([\d,\s]+)\]', path.read_text()):
                sdks.update(map(int, re.findall(r'\d+', values)))
        self.assertEqual(set(pins), {f"android-all-instrumented:{versions[sdk]}" for sdk in sdks}
                         | {"nativeruntime-dist-compat:1.0.18"})
        self.assertIn('testImplementation("org.robolectric:robolectric:4.16.1")', app)

    def test_only_dependency_downloads_get_the_longer_retry_budget(self):
        workflow = yaml.load((ROOT / ".github/workflows/android.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["build"]["steps"]
        prepare = next(s for s in steps if s.get("name") == "Resolve pinned Robolectric dependencies")
        tests = next(s for s in steps if s.get("name") == "Unit tests and Android lint")
        self.assertLess(steps.index(prepare), steps.index(tests))
        self.assertNotIn("if", prepare)
        self.assertIn(":app:prepareRobolectricDependencies", prepare["run"])
        # Root systemProp settings reach every Gradle daemon, including the
        # classpath downloads in debug tests/lint and signed Play bundle builds.
        properties = (ROOT / "android/gradle.properties").read_text()
        self.assertRegex(properties, r"(?m)^systemProp\.org\.gradle\.internal\.repository\.max\.tentatives=5$")
        self.assertRegex(properties, r"(?m)^systemProp\.org\.gradle\.internal\.repository\.initial\.backoff=10000$")
        script = (ROOT / "android/ci/robolectric.gradle.kts").read_text()
        self.assertIn('System.getProperty("org.gradle.internal.repository.max.tentatives")', script)
        self.assertIn('System.getProperty("org.gradle.internal.repository.initial.backoff")', script)
        self.assertIn("Gradle dependency-download policy (daemon):", script)
        play = (ROOT / ".github/workflows/android-play.yml").read_text()
        self.assertIn("testDebugUnitTest lintDebug bundleRelease", play)
        self.assertNotIn("testDebugUnitTest", prepare["run"])
        self.assertEqual(tests["run"].count("testDebugUnitTest lintDebug"), 1)
        self.assertIn("--info", tests["run"])  # Logs actual test-JVM properties and transfers.
        self.assertIn('run([ctx.python, "android/ci/test-robolectric-dependencies.py"])',
                      (ROOT / "scripts/local_ci.py").read_text())
        for step in (prepare, tests):
            self.assertNotIn("continue-on-error", step)
            self.assertNotIn("||", step["run"])
            self.assertNotIn("retry", step.get("uses", ""))


if __name__ == "__main__":
    unittest.main()
