#!/usr/bin/env bash
set -euo pipefail

out="$RUNNER_TEMP/android-emulator/instrumentation"
mkdir -p "$out"
on_exit() {
  local status=$?
  trap - EXIT
  timeout 30 adb -s emulator-5554 pull /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke "$out/screenshots" || true
  if (( status != 0 )); then
    bash ci/emulator-diagnostics.sh test-failure || true
  fi
  exit "$status"
}
trap on_exit EXIT

# sys.boot_completed does not mean the launcher has a responsive, focused window.
# Establish that before installing Shuffle so boot recovery cannot hide app ANRs.
python3 ci/prepare-android-ui.py

adb -s emulator-5554 install -r instrumentation-apks/debug/app-debug.apk
adb -s emulator-5554 install -r instrumentation-apks/androidTest/debug/app-debug-androidTest.apk
timeout --kill-after=15s 600s adb -s emulator-5554 shell am instrument -w -r   -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke   com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/runner.txt"
python3 ci/check-instrumentation.py "$out/runner.txt"
