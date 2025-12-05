# AI Hub

A full-featured web interface and API server for Claude Code, providing a Claude.ai-like chat experience with multi-user support, API access, and advanced AI agent management.

![Version](https://img.shields.io/badge/version-4.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Overview

AI Hub acts as a bridge between Claude Code CLI (OAuth authentication) and users/applications, exposing Claude's capabilities through:

- **Web UI** - Modern chat interface similar to Claude.ai
- **REST API** - OpenAI-compatible endpoints for programmatic access
- **WebSocket** - Real-time streaming and multi-device sync
- **Agent Profiles** - Customizable tool sets and system prompts

No API keys required - uses Claude Code's OAuth authentication.

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
- **Slash Commands** - Built-in (`/rewind`, `/help`) and custom commands
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

```bash
# Clone and start
git clone https://github.com/your-username/ai-hub.git
cd ai-hub
docker-compose up -d

# Authenticate with Claude
docker exec -it ai-hub claude login

# Open http://localhost:8000
```

### First-Time Setup

1. Open http://localhost:8000
2. Create your admin account
3. Authenticate Claude in the container (see above)
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
6. Run `docker exec -it ai-hub claude login` after starting

## Troubleshooting

### Claude Not Authenticated
```bash
docker exec -it ai-hub claude login
```

### Check Diagnostics
```bash
curl http://localhost:8000/api/v1/auth/diagnostics
```

### PUID/PGID Issues
- Unraid: `PUID=99`, `PGID=100`
- Standard: `PUID=1000`, `PGID=1000`

## License

MIT License - See [LICENSE](LICENSE) for details.

## Acknowledgments

- [FastAPI](https://fastapi.tiangolo.com/) - Backend framework
- [Svelte](https://svelte.dev/) - Frontend framework
- [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk) - AI integration
- [Anthropic](https://www.anthropic.com/) - Claude AI
