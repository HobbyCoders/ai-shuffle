# omni

A multi-provider AI agent harness. Headless, event-driven core; sessions as branching
trees; cache-first context management; classic JSON tool-calling plus a code-as-actions
execution layer; MCP 2.0 (spec 2026-07-28) native.

**Provider backends**

| Backend | Mode | Auth |
|---|---|---|
| Anthropic Messages API | native loop | API key |
| OpenAI Responses API | native loop | API key or Codex/ChatGPT-subscription OAuth |
| OpenRouter / OpenAI-compatible | native loop | API key |
| Claude Pro/Max subscription | delegated (`claude -p` stream-json subprocess) | official CLI's own OAuth |

Claude subscription traffic runs the official `claude` CLI as a subprocess backend —
the explicitly permitted path — never raw OAuth token reuse.

## Design pillars

- **Cache-first**: stable append-only prompt prefix, explicit cache breakpoints,
  `cache_hit_rate` as a first-class metric. Nothing mutates the prefix mid-session.
- **Event-driven headless core**: engine consumes Ops, emits a seq-numbered, replayable
  Event stream. TUI, server, and embedding apps are just subscribers.
- **Sessions are trees**: fork at any node; rewind is cache-preserving.
- **Prevention over lossy compaction**: cache-safe tool-result trimming, structured
  handoff, external notes — not cumulative summarization.
- **Code-as-actions**: tools projected as typed Python functions into a persistent,
  permission-gated kernel; schemas stay out of the prompt (progressive disclosure).
  The kernel blocks network *and* direct file I/O (files go through gated tools;
  large results stay in-kernel behind a `ResultRef`), and its memory cap is
  enforced on POSIX (rlimit) and Windows (Job Object) alike.
- **Continual harness**: every executed cell is journaled; a background synthesis
  pass distills recurring workflows into persistent Python skills (mounted in the
  kernel as `skills.<name>`, saved via the gated `save_skill` tool, usage-counted
  for curation). Enable with `--continual` / `OMNI_CONTINUAL=1`; skills live in
  `~/.omni/skills` (override with `skills_dir` / `OMNI_SKILLS_DIR`).
  Design rationale: [docs/agent-experience.md](docs/agent-experience.md).

Status: early development (M0).
