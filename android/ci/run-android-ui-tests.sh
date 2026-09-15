#!/usr/bin/env bash
set -euo pipefail

out="$RUNNER_TEMP/android-emulator/instrumentation"
mkdir -p "$out"
on_exit() {
  local status=$?
  trap - EXIT
  # Keep timing evidence on success too, before emulator teardown loses logcat.
  timeout 15 adb -s emulator-5554 logcat -d -v threadtime ConsentTiming:I ProfileTiming:I '*:S' > "$out/timing.txt" || true
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
  -e class 'com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#coldChatStartsWithBootstrapAndNativeBridges' \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/cold-chat.txt"
python3 ci/check-instrumentation.py "$out/cold-chat.txt"

# Revocation kills a running app, including its instrumentation process. Establish
# the denied state before this isolated run; later capability tests may grant it.
adb -s emulator-5554 shell pm revoke com.hobbycoders.aishuffle android.permission.RECORD_AUDIO
timeout --kill-after=15s 180s adb -s emulator-5554 shell am instrument -w -r \
  -e class com.hobbycoders.aishuffle.VoicePermissionSmokeTest \
  -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/voice-permission.txt"
python3 ci/check-instrumentation.py "$out/voice-permission.txt"

# The real Save dialog needs an external driver and has a separate manual run.
# Exclude only that opt-in test and the already executed permission class, so the
# strict result parser continues rejecting every unexpected skip or failure.
timeout --kill-after=15s 600s adb -s emulator-5554 shell am instrument -w -r \
  -e notClass 'com.hobbycoders.aishuffle.VoicePermissionSmokeTest,com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#realSystemSaveDialogReturnsToChatAndCompletesTransfer' \
  -e additionalTestOutputDir /sdcard/Android/media/com.hobbycoders.aishuffle/ui-smoke \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner | tee "$out/runner.txt"
python3 ci/check-instrumentation.py "$out/runner.txt"
