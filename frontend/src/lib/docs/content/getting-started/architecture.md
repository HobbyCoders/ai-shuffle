<!--
title: Architecture
description: Technical architecture and design decisions
order: 4
-->

# Architecture

## System Overview

AI Shuffle is a three-tier application:

```
┌──────────────────────────────────────────────────────────┐
│                      Client Layer                         │
│  SvelteKit SPA · WebSocket · REST API consumers          │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│                    Application Layer                       │
│  FastAPI · WebSocket handlers · Connector Manager         │
│  Automation Scheduler · Hook Executor · MCP Service       │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│                      AI Layer                             │
│  Claude Agent SDK · Session Management · Tool Execution   │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│                    Storage Layer                           │
│  PostgreSQL (metadata) · JSONL (messages) · File System   │
└──────────────────────────────────────────────────────────┘
```

## Frontend Architecture

### Technology Stack
- **SvelteKit** — Full-stack framework for the SPA
- **Svelte 5** — UI framework with runes reactivity (`$state`, `$derived`, `$effect`)
- **Tailwind CSS** — Utility-first styling with custom design tokens
- **TipTap** — Rich text editor for message input
- **Lucide** — Icon library

### Key Patterns
- **Layout Store** — Central state for sidebar, active view, and navigation
- **Active Session Store** — Manages current chat session and streaming state
- **Feature Pages** — Each feature (Settings, Profiles, etc.) is a card component using `FeaturePageLayout`
- **WebSocket** — Real-time chat streaming and message sync
- **Card Design System** — 2000+ lines of consistent component CSS tokens

### State Management
```
layout store      → sidebar state, active view, navigation
active-session    → current session, messages, streaming
app-data          → sessions list, projects, profiles
auth              → authentication state, admin status
```

## Backend Architecture

### Technology Stack
- **FastAPI** — Async Python web framework
- **PostgreSQL** — Relational database via psycopg2
- **Claude Agent SDK** — Anthropic's Python SDK for AI agents
- **APScheduler** — Cron and interval scheduling for automations
- **WebSocket** — Real-time bidirectional communication

### Key Services

| Service | File | Purpose |
|---------|------|---------|
| `ConnectorManager` | `app/core/connectors/manager.py` | Lifecycle management for all platform connectors |
| `AutomationManager` | `app/core/automation_manager.py` | APScheduler integration, budget enforcement |
| `HookExecutor` | `app/core/hook_executor.py` | Bash and webhook hook execution |
| `MCPService` | `app/core/mcp_service.py` | MCP server configuration and tool discovery |
| `AuthService` | `app/core/auth.py` | Admin auth, API keys |
| `EncryptionService` | `app/core/encryption.py` | Password-based encryption for secrets |

### Request Flow

1. **HTTP/WebSocket request** arrives at FastAPI
2. **Authentication middleware** validates session cookie or API key
3. **Router handler** processes the request
4. For chat: **Claude Agent SDK** creates/continues a session
5. **Tool execution** happens within the SDK (file ops, bash, MCP tools)
6. **Hooks** fire at appropriate points (pre/post tool use, etc.)
7. **Response** streams back via WebSocket or SSE

### Database Schema

Key tables:
- `sessions` — Chat sessions with metadata
- `profiles` — Agent configuration profiles
- `projects` — Project directory references
- `connectors` — Platform connector configs (encrypted)
- `automations` — Scheduled/triggered workflows
- `hooks` — Event handlers (bash/webhook)
- `api_users` — API authentication keys
- `tags` — Session organization tags

## Security

- **Encryption** — All sensitive fields (API keys, tokens) encrypted at rest with password-derived key
- **Authentication** — Cookie-based sessions for admin, Bearer tokens for API users
- **Authorization** — Admin-only endpoints, API user scoping by project/profile
- **Rate Limiting** — Per-IP and per-user request limits
- **CORS** — Configurable origin restrictions
- **Input Validation** — Pydantic models for all request/response schemas
