# AI Shuffle

**Build your own AI agent workspace. Configure Claude exactly how you need it—then use it from the web UI or call it programmatically.**

Self-hosted. With or without API keys. Full Claude Code power.

![Version](https://img.shields.io/badge/version-4.0.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What is AI Shuffle?

AI Shuffle is a **configurable AI agent platform** built on Claude Code. Create custom agent profiles, define their tools and behaviors, then interact with them through a polished web interface or integrate them into your applications via API.

**Two ways to work:**

| Mode | Description |
|------|-------------|
| **Interactive (UI)** | Chat with agents in "The Deck"—a desktop-like workspace with draggable cards |
| **Programmatic (API)** | Call your configured agents from scripts, apps, or automation workflows |

Same agents. Same configurations. Choose your interface.

---

## Build Your Agent Workspace

The core of AI Shuffle is **agent configuration**. Define exactly how Claude should behave for different tasks.

### Agent Profiles

Create specialized agents for different jobs:

```
┌─────────────────────────────────────────────────────────────────┐
│  PROFILE: Code Reviewer                                          │
├─────────────────────────────────────────────────────────────────┤
│  Model:        Claude Sonnet                                     │
│  System:       "You are a senior engineer. Be direct and        │
│                 critical. Focus on bugs, security, and perf."   │
│  Tools:        Read, Grep, Glob (no Write, no Bash)             │
│  Permissions:  Auto-accept all                                   │
│  Background:   No (interactive)                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  PROFILE: Full-Stack Developer                                   │
├─────────────────────────────────────────────────────────────────┤
│  Model:        Claude Opus                                       │
│  System:       "Expert in React, Python, PostgreSQL.            │
│                 Write clean, tested, documented code."          │
│  Tools:        All tools enabled                                 │
│  Permissions:  Ask before destructive actions                    │
│  Background:   Optional (can run autonomously with worktrees)    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  PROFILE: Research Assistant                                     │
├─────────────────────────────────────────────────────────────────┤
│  Model:        Claude Haiku (fast, cheap)                        │
│  System:       "Summarize findings concisely. Cite sources."    │
│  Tools:        WebSearch, WebFetch, Read                         │
│  Permissions:  Auto-accept all                                   │
│  Background:   No (interactive)                                  │
└─────────────────────────────────────────────────────────────────┘
```

### What You Can Configure

| Setting | Options | Purpose |
|---------|---------|---------|
| **Model** | Sonnet, Sonnet 1M, Opus, Haiku | Balance speed, cost, and capability |
| **System Prompt** | Custom instructions | Define personality, expertise, constraints |
| **Tools** | Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, etc. | Control what actions the agent can take |
| **Permission Mode** | Ask, Auto-accept, Bypass | How to handle tool confirmations |
| **Project Scope** | Specific directories | Limit agent access to certain codebases |
| **Background Mode** | Enable/disable | Run agent autonomously in a git worktree |
| **Worktree/Branching** | Auto-branch, auto-PR | Isolate agent work on separate branches |

### Background Agents via Configuration

Want an agent that works autonomously? Configure it in the profile:

- **Enable background mode** — Agent runs without waiting for your input
- **Auto-branch** — Automatically creates a feature branch for the work
- **Worktree isolation** — Agent works in a separate git worktree, keeping your main branch clean
- **Auto-PR** — Creates a pull request when the task completes

This means you control *how* agents work through configuration—interactive assistants, autonomous workers, or anything in between.

### Use Profiles Two Ways

**In the UI:** Select a profile from the dropdown and chat. Switch profiles mid-conversation.

**Via API:** Specify the profile when making requests:

```bash
curl -X POST http://localhost:8000/api/v1/query/stream \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Review this PR for security issues",
    "profile_id": "code-reviewer",
    "project_id": "my-app"
  }'
```

---

## AI Service Integrations

AI Shuffle integrates with multiple AI providers for enhanced capabilities directly in chat.

### Text-to-Speech (TTS)

Configure OpenAI for voice output:

| Provider | Models | Use Case |
|----------|--------|----------|
| **OpenAI** | tts-1, tts-1-hd | Read responses aloud, voice assistants |

### Image Generation

Generate images directly in chat conversations:

| Provider | Models | Best For |
|----------|--------|----------|
| **Google Gemini** | Nano Banana | Fast iteration, editing, reference images |
| **Google Imagen** | Imagen 4 | Highest quality, photo-realism |
| **OpenAI** | GPT Image | Accurate text in images, inpainting |

### 3D Model Generation

Create 3D models from text or images:

| Provider | Capabilities | Use Case |
|----------|--------------|----------|
| **Meshy AI** | Text-to-3D, Image-to-3D, Retexturing, Rigging, Animation | Game assets, prototyping, visualization |

### Video Generation

Generate and analyze video content:

| Provider | Models | Features |
|----------|--------|----------|
| **Google Veo** | Veo 2, Veo 3 | Video extension, frame bridging, native audio |
| **OpenAI Sora** | Sora | Fast generation, up to 12 seconds |

### Configuration

Add your API keys in Settings to enable these integrations:

```
Settings → AI Services → Configure API Keys
├── OpenAI API Key (for TTS, GPT Image, Sora)
├── Google AI API Key (for Gemini, Imagen, Veo)
└── Meshy API Key (for 3D generation)
```

Once configured, Claude can use these services directly in your conversations.

---

## Programmatic Agents (API)

Build AI into your workflows. Every agent you configure in AI Shuffle is accessible via REST API.

### API Features

- **OpenAI-compatible format** — Easy to integrate with existing tools
- **Streaming (SSE)** — Real-time token-by-token responses
- **Profile binding** — API users can be locked to specific profiles
- **Project scoping** — Restrict which codebases an API key can access
- **Usage tracking** — Monitor tokens and costs per API user

### Example: Automated Code Review

```python
import httpx

def review_pr(pr_diff: str) -> str:
    """Run automated code review using the configured agent."""
    response = httpx.post(
        "http://localhost:8000/api/v1/query",
        headers={"Authorization": "Bearer aih_reviewer_key"},
        json={
            "prompt": f"Review this diff for bugs and security issues:\n\n{pr_diff}",
            "profile_id": "code-reviewer"
        }
    )
    return response.json()["response"]
```

### Example: Streaming Response

```python
import httpx

def stream_response(prompt: str, profile_id: str):
    """Stream a response token by token."""
    with httpx.stream(
        "POST",
        "http://localhost:8000/api/v1/query/stream",
        headers={"Authorization": "Bearer aih_your_key"},
        json={"prompt": prompt, "profile_id": profile_id}
    ) as response:
        for chunk in response.iter_text():
            print(chunk, end="", flush=True)
```

### API User Management

Create API users with specific permissions:

| Setting | Description |
|---------|-------------|
| **Allowed Profiles** | Which agent profiles this key can use |
| **Allowed Projects** | Which codebases this key can access |
| **Rate Limits** | Requests per minute/hour |
| **Usage Quotas** | Maximum tokens per day/month |

---

## The Deck Interface

When you want to work interactively, AI Shuffle provides "The Deck"—a desktop-like workspace.

```
┌─────────────────────────────────────────────────────────────────┐
│ [Rail]           [Workspace]                      [Context]     │
│ ┌─────┐ ┌─────────────────────────────────────┐ ┌─────────────┐ │
│ │     │ │  ┌──────────────┐  ┌─────────────┐  │ │ Sessions    │ │
│ │ 🖥️  │ │  │  Chat Card   │  │  Chat Card  │  │ │ ├─ Chat 1   │ │
│ │     │ │  │  (draggable) │  │ (draggable) │  │ │ └─ Chat 2   │ │
│ │     │ │  └──────────────┘  └─────────────┘  │ │             │ │
│ │     │ │                                     │ │ Projects    │ │
│ │     │ │     ┌─────────────────────┐         │ │ ├─ my-app   │ │
│ │     │ │     │    Chat Card        │         │ │ └─ api      │ │
│ │     │ │     │    (resizable)      │         │ │             │ │
│ │ ⚙️  │ │     └─────────────────────┘         │ │ Profiles    │ │
│ └─────┘ └─────────────────────────────────────┘ └─────────────┘ │
│ RAIL              WORKSPACE (free-form)         CONTEXT PANEL   │
│         ┌───────────────────────────────────────────────────┐   │
│         │ [minimized cards]                                 │   │
│         └───────────────────────────────────────────────────┘   │
│                              DOCK                               │
└─────────────────────────────────────────────────────────────────┘
```

### Card Behaviors

Cards act like windows on a desktop:

- **Drag anywhere** — Position cards freely in the workspace
- **Resize** — Drag edges and corners to adjust size
- **Snap** — Drag to edges to snap to half-screen or quarters
- **Minimize** — Collapse to the dock at the bottom
- **Maximize** — Double-click title bar to fill workspace
- **Z-order** — Click to bring a card to front

### Mobile Experience

On mobile, the workspace transforms into a swipeable card stack with full-screen cards and dot indicators.

---

## Why Self-Host?

| Benefit | Description |
|---------|-------------|
| **Flexible auth** | Use Claude Code's OAuth (no API keys) or bring your own Anthropic API key |
| **Full tool access** | File operations, bash, code edits, web search |
| **Your infrastructure** | Runs on your hardware, your network |
| **Multi-user** | Share one instance across your team |
| **Cost visibility** | Track usage per user, per session, per project |
| **Customization** | Configure agents exactly how you need them |

---

## Quick Start

### Docker (Recommended)

```bash
docker pull ghcr.io/quickkill0/ai-shuffle:latest
docker run -d \
  -p 8000:8000 \
  -v ai-shuffle-data:/data \
  -v ai-shuffle-claude:/home/appuser/.claude \
  ghcr.io/quickkill0/ai-shuffle:latest
```

### Docker Compose

```yaml
version: '3.8'
services:
  ai-shuffle:
    image: ghcr.io/quickkill0/ai-shuffle:latest
    ports:
      - "8000:8000"
    volumes:
      - ai-shuffle-data:/data
      - ai-shuffle-claude:/home/appuser/.claude
      - ./workspace:/workspace
    environment:
      - PUID=1000
      - PGID=1000

volumes:
  ai-shuffle-data:
  ai-shuffle-claude:
```

### First-Time Setup

1. Open `http://localhost:8000`
2. Create your admin account
3. Go to **Settings** → Authenticate with Claude
4. (Optional) Add API keys for OpenAI, Google AI, Meshy
5. Create your first **Profile** with custom system prompt and tools
6. Start chatting or generate an API key

---

## Local Development

Run directly on your machine without Docker:

### Prerequisites

- Python 3.11+
- Node.js 20+
- Claude Code CLI (installed and authenticated)

### Backend

```bash
pip install -r requirements.txt
python -m app.main
```

### Frontend

```bash
cd frontend
npm install
npm run dev      # Development
npm run build    # Production
```

---

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

### Docker Volumes

| Volume | Container Path | Purpose |
|--------|----------------|---------|
| `ai-shuffle-data` | `/data` | Database, settings |
| `ai-shuffle-claude` | `/home/appuser/.claude` | Claude OAuth credentials |
| `workspace` | `/workspace` | Your project files |

---

## API Reference

Full interactive docs at `/docs` when running.

### Core Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/query` | One-shot query with profile |
| POST | `/api/v1/query/stream` | Streaming query (SSE) |
| POST | `/api/v1/conversation` | Multi-turn conversation |
| POST | `/api/v1/conversation/stream` | Streaming conversation |

### Profile Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/profiles` | List all profiles |
| POST | `/api/v1/profiles` | Create profile |
| PUT | `/api/v1/profiles/:id` | Update profile |
| DELETE | `/api/v1/profiles/:id` | Delete profile |

### API Users

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/api-users` | List API users |
| POST | `/api/v1/api-users` | Create API user with permissions |
| PUT | `/api/v1/api-users/:id` | Update permissions |
| DELETE | `/api/v1/api-users/:id` | Revoke API user |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Web Browser / API Client                  │
│              "The Deck" UI  |  REST API  |  WebSocket        │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                      FastAPI Server                          │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐   │
│  │  Profile  │ │   Query   │ │    AI     │ │    API    │   │
│  │  Manager  │ │  Engine   │ │ Services  │ │   Users   │   │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘   │
│                        │                                     │
│  ┌───────────┐ ┌───────▼───┐ ┌───────────┐ ┌───────────┐   │
│  │  Project  │ │  Claude   │ │  SQLite   │ │   Sync    │   │
│  │  Scoping  │ │ Agent SDK │ │    DB     │ │  Engine   │   │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│               Claude Code CLI (OAuth authenticated)          │
│         + OpenAI API | Google AI API | Meshy API             │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                      Anthropic API                           │
└─────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology |
|-------|------------|
| **Frontend** | Svelte 5, SvelteKit, TypeScript, Tailwind CSS |
| **Backend** | Python 3.11, FastAPI, Claude Agent SDK |
| **Database** | SQLite |
| **Container** | Docker, Claude Code CLI, GitHub CLI |
| **AI Services** | OpenAI, Google AI, Meshy (optional) |

---

## Use Cases

### For Developers

- **Code review agents** — Automated PR feedback with custom style guidelines
- **Documentation agents** — Generate and maintain docs from code
- **Test generation** — Create test suites for existing code
- **Refactoring assistants** — Safely modernize legacy codebases

### For Teams

- **Shared agent library** — Everyone uses the same configured profiles
- **Specialized experts** — Frontend agent, backend agent, DevOps agent
- **Onboarding** — New devs get AI assistance with team conventions

### For Automation

- **CI/CD integration** — Run agents on PR events via API
- **Scheduled tasks** — Nightly code health checks
- **Webhook triggers** — React to external events with AI agents

---

## Deployment

### Unraid

1. **Docker** tab → **Add Container**
2. Repository: `ghcr.io/quickkill0/ai-shuffle:latest`
3. `PUID=99`, `PGID=100`
4. Port `8000`
5. Add volumes, start, authenticate

### Reverse Proxy (nginx)

```nginx
server {
    listen 443 ssl;
    server_name ai.yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
```

---

## Troubleshooting

### Claude Not Authenticated

```bash
docker exec -it ai-shuffle claude login
```

Or use the Settings page in the web interface.

### Check Diagnostics

```bash
curl http://localhost:8000/api/v1/auth/diagnostics
```

### PUID/PGID

| Platform | PUID | PGID |
|----------|------|------|
| Unraid | 99 | 100 |
| Linux | 1000 | 1000 |

---

## Experimental Notice

**This is experimental software under active development.**

- Expect bugs and breaking changes
- Solo developer project
- Use at your own risk

---

## License

MIT License — see [LICENSE](LICENSE)

### Claude Code CLI

Uses Claude Code CLI, subject to [Anthropic's Terms](https://www.anthropic.com/legal/consumer-terms) and [Usage Policy](https://www.anthropic.com/legal/aup).

---

## Acknowledgments

- [Anthropic](https://www.anthropic.com/) — Claude AI
- [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk) — Official SDK
