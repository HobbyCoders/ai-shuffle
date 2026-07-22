# AI Shuffle

> A self-hosted web UI for Claude Code — run locally on your desktop or deploy via Docker for remote access from any device.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.2.1-green.svg)](https://github.com/HobbyCoders/ai-shuffle/releases)

## What Is AI Shuffle?

AI Shuffle puts Claude Code in a browser. Instead of a terminal, you get a full GUI with persistent sessions, multi-tab workflows, voice input, AI-powered media generation, and a plugin system — all accessible from your phone, laptop, or any device with a browser.

**Two ways to run it:**

- **Local mode** — Replace the Claude Code CLI on your desktop (`localhost:8000`)
- **Docker** — Self-host on a server or NAS for access from anywhere

---

## Feature Overview

### Chat Interface
Real-time streaming with WebSocket, rich text input (TipTap editor), `@file` autocomplete, drag-and-drop file upload, inline thinking blocks, tool use visualization, voice input (STT), and a permission queue that surfaces Claude's approval requests as interactive prompts.

### Profiles
Create specialized AI configurations with custom system prompts, model selection (Haiku / Sonnet / Opus), thinking modes, permission modes, tool allowlists, and per-profile subagent and plugin controls. Generate profiles from natural language descriptions.

### Sessions
Multi-tab persistent sessions with full history. Fork conversations at any point, rewind to prior states, tag, favorite, search, group, and import/export. Token usage and cost tracking per session.

### 14 Connectors
Bridge Claude to external platforms — each connector supports triggers, actions, and tools:

| Messaging | Smart Home & Services |
|---|---|
| Telegram, Discord, WhatsApp, Slack, Signal, MS Teams, LINE, iMessage, BlueBubbles | Home Assistant, Philips Hue, Spotify, Gmail, Webhook |

Connectors relay permission requests as interactive approve/deny buttons on their platforms.

### Automations
Run AI workflows without manual intervention. Trigger via cron, interval, webhook, connector events, or connector triggers. Supports prompt templates with variable substitution, budget controls (cost/run/day caps), human-in-the-loop approval, and output routing to sessions, connectors, or webhooks.

### Hooks
Execute custom code at lifecycle events: `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreCompact`, `SubagentStop`, `Notification`. Run bash commands or fire webhooks with HMAC signing. Filter by tool name.

### Subagents
Specialized AI agents Claude can invoke mid-conversation. Create custom subagents with model selection and tool restrictions. Autocomplete via `/agent-name` in chat.

### MCP Servers (Model Context Protocol)
Manage stdio, SSE, and HTTP MCP servers across four scopes (user, local, project, installed). Built-in browser automation MCP server included.

### Projects & Git
Full project management with file browser, git status/diff, branch management, commit (with AI-generated messages), push, pull request creation, GitHub CI status, and worktree support.

### AI Media Tools (fal.ai)
Image generation & editing, video generation & extension, image-to-video, text/image-to-3D, text-to-speech, and speech-to-text — powered by 600+ fal.ai models. Configurable per profile.

### Plugins
Extend AI Shuffle with external packages from git-based marketplaces. Plugins contribute hooks, commands, agents, skills, and MCP servers. Enable globally or per profile.

### Notes
Global markdown notes with tags, pinning, and search. Each note backed by a physical file on disk.

### Built-in Documentation
Full documentation rendered in-app covering getting started, all features, connectors, and API reference.

### Mobile & PWA
Fully responsive layout with a mobile-optimized sidebar, 44px touch targets, PWA install support, offline detection, and push notifications.

### Security
BCrypt password hashing, session tokens (secure HTTP-only cookies), API key authentication (`aih_` prefix, SHA-256 hashed), rate limiting, audit logging, encrypted secret storage (Fernet/PBKDF2), OAuth 2.0 + PKCE for Claude authentication, and GitHub device flow.

### Analytics
Usage dashboard with token counts, cost breakdowns by model/profile/project, daily trends, and top sessions by cost.

---

## Quick Start

### Local (Desktop)

```bash
# Clone or download a release package
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle

# First-time setup (creates venv, installs deps, builds frontend)
python run_local.py --setup

# Start the server
python run_local.py
# Windows: run_local.bat
# macOS/Linux: ./run_local.sh
```

Open `http://localhost:8000`. You'll be guided through account setup and Claude authentication on first launch.

**Options:** `--port 3000` · `--no-browser` · `--check` (verify prerequisites)

### Docker

```bash
docker pull ghcr.io/hobbycoders/ai-shuffle:latest

# Or use docker-compose
docker compose up -d

```

**Key environment variables:**

| Variable | Default | Description |
|---|---|---|
| `ADMIN_PASSWORD` | — | Headless encryption key init |
| `PUID` / `PGID` | `1000` | Runtime user/group for file permissions |
| `AUTO_UPDATE` | `true` | Auto-update Claude CLI on startup |
| `LOCAL_MODE` | `false` | Use local filesystem paths |
| `COMMAND_TIMEOUT` | `300` | Subprocess timeout (seconds) |
| `START_CHROME` | `1` | Launch headless Chrome for Claude in Chrome (`0` to disable) |
| `CHROME_CDP` | `1` | Bind Chrome's DevTools port to `127.0.0.1` so the in-app **Set up browser** panel works (`0` to disable) |
| `CHROME_DEBUG` | `0` | Legacy: also expose DevTools on `:9222` over the network (not needed — the in-app panel replaces it) |
| `CHROME_MAX_RESTARTS` | `8` | Circuit breaker: after this many *rapid* Chrome crash-restarts the supervisor gives up and disables Claude in Chrome for the container's lifetime (protects the PID budget). `0` = retry forever |
| `CHROME_HEALTHY_SECS` | `60` | A Chrome that stays up at least this long counts as healthy and resets the crash counter/backoff |
| `CHROME_BACKOFF_MAX` | `300` | Cap (seconds) on the exponential backoff between rapid Chrome restarts |

### Claude in Chrome (Docker)

The image bundles **Google Chrome** and force-installs the Claude in Chrome
extension so agent sessions can drive a real browser (`mcp__claude-in-chrome__*`).
Chrome starts automatically (headless, as the app user) when the container boots.
The extension needs a **one-time sign-in + pairing** because the browser is
headless — do it from the app, no exposed ports required:

1. Sign in to AI Shuffle as **admin** and open **Settings → Browser**.
2. The panel shows a live status checklist (Chrome running, DevTools port,
   extension installed, paired). Make sure **Enable browser tools** is on, then
   click **Set up browser**.
3. An interactive view of the headless browser opens. Click **Extension sign-in**
   (or use the URL bar to go to `https://claude.ai`) and **sign in** to your
   Claude account, then **Authorize** the extension on the consent page.
4. If a pairing prompt appears, name the browser and **Connect**. The panel flips
   to **Paired ✓** automatically — you can close it.

That's it. The profile (extension, sign-in, pairing) persists in the
`browser-profile` volume and the pairing in `~/.claude.json` (on the `claude-auth`
volume), so the browser **auto-reconnects on later restarts** without prompting.

> 🔒 The DevTools port is bound to `127.0.0.1` only and is never published to the
> network; the **Set up browser** panel reaches it through an authenticated,
> admin-only proxy behind the normal app login. You do **not** need `CHROME_DEBUG`
> or a `9222` port mapping. Diagnose the chain from the panel, or with
> `docker exec -u appuser -e HOME=/home/appuser ai-shuffle chrome-doctor`.
>
> Like local mode, the agent drives a real browser with **no egress filtering**
> (SSRF / local-network reach) — isolate the container if you run untrusted prompts.

---

## Prerequisites

- **Python 3.10+**
- **Node.js 18+** — For building the frontend
- **GitHub CLI** — [Download](https://cli.github.com/) (used for PR reviews, issue management, etc.)

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | SvelteKit · Svelte 5 · Tailwind CSS · TipTap |
| **Backend** | FastAPI · WebSockets · SQLite |
| **AI Engine** | Claude Agent SDK (Python) |
| **Media** | fal.ai (image, video, 3D, audio) |
| **Browser** | Claude in Chrome (`--chrome`, via the Claude in Chrome extension) |
| **CI/CD** | GitHub Actions |

---

## Project Structure

```
ai-shuffle/
├── app/               # FastAPI backend (API, WebSocket, DB, services)
├── frontend/          # SvelteKit frontend (Svelte 5, Tailwind)
├── tools/             # AI tool scripts (fal.ai integrations)
├── docs/              # Technology documentation
├── tests/             # Backend test suite
├── .github/           # CI/CD workflows
├── docker-compose.yml
├── run_local.py       # Cross-platform local launcher
├── run_local.sh       # macOS/Linux launcher
└── run_local.bat      # Windows launcher
```

---

## API

AI Shuffle exposes a full REST + WebSocket API. See the [API Reference](docs/content/api-reference/) or browse the built-in docs at `http://your-host:8000` → Documentation.

## License

MIT — see [LICENSE](LICENSE)
