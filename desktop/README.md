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
Downloading the installer needs no account. In-app updates need an active AI
Shuffle license while the hub's license gate is on: the app sends its worker's
license lease, or on a first install asks once for the license key
(`src/main/source-updates.ts`).

Stable milestones include an NSIS installer, built by `desktop-installer.yml`.
Between installers, the installed app downloads and builds signed release code
through its in-app update controls. No new installer build is required.
Build processes run with hidden consoles and show progress inside the app.
The installed EXE remains the shortcut and restart target. It selects the updated
code and matching runtimes while preserving the original Electron profile,
browser session, settings and PostgreSQL directory. Source versions and caches
live in the existing desktop profile's `updates` subdirectory.

Installed apps follow the **stable** channel: tagged releases signed by
`release.yml` after the release image passed verification
(docs/ci/release-channels.md). The updater accepts a manifest only when its
signed `channel` is the one this install follows, it carries a signed source
`tree` digest, and the signature, size, checksum and sequence checks pass. It
recomputes the tree digest over the extracted files and refuses to build on a
mismatch. For your own testing you can follow **edge** (every `main` revision)
on one install by writing `{"channel": "edge"}` to `update-channel.json` in the
profile's `updates` folder; delete the file to return to stable.

The `desktop-source.yml` job publishes an edge build of `main` when the owner
dispatches it manually: it stores the runtime-only source archive as an immutable asset in the private
`desktop-sources` GitHub release and signs its revision, checksum, size, channel,
version, release notes and tree digest. It uses a standard
GitHub-hosted runner and performs no application or installer build. The existing
account-hub D1 database holds signed metadata and the GitHub asset ID. No R2
subscription is needed. Installer downloads are public; the update feed answers
402 without a license lease or key while the hub's license gate is on.
The existing hub streams only CI-published archives using its `DESKTOP_GITHUB_TOKEN` Worker secret,
scoped to Contents: read on this repository. Clients never receive that GitHub
credential or an upstream signed URL. The publisher uses the `ACCOUNT_HUB_DATABASE_ID` GitHub environment
variable and existing D1 deployment permissions. The signing key is the
`DESKTOP_APPLICATION_SIGNING_KEY` GitHub secret; its public half ships with the launcher.

The app checks for updates in the background without downloading them. To check
now, use **Settings → System → Updates** or the sidebar update button. The app
menu's **Check for Updates…** (Help on Windows and Linux, the app menu on macOS)
opens that Settings section; the tray has no update item.

The desktop revalidates its entry page when opening the worker, and the worker
marks HTML responses as non-cacheable so a new revision does not reuse an old UI.
**View → Force Reload** (`Ctrl+Shift+R` on Windows/Linux, `Cmd+Shift+R` on macOS)
reloads the current desktop window without restarting the worker.

When an update is available, a small update button appears next to Search in the sidebar header.
Click it to download and build the new revision alongside the running one; the
button shows progress while dependencies are prepared. Dependencies are cached by
their manifests. Once ready, click the same button again: **Update and restart**
stops the worker and database, then switches to the new application and its matching
native runtimes. Startup readiness confirms activation. Failed downloads/builds do
not replace the running version. A failed first boot retains the previous files
but does not automatically run old code over a potentially migrated database.
PostgreSQL major-version upgrades still require a deliberate data migration; never
change the pinned major version without adding and testing that migration.

The repository stays private; the hub serves each published desktop source ZIP
(behind the license gate when it is on). `source-auth.ts` carries no GitHub credential;
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
does not take foreground focus, and does not register a tray icon.

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
  process adds it as `X-Shuffle-Desktop-Listener` to requests to the worker's
  exact origin, WebSocket handshakes included, when they are top-level page
  loads or come from a document under a worker-origin top frame (so an OAuth,
  hub or Plaid pop-up cannot use it); a service worker's own requests, which
  have no frame, carry it only when their referrer is the worker origin. It is
  stripped from every other request, and no page can read it. The app never
  keeps a service worker: its partition's are cleared at startup and whenever
  a pop-up closes, and a sized pop-up opened straight at another site gets a
  window in a separate in-memory session (`src/main/navigation.ts`). The worker uses it to tell this app from any other local process
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
  hands the connection to the worker. The password is stored in
  `<userData>/worker-settings.json`, encrypted with the OS keyring (DPAPI,
  Keychain, or libsecret) when one is available. Before startup the supervisor
  writes the private `managed-postgres.json` descriptor into the worker's data
  directory (see below); no database credentials
  are handed to the app through environment variables or `.env`. The transient
  descriptor is removed on shutdown; retain the private profile for persistence.
- PostgreSQL is managed-only. There is no connection form, external-server mode,
  or database environment configuration. Worker → Worker Status… shows lifecycle
  and failure details; Show PostgreSQL Log opens the database log.
- Restart, status and log actions in the Worker menu; restart, status,
  Open in Browser and Quit in the tray. The tray's Quit first stops sessions
  like any other quit; if the worker does not confirm within the grace period
  it is ended anyway, so a stuck worker cannot keep the app open. Closing the
  window instead offers "Quit Anyway" after a second failed attempt.
- Frameless window with the OS window controls overlaid. The web UI's top
  strip is the drag handle and keeps clear of the buttons through the Window
  Controls Overlay API; see the `html.desktop` rules in `frontend/src/app.css`
  and `frontend/src/lib/services/desktop.ts`.
- One unified strip inside the app: brand, search and bell on the left;
  sidebar toggle, breadcrumb, the current page's actions and the account badge
  on the right. Feature pages lend their header actions to the strip through
  `$lib/stores/page-header.svelte`.
- Native menu with the usual accelerators, native notifications, and window
  placement that survives restarts. Windows installs take signed source
  updates from the **stable** channel (tagged releases); macOS and Linux check
  the hub's signed installer list and offer the newer installer.
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
| `python/` | CPython 3.12 from python-build-standalone, with `requirements.txt`, `requirements-native.txt` and the `fluid` harness installed into its site-packages. No Claude CLI is bundled: agents use the user's own `claude` install |
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
(several hundred MB).

The database lives in `<userData>/postgres/data` (for example
`%APPDATA%\AI Shuffle\postgres\data` on Windows,
`~/Library/Application Support/AI Shuffle/postgres/data` on macOS,
`~/.config/AI Shuffle/postgres/data` on Linux) and the server log next to the
worker's under `<userData>/logs/postgres.log`. The worker log is
`<userData>/logs/worker.log`. It moves to `worker.prev.log` at each start and
whenever it passes 20 MB during a run, so `worker.prev.log` holds either the
previous run's log or, after a mid-run move, this run's earlier output (the
previous run's log is then gone). Windows source-update builds log to
`<userData>/updates/build.log`.

The worker's own data directory (`DATA_DIR`) holds its app data, the
credential keys the database contents depend on, and the transient
`managed-postgres.json`: `%USERPROFILE%\.ai-shuffle` on Windows,
`~/Library/Application Support/ai-shuffle` on macOS and
`~/.local/share/ai-shuffle` on Linux. To back up AI Shuffle, close the app and
back up both that directory and `<userData>/postgres/data`; keep them together.
The worker's `.env` carries no database credentials (see above), so it cannot
be used for a manual `pg_dump`. Uninstalling the app leaves both in place.

Optional tools the worker looks for on PATH, as on any native install: the
GitHub CLI for pull-request features, and a Chrome, Chromium or Edge for
browser automation.

## The bridge

The preload exposes `window.shuffleDesktop` to the worker page: window state
and title-bar colours, menu commands, the agent-access disclosure, and update
state and install. The web UI's side, and the members it uses, are in
`frontend/src/lib/services/desktop.ts`; `window.close`, `worker.getState` and
`updates.check` remain only for the CI verification scripts
(`scripts/verify-*.mjs`). The bundled status page additionally gets
`window.shuffleDesktopWorker` for the database lifecycle, the worker log and
restart. The main process answers IPC only from the main window's main frame
while it shows the bundled status page (that exact file) or the worker's own
origin (`src/main/ipc.ts`); redirects and links that would leave those two go
to the system browser (`src/main/navigation.ts`). Only the worker origin gets
browser permissions: microphone, clipboard write and fullscreen. Neither page
exposes credentials, the listener token or connection forms. The app registers
no URL scheme (`aishuffle://` was dropped: nothing produced or read such links).

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
installers. Pull-request CI (`desktop.yml`) runs the typecheck and unit tests;
with the `ci:full` label it also packages a Windows shell around a stub runtime
and installs and exercises a Windows preview with a real runtime. Assembling
the Linux runtime is a manual `workflow_dispatch` opt-in.

## Release

See [RELEASING.md](RELEASING.md): which workflow builds which installer,
Windows Authenticode, macOS Developer ID signing and notarization, unsigned
Linux packages with checksums, every GitHub secret involved, and how update
checks work. Installed Windows apps update in place from signed stable-channel
source (above); macOS and Linux installers are rebuilt and downloaded again.
