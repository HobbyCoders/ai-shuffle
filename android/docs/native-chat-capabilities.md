# Files and microphone in embedded chat

Chat file and image downloads use Android's system Save dialog. Files are streamed to the selected document, with one active save per chat and a 128 MB limit. A save expires after five minutes, including time spent choosing a destination. Cancelling, navigating, closing chat, removing the worker or changing accounts stops the transfer and attempts to delete its incomplete destination. A process crash or a document provider that refuses deletion can leave an incomplete file; retry the download in that case.

Ordinary HTTPS file requests use the current WebView session and optional Cloudflare Access headers. Initial URLs and every redirect must match the selected worker's exact HTTPS origin, including its port. Cross-origin and HTTP redirects fail without forwarding credentials. Same-origin redirects are limited to five. Authentication and network failures show an in-app error; they do not save the error response as a file.

The web frontend also creates downloads as Blob URLs and inline image data URLs. On supported Android System WebView versions, AndroidX WebKit installs a download-only message listener and a document-start script restricted to the worker origin. The native listener additionally checks the main frame, current page and saved worker/account before accepting a request. Blob bytes cross the boundary in acknowledged chunks of at most 32 KB. The protocol cannot read native files, return credentials, choose a storage destination, invoke arbitrary Android methods or launch another app. There is no `JavascriptInterface` and the existing frame/content restrictions stay in place. The script retains a Blob before the frontend's immediate URL revocation, without loosening the page's content security policy.

This integration requires WebView support for `WEB_MESSAGE_LISTENER` and `DOCUMENT_START_SCRIPT`. If those capabilities are missing, ordinary HTTPS file downloads remain available; Blob or data downloads ask the user to update Android System WebView and refresh chat.

Embedded chat can request audio capture from the current worker's HTTPS document. Android microphone permission must also be granted. Camera, combined audio/video requests and requests from other origins are rejected. Permission and file-picker results are discarded after navigation, worker removal or account changes. The Android picker and permission dialog may temporarily pause chat without invalidating their own pending result. This does not permit microphone use over HTTP.

Native voice sessions request `purpose=native` when obtaining their worker session token. Embedded chat uses `purpose=web`, so a backend implementing the independent session slots can keep both active at once.

When the web voice UI requests a native handoff, `shuffleNativeVoice` accepts only a start request with a correlation UUID and a bounded selected-chat ID. Like file saves, this listener is restricted to the worker's exact origin and main frame. The activity must be focused, resumed and still authorized for that worker. Its reply includes the request/chat IDs and is sent before opening the native voice screen. A request cannot choose a worker URL or provide credentials. The native voice implementation must carry the selected-chat extra through to the service; this bridge alone does not create a voice session or relax the contained page's network policy.

## Verification

```sh
node --test app/src/test/js/chat-downloads.test.cjs
./gradlew testDebugUnitTest lintDebug assembleDebug assembleDebugAndroidTest
adb shell am instrument -w -e class com.hobbycoders.aishuffle.ChatCapabilitiesSmokeTest com.hobbycoders.aishuffle.test/androidx.test.runner.AndroidJUnitRunner
```

The instrumentation fixture uses a real WebView, intercepted benign HTTP responses and a storage provider present only in the test APK. It does not connect to an account or a real worker, record microphone audio, or exercise a paid voice provider. Run it only on an isolated test device: like the existing native smoke tests, it resets the application's local account/worker state.
