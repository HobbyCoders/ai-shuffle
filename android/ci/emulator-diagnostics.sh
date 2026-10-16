#!/usr/bin/env bash
# Best-effort, bounded diagnostics; never change the instrumentation exit status.
set -u
phase=${1:?usage: emulator-diagnostics.sh before-boot|launcher-recovery|test-failure|after-emulator}
case "$phase" in
  before-boot|launcher-recovery|test-failure|after-emulator) ;;
  *) echo "Unknown diagnostic phase: $phase" >&2; exit 2 ;;
esac
root="${RUNNER_TEMP:?}/android-emulator"
out="$root/$phase"
mkdir -p "$out"

capture() {
  local name=$1
  shift
  timeout --kill-after=2s 15s "$@" > "$out/$name.txt" 2>&1
  local status=$?
  if (( status != 0 )); then
    printf '\nDiagnostic command exited %s (124 means timeout).\n' "$status" >> "$out/$name.txt"
  fi
  return 0
}

capture disk df -h
capture memory free -h
capture processes ps -eo pid,ppid,stat,pcpu,pmem,rss,args --sort=-rss
capture cpu lscpu
capture kvm ls -l /dev/kvm
capture devices adb devices -l
sdk=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}
avd=${ANDROID_AVD_HOME:-$HOME/.android/avd}
capture avd-config cat "$avd/test.avd/config.ini"
capture emulator-version cat "$sdk/emulator/source.properties"
# Every installed x86_64 image (the job installs the matrix API level), with its path.
capture system-image-version bash -c 'for f in "$1"/system-images/android-*/*/x86_64/source.properties; do echo "== $f"; cat "$f"; done' _ "$sdk"

# The action kills the emulator before subsequent workflow steps. Capture guest
# state inside its test script, before teardown, without dismissing ANR dialogs.
if [[ "$phase" == test-failure || "$phase" == launcher-recovery ]]; then
  serial=${ANDROID_SERIAL:-emulator-5554}
  capture logcat adb -s "$serial" logcat -b all -d -v threadtime
  capture properties adb -s "$serial" shell getprop
  capture guest-disk adb -s "$serial" shell df -h
  capture activity adb -s "$serial" shell dumpsys activity
  capture window adb -s "$serial" shell dumpsys window
  capture cpuinfo adb -s "$serial" shell dumpsys cpuinfo
  capture meminfo adb -s "$serial" shell dumpsys meminfo
  # The WebView provider and version the app ran on (emulator images never update it).
  capture webview adb -s "$serial" shell dumpsys webviewupdate
  for tag in data_app_anr system_app_anr system_server_anr; do
    capture "$tag" adb -s "$serial" shell dumpsys dropbox --print "$tag"
  done
fi

if [[ "$phase" == after-emulator && -f "$root/emulator.log" ]]; then
  echo 'Last emulator output (full startup and guest logcat are in the artifact):'
  tail -n 80 "$root/emulator.log"
fi
exit 0
