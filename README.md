# AI Hub
A full-featured web interface and API server for Claude Code, providing a Claude.ai-like chat experience with multi-user support, API access, and advanced AI agent management.

![Version](https://img.shields.io/badge/version-4.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ⚠️ Important Legal Disclaimer

**This software is provided for personal, educational, and development purposes only.**

This application can use Claude Code CLI with OAuth authentication, which is governed by [Anthropic's Consumer Terms of Service](https://www.anthropic.com/legal/consumer-terms). Please be aware of the following:

1. **Personal Use Only**: Anthropic's Consumer Terms prohibit accessing Claude "through automated or non-human means, whether through a bot, script, or otherwise" except via official API keys.

2. **No Production Deployment**: Do NOT deploy this application as a production service, commercial product, or multi-user platform using OAuth authentication. This would violate Anthropic's Terms of Service.

3. **For Production Use**: If you want to build a production application, you MUST use this with proper API keys under Anthropic's [Commercial Terms](https://www.anthropic.com/legal/commercial-terms).

---

## Experimental Software Notice

**This application is highly experimental and under active development.**

- **Expect Bugs**: This software is in early development and will have bugs. Please report issues, but understand they may not be fixed immediately.
- **Solo Developer**: This is a personal project maintained by a single developer in my spare time. There is no team or organization behind it.
- **Use at Your Own Risk**: This software is provided as-is for educational and experimental purposes.

---

## Overview

AI Hub lets you self-host a Claude Code web interface and access Claude's capabilities without the cost of API keys. It acts as a bridge between Claude Code CLI (using OAuth authentication) and your applications, exposing Claude through:

- **Web UI** - Modern chat interface similar to Claude.ai
- **REST API** - OpenAI-compatible endpoints for programmatic access
- **WebSocket** - Real-time streaming and multi-device sync
- **Agent Profiles** - Customizable tool sets and system prompts

No API keys required (Can be used though **requires login from cli inside container**) - uses Claude Code's OAuth authentication.

## Features

### Chat Interface
- Real-time SSE streaming responses
- Tool use visualization (file operations, code edits, bash commands)
- Markdown rendering with syntax highlighting
- Multi-tab conversations
- Dark theme optimized for coding
- File upload/attachments
- Spotlight search (Cmd/Ctrl+K)

### Agent System
- **Profiles** - Configure tool access, models, and system prompts
- **Subagents** - Delegate tasks to specialized Claude instances
- **Slash Commands** - Built-in (`/compact`, `/context`) and custom commands
- **Checkpoints** - Save and rewind conversation state
- **Model Selection** - Choose between Sonnet, Opus, and Haiku

### Multi-Device Sync
- Real-time message synchronization across devices
- Cross-device session control
- WebSocket-based event broadcasting

### Session Management
- Persistent conversation history
- Resume, fork, or delete conversations
- Project and profile-based filtering

### API Access
- REST API with OpenAPI documentation
- API key authentication (`aih_*` format)
- Per-user project/profile restrictions
- One-shot and streaming endpoints

## Quick Start

### Docker Compose (Recommended)

# Pull and run
docker pull ghcr.io/quickkill0/ai-hub:latest
docker run -d -p 8000:8000 -v ai-hub-data:/data ghcr.io/quickkill0/ai-hub:latest

### First-Time Setup

1. Open http://localhost:8000
2. Create your admin account
3. Authenticate Claude in app on settings page.
4. Start chatting

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `WORKSPACE_DIR` | Project workspace path | `./workspace` |
| `PUID` / `PGID` | Process user/group ID | `1000` / `1000` |
| `SESSION_EXPIRE_DAYS` | Admin session duration | `30` |
| `COMMAND_TIMEOUT` | Claude command timeout (seconds) | `300` |
| `AUTO_UPDATE` | Update Claude/gh CLI on startup | `true` |

### Docker Volumes

| Volume | Container Path | Purpose |
|--------|----------------|---------|
| `claude-auth` | `/home/appuser/.claude` | Claude OAuth credentials |
| `gh-auth` | `/home/appuser/.config/gh` | GitHub CLI authentication |
| `ai-hub-data` | `/data` | SQLite database |
| `workspace` | `/workspace` | Project files |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Web Browser (Svelte SPA)                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP / SSE / WebSocket
┌──────────────────────────▼──────────────────────────────────┐
│                      FastAPI Server                          │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐   │
│  │   Auth    │ │   Query   │ │  Session  │ │   Sync    │   │
│  │  Service  │ │  Engine   │ │  Manager  │ │  Engine   │   │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘   │
│                        │                                     │
│  ┌───────────┐ ┌───────▼───┐ ┌───────────┐ ┌───────────┐   │
│  │ Profiles  │ │  Claude   │ │  SQLite   │ │ Subagents │   │
│  │  Service  │ │ Agent SDK │ │    DB     │ │  Manager  │   │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│               Claude Code CLI (OAuth authenticated)          │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                      Anthropic API                           │
└─────────────────────────────────────────────────────────────┘
```

## API Reference

Full documentation available at `/docs` when running.

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/auth/setup` | Create admin account (first-run) |
| POST | `/api/v1/auth/login` | Login |
| POST | `/api/v1/auth/logout` | Logout |
| GET | `/api/v1/auth/status` | Check auth status |

### Query
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/query` | One-shot query |
| POST | `/api/v1/query/stream` | Streaming query (SSE) |
| POST | `/api/v1/conversation` | Multi-turn conversation |
| POST | `/api/v1/conversation/stream` | Streaming conversation |

### Sessions
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/sessions` | List sessions |
| GET | `/api/v1/sessions/:id` | Get session with history |
| DELETE | `/api/v1/sessions/:id` | Delete session |

### Profiles
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/profiles` | List all profiles |
| POST | `/api/v1/profiles` | Create profile |
| PUT | `/api/v1/profiles/:id` | Update profile |
| DELETE | `/api/v1/profiles/:id` | Delete profile |

### System
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/version` | Version info |
| GET | `/api/v1/stats` | Usage statistics |

## Development

### Local Setup

```bash
# Backend
pip install -r requirements.txt
python -m app.main

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

### Building

```bash
# Docker image
docker-compose build

# Frontend only
cd frontend
npm run build
```

### Project Structure

```
ai-hub/
├── app/                    # FastAPI application
│   ├── main.py             # Entry point
│   ├── api/                # Route handlers
│   ├── core/               # Business logic
│   └── db/                 # Database layer
├── frontend/               # Svelte SPA
│   ├── src/
│   │   ├── routes/         # Pages
│   │   └── lib/            # Components, stores, API
│   └── package.json
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## Tech Stack

**Backend:** Python 3.11, FastAPI, Claude Agent SDK, SQLite

**Frontend:** Svelte 5, SvelteKit, TypeScript, Tailwind CSS

**Container:** Docker, Claude Code CLI, GitHub CLI

## Unraid Installation

1. Go to **Docker** tab → **Add Container**
2. Set repository to your image
3. Configure `PUID=99`, `PGID=100`
4. Map port `8000`
5. Add volumes for persistence
6. Login on settings page after starting

## Troubleshooting

### Claude Not Authenticated
- Authenticate in app on settings page.

### Check Diagnostics
```bash
curl http://localhost:8000/api/v1/auth/diagnostics
```

### PUID/PGID Issues
- Unraid: `PUID=99`, `PGID=100`
- Standard: `PUID=1000`, `PGID=1000`

## License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE) file for details.

### Third-Party Licenses

This project uses the following open-source dependencies:

| Package | License | Use |
|---------|---------|-----|
| [FastAPI](https://fastapi.tiangolo.com/) | MIT | Backend framework |
| [Uvicorn](https://www.uvicorn.org/) | BSD-3-Clause | ASGI server |
| [Pydantic](https://pydantic.dev/) | MIT | Data validation |
| [Svelte](https://svelte.dev/) | MIT | Frontend framework |
| [SvelteKit](https://kit.svelte.dev/) | MIT | Frontend meta-framework |
| [Tailwind CSS](https://tailwindcss.com/) | MIT | CSS framework |
| [bcrypt](https://github.com/pyca/bcrypt) | Apache-2.0 | Password hashing |
| [httpx](https://www.python-httpx.org/) | BSD-3-Clause | HTTP client |
| [xterm.js](https://xtermjs.org/) | MIT | Terminal emulator |

### Claude Code CLI

This application interacts with Claude Code CLI, which is provided by Anthropic. Claude Code CLI usage is subject to:
- [Anthropic's Consumer Terms of Service](https://www.anthropic.com/legal/consumer-terms)
- [Anthropic's Usage Policy](https://www.anthropic.com/legal/aup)
- [Anthropic's Privacy Policy](https://www.anthropic.com/legal/privacy)

**Claude Code CLI is NOT bundled with this software** - it is installed separately and authenticated with your own Anthropic account.

## Acknowledgments

- [Anthropic](https://www.anthropic.com/) - Claude AI and Claude Code CLI
- [FastAPI](https://fastapi.tiangolo.com/) - Excellent Python web framework
- [Svelte](https://svelte.dev/) - Reactive frontend framework
- [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk) - Official SDK for Claude Code integration
