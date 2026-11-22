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
        self.assertIn('robolectricJars.filterKeys { it !in reused }.values.flatMap { it.resolve() }', script)
        self.assertIn('java.security.MessageDigest.getInstance("SHA-256")', script)
        self.assertIn('check(actual == expected)', script)
        # A restored CI copy is reused only when its bytes match the pin.
        self.assertIn('if (sha256(jar) == expected) {\n                reused[coordinate] = jar', script)
        self.assertLess(script.index('reused[coordinate] = jar'), script.index('copy {'))
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

    def test_pinned_jars_have_their_own_cache_saved_after_they_verify(self):
        # setup-java's shared Gradle key can be saved by a job that never
        # resolves these jars, so every run downloaded them (HTTP 429).
        script = (ROOT / "android/ci/robolectric.gradle.kts").read_text()
        self.assertIn('providers.environmentVariable("ROBOLECTRIC_JAR_CACHE")', script)
        workflow = yaml.load((ROOT / ".github/workflows/android.yml").read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["build"]
        path = "${{ github.workspace }}/.robolectric-jars"
        key = "robolectric-jars-${{ runner.os }}-${{ hashFiles('android/ci/robolectric.gradle.kts') }}"
        # Job-wide: every test task re-runs the preparation.
        self.assertEqual(job["env"]["ROBOLECTRIC_JAR_CACHE"], path)
        steps = job["steps"]
        restore = next(s for s in steps if s.get("uses") == "actions/cache/restore@v4")
        prepare = next(s for s in steps if s.get("name") == "Resolve pinned Robolectric dependencies")
        save = next(s for s in steps if s.get("uses") == "actions/cache/save@v4")
        self.assertEqual(steps.index(restore) + 1, steps.index(prepare))
        self.assertEqual(steps.index(prepare) + 1, steps.index(save))
        self.assertEqual(restore["with"], {"path": "${{ env.ROBOLECTRIC_JAR_CACHE }}", "key": key})
        self.assertEqual(save["with"], {"path": "${{ env.ROBOLECTRIC_JAR_CACHE }}",
                                        "key": "${{ steps.robolectric-jars.outputs.cache-primary-key }}"})
        self.assertEqual(restore["id"], "robolectric-jars")
        self.assertEqual(save["if"], "steps.robolectric-jars.outputs.cache-hit != 'true'")
        self.assertEqual(save["continue-on-error"], "true")
        # The signing workflow reads the same entry and never saves one.
        play = yaml.load((ROOT / ".github/workflows/android-play.yml").read_text(), Loader=yaml.BaseLoader)
        bundle = play["jobs"]["bundle"]["steps"]
        self.assertFalse([s for s in bundle if s.get("uses", "").startswith(("actions/cache@", "actions/cache/save@"))])
        play_restore = next(s for s in bundle if s.get("uses") == "actions/cache/restore@v4")
        build = next(s for s in bundle if s.get("name") == "Test and build signed bundle")
        self.assertEqual(play_restore["with"], {"path": path, "key": key})
        self.assertEqual(build["env"]["ROBOLECTRIC_JAR_CACHE"], path)
        self.assertLess(bundle.index(play_restore), bundle.index(build))
        # Only the android-all runtimes go there; nativeruntime stays in Gradle's cache.
        self.assertIn('coordinate.startsWith("android-all-instrumented:")', script)

    def test_android_tests_have_their_own_gradle_cache_key(self):
        # setup-java never re-saves an existing key; sharing the default key let the
        # minified build (no unit tests or lint) save it first.
        def setup_java(filename, job):
            steps = yaml.load((ROOT / ".github/workflows" / filename).read_text(),
                              Loader=yaml.BaseLoader)["jobs"][job]["steps"]
            return next(s for s in steps if s.get("uses", "").startswith("actions/setup-java@"))["with"]
        android = setup_java("android.yml", "build")
        self.assertEqual(android["cache"], "gradle")
        self.assertEqual(android["cache-dependency-path"].split(),
                         ["android/**/*.gradle*", "android/**/gradle-wrapper.properties", "android/gradle.properties"])
        for filename, job in (("ci-android-minified.yml", "build"), ("android-play.yml", "bundle")):
            other = setup_java(filename, job)
            self.assertNotEqual(other.get("cache-dependency-path"), android["cache-dependency-path"], filename)

    def test_pr_checks_also_run_unit_tests_with_push_on(self):
        # Release builds carry Firebase config (push on); PR builds do not.
        workflow = yaml.load((ROOT / ".github/workflows/android.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["build"]["steps"]
        push_on = next(s for s in steps if s.get("name") == "Unit tests with push enabled")
        self.assertNotIn("if", push_on)
        self.assertNotIn("continue-on-error", push_on)
        self.assertIn("-PpushEnabledDebug=true testDebugUnitTest", push_on["run"])
        app = (ROOT / "android/app/build.gradle.kts").read_text()
        self.assertIn('findProperty("pushEnabledDebug")', app)
        self.assertIn('if (pushEnabledDebug) {\n            getByName("debug").buildConfigField("boolean", "PUSH_ENABLED", "true")', app)


if __name__ == "__main__":
    unittest.main()
