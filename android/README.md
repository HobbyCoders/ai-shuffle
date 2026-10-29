# AI Shuffle Android

The Android companion and account hub live in this repository alongside the server and Fluid harness.

## Install or update

**[Get AI Shuffle on Google Play](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle)** · [Release notes](https://github.com/HobbyCoders/ai-shuffle/releases)

Requires Android 8.0 or newer. Install and update through Google Play instead of downloading an APK from GitHub. Availability is subject to release-track access and Play review; testing releases require tester enrollment with the Google account used in Play.

The app keeps the same application ID and release signing key as the former standalone repository, so a signed update can preserve existing app data. Do not uninstall the app as an update step.

### Where builds go

- Versioned project releases publish a verified Docker image, then build a signed Android App Bundle (AAB) for Google Play **internal testing** (never production). Testers must join using the opt-in invitation from Play Console. Play distributes installable APKs; the release workflow also attaches a signed APK, checksum and provenance to GitHub for sideloading.
- Every release promoted to latest stable publishes Windows, Linux and Android downloads, including sealed-candidate promotion. The release fails if the hub download list does not match the tag and revision for all three platforms; existing desktop installations can still update inside the app.
- Play publishing requires the existing signing key, verified Play signing enrollment, a Play publishing service account, and explicit compatible-worker readiness. Configure the `google-play-internal` GitHub environment to allow `main` and `v*` release tags, store `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` there, and set `PLAY_SIGNING_READY=true` only after verifying Play's app-signing certificate. Repository variable `ANDROID_WORKERS_READY=true` confirms compatible deployed workers; publishing also checks it in the Play environment. A successful bundle build alone does not mean the app is available on Play.
- Moving beyond internal testing (account deletion, the Data safety form, the store listing text in `play/listing/`) is covered in [RELEASING.md](RELEASING.md).
- [Android CI](https://github.com/HobbyCoders/ai-shuffle/actions/workflows/android.yml) still verifies Android pull requests and supports manual APK builds for troubleshooting. Pull requests never publish downloads. Actions artifacts require repository access and expire.

Locally, `./gradlew assembleRelease` produces `app/build/outputs/apk/release/app-release.apk` when signing is configured. Debug builds use `./gradlew assembleDebug` and produce `app/build/outputs/apk/debug/app-debug.apk`.

Chat shows the phone UI the paired worker serves at `https://<worker>/mobile/` (`mobile/` at the repository root, sharing `frontend/src/lib`; served by `app/mobile_ui.py`). A UI change therefore ships with a worker update; a new APK is needed only for native changes: phone tools, permissions, pairing, or a new native capability (`ui/MobileUi.kt` `CAPABILITIES`). See `docs/mobile-desktop-ui-split.md`.

The APK bundles one copy of the phone UI for the offline demo only (Settings → offline demo, no worker). Run `bash scripts/sync-offline-demo.sh` before assembling: it builds `mobile/` and copies it to `app/src/main/assets/offline-demo/` (git-ignored). Gradle's `checkOfflineDemo` task refuses to assemble without it.

## Firebase setup for Needs you push

Push is optional and free; no Firebase resources are created by this repository. After merge, the owner/coordinator must:

1. Create/select a Firebase project, enable **Firebase Cloud Messaging API (HTTP v1)**, and register an Android app with package **`com.hobbycoders.aishuffle`**. For FCM alone, **no SHA-1 or SHA-256 certificate fingerprints are required**. Do not change the existing Play signing key; if another Firebase feature later needs fingerprints, use the Play App Signing certificate (and debug certificate for debug builds), not an arbitrary replacement key.
2. Download that Android app's `google-services.json`. Store its complete JSON as the **repository Actions secret `GOOGLE_SERVICES_JSON`**. Both existing Android build workflows write it to `android/app/google-services.json` when available on manual/release builds; pull requests deliberately test the no-config build. It is git-ignored. Gradle applies the Google Services plugin only if this file exists; without it the app still builds with `PUSH_ENABLED=false` and does not register or receive push.
3. Create/download a sender service-account JSON for that same project with the least permission needed to send FCM messages (the `cloudmessaging.messages.create` permission, for example via Firebase Cloud Messaging API Admin). This is a **different secret** from the client `google-services.json` and from Play publishing credentials. Enter it only in the worker's **Settings → General → Notifications** password field (paste). It is encrypted and synced like provider billing keys. Never store it in GitHub source, an env file, issue, PR or chat.
4. Install the Firebase-configured Android build, pair each worker you want to receive from, and accept the one-time notification prompt. Confirm that each worker's Notifications row shows credentials set and a registered device. On a real phone, exercise one question/answer and verify one alert, silent additions, cancellation and tap routing. Real FCM/device delivery cannot be verified without this owner setup.

There is one high-importance **Needs you** channel, no push foreground service and no ongoing push notification. Device registrations are authenticated and worker-local. The server's owner-attention outbox plus Android's persisted per-session receipts deduplicate retries, reconnects and worker mirrors. Only the executing chat owner worker may send. User settings and Android can still suppress display; FCM acceptance never claims the owner saw the notification.

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

- Reddit login: open **Log In**, then tap **Email or username** and **Password**. These controls live inside open Shadow DOM, so the browser worker must discover nested roots and deliver composed input events to the page. Slotted labels should retain their names; password contents must never appear in shared workspace metadata. `browser-mcp/test/editors.test.ts` covers these contracts, and the browser workspace integration exercises a shadow-root field through the desktop/mobile panel. Closed shadow roots and iframe text fields are not covered by this fix.

- Chat attachments: select two files in one picker invocation and confirm both appear in the composer. Repeat with one file and cancellation. The native bridge reads multi-selection `ClipData`, preserves picker order, and accepts only 1–32 `content://` URIs while the originating document and worker remain authorized. `ChatFileChooserTest` covers parsing and limits; `ChatCapabilitiesSmokeTest` covers delivery through the activity callback and ownership rechecks.
- Browser panel: choose **Fit panel**, tap a text field, type with the Android keyboard, then tap outside it. Remote viewport auto-fit pauses while a native editor is focused so the IME's local resize cannot remove the focused field. Auto-fit resumes on blur. Check both first-tap takeover and an already-controlled page, and confirm scrolling outside a field does not summon the keyboard. `BrowserPanel.test.ts` covers the fit timer/focus regression; desktop browser emulation alone cannot establish Android IME behavior.

The Cloudflare account hub and its deployment workflow live in the private
[HobbyCoders/hobbycoders-cloud](https://github.com/HobbyCoders/hobbycoders-cloud/tree/main/hub)
repository. They are not an Android build dependency.

The retired private image-registry Worker is removed; public app image downloads
use GHCR as described in the app installation guide.

Report problems in the [shared issue tracker](https://github.com/HobbyCoders/ai-shuffle/issues).
