#!/usr/bin/env bash
set -euo pipefail

# android.yml splits the run over UI_TEST_PARTS emulator jobs per API level, so
# no job runs the whole 14-minute suite. Parts 1..N-1 each run one
# AndroidJUnitRunner hash shard of the suite (every test lands in exactly one
# shard, new tests included). The last part runs the fresh-process cold-chat
# check, the suite's longest test and the notification-permission decisions.
# Unset, one job runs everything, as before the split.
parts="${UI_TEST_PARTS:-1}"
part="${UI_TEST_PART:-1}"
if ! [[ "$parts" =~ ^[1-9][0-9]*$ && "$part" =~ ^[1-9][0-9]*$ ]] || (( part > parts )); then
  echo "::error::UI_TEST_PART=$part of UI_TEST_PARTS=$parts is not a part of this run." >&2
  exit 2
fi
last=$(( part == parts ))
if (( parts == 1 )); then
  scope="cold-start and full suite"
elif (( last )); then
  scope="part $part of $parts: cold-start, longest suite test and notification decisions"
else
  scope="part $part of $parts: suite shard $part of $(( parts - 1 ))"
fi
# The real Save dialog needs an external driver and has a separate manual run.
save_dialog='com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#realSystemSaveDialogReturnsToChatAndCompletesTransfer'
# About 135 of the suite's 860 seconds (run 44723603249): alone on the last part,
# it no longer decides how long the slowest shard takes.
longest='com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#workerFileLinkAboveTheBufferedCapStreamsWholeIntoTheSavedDocument'
# The last part runs this in its own fresh process, so the shards leave it out.
cold_chat='com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#coldChatStartsWithBootstrapAndNativeBridges'

out="$RUNNER_TEMP/android-emulator/instrumentation"
mkdir -p "$out"

on_exit() {
  local status=$?
  trap - EXIT
  # Check first, before slower adb calls. The action kills the emulator only
  # after this script exits, so a missing emulator process here is a real loss.
  # Only that case is marked for the workflow's single retry; app failures never are.
  if (( status != 0 )); then
    local state
    state="$(timeout 10 adb -s emulator-5554 get-state 2>/dev/null | tr -d '\r')" || true
    python3 ci/emulator-loss.py --adb-state "$state" || true
  fi
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    if (( status == 0 )); then
      printf '%s\n' "- Android emulator verification: PASSED ($scope)." >> "$GITHUB_STEP_SUMMARY"
    else
      printf '%s\n' "- Android emulator verification: FAILED (exit $status). No app failure was retried or excused; release-advisory policy does not change this failed result." >> "$GITHUB_STEP_SUMMARY"
    fi
  fi
  # Keep timing evidence on success too, before emulator teardown loses logcat.
  timeout 15 adb -s emulator-5554 logcat -d -v threadtime ConsentTiming:I ProfileTiming:I NativeInputTiming:I AppControlTiming:I AppControlFence:I DemoNavigation:I '*:S' > "$out/timing.txt" || true
  # The always-uploaded artifact is deliberately confined to readiness evidence.
  # Keep this compact trace there too, without widening its path allowlist.
  mkdir -p "$RUNNER_TEMP/android-emulator/readiness" && cp "$out/timing.txt" "$RUNNER_TEMP/android-emulator/readiness/native-timing.txt" || true
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

# Push-enabled builds ask at chat startup; grant before either test can open it.
# POST_NOTIFICATIONS is a runtime permission only on Android 13 (API 33)+.
api_level="$(adb -s emulator-5554 shell getprop ro.build.version.sdk | tr -d '\r')"
if (( api_level >= 33 )); then
  adb -s emulator-5554 shell pm grant com.hobbycoders.aishuffle android.permission.POST_NOTIFICATIONS
fi

if (( parts == 1 || last )); then
  # Start chat in its own fresh app process: a previous WebView can hide startup
  # ordering bugs in document-start script registration (seen on WebView 151).
  adb -s emulator-5554 shell am force-stop com.hobbycoders.aishuffle
  timeout --kill-after=15s 180s adb -s emulator-5554 shell am instrument -w -r \
    -e timeout_msec 120000 \
    -e class 'com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#coldChatStartsWithBootstrapAndNativeBridges' \
    com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/cold-chat.txt"
  python3 ci/check-instrumentation.py "$out/cold-chat.txt"
fi

# Exclude only the opt-in Save dialog test, so the strict result parser
# continues rejecting every unexpected skip or failure; a shard that selects no
# test fails it too.
# On ubuntu-24.04 the 61-test suite reached test 59 at 596 seconds;
# the old 600s suite deadline killed a healthy test, not a hung refresh.
# Give the full suite room while bounding each test independently (milliseconds).
# Keep the host deadline as a backstop for an unresponsive instrumentation process.
# Consent coverage also exercises a separate APK so the bundled WebView is not the target.
if (( parts == 1 )); then
  timeout --kill-after=15s 1200s adb -s emulator-5554 shell am instrument -w -r \
    -e timeout_msec 180000 \
    -e notClass "$save_dialog" \
    -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke \
    com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/runner.txt"
  python3 ci/check-instrumentation.py "$out/runner.txt"
elif (( ! last )); then
  # A shard takes about 4 minutes; the host deadline still leaves it twice that.
  timeout --kill-after=15s 600s adb -s emulator-5554 shell am instrument -w -r \
    -e timeout_msec 180000 \
    -e numShards "$(( parts - 1 ))" -e shardIndex "$(( part - 1 ))" \
    -e notClass "$save_dialog,$longest,$cold_chat" \
    -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke \
    com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/runner.txt"
  python3 ci/check-instrumentation.py "$out/runner.txt"
else
  timeout --kill-after=15s 300s adb -s emulator-5554 shell am instrument -w -r \
    -e timeout_msec 180000 \
    -e class "$longest" \
    -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke \
    com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/longest.txt"
  python3 ci/check-instrumentation.py "$out/longest.txt"
fi

if (( parts > 1 && ! last )); then
  exit 0
fi

# Deliberately NOT granted POST_NOTIFICATIONS. Clear data/permission flags before
# each decision; never retry a failed test. The debug-only push build has no FCM
# secrets and the tests use local worker responses, but the OS prompt is real.
adb -s emulator-5554 install -r notification-instrumentation-apks/debug/app-debug.apk
adb -s emulator-5554 install -r notification-instrumentation-apks/androidTest/debug/app-debug-androidTest.apk
for entry in Startup Launcher; do
  for decision in allowing denying; do
    adb -s emulator-5554 shell pm clear com.hobbycoders.aishuffle
    adb -s emulator-5554 shell pm clear-permission-flags com.hobbycoders.aishuffle android.permission.POST_NOTIFICATIONS user-set user-fixed
    timeout --kill-after=15s 180s adb -s emulator-5554 shell am instrument -w -r \
      -e timeout_msec 120000 \
      -e class "com.hobbycoders.aishuffle.NotificationPermissionSmokeTest#${decision}${entry}NotificationsLeavesChatUsable" \
      com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/notification-$entry-$decision.txt"
    python3 ci/check-instrumentation.py "$out/notification-$entry-$decision.txt"
  done
done
