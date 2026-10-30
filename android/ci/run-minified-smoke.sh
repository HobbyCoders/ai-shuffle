#!/usr/bin/env bash
set -euo pipefail
out="$RUNNER_TEMP/android-emulator/minified"
mkdir -p "$out"
on_exit() {
  local status=$?
  trap - EXIT
  if (( status != 0 )); then
    bash ci/emulator-diagnostics.sh minified-failure || true
    timeout 15 adb -s emulator-5554 logcat -d > "$out/logcat.txt" || true
  fi
  exit "$status"
}
trap on_exit EXIT
python3 ci/prepare-android-ui.py
adb -s emulator-5554 install -r -t minified-apks/releaseSmoke/app-releaseSmoke.apk
adb -s emulator-5554 install -r -t minified-apks/tests/minified-smoke-debug.apk
adb -s emulator-5554 shell am force-stop com.hobbycoders.aishuffle
timeout --kill-after=15s 300s adb -s emulator-5554 shell am instrument -w -r \
  -e timeout_msec 120000 \
  -e class com.hobbycoders.aishuffle.MinifiedReleaseSmokeTest \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/runner.txt"
python3 ci/check-instrumentation.py "$out/runner.txt"
