"""Exercise the UI runner's deadlines and fail-closed pipeline without an SDK."""
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


class UiRunnerTest(unittest.TestCase):
    def run_runner(self, mode="pass", phase="suite"):
        with tempfile.TemporaryDirectory(prefix="ui runner ") as directory:
            root = Path(directory)
            (root / "ci").mkdir()
            (root / "ci/prepare-android-ui.py").write_text("pass\n")
            # Even failing evidence collection must not mask the test's status.
            (root / "ci/emulator-diagnostics.sh").write_text("exit 7\n")
            shutil.copy(CI / "check-instrumentation.py", root / "ci")
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
if "instrument" in args:
    suite = "notClass" in args
    mode = os.environ["MODE"] if suite == (os.environ["PHASE"] == "suite") else "pass"
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
                'adb() { "$UI_RUNNER_TEST_PYTHON" "$UI_RUNNER_TEST_FAKE" adb "$@"; }\n'
                'timeout() { "$UI_RUNNER_TEST_PYTHON" "$UI_RUNNER_TEST_FAKE" timeout "$@"; }\n',
                encoding="utf-8", newline="\n")
            env = dict(os.environ, BASH_ENV=shell_env.as_posix(),
                       RUNNER_TEMP=str(root / "output"), CALLS=str(root / "calls"), MODE=mode, PHASE=phase,
                       GITHUB_STEP_SUMMARY=str(root / "summary"),
                       UI_RUNNER_TEST_PYTHON=Path(sys.executable).as_posix(),
                       UI_RUNNER_TEST_FAKE=(root / "fake.py").as_posix())
            result = subprocess.run([BASH, str(CI / "run-android-ui-tests.sh")], cwd=root,
                                    env=env, text=True, capture_output=True, timeout=10)
            self.assertTrue((root / "calls").exists(), result.stdout + result.stderr)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            self.summary = (root / "summary").read_text()
            return result, calls

    def test_both_runs_have_individual_and_host_deadlines(self):
        result, calls = self.run_runner()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        runs = [call for call in calls if call[0] == "timeout" and "instrument" in call]
        self.assertEqual(2, len(runs))
        for call, host, per_test in zip(runs, ("180s", "1200s"), ("120000", "180000")):
            self.assertEqual(["--kill-after=15s", host], call[1:3])
            self.assertEqual(per_test, call[call.index("timeout_msec") + 1])
        self.assertEqual(2, result.stdout.count("1 passed; no failed or skipped tests"))
        self.assertIn("Android emulator verification: PASSED", self.summary)
        excluded = runs[1][runs[1].index("notClass") + 1]
        self.assertEqual("com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#"
                         "realSystemSaveDialogReturnsToChatAndCompletesTransfer", excluded)

    def test_exit_zero_test_failure_is_not_hidden_by_tee(self):
        result, _ = self.run_runner("failure")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Failed, skipped or unknown test status: -2", result.stderr)

    def test_app_defects_and_unrecovered_webview_crashes_fail_without_retries(self):
        for phase in ("cold", "suite"):
            for mode in ("app-crash", "app-anr", "failure", "webview-sigtrap", "adb-error", "timeout"):
                with self.subTest(phase=phase, mode=mode):
                    result, calls = self.run_runner(mode, phase)
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn(f"Android emulator verification: FAILED (exit {result.returncode})", self.summary)
                    self.assertIn("No app failure was retried or excused", self.summary)
                    runs = [call for call in calls if call[0] == "adb" and "instrument" in call]
                    self.assertEqual(1 if phase == "cold" else 2, len(runs))
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


if __name__ == "__main__":
    unittest.main()
