# AI Hub - Claude Code Web Interface

A full-featured web interface and API for Claude Code, providing a Claude.ai-like experience with agent profiles, session management, and streaming conversations.

## Features

### Web Interface
- Modern, responsive chat interface built with Svelte
- Real-time SSE streaming for Claude responses
- Tool use visualization (file reads, code edits, bash commands)
- Profile selector for different agent configurations
- Session history and persistence
- Dark theme optimized for coding

### Agent Profiles
Pre-configured agent profiles with different capabilities:

| Profile | Description | Tools |
|---------|-------------|-------|
| **Simple Chat** | Text-only responses | None |
| **Code Reader** | Read-only code analysis | Read, Glob, Grep |
| **Code Writer** | File modifications | Read, Write, Edit, Glob, Grep |
| **Full Claude** | Complete Claude Code | All tools |
| **Data Extractor** | JSON output extraction | WebFetch |
| **Researcher** | Web search and analysis | Read, Glob, Grep, WebFetch, WebSearch |

### API Features
- RESTful API with OpenAPI documentation
- SSE streaming for real-time responses
- Session management with conversation history
- Project/workspace isolation
- Usage tracking and statistics
- Legacy API compatibility

## Quick Start

### Docker Compose (Recommended)

```bash
# Clone the repository
git clone https://github.com/your-username/ai-hub.git
cd ai-hub

# Start the container
docker-compose up -d

# Authenticate with Claude
docker exec -it ai-hub claude login

# Access the web UI
open http://localhost:8000
```

### First-Time Setup

1. Open http://localhost:8000
2. Create your admin account (first-time only)
3. Start chatting with Claude!

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Web Browser                             │
│                    (Svelte SPA)                              │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP/SSE
┌──────────────────────────▼──────────────────────────────────┐
│                    FastAPI Server                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   Auth      │  │   Query     │  │   Session   │         │
│  │   Service   │  │   Engine    │  │   Manager   │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│                           │                                  │
│  ┌─────────────┐  ┌───────▼─────┐  ┌─────────────┐         │
│  │   Profile   │  │   Claude    │  │   SQLite    │         │
│  │   Service   │  │   Agent SDK │  │   Database  │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                  Claude Code CLI                             │
│              (OAuth authenticated)                           │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                   Anthropic API                              │
└─────────────────────────────────────────────────────────────┘
```

## API Endpoints

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/auth/status` | Check authentication status |
| POST | `/api/v1/auth/setup` | Create admin account (first-run) |
| POST | `/api/v1/auth/login` | Login |
| POST | `/api/v1/auth/logout` | Logout |

### Profiles
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/profiles` | List all profiles |
| GET | `/api/v1/profiles/:id` | Get profile details |
| POST | `/api/v1/profiles` | Create custom profile |
| PUT | `/api/v1/profiles/:id` | Update profile |
| DELETE | `/api/v1/profiles/:id` | Delete profile |

### Sessions
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/sessions` | List sessions |
| GET | `/api/v1/sessions/:id` | Get session with history |
| DELETE | `/api/v1/sessions/:id` | Delete session |

### Query (AI Interface)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/query` | One-shot query |
| POST | `/api/v1/query/stream` | Streaming query (SSE) |
| POST | `/api/v1/conversation` | Multi-turn conversation |
| POST | `/api/v1/conversation/stream` | Streaming conversation (SSE) |

### System
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/version` | Version info |
| GET | `/api/v1/stats` | Usage statistics |

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Server host | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `SERVICE_NAME` | Service name | `ai-hub` |
| `LOG_LEVEL` | Logging level | `info` |
| `PUID` | Process user ID | `1000` |
| `PGID` | Process group ID | `1000` |
| `WORKSPACE_DIR` | Project workspace | `./workspace` |

### Docker Volumes

| Volume | Path | Description |
|--------|------|-------------|
| `claude-auth` | `/home/appuser/.claude` | OAuth credentials |
| `ai-hub-data` | `/data` | SQLite database & sessions |
| `workspace` | `/workspace` | Project files |

## Development

### Project Structure

```
ai-hub/
├── app/
│   ├── api/           # FastAPI routes
│   │   ├── auth.py
│   │   ├── profiles.py
│   │   ├── projects.py
│   │   ├── sessions.py
│   │   ├── query.py
│   │   └── system.py
│   ├── core/          # Business logic
│   │   ├── auth.py
│   │   ├── config.py
│   │   ├── models.py
│   │   ├── profiles.py
│   │   └── query_engine.py
│   ├── db/            # Database
│   │   └── database.py
│   ├── static/        # Built frontend
│   └── main.py        # Application entry
├── frontend/          # Svelte SPA
│   ├── src/
│   │   ├── lib/
│   │   │   ├── api/
│   │   │   └── stores/
│   │   └── routes/
│   └── package.json
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

### Local Development

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
# Build Docker image
docker-compose build

# Build frontend only
cd frontend
npm run build
```

## Unraid Installation

### Using Docker Template

1. Go to **Docker** tab in Unraid
2. Click **Add Container**
3. Configure:
   - **Repository**: `ghcr.io/your-username/ai-hub:latest`
   - **PUID**: `99`
   - **PGID**: `100`
   - **Port**: `8000`
4. Add volumes for persistence

### Post-Installation

```bash
# Authenticate with Claude
docker exec -it ai-hub claude login
```

## Troubleshooting

### Authentication Issues

```bash
# Check diagnostics
curl http://localhost:8000/api/v1/auth/diagnostics

# Re-authenticate
docker exec -it ai-hub claude login
```

### Common Issues

1. **PUID/PGID Mismatch**: Set `PUID=99` and `PGID=100` for Unraid
2. **Claude not authenticated**: Run `claude login` in container
3. **Port in use**: Change `PORT` in `.env`

## Legacy API Support

The following legacy endpoints are maintained for backward compatibility:

- `POST /chat` → maps to `/api/v1/query`
- `POST /conversation` → maps to `/api/v1/conversation`
- `GET /auth/status` → maps to `/api/v1/auth/claude/status`

## License

MIT License - See LICENSE file for details.

## Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/) and [Svelte](https://svelte.dev/)
- Powered by [Anthropic Claude](https://www.anthropic.com/) via Claude Code CLI
- Uses [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk)
