from contextlib import redirect_stdout
import io
import json
import re
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

module = runpy.run_path(str(Path(__file__).with_name("prepare-android-ui.py")))
readiness, prepare, configure_webview = module["readiness"], module["prepare"], module["configure_webview"]

HOME = "mCurrentFocus=Window{18f6feb u0 com.android.launcher3/com.android.launcher3.uioverrides.QuickstepLauncher}\n"
LAUNCHER_ANR = "mCurrentFocus=Window{563c1f5 u0 Application Not Responding: com.android.launcher3}\n"
UNLOCKED, AWAKE = "mInputRestricted=false\n", "mWakefulness=Awake\n"
CAPTURE = json.loads((Path(__file__).parent / "fixtures/api36-webview133.json").read_text())
SDK = re.fullmatch(r"\[ro.build.version.sdk\]: \[(.+)\]", CAPTURE["sdk_property"])[1]
BUILD_ID = re.fullmatch(r"\[ro.build.id\]: \[(.+)\]", CAPTURE["build_id_property"])[1]
PACKAGE, VERSION = re.search(r"Loading (\S+) version (\S+)", CAPTURE["webview_factory"]).groups()
# The run captured getprop and WebViewFactory logcat, NOT dumpsys webviewupdate.
# This dump-format sample is synthetic; the version/build match is real evidence.
SYNTHETIC_PROVIDER = f"Current WebView package (name, version): ({PACKAGE}, {VERSION})\n"


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
        for package in ("com.hobbycoders.aishuffle", "com.android.systemui.other", "com.android.launcher3.other"):
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

    def run_preparation(self, snapshots, deadline_seconds=12):
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
            if args == ("shell", "getprop", "ro.build.version.sdk"):
                return SDK + "\n"
            if args == ("shell", "getprop", "ro.build.id"):
                return BUILD_ID + "\n"
            if args == ("shell", "dumpsys", "webviewupdate"):
                return SYNTHETIC_PROVIDER
            return ""

        def sleep(seconds):
            self.elapsed += seconds

        def diagnostics():
            self.diagnostics += 1

        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / "summary"
            with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}):
                try:
                    prepare(adb, Path(folder), deadline_seconds=deadline_seconds, sleep=sleep, clock=lambda: self.elapsed,
                            diagnostics=diagnostics)
                finally:
                    self.summary = summary.read_text() if summary.exists() else ""

    def test_normal_boot_never_restarts_any_application(self):
        self.run_preparation([HOME])
        self.assertFalse(any("force-stop" in command for command in self.commands))
        self.assertEqual(0, self.diagnostics)
        self.assertEqual(4, self.elapsed)
        self.assertEqual("", self.summary)

    def test_launcher_boot_anr_gets_one_restart_then_all_readiness_checks(self):
        self.run_preparation([LAUNCHER_ANR, "mCurrentFocus=null", HOME])
        self.assertEqual([("shell", "am", "force-stop", "com.android.launcher3")],
                         [command for command in self.commands if "force-stop" in command])
        self.assertEqual(1, self.diagnostics)
        self.assertEqual(8, self.elapsed)
        self.assertIn("restarting Quickstep once", self.summary)
        self.assertIn("SDK 36, build BE2A.250530.026.D1", self.summary)
        self.assertIn("com.android.webview, 133.0.6943.137", self.summary)

    def test_repeated_launcher_anr_fails_without_repeated_recovery(self):
        with self.assertRaisesRegex(RuntimeError, "hung again"):
            self.run_preparation([LAUNCHER_ANR])
        self.assertEqual(1, sum("force-stop" in command for command in self.commands))

    def test_unexpected_app_anr_is_never_dismissed(self):
        with self.assertRaisesRegex(RuntimeError, "refusing recovery"):
            self.run_preparation([LAUNCHER_ANR.replace("com.android.launcher3", "com.hobbycoders.aishuffle")])
        self.assertFalse(any("force-stop" in command for command in self.commands))

    def test_app_crash_dialog_is_never_dismissed(self):
        crash = LAUNCHER_ANR.replace("com.android.launcher3", "com.hobbycoders.aishuffle").replace(
            "Application Not Responding", "Application Error")
        with self.assertRaisesRegex(RuntimeError, "refusing recovery"):
            self.run_preparation([crash])
        self.assertFalse(any("force-stop" in command or "kill" in " ".join(command) for command in self.commands))

    def test_systemui_boot_anr_gets_one_restart_then_all_readiness_checks(self):
        systemui_anr = LAUNCHER_ANR.replace("com.android.launcher3", "com.android.systemui")
        self.assertEqual("systemui_anr", readiness(systemui_anr, UNLOCKED, AWAKE))
        self.assertEqual("other_error", readiness(systemui_anr.replace("Application Not Responding", "Application Error"), UNLOCKED, AWAKE))
        self.run_preparation([systemui_anr, "mCurrentFocus=null", HOME], deadline_seconds=30)
        kills = [command for command in self.commands if any("kill" in part for part in command)]
        self.assertEqual([("shell", "su 0 kill -9 $(pidof com.android.systemui)")], kills)
        self.assertIn("restarting SystemUI once", self.summary)
        self.assertIn("SDK 36, build BE2A.250530.026.D1", self.summary)
        self.assertFalse(any("force-stop" in command for command in self.commands))
        self.assertEqual(1, self.diagnostics)

    def test_repeated_systemui_anr_fails_without_repeated_recovery(self):
        systemui_anr = LAUNCHER_ANR.replace("com.android.launcher3", "com.android.systemui")
        with self.assertRaisesRegex(RuntimeError, "SystemUI hung again"):
            self.run_preparation([systemui_anr], deadline_seconds=30)
        self.assertEqual(1, sum(any("kill" in part for part in command) for command in self.commands))

    def test_lost_focus_restarts_stability_count_and_eventually_times_out(self):
        with self.assertRaisesRegex(RuntimeError, "never reached"):
            self.run_preparation([HOME, HOME, "mCurrentFocus=null", HOME, HOME, "mCurrentFocus=null"])

    def run_webview(self, build_type="userdebug", readback=None, sdk=SDK, build_id=BUILD_ID,
                    provider=SYNTHETIC_PROVIDER):
        commands, flags = [], {}

        def adb(*args):
            commands.append(args)
            properties = {"ro.build.type": build_type, "ro.build.version.sdk": sdk, "ro.build.id": build_id}
            if args[:2] == ("shell", "getprop"):
                return properties[args[2]] + "\n"
            if args[:2] == ("shell", "cat"):
                return (readback if readback is not None else flags.get(args[2], "")) + "\n"
            if args == ("shell", "dumpsys", "webviewupdate"):
                return provider
            written = re.fullmatch(r"echo '([^']*)' > (\S+) && chmod 644 \2", args[1]) if len(args) == 2 else None
            if written:
                flags[written.group(2)] = written.group(1)
            return ""

        self.webview_log = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(self.webview_log):
            summary = Path(folder) / "summary"
            with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}):
                configure_webview(adb, Path(folder))
            evidence = (Path(folder) / "webview.txt").read_text()
            self.summary = summary.read_text() if summary.exists() else ""
        return commands, flags, evidence

    def test_webview_turns_off_only_the_unretained_dangling_ptr_crash(self):
        commands, flags, evidence = self.run_webview()
        self.assertEqual({"/data/local/tmp/webview-command-line": "_ --disable-features=PartitionAllocUnretainedDanglingPtr"}, flags)
        self.assertIn("133.0.6943.137", evidence)
        self.assertIn("Known emulator WebView 133 debug-check workaround", self.webview_log.getvalue())
        self.assertIn(module["WEBVIEW_FLAGS"], self.webview_log.getvalue())
        self.assertIn(module["WEBVIEW_FLAGS"], self.summary)
        self.assertIn("SDK 36, build BE2A.250530.026.D1", self.summary)
        self.assertIn("com.android.webview, 133.0.6943.137", self.summary)
        # Only that one feature: no blanket crash, field-trial or sandbox switches.
        written = flags["/data/local/tmp/webview-command-line"].split()
        self.assertEqual(["_", "--disable-features=PartitionAllocUnretainedDanglingPtr"], written)
        self.assertFalse(any("force-stop" in command or "kill" in " ".join(command) for command in commands))

    def test_other_images_and_providers_keep_all_crash_checks(self):
        known = "Current WebView package (name, version): (com.android.webview, 133.0.6943.137)\n"
        for overrides in ({"sdk": "34"}, {"sdk": ""}, {"build_id": "new-image"},
                          {"provider": known.replace("133.0.6943.137", "134.0.0.0")},
                          {"provider": known.replace("com.android.webview", "com.google.android.webview")},
                          {"provider": ""}, {"provider": known * 2},
                          {"provider": known.replace("Current WebView", "Preferred WebView")}):
            with self.subTest(overrides=overrides):
                commands, flags, _ = self.run_webview(**overrides)
                self.assertEqual({}, flags)
                self.assertFalse(any("chmod" in " ".join(command) for command in commands))
                self.assertIn("keeping crash checks enabled", self.webview_log.getvalue())

    def test_webview_flags_fail_closed_when_android_would_ignore_them(self):
        with self.assertRaisesRegex(RuntimeError, "ignores"):
            self.run_webview(build_type="user")
        with self.assertRaisesRegex(RuntimeError, "reads back"):
            self.run_webview(readback="")


if __name__ == "__main__":
    unittest.main()
