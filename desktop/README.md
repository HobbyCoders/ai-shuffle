# AI Shuffle Desktop

The desktop app runs an AI Shuffle worker on this computer and shows its web
UI in a native window, for Windows x64, Apple Silicon macOS and Linux x64. It
is the desktop counterpart of the Android app: the same UI, served by the
worker the app starts, so the two never drift apart.

## What the app does

- Starts its bundled PostgreSQL, then the worker (`python -m app.main` in
  local mode, listening on 127.0.0.1:8000), and opens the worker's UI once it
  answers. If a worker is already running on that port, the app attaches to
  it instead of starting a second one. Quitting the app stops the worker and
  the database it started.
- Needs no database setup. The first run creates a cluster in
  `<userData>/postgres/data` (`initdb`, user `ai_shuffle`, a generated
  password, `scram-sha-256`); every start picks a free loopback port and
  hands the connection to the worker. The password is stored with the OS
  keyring (DPAPI, Keychain, or libsecret); the worker gets it through its
  environment and a `.env` file only your account can read. If the stored
  password is ever lost, the next start sets a new one through PostgreSQL's
  single-user mode, so the data stays reachable.
- Advanced: Worker → Advanced → Use external PostgreSQL… points the worker at
  a server you run instead (tests the connection, can create the database
  when the user exists but the database does not) and leaves the bundled
  server stopped. The same page has a button back to the bundled one.
- Restart and log actions in the Worker menu and the tray.
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
  fed by the GitHub release for the app's `v*` tag.

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

The same script assembles `desktop/postgres/`, shipped under
`resources/postgres`: PostgreSQL 16 (`bin/` with initdb, pg_ctl, postgres and
the client tools, plus `lib/` and `share/`) from a
[theseus-rs/postgresql-binaries](https://github.com/theseus-rs/postgresql-binaries)
release, pinned in `scripts/postgres-binaries.mjs` and verified against the
release's `.sha256` sidecar. The worker manifest records it under
`postgres: {version, target}`. The runtime is built on a runner of the target
architecture: Windows x64, macOS arm64, Linux x64. Installers are large
(several hundred MB); updates download only the changed blocks.

The database lives in `<userData>/postgres/data` (for example
`%APPDATA%\AI Shuffle\postgres\data` on Windows,
`~/Library/Application Support/AI Shuffle/postgres/data` on macOS,
`~/.config/AI Shuffle/postgres/data` on Linux) and the server log next to the
worker's under `<userData>/logs/postgres.log`. To back up AI Shuffle, back up
that data directory with the app closed (or use `pg_dump` from
`resources/postgres/bin` against the connection in `<userData>/worker/.env`).
Uninstalling the app leaves the data directory in place.

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
It has no bundled PostgreSQL of its own: point `SHUFFLE_DESKTOP_POSTGRES_DIR`
at any PostgreSQL 16 installation (a directory with `bin/initdb`, `bin/pg_ctl`
and `bin/postgres`, such as an assembled `desktop/postgres/` or a Homebrew or
EnterpriseDB install), or choose an external server under Worker → Advanced.

| Variable | Purpose |
| --- | --- |
| `SHUFFLE_DESKTOP_USER_DATA` | Use an isolated profile directory instead of the real one. |
| `SHUFFLE_DESKTOP_REPO_DIR` | Borrow another checkout's venv and frontend build (worktrees). |
| `SHUFFLE_DESKTOP_WORKER_DIR` | Use an assembled runtime (`npm run build:worker`) instead of the repository. |
| `SHUFFLE_DESKTOP_POSTGRES_DIR` | PostgreSQL binaries for the bundled server in a development run (a directory with `bin/initdb`). |
| `SHUFFLE_DESKTOP_DEV_URL` | Show a frontend dev server instead of starting the worker. |
| `SHUFFLE_DESKTOP_SHOT` | Write a PNG of the window after load and quit (used by CI). |
| `SHUFFLE_DESKTOP_SHOT_DELAY` | Milliseconds to wait before the screenshot (default 4000). |
| `SHUFFLE_DESKTOP_SHOT_JS` | JavaScript to run in the page before the screenshot. |
| `SHUFFLE_DESKTOP_NO_UPDATES` | Skip update checks in a packaged build. |

`DATA_DIR` and `WORKSPACE_DIR` in the environment pass through to the worker,
which is handy for a throwaway data directory while testing.

Tests: `npm test` (settings store, runtime resolution, worker environment,
the PostgreSQL supervisor against a fake runtime, the download helpers).
`npm run build:worker` assembles the runtime for this machine (downloads
Python, Node and PostgreSQL, installs the Python dependencies; takes a few
minutes). `--skip-postgres` leaves PostgreSQL out of a build.
`npm run pack` builds an unpacked app in `release/`; `npm run dist` builds the
installers. Pull-request CI packages a stub runtime on every platform and
assembles a real one on Linux.

## Release

`.github/workflows/release.yml` creates the GitHub release, then assembles the
runtime and builds the installer on each supported target and uploads them,
with the `latest*.yml` update metadata, to that release. The desktop version
is stamped from the tag, so the app carries the same version number as the
server it ships. Supported downloads are the Windows x64 EXE, the macOS arm64
DMG (plus an arm64 ZIP for updates), and the Linux x64 AppImage or amd64
Debian package. The workflow no longer publishes standalone server archives;
Docker remains the headless option.

Older releases may retain Windows ARM update files so the shared Windows
update feed does not hand an ARM client an x64 installer. Windows ARM is no
longer built or listed as a supported download.

Code signing is not configured yet. Windows and Linux builds install and
update unsigned (Windows shows a SmartScreen prompt on first install). macOS
requires a Developer ID certificate and notarisation before the built-in
updater can replace the app, and until then Gatekeeper may also quarantine the
bundled Python: after copying the app to Applications run
`xattr -dr com.apple.quarantine "/Applications/AI Shuffle.app"` once.
