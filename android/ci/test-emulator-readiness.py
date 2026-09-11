import runpy
import tempfile
import unittest
from pathlib import Path

module = runpy.run_path(str(Path(__file__).with_name("prepare-android-ui.py")))
readiness, prepare = module["readiness"], module["prepare"]

HOME = "mCurrentFocus=Window{18f6feb u0 com.android.launcher3/com.android.launcher3.uioverrides.QuickstepLauncher}\n"
LAUNCHER_ANR = "mCurrentFocus=Window{563c1f5 u0 Application Not Responding: com.android.launcher3}\n"
UNLOCKED, AWAKE = "mInputRestricted=false\n", "mWakefulness=Awake\n"


class EmulatorReadinessTest(unittest.TestCase):
    def test_requires_launcher_focus_awake_and_unlocked(self):
        self.assertEqual("ready", readiness(HOME, UNLOCKED, AWAKE))
        for display, policy, power in (("mCurrentFocus=null", UNLOCKED, AWAKE),
                                       (HOME, "mInputRestricted=true", AWAKE),
                                       (HOME, UNLOCKED, "mWakefulness=Asleep"),
                                       (HOME, "", AWAKE),
                                       (HOME.replace("com.android.launcher3/", "com.android.settings/"), UNLOCKED, AWAKE)):
            with self.subTest(display=display, policy=policy, power=power):
                self.assertEqual("waiting", readiness(display, policy, power))

    def test_only_exact_launcher_anr_is_recoverable(self):
        self.assertEqual("launcher_anr", readiness(LAUNCHER_ANR, UNLOCKED, AWAKE))
        for package in ("com.hobbycoders.aishuffle", "com.android.systemui", "com.android.launcher3.other"):
            self.assertEqual("other_error", readiness(LAUNCHER_ANR.replace("com.android.launcher3", package), UNLOCKED, AWAKE))
        self.assertEqual("other_error", readiness(LAUNCHER_ANR.replace("Application Not Responding", "Application Error"), UNLOCKED, AWAKE))

    def test_google_api36_launcher_is_ready_but_not_anr_recoverable(self):
        pixel = HOME.replace("com.android.launcher3", "com.google.android.apps.nexuslauncher")
        self.assertEqual("ready", readiness(pixel, UNLOCKED, AWAKE))
        self.assertEqual("waiting", readiness(pixel, "mInputRestricted=true", AWAKE))
        self.assertEqual("waiting", readiness(pixel, UNLOCKED, "mWakefulness=Asleep"))
        self.assertEqual("waiting", readiness(pixel.replace("nexuslauncher/", "nexuslauncher.other/"), UNLOCKED, AWAKE))
        self.assertEqual("waiting", readiness(pixel + HOME, UNLOCKED, AWAKE))
        self.run_preparation([pixel])
        self.assertFalse(any("force-stop" in command for command in self.commands))
        pixel_anr = LAUNCHER_ANR.replace("com.android.launcher3", "com.google.android.apps.nexuslauncher")
        with self.assertRaisesRegex(RuntimeError, "refusing recovery"):
            self.run_preparation([pixel_anr])
        self.assertFalse(any("force-stop" in command for command in self.commands))

    def test_full_dump_with_historical_focus_is_not_ready(self):
        self.assertEqual("waiting", readiness(HOME + LAUNCHER_ANR, UNLOCKED, AWAKE))

    def run_preparation(self, snapshots):
        self.commands = []
        self.diagnostics = 0
        self.elapsed = 0
        iterator = iter(snapshots)
        current = None

        def adb(*args):
            nonlocal current
            self.commands.append(args)
            if args == ("shell", "dumpsys", "window", "displays"):
                current = next(iterator, current)
                return current
            if args == ("shell", "dumpsys", "window", "policy"):
                return UNLOCKED
            if args == ("shell", "dumpsys", "power"):
                return AWAKE
            return ""

        def sleep(seconds):
            self.elapsed += seconds

        def diagnostics():
            self.diagnostics += 1

        with tempfile.TemporaryDirectory() as folder:
            prepare(adb, Path(folder), deadline_seconds=12, sleep=sleep, clock=lambda: self.elapsed,
                    diagnostics=diagnostics)

    def test_normal_boot_never_restarts_any_application(self):
        self.run_preparation([HOME])
        self.assertFalse(any("force-stop" in command for command in self.commands))
        self.assertEqual(0, self.diagnostics)
        self.assertEqual(4, self.elapsed)

    def test_launcher_boot_anr_gets_one_restart_then_all_readiness_checks(self):
        self.run_preparation([LAUNCHER_ANR, "mCurrentFocus=null", HOME])
        self.assertEqual([("shell", "am", "force-stop", "com.android.launcher3")],
                         [command for command in self.commands if "force-stop" in command])
        self.assertEqual(1, self.diagnostics)
        self.assertEqual(8, self.elapsed)

    def test_repeated_launcher_anr_fails_without_repeated_recovery(self):
        with self.assertRaisesRegex(RuntimeError, "hung again"):
            self.run_preparation([LAUNCHER_ANR])
        self.assertEqual(1, sum("force-stop" in command for command in self.commands))

    def test_unexpected_app_anr_is_never_dismissed(self):
        with self.assertRaisesRegex(RuntimeError, "refusing recovery"):
            self.run_preparation([LAUNCHER_ANR.replace("com.android.launcher3", "com.hobbycoders.aishuffle")])
        self.assertFalse(any("force-stop" in command for command in self.commands))

    def test_lost_focus_restarts_stability_count_and_eventually_times_out(self):
        with self.assertRaisesRegex(RuntimeError, "never reached"):
            self.run_preparation([HOME, HOME, "mCurrentFocus=null", HOME, HOME, "mCurrentFocus=null"])


if __name__ == "__main__":
    unittest.main()
