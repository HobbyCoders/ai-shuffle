<!--
title: Overview
description: What is AI Shuffle and what can it do
order: 1
-->

# AI Shuffle

AI Shuffle is a self-hosted workspace for AI agents, made by HobbyCoders. It runs AI agents with your own account from any supported provider: Claude, OpenAI, OpenRouter, xAI Grok or a custom gateway, by subscription sign-in or API key. AI Shuffle includes no AI usage. New here? Start with [Requirements](/docs/getting-started/requirements) and [First run](/docs/getting-started/first-run). It includes a chat interface, connectors for messaging platforms and services, automation pipelines, and a REST API for building integrations.

## Key Features

### Chat Interface
A real-time chat workspace for the model and provider you choose. Features include:
- Streaming responses with tool use visualization
- Session management with search, tags, and an Active group: sessions you're working in stay in Active until you settle them, which moves them to Settled. You can settle or reopen a session from any online worker, even if its execution worker is offline; membership syncs when workers reconnect. This does not stop running work or move the session. Update all workers to use this behavior across the network.
- Session forking to branch conversations
- Git worktree integration for isolated development
- File sharing and media generation
- Model and reasoning effort chosen per session
- Permission modes per session (Ask, Accept Edits, Bypass, and Auto where available)

### Connectors
Connect AI Shuffle to external platforms so an agent can respond to messages from:
- **Messaging:** Telegram, Discord, WhatsApp, Slack, Line, Signal, MS Teams, iMessage, BlueBubbles
- **Smart Home:** Home Assistant, Philips Hue
- **Other:** Spotify, Gmail, Plaid (read-only bank data), Webhooks

Each connector creates a bridge: messages from the platform flow to the agent, and responses flow back.

### Automations
Schedule and trigger AI workflows:
- **Cron triggers** for scheduled tasks
- **Interval triggers** for repeating tasks
- **Webhook triggers** for event-driven execution
- **Connector event triggers** for responding to platform events
- Budget controls, approval gates, and output routing

### Hooks
Run custom bash scripts or webhook calls at key points in the AI workflow:
- Before a tool runs, with the option to block it
- When the agent finishes its turn
- After a tool runs, when a prompt is sent, when a session starts or ends, and at other points, in the background

### MCP Servers
Extend what agents can do by connecting Model Context Protocol (MCP) servers:
- **stdio** servers (subprocess-based)
- **SSE** servers (Server-Sent Events)
- **HTTP** servers
- Global and project-scoped configurations

### Agents
Configurations that control how an agent behaves, on the **Agents** page:
- System prompt and tool permissions
- Durable memory the agent keeps across sessions
- Agents can call other agents (the **Subagents** section), each on the model you pick for it

### Projects
Organize work into project directories:
- File browsing and uploads
- Git integration with branch management
- Worktree support for parallel development
- Per-project MCP servers, agents, hooks and memory

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
              │(15 types)│ │    DB    │ │(stdio/sse)│
              └──────────┘ └──────────┘ └──────────┘
```

- **Frontend:** SvelteKit with Svelte 5, Tailwind CSS, TipTap editor
- **Backend:** FastAPI with WebSocket support, PostgreSQL database
- **Agents:** the Fluid harness runs sessions, streaming and tool execution against the configured provider; Claude sessions run the standard Claude Code CLI with your own sign-in or API key
- **Storage:** PostgreSQL for sessions, message history and settings; your projects stay as ordinary files in the workspace
