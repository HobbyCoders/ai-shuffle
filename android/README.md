# AI Shuffle Android

The Android companion and account hub live in this repository alongside the server and Fluid harness.

## Install or update

Open [Android CI](https://github.com/HobbyCoders/ai-shuffle/actions/workflows/android.yml), select a successful run for `main`, and download the `ai-shuffle-release-apk` artifact. Extract and install `app-release.apk`. GitHub requires sign-in to download workflow artifacts.

The app keeps the same application ID and release signing key as the former standalone repository, so a signed update can preserve existing app data. Do not uninstall the app as an update step.

## Build and test

Use JDK 17 and the Android SDK. From this directory:

```sh
./gradlew testDebugUnitTest lintDebug assembleDebug assembleDebugAndroidTest
node --test app/src/test/js/chat-downloads.test.cjs
```

Release builds use `KEYSTORE_PATH`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, and `KEY_PASSWORD`. GitHub Actions reconstructs the keystore from the repository's `KEYSTORE_BASE64` secret. Never commit a keystore or its passwords.

CI helpers are in `ci/`; run them from this directory. Instrumentation tests reset app state and belong on a disposable emulator, not a phone with saved accounts.

The Cloudflare account hub is in `hub/`. Its deployment workflow uses the `account-hub-production` environment in this repository.

Report problems in the [shared issue tracker](https://github.com/HobbyCoders/ai-shuffle/issues).
