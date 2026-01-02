# AI Shuffle

> Your AI Workspace Awaits

A sophisticated Claude Code web interface and workspace application that provides a rich, desktop-like experience for interacting with Claude AI.

![AI Shuffle Demo](docs/images/ai-shuffle-demo.gif)

## Overview

AI Shuffle is a full-stack application that bridges Claude's capabilities with a desktop-style workspace interface. It features a unique card-based UI system called "The Deck" that allows you to organize multiple tools and conversations in a flexible, intuitive layout.

## Features

### Chat & Conversation
- **Multi-tab Chat System** - Independent chat sessions with WebSocket connections
- **Profiles & Projects** - Context-aware configurations for different AI workflows
- **Execution Modes** - Local execution and worktree-based execution for safer AI operations
- **Token Tracking** - Real-time token counting and cost analysis
- **Session Management** - Save, fork, export, tag, and search chat sessions

### The Deck (Card-based Workspace)
A novel window-management system with draggable, resizable cards:
- **Card Types**: Chat, Terminal, Settings, Profile, Project, Subagent, Image Studio, Model Studio, Audio Studio, File Browser, Plugins
- **Layout Modes**: Freeflow, Side-by-side, Tile, Stack, Focus
- **Mobile Support**: Swipe-based navigation on small screens

### AI Agents & Automation
- **Subagents** - Task-oriented AI agents that can be spawned for specific work
- **Agent Engine** - Built on Claude Agent SDK for autonomous tool use
- **Tool Integration** - System commands, file operations, Git commands, and more
- **Plugin System** - Extensible architecture for custom tools and integrations

### Workspace & Project Management
- **File Browser** - Navigate and edit project files
- **Git Integration** - Repository info, branches, worktrees, diffs
- **GitHub Integration** - Pull requests, issues, authentication
- **Project Context** - Automatic workspace/repository binding

### Creative & Media Tools
- **Image Generation & Editing** - Image studio with multiple providers
- **Video Generation** - Text-to-video capabilities
- **3D Model Generation** - Meshy integration for 3D model creation
- **Audio Processing** - Text-to-speech, speech-to-text transcription

### Analytics & Monitoring
- **Usage Analytics** - Token counts, cost breakdown by profile/date
- **Session Analytics** - Session duration, token usage trends
- **Performance Monitoring** - WebSocket connection status, API health

## Tech Stack

### Frontend
- **Svelte 5** + SvelteKit
- **Tailwind CSS** for styling
- **Vite** for fast builds
- **xterm.js** for terminal emulation
- **TypeScript** for type safety

### Backend
- **FastAPI** (Python async web framework)
- **WebSockets** for real-time communication
- **SQLite** database
- **Claude Agent SDK** for AI integration

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 20+
- Claude API access (OAuth or API key)

### Local Setup

1. Clone the repository:
```bash
git clone https://github.com/yourusername/ai-shuffle.git
cd ai-shuffle
```

2. Install backend dependencies:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

3. Install frontend dependencies:
```bash
cd frontend
npm install
```

4. Configure environment:
```bash
cp .env.example .env
# Edit .env with your settings
```

5. Run the application:
```bash
# Terminal 1: Backend
python -m uvicorn app.main:app --reload --port 8000

# Terminal 2: Frontend (development)
cd frontend
npm run dev
```

### Docker Setup

```bash
docker-compose up -d
```

The application will be available at `http://localhost:8000`

## Configuration

Key environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Claude API key (optional with OAuth) | - |
| `DATABASE_URL` | SQLite database path | `sqlite:///./data/ai_shuffle.db` |
| `SECRET_KEY` | Session encryption key | Generated |
| `ENABLE_2FA` | Enable two-factor authentication | `false` |

See `.env.example` for all available options.

## Architecture

```
ai-shuffle/
├── app/                    # Backend (FastAPI)
│   ├── api/               # API routes
│   ├── core/              # Business logic
│   └── database.py        # Database models
├── frontend/              # Frontend (SvelteKit)
│   ├── src/
│   │   ├── lib/          # Components & utilities
│   │   └── routes/       # Pages
│   └── static/           # Static assets
├── agents/                # Subagent definitions
├── plugins/               # Plugin system
└── docs/                  # Documentation
```

## Usage

### Starting a Chat
1. Click the **Chat** card from the landing page
2. Select a profile and project (optional)
3. Start typing your message to Claude

### Managing Cards
- Click the floating menu button to open the card deck
- Select any card type to add it to your workspace
- Drag cards to reposition them
- Use the navigation arrows to switch between cards

### File Browser
1. Open the **Files** card
2. Select a project from the dropdown
3. Browse, upload, or manage project files

### Keyboard Shortcuts
- `Cmd/Ctrl + K` - Spotlight search
- `Cmd/Ctrl + N` - New chat
- `Cmd/Ctrl + /` - Keyboard shortcuts help

## Contributing

Contributions are welcome! Please read our contributing guidelines before submitting PRs.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built with [Claude](https://anthropic.com/claude) by Anthropic
- UI inspired by modern desktop workspace applications
