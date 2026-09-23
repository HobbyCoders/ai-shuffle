# AI Shuffle - Local Installation Guide

Run AI Shuffle directly on your PC without Docker! Perfect for game development, local file access, and working with native applications.

## Quick Start

Install and open the [desktop app](desktop/README.md). It bundles PostgreSQL and
supervises the worker, including startup and shutdown. A fresh installation starts
with a clean database: no host, port, password or database import is required.

For a test installer, use its separate test data directory and close the running
stable app first. Do not attach to an existing worker, import production data, or
copy production settings/credentials into the test installation. Keep the existing
installation and protected backups unchanged.

## Source-build Prerequisites

- **Python 3.12** — [Download](https://www.python.org/downloads/). The
  vendored `fluid` package (`pip install -e ./fluid`) requires 3.12, so the
  backend test suite (`pytest tests`) needs a 3.12 virtual environment; a
  newer interpreter cannot install it.
- **Node.js 22.12+** — [Download](https://nodejs.org/) (for building frontend)
- **GitHub CLI** — [Download](https://cli.github.com/) (used for PR reviews, issue management, etc.)
- **Claude Code CLI** — install it yourself with Anthropic's installer, then
  run `claude` once to sign in. Claude sessions run the `claude` on your PATH;
  AI Shuffle does not bundle, pin or install a copy for native runs.
  - macOS/Linux/WSL: `curl -fsSL https://claude.ai/install.sh | bash`
  - Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`

  The desktop app uses the same PATH copy and tells you at startup when it is
  missing. The Docker image installs it on first start with the same installer.
- **Managed runtime** — Desktop or Docker supplies the database; an external
  PostgreSQL server is not supported.

`python run_local.py --setup` (or `run_local.bat --setup` / `./run_local.sh --setup`)
installs dependencies and builds source assets only. It does not prompt for a
database connection or create a standalone database. Source worker startup needs
the managed supervisor's private runtime descriptor.

## Manual Setup

If you prefer to set things up manually:

### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/ai-shuffle.git
cd ai-shuffle
```

### 2. Create Virtual Environment
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt ./fluid
# Optional: computer-use (host desktop control), native runs only
pip install -r requirements-native.txt
```

### 4. Use the Managed Database

The desktop/Docker supervisor creates and retains the database privately. It
writes `DATA_DIR/managed-postgres.json` before starting the worker. Do not edit,
print or copy this descriptor into `.env`; it is sensitive runtime state, not a
user-facing configuration file. There is no database connection setup step.

### 5. Build Frontend
```bash
cd frontend
npm install
npm run build
cd ..

# Copy build to static directory
# Windows:
rm -rf app/static/_app && cp -r frontend/build/_app app/static/_app && cp frontend/build/index.html app/static/index.html

# macOS/Linux:
rm -rf app/static/_app && cp -r frontend/build/* app/static/
```

### 6. Start Through the Managed Supervisor

Open the desktop app, or use `docker compose up -d --build` for a source-built
managed container. Do not start `app.main` or Uvicorn directly: source workers
require the supervisor's validated database descriptor. Standalone startup with
no managed database refuses instead of connecting to another server.

On first launch, choose your workspace and connect development tools or model
providers. A local username, password or Shuffle account is not required.

## Configuration

### Remote access

Native setup and startup install a checksum-verified Cloudflare tunnel client
under `downloads/cloudflared/` in the Shuffle folder. A client already on PATH
is reused. No service or tunnel is started until you link the worker in Devices.
If the download fails, local access still works. Retry the install with
`python -m app.core.cloudflared` from the Shuffle folder.

Each worker signs in to and out of its own Shuffle account connection and has its
own private managed database. Sign in with the same account on your phone to see
all of those workers. LAN device authorization is separate from account sign-in.
Signing out of a worker stops its tunnel and removes its account registration;
other workers and local network connections remain available.

### Credential storage locked after an upgrade

If you are recovering an existing database into a separate managed installation,
its encrypted records need the matching credential key. Export a portable
recovery package from the working source install, stop the recovery app, then import it
with `venv\Scripts\python.exe -m scripts.credential_keys import <recovery-file>`.
The commands prompt privately for the recovery passphrase. Restart Shuffle
after `venv\Scripts\python.exe -m scripts.credential_keys status` reports ready.
See [credential key recovery](docs/credential-key-recovery.md) for the export
steps. Changing the old admin password or signing into an account does not
restore a missing key.

### Environment Variables

Create a `.env` file in the project root (copy from `.env.example`):

```env
# Enable local mode
LOCAL_MODE=true

# Server settings
HOST=127.0.0.1
PORT=8000

# Disable secure cookies for HTTP
COOKIE_SECURE=false

# Database credentials are private runtime state, not environment options.
```

### Data Locations

In local mode, AI Shuffle stores data in platform-specific locations:

| Platform | Data Directory | Workspace |
|----------|---------------|-----------|
| Windows | `%APPDATA%\ai-shuffle` | `Documents\ai-shuffle-workspace` |
| macOS | `~/Library/Application Support/ai-shuffle` | `~/ai-shuffle-workspace` |
| Linux | `~/.local/share/ai-shuffle` | `~/ai-shuffle-workspace` |

Override with environment variables:
```env
DATA_DIR=/path/to/custom/data
WORKSPACE_DIR=/path/to/custom/workspace
```

### Browser automation (native installs)

AI Shuffle's built-in browser integration is the bundled **`browser` MCP server**
(TypeScript package `browser-mcp/`). When enabled, each agent session spawns
`node browser-mcp/dist/index.js`, which exposes `mcp__browser__*` tools
(`browser_navigate`, `browser_snapshot`, `browser_act`, `browser_screenshot`) and
attaches over CDP to a **local headless Chrome/Edge** — no Claude account, no
browser extension, and no native-messaging host.

On native installs there are two moving parts, both handled for you:

1. **The tool server is built once.** `run_local.py` compiles `browser-mcp/`
   (`npm ci --ignore-scripts && npm run build` → `dist/index.js`) during setup,
   skipping the rebuild when the output is already up to date. To build it by
   hand: `cd browser-mcp && npm ci --ignore-scripts && npm run build`.
2. **The browser is auto-launched.** When the `browser` built-in is enabled, the
   backend launches and supervises a headless Chrome/Edge on a loopback
   DevTools/CDP port (default 9221, localhost only — never published), restarting
   it if it exits. The tool server attaches to that same port.

The integration is on by default. Toggle it off via the `browser` entry in the
MCP admin UI (**Settings → Browser automation**).

**Environment knobs** (see `.env.example`):

| Variable | Purpose |
|----------|---------|
| `CHROME_CDP_PORT` | Loopback DevTools port (default `9221`). |
| `CHROME_EXECUTABLE` | Explicit Chrome/Edge/Chromium path. Auto-detected from the standard Chrome/Edge install locations when unset. |
| `BROWSER_MCP_ENTRY` | Override the tool-server entry path (defaults to `<repo>/browser-mcp/dist/index.js` on native checkouts). |
| `BROWSER_SSRF_ALLOW_LOOPBACK` | Whether the agent's browser may open `localhost` URLs. **On a local install this is on by default**, so an agent can view a dev server it just started. Set `false` to refuse loopback. |
| `BROWSER_SSRF_ALLOW` / `BROWSER_SSRF_BLOCK` | Comma-separated CIDRs to allow / refuse outright. An explicit `BROWSER_SSRF_ALLOW` replaces the loopback default above; `BROWSER_SSRF_BLOCK` wins over both. Cloud metadata (`169.254.0.0/16`) is always refused and cannot be re-enabled. |

**Troubleshooting.** The **Settings → Browser automation** card shows a health
dot and, when the browser is unreachable, a **detail line** explaining why —
e.g. `browser-mcp not built: … missing` (run the build in step 1),
`no Chrome/Edge executable found; set CHROME_EXECUTABLE` (install Chrome/Edge or
point `CHROME_EXECUTABLE` at it), `external browser on port 9221` (something else
already owns the port), or `Chrome crash-looped …` (check the app logs).

> **Docker:** the container image bundles Chromium and supervises it itself
> (`docker/chrome-supervisor.sh`); the native supervisor
> described above is a no-op inside the container.

> **Browser network policy:** HTTP request interception refuses cloud metadata,
> allows private LAN hosts, and permits loopback by default on native installs.
> This is not a network sandbox: WebSocket upgrades and DNS resolution races
> are not fully covered. Use host/network isolation when handling untrusted prompts.


## Differences from Docker Mode

| Feature | Docker | Local |
|---------|--------|-------|
| File access | Container only | Full PC access |
| Installation | Single command | Packaged desktop app |
| Updates | Rebuild image | Update package; rebuild source assets for development |
| Port binding | `127.0.0.1:8000` by default; `BIND_HOST`/`PORT` in `.env` (Docker-published ports bypass host firewall rules) | Loopback only, default 8000 |
| Data isolation | Volume mounts | User directories |

## Troubleshooting

### "Frontend not built"
```bash
cd frontend
npm install
npm run build
cd ..
rm -rf app/static/_app && cp -r frontend/build/* app/static/
```

### "Permission denied" on macOS/Linux
```bash
chmod +x run_local.sh
```

### "Port already in use"

Close the other AI Shuffle app normally before starting the managed desktop
runtime or a test installer. Do not force-kill a database process or attach the
test app to a production worker. The supervisor needs time to stop PostgreSQL
cleanly; preserve existing data and backups.

The desktop app adopts a worker already answering on its port only when that
worker proves it was started by this app (its `/health` reports the hash of
the attach token the app passed in). A worker started by another user, by
Docker, or by hand is reported as "in use by another AI Shuffle worker that
this app did not start" and nothing is spawned. Developers who run
`python -m app.main` themselves can set `SHUFFLE_DESKTOP_ATTACH_ANY=1` before
starting the desktop app to attach to it anyway.

### Database Issues

The managed database lives in the runtime's data directory, not a customer-supplied
connection. Restart through the desktop app or Docker supervisor and inspect its
startup diagnostics. Do not create a replacement database, reset a password,
rewrite the descriptor or delete lifecycle files to bypass a startup failure.
Preserve the original data and follow [backup and recovery](docs/RESTORE.md).
A clean test install must use a separate empty data directory, never production.

## Development Mode

Use the desktop or Docker supervisor for backend startup/restarts. A direct
Uvicorn process does not manage the database lifecycle or the application's
listener boundary. Build source dependencies with `run_local.py --setup` and
refresh them with `--update`; neither command creates a standalone database.

For frontend development:
```bash
cd frontend
npm run dev
```

## Updating

```bash
# Pull latest changes
git pull

# Update dependencies
pip install -r requirements.txt ./fluid

# Rebuild frontend
cd frontend
npm install
npm run build
cd ..
rm -rf app/static/_app && cp -r frontend/build/* app/static/
```

## Worker Network

A native install is one worker in a worker network. On first run it offers to
start a new network, join another worker with a code and fingerprint check, or
sign in to a Shuffle account. Workers in one network share settings, provider
keys, profiles, preferences, notes and the session catalog, all end-to-end
encrypted; each session runs on the worker that owns it. Settings > Devices
manages members, invites, the recovery key and sync conflicts. Every desktop or
Docker worker owns its own bundled managed PostgreSQL; shared database setup is
not supported. See [worker networks](docs/network/README.md).

## Security Notes

- Local mode binds to `127.0.0.1` by default (localhost only)
- To allow LAN access, set `HOST=0.0.0.0` (use with caution)
- `COOKIE_SECURE=false` is required for HTTP (no HTTPS)
- Claude credentials are stored in `~/.claude/.credentials.json`

## Getting Help

- Check prerequisites: `python run_local.py --check`
- View logs: Check terminal output
- Report issues: https://github.com/HobbyCoders/ai-shuffle/issues

### Refreshing a native checkout

After `git pull`, run `python run_local.py --update` (or the platform launcher with
`--update`). This installs the checked-in Fluid harness and locked Node packages,
then rebuilds the frontend, browser MCP and AI tools. It does not reconfigure the
database or restart an already running server. Restart through the managed desktop
runtime after it succeeds.
Native release archives already contain the frontend; replace the archive to update
that frontend, then run `--update` to install/build its runtime tools.
