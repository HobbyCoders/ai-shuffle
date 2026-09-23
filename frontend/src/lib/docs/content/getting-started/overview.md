<!--
title: Overview
description: What is AI Shuffle and what can it do
order: 1
-->

# AI Shuffle

AI Shuffle is a self-hosted workspace for AI agents, made by HobbyCoders. It runs Claude Code, and it can also run models from other providers. You bring your own AI account or API key; AI Shuffle includes no AI usage. New here? Start with [Requirements](/docs/getting-started/requirements) and [First run](/docs/getting-started/first-run). It includes a chat interface, connectors for messaging platforms and services, automation pipelines, and a REST API for building integrations.

## Key Features

### Chat Interface
A real-time chat workspace for the model and provider you choose. Features include:
- Streaming responses with tool use visualization
- Session management with search, tags, and an Active group: sessions you're working in stay in Active until you mark them Complete, which moves them to Recent
- Session forking to branch conversations
- Git worktree integration for isolated development
- File sharing and media generation
- Thinking mode control (default, think, think harder, ultra think)

### Connectors
Connect AI Shuffle to external platforms so an agent can respond to messages from:
- **Messaging:** Telegram, Discord, WhatsApp, Slack, Line, Signal, MS Teams, iMessage, BlueBubbles
- **Smart Home:** Home Assistant, Philips Hue
- **Other:** Spotify, Gmail, Webhooks

Each connector creates a bridge: messages from the platform flow to the agent, and responses flow back.

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
Extend what agents can do by connecting Model Context Protocol (MCP) servers:
- **stdio** servers (subprocess-based)
- **SSE** servers (Server-Sent Events)
- **HTTP** servers
- Global and project-scoped configurations

### Subagents
Define specialized AI agents for specific tasks:
- Custom system prompts and tool restrictions
- Model selection from the providers you set up
- Per-profile agent enablement

### Profiles
Configuration containers that control an agent's behavior:
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
│   SvelteKit UI  │────▶│   FastAPI Backend │────▶│  Fluid harness  │
│   (Frontend)    │◀────│   (WebSocket/REST)│◀────│  and providers  │
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
- **Agents:** the Fluid harness runs sessions, streaming and tool execution against the configured provider; Claude Code sessions run the standard Claude Code CLI with your own sign-in or API key
- **Storage:** PostgreSQL for metadata, JSONL files for message history
