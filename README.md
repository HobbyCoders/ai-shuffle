# AI Shuffle

> **A web UI for Claude Code** — Run locally or self-host for remote access.

---

## What Is It?

AI Shuffle wraps Claude Code in a browser-based interface. Same AI coding assistant, but with a proper GUI instead of terminal-only interaction.

**Run it two ways:**
- **Local** — Replace Claude Code CLI on your desktop (`localhost:8000`)
- **Self-hosted** — Docker on your server/NAS for access from anywhere

---

## Why Use This Over Claude Code CLI?

| | Claude Code CLI | AI Shuffle |
|---|---|---|
| **Interface** | Terminal | Browser UI |
| **Sessions** | One at a time | Multi-tab, persistent history |
| **History** | `.jsonl` files | Searchable, tagged, exportable |
| **Remote access** | Needs SSH | Just a URL |
| **Mobile** | Not practical | Fully responsive |
| **Customization** | Edit config files | Visual profile builder |

---

## Key Features

### Custom Profiles
Build specialized AI agents with custom system prompts, tool permissions, and auto-accept patterns. Create a code reviewer that only reads, or an autonomous PR bot that commits and pushes.

### Multi-Session Workflow
Work on multiple problems in tabs. Fork sessions, rewind to any point, search history.

### Thinking Mode & Permission Overrides
Toggle extended thinking and permission modes per-session without changing your profile.

### Built-in Creative Tools
Image generation (Gemini, Imagen, GPT-Image), video generation (Veo, Sora), 3D models (Meshy).

### Self-Host Security
2FA, rate limiting, session management — designed to be exposed to the internet safely.

---

## Quick Start

### Local (Desktop)

```bash
# Download release from GitHub Releases, or clone:
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle

# Setup
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cd frontend && npm install && cd ..

# Configure
cp .env.example .env
# Set LOCAL_MODE=true in .env

# Run
python run_local.py
```

Open `http://localhost:8000`. Uses your existing `claude login` credentials.

### Docker (Self-Hosted)

```bash
# Using pre-built image
docker pull ghcr.io/hobbycoders/ai-shuffle:latest
docker-compose up -d

# Authenticate
docker exec -it ai-shuffle claude login
```

---

## Tech Stack

- **Frontend:** SvelteKit, Svelte 5, Tailwind CSS
- **Backend:** FastAPI, WebSockets, SQLite
- **AI:** Claude Agent SDK

---

## License

MIT — see [LICENSE](LICENSE)
