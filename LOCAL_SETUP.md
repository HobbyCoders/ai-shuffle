# AI Shuffle - Local Installation Guide

Run AI Shuffle directly on your PC without Docker! Perfect for game development, local file access, and working with native applications.

## Quick Start

### Windows
```batch
# First time setup
python run_local.py --setup

# Start the server
python run_local.py
```

Or double-click `run_local.bat`

### macOS / Linux
```bash
# First time setup
./run_local.sh --setup

# Start the server
./run_local.sh
```

## Prerequisites

### Required
- **Python 3.12+** — [Download](https://www.python.org/downloads/)
- **Node.js 22.12+** — [Download](https://nodejs.org/) (for building frontend)
- **PostgreSQL 14+** — [Download](https://www.postgresql.org/download/) (or any reachable Postgres server on your network)
- **GitHub CLI** — [Download](https://cli.github.com/) (used for PR reviews, issue management, etc.)

During `--setup` you'll be asked for the database connection: the host/IP of
your Postgres server, the username, and the password (port and database name
have sensible defaults). The answers are saved to `.env` — the app assembles
the connection URL itself, so you never have to hand-build a `postgresql://`
string.

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
pip install -r requirements.txt ./vendor/fluid
# Optional: computer-use (host desktop control), native runs only
pip install -r requirements-native.txt
```

### 4. Configure the Database
```bash
# Prompts for host/IP, username, and password; saves them to .env
python -c "import run_local; run_local.configure_database()"
```
Or add them to `.env` by hand (see `.env.example`): `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.

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

### 6. Start the Server
```bash
# Set local mode
export LOCAL_MODE=true  # or set LOCAL_MODE=true on Windows

# Start
python -m app.main
```

Visit http://127.0.0.1:8000 in your browser. On first launch, you'll be guided through account setup and Claude authentication.

## Configuration

### Remote access

Native setup and startup install a checksum-verified Cloudflare tunnel client
under `downloads/cloudflared/` in the Shuffle folder. A client already on PATH
is reused. No service or tunnel is started until you link the worker in Devices.
If the download fails, local access still works. Retry the install with
`python -m app.core.cloudflared` from the Shuffle folder.

Each worker signs in to and out of its own Shuffle account connection, including
workers sharing a database. Sign in with the same account on your phone to see
all of those workers. LAN device authorization is separate from account sign-in.
Signing out of a worker stops its tunnel and removes its account registration;
other workers and local network connections remain available.

### Credential storage locked after an upgrade

If another install (for example Unraid) has already initialized the shared
database's credential key, this machine also needs that key. Export a portable
recovery package from the working install, stop this local app, then import it
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

# Database connection (written by the setup prompts; edit here to change)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=ai_shuffle
POSTGRES_PASSWORD=your-strong-password
POSTGRES_DB=ai_shuffle
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
| Installation | Single command | Manual setup |
| Updates | Rebuild image | Git pull + rebuild |
| Port binding | Configurable | Default 8000 |
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
```bash
# Use a different port
python run_local.py --port 3000

# Or kill the process using port 8000
# Windows:
netstat -ano | findstr :8000
taskkill /PID <pid> /F

# macOS/Linux:
lsof -i :8000
kill -9 <pid>
```

### Database Issues
Data lives in the PostgreSQL database configured during setup (the
`POSTGRES_*` values in `.env`). If the server refuses to start with a
database error, check that Postgres is running and reachable, then re-run the
connection setup:
```bash
python -c "import run_local; run_local.configure_database()"
```
To reset the app completely, drop and recreate the database:
```sql
DROP DATABASE ai_shuffle;
CREATE DATABASE ai_shuffle;
```

## Development Mode

For development with auto-reload:
```bash
# Activate venv first
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Run with reload
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

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
pip install -r requirements.txt ./vendor/fluid

# Rebuild frontend
cd frontend
npm install
npm run build
cd ..
rm -rf app/static/_app && cp -r frontend/build/* app/static/
```

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
database or restart an already running server. Restart the backend after it succeeds.
Native release archives already contain the frontend; replace the archive to update
that frontend, then run `--update` to install/build its runtime tools.
