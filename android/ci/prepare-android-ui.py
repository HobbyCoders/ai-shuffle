"""Require an unlocked, focused launcher before installing the app under test.

API 34 first boot can leave a Quickstep ANR in front of every app, and API 36
first boot on a 4-vCPU hosted runner can leave a SystemUI ANR (its
KeyguardService bind times out while the cold image is still optimizing; runs
38343028922 and 38347338129). Recover only those exact first-boot failures,
each once, before any Shuffle code is installed. Never dismiss an application
ANR, never recover during instrumentation, and never turn off Android's dialogs.

Then set the emulator WebView's debug flags (configure_webview) before the app
is installed.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

LAUNCHER = "com.android.launcher3"
SYSTEMUI = "com.android.systemui"


def readiness(displays: str, policy: str, power: str) -> str:
    # Use `dumpsys window displays`, not the full window dump: the latter also
    # includes the previous ANR's historical focus, which is not current state.
    focuses = re.findall(r"^\s*mCurrentFocus=(.+)$", displays, re.MULTILINE)
    if len(focuses) != 1:
        return "waiting"
    focus = focuses[0].strip()
    if re.fullmatch(r"Window\{[^}]+ u0 Application Not Responding: " + re.escape(LAUNCHER) + r"\}", focus):
        return "launcher_anr"
    if re.fullmatch(r"Window\{[^}]+ u0 Application Not Responding: " + re.escape(SYSTEMUI) + r"\}", focus):
        return "systemui_anr"
    if "Application Not Responding:" in focus or "Application Error:" in focus:
        return "other_error"
    # AOSP API 34 uses Quickstep; Google's API 36 image uses Pixel Launcher.
    # Recognize only these exact home packages. ANR recovery above remains
    # restricted to the known Quickstep first-boot issue, never a Pixel/app ANR.
    launcher_focused = any(
        re.fullmatch(r"Window\{[^}]+ u0 " + re.escape(package) + r"/[^}]+\}", focus)
        for package in (LAUNCHER, "com.google.android.apps.nexuslauncher")
    )
    awake = re.search(r"^\s*mWakefulness=Awake\s*$", power, re.MULTILINE)
    unlocked = re.search(r"^\s*mInputRestricted=false\s*$", policy, re.MULTILINE)
    return "ready" if launcher_focused and awake and unlocked else "waiting"


def prepare(adb, out: Path, *, deadline_seconds=120, required_samples=3, sleep=time.sleep, clock=time.monotonic,
            diagnostics=lambda: None):
    out.mkdir(parents=True, exist_ok=True)
    # This script is invoked on a fresh CI emulator before app installation.
    adb("shell", "svc", "power", "stayon", "true")
    adb("shell", "settings", "put", "system", "screen_off_timeout", "1800000")
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    adb("shell", "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME")
    deadline = clock() + deadline_seconds
    recovered = False
    systemui_recovered = False
    stable = 0
    while clock() < deadline:
        displays = adb("shell", "dumpsys", "window", "displays")
        policy = adb("shell", "dumpsys", "window", "policy")
        power = adb("shell", "dumpsys", "power")
        for name, data in (("displays", displays), ("policy", policy), ("power", power)):
            (out / f"{name}.txt").write_text(data, encoding="utf-8")
        state = readiness(displays, policy, power)
        if state == "other_error":
            raise RuntimeError("An unexpected Android error dialog is blocking the emulator; refusing recovery")
        if state == "launcher_anr":
            if recovered:
                raise RuntimeError("The Android launcher hung again after its single pre-test restart")
            print("First-boot launcher ANR detected before app installation; retaining diagnostics and restarting Quickstep once", flush=True)
            for name, data in (("displays", displays), ("policy", policy), ("power", power)):
                (out / f"launcher-anr-{name}.txt").write_text(data, encoding="utf-8")
            diagnostics()
            adb("shell", "am", "force-stop", LAUNCHER)
            adb("shell", "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME", "-p", LAUNCHER)
            recovered = True
            stable = 0
        elif state == "systemui_anr":
            if systemui_recovered:
                raise RuntimeError("SystemUI hung again after its single pre-test restart")
            print("First-boot SystemUI ANR detected before app installation; retaining diagnostics and restarting SystemUI once", flush=True)
            for name, data in (("displays", displays), ("policy", policy), ("power", power)):
                (out / f"systemui-anr-{name}.txt").write_text(data, encoding="utf-8")
            diagnostics()
            # SystemUI is persistent: `am force-stop` skips it. Killing the hung
            # process closes its ANR dialog and Android restarts it. Emulator
            # system images are userdebug, so `su` is available.
            adb("shell", "su 0 kill -9 $(pidof com.android.systemui)")
            sleep(10)
            adb("shell", "wm", "dismiss-keyguard")
            adb("shell", "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME")
            systemui_recovered = True
            stable = 0
        elif state == "ready":
            stable += 1
            if stable >= required_samples:
                print("Emulator is awake, unlocked, and has a stable focused launcher; app verification can start", flush=True)
                return
        else:
            stable = 0
        sleep(2)
    raise RuntimeError("Emulator never reached a stable unlocked launcher before app verification")


# The emulator's built-in AOSP WebView is never updated and gets no variations
# seed, so it runs Chromium's code defaults. In Chromium 133 (API 36 image
# BE2A.250530.026.D1) PartitionAllocUnretainedDanglingPtr defaults to mode=crash:
# a dangling base::Unretained inside WebView's own IO thread aborted the app
# process with SIGTRAP mid-suite (release run 38824056427, attempts 1 and 2;
# Chromium's own field-trial testing config runs this study as
# dump_without_crashing). Turn off only that internal check. Every other
# WebView, renderer or app crash still fails the run.
WEBVIEW_FLAGS_FILE = "/data/local/tmp/webview-command-line"
WEBVIEW_FLAGS = "_ --disable-features=PartitionAllocUnretainedDanglingPtr"


def configure_webview(adb, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    # WebView reads the flags file only on debuggable (userdebug/eng) builds.
    build_type = adb("shell", "getprop", "ro.build.type").strip()
    if build_type not in ("userdebug", "eng"):
        raise RuntimeError(f"WebView ignores {WEBVIEW_FLAGS_FILE} on a {build_type or 'unknown'} build")
    adb("shell", f"echo '{WEBVIEW_FLAGS}' > {WEBVIEW_FLAGS_FILE} && chmod 644 {WEBVIEW_FLAGS_FILE}")
    written = adb("shell", "cat", WEBVIEW_FLAGS_FILE).strip()
    if written != WEBVIEW_FLAGS:
        raise RuntimeError(f"WebView flags file reads back as {written!r}")
    # Keep the exact WebView provider and version with the run's evidence.
    (out / "webview.txt").write_text(adb("shell", "dumpsys", "webviewupdate"), encoding="utf-8")
    print(f"WebView flags for this emulator: {WEBVIEW_FLAGS}", flush=True)


def main():
    serial = os.environ.get("ANDROID_SERIAL", "emulator-5554")
    if not re.fullmatch(r"emulator-\d+", serial):
        raise RuntimeError("UI test preparation is restricted to an emulator")

    def adb(*args):
        return subprocess.run(["adb", "-s", serial, *args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15).stdout

    def diagnostics():
        subprocess.run(["bash", "ci/emulator-diagnostics.sh", "launcher-recovery"],
                       check=False, timeout=240)

    out = Path(os.environ["RUNNER_TEMP"]) / "android-emulator" / "readiness"
    prepare(adb, out, diagnostics=diagnostics)
    # Before the app is installed: WebView reads its flags when it first loads in a process.
    configure_webview(adb, out)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(f"Emulator readiness failed: {error}", file=sys.stderr)
        sys.exit(1)
