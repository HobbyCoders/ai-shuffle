# Testing the AI Shuffle Android app (spine)

This first release is the **communication spine** only: connect to one or more
self-hosted AI Shuffle servers, send messages through the server's web UI in a
WebView, and receive push notifications. Device features (calendar, SMS, etc.)
are intentionally deferred.

## 1. Backend prerequisite

The app talks to mobile endpoints that ship on the AI Shuffle branch
**`feat/android-mobile-shell-app`** (not yet on AI Shuffle `main`). Deploy that
branch before testing. It adds:

- `POST /api/v1/mobile/web-session` — mints a web `session` cookie from an `aim_`
  device key so the WebView can log in.
- `POST /api/v1/mobile/push/unifiedpush` — stores the device's UnifiedPush
  endpoint.
- Server-side notification hooks that fire on **session finished**, **question
  for you**, and **permission needed**, broadcast to every registered device.

A `mobile_devices.unifiedpush_endpoint` column is added automatically by the
startup migration.

## 2. Install the app

- Grab the APK from the GitHub release **v0.1.0-spine**, or build it:
  ```bash
  ./gradlew assembleDebug
  # -> app/build/outputs/apk/debug/app-debug.apk
  ```
- It is **debug-signed**. Allow "install from unknown sources" for your browser
  or file manager. minSdk 26 (Android 8.0+), targetSdk 34.

## 3. Notifications: install a UnifiedPush distributor

Push uses [UnifiedPush](https://unifiedpush.org), so notifications are fully
self-hosted — no Google FCM. Install any distributor app, e.g. **ntfy**
(F-Droid / Play), and point it at your ntfy server (or ntfy.sh for a quick test).
With no distributor installed the app still works; it just shows a one-time hint
and won't receive pushes.

## 4. Connect a server

1. Launch **AI Shuffle**, tap **＋**.
2. Enter a display name and the server URL (`http://host:port` or `https://…`).
3. Tap **Connect** — the app calls `/register`, stores the returned `aim_` key in
   `EncryptedSharedPreferences`, and (if a push endpoint is known) registers it.
4. Tap the server to open the chat WebView. Repeat **＋** for more servers.

## 5. What to verify

- [ ] Add a server and open it; the web UI loads already-authenticated.
- [ ] Send a message from the app and get a normal response.
- [ ] Start a long task, background the app → **Session finished** notification.
- [ ] Trigger a permission/question prompt → matching notification arrives.
- [ ] Add a second server; both appear and open independently.
- [ ] Toggle airplane mode on → the connection notice reads **Waiting for
      network…** and the log stops attempting reconnects (no per-minute radio
      wake-ups). Toggle it off → streams reconnect within a second or two.

## Notes / limitations

- Debug build, cleartext HTTP allowed (`usesCleartextTraffic`) for self-hosted
  LAN servers. A release build should pin to HTTPS.
- The web-session endpoint grants a full web session from any valid device key —
  acceptable for a single-owner self-hosted deployment; revisit before any
  multi-user exposure.
- One push endpoint is shared across all connected servers and (re)registered
  with each on rotation.
