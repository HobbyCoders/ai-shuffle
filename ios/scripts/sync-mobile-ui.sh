#!/usr/bin/env bash
# Build the mobile UI (mobile/) and copy it into ios/mobile-ui/.
#
# The iOS app ships its own copy of the UI, the same build the Android app
# bundles, and serves it from the app bundle at aishuffle://app/assets/mobile-ui/
# (BundleSchemeHandler). Workers never serve the phone's UI; they only answer
# its API calls. ios/mobile-ui/ is generated and git-ignored. Run this before
# building in Xcode; the app target's pre-build check fails without it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND="$ROOT/frontend"
MOBILE="$ROOT/mobile"
DEST="$ROOT/ios/mobile-ui"

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

echo "[sync-mobile-ui] copying mobile/build -> ios/mobile-ui"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$MOBILE/build/." "$DEST/"

# Provenance for bug reports, same format as the Android copy.
version="$(node --input-type=module -e "const { pathToFileURL } = await import('node:url'); const { appVersion } = await import(pathToFileURL(process.argv[1]).href); console.log(appVersion)" "$FRONTEND/app-version.js")"
commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '{"version":"%s","commit":"%s","builtAt":"%s"}\n' "$version" "$commit" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEST/version.json"

echo "[sync-mobile-ui] done: $(find "$DEST" -type f | wc -l | tr -d ' ') files, ui $version ($commit)"
