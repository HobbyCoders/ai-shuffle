# AI Shuffle for iOS

A native SwiftUI shell around one locked-down `WKWebView`, built the same way
as the Android app (`android/`). The chat screen shows the phone UI the paired
worker serves at `https://<worker>/mobile/` (built from `mobile/`, served by
`app/mobile_ui.py`), so UI changes ship with a worker update, not an App Store
build. The app owns pairing, permissions and the native capabilities it
advertises to the page (`MobileUI.capabilities`). Bundle ID
`com.hobbycoders.aishuffle`, iOS 16 or later. Design and rollout:
[docs/mobile-desktop-ui-split.md](../docs/mobile-desktop-ui-split.md).

## Build

Needs a Mac with Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`). No Apple account is needed for the Simulator.

```sh
cd ios && xcodegen generate            # project.yml -> AIShuffle.xcodeproj (git-ignored)
xcodebuild test -project AIShuffle.xcodeproj -scheme AIShuffle \
  -destination 'platform=iOS Simulator,name=iPhone 16' CODE_SIGNING_ALLOWED=NO
```

The tests need no worker: the WebKit tests load a fixture page at
`https://worker.invalid/mobile/`. CI runs the same steps in
`.github/workflows/ios.yml` on PRs that touch `ios/`. A change under `mobile/`
needs no app build at all.

## How it maps to Android

| Android | iOS |
| --- | --- |
| loads `MobileUi.indexUrl` (`https://<worker>/mobile/`) | loads `MobileUI.indexURL(base:)`, the same URL |
| `shouldInterceptRequest` allowlist (paired worker only) | `ChatPolicy.contentRules`: a WebKit content rule list that blocks every load except the worker's `https://` and `wss://` origin, `data:` and `blob:` |
| `shouldOverrideUrlLoading` | `ChatWebController` navigation delegate using `ChatPolicy.navigation` |
| document-start script with the Bearer token and `native` capabilities | `WKUserScript` at document start, guarded to the worker's top-level `/mobile/` page (`MobileUI.bootstrapScript`) |
| no `JavascriptInterface` | no `WKScriptMessageHandler` |
| microphone granted only to the phone UI page | `WKUIDelegate` media-capture decision (`ChatPolicy.microphone`) |
| `EncryptedSharedPreferences` server store | one Keychain item, this device only (`ServerStore`) |
| `AIShuffleAndroid/<version>` user-agent token | `AIShuffleIOS/<version>` |
| `/api/v1/mobile/register` and web-session minting | `ApiClient` (same endpoints, `platform: "ios"`, no cookies, no redirects) |

TLS errors are never overridden; WebKit's default certificate checks apply.

A worker from before `/mobile/` existed answers the page with 404; the app then
asks for a worker update instead of showing a blank chat.

## Not ported yet

- Media the page loads straight from the worker (`<img src="https://worker/...">`)
  has no Authorization header. Android proxies those loads natively; WKWebView
  cannot intercept `https`, so these need another approach.
- Saving worker files (Android's `shuffleFileSave` download bridge).
- QR pairing (VisionKit), account hub sign-in, worker presence checks,
  Cloudflare Access service tokens, device tools.

Releasing and the TestFlight path: [RELEASING.md](RELEASING.md).
