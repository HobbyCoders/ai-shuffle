"""Exercise emulator runner permissions, deadlines and fail-closed pipelines without an SDK."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

CI = Path(__file__).resolve().parent
# Match test-publish-apk.py: PATH's bash.exe can be the unconfigured WSL launcher.
BASH = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe") if os.name == "nt" else "bash"
ROOT = CI.parents[1]


def fake_proc(proc, qemu):
    """A minimal /proc: a shell, a zombie (empty cmdline) and optionally the emulator."""
    for pid, cmdline in (("1", b"/bin/bash\0-l\0"), ("2", b""), ("3", b"python3\0qemu-system-x86_64\0")):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(cmdline)
    if qemu:
        (proc / "40").mkdir()
        (proc / "40/cmdline").write_bytes(b"/sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64\0-avd\0test\0")
    (proc / "self").mkdir()


def passing(name="fixture"):
    return (f"INSTRUMENTATION_STATUS: class=fixture.SmokeTest\nINSTRUMENTATION_STATUS: test={name}\n"
            "INSTRUMENTATION_STATUS: numtests=1\nINSTRUMENTATION_STATUS_CODE: 1\n"
            f"INSTRUMENTATION_STATUS: class=fixture.SmokeTest\nINSTRUMENTATION_STATUS: test={name}\n"
            "INSTRUMENTATION_STATUS: numtests=1\nINSTRUMENTATION_STATUS_CODE: 0\n"
            "OK (1 test)\nINSTRUMENTATION_CODE: -1\n")


class UiRunnerTest(unittest.TestCase):
    def run_runner(self, mode="pass", phase="suite", runner="run-android-ui-tests.sh", api=34,
                   qemu=True, adb_state=None):
        with tempfile.TemporaryDirectory(prefix="ui runner ") as directory:
            root = Path(directory)
            (root / "ci").mkdir()
            (root / "ci/prepare-android-ui.py").write_text("pass\n")
            # Even failing evidence collection must not mask the test's status.
            (root / "ci/emulator-diagnostics.sh").write_text('printf "%s" "$1" > "$DIAGNOSTIC_PHASE"\nexit 7\n')
            shutil.copy(CI / "check-instrumentation.py", root / "ci")
            shutil.copy(CI / "emulator-loss.py", root / "ci")
            fake_proc(root / "proc", qemu)
            fake = r'''
import json, os, subprocess, sys
name, *args = sys.argv[1:]
assert name in ("adb", "timeout"), name
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "timeout":
    while args[0].startswith("--"):
        args.pop(0)
    args.pop(0)  # Recorded host deadline; do not sleep in this self-test.
    # Run the requested fake command with this interpreter, not Windows execvp
    # (which cannot execute an extensionless POSIX shebang script).
    sys.exit(subprocess.call([sys.executable, __file__, *args]))
if args == ["-s", "emulator-5554", "get-state"]:
    print(os.environ["ADB_STATE"])
if args ==["-s", "emulator-5554", "shell", "getprop", "ro.build.version.sdk"]:
    if os.environ["MODE"] == "sdk-error":
        sys.exit(19)
    print(os.environ["API_LEVEL"] + "\r")
if "grant" in args and os.environ["MODE"] == "grant-error":
    sys.exit(23)
if "instrument" in args:
    phase = ("suite" if "notClass" in args else
             "allowing-launcher" if any("#allowingLauncher" in arg for arg in args) else
             "denying-launcher" if any("#denyingLauncher" in arg for arg in args) else
             "allowing" if any("#allowingStartup" in arg for arg in args) else
             "denying" if any("#denyingStartup" in arg for arg in args) else
             "minified" if any("MinifiedReleaseSmokeTest" in arg for arg in args) else "cold")
    mode = os.environ["MODE"] if phase == os.environ["PHASE"] else "pass"
    if mode == "timeout":
        sys.exit(124)
    if mode == "adb-error":
        sys.exit(1)
    if mode in ("app-crash", "app-anr", "webview-sigtrap"):
        if mode == "webview-sigtrap":
            print("SIGTRAP Chrome_IOThread [FATAL:partition_alloc_support.cc(770)] Detected dangling raw_ptr in unretained")
        print("INSTRUMENTATION_RESULT: shortMsg=" + ("Application Not Responding: com.hobbycoders.aishuffle" if mode == "app-anr" else "Process crashed."))
        print("INSTRUMENTATION_CODE: 0")
        sys.exit(0)  # adb often exits zero even when the app dies.
    for status in (1, -2 if mode == "failure" else 0):
        print("INSTRUMENTATION_STATUS: class=fixture.SmokeTest")
        print("INSTRUMENTATION_STATUS: test=fixture")
        print("INSTRUMENTATION_STATUS: numtests=1")
        print(f"INSTRUMENTATION_STATUS_CODE: {status}")
    print("OK (1 test)\nINSTRUMENTATION_CODE: -1")
'''
            (root / "fake.py").write_text(fake, encoding="utf-8")
            # BASH_ENV installs test-only functions before the real runner.
            # Git Bash can prefer a host timeout.exe over an extensionless PATH
            # shim; functions make both commands deterministic on every host.
            # Explicitly launch Python instead of using a native Windows path
            # as a POSIX shebang, and keep spaces in paths quoted.
            shell_env = root / "fake-tools.sh"
            shell_env.write_text(
                'python3() { "$UI_RUNNER_TEST_PYTHON" "$@"; }\n'
                'adb() { "$UI_RUNNER_TEST_PYTHON" "$UI_RUNNER_TEST_FAKE" adb "$@"; }\n'
                'timeout() { "$UI_RUNNER_TEST_PYTHON" "$UI_RUNNER_TEST_FAKE" timeout "$@"; }\n',
                encoding="utf-8", newline="\n")
            env = dict(os.environ, BASH_ENV=shell_env.as_posix(),
                       RUNNER_TEMP=str(root / "output"), CALLS=str(root / "calls"), MODE=mode, PHASE=phase,
                       API_LEVEL=str(api), DIAGNOSTIC_PHASE=str(root / "diagnostic-phase"),
                       GITHUB_STEP_SUMMARY=str(root / "summary"),
                       EMULATOR_LOSS_PROC=str(root / "proc"),
                       ADB_STATE=("device" if qemu else "") if adb_state is None else adb_state,
                       UI_RUNNER_TEST_PYTHON=Path(sys.executable).as_posix(),
                       UI_RUNNER_TEST_FAKE=(root / "fake.py").as_posix())
            result = subprocess.run([BASH, str(CI / runner)], cwd=root,
                                    env=env, text=True, capture_output=True, timeout=10)
            self.assertTrue((root / "calls").exists(), result.stdout + result.stderr)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            marker = root / "output/android-emulator/emulator-lost"
            self.marker = marker.read_text() if marker.exists() else None
            self.summary = (root / "summary").read_text() if runner == "run-android-ui-tests.sh" else ""
            self.diagnostic_phase = (root / "diagnostic-phase").read_text() if (root / "diagnostic-phase").exists() else ""
            return result, calls

    def test_notifications_are_granted_after_install_and_before_launch_on_android_13_plus(self):
        for runner in ("run-android-ui-tests.sh", "run-minified-smoke.sh"):
            for api in (32, 33, 34, 36):
                with self.subTest(runner=runner, api=api):
                    result, calls = self.run_runner(runner=runner, api=api)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    grants = [i for i, call in enumerate(calls) if "grant" in call]
                    if api < 33:
                        self.assertEqual([], grants)
                        continue
                    self.assertEqual(1, len(grants))
                    self.assertEqual(["adb", "-s", "emulator-5554", "shell", "pm", "grant",
                                      "com.hobbycoders.aishuffle", "android.permission.POST_NOTIFICATIONS"],
                                     calls[grants[0]])
                    installs = [i for i, call in enumerate(calls) if "install" in call and
                                not any("notification-instrumentation-apks/" in arg for arg in call)]
                    self.assertEqual(2, len(installs))
                    self.assertLess(max(installs), grants[0])
                    launches = [i for i, call in enumerate(calls) if "am" in call]
                    self.assertTrue(launches)
                    self.assertLess(grants[0], min(launches))

    def test_notification_grant_failure_stops_both_runners_before_launch(self):
        for runner in ("run-android-ui-tests.sh", "run-minified-smoke.sh"):
            with self.subTest(runner=runner):
                result, calls = self.run_runner("grant-error", runner=runner)
                self.assertEqual(23, result.returncode, result.stdout + result.stderr)
                self.assertFalse(any("am" in call for call in calls))

    def test_sdk_query_failure_stops_both_runners_before_launch(self):
        for runner in ("run-android-ui-tests.sh", "run-minified-smoke.sh"):
            with self.subTest(runner=runner):
                result, calls = self.run_runner("sdk-error", runner=runner)
                self.assertEqual(19, result.returncode, result.stdout + result.stderr)
                self.assertFalse(any("grant" in call or "am" in call for call in calls))

    def test_all_runs_have_individual_and_host_deadlines(self):
        result, calls = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        runs = [call for call in calls if call[0] == "timeout" and "instrument" in call]
        self.assertEqual(6, len(runs))
        for call, host, per_test in zip(runs, ("180s", "1200s", "180s", "180s", "180s", "180s"),
                                         ("120000", "180000", "120000", "120000", "120000", "120000")):
            self.assertEqual(["--kill-after=15s", host], call[1:3])
            self.assertEqual(per_test, call[call.index("timeout_msec") + 1])
        self.assertEqual(6, result.stdout.count("1 passed; no failed or skipped tests"))
        self.assertIn("Android emulator verification: PASSED", self.summary)
        excluded = runs[1][runs[1].index("notClass") + 1]
        self.assertEqual("com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#"
                         "realSystemSaveDialogReturnsToChatAndCompletesTransfer", excluded)

    def test_notification_decisions_start_clean_without_pregrant_or_retries(self):
        result, calls = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        smoke_installs = [i for i, call in enumerate(calls) if "install" in call and
                          any("notification-instrumentation-apks/" in arg for arg in call)]
        self.assertEqual(2, len(smoke_installs))
        smoke_calls = calls[max(smoke_installs) + 1:]
        self.assertFalse(any("grant" in call for call in smoke_calls))
        self.assertEqual(4, sum("clear" in call for call in smoke_calls))
        self.assertEqual(4, sum("clear-permission-flags" in call for call in smoke_calls))
        runs = [call for call in smoke_calls if call[0] == "adb" and "instrument" in call]
        self.assertEqual(["allowingStartup", "denyingStartup", "allowingLauncher", "denyingLauncher"],
                         [next(word for word in ("allowingStartup", "denyingStartup", "allowingLauncher", "denyingLauncher")
                               if any("#" + word in arg for arg in call)) for call in runs])

    def test_exit_zero_test_failure_is_not_hidden_by_tee(self):
        result, _ = self.run_runner("failure")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Failed, skipped or unknown test status: -2", result.stderr)

    def test_app_defects_and_unrecovered_webview_crashes_fail_without_retries(self):
        for phase in ("cold", "suite", "allowing", "denying", "allowing-launcher", "denying-launcher"):
            for mode in ("app-crash", "app-anr", "failure", "webview-sigtrap", "adb-error", "timeout"):
                with self.subTest(phase=phase, mode=mode):
                    result, calls = self.run_runner(mode, phase)
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn(f"Android emulator verification: FAILED (exit {result.returncode})", self.summary)
                    self.assertIn("No app failure was retried or excused", self.summary)
                    # The emulator is still running: never a retryable emulator loss.
                    self.assertIsNone(self.marker)
                    runs = [call for call in calls if call[0] == "adb" and "instrument" in call]
                    self.assertEqual({"cold": 1, "suite": 2, "allowing": 3, "denying": 4,
                                      "allowing-launcher": 5, "denying-launcher": 6}[phase], len(runs))
                    if mode in ("app-crash", "app-anr", "webview-sigtrap"):
                        self.assertIn("Incomplete or unsuccessful instrumentation run", result.stderr)

    def test_all_webview_destroy_sites_detach_first(self):
        # Cheap source guard for the two production WebView owners. Require any
        # future destroy() site to spell out its receiver and detach immediately
        # before teardown; comments are not evidence of detachment.
        count = 0
        for path in (CI.parent / "app/src/main").rglob("*.kt"):
            source = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.DOTALL)
            source = re.sub(r"//[^\n]*", "", source)
            for call in re.finditer(r"(?:(\w+)\.)?\bdestroy\s*\(\s*\)", source):
                count += 1
                with self.subTest(path=path.name):
                    self.assertIsNotNone(call[1], "Use an explicit WebView receiver for the teardown guard")
                    receiver = re.escape(call[1])
                    detached = (rf"\({receiver}\.parent as\? ViewGroup\)\?\.removeView\({receiver}\)\s*"
                                rf"(?:{receiver}\.(?:stopLoading|clearHistory)\(\);\s*)*$")
                    self.assertRegex(source[:call.start()], detached)
        self.assertGreaterEqual(count, 2, "The WebView owners must remain covered")

    def test_host_timeout_is_not_hidden_by_tee_or_diagnostics(self):
        result, _ = self.run_runner("timeout")
        self.assertEqual(124, result.returncode)

    def test_vanished_emulator_after_a_passing_run_is_marked_for_one_workflow_retry(self):
        # Runs 42746205944 and 42737300656: adb loses the device right after "OK".
        for phase, after in (("cold", "before any instrumentation result passed"),
                             ("suite", "after cold-chat.txt passed"), ("allowing", "after runner.txt passed")):
            with self.subTest(phase=phase):
                result, calls = self.run_runner("adb-error", phase, qemu=False)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIsNotNone(self.marker, result.stdout + result.stderr)
                self.assertIn(f"Emulator process disappeared {after}", self.marker)
                self.assertIn("not an app failure", self.marker)
                self.assertIn("::warning::Emulator process disappeared", result.stdout)
                # The script itself never re-runs anything.
                self.assertEqual({"cold": 1, "suite": 2, "allowing": 3}[phase],
                                 sum(call[0] == "adb" and "instrument" in call for call in calls))
                self.assertIn("No app failure was retried or excused", self.summary)
                # Loss is decided before the slower evidence pulls.
                get_state = calls.index(["timeout", "10", "adb", "-s", "emulator-5554", "get-state"])
                logcat = next(i for i, call in enumerate(calls) if "logcat" in call)
                self.assertLess(get_state, logcat)

    def test_app_failures_are_never_marked_even_when_the_emulator_is_gone(self):
        for mode in ("failure", "app-crash", "app-anr", "webview-sigtrap"):
            with self.subTest(mode=mode):
                result, _ = self.run_runner(mode, "suite", qemu=False)
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(self.marker, result.stdout)
                self.assertIn("runner.txt shows a test failure", result.stdout)

    def test_live_or_still_attached_emulator_is_never_marked(self):
        for mode in ("adb-error", "timeout"):
            for qemu, state in ((True, "device"), (True, "offline"), (True, ""), (False, "device")):
                with self.subTest(mode=mode, qemu=qemu, state=state):
                    result, _ = self.run_runner(mode, "suite", qemu=qemu, adb_state=state)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIsNone(self.marker, result.stdout)

    def test_success_does_not_probe_for_emulator_loss(self):
        result, calls = self.run_runner(qemu=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIsNone(self.marker)
        self.assertFalse(any("get-state" in call for call in calls))

    def test_minified_failures_request_a_supported_diagnostic_phase(self):
        result, calls = self.run_runner("app-crash", phase="minified", runner="run-minified-smoke.sh")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("test-failure", self.diagnostic_phase)
        self.assertEqual(1, sum(call[0] == "adb" and "instrument" in call for call in calls))
        self.assertIn("Incomplete or unsuccessful instrumentation run", result.stderr)


class EmulatorLossTest(unittest.TestCase):
    """The marker decides the workflow's only retry; it must fail closed."""

    def setUp(self):
        import runpy
        self.loss = runpy.run_path(str(CI / "emulator-loss.py"))
        directory = tempfile.TemporaryDirectory(prefix="emulator loss ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "android-emulator"
        (self.root / "instrumentation").mkdir(parents=True)
        self.proc = Path(directory.name) / "proc"

    def decide(self, qemu=False, state="", **results):
        fake_proc(self.proc, qemu)
        for index, (name, text) in enumerate(results.items()):
            path = self.root / "instrumentation" / f"{name}.txt"
            path.write_text(text)
            os.utime(path, ns=(index * 10**9, index * 10**9))
        return self.loss["decide"](self.root, self.proc, state)

    def test_running_emulator_is_never_lost(self):
        for state in ("device", "offline", ""):
            with self.subTest(state=state):
                shutil.rmtree(self.proc, ignore_errors=True)
                self.assertEqual((False, "emulator process is still running"), self.decide(True, state))

    def test_missing_process_table_is_never_lost(self):
        self.assertFalse(self.loss["decide"](self.root, self.proc / "absent", "")[0])

    def test_adb_device_without_a_visible_emulator_process_is_not_lost(self):
        self.assertFalse(self.decide(state="device")[0])

    def test_gone_emulator_after_passing_and_cut_off_results_is_lost(self):
        cut = passing("second").split("OK (1 test)")[0]
        lost, reason = self.decide(**{"cold-chat": passing(), "runner": cut})
        self.assertTrue(lost)
        self.assertEqual("Emulator process disappeared after cold-chat.txt passed; "
                         "this is a virtual-device failure, not an app failure.", reason)

    def test_any_reported_test_failure_is_an_app_failure(self):
        good = passing()
        failures = {
            "failed status": good.replace("STATUS_CODE: 0", "STATUS_CODE: -2"),
            "skipped status": good.replace("STATUS_CODE: 0", "STATUS_CODE: -3"),
            "failures": good + "FAILURES!!!\n",
            "crash": "INSTRUMENTATION_RESULT: shortMsg=Process crashed.\nINSTRUMENTATION_CODE: 0\n",
            "anr": "INSTRUMENTATION_RESULT: shortMsg=Application Not Responding\n",
            "failed instrumentation": "INSTRUMENTATION_FAILED: com.hobbycoders.aishuffle.test\n",
            # Finished but rejected by the strict parser (wrong count, no OK line).
            "short result": good.replace("numtests=1", "numtests=2"),
            "terminal without summary": good.replace("OK (1 test)\n", ""),
        }
        for label, text in failures.items():
            with self.subTest(label=label):
                shutil.rmtree(self.proc, ignore_errors=True)
                lost, reason = self.decide(**{"cold-chat": good, "runner": text})
                self.assertFalse(lost)
                self.assertEqual("runner.txt shows a test failure", reason)

    def test_timing_logcat_is_not_an_instrumentation_result(self):
        lost, _ = self.decide(**{"cold-chat": passing(), "timing": "W Process crashed in some other log"})
        self.assertTrue(lost)

    def test_cli_writes_and_clears_the_marker(self):
        marker = self.root / "emulator-lost"
        fake_proc(self.proc, qemu=False)
        argv = ["--root", str(self.root), "--proc", str(self.proc), "--adb-state", ""]
        self.assertEqual(0, self.loss["main"](argv))
        self.assertIn("virtual-device failure", marker.read_text())
        (self.proc / "40").mkdir()
        (self.proc / "40/cmdline").write_bytes(b"qemu-system-x86_64-headless\0")
        self.assertEqual(0, self.loss["main"](argv))
        self.assertFalse(marker.exists())


class EmulatorRetryWorkflowTest(unittest.TestCase):
    """android.yml retries only a marked emulator loss, once, and gates on the result."""

    @classmethod
    def setUpClass(cls):
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/android.yml").read_text(), Loader=yaml.BaseLoader)
        cls.steps = workflow["jobs"]["android-ui"]["steps"]
        cls.by_id = {step["id"]: step for step in cls.steps if "id" in step}

    def bash(self, script, **env):
        return subprocess.run([BASH, "-e", "-c", script], env=dict(os.environ, **env),
                              text=True, capture_output=True, timeout=10)

    def test_exactly_one_identical_retry_only_after_a_marked_loss(self):
        runners = [step for step in self.steps if step.get("uses", "").startswith("reactivecircus/android-emulator-runner@")]
        self.assertEqual(["emulator_tests", "emulator_tests_retry"], [step["id"] for step in runners])
        first, retry = runners
        self.assertEqual(first["uses"], retry["uses"])
        self.assertEqual(first["with"], retry["with"])
        self.assertEqual(first["env"], retry["env"])
        self.assertEqual("true", first["continue-on-error"])
        self.assertNotIn("continue-on-error", retry)
        condition = "steps.emulator_tests.outcome == 'failure' && steps.emulator_loss.outputs.lost == 'true'"
        self.assertEqual(condition, retry["if"])
        prepare = self.steps[self.steps.index(retry) - 1]
        self.assertEqual(condition, prepare["if"])
        order = [self.steps.index(self.by_id[name]) for name in
                 ("emulator_tests", "emulator_loss", "emulator_tests_retry", "emulator_result")]
        self.assertEqual(sorted(order), order)
        self.assertEqual("always()", self.by_id["emulator_loss"]["if"])
        self.assertEqual("always()", self.by_id["emulator_result"]["if"])
        # Nothing re-runs the retry.
        self.assertFalse(any("emulator_tests_retry" in step.get("if", "") for step in self.steps))

    def test_retry_rebuilds_the_preflight_avd_and_keeps_first_attempt_evidence(self):
        prepare = self.steps[self.steps.index(self.by_id["emulator_tests_retry"]) - 1]["run"]
        preflight = next(step["run"] for step in self.steps
                         if step.get("name") == "Prepare AVD and check disk space before emulator startup")
        for line in ("avdmanager create avd", "disk.dataPartition.size", "emulator-storage.sh check"):
            ours = [entry for entry in prepare.splitlines() if line in entry]
            self.assertEqual([entry for entry in preflight.splitlines() if line in entry], ours)
        with tempfile.TemporaryDirectory(prefix="retry evidence ") as directory:
            root = Path(directory) / "android-emulator"
            for name in ("emulator.log", "emulator-lost", "instrumentation/runner.txt", "before-boot/disk.txt"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(name)
            # Run the step's own evidence lines (the AVD lines need an SDK).
            lines = [line for line in prepare.splitlines() if line.startswith("root=") or "first-attempt" in line]
            self.assertEqual(3, len(lines))
            result = self.bash("\n".join(lines), RUNNER_TEMP=directory)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["first-attempt"], sorted(path.name for path in root.iterdir()))
            for name in ("emulator.log", "emulator-lost", "instrumentation/runner.txt", "before-boot/disk.txt"):
                self.assertEqual(name, (root / "first-attempt" / name).read_text())

    def test_loss_output_requires_a_failed_first_attempt_and_the_marker(self):
        run = self.by_id["emulator_loss"]["run"]
        for first in ("success", "failure", "cancelled", "skipped"):
            for marked in (False, True):
                with self.subTest(first=first, marked=marked), tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / "android-emulator").mkdir()
                    if marked:
                        (Path(directory) / "android-emulator/emulator-lost").write_text("gone\n")
                    output = Path(directory) / "output"
                    result = self.bash(run, FIRST=first, RUNNER_TEMP=directory, GITHUB_OUTPUT=str(output))
                    self.assertEqual(0, result.returncode, result.stderr)
                    expected = "true" if first == "failure" and marked else "false"
                    self.assertEqual(f"lost={expected}\n", output.read_text())

    def test_gate_passes_only_on_first_success_or_a_successful_retry_after_loss(self):
        run = self.by_id["emulator_result"]["run"]
        outcomes = ("success", "failure", "cancelled", "skipped", "")
        for first in outcomes:
            for lost in ("true", "false", ""):
                for retry in outcomes:
                    with self.subTest(first=first, lost=lost, retry=retry):
                        result = self.bash(run, FIRST=first, LOST=lost, RETRY=retry)
                        passed = first == "success" or (lost == "true" and retry == "success")
                        self.assertEqual(0 if passed else 1, result.returncode, result.stdout + result.stderr)

    def test_failure_evidence_and_summary_cover_a_recovered_loss(self):
        reports = next(step for step in self.steps if step.get("name") == "Upload test reports")
        self.assertEqual("failure() || steps.emulator_tests.outcome == 'failure'", reports["if"])
        summary = next(step for step in self.steps if step.get("name") == "Report emulator leg outcome")
        self.assertEqual("${{ steps.emulator_loss.outputs.lost }}", summary["env"]["LOST"])
        self.assertEqual("${{ steps.emulator_tests_retry.outcome }}", summary["env"]["RETRY"])
        self.assertIn("A vanished emulator process is retried once on a fresh emulator; "
                      "app and test failures are never retried or excused.", summary["run"])


if __name__ == "__main__":
    unittest.main()
