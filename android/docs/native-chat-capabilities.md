# Files and microphone in embedded chat

Embedded chat appends `AIShuffleAndroid/<versionName>` to the WebView's default user agent (`ui/WebUserAgent.kt`). The web frontend reads that token before first paint and renders its mobile shell; every other browser, including a phone browser, gets the desktop shell regardless of width. See `docs/mobile-desktop-ui-split.md` at the repository root. The token adds no capability and carries no credentials; it is appended once and the rest of the user agent is unchanged so the server still sees the real WebView version.

Chat file and image downloads use Android's system Save dialog. Files are streamed to the selected document, with one active save per chat and a 128 MB limit. A save expires after five minutes, including time spent choosing a destination. Cancelling, navigating, closing chat, removing the worker or changing accounts stops the transfer and attempts to delete its incomplete destination. A process crash or a document provider that refuses deletion can leave an incomplete file; retry the download in that case.

Ordinary HTTPS file requests use the current WebView session and optional Cloudflare Access headers. Initial URLs and every redirect must match the selected worker's exact HTTPS origin, including its port. Cross-origin and HTTP redirects fail without forwarding credentials. Same-origin redirects are limited to five. Authentication and network failures show an in-app error; they do not save the error response as a file.

The web frontend also creates downloads as Blob URLs and inline image data URLs. On supported Android System WebView versions, AndroidX WebKit installs a download-only message listener and a document-start script restricted to the worker origin. The native listener additionally checks the main frame, current page and saved worker/account before accepting a request. Blob bytes cross the boundary in acknowledged chunks of at most 32 KB. The protocol cannot read native files, return credentials, choose a storage destination, invoke arbitrary Android methods or launch another app. There is no `JavascriptInterface` and the existing frame/content restrictions stay in place. The script retains a Blob before the frontend's immediate URL revocation, without loosening the page's content security policy.

This integration requires WebView support for `WEB_MESSAGE_LISTENER` and `DOCUMENT_START_SCRIPT`. If those capabilities are missing, ordinary HTTPS file downloads remain available; Blob or data downloads ask the user to update Android System WebView and refresh chat.

Embedded chat can request audio capture from the current worker's HTTPS document. Android microphone permission must also be granted. Camera, combined audio/video requests and requests from other origins are rejected. Permission and file-picker results are discarded after navigation, worker removal or account changes. The Android picker and permission dialog may temporarily pause chat without invalidating their own pending result. This does not permit microphone use over HTTP.

Native voice sessions request `purpose=native` when obtaining their worker session token. Embedded chat uses `purpose=web`, so a backend implementing the independent session slots can keep both active at once.

If Android stops chat before its initial web session is ready, returning to chat retries that interrupted load. Late callbacks from the cancelled attempt cannot load an obsolete session. Returning to an already loaded chat keeps its cookie rather than rotating it unnecessarily.

Account sync treats a changed HTTPS tunnel address from the authenticated hub as an endpoint update for the same worker. It reuses a saved device key only when hub, account, worker and device identity all match. Foreign identities must enroll normally. Optional Cloudflare Access service headers configured for the old origin are cleared when the origin changes; they are not carried to a new host. This avoids duplicate-enrollment conflicts while keeping unrelated credentials separate.

When the web voice UI requests a native handoff, `shuffleNativeVoice` accepts only a start request with a correlation UUID and a bounded selected-chat ID. Like file saves, this listener is restricted to the worker's exact origin and main frame. The activity must be focused, resumed and still authorized for that worker. Its single result includes the request/chat IDs; success is sent only after the native activity launch returns successfully, before lifecycle pause dispatch. A request cannot choose a worker URL or provide credentials. The native voice implementation must carry the selected-chat extra through to the service; this bridge alone does not create a voice session or relax the contained page's network policy.

## Verification

```sh
node --test app/src/test/js/chat-downloads.test.cjs
./gradlew testDebugUnitTest lintDebug assembleDebug assembleDebugAndroidTest
adb -s emulator-5554 shell am instrument -w \
  -e class com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest \
  -e notClass 'com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest#realSystemSaveDialogReturnsToChatAndCompletesTransfer' \
  com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner
```

The instrumentation fixture uses a real WebView, intercepted benign HTTP responses and a storage provider present only in the test APK. It does not connect to an account or a real worker, record microphone audio, or exercise a paid voice provider. Run it only on an isolated test device: like the existing native smoke tests, it resets the application's local account/worker state.

CI runs `VoicePermissionSmokeTest` separately, revoking microphone permission from the host before instrumentation starts. Revoking a granted permission inside the running test process kills that process on Android. The remaining suite can then grant permission for capability checks. Both runs use the strict result parser, which rejects crashes, failures, unexpected skips, and incomplete results.

`realSystemSaveDialogReturnsToChatAndCompletesTransfer` is a separate opt-in test requiring an external driver to complete the Android Save dialog. It is explicitly excluded from unattended CI rather than reported as a passing or skipped check there.
