# Testing the AI Shuffle Android app

## Automated

Build and unit-test commands are in [README.md](../README.md#build-and-test)
(JDK 21). Chat loads the paired worker's `/mobile/`, but the APK bundles a copy
of the phone UI for the offline demo, so run `bash scripts/sync-offline-demo.sh`
from `android/` before any `assemble*` task; Gradle's `checkOfflineDemo` task
fails the build without it. The chat smoke tests serve that copy as the fixture
worker's `/mobile/` (`androidTest/.../PhoneUiFixture.kt`).

Instrumentation tests (`ci/run-android-ui-tests.sh`) reset app state. Run them
on a disposable emulator, never on a phone with saved accounts. The existing
Android CI runs the full instrumentation suite against **debug**, not release.

Release builds use R8 and resource shrinking. `ci-android-minified.yml` separately
builds the production Play release bundle and a **releaseSmoke** APK with the
same optimization/keep rules, non-debuggable code and Play app-control setting.
Only the smoke variant has disposable debug signing, `testOnly=true`, and a
fixture launcher that writes a worker using the real model/encrypted store.
It has no test-only keep rules or reflection-based app-field injection. On
API 34 and 36 it checks real chat WebView shell/file-message round trips,
untrusted-origin isolation and a live QR camera frame. Its synthetic HTTPS
page does not exercise live worker authentication, OAuth or delivery from FCM;
those still need the device checks below. The full debug suite is not relabelled
as minified coverage.

To build the same disposable smoke locally (after bundling the offline demo):

```sh
./gradlew bundleRelease assembleReleaseSmoke assembleReleaseSmokeAndroidTest \
  -PplayStoreBuild=true -PminifiedSmoke=true
```

Release mappings are generated under `app/build/outputs/mapping/release/`.
The smoke workflow retains exact-build mappings briefly for diagnosis. The
current Play publishing workflow does **not** upload a mapping file; arrange
its upload in Play Console for the exact release being published. Do not use
a smoke mapping to deobfuscate a production release.

## Manual check on a device

Install from the Play internal-testing track, or build `assembleDebug` and
install `app/build/outputs/apk/debug/app-debug.apk`. minSdk 26 (Android 8.0+),
targetSdk 36. Workers must be reachable over HTTPS: the app does not allow
cleartext HTTP (`usesCleartextTraffic="false"`).

- [ ] Add a worker (account sign-in, or Add server with its HTTPS address or QR
      code). Opening it loads the worker's phone UI (`/mobile/`) already
      authenticated. A worker too old to serve `/mobile/` shows an
      "update the worker" message instead.
- [ ] Send a message and get a normal response.
- [ ] Add a second worker; both appear and open independently.
- [ ] Start a voice call from the chat page's own voice button; the first call
      asks for the microphone once, and hang-up from the page ends it (see
      [VOICE_ASSISTANT.md](VOICE_ASSISTANT.md)).
- [ ] Settings → offline demo opens without a worker.

The app does not receive push notifications; it only calls worker APIs while
it is open.
