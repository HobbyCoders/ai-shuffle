# AI Shuffle Desktop

The desktop app runs an AI Shuffle worker on this computer and shows its web
UI in a native window, for Windows, macOS and Linux. It is the desktop
counterpart of the Android app: the same UI, served by the worker the app
starts, so the two never drift apart.

## What the app does

- Starts the worker on launch (`python -m app.main` in local mode, listening
  on 127.0.0.1:8000) and opens its UI once the worker answers. If a worker is
  already running on that port, the app attaches to it instead of starting a
  second one. Quitting the app stops the worker it started.
- Asks for the PostgreSQL connection on first run, tests it, and can create
  the database when the user exists but the database does not. The password
  is stored with the OS keyring (DPAPI, Keychain, or libsecret); the worker
  gets it through its environment and a `.env` file only your account can read.
- Restart, log and database-connection actions in the Worker menu and the tray.
- Frameless window with the OS window controls overlaid. The web UI's top
  strip is the drag handle and keeps clear of the buttons through the Window
  Controls Overlay API; see the `html.desktop` rules in `frontend/src/app.css`
  and `frontend/src/lib/services/desktop.ts`.
- One unified strip inside the app: brand, search and bell on the left;
  sidebar toggle, breadcrumb, the current page's actions and the account badge
  on the right. Feature pages lend their header actions to the strip through
  `$lib/stores/page-header.svelte`.
- Native menu with the usual accelerators, native notifications, window
  placement that survives restarts, and auto-update through electron-updater,
  fed by the GitHub release that matches the server's `v*` tag.

Other workers (a homelab box, a remote managed worker) are managed inside the
web UI under Devices, as on the web and on Android. The app itself only ever
runs the local one.

## What the installer carries

`scripts/build-worker.mjs` assembles `desktop/worker/`, which electron-builder
ships under `resources/worker`:

| Path | Contents |
| --- | --- |
| `python/` | CPython 3.12 from python-build-standalone, with `requirements.txt`, `requirements-native.txt` and the `fluid` harness installed into its site-packages (that includes the bundled Claude CLI) |
| `node/` | Node.js 22 for the AI tools and the browser MCP server |
| `server/app/` | the backend, with the built frontend in `app/static` |
| `server/tools/`, `server/browser-mcp/` | the two Node packages, built, with production dependencies |
| `manifest.json` | versions and build time |

PostgreSQL is not bundled. The runtime is built on a runner of the target
architecture: Windows x64, macOS arm64 and x64, Linux x64. Installers are
large (several hundred MB); updates download only the changed blocks.

Optional tools the worker looks for on PATH, as on any native install: the
GitHub CLI for pull-request features, and a Chrome, Chromium or Edge for
browser automation.

## The bridge

The preload exposes `window.shuffleDesktop` to the worker page: window and
theme control, native notifications, menu commands, update state, and the
worker's state with restart/log/database actions. The bundled setup and
status page additionally gets `window.shuffleDesktopWorker` for the database
form and the raw worker log; the worker page never sees the credentials.

## Develop

```sh
cd desktop
npm ci
npm start
```

`npm start` compiles `src/` to `dist/` and launches Electron. A development
run uses the repository as the worker runtime: its `venv/` and a built
`app/static` (run `python run_local.py --setup` once), with Node from PATH.

| Variable | Purpose |
| --- | --- |
| `SHUFFLE_DESKTOP_USER_DATA` | Use an isolated profile directory instead of the real one. |
| `SHUFFLE_DESKTOP_REPO_DIR` | Borrow another checkout's venv and frontend build (worktrees). |
| `SHUFFLE_DESKTOP_WORKER_DIR` | Use an assembled runtime (`npm run build:worker`) instead of the repository. |
| `SHUFFLE_DESKTOP_DEV_URL` | Show a frontend dev server instead of starting the worker. |
| `SHUFFLE_DESKTOP_SHOT` | Write a PNG of the window after load and quit (used by CI). |
| `SHUFFLE_DESKTOP_SHOT_DELAY` | Milliseconds to wait before the screenshot (default 4000). |
| `SHUFFLE_DESKTOP_SHOT_JS` | JavaScript to run in the page before the screenshot. |
| `SHUFFLE_DESKTOP_NO_UPDATES` | Skip update checks in a packaged build. |

`DATA_DIR` and `WORKSPACE_DIR` in the environment pass through to the worker,
which is handy for a throwaway data directory while testing.

Tests: `npm test` (settings store, runtime resolution, worker environment).
`npm run build:worker` assembles the runtime for this machine (downloads
Python and Node, installs the Python dependencies; takes a few minutes).
`npm run pack` builds an unpacked app in `release/`; `npm run dist` builds the
installers. Pull-request CI packages a stub runtime on every platform and
assembles a real one on Linux.

## Release

`.github/workflows/release.yml` builds the installers on all four targets
after the server packages and uploads them, with the `latest*.yml` update
metadata, to the same GitHub release. The desktop version is stamped from the
tag, so the app carries the same version number as the server it ships.

Code signing is not configured yet. Windows and Linux builds install and
update unsigned (Windows shows a SmartScreen prompt on first install). macOS
requires a Developer ID certificate and notarisation before the built-in
updater can replace the app, and until then Gatekeeper may also quarantine the
bundled Python: after copying the app to Applications run
`xattr -dr com.apple.quarantine "/Applications/AI Shuffle.app"` once.
