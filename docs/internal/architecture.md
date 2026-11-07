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
│  Fluid harness · Provider adapters · Tool Execution       │
└──────────────────────┬───────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│                    Storage Layer                           │
│  PostgreSQL (metadata + transcripts) · File System        │
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
- **Feature Pages** — Each feature (Settings, Profiles, etc.) is a card component rendered in the Studio shell
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
- **PostgreSQL** — Private relational database bundled and supervised by the desktop/Docker runtime, accessed via psycopg2; no external connection setup
- **Fluid** — The in-repo agent harness (`fluid/`), driven through `app/core/provider/harness_adapter.py`; a session either delegates to a provider CLI (such as `claude`) or runs a native loop against a provider API
- **APScheduler** — Cron and interval scheduling for automations
- **WebSocket** — Real-time bidirectional communication

### Key Services

| Service | File | Purpose |
|---------|------|---------|
| `ConnectorManager` | `app/core/connectors/manager.py` | Lifecycle management for all platform connectors |
| `AutomationManager` | `app/core/automation_manager.py` | APScheduler integration, budget enforcement |
| Hook executor | `app/core/hook_executor.py` | Bash and webhook hook execution (message hooks are saved but not delivered) |
| `McpService` | `app/core/mcp_service.py` | MCP server configuration and tool discovery |
| `AuthService` | `app/core/auth.py`, `app/core/owner_auth.py` | Local owner access, account-authorized sessions and scoped API credentials |
| Credential encryption | `app/core/encryption.py`, `app/core/credential_keys.py` | Persistent data key protected by a private server key outside PostgreSQL |

### Request Flow

1. **HTTP/WebSocket request** arrives at FastAPI
2. **Authentication** checks the trusted ingress socket and local peer, or validates a session cookie/API key. Remote owner sessions also require current hub ownership and device authorization.
3. **Router handler** processes the request
4. For chat: the **Fluid harness** (`HarnessAdapter`, built by `app/core/provider/factory.py`) creates/continues a session
5. **Tool execution** happens inside the harness, or inside the provider CLI when the session delegates to one (file ops, bash, MCP tools)
6. **Hooks** (`app/core/hook_service.py`): `PreToolUse` bash hooks can block a tool and `Stop` hooks can keep the turn going; the other seven events run bash and webhook hooks in the background; plugin hooks fire on the events they declare
7. **Response** streams back via WebSocket or SSE

### Database Schema

Key tables:
- `sessions` — Chat sessions with metadata
- `session_events` — Ordered event log for each session
- `fluid_sessions`, `fluid_session_nodes`, `fluid_session_events` — The harness's session tree and event log (`app/core/provider/fluid_pg_store.py`). Each new engine takes the session over by bumping `fluid_sessions.writer_generation`; a replaced engine's later writes are refused inside their own transaction, without an error. Inside one process that takeover never reaches a live turn: each route takes the chat's lease (`app/core/provider/session_admission.py`, keyed on the app session id) before it does anything else and its outermost scope closes it, so a second turn is refused as busy while the first is setting up, connecting, running or saving. A message sent once the turn has announced its end waits for that close rather than being refused. A healthy turn outlives its HTTP reader and saves on a task the lease module owns; a reader that leaves is forgotten at once, and one that falls about 8 MB behind is cut off with an error frame while the turn runs on. If the pump itself abandons an engine iterator at a yielded frame, a finishing task retains admission through draining and persistence. A 15-second deadline bounds only engine reads: it never cancels an app-session write between frames or the saving tail. On an engine-read timeout it fences the originating engine store, evicts only that engine, requests interrupt and closes its transport without stopping session-owned jobs; cancellation of that engine-only reader gets at most one further second. The timed-out iterator cannot deliver more mutation frames or run its persistence/completion tail, and the next request must build a fresh engine. Once the engine finishes normally, saving is shielded from caller, watchdog and finisher cancellation; admission stays held until all issued saving writes settle, including their off-loop threads. The engine-read signal surrounds only the raw Fluid iterator, excluding adapter-side Away receipts and question persistence. All adapter/query/persistence thread dispatch uses the shared tracked executor primitive; a ContextVar carries the exact turn's in-flight tracker, and even cancellation of a thread's caller cannot hide that future. Admission release drains it regardless of the phase signal, including detached saving producers. Saving relies on DB/pool timeouts rather than the engine deadline. A genuinely wedged executor thread has no safe cancellation deadline: the chat intentionally stays busy until recovery or restart rather than admitting a successor under an outstanding write. A replaced runtime may release its own run and deregister itself, but cannot publish a terminal frame or halt its direct-API successor's computer use. A rewind instead takes the lease from a running reply, stops it, and waits for it to save before it claims. Rewinds of one chat run one at a time and finish even if their caller leaves, and turns wait for them, so nothing a rewind does can land after a later rewind or turn. Those waits end in the busy error after 60 seconds, and the chat is not freed by that, because a rewind stuck on a database write can still act when the write returns. Another install on the same database never passes through that lease, so a turn it takes over stops with `SessionReplacedError` instead of ending as a success.
- `agents` — Agent configuration (profile-named functions in `app/db/profiles.py` remain as compatibility names)
- `projects` — Project directory references
- `connectors` — Platform connector configs (encrypted)
- `automations` — Scheduled/triggered workflows
- `project_hooks` — Event handlers (bash, webhook, message)
- `api_users` — API authentication keys
- `tags` — Session organization tags

## Security

- **Encryption** — Stored credentials use a persistent data key wrapped by a private key under `DATA_DIR/.credential-keys`. Adoption retains existing ciphertext and data keys; fresh installations generate random keys. Key backups are required alongside PostgreSQL backups.
- **Authentication** — Trusted local/LAN owner access on the local listener; account-authorized cookie sessions on protected remote ingress; scoped Bearer tokens for API users. Local setup requires no account or password.
- **Authorization** — Admin-only endpoints, API user scoping by project/profile
- **CORS** — Configurable origin restrictions
- **Input Validation** — Pydantic models for all request/response schemas
