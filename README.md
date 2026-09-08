# AI Shuffle

> A self-hosted web UI for AI Agents — run locally on your desktop or deploy via Docker for remote access from any device.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.8.5-green.svg)](https://github.com/HobbyCoders/ai-shuffle/releases)

## What Is AI Shuffle?

AI Shuffle puts AI agents in a browser. Instead of a terminal, you get a full GUI with persistent sessions, multi-tab workflows, voice input, AI-powered media generation, and a plugin system — all accessible from your phone, laptop, or any device with a browser.

**Two ways to run it:**

- **Local mode** — Replace the Claude Code CLI on your desktop (`localhost:8000`)
- **Docker** — Self-host on a server or NAS for access from anywhere

**Three ways to open it:** any browser, the [Android app](android/README.md), or
the [desktop app](desktop/README.md) for Windows, macOS and Linux. The desktop
app runs the worker on your computer (bring your own PostgreSQL) and shows the
same web UI in a native window; the Android app connects to your workers.

---

## Feature Overview

### Chat Interface
Real-time streaming with WebSocket, rich text input (TipTap editor), `@file` autocomplete, drag-and-drop file upload, inline thinking blocks, tool use visualization, voice input (STT), and a permission queue that surfaces Claude's approval requests as interactive prompts.

### Profiles
Create specialized AI configurations with custom system prompts, model selection from a built-in model registry (Anthropic's Claude models by default; admins can also add OpenRouter, custom Anthropic-compatible gateways, or a Codex / ChatGPT-subscription provider), thinking modes, permission modes, tool allowlists, and per-profile subagent and plugin controls. Generate profiles from natural language descriptions.

### Voice Mode
Talk to a voice agent (OpenAI Realtime, direct browser connection) that hands real work to your agents, steers or stops them mid-task, relays their permission prompts, and can manage automations, hooks, agents, MCP servers and connectors by voice. Each agent opts in with a per-agent Voice model assignment.

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
Manage stdio, SSE, and HTTP MCP servers across four scopes (user, local, project, installed). A built-in `browser` MCP server (Playwright over CDP) is bundled for web automation — see [Browser Automation](#browser-automation).

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
The web UI is the desktop layout at every window size; narrow windows collapse the sidebar into an overlay and tighten spacing. The Android app renders a separate mobile shell with a compact app bar, slide-in drawer, collapsing composer and 44px touch targets. PWA install support, offline detection, and push notifications work in both. See [docs/mobile-desktop-ui-split.md](docs/mobile-desktop-ui-split.md).

Pair another browser, phone, tablet, or cluster runner with a short-lived,
one-time code or locally rendered QR. Local/LAN use requires no Shuffle account; optionally link a worker to an account for managed remote access.

### Security
Local/LAN owner access is isolated from the authenticated remote listener. Remote browser and Android access use Clerk-backed owner identity and revocable device sessions. Integration API keys (`aih_`) remain scoped and non-admin. Stored credentials use Fernet with protected persistent key custody; existing password-derived keys are adopted once without rewriting their ciphertext. See [local and remote access](docs/LOCAL_ACCESS.md) and [recovery](docs/RESTORE.md).

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

Open `http://localhost:8000`. Choose your workspace and connect your development tools or model providers on first launch. No local username, password, or Shuffle account is required.

**Options:** `--port 3000` · `--no-browser` · `--check` (verify prerequisites)

### Docker

```bash
# Configure secrets first — compose fails fast if POSTGRES_PASSWORD is unset
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD

# Bring up the app together with its bundled PostgreSQL
docker compose up -d
```

The compose stack provisions PostgreSQL for you (`postgres:16-alpine`); the app
waits for the database to be healthy before starting. Open `http://localhost:8000`
and choose your workspace and model providers. No local username, password, or Shuffle account is required.

**Key environment variables:**

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | — | **Required.** Database password — compose fails fast if unset. The app builds its own connection URL from the `POSTGRES_*` pieces |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_DB` | `postgres` / `5432` / `ai_shuffle` / `ai_shuffle` | Remaining connection pieces — only set them if your database lives outside the bundled compose service |
| `DATABASE_URL` | — | *Legacy.* Full connection string from pre-split installs — auto-imported into the `POSTGRES_*` values at startup, so a `.env` with only this set is fully configured (no re-setup needed); still wins at connect time when explicit pieces are also present |
| `ADMIN_PASSWORD` | — | Upgrade only: unlock legacy ciphertext for its first verified key adoption, then remove. Never needed on a fresh installation |
| `SHUFFLE_LOCAL_ACCESS` | `true` | Allow owner access on the local/LAN listener. Set `false` during a staged upgrade until managed tunnel ingress is verified |
| `PUID` / `PGID` | `1000` | Runtime user/group for file permissions (Unraid: `99` / `100`) |
| `AUTO_UPDATE` | `true` | Auto-update the GitHub CLI on startup (the Claude engine ships with `claude-agent-sdk`) |
| `LOCAL_MODE` | `false` | Use local filesystem paths |
| `COMMAND_TIMEOUT` | `300` | Subprocess timeout (seconds) |
| `CHROME_CDP_PORT` | `9221` | Loopback DevTools (CDP) port the browser listens on and the tools attach to |
| `CHROME_EXECUTABLE` | auto | Path to the Chrome / Chromium / Edge binary (auto-detected; set if yours is elsewhere) |
| `BROWSER_MCP_ENTRY` | packaged | Override the browser-mcp entry point (defaults to the packaged Docker path, else `<repo>/browser-mcp/dist/index.js`) |
| `CHROME_MAX_RESTARTS` | `8` | Circuit breaker: after this many *rapid* crash-restarts the supervisor stops relaunching the browser for the container's lifetime (protects the PID budget). `0` = retry forever |
| `CHROME_HEALTHY_SECS` | `60` | A browser that stays up at least this long counts as healthy and resets the crash counter/backoff |
| `CHROME_BACKOFF_MAX` | `300` | Cap (seconds) on the exponential backoff between rapid restarts |

### Browser Automation

AI Shuffle ships its own **browser MCP server** — `browser-mcp`, a
provider-agnostic, token-conservative server built on Playwright over CDP — so
agent sessions can drive a real browser through `mcp__browser__*` tools (navigate,
snapshot, click/type, screenshot, find, read-text, eval, tabs, network, and more).
There's **no browser extension, no separate account, and no pairing** — it just
works once enabled.

- **Docker:** the image bundles Chromium and supervises it automatically
  (headless, as the app user) over a loopback DevTools port (`CHROME_CDP_PORT`,
  default `9221`). A per-session MCP server attaches to it.
- **Native installs:** the backend auto-launches and supervises a local
  Chrome / Chromium / Edge the same way. Point `CHROME_EXECUTABLE` at your binary
  if it isn't auto-detected. To run your own browser instead, just start it on
  the CDP port — if the port already answers, the app won't spawn another.

Enable it under **Settings → Browser** (admin): a single toggle turns the tools on
or off and a health dot shows whether the browser is reachable. The browser profile
persists in the `browser-profile` volume, so logins survive restarts.

**Where the browser may connect.** Cloud metadata (`169.254.0.0/16`) is always
refused and no setting re-enables it. Private LAN ranges and the public internet
are allowed. Loopback — `127.0.0.1`, `localhost`, `::1` — is the one range that
depends on how you run the app:

| Setting | Default | Description |
|---|---|---|
| `BROWSER_SSRF_ALLOW_LOOPBACK` | auto | Whether the agent's browser may open `localhost` URLs. Unset = allowed in local mode, refused in Docker. Set `true` on a single-user self-hosted install so an agent can view a dev server it started itself; leave it off if untrusted people can prompt this install |
| `BROWSER_SSRF_ALLOW` | — | Comma-separated CIDRs to allow outright (e.g. `127.0.0.0/8,::1/128`). Set explicitly, it replaces the automatic choice above |
| `BROWSER_SSRF_BLOCK` | — | Comma-separated CIDRs to refuse. Wins over any allow entry |

Turning loopback on lets a page the agent visits reach services bound to
`127.0.0.1` inside the container — including this app's own API, using whatever
session the browser profile holds. That is a fair trade on a machine with one
user and no fair trade on a shared one, which is why Docker defaults to off. A
refused address explains itself in the tool result, naming the setting that
would allow it.

> 🔒 The DevTools port is bound to `127.0.0.1` only and is never published to the
> network. Egress filtering aside, the agent drives a real browser with your
> profile and your logins — isolate the container if you run untrusted prompts.
> Diagnose the browser from the Settings panel, or with
> `docker exec -u appuser -e HOME=/home/appuser ai-shuffle chrome-doctor`.

---

## Prerequisites

- **Python 3.12+**
- **Node.js 22.12+** — For building the frontend and the bundled `browser-mcp` server
- **PostgreSQL** — The database backend (provisioned automatically by Docker Compose)
- **GitHub CLI** — [Download](https://cli.github.com/) (used for PR reviews, issue management, etc.)

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | SvelteKit · Svelte 5 · Tailwind CSS · TipTap |
| **Backend** | FastAPI · WebSockets · PostgreSQL |
| **AI Engine** | Claude Agent SDK (Python) · pluggable model providers |
| **Media** | fal.ai (image, video, 3D, audio) |
| **Browser** | `browser-mcp` — bundled Playwright-over-CDP MCP server driving a headless Chrome / Chromium |
| **CI/CD** | GitHub Actions |

---

## Project Structure

```
ai-shuffle/
├── app/               # FastAPI backend (API, WebSocket, DB, services)
├── frontend/          # SvelteKit frontend (Svelte 5, Tailwind)
├── fluid/             # The fluid agent harness (own pyproject; package is fluid/fluid)
├── android/           # Android app (Kotlin/Gradle) and the Cloudflare account hub (android/hub)
├── browser-mcp/       # Bundled browser-control MCP server (Playwright/CDP)
├── tools/             # AI tool scripts (fal.ai integrations)
├── docs/              # Technology documentation
├── tests/             # Backend test suite (fluid's own suite lives in fluid/tests)
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

### Refreshing a native checkout

After `git pull`, run `python run_local.py --update` (or the platform launcher with
`--update`). This installs the checked-in Fluid harness and locked Node packages,
then rebuilds the frontend, browser MCP and AI tools. It does not reconfigure the
database or restart an already running server. Restart the backend after it succeeds.
Native release archives already contain the frontend; replace the archive to update
that frontend, then run `--update` to install/build its runtime tools.
