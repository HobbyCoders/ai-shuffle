# AI Shuffle browser extension

A Manifest V3 extension for Chrome and Microsoft Edge (Windows, macOS and
Linux). It does two things:

- **Side panel.** Sign in with your Shuffle account and the panel lists your
  workers. Pick one to chat with it in the phone layout of AI Shuffle.
- **A browser your agents can drive.** Every connected worker lists this
  browser as a choice in the Browser panel's browser picker. A chat that picks
  it works in your real tabs: each chat gets its own tab group, named after the
  chat, and the agent only ever touches tabs in its group.

User-facing documentation lives in `docs/content/features/browser-extension.md`.

## Install for testing

```sh
python browser-extension/build.py
```

1. Open `chrome://extensions` (or `edge://extensions`) and turn on
   **Developer mode**.
2. Choose **Load unpacked** and select `browser-extension/dist/unpacked`.
3. Pin AI Shuffle and click it to open the side panel, then sign in.

The unpacked build carries the public key in `dev-key.txt`, so its ID is
always `fnjpjlkegjhjepkdnnignfokaffgdmak`. The account service and workers
accept that ID for sign-in and for framing the side panel. After you change a
file, run `build.py` again and press the reload button on the extension card.

## How it connects

- **Sign-in.** The flow is the same as the Android app: account service OAuth
  with PKCE through `chrome.identity.launchWebAuthFlow`, then a ticket for each
  worker, redeemed at the worker's `/api/v1/mobile/hub/enroll` for a device
  key. That key has the scopes `controller`, `web:session` and
  `browser:extension`.
- **Browser control.** The service worker keeps a WebSocket to
  `/api/v1/browser-extension/connect` on each worker. The worker relays the
  agent runtime's CDP traffic to it (`app/core/browser_extension.py`), and the
  extension forwards it to the chat's tabs with `chrome.debugger`.
- **Side panel.** The panel frames the worker's `/mobile/` UI with a
  web-session token. Workers allow `chrome-extension://` framing for the
  developer ID and for any IDs listed in `AI_SHUFFLE_BROWSER_EXTENSION_IDS`.

## Publishing to the Chrome Web Store and Edge Add-ons

`build.py` also writes `dist/ai-shuffle-extension-<version>.zip` without the
`key`, ready to upload. Each store assigns its own extension ID. Before the
store build can sign in, add the store IDs in three places:

1. The account service's `EXTENSION_IDS`.
2. The Clerk redirect URIs, as `https://<id>.chromiumapp.org/`.
3. `EXTENSION_IDS` in `app/core/browser_extension.py`, so every worker lets
   the side panel frame `/mobile/`. A single worker can add IDs through the
   `AI_SHUFFLE_BROWSER_EXTENSION_IDS` environment variable (comma-separated).

The store listing has to justify the `debugger` permission and access to all
http and https sites: agents work in pages the user chooses, and only in the
chat's tab group. Bump `version` in `manifest.json` for every upload.
