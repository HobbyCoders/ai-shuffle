# AI Shuffle Android

The Android companion and account hub live in this repository alongside the server and Fluid harness.

## Install or update

**[Get AI Shuffle on Google Play](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle)** · [Release notes](https://github.com/HobbyCoders/ai-shuffle/releases)

Requires Android 8.0 or newer. Install and update through Google Play instead of downloading an APK from GitHub. Availability is subject to release-track access and Play review; testing releases require tester enrollment with the Google account used in Play.

The app keeps the same application ID and release signing key as the former standalone repository, so a signed update can preserve existing app data. Do not uninstall the app as an update step.

### Where builds go

- Versioned project releases publish a verified Docker image, then build a signed Android App Bundle (AAB) for Google Play **internal testing** (never production). Testers must join using the opt-in invitation from Play Console. Play distributes the installable APKs; the release workflow does not attach APK downloads to GitHub releases.
- Desktop installers are disabled by default. Select `build_desktop` when manually running the release workflow to build one; existing desktop installations update inside the app.
- Play publishing requires the existing signing key, verified Play signing enrollment, a Play publishing service account, and explicit compatible-worker readiness. Configure the `google-play-internal` GitHub environment to allow `main` and `v*` release tags, store `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` there, and set `PLAY_SIGNING_READY=true` only after verifying Play's app-signing certificate. Repository variable `ANDROID_WORKERS_READY=true` confirms compatible deployed workers; publishing also checks it in the Play environment. A successful bundle build alone does not mean the app is available on Play.
- Moving beyond internal testing (account deletion, the Data safety form, the store listing text in `play/listing/`) is covered in [RELEASING.md](RELEASING.md).
- [Android CI](https://github.com/HobbyCoders/ai-shuffle/actions/workflows/android.yml) still verifies Android pull requests and supports manual APK builds for troubleshooting. Pull requests never publish downloads. Actions artifacts require repository access and expire.

Locally, `./gradlew assembleRelease` produces `app/build/outputs/apk/release/app-release.apk` when signing is configured. Debug builds use `./gradlew assembleDebug` and produce `app/build/outputs/apk/debug/app-debug.apk`.

The APK ships its own build of the mobile UI (`mobile/` at the repository root, sharing `frontend/src/lib`). Run `bash scripts/sync-mobile-ui.sh` (or `npm run sync:mobile` in `frontend/`) first: it builds `mobile/` and copies it to `app/src/main/assets/mobile-ui/` (git-ignored). Gradle's `checkMobileUi` task refuses to assemble without it. Workers never serve the phone's UI; the app only calls their APIs (see `docs/mobile-desktop-ui-split.md`).

## Build and test

Use JDK 21 (Robolectric needs it; app bytecode targets 17) and the Android SDK. From this directory:

```sh
./gradlew testDebugUnitTest lintDebug assembleDebug assembleDebugAndroidTest
node --test app/src/test/js/chat-downloads.test.cjs
```

Release builds use `KEYSTORE_PATH`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, and `KEY_PASSWORD`. GitHub Actions reconstructs the keystore from the repository's `KEYSTORE_BASE64` secret. Never commit a keystore or its passwords.

CI sets `ANDROID_VERSION_CODE` to the build time in Unix minutes so signed updates advance across main and versioned release workflows. `ANDROID_VERSION_NAME` follows the release tag for versioned releases; main builds use the version in Gradle. `android-build.json` records both versions, source commit, verification run and APK SHA-256.

CI helpers are in `ci/`; run them from this directory. Instrumentation tests reset app state and belong on a disposable emulator, not a phone with saved accounts.

### Mobile input regression checks

- Chat attachments: select two files in one picker invocation and confirm both appear in the composer. Repeat with one file and cancellation. The native bridge reads multi-selection `ClipData`, preserves picker order, and accepts only 1–32 `content://` URIs while the originating document and worker remain authorized. `ChatFileChooserTest` covers parsing and limits; `ChatCapabilitiesSmokeTest` covers delivery through the activity callback and ownership rechecks.
- Browser panel: choose **Fit panel**, tap a text field, type with the Android keyboard, then tap outside it. Remote viewport auto-fit pauses while a native editor is focused so the IME's local resize cannot remove the focused field. Auto-fit resumes on blur. Check both first-tap takeover and an already-controlled page, and confirm scrolling outside a field does not summon the keyboard. `BrowserPanel.test.ts` covers the fit timer/focus regression; desktop browser emulation alone cannot establish Android IME behavior.

The Cloudflare account hub is in `hub/`. Its deployment workflow uses the `account-hub-production` environment in this repository.

The image registry Worker for `registry.hobbycoders.com` is in `registry/`; its deployment workflow (`deploy-registry.yml`) uses the `registry-production` environment. Owner steps: `registry/README.md`.

Report problems in the [shared issue tracker](https://github.com/HobbyCoders/ai-shuffle/issues).
