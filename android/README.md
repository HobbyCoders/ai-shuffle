# AI Shuffle Android

The Android companion and account hub live in this repository alongside the server and Fluid harness.

## Install or update

**[Download latest Android APK](https://github.com/HobbyCoders/ai-shuffle/releases/download/android-latest/AI-Shuffle-Android.apk)** · [Version, build details and checksum](https://github.com/HobbyCoders/ai-shuffle/releases/tag/android-latest)

Requires Android 8.0 or newer. Open `AI-Shuffle-Android.apk` on your phone and allow installation from your browser if Android asks. The download link is also available on the account hub's home page. Release downloads do not require ZIP extraction.

**GitHub access:** this repository is private, so sign in to GitHub in your browser with an account that has repository access before downloading. GitHub may show a 404 page when you are signed out or lack access. The APK release link is permanent; it does not bypass repository permissions.

The app keeps the same application ID and release signing key as the former standalone repository, so a signed update can preserve existing app data. Do not uninstall the app as an update step.

### Where builds go

- Android changes on `main` run [Android CI](https://github.com/HobbyCoders/ai-shuffle/actions/workflows/android.yml). After unit tests, lint, hub tests and emulator checks pass, CI updates the **Android — latest verified APK** prerelease at `android-latest`. This is a rolling main build; its notes identify the exact source commit. It does not replace the latest stable desktop release.
- Versioned project releases build and verify Android from the same checkout, then attach `AI-Shuffle-Android.apk`, its SHA-256 checksum and `android-build.json` to that release.
- Pull requests run verification but never publish downloads. Fork pull requests skip release signing because repository secrets are unavailable.
- CI also keeps the original `ai-shuffle-release-apk` workflow artifact for troubleshooting. Actions artifacts require sign-in and expire; release downloads remain available under the repository's access rules.

Locally, `./gradlew assembleRelease` produces `app/build/outputs/apk/release/app-release.apk` when signing is configured. Debug builds use `./gradlew assembleDebug` and produce `app/build/outputs/apk/debug/app-debug.apk`.

The APK ships its own build of the mobile UI (`mobile/` at the repository root, sharing `frontend/src/lib`). Run `bash scripts/sync-mobile-ui.sh` (or `npm run sync:mobile` in `frontend/`) first: it builds `mobile/` and copies it to `app/src/main/assets/mobile-ui/` (git-ignored). Gradle's `checkMobileUi` task refuses to assemble without it. Workers never serve the phone's UI; the app only calls their APIs (see `docs/mobile-desktop-ui-split.md`).

## Build and test

Use JDK 17 and the Android SDK. From this directory:

```sh
./gradlew testDebugUnitTest lintDebug assembleDebug assembleDebugAndroidTest
node --test app/src/test/js/chat-downloads.test.cjs
```

Release builds use `KEYSTORE_PATH`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, and `KEY_PASSWORD`. GitHub Actions reconstructs the keystore from the repository's `KEYSTORE_BASE64` secret. Never commit a keystore or its passwords.

CI sets `ANDROID_VERSION_CODE` to the build time in Unix minutes so signed updates advance across main and versioned release workflows. `ANDROID_VERSION_NAME` follows the release tag for versioned releases; main builds use the version in Gradle. `android-build.json` records both versions, source commit, verification run and APK SHA-256.

CI helpers are in `ci/`; run them from this directory. Instrumentation tests reset app state and belong on a disposable emulator, not a phone with saved accounts.

The Cloudflare account hub is in `hub/`. Its deployment workflow uses the `account-hub-production` environment in this repository.

Report problems in the [shared issue tracker](https://github.com/HobbyCoders/ai-shuffle/issues).
