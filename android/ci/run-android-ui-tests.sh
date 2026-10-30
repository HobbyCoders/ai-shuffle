#!/usr/bin/env bash
set -euo pipefail

out="$RUNNER_TEMP/android-emulator/instrumentation"
mkdir -p "$out"
on_exit() {
  local status=$?
  trap - EXIT
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    if (( status == 0 )); then
      printf '%s\n' '- Android emulator verification: PASSED (cold-start and full suite).' >> "$GITHUB_STEP_SUMMARY"
    else
      printf '%s\n' "- Android emulator verification: FAILED (exit $status). No app failure was retried or excused; release-advisory policy does not change this failed result." >> "$GITHUB_STEP_SUMMARY"
    fi
  fi
  # Keep timing evidence on success too, before emulator teardown loses logcat.
  timeout 15 adb -s emulator-5554 logcat -d -v threadtime ConsentTiming:I ProfileTiming:I DemoNavigation:I '*:S' > "$out/timing.txt" || true
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

# Start chat in its own fresh app process: a previous WebView can hide startup
# ordering bugs in document-start script registration (seen on WebView 151).
adb -s emulator-5554 shell am force-stop com.hobbycoders.aishuffle
timeout --kill-after=15s 180s adb -s emulator-5554 shell am instrument -w -r \
  -e timeout_msec 120000 \
  -e class 'com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#coldChatStartsWithBootstrapAndNativeBridges' \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/cold-chat.txt"
python3 ci/check-instrumentation.py "$out/cold-chat.txt"

# The real Save dialog needs an external driver and has a separate manual run.
# Exclude only that opt-in test, so the strict result parser continues rejecting
# every unexpected skip or failure.
# On ubuntu-latest the 61-test suite reached test 59 at 596 seconds;
# the old 600s suite deadline killed a healthy test, not a hung refresh.
# Give the full suite room while bounding each test independently (milliseconds).
# Keep the host deadline as a backstop for an unresponsive instrumentation process.
# Consent coverage also exercises a separate APK so the bundled WebView is not the target.
timeout --kill-after=15s 1200s adb -s emulator-5554 shell am instrument -w -r \
  -e timeout_msec 180000 \
  -e notClass 'com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#realSystemSaveDialogReturnsToChatAndCompletesTransfer' \
  -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/runner.txt"
python3 ci/check-instrumentation.py "$out/runner.txt"
