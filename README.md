# AI Shuffle

> **Claude Code with a Web UI** — Run locally on your desktop or self-host anywhere.

<p align="center">
  <img src="docs/images/ai-shuffle-demo.png" alt="AI Shuffle" width="800"/>
</p>

<p align="center">
  <a href="#why-ai-shuffle">Why AI Shuffle?</a> •
  <a href="#run-it-your-way">Run It Your Way</a> •
  <a href="#features">Features</a> •
  <a href="#quick-start">Quick Start</a>
</p>

---

## Why AI Shuffle?

**AI Shuffle is Claude Code with a browser-based UI** — same powerful AI coding assistant, but accessible through your web browser instead of just the terminal.

**Two ways to use it:**

| | **Local Mode** | **Self-Hosted (Docker)** |
|---|---|---|
| **Use Case** | Replace Claude Code CLI on your desktop | Host for remote access or team use |
| **Where It Runs** | Your local machine | Any Docker host (NAS, server, cloud) |
| **Access From** | `localhost:8000` in your browser | Any device, anywhere |
| **Best For** | Solo developers who prefer a GUI | Teams, remote work, multi-device |

---

## Run It Your Way

### Option 1: Local Desktop (Replace Claude Code CLI)

Run AI Shuffle directly on your development machine as a **local web app**. Same computer, better interface.

**Why run locally?**
- **Visual interface** — No more terminal-only interaction
- **Persistent sessions** — Your chat history is searchable and organized
- **Multi-tab workflow** — Work on multiple problems simultaneously
- **Built-in tools** — Image generation, file browsing, analytics dashboard
- **Same authentication** — Uses your existing `claude login` credentials

```
┌─────────────────────────────────────────────┐
│  Your Desktop                               │
│  ┌───────────────┐    ┌──────────────────┐  │
│  │ AI Shuffle    │───▶│ Browser UI       │  │
│  │ (Python)      │    │ localhost:8000   │  │
│  └───────────────┘    └──────────────────┘  │
│         │                                   │
│         ▼                                   │
│  ┌───────────────┐                          │
│  │ Your Projects │                          │
│  │ (local files) │                          │
│  └───────────────┘                          │
└─────────────────────────────────────────────┘
```

### Option 2: Self-Hosted (Docker)

Run AI Shuffle on any Docker host — your home server, NAS (Unraid, Synology), cloud VM, or anywhere Docker runs.

**Why self-host?**
- **Access anywhere** — Work from laptop, tablet, phone, any browser
- **Team collaboration** — Shared profiles, projects, and usage analytics
- **Always running** — No need to start/stop, it's just there
- **Multiple workspaces** — Different projects on different devices
- **Your infrastructure** — Run on your NAS, home server, or cloud VM

```
┌────────────────────────────────────────────────────────┐
│  Your Server / NAS / Cloud VM                          │
│  ┌─────────────────────┐                               │
│  │ AI Shuffle (Docker) │                               │
│  │ Port 8000           │◀──────┐                       │
│  └─────────────────────┘       │                       │
│            │                   │                       │
│            ▼                   │                       │
│  ┌─────────────────────┐       │                       │
│  │ Mounted Volumes     │       │                       │
│  │ /data, /workspace   │       │                       │
│  └─────────────────────┘       │                       │
└────────────────────────────────│───────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
        ▼                        ▼                        ▼
   ┌─────────┐             ┌─────────┐             ┌─────────┐
   │ Laptop  │             │ Tablet  │             │ Phone   │
   │ Browser │             │ Browser │             │ Browser │
   └─────────┘             └─────────┘             └─────────┘
```

---

## vs Claude Code CLI

| Feature | Claude Code CLI | AI Shuffle |
|---------|-----------------|------------|
| **Interface** | Terminal only | Web browser UI |
| **Multi-Session** | One terminal at a time | Multiple tabs, persistent history |
| **Session History** | Local `.jsonl` files | Searchable, tagged, exportable |
| **File Browsing** | CLI commands | Visual file browser |
| **Remote Access** | SSH required | Just open a URL (self-hosted) |
| **Mobile/Tablet** | Not practical | Fully responsive |
| **AI Image/Video/3D** | Manual setup | Built-in tools |
| **Token Analytics** | Basic | Full usage dashboard |
| **Team Sharing** | Not built-in | Self-host for your team |

---

## Data & Privacy

**Your data stays yours:**

- **All conversations stored locally** — SQLite database on your machine/server
- **Direct API connection** — Your requests go straight to Anthropic
- **Same auth as CLI** — Uses Claude OAuth, no separate API key needed
- **Export everything** — Sessions, analytics, all your data
- **No telemetry** — Nothing phones home

---

## Features

### Custom Profiles — Build Any Agent You Want

Profiles are the heart of AI Shuffle's customization. Create specialized AI agents tailored to your exact workflow:

- **Custom system prompts** — Define exactly how your agent behaves
- **Allowed/disallowed tools** — Control what the agent can do (file access, git, terminal, etc.)
- **Auto-accept patterns** — Let trusted operations run without confirmation
- **Autonomous mode** — Create fully autonomous background agents that work independently

**Example use cases:**
- **Code reviewer** — An agent that only reads files and provides feedback
- **PR bot** — Autonomous agent that picks up issues, implements fixes, and creates PRs
- **Documentation writer** — Agent focused on generating and updating docs
- **Refactoring assistant** — Agent with full file access but restricted to specific directories

```
┌─────────────────────────────────────────────────────────────┐
│  Profile: "Autonomous PR Bot"                               │
│  ─────────────────────────────────────────────────────────  │
│  System Prompt: "You are an autonomous coding agent..."     │
│  Allowed Tools: [Read, Write, Edit, Bash, Git]              │
│  Auto-accept: ["git commit", "git push", "gh pr create"]    │
│  Mode: Fully autonomous — no user interaction required      │
└─────────────────────────────────────────────────────────────┘
         │
         ▼  Runs in background
┌─────────────────────────────────────────────────────────────┐
│  1. Picks up GitHub issue                                   │
│  2. Creates branch                                          │
│  3. Implements fix                                          │
│  4. Runs tests                                              │
│  5. Creates PR                                              │
│  6. Notifies you when done ────────────────────────▶ 🔔     │
└─────────────────────────────────────────────────────────────┘
```

### Web Notifications — Never Miss Background Work

Enable browser notifications to stay informed without watching the screen:

- **Session complete** — Get notified when a chat session finishes
- **Background tasks** — Know when autonomous agents complete their work
- **Subagent updates** — Track progress of spawned subagents
- **Works everywhere** — Desktop and mobile browser notifications

Perfect for:
- Running long tasks while doing other work
- Monitoring autonomous agents
- Self-hosted setups where you're not always watching the browser

### AI Chat & Conversations

<p align="center">
  <img src="docs/images/chat-input.png" alt="Chat Input" width="700"/>
  <br/>
  <em>Clean chat interface with Local/Worktree execution modes, profile & project selection</em>
</p>

<p align="center">
  <img src="docs/images/chat-conversation.png" alt="Chat Conversation" width="700"/>
  <br/>
  <em>Rich conversation view with tool execution, copy/read-aloud, and detailed summaries</em>
</p>

- **Multi-tab chat sessions** — work on multiple problems simultaneously
- **Profiles & Projects** — context-aware configurations for different workflows
- **Execution modes** — local execution or git worktree sandboxing for safety
- **Real-time token tracking** — know your costs as you work
- **Session management** — save, fork, export, tag, and search history

### The Deck (Card-Based Workspace)
A unique workspace system with draggable, resizable cards:
- **Card Types**: Chat, Terminal, Files, Settings, Profiles, Projects, Subagents, Plugins
- **Layout Modes**: Freeflow, Side-by-side, Tile, Stack, Focus
- **Mobile-first**: Swipe navigation on touch devices

### AI-Powered Creative Tools
- **Image Generation** — Google Gemini, Imagen 4, OpenAI GPT-Image
- **Image Editing** — AI-powered editing with natural language
- **Video Generation** — Google Veo, OpenAI Sora
- **3D Model Generation** — Text-to-3D and image-to-3D with Meshy
- **Audio Processing** — Text-to-speech and transcription

### Developer Experience
- **Integrated file browser** — navigate and edit project files
- **Full terminal** — xterm.js-based terminal in the browser
- **Git integration** — branches, diffs, worktrees, repository info
- **GitHub integration** — PRs, issues, OAuth authentication
- **Plugin system** — extend with custom tools and workflows

### Security (Self-Hosted)
- **Two-factor authentication (2FA)** built-in
- **Rate limiting** and brute-force protection
- **Session management** with configurable expiration
- **Proper CORS and cookie security**

---

## Quick Start

### Local Desktop

#### Option A: Download Release Package (Easiest)

Download the pre-built package for your platform:

| Platform | Download |
|----------|----------|
| **Windows** | [ai-shuffle-v1.5.0-windows.zip](https://github.com/HobbyCoders/ai-shuffle/releases/download/v1.5.0/ai-shuffle-v1.5.0-windows.zip) |
| **macOS** | [ai-shuffle-v1.5.0-macos.tar.gz](https://github.com/HobbyCoders/ai-shuffle/releases/download/v1.5.0/ai-shuffle-v1.5.0-macos.tar.gz) |
| **Linux** | [ai-shuffle-v1.5.0-linux.tar.gz](https://github.com/HobbyCoders/ai-shuffle/releases/download/v1.5.0/ai-shuffle-v1.5.0-linux.tar.gz) |

```bash
# Extract and run
# Windows: Unzip, then run run_local.bat
# macOS/Linux:
tar -xzf ai-shuffle-v1.5.0-*.tar.gz
cd ai-shuffle
./run_local.sh
```

Open `http://localhost:8000` in your browser.

#### Option B: Clone Repository (Latest Changes)

For the most recent updates or if you want to contribute:

```bash
# Clone the repository
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle

# Setup Python environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Setup frontend
cd frontend && npm install && cd ..

# Configure for local mode
cp .env.example .env
# Edit .env and set: LOCAL_MODE=true

# Run it
python run_local.py
```

Open `http://localhost:8000` in your browser.

#### Authentication

AI Shuffle uses your existing Claude login credentials:

```bash
# If you haven't logged in to Claude before:
claude login
```

---

### Docker (Self-Hosted)

#### Option A: Pre-built Docker Image (Easiest)

Pull and run the pre-built image from GitHub Container Registry:

```bash
# Create a directory for AI Shuffle
mkdir ai-shuffle && cd ai-shuffle

# Download docker-compose.yml
curl -O https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/docker-compose.yml

# Download example environment file
curl -O https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/.env.example
cp .env.example .env

# Edit docker-compose.yml to use the pre-built image:
# Uncomment: image: ghcr.io/hobbycoders/ai-shuffle:latest
# Comment out the 'build:' section

# Start the container
docker-compose up -d

# Authenticate with Claude
docker exec -it ai-shuffle claude login
```

#### Option B: Build from Source

Build the Docker image locally from the repository:

```bash
# Clone the repository
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle

# Configure
cp .env.example .env
# Edit .env with your settings

# Build and start
docker-compose up -d --build

# Authenticate with Claude
docker exec -it ai-shuffle claude login
```

#### Access

- **Local Docker**: `http://localhost:8000`
- **Remote access**: Set up a reverse proxy (Nginx, Caddy, Traefik) with HTTPS

#### Unraid Users

AI Shuffle works great on Unraid! Use the Community Applications template or configure manually with:
- **PUID/PGID**: `99/100` (default Unraid user)
- **Volumes**: Map `/data` and `/workspace` to your preferred locations

---

## Configuration

Key environment variables in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `LOCAL_MODE` | `true` for local desktop, `false` for Docker | `false` |
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `ENABLE_2FA` | Two-factor authentication (self-hosted) | `false` |
| `COOKIE_SECURE` | Secure cookies — set `false` for local HTTP | `true` |

See [`.env.example`](.env.example) for all options.

---

## Architecture

```
ai-shuffle/
├── app/                    # Backend (FastAPI + Python)
│   ├── api/               # API routes
│   ├── core/              # Business logic, Claude integration
│   └── db/                # Database models
├── frontend/              # Frontend (SvelteKit + Svelte 5)
│   └── src/
│       ├── lib/           # Components, stores, utilities
│       └── routes/        # Pages
├── tools/                 # AI tools (image, video, 3D generation)
└── plugins/               # Plugin system
```

**Tech Stack:**
- **Frontend**: Svelte 5, SvelteKit, Tailwind CSS, xterm.js
- **Backend**: FastAPI, WebSockets, SQLite
- **AI**: Claude Agent SDK, Google AI, OpenAI, Meshy

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd/Ctrl + K` | Spotlight search |
| `Cmd/Ctrl + N` | New chat |
| `Cmd/Ctrl + /` | Keyboard shortcuts help |

---

## Contributing

Contributions welcome! Please read our contributing guidelines before submitting PRs.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- Built with [Claude](https://anthropic.com/claude) by Anthropic
- UI inspired by modern desktop workspace applications
