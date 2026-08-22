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
journal of what actually happened and runs a background pass that distills
recurring workflows into persistent Python skills. Skills have to prove
themselves: each one carries a self-test, verified in a throwaway kernel on save
and whenever it changes — broken skills are unmounted and flagged, not silently
trusted. Over time the harness gets genuinely better at *your* work. (Design
rationale: [docs/agent-experience.md](docs/agent-experience.md).)

**A real base prompt.** The CLI ships a curated system prompt — a behavioral
contract covering communication, scope, tool use, permissions, and context
hygiene — the way Claude Code and Codex do, and layers your `AGENTS.md` stack,
skills index, and environment block on top of it. Set `system` in `config.toml`
to use your own base prompt instead, or `base_prompt = false` to run with none.

**Replayable by construction.** The engine consumes operations and emits a
sequence-numbered event stream. Every UI, log, and integration is a subscriber
of that stream — which means sessions are replayable, debuggable, and embeddable
for free.

## Status

Early development, moving fast. Working today: both engine legs (native API loop
and delegated Claude CLI), all five provider backends, the code-as-actions
kernel with continual skills, MCP 2.0 (spec 2026-07-28), permissions, hooks, and
the TUI/REPL (`fluid chat`, `fluid run`, `fluid sessions`; `--resume <id>` and
`--continue` pick up a stored session, and the REPL answers `/context`, `/tools`,
`/skills`, `/model`, `/fork`, `/rewind`). Fluid-native instruction, skill, and agent discovery is available;
the explicit provider-settings importer inventories and copies portable Claude
and Codex content without enabling provider-owned runtime discovery.
