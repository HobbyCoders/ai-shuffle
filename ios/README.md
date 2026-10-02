# AI Shuffle for iOS

A native SwiftUI shell around one locked-down `WKWebView`, built the same way
as the Android app (`android/`). The chat screen shows the app's own build of
the mobile UI (`mobile/`), never a page served by a worker. Bundle ID
`com.hobbycoders.aishuffle`, iOS 16 or later.

## Build

Needs a Mac with Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`). No Apple account is needed for the Simulator.

```sh
(cd frontend && npm ci)
bash ios/scripts/sync-mobile-ui.sh     # mobile/build -> ios/mobile-ui (git-ignored)
cd ios && xcodegen generate            # project.yml -> AIShuffle.xcodeproj (git-ignored)
xcodebuild test -project AIShuffle.xcodeproj -scheme AIShuffle \
  -destination 'platform=iOS Simulator,name=iPhone 16' CODE_SIGNING_ALLOWED=NO
```

The app target fails its pre-build check when `ios/mobile-ui/index.html` is
missing. CI runs the same steps in `.github/workflows/ios.yml` on PRs that touch
`ios/` or `mobile/`.

## How it maps to Android

| Android | iOS |
| --- | --- |
| `WebViewAssetLoader` at `https://appassets.androidplatform.net/assets/mobile-ui/` | `BundleSchemeHandler` at `aishuffle://app/assets/mobile-ui/` |
| `shouldInterceptRequest` allowlist (bundle + paired worker only) | `ChatPolicy.contentRules`: a WebKit content rule list that blocks every load except the bundle, the worker's `https://` and `wss://` origin, `data:` and `blob:` |
| `shouldOverrideUrlLoading` | `ChatWebController` navigation delegate using `ChatPolicy.navigation` |
| document-start script with the Bearer token | `WKUserScript` at document start, guarded to the bundle origin (`MobileUI.bootstrapScript`) |
| no `JavascriptInterface` | no `WKScriptMessageHandler` |
| microphone granted only to the asset origin | `WKUIDelegate` media-capture decision (`ChatPolicy.microphone`) |
| `EncryptedSharedPreferences` server store | one Keychain item, this device only (`ServerStore`) |
| `AIShuffleAndroid/<version>` user-agent token | `AIShuffleIOS/<version>` |
| `/api/v1/mobile/register` and web-session minting | `ApiClient` (same endpoints, `platform: "ios"`, no cookies, no redirects) |

TLS errors are never overridden; WebKit's default certificate checks apply.

## Not ported yet

- Workers accept `aishuffle://app` in CORS (`app/core/cors.py`, alongside the
  Android asset origin) from the release that added this app on. Older workers
  pair, but refuse the chat page's API calls until they update.
- Media the page loads straight from the worker (`<img src="https://worker/...">`)
  has no Authorization header. Android proxies those loads natively; WKWebView
  cannot intercept `https`, so these need another approach.
- Saving worker files (Android's `shuffleFileSave` download bridge).
- QR pairing (VisionKit), account hub sign-in, worker presence checks,
  Cloudflare Access service tokens, device tools.

Releasing and the TestFlight path: [RELEASING.md](RELEASING.md).
