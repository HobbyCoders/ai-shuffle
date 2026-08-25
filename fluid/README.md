# fluid

**An AI agent harness that doesn't fight you.**

Most agent harnesses are one app welded to one provider, with context management
bolted on as an afterthought. fluid is the opposite: a headless, provider-agnostic
engine built around the ideas that actually make agents fast, cheap, and durable —
aggressive prompt caching, sessions you can branch like git, an agent that writes
code instead of clicking through tool calls one at a time, and a memory that turns
repeated work into reusable skills.

Fluid uses native, explicit paths: `~/.fluid/AGENTS.md`,
`~/.fluid/skills/<name>/SKILL.md`, `~/.fluid/agents/**/*.md`, and project
`.agents/` directories. Existing Claude and Codex content can be reviewed and
copied with `fluid import-settings`; it is never silently loaded or executed.

## Getting started

fluid needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). From a clone:

```sh
uv sync --extra mcp          # add --extra tui for the Textual interface
export ANTHROPIC_API_KEY=sk-ant-...
uv run fluid chat
```

`fluid run "summarize the failing tests"` does one turn and exits;
`fluid chat --continue` picks up where the last session stopped. Switch
provider with `--provider openrouter` (or `openai`, `local`, `claude`) after
setting the matching key; `fluid config` prints what a run will use.

- [docs/cli.md](docs/cli.md) — every subcommand, flag, slash command,
  permission prompt answer, and exit code.
- [docs/configuration.md](docs/configuration.md) — every config key and
  default, the layer order, `FLUID_*` variables, `[[hooks]]` and
  `[[permission.rules]]`, provider blocks with an OpenRouter example.
- [docs/hooks.md](docs/hooks.md) — shell-command hooks.
- [docs/background-tasks.md](docs/background-tasks.md) — `Task` and `Monitor`
  workers, and how the CLI hosts them.
- [docs/local-models.md](docs/local-models.md) — llama.cpp, Ollama, and the
  text tool-call protocol.
- [docs/security.md](docs/security.md) — harness protection: injection
  fencing, control-marker spoofing defense, credential guards, exfiltration
  gating, and how to override each layer.

## Why fluid

- **Bring your own provider.** Anthropic, OpenAI, OpenRouter, or your Claude
  Pro/Max subscription — one harness, same experience.
- **Native paths, explicit migration.** Global instructions, skills, and agents
  live under `~/.fluid`; project content lives under `.agents/`. Root
  `AGENTS.md`, `AGENT.md`, and `AGENTS.override.md` retain their familiar
  precedence. `fluid import-settings` previews Claude and Codex sources, never
  overwrites a destination, redacts credential-bearing MCP values, and leaves
  unsupported provider settings inert.
- **Built to be embedded.** The core is headless and event-driven — the bundled
  TUI is just one subscriber. Wire the same engine into a server, a bot, or your
  own app.
- **Cheap by design.** Cache hit rate is a first-class metric, not an accident.
  The prompt is engineered so providers can reuse almost everything between turns.
- **Never lose work.** Sessions are branching trees — fork from any point, rewind
  without paying the cache penalty again. The code kernel checkpoints itself, so
  even a crash restores your data.

**Provider backends**

| Backend | Mode | Auth |
|---|---|---|
| Anthropic Messages API | native loop | API key |
| OpenAI Responses API | native loop | API key or Codex/ChatGPT-subscription OAuth |
| OpenRouter / OpenAI-compatible | native loop | API key |
| Local (llama.cpp `llama-server`, Ollama, any OpenAI-compatible server) | native loop | optional API key (`--api-key` servers) |
| Claude Pro/Max subscription | delegated (official `claude` CLI as subprocess) | the CLI's own OAuth |

Subscription traffic always goes through the official CLI — the explicitly
permitted path — never raw token reuse.

The local backend points at any OpenAI-compatible `chat/completions` endpoint
(`base_url` accepts an origin, a `/v1` prefix, or the full endpoint) and adds an
opt-in `tool_style = "text"` that runs tool calling over a `<tool_call>` text
protocol — so small local models without reliable native tool calls still work
as full agents. See [docs/local-models.md](docs/local-models.md).

Shell-command hooks (`[[hooks]]` in config.toml, or a copied Claude Code
`hooks.json`) run before and after tool calls and at turn boundaries, and can
deny or rewrite a call. See [docs/hooks.md](docs/hooks.md).

Permissions are a policy (`never_ask`, `on_write`, `always_ask`) plus explicit
rules. Under the default `on_write`, a `Bash` command the classifier can prove
is read-only (`git status`, `grep`, `ls`, a piped `head`) runs without a
prompt; anything else asks. Answering `a` at a prompt allows that tool for the
session, scoped for `Bash` to the programs the command ran. A denial tells the
model why (rule, user, or non-interactive run) so it stops instead of trying
another route.

Long-running work leaves the turn: `Monitor` detaches a shell command and
`Task` runs a child agent, the session parks when the turn ends first and
wakes with the result. The standalone CLI hosts both; see
[docs/background-tasks.md](docs/background-tasks.md).

## Under the hood

A few of the ideas that make fluid different, in plain terms:

**Cache-first context.** Providers give huge discounts when the start of your
prompt is identical to the last request. Most harnesses squander this by
shuffling the prompt around. fluid treats the prompt as append-only with
deliberate cache breakpoints — nothing mutates the prefix mid-session — so
nearly every turn is a cache hit. You see the hit rate reported like any other
metric.

**Sessions as trees, not tapes.** A conversation isn't a straight line — you try
things, back up, try again. fluid stores sessions as branching trees: fork at
any message, explore, and come back, with the cache preserved along the way.

**Prevention over compaction — and recoverable when it happens.** When context
fills up, most tools summarize the conversation and hope nothing important gets
lost. fluid works to never reach that point: old tool output is trimmed in
cache-safe ways, big results are kept out of the conversation entirely, and
handoffs are structured — not a lossy "previously, on this session…" paragraph.
When a session outgrows even that, a mechanical eviction ladder takes over —
old turns collapse into deterministic, extractive digests, then into one-line
epoch stubs — and every evicted thing keeps a ref: `HistoryExpand` and
`HistorySearch` re-read the originals from the session store on demand, so
nothing is ever permanently lost. The window becomes a working set, not a
lifetime cap; sessions age instead of dying.

**Code-as-actions.** Instead of the model requesting tools one JSON call at a
time, fluid projects tools as typed Python functions into a persistent,
sandboxed kernel. The model writes a few lines of code that loop, filter, and
combine tools in one shot — dramatically fewer round trips, and tool schemas
stay out of the prompt until needed. The sandbox blocks network and direct file
access (files go through permission-gated tools), and its memory limits hold on
both Linux/macOS and Windows.

**A harness that learns.** With the continual mode on, fluid keeps an episodic
journal of what actually happened — executed kernel cells under code mode, or
per-tool-call entries in plain JSON sessions — and runs a background pass that
distills recurring workflows into persistent Python skills. Skills have to
prove themselves: each one carries a self-test, verified in a throwaway kernel
on save and whenever it changes — broken skills are unmounted and flagged, not
silently trusted. The synthesizer is explicitly told what *not* to learn
(environment-dependent failures, negative tool claims that harden into
standing refusals, unresolved failures written up as workflows), and a
deterministic curation pass ages the library: stale after 30 days, archived —
never deleted — after 90, pinned skills untouched. Over time the harness gets
genuinely better at *your* work. (Design rationale:
[docs/agent-experience.md](docs/agent-experience.md).)

**A real base prompt — composed, not pasted.** The CLI's system prompt is a
behavioral contract covering communication, scope, tool use, permissions, and
context hygiene, assembled from **gated blocks**: prose about a tool appears
only when that tool is actually in the session's surface (a restricted session
never reads about capabilities it doesn't have — and a test enforces it). An
**identity layer** (`identity` in config, or `~/.fluid/IDENTITY.md`) carries
your durable voice as the first block without touching the contract below it.
**Model-family guidance** injects act-don't-narrate and execution-discipline
blocks only for model families that need them (`model_guidance = "auto"`) —
the same prompt that stays lean on Claude keeps a mid-tier OpenRouter model
finishing jobs. The environment block ends with a workspace snapshot: branch
and dirty state, detected manifests, and the project's actual verify commands
(`pytest`, `npm run test`, make targets), so the model starts with its
verification loop in hand. Set `system` to replace the whole contract, or
`base_prompt = false` to run with none.

**Web, questions, and memory.** `WebFetch` and `WebSearch` (keyless,
SSRF-guarded, results fenced as untrusted data) close the network gap without
an MCP server. `AskUser` asks the user structured questions mid-turn and
parks until they answer. Memory is two-sided: the continual learner distills
remembered facts (alongside its skills) into a bounded, machine-curated list
injected each session, and the session store itself is episodic memory —
`SessionSearch` lists and searches past sessions, `SessionView` opens one, so
"like we did last week" is answered from the record.

**Replayable by construction.** The engine consumes operations and emits a
sequence-numbered event stream. Every UI, log, and integration is a subscriber
of that stream — which means sessions are replayable, debuggable, and embeddable
for free.

## Status

Early development, moving fast. Working today:

- Both engine legs: the native API loop and the delegated `claude` CLI.
- Five provider backends (Anthropic, OpenAI Responses, OpenRouter, local
  OpenAI-compatible servers, Claude subscription via the CLI).
- The CLI: `fluid chat` (REPL and `--tui`), `fluid run`, `fluid sessions
  --limit`, `fluid config`, `fluid import-settings`, `--version`;
  `--resume <id>` and `--continue` rehydrate a stored session. REPL commands:
  `/context` (`/usage`), `/tools`, `/skills`, `/tasks`, `/cancel`, `/model`,
  `/fork`, `/rewind`, `/help`, plus user-defined `.md` commands.
- A shipped base prompt with an environment block; Git Bash on Windows.
- Permissions with read-only Bash classification, program-scoped
  allow-always, last-match-wins rules, and actionable denial messages.
- Shell-command hooks from `[[hooks]]` or a copied Claude Code `hooks.json`.
- Background `Task` and `Monitor` workers hosted by the CLI, with park and
  wake.
- MCP servers over stdio and HTTP (`mcp>=2.0`), tool images conditioned to
  the vision budget, deferred tool schemas with `ToolSearch` (budget-tiered
  catalog: full listing → names only → per-server summary).
- Dispatch-boundary hardening: MCP results fenced as untrusted data with
  elision-marker notices, bounded and de-framed error text, schema-guided
  argument coercion, and availability probes with a last-good grace window.
- `WebFetch`/`WebSearch` (keyless, SSRF-guarded, fenced), `AskUser`
  structured questions, cross-session recall with `SessionSearch` /
  `SessionView`, synthesizer-curated memories, and a mid-turn `Steer` op
  delivered on the next tool result inside a trusted marker.
- Code mode with `from tools import X`, a pure-stdlib allowlist, trimmed
  tracebacks, and the continual skill synthesizer.
- The compaction ladder with head excerpts on trim stubs, `HistoryExpand` /
  `HistorySearch` recall, and a `/context` breakdown that reports what the
  provider billed.
- Request tracing with `FLUID_TRACE_REQUESTS=<dir>` for cache-prefix diffs.
- Fluid-native instruction, skill, agent, and command discovery; the explicit
  provider-settings importer copies portable Claude and Codex content without
  enabling provider-owned runtime discovery.
