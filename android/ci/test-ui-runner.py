"""Exercise the UI runner's deadlines and fail-closed pipeline without an SDK."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

CI = Path(__file__).resolve().parent


class UiRunnerTest(unittest.TestCase):
    def run_runner(self, mode="pass"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "ci").mkdir()
            (root / "ci/prepare-android-ui.py").write_text("pass\n")
            (root / "ci/emulator-diagnostics.sh").write_text("exit 0\n")
            shutil.copy(CI / "check-instrumentation.py", root / "ci")
            fake = "#!" + sys.executable + "\n" + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps([Path(sys.argv[0]).name, *args]) + "\n")
if Path(sys.argv[0]).name == "timeout":
    while args[0].startswith("--"):
        args.pop(0)
    args.pop(0)  # Recorded host deadline; do not sleep in this self-test.
    os.execvp(args[0], args)
if "instrument" in args:
    suite = "notClass" in args
    mode = os.environ["MODE"] if suite else "pass"
    if mode == "timeout":
        sys.exit(124)
    for status in (1, -2 if mode == "failure" else 0):
        print("INSTRUMENTATION_STATUS: class=fixture.SmokeTest")
        print("INSTRUMENTATION_STATUS: test=fixture")
        print("INSTRUMENTATION_STATUS: numtests=1")
        print(f"INSTRUMENTATION_STATUS_CODE: {status}")
    print("OK (1 test)\nINSTRUMENTATION_CODE: -1")
'''
            for name in ("adb", "timeout"):
                executable = root / "bin" / name
                executable.write_text(fake)
                executable.chmod(0o755)
            env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                       RUNNER_TEMP=str(root / "output"), CALLS=str(root / "calls"), MODE=mode)
            result = subprocess.run(["bash", str(CI / "run-android-ui-tests.sh")], cwd=root,
                                    env=env, text=True, capture_output=True, timeout=10)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
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
        excluded = runs[1][runs[1].index("notClass") + 1]
        self.assertEqual("com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#"
                         "realSystemSaveDialogReturnsToChatAndCompletesTransfer", excluded)

    def test_exit_zero_test_failure_is_not_hidden_by_tee(self):
        result, _ = self.run_runner("failure")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Failed, skipped or unknown test status: -2", result.stderr)

    def test_host_timeout_is_not_hidden_by_tee_or_diagnostics(self):
        result, _ = self.run_runner("timeout")
        self.assertEqual(124, result.returncode)


if __name__ == "__main__":
    unittest.main()
