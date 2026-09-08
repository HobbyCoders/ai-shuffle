#!/usr/bin/env bash
# Run only after every verification job succeeds. No signing secrets needed.
set -euo pipefail
cd "${1:?Download directory required}"
sha256sum --check AI-Shuffle-Android.apk.sha256
files=(AI-Shuffle-Android.apk AI-Shuffle-Android.apk.sha256 android-build.json)

if [ -n "${RELEASE_TAG:-}" ]; then
  version="$(printf '%s' "$RELEASE_TAG" | tr -d '[:space:]')"
  version="${version#v}"; version="${version#V}"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || exit 1
  # The parent workflow creates the release; never silently create the wrong tag.
  gh release view "v$version" --repo "$GITHUB_REPOSITORY" > /dev/null
  gh release upload "v$version" "${files[@]}" --clobber --repo "$GITHUB_REPOSITORY"
  exit 0
fi

# A re-run of an older commit must not replace a newer successful main build.
if gh release view android-latest --repo "$GITHUB_REPOSITORY" > /dev/null 2>&1; then
  previous="$(mktemp -d)"
  gh release download android-latest --pattern android-build.json --dir "$previous" --repo "$GITHUB_REPOSITORY"
  if ! python3 - "$previous/android-build.json" android-build.json <<'PY'
import json, sys
old, new = [json.load(open(path)) for path in sys.argv[1:]]
sys.exit(0 if new['runNumber'] >= old['runNumber'] else 1)
PY
  then
    echo 'A newer Android build is already published; leaving it in place.'
    exit 0
  fi
  exists=true
else
  exists=false
fi

python3 - <<'PY' > release-notes.md
import json, os
info = json.load(open('android-build.json'))
repo = os.environ['GITHUB_REPOSITORY']
print(f"""## Android APK

[Download latest Android APK](https://github.com/{repo}/releases/download/android-latest/AI-Shuffle-Android.apk)

Version **{info['versionName']}**, build **{info['versionCode']}**. Requires Android 8.0 or newer.

Install over your existing AI Shuffle app to keep your accounts, workers and settings.

This rolling download follows verified builds of main. Android unit tests, lint,
account hub tests and emulator checks passed before publication.

Source: [{info['commit']}](https://github.com/{repo}/commit/{info['commit']}) · [Verification run]({info['runUrl']})

The APK, SHA-256 checksum and build metadata are attached below. The download is
updated in place; use the source commit above to identify this build.
""")
PY

if [ "$exists" = true ]; then
  gh release upload android-latest "${files[@]}" --clobber --repo "$GITHUB_REPOSITORY"
  gh release edit android-latest --title 'Android — latest verified APK' --notes-file release-notes.md --prerelease --latest=false --repo "$GITHUB_REPOSITORY"
else
  gh release create android-latest "${files[@]}" --target "$GITHUB_SHA" --title 'Android — latest verified APK' --notes-file release-notes.md --prerelease --latest=false --repo "$GITHUB_REPOSITORY"
fi
