#!/usr/bin/env bash
# Build the phone UI (mobile/) and copy it into the APK as the offline demo.
#
# Chat does not use this copy: the paired worker serves the phone UI at
# /mobile/ (app/mobile_ui.py), so UI changes ship with a worker update. The only
# bundled copy is the offline demo (OfflineDemoActivity), which needs no worker
# and runs on canned data, for store review and first-run browsing. A stale demo
# is harmless. The assets directory is generated and git-ignored; run this
# before `./gradlew assemble*`. The Gradle `checkOfflineDemo` task fails the
# build when it is missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND="$ROOT/frontend"
MOBILE="$ROOT/mobile"
DEST="$ROOT/android/app/src/main/assets/offline-demo"

if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "[sync-offline-demo] installing frontend dependencies (shared with mobile/)"
  (cd "$FRONTEND" && npm ci)
fi

echo "[sync-offline-demo] building mobile/"
(cd "$MOBILE" && npm run deps && npm run build)

if [ ! -f "$MOBILE/build/index.html" ]; then
  echo "[sync-offline-demo] mobile/build/index.html is missing after the build" >&2
  exit 1
fi

echo "[sync-offline-demo] copying mobile/build -> android/app/src/main/assets/offline-demo"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$MOBILE/build/." "$DEST/"

# Provenance for bug reports: which demo UI is inside this APK. Same source as the
# bundle's __APP_VERSION__ (frontend/app-version.js: APP_VERSION, else
# frontend/package.json, which tracks the release tag).
version="$(node --input-type=module -e "const { pathToFileURL } = await import('node:url'); const { appVersion } = await import(pathToFileURL(process.argv[1]).href); console.log(appVersion)" "$FRONTEND/app-version.js")"
commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '{"version":"%s","commit":"%s","builtAt":"%s"}\n' "$version" "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/version.json"

echo "[sync-offline-demo] done: $(find "$DEST" -type f | wc -l | tr -d ' ') files, ui $version ($commit)"
