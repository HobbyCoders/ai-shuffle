"""Record a vanished emulator process so the workflow can retry it once.

Called from run-android-ui-tests.sh only after a failure, before the emulator
action's own teardown. A marker is written only when the emulator process is
gone, adb no longer sees a device, and no instrumentation result shows a test
failure. A live emulator (hung, offline or ANR) and every app failure stay hard
failures with no marker.
"""
import argparse
import os
import re
import runpy
import sys
from pathlib import Path

validate = runpy.run_path(str(Path(__file__).with_name("check-instrumentation.py")))["validate"]

FAILURE = re.compile(r"FAILURES!!!|INSTRUMENTATION_FAILED|Process crashed|shortMsg=")
STATUS = re.compile(r"^INSTRUMENTATION_STATUS_CODE:\s*(-?\d+)\s*$", re.MULTILINE)
TERMINAL = re.compile(r"^INSTRUMENTATION_CODE:\s*(-?\d+)\s*$", re.MULTILINE)


def qemu_running(proc):
    """True when any host process runs qemu-system*, or when this cannot be told."""
    proc = Path(proc)
    if not proc.is_dir():
        return True  # No process table to inspect: never claim the emulator vanished.
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv0 = (entry / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            continue  # Exited while scanning, or not readable.
        # Zombies and kernel threads have an empty command line.
        if argv0 and os.path.basename(argv0).startswith("qemu-system"):
            return True
    return False


def test_failure(text):
    """True when output shows a test or app failure, not just a cut-off stream."""
    if FAILURE.search(text) or any(int(code) not in (0, 1) for code in STATUS.findall(text)):
        return True
    if TERMINAL.search(text):
        # A finished run that the strict parser rejects is an app failure.
        try:
            validate(text)
        except ValueError:
            return True
    return False


def results(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    files = [path for path in directory.glob("*.txt") if path.name != "timing.txt"]
    return sorted(files, key=lambda path: (path.stat().st_mtime_ns, path.name))


def decide(root, proc, adb_state):
    """Return (lost, reason)."""
    if qemu_running(proc):
        return False, "emulator process is still running"
    if adb_state.strip() == "device":
        return False, "adb still reports emulator-5554 as a device"
    passed = None
    for path in results(Path(root) / "instrumentation"):
        text = path.read_text(errors="replace")
        if test_failure(text):
            return False, f"{path.name} shows a test failure"
        try:
            validate(text)
            passed = path.name
        except ValueError:
            pass  # Cut off by the device loss; no failure was reported.
    when = f"after {passed} passed" if passed else "before any instrumentation result passed"
    return True, f"Emulator process disappeared {when}; this is a virtual-device failure, not an app failure."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.path.join(os.environ.get("RUNNER_TEMP", ""), "android-emulator"))
    parser.add_argument("--proc", default=os.environ.get("EMULATOR_LOSS_PROC", "/proc"))
    parser.add_argument("--adb-state", default="")
    args = parser.parse_args(argv)
    lost, reason = decide(args.root, args.proc, args.adb_state)
    marker = Path(args.root) / "emulator-lost"
    if lost:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(reason + "\n")
        print(f"::warning::{reason}")
    else:
        marker.unlink(missing_ok=True)
        print(f"Not an emulator loss ({reason}); the failure stands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
