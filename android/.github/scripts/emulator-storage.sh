#!/usr/bin/env bash
# Storage preparation for the API 34 instrumentation job, not general cleanup.
set -euo pipefail

case "${1:-}" in
  check)
    avd="${ANDROID_AVD_HOME:-$HOME/.android/avd}/test.avd"
    config="$avd/config.ini"
    if [[ ! -f "$config" ]]; then
      echo "::error::AVD config missing: $config. Run the disk preflight after AVD creation." >&2
      exit 1
    fi
    # android-emulator-runner appends overrides; use the last matching entry.
    requested=$(awk -F= '/^[[:space:]]*disk[.]dataPartition[.]size[[:space:]]*=/ {
      value=$2; gsub(/[[:space:]]/, "", value)
    } END { print value }' "$config")
    if [[ ! "$requested" =~ ^([0-9]+)([KMG]?)(B?)$ ]]; then
      echo "::error::Unsupported userdata size '$requested' in $config; update the preflight parser." >&2
      exit 1
    fi
    bytes=$((10#${BASH_REMATCH[1]}))
    case "${BASH_REMATCH[2]}" in
      K) bytes=$((bytes * 1024)) ;;
      M) bytes=$((bytes * 1024 * 1024)) ;;
      G) bytes=$((bytes * 1024 * 1024 * 1024)) ;;
    esac
    # Emulator android-qemu2-glue/main.cpp enforces a 6 GiB first-boot minimum
    # for API >=24 (even google_apis, not just Play Store), then a 1.2 safety
    # factor. Run 33970589931 rewrote 4096M to 6442450944 and required 7372.80 MiB.
    # https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android-qemu2-glue/main.cpp
    minimum=$((6 * 1024 * 1024 * 1024))
    if (( bytes < minimum )); then bytes=$minimum; fi
    startup=$(((bytes * 12 + 9) / 10))
    # The emulator already reserves 20% above its 6 GiB image. Add 512 MiB
    # for prebuilt APK installation and artifacts; Gradle runs on another host.
    required=$((startup + 512 * 1024 * 1024))
    available=$(df -B1 --output=avail "$avd" | awk 'NR == 2 {print $1}')
    if [[ ! "$available" =~ ^[0-9]+$ ]]; then
      echo "::error::Cannot determine available bytes on the AVD filesystem: $avd" >&2
      exit 1
    fi
    printf 'AVD storage: path=%s requested=%s effective=%s startup=%s reserve=%s available=%s required=%s bytes\n' \
      "$avd" "$requested" "$bytes" "$startup" "$((required - startup))" "$available" "$required"
    df -h "$avd"
    if (( available < required )); then
      echo "::error::Insufficient AVD disk space at $avd: $available bytes available, $required required (6 GiB minimum userdata, 20% startup margin, 512 MiB test reserve). Use a runner with more storage or move ANDROID_AVD_HOME to a larger disk. Lowering disk-size below 6144M cannot help API 34. Emulator launch aborted before the boot timeout." >&2
      exit 1
    fi
    ;;
  *) echo 'Usage: emulator-storage.sh check' >&2; exit 2 ;;
esac
