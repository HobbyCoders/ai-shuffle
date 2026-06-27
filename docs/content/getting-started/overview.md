<!--
title: Overview
description: What is AI Shuffle and what can it do
order: 1
-->

# AI Shuffle

AI Shuffle is a self-hosted web interface for Claude Code — Anthropic's AI coding assistant. It provides a rich chat UI, multi-platform connectors, automation pipelines, and a full REST API for building integrations.

## Key Features

### Chat Interface
A real-time chat workspace powered by Claude via the Agent SDK. Features include:
- Streaming responses with tool use visualization
- Session management with search, tags, and favorites
- Session forking to branch conversations
- Git worktree integration for isolated development
- File sharing and media generation
- Thinking mode control (default, think, think harder, ultra think)

### Connectors
Connect AI Shuffle to external platforms so Claude can respond to messages from:
- **Messaging:** Telegram, Discord, WhatsApp, Slack, Line, Signal, MS Teams, iMessage, BlueBubbles
- **Smart Home:** Home Assistant, Philips Hue
- **Other:** Spotify, Gmail, Webhooks

Each connector creates a bridge — messages from the platform flow into Claude, and responses flow back.

### Automations
Schedule and trigger AI workflows:
- **Cron triggers** for scheduled tasks
- **Interval triggers** for repeating tasks
- **Webhook triggers** for event-driven execution
- **Connector event triggers** for responding to platform events
- Budget controls, approval gates, and output routing

### Hooks
Execute custom bash scripts or webhook calls at key points in the AI workflow:
- Before/after tool use
- Session start/end
- On user prompt submission
- On notifications

### MCP Servers
Extend Claude's capabilities by connecting Model Context Protocol (MCP) servers:
- **stdio** servers (subprocess-based)
- **SSE** servers (Server-Sent Events)
- **HTTP** servers
- Global and project-scoped configurations

### Subagents
Define specialized AI agents for specific tasks:
- Custom system prompts and tool restrictions
- Model selection (Haiku, Sonnet, Opus)
- Per-profile agent enablement

### Profiles
Configuration containers that control Claude's behavior:
- System prompt and tool permissions
- Model selection and thinking mode
- MCP servers, plugins, and subagents
- Execution mode (default, accept edits, plan, bypass)

### Projects
Organize work into project directories:
- File browsing and uploads
- Git integration with branch management
- Worktree support for parallel development
- Per-project MCP and plugin configuration

### API
Full REST API for external integrations:
- One-shot queries and stateful conversations
- Session management and search
- Connector, automation, and hook management
- API key authentication for programmatic access

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   SvelteKit UI  │────▶│   FastAPI Backend │────▶│  Claude Agent   │
│   (Frontend)    │◀────│   (WebSocket/REST)│◀────│  SDK (Python)   │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │Connectors│ │PostgreSQL│ │MCP Servers│
              │(14 types)│ │    DB    │ │(stdio/sse)│
              └──────────┘ └──────────┘ └──────────┘
```

- **Frontend:** SvelteKit with Svelte 5, Tailwind CSS, TipTap editor
- **Backend:** FastAPI with WebSocket support, PostgreSQL database
- **AI:** Claude Agent SDK for Python — manages sessions, streaming, tool execution
- **Storage:** PostgreSQL for metadata, JSONL files for message history
