# AI Shuffle Desktop

The desktop app runs an AI Shuffle worker on this computer and shows its web
UI in a native window, for Windows x64, Apple Silicon macOS and Linux x64. It
is the desktop counterpart of the Android app: the same UI, served by the
worker the app starts, so the two never drift apart.

## Windows distribution: no installer

Download the Windows source ZIP at <https://hobbycoders.com/desktop>, unzip it,
and double-click `Start AI Shuffle.cmd`. No account is needed.
The launcher adds a Start menu shortcut, downloads
Python, Node, PostgreSQL and Electron, and builds the frontend, tools and desktop
JavaScript locally. The first launch can take several minutes and needs internet
access. Runtime files and dependency caches live under
`%LOCALAPPDATA%/AI Shuffle Source`; existing user data stays in its current locations.

Updates follow `main`, not tagged releases. The small `desktop-source.yml` job
stores GitHub's source ZIP as an immutable asset in the private `desktop-sources`
GitHub release and signs its revision, checksum and size. It uses a standard
GitHub-hosted runner and performs no application or installer build. The existing
account-hub D1 database holds signed metadata and the GitHub asset ID. No R2
subscription is needed. Downloads and updates are public and require no account.
The existing hub streams only CI-published archives using its `DESKTOP_GITHUB_TOKEN` Worker secret,
scoped to Contents: read on this repository. Clients never receive that GitHub
credential or an upstream signed URL. The publisher uses the `ACCOUNT_HUB_DATABASE_ID` GitHub environment
variable and existing D1 deployment permissions. The signing key is the
`DESKTOP_APPLICATION_SIGNING_KEY` GitHub secret; its public half ships with the launcher.

The app checks for updates in the background without downloading them. When one
is available, a small update button appears next to Search in the sidebar header.
Click it to download and build the new revision alongside the running one; the
button shows progress while dependencies are prepared. Dependencies are cached by
their manifests. Once ready, click the same button again: **Update and restart**
stops the worker and database, then switches to the new application and its matching
native runtimes. Startup readiness confirms activation. Failed downloads/builds do
not replace the running version. A failed first boot retains the previous files
but does not automatically run old code over a potentially migrated database.
PostgreSQL major-version upgrades still require a deliberate data migration; never
change the pinned major version without adding and testing that migration.

The repository stays private, but each published desktop source ZIP is available
to anyone through the hub. `source-auth.ts` is now a credential-free transport;
signature verification and local building remain independent of hosting.

The packaging commands below remain for legacy/manual development builds; normal
Windows distribution no longer builds or publishes NSIS installers. Existing
installer-based versions need to move to this launcher once, because those older
binaries cannot discover the new source-update protocol themselves.

## Testing without stopping the normal desktop

The source launcher supports `--isolated --root <separate-folder> --port 18080
--protected-port 18081`. The profile stores its port choices and keeps its own
settings, browser session, provider credentials, workspace and PostgreSQL data.
Later launches use `--root <same-folder>`; Update and restart preserves the profile.
Isolated roots cannot overlap the normal application or its data, and neither
listener can claim ports 8000/8001. The window is labelled **AI Shuffle Isolated**,
does not take foreground focus, and does not register a tray icon or replace the
normal URL handler.

For Windows acceptance testing, install the desktop and browser-mcp Node
dependencies, build the desktop TypeScript, then run
`node desktop/scripts/verify-source-desktop.mjs <new-temporary-root>`.
The test creates a signed local source feed, launches the actual Electron app,
drives its real preload/IPC controls, and reconnects after its real Node/Electron
restart. No worker source patches or replacement UI bridge are used. It checks
that the original application's listener PIDs stay unchanged. The optional
`--feed-directory` and `--public-key` switches are restricted to explicit isolated
profiles; they never change the normal update feed. A local feed does not test
hosted public downloads or GitHub download permissions.

## What the app does

- Starts its bundled PostgreSQL, then the worker (`python -m app.main` in
  local mode, listening on 127.0.0.1:8000), and opens the worker's UI once it
  answers. Stable builds may attach to an already running local worker; preview
  builds refuse occupied ports and never attach. Exit the old app before opening
  Preview. Quitting stops the worker and database that this app started.
- Needs no database setup. The first run creates a cluster in
  `<userData>/postgres/data` (`initdb`, user `ai_shuffle`, a generated
  password, `scram-sha-256`); every start picks a free loopback port and
  hands the connection to the worker. The password is stored with the OS
  keyring (DPAPI, Keychain, or libsecret). Before startup the supervisor writes
  the private `data/managed-postgres.json` descriptor; no database credentials
  are handed to the app through environment variables or `.env`. The transient
  descriptor is removed on shutdown; retain the private profile for persistence.
- PostgreSQL is managed-only. There is no connection form, external-server mode,
  or database environment configuration. Worker → Worker Status… shows lifecycle
  and failure details; Show PostgreSQL Log opens the database log.
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
- Settings → Downloads shows the installed desktop version and update status,
  with Check for updates and Restart and update actions. Updates download in
  the background; preview and development builds show that updates are disabled.

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
| `server/downloads/cloudflared/<version>/` | pinned cloudflared for optional remote access, downloaded and checksum-verified during assembly |
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
worker's state with restart/log/status actions. The bundled
status page additionally gets `window.shuffleDesktopWorker` for the database
lifecycle and worker log; neither page exposes credentials or connection forms.

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
EnterpriseDB install). This development-only binary override is ignored by
packaged apps; it never selects an external database server.

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
Python, Node, PostgreSQL and cloudflared, installs the Python dependencies; takes a few
minutes). `--skip-postgres` leaves PostgreSQL out of a build.
Cloudflared assembly always verifies a fresh package-local download using the
pins in `app/core/cloudflared.py`, runs that binary's version check, and records
its path and hashes in the runtime manifest. A system client cannot substitute
for the bundled file; missing or invalid downloads fail the build.
Both installer configurations also verify the copied cloudflared binary in
their final resources directory before producing an installer.
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

### Test desktop updates

Use a packaged stable app on Windows with a lower version than the target
release. To test the Settings controls, the starting build must already include
them; an older installed UI cannot acquire the controls before updating.

1. Publish the higher stable GitHub release with its Windows installer and
   `latest.yml` metadata. Drafts and prereleases are not the stable update feed.
2. In the older app, open Settings → Downloads and choose Check for updates.
   Confirm that the new version and download progress appear.
3. Choose Restart and update once the download is ready. Confirm that the app
   reopens, the installed version changes, and existing data is still available.
4. Check again and confirm that the UI says it is up to date.

For an older starting build without the Settings controls, the automatic check
runs about 15 seconds after launch, and the notification bell offers Restart
and update when the download finishes. Preview builds and development runs
cannot exercise the release update path.

Code signing is not configured yet. Windows and Linux builds install and
update unsigned (Windows shows a SmartScreen prompt on first install). macOS
requires a Developer ID certificate and notarisation before the built-in
updater can replace the app, and until then Gatekeeper may also quarantine the
bundled Python: after copying the app to Applications run
`xattr -dr com.apple.quarantine "/Applications/AI Shuffle.app"` once.
