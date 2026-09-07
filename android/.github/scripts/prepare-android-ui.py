"""Require an unlocked, focused launcher before installing the app under test.

API 34 first boot can leave a Quickstep ANR in front of every app. Recover only
that exact launcher failure, once, before any Shuffle code runs. Never dismiss
an application/system ANR during instrumentation or turn off Android's dialogs.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

LAUNCHER = "com.android.launcher3"


def readiness(displays: str, policy: str, power: str) -> str:
    # Use `dumpsys window displays`, not the full window dump: the latter also
    # includes the previous ANR's historical focus, which is not current state.
    focuses = re.findall(r"^\s*mCurrentFocus=(.+)$", displays, re.MULTILINE)
    if len(focuses) != 1:
        return "waiting"
    focus = focuses[0].strip()
    if re.fullmatch(r"Window\{[^}]+ u0 Application Not Responding: " + re.escape(LAUNCHER) + r"\}", focus):
        return "launcher_anr"
    if "Application Not Responding:" in focus or "Application Error:" in focus:
        return "other_error"
    launcher_focused = re.fullmatch(r"Window\{[^}]+ u0 " + re.escape(LAUNCHER) + r"/[^}]+\}", focus)
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
        elif state == "ready":
            stable += 1
            if stable >= required_samples:
                print("Emulator is awake, unlocked, and has a stable focused launcher; app verification can start", flush=True)
                return
        else:
            stable = 0
        sleep(2)
    raise RuntimeError("Emulator never reached a stable unlocked launcher before app verification")


def main():
    serial = os.environ.get("ANDROID_SERIAL", "emulator-5554")
    if not re.fullmatch(r"emulator-\d+", serial):
        raise RuntimeError("UI test preparation is restricted to an emulator")

    def adb(*args):
        return subprocess.run(["adb", "-s", serial, *args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15).stdout

    def diagnostics():
        subprocess.run(["bash", ".github/scripts/emulator-diagnostics.sh", "launcher-recovery"],
                       check=False, timeout=240)

    prepare(adb, Path(os.environ["RUNNER_TEMP"]) / "android-emulator" / "readiness", diagnostics=diagnostics)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError) as error:
        print(f"Emulator readiness failed: {error}", file=sys.stderr)
        sys.exit(1)
