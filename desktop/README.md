# AI Shuffle Desktop

The desktop app runs an AI Shuffle worker on this computer and shows its web
UI in a native window, for Windows x64, Apple Silicon macOS and Linux x64. It
is the desktop counterpart of the Android app: the same UI, served by the
worker the app starts, so the two never drift apart.

## Windows installation and updates

Download the Windows installer at <https://hobbycoders.com/desktop> and run it.
For an existing installation, keep the same installation location. Continue using
your normal AI Shuffle shortcut. The installer includes a ready-to-run app,
Python, Node and PostgreSQL; startup does not need to build or download code.
No account is needed for downloads or updates.

Stable milestones include an NSIS installer, built by `desktop-installer.yml`.
Between releases, the installed app downloads and builds signed code from `main`
through its in-app update controls. No new tag or installer build is required.
Build processes run with hidden consoles and show progress inside the app.
The installed EXE remains the shortcut and restart target. It selects the updated
code and matching runtimes while preserving the original Electron profile,
browser session, settings and PostgreSQL directory. Source versions and caches
live in the existing desktop profile's `updates` subdirectory.

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

Existing installer-based versions need this installer upgrade once to gain the
source update protocol. They do not need a separate source ZIP or console launcher.
The old `Start AI Shuffle.cmd` entry remains a development/testing tool.

## Testing without stopping the normal desktop

`verify-installed-desktop.mjs` runs on fresh Windows CI only. It installs the real
NSIS package, completes onboarding, reinstalls over the same profile, downloads
and builds the real signed main archive through IPC, restarts through the installed
EXE, and checks that setup and data survive. Only the disposable installed copy's
baseline revision is changed to simulate an older release; signatures, the feed,
builds, application code and restart behavior are real. The shipped installer is
never modified by this test.

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
  answers. Every spawn gets a random attach token (`SHUFFLE_DESKTOP_ATTACH_TOKEN`)
  whose SHA-256 the worker reports in `/health`; the token and pid are kept in
  the private `<userData>/worker/worker-process.json`. A worker already
  answering on the port is adopted only when that hash matches, so another
  user's worker, a Docker container or a manual run is reported as "in use by
  another AI Shuffle worker that this app did not start" and no second worker
  is spawned. An orphan of our own from a crashed run is stopped by pid and
  replaced. Every spawn also gets a fresh listener token
  (`SHUFFLE_DESKTOP_LISTENER_TOKEN`, see `src/main/listener.ts`). The main
  process adds it as `X-Shuffle-Desktop-Listener` to every request the window
  makes to the worker's exact origin, WebSocket handshakes included, and strips
  it from every other request; the worker's top-level page can also read it
  over IPC. The worker uses it to tell this app from any other local process
  that reaches its loopback port.
  Set `SHUFFLE_DESKTOP_ATTACH_ANY=1` to attach to any local worker
  (developers running the worker by hand). Preview builds refuse occupied ports
  and never attach. Exit the old app before opening Preview. Quitting stops the
  worker and database that this app started. A worker that crashes after it was
  ready is restarted, at most twice in five minutes, before the status page
  reports it.
- Says plainly that agents run as you. Before the first worker ever starts,
  a dialog explains that agents run on this computer as the signed-in user,
  with the user's own `claude` login and every file that user can reach
  (`src/main/disclosure.ts`). Quit leaves nothing running and asks again next
  launch; the acknowledgement lives in `<userData>/agent-access.json`, and
  raising `AGENT_ACCESS_VERSION` asks again after an upgrade. The same text
  stays under Help > Agent Access… and in the web UI's Settings > General.
  Automated acceptance runs set `SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS=1`.
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
  placement that survives restarts, and signed source updates from `main`.
- The sidebar update button checks, downloads and prepares updates, then offers
  Update and restart. Preview builds keep their updates disabled.

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

On Windows, set `SHUFFLE_DESKTOP_WINDOW_TEST=1` and run
`node --test test/window-launch.test.mjs` after building to verify splash
visibility and focus across repeated launches with both current and older
launchers using a temporary profile. This test opens real foreground windows
without starting a worker or database.

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
