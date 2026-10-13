# Development overview

Internal: HobbyCoders team only. This is the developer half of the former
README: how the app and the Fluid harness fit together, how sessions run,
source builds and the repository layout. Buyers read the in-app
documentation (`docs/content/getting-started/`) instead.

Related internal guides: [CONTRIBUTING.md](CONTRIBUTING.md),
[LOCAL_SETUP.md](LOCAL_SETUP.md), [architecture.md](architecture.md) and the
[internal index](README.md).

> A self-hosted workspace for AI agents, powered by the Fluid harness.

[Releases](https://github.com/HobbyCoders/ai-shuffle/releases) · [Desktop](../../desktop/README.md) · [Android](../../android/README.md) · [Fluid](../../fluid/README.md)

AI Shuffle brings your agents, projects, conversations, and tools into one workspace. Use it to build software, research a problem, automate recurring work, browse the web, or generate media. Run a worker on your computer or server, then work through the browser, desktop app, or Android app.

**Fluid is the agent harness underneath it.** A harness is the software that turns a model into a working agent: it builds context, runs tools, handles approvals, keeps the session, and manages what happens when a conversation grows. Fluid has its own execution loop for API and local models, plus a delegated path for Claude subscription sessions.

## AI Shuffle and Fluid

| | What it does |
| --- | --- |
| **AI Shuffle — the app** | Gives you the interface, projects, session management, model-provider configuration, voice, connectors, automations, and network of workers. |
| **Fluid — the harness** | Runs agent sessions, manages tools and permissions, preserves searchable history, compacts context, and supports reusable skills and memory. |
| **Your model provider** | Supplies the model's reasoning and generation. Capabilities, context limits, availability, and billing depend on the provider and model you choose. |

Fluid lives in this repository as an independently installable Python package. You can use it through AI Shuffle, run its standalone CLI, or embed it in another application.

Harness-owned files never belong in a checkout's `.fluid/`: Ledger, Note and
Task reports use `settings.get_fluid_projects_dir/<native-memory-project-slug>/`
(normally `~/.fluid/projects/<slug>/`; a configured `DATA_DIR/fluid/` projects
root is also supported), with `ledger/<session>.md`, `notes/<session>.md`,
`task-reports/<worker>.md`, and designated `scratch/<session>/`. The app anchors
the slug on the canonical project/base checkout across worktrees; standalone Fluid uses its own memory project identity
and has no app dependency. Report receipts are absolute and use normal Read
permissions, not a blanket allow. Legacy cwd ledger/notes are copied exclusively
on first access; existing canonical files (including empty cleared state) win.
Old files and owner scratch are left intact. Project `.fluid/` remains a load
scope for skills, agents, commands, plugins and settings, plus app-managed
worktrees; explicit skill/plugin authoring and worktree lifecycle are unchanged.
Standalone scratch guidance is CLI-owned context: native turns carry a removable
system reminder, while the delegated CLI receives SessionStart hook context
(including after compaction). Bare embedded loops keep their caller's prompt
unchanged; the app supplies its own scratch guidance. Journals and client
transcripts exclude the scratch reminder just as they exclude ledger reminders.

## What you can do with AI Shuffle

### Work with agents in a full interface

Stream responses and tool activity as they happen. Attach files, reference project files with autocomplete, dictate a prompt, and respond to permission requests in the conversation. Keep multiple sessions open, search their history, organize them with tags and favorites, and fork a conversation to explore another direction.

Profiles define instructions and the tools, plugins, and subagents available to an agent. Choose the model, reasoning effort, and permission mode for each session. The same profile can serve different tasks with different models.

### Build and maintain projects

Browse files, inspect diffs, work on branches and worktrees, commit changes, create pull requests, and inspect GitHub CI from the app. Give specialized agents focused tasks and follow their progress alongside the main conversation.

### Connect tools and services

Add MCP servers and plugins to extend an agent's capabilities. The bundled browser tools can navigate pages, interact with forms, inspect content, and take screenshots. AI media tools integrate with fal.ai for images, video, audio, and 3D generation.

Connectors bring agents into services such as Telegram, Discord, Slack, WhatsApp, Gmail, Home Assistant, and Spotify. See the [connector guides](../../docs/content/connectors/) for each integration's setup and supported actions.

### Run work on a schedule or event

Automations can start from schedules, intervals, webhooks, and connector events. Configure prompts, budgets, approval requirements, and where results should go. Hooks add custom behavior around supported lifecycle events.

### Talk to your agents

Voice mode uses OpenAI Realtime for the conversation and can hand work to agents, steer or stop their tasks, and relay approval requests. Configure voice separately from the model doing the delegated work. See [voice mode](../../docs/content/features/voice.md).

### Use your own machines together

Each worker has its own managed PostgreSQL database and can run independently. Pair workers over your local network or link them through a Shuffle account to synchronize shared settings, profiles, notes, memory, plugins, and the session catalog.

A session executes on one worker at a time. Connected clients can see network sessions and direct work to the worker that owns them. Synced payloads are end-to-end encrypted. See the [worker network](../../docs/network/README.md) for synchronization, ownership, recovery, and offline behavior.

## Why Fluid

### Recoverable compaction for long-running work

A long conversation contains more history than the model needs for its next decision. Fluid manages the active context as a working set while retaining the stored conversation for later retrieval.

Its native engine uses a three-stage compaction layer:

| Stage | What changes in the model's context |
| --- | --- |
| **Trim tool results** | Older tool output becomes compact placeholders with references to the stored content. |
| **Digest older turns** | Older conversation spans become deterministic, extractive digests. |
| **Fold older digests** | Groups of digests become smaller epoch stubs, still carrying history references. |

These transformations change the **model-facing view**, not the stored session nodes. `HistorySearch` searches the stored text, including compacted turns; `HistoryExpand` retrieves original text by reference in bounded windows. The agent can return to an earlier decision, error, or tool result when it becomes relevant again.

Compaction is mechanical: it does not need a separate model call to invent a summary. It normally protects recent turns, keeps bounded error details, and applies changes in batches to limit disruption to the cached prompt prefix. If the provider rejects an oversized request, an escalation path can reclaim more recent content and retry.

This is recoverable history, not unlimited active context. Retrieval depends on the retained session store and available history tools; content truncated before storage cannot be reconstructed. History expansion is text-only, so images may need to be read again. On the delegated Claude path, the Claude CLI manages compaction instead.

### Python tool orchestration

With code mode enabled, the model can discover tools and call them as Python functions inside a persistent subprocess kernel. A single cell can combine calls, filter large results, and carry intermediate values into later cells.

Tool calls still cross back to the host for permission and hook checks. Large results can remain behind references instead of filling the conversation with raw output. This can reduce model round trips for workflows that benefit from batching and data processing.

The kernel has import restrictions, audit guards, timeouts, and memory limits. It is **not an OS security sandbox**; host tools run with the access granted to the worker.

### Memory and reusable workflows

Fluid supports explicit remembered facts, scoped memory, cross-session recall, and an optional continual-learning process. The learner reads a journal of activity and can distill useful workflows into saved Python skills.

Skills are imported in a verification kernel and can supply a pure `_selftest`. Memory and skill curation bound what stays active. Learning here means updating stored knowledge and reusable code; it does not train model weights or guarantee that every learned workflow is correct.

In AI Shuffle, code mode and continual learning default on for eligible native sessions. In the standalone CLI they are opt-in. Delegated sessions have a different capability set.

### Inspectable context and execution

Fluid exposes context usage, provider-reported cache metrics where available, and a sequence-numbered event stream for integrations. Its session store supports branching history, and compaction decisions can be replayed when a native session resumes.

Prompt caching and compaction work together: stable content stays reusable between requests, while occasional compaction batches reduce the active history. Actual cost and speed depend on the model, provider, workload, and cache behavior.

### Multiple providers, explicit configuration

Fluid provides native adapters for Anthropic Messages, OpenAI Responses, OpenRouter, and local OpenAI-compatible endpoints. The standalone local adapter also offers an optional text tool-call protocol for models that struggle with native tool calling.

Instructions, skills, agents, and settings use Fluid's own paths. `fluid import-settings` previews portable content from Claude or Codex setups before copying it; provider configuration is not silently adopted.

## How sessions run

AI Shuffle uses Fluid as its provider backend. Fluid then chooses the execution path appropriate to the configured provider.

| Connection | Execution path | What manages tools and context |
| --- | --- | --- |
| Anthropic API or a compatible Messages endpoint | Fluid native loop | Fluid |
| OpenAI API / Responses endpoint | Fluid native loop | Fluid |
| Configured ChatGPT/Codex subscription connection | Fluid native Responses adapter with the configured OAuth credentials | Fluid; this does not launch the Codex CLI |
| OpenRouter or a custom OpenAI-compatible endpoint | Fluid native loop | Fluid |
| Claude subscription account | Fluid delegated loop, launching the official Claude CLI | Claude CLI, with AI Shuffle/Fluid integration |

Native Fluid features such as Python code mode and the compaction ladder do not replace the Claude CLI's internals on delegated sessions. Provider integrations also differ in supported tools, reasoning controls, authentication, and usage limits.

For local models, the standalone Fluid CLI has a dedicated `local` provider. AI Shuffle routes compatible custom endpoints through its configured API dialect; standalone configuration options are not all exposed in the app. See [Fluid configuration](../../fluid/docs/configuration.md) and [local models](../../fluid/docs/local-models.md).

## Quick start

### Desktop

Install the [desktop app](../../desktop/README.md) for Windows, macOS, or Linux. It packages the worker and its private PostgreSQL database and opens the web interface in a native window.

On first launch, choose a fresh setup, sign in to your Shuffle account, or join an existing worker network. Select a workspace and configure a model provider. Local use does not require a Shuffle account.

### Docker

For a **fresh installation**, clone this repository and run Docker Compose 2.24 or newer:

~~~sh
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle
docker compose up -d --build
~~~

Open [localhost:8000](http://localhost:8000), choose your workspace, and add a model provider. The container includes managed PostgreSQL 16; a fresh install needs no database password or `.env`.

The default port binding is `127.0.0.1:8000`. To deliberately share the local listener on your LAN, set `BIND_HOST` in `.env` and review [local and remote access](../../docs/LOCAL_ACCESS.md): the local listener grants owner authority, and Docker-published ports can bypass host firewall rules.

**Updating an older installation:** this release requires managed embedded PostgreSQL. It does not automatically import an existing external database. Keep a non-managed installation on its existing release with its data, configuration, and protected backups until a tested migration is available. Do not use `down -v` or remove retained volumes. Follow [backup and recovery](../../docs/RESTORE.md).

Retain the `ai-shuffle-data:/data` volume, workspace, and authentication/settings/browser volumes. The data volume includes PostgreSQL, identity, and credential keys. Compose allows 90 seconds for shutdown; use at least that timeout with other container launchers. The runtime owns database credentials and its private descriptor; external database connection settings are not supported.

### Android

[Get AI Shuffle on Google Play](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle).

The Android app connects to workers and bundles its own mobile interface. It requires Android 8.0 or newer. Availability depends on the release track and Play review; testing tracks require enrollment. See [Android setup and builds](../../android/README.md).

### Fluid on its own

From this repository, with Python 3.12+:

~~~sh
cd fluid
python -m pip install -e '.[mcp]'
export ANTHROPIC_API_KEY=your-api-key
fluid chat
~~~

Use the environment-variable syntax for your shell. To try native Python tool orchestration and continual learning:

~~~sh
fluid chat --continual
~~~

For a single task, use `fluid run "explain this project"`; resume an interactive session with `fluid chat --continue`. See the [CLI guide](../../fluid/docs/cli.md) for provider selection, approvals, and non-interactive behavior.

## Browser automation

The bundled `browser-mcp` server uses Playwright over CDP. Enable it in **Settings → Browser**. The Docker image runs Debian Chromium on every architecture (linked as `google-chrome-stable`); it moved off Google Chrome because Chrome used far more memory. Native installs, including the desktop app, run headless Google Chrome, then Microsoft Edge, then a Chromium found on `PATH` (Linux and macOS only). Set `CHROME_EXECUTABLE` to choose a binary or when auto-detection cannot find one. The browser tool's description names the browser the worker resolves.

The browser profile persists, including its logged-in sessions. The DevTools port stays on loopback and should not be published. Browser tools act with that profile's access.

Public addresses are allowed and cloud metadata addresses are always blocked. Private LAN destinations (RFC1918, carrier-grade NAT or Tailscale `100.64.0.0/10`, IPv6 unique-local) are refused unless listed in `BROWSER_SSRF_ALLOW`. Loopback destinations are allowed by default in every runtime (Docker, native and desktop) so an agent can view a preview server it started; this also exposes other loopback services to pages the browser visits. `BROWSER_SSRF_ALLOW_LOOPBACK=false` refuses loopback, and so does a `BROWSER_SSRF_BLOCK` entry covering it. An explicit `BROWSER_SSRF_ALLOW` CIDR list replaces the loopback default, and `BROWSER_SSRF_BLOCK` entries win over both. The worker's own ports and the DevTools port stay refused on every internal address whatever the allow-list says.

The supervised browser runs behind a loopback-only egress proxy (`browser-mcp/dist/egress-launch.js`, started by `docker/start-chrome.sh` and by the native supervisor). Every connection the browser makes goes through it, including WebSockets and loopback. The proxy resolves each host once, checks every address, and connects only to an address it checked, so DNS rebinding cannot swap the target. It enforces the app's base policy from the process environment. A `BROWSER_SSRF_ALLOW` set only in a profile's environment does not widen it, because every session shares the browser. If the launcher is missing, the browser does not start.

## Permissions and data

AI Shuffle separates local owner access from authenticated remote access. Remote access uses owner identity and revocable device sessions; integration API keys are scoped. Stored credentials use protected key custody. See [access boundaries](../../docs/LOCAL_ACCESS.md) and [recovery requirements](../../docs/RESTORE.md).

Fluid adds tool approvals, rules, hooks, sensitive-file checks, and defenses against untrusted tool output. These controls complement the permissions of the machine or container running the worker. Use isolation appropriate to the tools and projects you expose. See [Fluid's security model and limitations](../../fluid/docs/security.md).

Self-hosting controls where the app runs and its data is stored. Configured cloud models, connectors, and tools still receive the data needed to perform their work.

## For contributors

The app uses Python 3.12+, FastAPI, PostgreSQL 16, Svelte 5/SvelteKit, TypeScript, and Tailwind. Building the web and tool packages requires Node.js 22.12+. The desktop shell uses Electron; Android uses Kotlin.

| Directory | Responsibility |
| --- | --- |
| `app/` | APIs, session orchestration, Fluid adapter, database, and worker services |
| `fluid/` | Standalone harness package, CLI, provider adapters, tools, and unit tests |
| `frontend/` | Worker-served web interface |
| `desktop/` | Desktop shell and managed local runtime |
| `mobile/`, `android/` | Mobile interface, Android app, and account hub |
| `browser-mcp/` | Browser automation server |
| `tools/` | Media tools and supporting integrations |
| `tests/`, `docs/` | App tests and documentation |

For source development, `python run_local.py --setup` prepares dependencies and assets; it does not provision a standalone database. Use the Docker source build or the [desktop development supervisor](../../desktop/README.md#develop) to run the worker. That supervisor manages a private database; pointing it at PostgreSQL binaries does not select an external database server.

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for contribution and test workflows. Fluid's suite runs in its own environment:

~~~sh
python -m pip install -e './fluid[dev]'
python -m pytest fluid/tests/unit -q
~~~

Keep that environment separate from the app's dependency environment. For harness evaluation, the repository includes a [Harbor / Terminal-Bench adapter](../../fluid/benchmarks/harbor/README.md). Architectural benefits are not a claim of benchmark superiority over another harness.

## Performance diagnostics

These measures exist for performance work. They stay on this worker and in this
browser, and they carry no prompt, transcript or session identifier.

**Browser journeys.** The User Timing buffer keeps the last 32 samples of each
journey as `shuffle:app.bootstrap:<slot>` and `shuffle:chat.open:<slot>`. Read them
with `performance.getEntriesByType('measure')` and group them by `detail.journey`.
A sample ends two animation frames after the Svelte update: a rendering
opportunity, not proof of paint. Samples taken while the tab was hidden, and
samples superseded by another navigation, are dropped.

- `app.bootstrap` runs from navigation start until AppPage has loaded its lists and
  reconciled the restored selection. Only the document's first mount is measured,
  and only before any user input, so time on the login form is never counted.
- `chat.open` runs from `switchSession` until ChatView shows the session neither
  loading nor restoring. Cached opens count too, so keep cold and cached runs apart.

**Agent startup.** Each turn logs up to four INFO lines starting
`perf.agent_startup`, with session and turn IDs: `options_built`, `adapter_ready`,
`submitted` (the adapter accepted the call, not model receipt) and `first_event`
(the first adapter event, which may be an init frame, not a token). `elapsed_ms`
counts from entry to the query generator and `stage_ms` from the previous stage. A
stage that is missing means the turn ended before it. Queue time before the
generator and cross-worker delivery are not included.

**Parallel bootstrap reads.** AppPage starts the profile, session, API-user session
and tag reads while preferences restore. Reconciliation still waits for both. Build
with `VITE_PERF_PARALLEL_BOOTSTRAP=0` to restore the serial order (a build-time kill
switch, to be removed once the change is proven in production). `AppPage.test.ts`
pins the ordering. An opt-in wall-clock lab mounts AppPage with synthetic 25 ms
preference and 40 ms data latency and prints p75 for both modes. It measures the
component under synthetic I/O, not the built app:

~~~sh
cd frontend && PERF_BOOTSTRAP_LAB=1 npx vitest run src/lib/components/layout/AppPage.test.ts -t 'bootstrap wall-clock lab'
~~~

Compare user-facing timings only between production builds on the same worker,
never against Vite dev-server timings.

### Performance flags

Short-lived switches for performance changes. Each is on by default. Set a `VITE_PERF_*` flag when building the frontend (`VITE_PERF_X=0 npm run build`) and an `AI_SHUFFLE_PERF_*` flag in the worker's environment (read when a session connects) to restore the old behaviour. Remove a flag, and the old path, once the change has been live for a release without regressions.

| Flag | Kind | What `0` restores |
| --- | --- | --- |
| `VITE_PERF_PARALLEL_BOOTSTRAP` | Kill switch | AppPage reads profiles, sessions, API-user sessions and tags only after preferences restore (see above). |
| `VITE_PERF_DEFER_GIT_ON_OPEN` | Kill switch | Opening an existing chat fires the full Git fan-out alongside the transcript: commit graph, worktree list and PR state no longer wait for the transcript or the panel that shows them. |
| `VITE_PERF_STARTUP_DEDUPE` | Kill switch | Each startup reader sends its own request: `api.getShared` callers (usage limits, the away-mode/ChatAccess metadata read) no longer join a read in flight, the composer re-reads `/preferences/all` instead of joining the bind's snapshot, and the first `sync_ready` after sign-in refreshes every resource again, including reads that just went out. With the flag on, a change another device makes between a startup read and the socket subscription shows up at the next `sync_reconcile` (60 s) instead. |
| `VITE_PERF_PARALLEL_AUTH_PROBES` | Kill switch | `GET /auth/status` goes out only after `GET /owner/status` answers, instead of alongside it. |
| `VITE_PERF_EARLY_SESSION_LIST` | Kill switch | The sidebar's active and recent session lists are requested by AppPage after the catalog, instead of by RootLayout as soon as auth answers (desktop `/` only). |
| `VITE_PERF_SLIM_SESSION_READ` | Kill switch | Away mode and ChatAccess read the whole chat (`GET /sessions/:id`) to learn `away_mode` / `is_remote`, instead of the metadata-only `GET /sessions/:id/live?include_messages=false&owner_fields=true`. |
| `AI_SHUFFLE_PERF_PROVIDER_HTTP_REUSE` | Kill switch (server) | Native Anthropic and OpenAI-format chat sessions build a new HTTP client for every model call (a CA bundle load and a new TLS connection each time), and every provider client loads its own TLS context. |
| `AI_SHUFFLE_PERF_BRIDGE_DEFER` | Kill switch (server) | Every app tool on the bundled bridge is loaded into the Claude Code CLI's context again (~30k tokens of "MCP tools" instead of ~12k). By default only `DELEGATED_LOADED_TOOLS` in `app/core/provider/fluid_tool_bridge.py` is loaded and the rest wait behind the CLI's ToolSearch. |

Server-side switches are environment variables read by the worker on each event-feed request, so no rebuild is needed. Each is on by default; set it to `0` in the worker's environment to restore the old behaviour.

| Variable | Kind | What `0` restores |
| --- | --- | --- |
| `AI_SHUFFLE_PERF_SLIM_USAGE_REPLAY` | Kill switch | The event feed sends stored `turn.usage` rows unchanged. Rows written before this change then carry the full context-usage snapshot twice (about 50 KB each). New rows are written compact regardless; the full breakdown is always available from `GET /api/v1/sessions/{id}/context-usage`. |
| `AI_SHUFFLE_PERF_COALESCE_SUBAGENT_PROGRESS` | Kill switch | Opening a chat replays one `subagent.progress` frame per subagent text delta, instead of one frame per run of consecutive deltas from the same subagent. |

## Documentation

| Topic | Guide |
| --- | --- |
| Fluid CLI, providers, code mode, and memory | [Fluid](../../fluid/README.md) · [Configuration](../../fluid/docs/configuration.md) |
| Background agents and shell tasks | [Fluid background tasks](../../fluid/docs/background-tasks.md) |
| Profiles and tools | [Profiles](../../docs/content/features/profiles.md) · [MCP](../../docs/content/features/mcp-servers.md) · [Plugins](../../docs/content/features/plugins.md) |
| Automations and hooks | [Automations](../../docs/content/features/automations.md) · [Hooks](../../docs/content/features/hooks.md) |
| Workers and devices | [Network](../../docs/network/README.md) · [Mobile/web interface split](../../docs/mobile-desktop-ui-split.md) |
| Installation and recovery | [Desktop](../../desktop/README.md) · [Android](../../android/README.md) · [Backups](../../docs/RESTORE.md) |
| API integrations | [REST and WebSocket reference](../../docs/content/api-reference/) |

Documentation is also available inside the app.
