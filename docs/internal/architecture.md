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
- `session_events` — Ordered event log for each session. Standalone lifecycle writes retry only PostgreSQL-confirmed lock-timeout rollbacks, within the recorder's existing bounded recovery window; uncertain commits are never replayed. Interaction and settlement events share the recorder's ordered, cancellation-shielded between-turn writer. Synchronous Away receipts, orphaned-turn stops and fenced Swarm snapshots use `append_events_with_lock_retry`; each attempt owns a new transaction and checks its fence again. Caller-owned transactions (`_conn`) remain the caller's responsibility. A `recording_failed` abort writes a new stable terminal error event atomically with the guarded turn settlement, independently of the failed recorder, so reload cannot leave an aborted reply streaming.
- `fluid_sessions`, `fluid_session_nodes`, `fluid_session_events` — The harness's session tree and event log (`app/core/provider/fluid_pg_store.py`). Each new engine takes the session over by bumping `fluid_sessions.writer_generation`; a replaced engine's later writes are refused inside their own transaction, without an error. Inside one process that takeover never reaches a live turn: each route takes the chat's lease (`app/core/provider/session_admission.py`, keyed on the app session id) before it does anything else and its outermost scope closes it, so a second turn is refused as busy while the first is setting up, connecting, running or saving. A message sent once the turn has announced its end waits for that close rather than being refused. A healthy turn outlives its HTTP reader and saves on a task the lease module owns; a reader that leaves is forgotten at once, and one that falls about 8 MB behind is cut off with an error frame while the turn runs on. If the pump itself abandons an engine iterator at a yielded frame, a finishing task retains admission through draining and persistence. A 15-second deadline bounds only engine reads: it never cancels an app-session write between frames or the saving tail. On an engine-read timeout it fences the originating engine store, evicts only that engine, requests interrupt and closes its transport without stopping session-owned jobs; cancellation of that engine-only reader gets at most one further second. The timed-out iterator cannot deliver more mutation frames or run its persistence/completion tail, and the next request must build a fresh engine. Once the engine finishes normally, saving is shielded from caller, watchdog and finisher cancellation; admission stays held until all issued saving writes settle, including their off-loop threads. Completed futures are reaped synchronously, including before close checks for outstanding work: Python 3.12's eager all-done gather must not spin waiting for queued discard callbacks. All owning routes (runtime, direct streaming, headless and background) await admission's actual release before reporting completion; the background done callback remains the cancellation-before-start safety net. The engine-read signal surrounds only the raw Fluid iterator, excluding adapter-side Away receipts and question persistence. Binding admission installs a once-per-loop executor-dispatch hook: every raw `asyncio.to_thread` or `run_in_executor` call carrying that turn's context registers and shields its underlying future, including permission-interaction inserts. Context-free dispatch is unchanged. The tracker belongs to one loop; nested `asyncio.run` work is covered by its outer dispatch (which cannot return before the nested default executor joins), never by gathering foreign-loop futures. Foreign-loop shared custom-executor helpers also wait through cancellation until their write returns, since `asyncio.run` cannot join those pools itself. A failed drain fails closed and keeps admission held for recovery; runtime settlement waits for admission's actual release. All adapter/query/persistence thread dispatch uses the shared tracked executor primitive; a ContextVar carries the exact turn's in-flight tracker, and even cancellation of a thread's caller cannot hide that future. Admission release drains it regardless of the phase signal, including detached saving producers. Saving relies on DB/pool timeouts rather than the engine deadline. A genuinely wedged executor thread has no safe cancellation deadline: the chat intentionally stays busy until recovery or restart rather than admitting a successor under an outstanding write. A replaced runtime may release its own run and deregister itself, but cannot publish a terminal frame or halt its direct-API successor's computer use. A rewind instead takes the lease from a running reply, stops it, and waits for it to save before it claims. Rewinds of one chat run one at a time and finish even if their caller leaves, and turns wait for them, so nothing a rewind does can land after a later rewind or turn. Those waits end in the busy error after 60 seconds, and the chat is not freed by that, because a rewind stuck on a database write can still act when the write returns. Another install on the same database never passes through that lease, so a turn it takes over stops with `SessionReplacedError` instead of ending as a success.
- `agents` — Agent configuration (profile-named functions in `app/db/profiles.py` remain as compatibility names)
- `projects` — Project directory references
- `connectors` — Platform connector configs (encrypted)
- `automations` — Scheduled/triggered workflows
- `project_hooks` — Event handlers (bash, webhook, message)
- `api_users` — API authentication keys
- `tags` — Session organization tags

### Event write recovery boundaries

The earlier lock-timeout fix protected recorder batches but not standalone
lifecycle producers. Deletion's lock-order fix removed a deadlock cycle, not
ordinary contention. Neither could supply a closing event once a failed recorder
stopped accepting writes. Recovery therefore covers the standalone producers as
well as the guarded terminal transition; it does not increase SQL timeouts or
retry caller-owned transactions or uncertain commits. The focused PostgreSQL
permission tests hold the event counter past `lock_timeout`, including two
independent processes where the answering install wins the counter before the
requesting install retries. Each terminal batch includes its stable-id request
snapshot; the counter serializes them and late duplicate requests cannot reopen
settled cards. Deferred re-asks have a new durable interaction id and still reopen
the existing display card.

Recording failure first commits a run-id-guarded, row-only `draining` marker
with a stable terminal intent in the existing Stop notice outbox. This does not
need the transcript counter. The marker excludes the already-executed prompt
from reaping/requeue and holds its live slot until the receipt commits, preventing
a delayed close from closing a successor's stream. Orphan Stop and parking also
leave failure-owned rows to this intent: neither may erase its marker. Stop's
shared cleanup admission captures its input receipt and ownership identity
under the session admission lock, or refuses a stale target. Local WebSocket
and owning-peer Stop use that same captured boundary, including parked and
session-scoped Stops. A flip's receipt is reused, never read afresh after a wait.
Background tasks stop by captured incarnation; queued words become restorable
"Not sent" messages and cannot run when terminal delivery releases the slot.
The session-wide user-stop/Swarm latch is set only under a fresh ownership and
input check holding the admission lock: late cleanup cannot latch a successor
or discover its input. Newer queued input disables the latch but does not spare
older captured words while ownership is unchanged. Empty captured receipts
never trigger a fresh read. If receipt validation fails before its guarded
callback, it grants no queue or latch authority, but captured task incarnations
still stop. Validation and quiescing failures stay retryable: an incomplete
warning does not checkpoint or consume the durable receipt, so the owner
observer retries its original boundary. The owning process keeps the first
local task-incarnation scope for an unfinished receipt, rather than recapturing
a resumed task. Its live ownership proof also covers older tasks whose durable
mirror has not flushed yet. Restart hydration retains its existing captured
rows and logical input bounds; local process-incarnation scopes do not survive
a restart. A completed read-only validation is
still valid if only its transaction release fails. Capture failure, before any
authority exists, instead reports that Stop was not admitted and asks for a new
Stop; it cannot safely kill a possibly newer owner's scope. A committed flip's
existing receipt remains independently replayable. No admission lock is held
into async cleanup, and admission needs neither the run
nor transcript-counter lock. A newer run (even released) or started successor makes turn-scoped
admission stale. The marker and closing intent stay untouched;
the page gets no false "nothing stopped" response and waits for the close. Delivery
atomically aborts the marked turn, appends its terminal receipt and acknowledges
the outbox using the shared run/counter/session/turn lock order and the captured
run-id guard.
Only confirmed lock-timeout rollbacks get immediate bounded retries. If they
exhaust, the existing pending-notice observer finishes delivery after contention
clears, including after restart; it never replays the prompt, failed recorder
batch or Stop cleanup. Deleted/replaced targets consume stale intents without
closing their successor. No schema or SQL-timeout increase is required. This
protects a persisted failure intent, not arbitrary crashes before intent can
commit or an exactly-once execution guarantee during a database outage.

The standalone sweep includes rewind boundaries (admission stays held through
recovery) and archive-retirement reconciliation (the durable marker stays pending
until its event commits). Caller-owned appends in Stop/unsent restoration, plans,
session moves, parked settlement, task replay and orchestration remain with their
owning transaction; warning delivery already has a durable outbox. Recorder and
between-turn taps retain their existing ordered recovery. Watch events use a
separate watch store, not the transcript event log.

### Admission recovery and independent background work

A long-lived watcher is not itself a turn-write producer: merely inheriting a turn's context does not keep admission held. Its already-issued executor jobs join the originating tracker; jobs issued after that admission closes still inherit that tracker but cannot reopen it or join the successor's tracker. Completion callbacks remove those futures rather than accumulating them. A detached producer that can issue delayed turn writes must register its whole task, as context-usage saving does; generation fencing of independent background side effects remains separate.

Shutdown waits five seconds for unattended owners, but cannot stop their actual executor threads. Shielded saving can delay subsequent event-loop/process exit until a thread returns; an irrecoverably wedged thread can require an external restart. Ordinary database waits retain pool/statement/lock timeouts. If draining itself fails, admission is not automatically unlocked even after a thread later returns: ordinary input receives the existing busy error (after the ending wait where applicable), and the server logs `Failed to drain turn writes ... keeping admission held`. There is no dedicated drain-health UI or safe force-unlock action. Restart is the supported recovery; never release admission over an unproven outstanding write.

## Security

- **Encryption** — Stored credentials use a persistent data key wrapped by a private key under `DATA_DIR/.credential-keys`. Adoption retains existing ciphertext and data keys; fresh installations generate random keys. Key backups are required alongside PostgreSQL backups.
- **Authentication** — Trusted local/LAN owner access on the local listener; account-authorized cookie sessions on protected remote ingress; scoped Bearer tokens for API users. Local setup requires no account or password.
- **Authorization** — Admin-only endpoints, API user scoping by project/profile
- **CORS** — Configurable origin restrictions
- **Input Validation** — Pydantic models for all request/response schemas
