#!/usr/bin/env bash
# Build the mobile UI (mobile/) and copy it into the Android app's assets.
#
# The app ships its own copy of the UI at src/main/assets/mobile-ui/ and loads
# it through WebViewAssetLoader (https://appassets.androidplatform.net/assets/
# mobile-ui/). Workers never serve the phone's UI; they only answer its API
# calls. The assets directory is generated and git-ignored — run this (or
# `npm run sync:mobile` in frontend/) before `./gradlew assemble*`; the Gradle
# `checkMobileUi` task fails the build when the bundle is missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND="$ROOT/frontend"
MOBILE="$ROOT/mobile"
DEST="$ROOT/android/app/src/main/assets/mobile-ui"

if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "[sync-mobile-ui] installing frontend dependencies (shared with mobile/)"
  (cd "$FRONTEND" && npm ci)
fi

echo "[sync-mobile-ui] building mobile/"
(cd "$MOBILE" && npm run deps && npm run build)

if [ ! -f "$MOBILE/build/index.html" ]; then
  echo "[sync-mobile-ui] mobile/build/index.html is missing after the build" >&2
  exit 1
fi

echo "[sync-mobile-ui] copying mobile/build -> android/app/src/main/assets/mobile-ui"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$MOBILE/build/." "$DEST/"

# Provenance for bug reports: which UI is inside this APK. Same source as the
# bundle's __APP_VERSION__ (frontend/app-version.js: APP_VERSION, else
# frontend/package.json, which tracks the release tag).
version="$(node --input-type=module -e "const { pathToFileURL } = await import('node:url'); const { appVersion } = await import(pathToFileURL(process.argv[1]).href); console.log(appVersion)" "$FRONTEND/app-version.js")"
commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '{"version":"%s","commit":"%s","builtAt":"%s"}\n' "$version" "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/version.json"

echo "[sync-mobile-ui] done: $(find "$DEST" -type f | wc -l | tr -d ' ') files, ui $version ($commit)"
