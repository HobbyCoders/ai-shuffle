# Testing the AI Shuffle Android app

## Automated

Build and unit-test commands are in [README.md](../README.md#build-and-test)
(JDK 21). The APK bundles its own mobile UI, so run
`bash scripts/sync-mobile-ui.sh` from `android/` before any `assemble*` task;
Gradle's `checkMobileUi` task fails the build without it.

Instrumentation tests (`ci/run-android-ui-tests.sh`) reset app state. Run them
on a disposable emulator, never on a phone with saved accounts.

## Manual check on a device

Install from the Play internal-testing track, or build `assembleDebug` and
install `app/build/outputs/apk/debug/app-debug.apk`. minSdk 26 (Android 8.0+),
targetSdk 36. Workers must be reachable over HTTPS: the app does not allow
cleartext HTTP (`usesCleartextTraffic="false"`).

- [ ] Add a worker (account sign-in, or Add server with its HTTPS address or QR
      code). Opening it loads the bundled UI already authenticated.
- [ ] Send a message and get a normal response.
- [ ] Add a second worker; both appear and open independently.
- [ ] Start a voice call from the chat page's own voice button; the first call
      asks for the microphone once, and hang-up from the page ends it (see
      [VOICE_ASSISTANT.md](VOICE_ASSISTANT.md)).
- [ ] Settings → offline demo opens without a worker.

The app does not receive push notifications; it only calls worker APIs while
it is open.
