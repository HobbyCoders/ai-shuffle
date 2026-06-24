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
- **Python 3.10+** — [Download](https://www.python.org/downloads/)
- **Node.js 18+** — [Download](https://nodejs.org/) (for building frontend)
- **GitHub CLI** — [Download](https://cli.github.com/) (used for PR reviews, issue management, etc.)

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
pip install -r requirements.txt
# Optional: computer-use (host desktop control), native runs only
pip install -r requirements-native.txt
```

### 4. Build Frontend
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

### 5. Start the Server
```bash
# Set local mode
export LOCAL_MODE=true  # or set LOCAL_MODE=true on Windows

# Start
python -m app.main
```

Visit http://127.0.0.1:8000 in your browser. On first launch, you'll be guided through account setup and Claude authentication.

## Configuration

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

### Browser automation — Claude in Chrome

AI Shuffle's built-in browser integration is Anthropic's **Claude in Chrome**
(built-in server `claude-in-chrome`). When enabled, agent sessions launch the
bundled Claude Code CLI with `--chrome`, which exposes `mcp__claude-in-chrome__*`
tools and drives a **real, signed-in Chrome/Chromium** through the Claude in
Chrome browser extension — not a throwaway headless browser.

On **Windows local mode** this uses your existing desktop Chrome:

1. Install the **Claude in Chrome** extension in Chrome and sign in to your
   Claude account (the same setup you use for Claude Code's `/chrome`).
2. Keep that Chrome running while AI Shuffle sessions are active. The CLI
   auto-installs its native-messaging host (registered under
   `HKCU\Software\Google\Chrome\NativeMessagingHosts`) the first time a session
   runs with `--chrome`; pair the browser once when prompted.
3. The integration is on by default. Toggle it off via the `claude-in-chrome`
   entry in the MCP admin UI.

> **Concurrency:** Claude in Chrome pairs to a single account/browser. AI Shuffle
> runs multiple pooled sessions that all share that one browser, so browser
> actions across concurrent sessions may serialize or interfere. Use it from one
> active session at a time for predictable results.

> **Docker:** the container image bundles Google Chrome + the Claude in Chrome
> extension and launches a headless browser automatically (`START_CHROME=1`). Do
> the one-time sign-in/pairing from the app — **Settings → Browser → Set up
> browser** opens an interactive view of the headless browser behind the admin
> login (no exposed DevTools port). See "Claude in Chrome (Docker)" in the README.

> **Security note (SSRF / local-network reach).** The agent drives a real browser
> with **no egress filtering** and your real cookies/sessions. It can reach
> `localhost`/loopback services, other RFC1918 private hosts on your network, and
> cloud metadata endpoints such as `169.254.169.254` (which on some clouds exposes
> credentials). If you run untrusted prompts, isolate the host at the network
> layer or disable the `claude-in-chrome` built-in server in the MCP admin UI.

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
The SQLite database is stored in your data directory. To reset:
```bash
# Find and delete db.sqlite
# Windows:
del "%APPDATA%\ai-shuffle\db.sqlite"

# macOS:
rm ~/Library/Application\ Support/ai-shuffle/db.sqlite

# Linux:
rm ~/.local/share/ai-shuffle/db.sqlite
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
pip install -r requirements.txt

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
- Report issues: https://github.com/yourusername/ai-shuffle/issues
