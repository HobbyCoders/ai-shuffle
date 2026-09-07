# AI Shuffle Desktop

The desktop app is a native window around the web UI of any AI Shuffle worker,
for Windows, macOS and Linux. It is the desktop counterpart of the Android app:
one thin shell per platform, with the UI itself always served by the worker so
the two never drift apart.

## What the shell does

- Opens a worker at its real origin: the local install on this computer, a box
  on the LAN, or a remote managed worker through its tunnel hostname. Requests
  stay same-origin, so the server's CSRF and WebSocket origin checks behave as
  they do in a browser. Sign-in state lives in the app's own Chromium profile.
- Frameless window with the OS window controls overlaid. The web UI's top
  strip is the drag handle and keeps clear of the buttons through the Window
  Controls Overlay API (`env(titlebar-area-*)`); see the `html.desktop` rules
  in `frontend/src/app.css` and `frontend/src/lib/services/desktop.ts`.
- One unified strip. Inside the app the sidebar head and the main panel's top
  bar read as a single black band holding what they already held: brand,
  search and bell on the left; sidebar toggle, breadcrumb, the current page's
  actions and the account badge on the right. Feature pages lend their header
  actions to the strip through `$lib/stores/page-header.svelte` (claimed by
  `StudioPageHeader` while the page is on screen) and drop their own large
  header, so the content gets the height back.
- Native menu with the usual accelerators (new session, command palette,
  toggle sidebar, settings, zoom, full screen), a tray icon, native
  notifications, and window placement that survives restarts.
- Auto-update through electron-updater, fed by the GitHub release that
  matches the server's `v*` tag.

## The bridge

The preload exposes `window.shuffleDesktop` to the worker page. The UI uses it
to notice it is inside the app, to colour the OS window controls after the
theme, to receive menu commands, to raise native notifications, and to show
update state. The connect screen additionally gets `window.shuffleDesktopServers`
for the saved-worker list; a worker page never sees other workers.

## Develop

```sh
cd desktop
npm ci
npm start
```

`npm start` compiles `src/` to `dist/` and launches Electron. Useful
environment variables in development:

| Variable | Purpose |
| --- | --- |
| `SHUFFLE_DESKTOP_USER_DATA` | Use an isolated profile directory instead of the real one. |
| `SHUFFLE_DESKTOP_SHOT` | Write a PNG of the window after load and quit (used by CI). |
| `SHUFFLE_DESKTOP_SHOT_DELAY` | Milliseconds to wait before the screenshot (default 4000). |
| `SHUFFLE_DESKTOP_NO_UPDATES` | Skip update checks in a packaged build. |

To review frontend changes inside the shell before they are deployed, run the
frontend dev server with its API proxy pointed at a worker (see
`frontend/vite.config.js`) and add `http://localhost:5173` as a worker in the
app.

Tests: `npm test` (unit tests for the saved-worker store). `npm run pack`
builds an unpacked app in `release/` for the current platform; `npm run dist`
builds the installers.

## Release

`.github/workflows/release.yml` builds the installers on all three platforms
after the server packages and uploads them, with the `latest*.yml` update
metadata, to the same GitHub release. The desktop version is stamped from the
tag, so the shell carries the same version number as the server it ships with.

Code signing is not configured yet. Windows and Linux builds install and
update unsigned (Windows shows a SmartScreen prompt on first install). macOS
requires a Developer ID certificate and notarisation before the built-in
updater can replace the app; until then macOS users reinstall from the DMG.
