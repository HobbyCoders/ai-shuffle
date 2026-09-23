# AI Shuffle

> A self-hosted workspace for AI agents, powered by the Fluid harness.

[Releases](https://github.com/HobbyCoders/ai-shuffle/releases) · [Desktop](desktop/README.md) · [Android](android/README.md) · [Fluid](fluid/README.md)

AI Shuffle brings your agents, projects, conversations, and tools into one workspace. Use it to build software, research a problem, automate recurring work, browse the web, or generate media. Run a worker on your computer or server, then work through the browser, desktop app, or Android app.

**Fluid is the agent harness underneath it.** A harness is the software that turns a model into a working agent: it builds context, runs tools, handles approvals, keeps the session, and manages what happens when a conversation grows. Fluid has its own execution loop for API and local models, plus a delegated path for Claude subscription sessions.

## AI Shuffle and Fluid

| | What it does |
| --- | --- |
| **AI Shuffle — the app** | Gives you the interface, projects, session management, model-provider configuration, voice, connectors, automations, and network of workers. |
| **Fluid — the harness** | Runs agent sessions, manages tools and permissions, preserves searchable history, compacts context, and supports reusable skills and memory. |
| **Your model provider** | Supplies the model's reasoning and generation. Capabilities, context limits, availability, and billing depend on the provider and model you choose. |

Fluid lives in this repository as an independently installable Python package. You can use it through AI Shuffle, run its standalone CLI, or embed it in another application.

## What you can do with AI Shuffle

### Work with agents in a full interface

Stream responses and tool activity as they happen. Attach files, reference project files with autocomplete, dictate a prompt, and respond to permission requests in the conversation. Keep multiple sessions open, search their history, organize them with tags and favorites, and fork a conversation to explore another direction.

Profiles define instructions and the tools, plugins, and subagents available to an agent. Choose the model, reasoning effort, and permission mode for each session. The same profile can serve different tasks with different models.

### Build and maintain projects

Browse files, inspect diffs, work on branches and worktrees, commit changes, create pull requests, and inspect GitHub CI from the app. Give specialized agents focused tasks and follow their progress alongside the main conversation.

### Connect tools and services

Add MCP servers and plugins to extend an agent's capabilities. The bundled browser tools can navigate pages, interact with forms, inspect content, and take screenshots. AI media tools integrate with fal.ai for images, video, audio, and 3D generation.

Connectors bring agents into services such as Telegram, Discord, Slack, WhatsApp, Gmail, Home Assistant, and Spotify. See the [connector guides](docs/content/connectors/) for each integration's setup and supported actions.

### Run work on a schedule or event

Automations can start from schedules, intervals, webhooks, and connector events. Configure prompts, budgets, approval requirements, and where results should go. Hooks add custom behavior around supported lifecycle events.

### Talk to your agents

Voice mode uses OpenAI Realtime for the conversation and can hand work to agents, steer or stop their tasks, and relay approval requests. Configure voice separately from the model doing the delegated work. See [voice mode](docs/content/features/voice.md).

### Use your own machines together

Each worker has its own managed PostgreSQL database and can run independently. Pair workers over your local network or link them through a Shuffle account to synchronize shared settings, profiles, notes, memory, plugins, and the session catalog.

A session executes on one worker at a time. Connected clients can see network sessions and direct work to the worker that owns them. Synced payloads are end-to-end encrypted. See the [worker network](docs/network/README.md) for synchronization, ownership, recovery, and offline behavior.

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

For local models, the standalone Fluid CLI has a dedicated `local` provider. AI Shuffle routes compatible custom endpoints through its configured API dialect; standalone configuration options are not all exposed in the app. See [Fluid configuration](fluid/docs/configuration.md) and [local models](fluid/docs/local-models.md).

## Quick start

### Desktop

Install the [desktop app](desktop/README.md) for Windows, macOS, or Linux. It packages the worker and its private PostgreSQL database and opens the web interface in a native window.

On first launch, choose a fresh setup, sign in to your Shuffle account, or join an existing worker network. Select a workspace and configure a model provider. Local use does not require a Shuffle account.

### Docker

For a **fresh installation**, clone this repository and run Docker Compose 2.24 or newer:

~~~sh
git clone https://github.com/HobbyCoders/ai-shuffle.git
cd ai-shuffle
docker compose up -d --build
~~~

Open [localhost:8000](http://localhost:8000), choose your workspace, and add a model provider. The container includes managed PostgreSQL 16; a fresh install needs no database password or `.env`.

The default port binding is `127.0.0.1:8000`. To deliberately share the local listener on your LAN, set `BIND_HOST` in `.env` and review [local and remote access](docs/LOCAL_ACCESS.md): the local listener grants owner authority, and Docker-published ports can bypass host firewall rules.

**Updating an older installation:** this release requires managed embedded PostgreSQL. It does not automatically import an existing external database. Keep a non-managed installation on its existing release with its data, configuration, and protected backups until a tested migration is available. Do not use `down -v` or remove retained volumes. Follow [backup and recovery](docs/RESTORE.md).

Retain the `ai-shuffle-data:/data` volume, workspace, and authentication/settings/browser volumes. The data volume includes PostgreSQL, identity, and credential keys. Compose allows 90 seconds for shutdown; use at least that timeout with other container launchers. The runtime owns database credentials and its private descriptor; external database connection settings are not supported.

### Android

[Get AI Shuffle on Google Play](https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle).

The Android app connects to workers and bundles its own mobile interface. It requires Android 8.0 or newer. Availability depends on the release track and Play review; testing tracks require enrollment. See [Android setup and builds](android/README.md).

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

For a single task, use `fluid run "explain this project"`; resume an interactive session with `fluid chat --continue`. See the [CLI guide](fluid/docs/cli.md) for provider selection, approvals, and non-interactive behavior.

## Browser automation

The bundled `browser-mcp` server uses Playwright over CDP. Enable it in **Settings → Browser**. Docker includes supervised Chromium; native installs can use an available Chrome, Chromium, or Edge binary. Set `CHROME_EXECUTABLE` if auto-detection cannot find it.

The browser profile persists, including its logged-in sessions. The DevTools port stays on loopback and should not be published. Browser tools act with that profile's access.

Cloud metadata addresses are blocked. Private LAN destinations are allowed; loopback destinations are allowed by default in local mode and refused by default in Docker. `BROWSER_SSRF_ALLOW_LOOPBACK=true` permits local preview servers in a trusted single-user Docker deployment, but also exposes other loopback services to pages the browser visits. Explicit `BROWSER_SSRF_ALLOW` and `BROWSER_SSRF_BLOCK` CIDR settings refine this behavior; block entries win.

## Permissions and data

AI Shuffle separates local owner access from authenticated remote access. Remote access uses owner identity and revocable device sessions; integration API keys are scoped. Stored credentials use protected key custody. See [access boundaries](docs/LOCAL_ACCESS.md) and [recovery requirements](docs/RESTORE.md).

Fluid adds tool approvals, rules, hooks, sensitive-file checks, and defenses against untrusted tool output. These controls complement the permissions of the machine or container running the worker. Use isolation appropriate to the tools and projects you expose. See [Fluid's security model and limitations](fluid/docs/security.md).

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

For source development, `python run_local.py --setup` prepares dependencies and assets; it does not provision a standalone database. Use the Docker source build or the [desktop development supervisor](desktop/README.md#develop) to run the worker. That supervisor manages a private database; pointing it at PostgreSQL binaries does not select an external database server.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and test workflows. Fluid's suite runs in its own environment:

~~~sh
python -m pip install -e './fluid[dev]'
python -m pytest fluid/tests/unit -q
~~~

Keep that environment separate from the app's dependency environment. For harness evaluation, the repository includes a [Harbor / Terminal-Bench adapter](fluid/benchmarks/harbor/README.md). Architectural benefits are not a claim of benchmark superiority over another harness.

## Documentation

| Topic | Guide |
| --- | --- |
| Fluid CLI, providers, code mode, and memory | [Fluid](fluid/README.md) · [Configuration](fluid/docs/configuration.md) |
| Background agents and shell tasks | [Fluid background tasks](fluid/docs/background-tasks.md) |
| Profiles and tools | [Profiles](docs/content/features/profiles.md) · [MCP](docs/content/features/mcp-servers.md) · [Plugins](docs/content/features/plugins.md) |
| Automations and hooks | [Automations](docs/content/features/automations.md) · [Hooks](docs/content/features/hooks.md) |
| Workers and devices | [Network](docs/network/README.md) · [Mobile/web interface split](docs/mobile-desktop-ui-split.md) |
| Installation and recovery | [Desktop](desktop/README.md) · [Android](android/README.md) · [Backups](docs/RESTORE.md) |
| API integrations | [REST and WebSocket reference](docs/content/api-reference/) |

Documentation is also available inside the app.

## License

Proprietary. See [LICENSE](LICENSE).
