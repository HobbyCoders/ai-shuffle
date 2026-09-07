# Hermes Agent harness study — and a plan for fluid

Research date: 2026-08-23. Sources: the Hermes Agent source tree
(github.com/NousResearch/hermes-agent, shallow clone at HEAD), its public docs
(hermes-agent.nousresearch.com/docs), and a full audit of fluid's own context,
tool, and engine pipeline. File references like `agent/system_prompt.py:340`
point into the Hermes repo unless prefixed with `fluid/`.

Ground rule for the plan (per the request that motivated this study): we learn
the **function** of each Hermes mechanism, not its names or surface features.
Nothing below proposes importing `SOUL.md`, Hermes wording, or Hermes naming —
each recommendation restates the job to be done and proposes a fluid-native
design for it.

---

## Part I — Why Hermes works (the short version)

Hermes's standing (top harness by traffic on OpenRouter) is not one feature.
Reading the code, four compounding disciplines explain it:

1. **Any-model competence.** Hermes is aimed at the whole OpenRouter model
   population, not one lab's flagship. A large fraction of its prompt
   machinery exists to make mid-tier and open-weight models *finish jobs*:
   model-family-gated guidance blocks (tool-use enforcement, execution
   discipline, edit-format steering), schema sanitization for llama.cpp
   grammars, argument coercion for models that return `"5"` for `5`,
   multimodal downgrade ladders for providers that reject list content.
   The harness absorbs model weaknesses so the user doesn't feel them.
2. **Prompt-cache discipline as a stated invariant.** The system prompt is
   built once per session in three stability tiers, rebuilt only at
   compaction; memory is a frozen snapshot; the timestamp is date-only;
   ephemeral injections are stamped onto a replayable sidecar so turn N+1
   resends turn N's exact bytes. Cache breaks are enumerated and "sanctioned."
3. **A closed learning loop with consent boundaries.** Turn-counted nudges
   spawn a background review fork that writes memory and skills; a weekly
   curator consolidates the library; everything is archive-not-delete,
   pinned-is-untouchable, and visible/undoable by the user.
4. **Tool descriptions that police the tool surface.** Descriptions don't just
   describe — they route ("do NOT use cat, use read_file"), explain mechanisms
   ("the pipe makes exit_code report tail's status, masking real failures"),
   and prevent wasted turns ("verified:true means the hash was confirmed — do
   NOT re-read the file").

fluid already matches or beats Hermes on several fronts (append-only cache
plan, a *lossless* mechanical eviction ladder vs. Hermes's LLM summarizer,
code-mode with `ResultRef`, self-testing synthesized skills, a stronger
permission engine). The gaps are concentrated in: prompt layering and
model-adaptivity, workspace grounding, tool-result hardening, declarative
memory, and learning-loop reach. Part IV maps all of it.

---

## Part II — The Hermes system prompt, in depth

### 1. Assembly: three cache tiers, built once

`agent/system_prompt.py:340-900` (`build_system_prompt_parts`) returns three
strings joined with `\n\n`:

| Tier | Contents (in order) | Stability |
|---|---|---|
| **stable** | identity (persona file or default) → self-help pointer → task-completion guidance → parallel-call guidance → tool-gated guidance (memory/skills/session-search/kanban) → steer-channel note → computer-use brief → subscription capabilities → model-family enforcement blocks → coding operating brief → environment hints → python-toolchain probe line → profile hint → platform hint | Identical across sessions on the same install |
| **context** | live workspace snapshot → caller `system_message` → project context files (AGENTS.md chain etc.) | Stable per project/session |
| **volatile** | skills index → memory snapshot → user profile snapshot → external memory block → plugin sections → date-only timestamp + session/model/provider lines | Most likely to differ across rebuilds; kept last so the prefix above survives |

Design rules stated in the code:

- Built **once per session**, cached, rebuilt only after context compression.
  "Hermes never re-renders parts of this string mid-session — that's the only
  way to keep upstream prompt caches warm across turns."
- Within the volatile tier, the **most-mutable content is ordered last**. The
  skills index (mutated when the agent saves/patches a skill) sits at the
  *front* of the volatile band so on longest-prefix backends an unchanged
  index still lands in the reused prefix and a changed one only re-prefills
  from that point.
- The timestamp is **date-only** with timezone + UTC offset (both constant for
  a day), because minute precision invalidates prefix-cache KV on every
  rebuild path. Eternal sessions (long-lived bot channels) drop the date
  entirely — "a birth date pinned in a session that lives for months is
  misinformation."
- The static prefix is **reconstructed and verified with `startswith`** on
  session restore; if any input changed since the prompt was persisted, the
  system falls back to the stored bytes untouched, never a rewritten prompt
  (`agent/system_prompt.py:944-995`).
- Plugin-contributed sections are frozen at first render, framed with a
  length-stamped marker, and can be **recovered byte-exactly from the
  persisted prompt** on resume without re-running plugin code
  (`agent/system_prompt.py:190-259`).

### 2. Slot #1: the identity layer

The very first block is a user-owned persona file loaded raw — "no wrapper
language is added around it" — from the harness home, never the project
directory. If absent/empty, a hardcoded default identity takes the slot
(`hermes_cli/default_soul.py`):

> "You are Hermes Agent, an intelligent AI assistant created by Nous Research.
> You are helpful, knowledgeable, and direct. … You communicate clearly, admit
> uncertainty when appropriate, and prioritize being genuinely useful over
> being verbose unless otherwise directed below. Be targeted and efficient in
> your exploration and investigations."

What the mechanism actually does, stripped of its name:

- **Separates "who the agent is" from "how the agent must operate."** The
  persona file carries durable voice — tone, directness, how to handle
  uncertainty/disagreement — and is explicitly *not* the place for project
  instructions, paths, or workflow rules (those belong to the project
  instruction files). Docs doctrine: "if it should follow you everywhere, it
  belongs in the identity file; if it belongs to a project, it belongs in
  AGENTS.md."
- **User-ownable without forfeiting the harness's operating rules.** Editing
  the persona never removes tool guidance, safety blocks, or coding brief —
  those are separate stable-tier blocks below it. Contrast fluid today: the
  only way to change fluid's voice is to replace the *entire* base prompt
  (`config.system`), which throws away the whole behavioral contract.
- **Defense-in-depth on the way in**: the file is scanned by the same
  threat-pattern library used for project context files and memory; a match
  replaces the content with a `[BLOCKED: …]` placeholder rather than letting
  it into the prompt. Oversize content is head/tail-truncated with a marker
  telling the model the exact path to `read_file` for the rest.
- There is also a session-level overlay command for temporary personality
  switches that does not touch the durable file.

### 3. Conditional guidance: inject only what applies

The most transferable idea in the whole prompt builder: **every guidance block
is gated on whether its subject actually exists in this session.**

- Tool-gated: memory guidance only when the memory tool is loaded *and* its
  store is enabled (with a narrower variant when only the user-profile half is
  on); skills guidance only with the skill-management tool; session-search
  guidance only with that tool; the multi-paragraph computer-use brief only
  with that toolset; the steer-channel note only when tools exist at all
  (steers ride tool results). A companion mechanism in `model_tools.py`
  (§ II.8) even performs *surgery on tool descriptions* to delete mentions of
  absent tools — "otherwise the model sees tools mentioned in descriptions
  that don't actually exist, and hallucinates calls to them."
- Model-family-gated (`agent/prompt_builder.py:357-587`), each controlled by
  a config knob (`auto`/`true`/`false`/substring-list) resolved once at
  session start so the prompt stays byte-stable:
  - **Tool-use enforcement** for `gpt, codex, gemini, gemma, grok, glm, qwen,
    deepseek`: "You MUST use your tools to take action — do not describe what
    you would do … Never end your turn with a promise of future action —
    execute it now."
  - **Execution discipline** for `gpt, codex, grok, deepseek, kimi, qwen, glm,
    minimax, mimo, mistral` — an XML-sectioned block (`<tool_persistence>`,
    `<mandatory_tool_use>`, `<act_dont_ask>`, `<prerequisite_checks>`,
    `<verification>`, `<external_state_verification>`,
    `<literal_preservation>`, `<missing_context>`) targeting failure modes
    observed in eval traces: arithmetic in prose, no read-back after external
    writes, "repairing" malformed identifiers, claiming completeness despite
    count mismatches. **Claude is deliberately excluded** — "it does not
    exhibit these failure modes."
  - **Operational directives for Gemini/Gemma** (absolute paths, verify
    before edit, dependency checks, conciseness, non-interactive flags).
  - **Edit-format steering** in the coding brief: GPT/Codex are nudged to a
    V4A diff mode ("the edit format you handle most reliably" — their
    first-party harness never taught them str-replace), everyone else to
    find-and-swap. Unknown families get neutral wording.
- Universal but toggleable blocks, applied to all models because the failure
  modes aren't family-specific:
  - **Task completion / anti-fabrication**: "the deliverable is a working
    artifact backed by real tool output — not a description of one … NEVER
    substitute plausible-looking fabricated output … Reporting a blocker
    honestly is always better than inventing a result." (Motivated, per the
    comment, by an observed Opus stub-and-stop and a DeepSeek fabrication on
    the same real task.)
  - **Parallel tool calls**: batch independent reads into one turn; the
    comment quantifies the cost model (every extra round-trip resends the
    conversation) and notes the runtime already executes independent calls
    concurrently — "the missing piece was telling the *model*."

### 4. The coding posture: auto-detected operating brief + workspace snapshot

`agent/coding_context.py` resolves a frozen "runtime mode" once per session:
coding vs. general, detected from surface (CLI/TUI/desktop/editor — never chat
platforms) plus cwd signals (project manifests, or a git repo that actually
contains code files; a dotfiles repo at `$HOME` and stray manifests in `/tmp`
are explicitly excluded). When active it injects:

1. **An operating brief** (~60 lines) written as a senior-engineer contract:
   gather context first (trace symbols, don't guess; batch independent
   lookups; never invent files/APIs; check the manifest before assuming a
   library), make changes through tools not chat ("Do NOT print code blocks to
   the user as a substitute for editing"), re-read before retrying a failed
   patch and rewrite the region after two failures, verify with the project's
   own tests/linters before claiming done, fix root causes ("check sibling
   call paths for the same flaw and fix the class, not just the reported
   site"), stop after ~3 lint-fix attempts on one file and ask, respect the
   repo (no commit/push unless asked, leave `.env` alone).
2. **A live workspace snapshot**, built once at prompt build and placed behind
   its own cache boundary:

   ```
   Workspace (snapshot at session start — re-check with `git` before acting on it):
   - Root: /path
   - Branch: main → origin/main (ahead 2)
   - Status: 3 modified, 1 untracked
   - Recent commits: …
   - Project: pyproject.toml, package.json (uv/pnpm)
   - Verify: pytest; pnpm run test; make lint
   - Context files: AGENTS.md
   ```

   The **Verify line is the sleeper feature**: manifests, lockfile→package-
   manager mapping, `package.json` scripts, Makefile targets, and pytest
   config are sniffed once so the model gets its verification loop handed to
   it instead of rediscovering it every session. The same structured facts
   feed the desktop UI, so prompt and UI can't drift.
3. An opt-in "focus" mode that additionally collapses the toolset to the
   coding set and demotes non-coding skill categories to names-only in the
   index (never hidden — "agent-created skills are the model's project memory,
   and models don't reach for skills_list to rediscover what the index stops
   showing them").

Cache note repeated in the code: branch/dirty state drifts mid-session, so the
brief says to re-check with `git` rather than ever re-probing the prompt.

### 5. Environment hints: describe the machine the tools actually touch

`build_environment_hints()` (`agent/prompt_builder.py:1394-1500`):

- Local backend: host OS + user home + cwd, plus a Windows-only block warning
  that the terminal is git-bash not PowerShell — with concrete failure modes
  (MSYS path conversion disabled for native tools; a bare `\n` isn't Enter on
  a Windows PTY) rather than bare rules.
- Remote backends (docker/ssh/modal/…): host info is **suppressed** — the
  prompt describes only the sandbox, populated by a live one-line POSIX probe
  run inside the backend (`uname/home/cwd/user`), with a graceful fallback
  that tells the model exactly what probe command to run itself.
- A python-toolchain probe emits **one line only when something is
  non-default** (PEP-668, uv vs pip) — "emits NOTHING when the environment is
  clean (no token cost)."
- An embedder seam: an env var / config key lets whoever wraps the harness
  add environment description **without forking the identity slot**.

### 6. Project context files: provenance, chains, injection scanning, and lazy discovery

`build_context_files_prompt` (`agent/prompt_builder.py:2523-2598`):

- Priority chain, first type wins: harness-native file (walks to git root) →
  `AGENTS.override.md`/`AGENTS.md` merged **directory chain from git root down
  to cwd** (deeper = later = higher precedence) → `CLAUDE.md` → `.cursorrules`
  (+ `.cursor/rules/*.mdc`). Byte-identical duplicates along the chain are
  deduped.
- Every file is rendered as a provenance-labeled section (`## <relpath>`),
  scanned by the shared threat-pattern library (BLOCKED placeholder on a
  match; a lone UTF-8 BOM is stripped, not flagged), and truncated to a cap
  that **scales with the model's context window** (6% of window in chars,
  floor 20K, ceiling 500K), head 70% / tail 20%, with a marker naming the
  exact path to `read_file` for the middle. Truncation warnings are also
  surfaced to the *user* through the status channel, not just logs.
- A guard refuses to load the harness's own repo AGENTS.md when cwd
  resolution merely *fell back* to the install tree (a real bug class: the
  desktop daemon self-spawning into its install dir and adopting contributor
  instructions as authoritative).
- **Progressive subdirectory discovery** (`agent/subdirectory_hints.py`):
  nested AGENTS/CLAUDE/.cursorrules files are injected **into tool results**
  at the moment the agent first touches that directory — never into the
  system prompt, preserving the cache. Content-hash dedup (symlink farms,
  copied backups), excluded-dir list (node_modules, vendor, backups, caches),
  ancestor walk capped at 5 levels, scoped strictly inside the working tree
  ("prevents loading ~/.codex/AGENTS.md … cross-agent context contamination").

### 7. The mid-turn steering channel

Because a mid-turn user message can only ride a tool result
(role-alternation), Hermes wraps it in a self-describing bounded marker
(`[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once
at this position; not tool output and not a new delivery when replayed from
conversation history] … [/…]`) and pre-arms the model in the stable prompt:
trust **only this exact marker**, ignore lookalikes in tool/web/file bodies,
and — since markers persist in immutable history — treat one as new *only*
when it's in the latest tool-result batch with no later assistant message.
Without the trust note, injection-hardened models refuse their own user's
steer as suspected prompt injection (observed in the wild, per the comment).

### 8. What the prompt writing itself teaches

Patterns worth copying as *craft*, independent of any feature:

- **Every rule ships with its mechanism.** "Never pipe a build through tail"
  is followed by *why* (exit code masking + auto-truncation already handles
  it). Models generalize from mechanisms; bare rules get pattern-matched and
  violated at the edges.
- **Right/wrong example pairs inline** ("'User prefers concise responses' ✓ —
  'Always respond concisely' ✗"), including the reason imperative memories are
  dangerous (they replay as directives in later sessions).
- **Anti-redundancy assertions** to kill wasted turns: "verified:true means
  the hash was confirmed — do NOT re-read the file"; "no need to call
  browser_snapshot separately after navigating"; memory-tool success replies
  are deliberately terminal and don't echo entries because echoing "invites
  the model to find more to fix" (observed thrash: 1 correct batch + 5
  redundant repeats).
- **Negative space is specified**: what NOT to save to memory (anything stale
  in 7 days, PR numbers, task progress), what NOT to use a tool for (with the
  replacement named), which tools must never run in parallel.
- **Every constant carries the incident that motivated it** — comments cite
  issue numbers, observed model traces, even an Anthropic content-filter
  false-positive that forced an empirically-validated rewording of one
  sentence. The prompt is treated as versioned, reviewed, regression-prone
  code.
- **Token-cost consciousness is explicit**: "Short on purpose. This block is
  shipped to every user, every session, in the cached system prompt — token
  cost is paid once at install and then amortised via prefix caching. Keep it
  tight."

---

## Part III — Tooling, memory, and the learning loop (condensed findings)

### Tool system

- **Registry with availability probes**: every tool can register a `check_fn`;
  schemas are emitted only when it passes. Results are TTL-cached (30 s) with
  a 60 s last-good grace window so one flaky `docker version` probe can't
  strip the terminal toolset mid-session.
- **Dynamic schemas**: a tool may register a callable that rebuilds its
  description/params per request from live config (the delegation tool's
  description states the user's actual concurrency and nesting limits).
- **Post-filter description surgery**: after schema assembly, cross-references
  to unavailable tools are literally string-replaced out of descriptions, and
  embedded API catalogs (the code-execution tool) are regenerated to list only
  tools that exist.
- **Progressive disclosure** with budget tiers: deferred (MCP/plugin) tools
  are replaced by a search/describe/call bridge; the catalog embedded in the
  bridge description degrades from name+description → names-only → per-server
  summaries as it exceeds ~5% of the context window. Wording preempts the
  failure mode: "If a tool name appears here, do NOT claim it is unavailable."
  Core tools are never deferred. Dispatch resolves the *real* tool name so
  hooks/permissions see through the bridge.
- **Result overflow, three layers**: per-tool caps inside tools → per-result
  spillover to a disk file (in-context replaced by a `<persisted-output>`
  block with a preview and "page through the saved file with read_file — do
  NOT re-request the same data from the remote API") → a per-turn aggregate
  budget (200K chars) that spills the largest results until the turn fits.
  MCP tools get a tighter default cap (50K) because servers return
  unpaginated blobs that "sail under the generic threshold."
- **Untrusted-content fencing**: web/browser/MCP results ≥32 chars are wrapped
  in a labeled data-not-instructions block; embedded closing tags are
  defanged case-insensitively; there is deliberately no "already wrapped"
  fast-path because that check is attacker-forgeable.
- **Elision detection**: results containing provider-side truncation markers
  (`"has_more": true`, `…N more items`) get one appended notice — "The data
  shown is INCOMPLETE — page/fetch the remainder before treating any
  enumeration as complete" — placed *inside* the trust fence, at
  message-construction time so caching stays safe.
- **Error hygiene**: all tool errors are bounded (2048 chars), and exception
  text is scrubbed of role tags / code fences / CDATA before entering context,
  prefixed `[TOOL_ERROR]` — not because framing can escape JSON, but because
  the model still *reads* those tokens.
- **Argument coercion**: string→int/float/bool/JSON against the declared
  schema ("LLMs frequently return numbers as strings"), originals preserved on
  failure.
- **Multimodal downgrade ladder**: vision-capable+list-accepting → pass
  through; provider rejects list content → text summary, with the lesson
  memoized per (provider, model) and a retroactive-repair path on 400s;
  no vision → text summary or an explicit actionable error.

### Skills

- Markdown skills with frontmatter: name (≤64), description (index truncates
  at ~57 chars — the docs *teach authors to design for the truncation
  window*), platform gates, and **conditional activation** (`requires_tools`,
  `fallback_for_tools` — a fallback skill hides itself when the primary tool
  is present).
- The index header is maximalist by design: "Skills (mandatory) — Before
  replying, scan the skills below… you MUST load it… Err on the side of
  loading… load them even for tasks you already know how to do, because the
  skill defines how it should be done here."
- Usage telemetry in a sidecar (not frontmatter, to avoid merge pressure),
  states active→stale(30d)→archived(90d) with pin as an opt-out, and a
  never-delete-only-archive rule.
- Repeat-view dedup: re-loading an unchanged skill returns a stub (a real
  session was measured wasting ~286K tokens on verbatim repeat loads),
  cleared on compaction so post-compaction reloads return full content.
- Compaction safety protocol: pruned skill bodies leave `[SKILL_PRUNED]`
  markers; the prompt teaches reload-before-acting and ignore-stale-markers,
  and the markers are re-injected deterministically after summarization
  because LLM summaries paraphrase them away.

### Memory

- Two small char-bounded files (agent notes ~2200 chars; user profile ~1375),
  entries `§`-delimited, injected in the volatile tier as a **frozen
  session-start snapshot** with a header showing capacity:
  `MEMORY (your personal notes) [67% — 1,474/2,200 chars]` — the model sees
  its own fill level and consolidates proactively. Mid-session writes are
  durable on disk but never touch the prompt (cache).
- Writes are atomic batches; the char limit is checked on the *final* result
  so one call can remove stale entries and add new ones even when the add
  alone would overflow. A drift guard refuses to write when the on-disk file
  wouldn't round-trip the parser (protects against concurrent sessions).
- Guidance ontology, stated twice: memory = "who the user is and what the
  current situation is"; skills = "how to do this class of task for this
  user"; transcripts = "what happened" (recalled via cross-session FTS5
  search, with prompt guidance to search before asking the user to repeat
  themselves). Declarative facts, never imperatives; nothing that will be
  stale in 7 days.
- Snapshot entries are threat-scanned at build time (a poisoned entry
  persists across sessions until removed, so it's blocked in the snapshot but
  left visible on disk for the user to delete).

### The learning loop

Two clocks:

- **Fast (per-session)**: counters (10 user turns / 10 tool iterations,
  hydrated from persisted history so restarts can't reset them) spawn a
  background review **fork** of the agent: same session id and cached prompt
  prefix (reducing repeated prompt work), but persistence disabled,
  compression off, memory-provider off, iteration-capped. Its prompts are
  remarkable: the skill-review prompt declares "a pass that does nothing is a
  missed learning opportunity," makes *user frustration signals* first-class
  skill triggers ("stop doing X," "why are you explaining"), imposes a strict
  preference order (update loaded skill → update umbrella → add reference
  file → only then create), and has an explicit do-NOT-capture list
  (environment-dependent failures; negative tool claims that "harden into
  refusals the agent cites against itself for months"; unresolved failures
  written up as validated workflows).
- **Slow (weekly, idle-triggered, no daemon)**: a curator pass on the
  auxiliary model. Deterministic phase first (stale/archive transitions,
  pin/cron-referenced skips, grace floor for never-used skills), then an
  opt-in LLM consolidation pass whose prompt targets library *shape*
  ("hundreds of narrow skills where each captures one session's bug is a
  FAILURE of the library"), forbids deletion, requires a machine-readable
  YAML summary reconciled against actual tool calls, and tars a backup before
  running.
- Self-prompting surfaces (heartbeat/recurring loop/goal continuation) all
  share one invariant: **the injection is a plain user-role message** — never
  a system-prompt mutation or toolset swap — so caching and role alternation
  survive. Each template ends with an anti-busywork clause ("If there is
  nothing meaningful to do… reply briefly that nothing has changed and stop —
  do not invent work").

### Compaction and caching

- LLM summarization with deterministic backstops: a mechanically-extracted
  **anchor index** (SHAs, PRs, branches, files, error strings — "no LLM in
  the loop, so nothing can be paraphrased away"), verbatim user-message
  preservation ("compact the derived material, keep the source of truth"),
  and verbatim-quoted security constraints.
- The re-injection wrapper is the most defensive text in the codebase: the
  summary is REFERENCE ONLY, the latest user message wins, reverse signals
  ("stop," "never mind") kill in-flight work, and topic overlap does not mean
  resume.
- Anthropic cache markers: 4 breakpoints (static system prefix, system end,
  last 2 legal transaction endpoints — never mid tool-call pair), idempotent
  application, TTL clamped per provider that silently drops 1h markers.
- The system prompt is persisted (deduped by hash) and restored verbatim so a
  fresh process rebuilds the same cache prefix; ephemeral per-turn injections
  are stamped onto an `api_content` sidecar so replays are byte-identical
  ("what turn N sends must be what turn N+1 replays").

---

## Part IV — What this means for fluid

### Where fluid is already at parity or ahead

Keep these; several are genuine advantages worth defending in docs/benchmarks:

- **Append-only prefix + explicit cache plan** (`fluid/context/cache_plan.py`)
  is the same invariant Hermes enforces, in ~36 lines instead of scattered
  discipline. Fork/rewind moving only the head pointer is something Hermes
  doesn't have at all.
- **Mechanical, lossless eviction** (trim → digests → epoch stubs, with
  `HistoryExpand`/`HistorySearch`) is philosophically stronger than Hermes's
  LLM summarizer: Hermes needed the anchor index, marker re-injection, and a
  paragraph of "REFERENCE ONLY" armor precisely because summarization loses
  and distorts; fluid's view-transform design never loses anything. Hermes's
  machinery is the cautionary tale that validates fluid's choice.
- **Code mode + `ResultRef`** solves the tool-result-bloat problem earlier in
  the pipeline than Hermes's disk spillover (the data never enters context at
  all), and collapses N round-trips into one cell where Hermes can only ask
  the model to batch calls.
- **Skill verification** (`_selftest` in a throwaway kernel, broken skills
  unmounted and flagged) is stricter than anything Hermes does; Hermes skills
  are prose and unverifiable by construction.
- **Permissions** (risk classification with a provable read-only Bash
  classifier, scoped allow-always, doom-loop guard, actionable denials) are
  more principled than Hermes's approval model.
- The base prompt's **anti-workaround and injection stances** are already
  Hermes-grade prose.

### The plan

Grouped in four phases, roughly by leverage-per-effort. Each item names the
Hermes mechanism it learns from, the function to reproduce, and the
fluid-native shape. No Hermes names, no copied wording — when a block of
prompt text is called for, we write our own to serve the stated function.

#### Phase 1 — Prompt layering and model adaptivity (highest leverage)

**1.1 Split the base prompt into gated blocks.**
*Learns from:* Hermes's tool-gated guidance + "never mention a tool that
doesn't exist."
*Problem in fluid:* `DEFAULT_BASE_PROMPT` is one static string that
unconditionally references Note, Ledger, Skill, HistoryExpand, and the
system-reminder conventions. A session with `allowed_tools` restrictions, a
code-mode session, or an embedder that disables skills ships prose about
capabilities that don't exist — the exact hallucinated-call risk Hermes
engineers against.
*Design:* keep `fluid/cli/prompt.py` as an ordered list of `(condition,
block)` pairs — identity, communication, doing-the-work, tools policy,
permissions, instructions/skills/memory, context recovery, environment
framing, ending-a-turn — where each condition is a pure function of the
session's frozen tool surface and config. Resolve once at session build
(cache-safe by construction; conditions can't change mid-session). The
compiler already supports this shape via `extra_blocks`; the change is
factoring the constant. Add a unit test asserting every tool name mentioned
in the compiled prompt exists in the session's tool registry — that single
test also catches description/prompt drift (the class of bug behind the
Bash "Git discipline" incident).

**1.2 An identity layer above the contract, separable from it.**
*Learns from:* the persona-in-slot-1 mechanism.
*Problem in fluid:* voice/persona can only be changed by replacing the whole
base prompt, losing the behavioral contract.
*Design:* a fluid-native identity source — recommend a `[identity]` config key
plus an optional `~/.fluid/IDENTITY.md` (name open; anything but the Hermes
name) — compiled as the **first** section, above the behavioral contract. When
absent, the current four-sentence fluid header is the default identity block.
Document the doctrine in fluid's own words: durable voice and style live in
the identity file; project rules live in AGENTS.md; the operating contract is
the harness's and survives both. Apply the same treatment as other
instruction files: size cap and (per 2.4 below) injection screening.
Explicitly *not* re-loaded mid-session (cache).

**1.3 Model-family guidance, config-gated.**
*Learns from:* the enforcement/discipline blocks and their gating machinery.
*Problem in fluid:* fluid's stated ambition is provider-agnostic — OpenRouter,
local llama.cpp, small models via the text tool-call protocol. That is
precisely the population these blocks were written for; fluid currently ships
the same prompt to Claude and to a 7B local model.
*Design:* add a `model_guidance` resolution step in `fluid/cli/prompt.py`
(config: `auto` / `true` / `false` / list of model substrings, resolved once
per session). Write two fluid-authored blocks:
  - *act-don't-narrate* (for families that describe instead of calling tools,
    and stop after stubs);
  - *execution discipline* (tool persistence; compute-don't-recall for
    arithmetic/hashes/dates; read-back verification after external writes;
    preserve identifiers literally; completion = every criterion verified).
Ship a default substring table, and let config extend it. Keep fluid's
existing communication style rules universal. This is likely the single
biggest quality lever for fluid's non-Anthropic legs, and it costs nothing on
providers that don't need it.

**1.4 Workspace snapshot with a verify line.**
*Learns from:* the coding-posture workspace block + project-facts probe.
*Problem in fluid:* the environment block names shell/OS/cwd but nothing about
the project; the model re-derives "how do I run the tests here" every session.
*Design:* extend `fluid/context/environment.py` with a one-shot, bounded probe
at session build: git branch/upstream/ahead-behind/dirty counts/last 3
commits (with a "snapshot at session start — re-check before relying on it"
caveat in fluid's words), detected manifests, lockfile→package-manager, and
verify commands (test/lint/build from package.json scripts, Makefile targets,
pytest config, `scripts/run_tests.sh`). Hard timeout on the git calls,
byte-stable output, lives in the compiled prompt's environment section. Also
add a **date-only** date line (fluid currently injects no date at all; models
guess). Follow Hermes's reasoning exactly: date + timezone only, never
minutes.

**1.5 Instruction-file caps with recovery pointers.**
*Learns from:* context-file truncation design.
*Problem in fluid:* the instruction stack has no size cap; a bloated AGENTS.md
chain silently eats the window (fluid's intake stubs only cover user
attachments).
*Design:* cap each instruction section at a size scaled to the configured
context window (floor ~20K chars), head-biased head/tail split, and a marker
in fluid's voice naming the exact path to `Read` for the elided middle — the
same "recoverable, never silently lossy" principle fluid already applies
everywhere else. Surface a warning event so the CLI can tell the user.

**1.6 Coding operating brief.**
*Learns from:* the coding brief's content, not its posture machinery (fluid is
already a coding-first harness; no detection needed).
*Design:* fluid's "Doing the work" section already covers scope, finishing,
and verification. Fold in the missing, demonstrably useful rules, written
fresh: trace-symbols-don't-guess; check the manifest before assuming a
dependency; re-read after a failed Edit and rewrite the region after repeated
failures (fluid's Edit description covers the retry but the escalation rule
belongs in policy); fix the class not the site; ~3-attempt cap on lint loops
before asking. Keep it in the gated-blocks structure from 1.1.

#### Phase 2 — Tool-surface hardening

**2.1 Untrusted-result fencing for MCP and future web tools.**
*Learns from:* the untrusted-wrap + delimiter-defang design.
fluid's prompt states the data-not-instructions policy, but policy prose is
weaker than per-result framing. Wrap MCP tool results (and any web tool fluid
grows) above a small size floor in a fluid-native fence with a one-line data-
not-instructions label; defang embedded closing tags case-insensitively; no
"already wrapped" fast path. Implement at the dispatch boundary in
`fluid/tools/registry.py` so both JSON mode and code-mode host calls get it.
Note the interaction with code mode: the fence belongs on what enters
*context* (the cell's surfaced output), not on the in-kernel value.

**2.2 Elision-marker notice for MCP results.**
Scan MCP results for provider-side truncation markers (`has_more`, `…N more`)
and append one bounded notice inside the fence. Cheap, targets a real
false-completeness failure mode.

**2.3 Tool-error scrubbing and bounding.**
Bound all error text at the dispatch boundary and strip role tags/code
fences/CDATA from exception strings before they enter context. fluid already
routes exceptions into `is_error` results; this is a small filter on that
path.

**2.4 Injection screening for instruction files.**
*Learns from:* the shared threat-pattern scan over persona/AGENTS/memory.
fluid injects AGENTS.md (and per 1.2, an identity file) verbatim. Add a small
pattern screen at `fluid/context/instructions.py` load time — block-with-
placeholder on a match, warning surfaced to the user. Keep the pattern set
conservative (context scope only; don't flag security-research repos).

**2.5 Argument coercion in dispatch.**
Schema-guided string→int/float/bool/JSON coercion before validation, originals
kept on failure. Disproportionately valuable for fluid's local-model leg.

**2.6 Description craft pass over the builtin tools.**
Apply the patterns from Part II §8 to fluid's 14 descriptions:
reciprocal anti-substitution routing with mechanisms (Bash ↔ Read/Grep/Glob —
including fluid's own version of the pipe-through-`head` exit-code trap, which
fluid's 30KB cap makes real); USE-FOR/NOT-FOR blocks with named replacements
on `Task` and `Monitor`; extend the existing anti-redundancy lines (Edit has
one; give Write and Monitor theirs); self-report skepticism on `Task`
("a child's summary is a claim, not a verified fact — verify external side
effects yourself"). Fluid's descriptions are already good; this is an editing
pass, not a rewrite.

**2.7 Tiered deferred-tool catalog.**
fluid's ToolSearch reminder lists deferred names. Adopt the budget ladder:
name+description listing while it fits a small window fraction; names-only
past that; per-server one-liners past that; and add the one sentence that
kills the worst failure mode — a tool listed as deferred must never be
declared unavailable.

**2.8 Immediate stubbing of oversized tool results.**
*Learns from:* the per-turn aggregate budget.
fluid's trim ladder is batch-triggered at 120K estimated tokens; a single
pathological turn (several 200KB results) can overflow before any trigger
fires. Extend fluid's intake-stub mechanism (currently user-attachments only)
to tool results above a per-result cap: stateless, immediate, preview +
`HistoryExpand` pointer — mechanically identical to what fluid already does,
just applied at result-append time. This keeps fluid's "prevention over
compaction" story honest under adversarial result sizes.

#### Phase 3 — Memory and the learning loop

**3.1 Bounded declarative memory with a frozen snapshot.**
*Learns from:* the two-file bounded store; deliberately *not* its external
provider zoo.
fluid has procedural memory (Python skills) but no fact store — named as a gap
in fluid's own `docs/agent-experience.md`. Design, fluid-native:
- One or two small char-capped stores under `~/.fluid/` (agent facts; user
  preferences), entry-delimited, edited via one batched tool whose limit is
  checked on the final state (remove+add in one call).
- Injected as a session-start **frozen snapshot** in the compiled prompt's
  tail (after skills/agents, before environment or at the very end — most
  mutable last), with a capacity header (`[62% — n/N chars]`) so the model
  self-curates.
- Guidance block (gated on the tool per 1.1) written fresh but serving the
  same functions: declarative facts not imperatives (with a right/wrong
  pair); nothing that stales in a week; procedures go to skills; the best
  entry is the one that stops the user repeating themselves.
- Mid-session writes are durable but never touch the prompt; the snapshot
  refreshes next session. Threat-screen entries at snapshot build.
This composes cleanly with Ledger (in-session pinned state) — memory is the
cross-session sibling, and the Ledger description should say so.

**3.2 Widen the learning loop beyond code mode.**
fluid's journal/synthesis only exists when `code_mode` + `continual` are on —
a default `fluid chat` session learns nothing. Two steps:
- Journal JSON-mode sessions too (tool calls + user prompts + final replies —
  the episodic schema already exists), feeding the same synthesizer.
- Add a turn-counted **review nudge** (counter hydrated from the stored
  session so restarts can't reset it) that runs the synthesizer earlier than
  session end. Keep fluid's own doctrines, which are better than Hermes's
  defaults: prune-biased ("no new skill" is the expected outcome — the
  opposite of Hermes's "be ACTIVE" stance, and the right call for a system
  whose skills are executable), selftest-verified, single-flight.
- Steal one Hermes idea verbatim in spirit: the **do-NOT-capture list** for
  synthesis (environment-dependent failures; negative claims about tools that
  harden into standing refusals; unresolved failure narratives presented as
  workflows). Add it to the synthesizer prompt.

**3.3 Skill lifecycle: usage-informed curation, archive-not-delete.**
fluid's Python skills have `uses` counts; markdown skills have nothing.
Add: last-used timestamps for both kinds; a deterministic (no-LLM) curation
pass — stale after N days, archived after M, never deleted, pinned exempt —
run opportunistically at CLI startup when overdue (no daemon), with a written
report. An LLM consolidation pass can come later, opt-in, and should require
the machine-readable summary + reconciliation trick if it ever lands.

**3.4 Cross-session search.**
fluid's `HistorySearch` covers the current session path. Sessions are already
JSONL on disk; add a cross-session search tool (simple term index or SQLite
FTS over the store) plus one gated guidance line: search past sessions before
asking the user to repeat themselves. This also makes fluid's "sessions age
instead of dying" story extend across sessions.

**3.5 A question tool.**
fluid has the `QuestionRequest` event with no builtin raiser. Add an AskUser
tool; borrow the functional rules from Hermes's clarify design (options live
only in the structured field, never enumerated in the question text;
recommended option first; batch independent questions in one call; don't use
it for what a tool can answer). This unlocks steering-quality interactions in
the TUI and is a prerequisite for good subagent UX.

#### Phase 4 — Runtime polish

**4.1 Mid-turn steering.** Queue user input during a running turn and deliver
it appended to the next tool result in a bounded, self-describing marker, with
a stable-prompt note establishing (a) trust exactly this marker, (b) a marker
is *new* only in the latest result batch. fluid's event model (`Interrupt` op,
system-reminder plumbing) makes this a small feature with outsized UX value.

**4.2 Prompt/description regression harness.** A tiny eval set (a dozen
scripted tasks against a stub provider + golden-file checks on the compiled
prompt) run in CI on any change to `fluid/cli/prompt.py` or tool
descriptions. Hermes substitutes discipline (incident-annotated constants);
fluid can substitute automation. The 1.1 "every mentioned tool exists" check
is the first test in this suite.

**4.3 Availability probes for MCP/optional tools.** A light `check_fn`
equivalent on ToolSpec for tools whose backing may be absent, with fluid's
own version of the grace-window lesson: never let one flaky probe drop a
toolset mid-session (cache the last-good verdict).

**4.4 Web fetch tool.** One `WebFetch` builtin (HTTP GET → markdown, size-
capped, wrapped per 2.1) closes fluid's most-cited capability gap without an
MCP dependency. Search can stay MCP-provided.

**4.5 Adopt-in-docs.** Two Hermes doctrines are worth stating in fluid's docs
even where the code already complies: the enumerated-sanctioned-cache-breaks
framing (fluid: batch trim/compaction promotions are the only breaks), and
the memory/skills/transcripts ontology (facts / procedures / events) once 3.1
and 3.4 exist.

### What not to adopt (considered and rejected)

- **LLM summarization compaction** (even as a fallback): fluid's mechanical
  ladder + recall is strictly more recoverable; Hermes's own defensive
  apparatus around summaries is the argument against them.
- **Per-model-family *edit-format* switching**: fluid has one edit tool with
  one semantics; adding a diff-format mode purely to match GPT training would
  complicate the surface for a marginal win. Revisit only if OpenAI-leg edit
  failure rates demand it.
- **Micro-compaction**: Hermes's own docs recommend against it for exactly
  the cache reasons fluid is built on.
- **The external memory-provider plugin zoo**: eight integrations with a
  streaming scrubber to keep their tags out of the UI — heavy machinery that
  fluid's headless/embeddable design doesn't need. One good bounded builtin
  (3.1) first.
- **A skills-index "mandatory, err on the side of loading" posture**: right
  for Hermes's prose skills, wrong for fluid — fluid's skills are executable
  and self-testing, and over-loading is a real context cost. fluid's current
  "check the index; load when it covers the task" stance is better calibrated.
- **Probabilistic toolset distributions**: that's RL/data-gen tooling, not
  harness behavior.

### Suggested sequencing

| Order | Items | Rationale |
|---|---|---|
| 1 | 1.1, 1.3, 1.4 | Biggest quality lift for non-Anthropic models; all cache-safe; no new subsystems |
| 2 | 1.2, 1.5, 1.6, 2.6 | Prompt-layer completeness + description pass; mostly editing |
| 3 | 2.1–2.5, 2.8 | Hardening; small bounded diffs at the dispatch boundary |
| 4 | 3.1, 3.5 | First new capabilities: memory + AskUser |
| 5 | 3.2–3.4, 2.7 | Learning-loop reach and lifecycle |
| 6 | 4.1–4.4 | Polish, safety nets, web |

Every Phase-1 item preserves fluid's core invariant — the compiled prompt is
resolved once per session from frozen inputs, so nothing here introduces a
mid-session prefix mutation.

### Implementation status

All four phases were implemented on this branch (see the commit series
following this document):

- **Phase 1** — gated base-prompt blocks + mentioned-tools invariant test,
  identity layer (`identity` / `~/.fluid/IDENTITY.md`), model-family guidance
  (`model_guidance`), workspace snapshot with verify commands, instruction
  caps with Read-back markers, injection screening, coding-brief additions.
- **Phase 2** — untrusted-result fencing + elision notices for MCP results,
  error scrubbing/bounding, argument coercion, description craft pass,
  budget-tiered deferred catalog, immediate tool-result intake stubs
  (`context.tool_result_stub_chars`).
- **Phase 3** — bounded declarative memory (`[memory]`, `Memory` tool,
  frozen snapshot with fill percentage), `SessionSearch`, `AskUser` (riding
  the input-required round-trip), JSON-mode learning loop (`ToolJournal`),
  synthesizer do-not-capture list, deterministic skill curation
  (stale 30d / archive 90d / never delete / pinned exempt).
- **Phase 4** — `Steer` op + marker + prompt block, `WebFetch`/`WebSearch`
  (keyless, SSRF-guarded, fenced), availability probes with a last-good
  grace window, and the prompt/description regression harness
  (`tests/unit/test_prompt_regression.py`).
