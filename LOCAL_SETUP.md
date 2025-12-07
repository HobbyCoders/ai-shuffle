# AI Hub - Local Installation Guide

Run AI Hub directly on your PC without Docker! Perfect for game development, local file access, and working with native applications.

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
- **Python 3.10+** - [Download](https://www.python.org/downloads/)
- **Node.js 18+** - [Download](https://nodejs.org/) (for building frontend)
- **Claude Code CLI** - Install after Node.js:
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```

### Optional
- **GitHub CLI** - [Download](https://cli.github.com/) (for GitHub integration)

## Manual Setup

If you prefer to set things up manually:

### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/ai-hub.git
cd ai-hub
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
```

### 4. Build Frontend
```bash
cd frontend
npm install
npm run build
cd ..

# Copy build to static directory
# Windows:
xcopy /e /i frontend\build app\static

# macOS/Linux:
cp -r frontend/build app/static
```

### 5. Authenticate Claude
```bash
claude login
```
Follow the prompts to authenticate with your Anthropic account.

### 6. Start the Server
```bash
# Set local mode
export LOCAL_MODE=true  # or set LOCAL_MODE=true on Windows

# Start
python -m app.main
```

Visit http://127.0.0.1:8000 in your browser.

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

In local mode, AI Hub stores data in platform-specific locations:

| Platform | Data Directory | Workspace |
|----------|---------------|-----------|
| Windows | `%APPDATA%\ai-hub` | `Documents\ai-hub-workspace` |
| macOS | `~/Library/Application Support/ai-hub` | `~/ai-hub-workspace` |
| Linux | `~/.local/share/ai-hub` | `~/ai-hub-workspace` |

Override with environment variables:
```env
DATA_DIR=/path/to/custom/data
WORKSPACE_DIR=/path/to/custom/workspace
```

## Differences from Docker Mode

| Feature | Docker | Local |
|---------|--------|-------|
| File access | Container only | Full PC access |
| Installation | Single command | Manual setup |
| Updates | Rebuild image | Git pull + rebuild |
| Port binding | Configurable | Default 8000 |
| Data isolation | Volume mounts | User directories |

## Troubleshooting

### "Claude CLI not found"
```bash
# Install globally via npm
npm install -g @anthropic-ai/claude-code

# Verify installation
claude --version
```

### "Frontend not built"
```bash
cd frontend
npm install
npm run build
cp -r build ../app/static
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
del "%APPDATA%\ai-hub\db.sqlite"

# macOS:
rm ~/Library/Application\ Support/ai-hub/db.sqlite

# Linux:
rm ~/.local/share/ai-hub/db.sqlite
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
cp -r build ../app/static
cd ..
```

## Security Notes

- Local mode binds to `127.0.0.1` by default (localhost only)
- To allow LAN access, set `HOST=0.0.0.0` (use with caution)
- `COOKIE_SECURE=false` is required for HTTP (no HTTPS)
- Claude credentials are stored in `~/.claude/.credentials.json`

## Getting Help

- Check prerequisites: `python run_local.py --check`
- View logs: Check terminal output
- Report issues: https://github.com/yourusername/ai-hub/issues
