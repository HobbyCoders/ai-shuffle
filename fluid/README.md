# fluid

**An AI agent harness that doesn't fight you.**

Most agent harnesses are one app welded to one provider, with context management
bolted on as an afterthought. fluid is the opposite: a headless, provider-agnostic
engine built around the ideas that actually make agents fast, cheap, and durable —
aggressive prompt caching, sessions you can branch like git, an agent that writes
code instead of clicking through tool calls one at a time, and a memory that turns
repeated work into reusable skills.

Point it at your existing Claude Code setup and it just works — your CLAUDE.md,
settings, plugins, skills, agents, and slash commands come along with zero
conversion.

## Why fluid

- **Bring your own provider.** Anthropic, OpenAI, OpenRouter, or your Claude
  Pro/Max subscription — one harness, same experience.
- **Bring your existing setup.** Reads Claude Code's on-disk world natively:
  `CLAUDE.md` (with `@`-imports), `settings.json` permissions and env, plugins,
  skills, agents, and slash commands with `$1` positionals, inline `` !`bash` ``,
  and `@file` references. Codex-style `AGENTS.md` too. Switching costs nothing.
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
| Claude Pro/Max subscription | delegated (official `claude` CLI as subprocess) | the CLI's own OAuth |

Subscription traffic always goes through the official CLI — the explicitly
permitted path — never raw token reuse.

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

**Prevention over compaction.** When context fills up, most tools summarize the
conversation and hope nothing important gets lost. fluid works to never reach
that point: old tool output is trimmed in cache-safe ways, big results are kept
out of the conversation entirely, and handoffs are structured — not a lossy
"previously, on this session…" paragraph.

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

**Replayable by construction.** The engine consumes operations and emits a
sequence-numbered event stream. Every UI, log, and integration is a subscriber
of that stream — which means sessions are replayable, debuggable, and embeddable
for free.

## Status

Early development, moving fast. Working today: both engine legs (native API loop
and delegated Claude CLI), all four provider backends, the code-as-actions
kernel with continual skills, MCP 2.0 (spec 2026-07-28), permissions, hooks, and
the TUI/REPL. Claude Code ecosystem compatibility is landing in waves —
instruction files, settings, plugins, skills, agents, and slash commands are in;
remaining waves are tracked in
[docs/CLAUDE_ECOSYSTEM_COMPAT_PLAN.md](docs/CLAUDE_ECOSYSTEM_COMPAT_PLAN.md).
