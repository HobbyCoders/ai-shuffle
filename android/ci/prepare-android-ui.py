"""Require an unlocked, focused launcher before installing the app under test.

API 34 first boot can leave a Quickstep ANR in front of every app, and API 36
first boot on a 4-vCPU hosted runner can leave a SystemUI ANR (its
KeyguardService bind times out while the cold image is still optimizing; runs
38343028922 and 38347338129). Recover only those exact first-boot failures,
each once, before any Shuffle code is installed. Never dismiss an application
ANR, never recover during instrumentation, and never turn off Android's dialogs.

Then select and verify the official emulator WebView (configure_webview) before
the app is installed, keeping all Chromium crash checks enabled.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

LAUNCHER = "com.android.launcher3"
SYSTEMUI = "com.android.systemui"


def report_workaround(adb, message):
    sdk = adb("shell", "getprop", "ro.build.version.sdk").strip()
    build_id = adb("shell", "getprop", "ro.build.id").strip()
    provider = adb("shell", "dumpsys", "webviewupdate")
    current = next((line.strip() for line in provider.splitlines()
                    if line.strip().startswith("Current WebView package (name, version):")), "WebView unknown")
    detail = f"{message}; SDK {sdk or 'unknown'}, build {build_id or 'unknown'}; {current}"
    print(detail, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"- {detail}\n")


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
            report_workaround(adb, "First-boot launcher ANR detected before app installation; retaining diagnostics and restarting Quickstep once")
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
            report_workaround(adb, "First-boot SystemUI ANR detected before app installation; retaining diagnostics and restarting SystemUI once")
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


# Both API 36 SDK images ship WebView 133. Suppressing its dangling-pointer
# check only moved the native abort to basic_string SIGABRT (41509192630,
# 41509957738). Chromium's intercepted-request WeakPtr fix is in 136 (74beb528),
# but its causal link to those unsymbolized aborts is inferred, not established.
# On google_apis, com.android.webview is an allowed but ABSENT alternate provider
# (41544019324). Co-install the official signed AOSP package: do not replace a
# differently signed system package, bypass signatures or disable crash checks.
def install_webview(adb):
    from ci_webview import verify
    apk = os.environ.get("CI_WEBVIEW_APK")
    if not apk:
        raise RuntimeError("API 36 requires a verified CI_WEBVIEW_APK")
    verify(apk)
    adb("install", apk)
    adb("shell", "cmd", "webviewupdate", "set-webview-implementation", "com.android.webview")


def configure_webview(adb, out: Path, installer=install_webview):
    from ci_webview import VERSION
    out.mkdir(parents=True, exist_ok=True)
    sdk = adb("shell", "getprop", "ro.build.version.sdk").strip()
    provider = adb("shell", "dumpsys", "webviewupdate")
    (out / "webview.txt").write_text(provider, encoding="utf-8")
    if sdk == "36":
        current = re.findall(r"^\s*Current WebView package \(name, version\): \((com\.(?:android|google\.android)\.webview), (\d+)\.\d+\.\d+\.\d+\)\s*$",
                             provider, re.MULTILINE)
        if len(current) != 1:
            raise RuntimeError("API 36 requires a WebView >= 136; missing or ambiguous provider: " + provider.strip())
        if int(current[0][1]) < 136:
            if current[0][0] != "com.google.android.webview" or "com.android.webview is NOT installed." not in provider:
                raise RuntimeError("API 36 requires a WebView >= 136 and the google_apis image with an absent AOSP provider")
            (out / "webview-before.txt").write_text(provider, encoding="utf-8")
            installer(adb)
            provider = adb("shell", "dumpsys", "webviewupdate")
            (out / "webview.txt").write_text(provider, encoding="utf-8")
            expected = f"Current WebView package (name, version): (com.android.webview, {VERSION})"
            if provider.count(expected) != 1:
                raise RuntimeError("Verified AOSP WebView did not become the active provider: " + provider.strip())
        if int(current[0][1]) < 136:
            description = "API 36 co-installed pinned AOSP alternate, not the phone provider"
        else:
            description = "API 36 retained its existing provider; see exact package/version"
        report_workaround(adb, description + "; abort/request-lifetime link inferred; all crash checks enabled")
    else:
        print(f"SDK {sdk!r}: keeping stock WebView and all crash checks enabled", flush=True)


def main():
    serial = os.environ.get("ANDROID_SERIAL", "emulator-5554")
    if not re.fullmatch(r"emulator-\d+", serial):
        raise RuntimeError("UI test preparation is restricted to an emulator")

    def adb(*args):
        return subprocess.run(["adb", "-s", serial, *args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=120 if args[0] == "install" else 15).stdout

    def diagnostics():
        subprocess.run(["bash", "ci/emulator-diagnostics.sh", "launcher-recovery"],
                       check=False, timeout=240)

    out = Path(os.environ["RUNNER_TEMP"]) / "android-emulator" / "readiness"
    prepare(adb, out, diagnostics=diagnostics)
    # Before the app is installed, select and verify the provider without suppressing crash checks.
    configure_webview(adb, out)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(f"Emulator readiness failed: {error}", file=sys.stderr)
        sys.exit(1)
