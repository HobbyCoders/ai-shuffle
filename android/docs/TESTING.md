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
It has no test-only keep rules or reflection-based app-field injection. A
standalone `minified-smoke` instrumentation APK carries its complete test runtime;
it has no Gradle dependency on `:app`, so AGP cannot deduplicate runner libraries
against code that R8 removed from the app. On API 34 and 36 it checks real chat
WebView shell/file-message round trips,
untrusted-origin isolation and a live QR camera frame. Its synthetic HTTPS
page does not exercise live worker authentication, OAuth or delivery from FCM;
those still need the device checks below. The full debug suite is not relabelled
as minified coverage.

Both debug and minified API 36 jobs use the Google APIs emulator image to permit
an absent, separately signed AOSP alternate provider. This reverses the earlier
AOSP-only image choice; six first-attempt hosted boots across the two workflows
reached stable launchers without ANR recovery, not proof of zero future GMS ANRs.
The provider is a **co-installed pinned AOSP alternate, not the phone provider**:
monolithic AOSP WebView 136.0.7103.129 is not Google's auto-updating phone WebView.
These jobs do not establish compatibility with the current Google provider or
protect phones still on 133. The native-abort/request-lifetime link is inferred.
The pinned Gitiles APK is SHA-keyed in `actions/cache`; its size and SHA-256 are
checked on hits/misses and again before installation, with all crash checks on.

The separate push-enabled debug fixture runs real OS notification permission
Allow/Deny decisions from both direct chat entry and the production launcher.
Each starts from cleared app data and permission flags, checks usable chat and a
physical Refresh action, and never pre-grants notification permission. Launcher
coverage also requires the real presence probe/parser and automatic worker
selection to reach chat after dismissing the prompt. Only worker HTTP/page
responses are fixtures; these checks do not cover Firebase delivery.

To build the same disposable smoke locally (after bundling the offline demo):

```sh
./gradlew :app:bundleRelease :app:assembleReleaseSmoke :minified-smoke:assembleDebug \
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
