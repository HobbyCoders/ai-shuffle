# Continual harness and memory study — HCL, EvoHarness-RL, and a plan for fluid + ai-shuffle

Research date: 2026-08-30. Sources: "Harness Continual Learning: Continual
Adaptation Beyond Model Parameters" (Kang et al., Nanjing University,
arXiv 2608.19013), "EvoHarness-RL" (Meta AI + UIUC, arXiv 2608.05446), and a
full audit of fluid's continual-learning pipeline plus ai-shuffle's memory
architecture. File references are into this repo unless prefixed
`ai-shuffle/`.

The prompt for this study: fluid has a form of continual learning that no
model has ever visibly used; memory in ai-shuffle is opaque even to its
owner; and the goal is frontier-leading memory plus a harness that evolves
itself so it does not need hand-modification every time a new model ships.

---

## Part I — What the papers actually contribute

### HCL (arXiv 2608.19013): the harness is the thing that learns

HCL's core move is to relocate continual learning from model weights to the
harness around a frozen model, and — critically — to treat the harness as
something that can *forget*. A prompt tweak, a new memory entry, or a new
skill can silently break behavior that used to work. They name this
**harness-level forgetting** and build the whole system around preventing it.

The harness state is four jointly-versioned components: a **task interface**
(prompts, templates, parsing rules), **experience memory** (raw interaction
buffers + LLM-abstracted summaries), a **capability map** (external tools +
skills consolidated from experience), and an **adaptive router** (what gets
selected into context per task). An LLM optimizer proposes candidate edits to
these components from execution evidence; a separate evaluator only
**commits** a candidate if it passes three gates:

1. **Current improvement** — measurably better on the current task's
   validation set (Δ ≥ δ).
2. **Historical retention** — replayed against a compact **anchor set** of
   previously solved cases; the number of anchors that regress must stay
   within a budget B. B=0 is strict stability, B=∞ is pure plasticity.
3. **Validity** — syntax, schema compliance, legal tool use.

Their strongest empirical finding: a *moderate* retention budget (B=1) beats
both strict and unrestricted settings — some willingness to lose old behavior
is optimal, but unguarded evolution overwrites reusable harness content.
Their ablation puts **memory as the highest-impact component** for both
acquisition and retention. Stated limitations: anchor replay gets expensive
as tasks accumulate, and they have **no consolidation** mechanism (merging
redundant memories, retiring obsolete skills).

### EvoHarness-RL (arXiv 2608.05446): the model participates, cheaply

EvoHarness organizes external state into **Belief / Progress / Experience**
(facts about the environment, subgoal status, cross-episode knowledge) and
gives the model four meta-actions: `track` (query belief), `commit` (write a
subgoal to a bounded progress list), `recall` (keyword retrieval over
experience, top-3 per category, usage-counted), and `note` (write an insight
to a **buffer**, not to the store).

The design detail that matters most for us is the **two-timescale write
path**: `note` writes never touch the experience store directly. They
accumulate in a buffer, and at epoch boundaries a separate consolidation
model processes the buffer against the current store, deciding per note to
ADD / UPDATE / REMOVE / SKIP. Capacity is capped (80 per category) with
least-frequently-used eviction driven by `recall` usage counts. So the model
*participates* in learning — it flags what seemed worth keeping while the
context was hot — but a curator with global view decides what persists. The
store is "refined through retrieval, consolidation, and forgetting," not
append-only.

Two results are directly relevant to frozen frontier models (their RL stage
is not):

- **Prompt-time BPE with no training at all** lifts frozen models massively:
  GPT-4.1 +22.1pp, GPT-5 +25.7pp, Claude Opus +2.1pp on ALFWorld. Explicit
  state externalization helps *any* model; it helps weaker models most. This
  is the empirical case that a good harness closes the gap between models —
  exactly the "don't modify the harness per model" property we want.
- **Harness annealing**: as the trained agent improves, scaffold use declines
  selectively — `recall` (cross-episode knowledge) persists while `commit`
  and `note` decay. Different models will lean on different parts of the
  scaffold to different degrees, which argues for offering the whole surface
  and letting usage patterns differ, rather than tuning the surface per model.

### Reading them together

The two papers are complementary halves of one architecture:

- EvoHarness answers **"how does knowledge get in and out during work"** —
  model-facing meta-actions, buffered notes, scored retrieval, usage-driven
  forgetting, epoch consolidation.
- HCL answers **"how does the harness itself change safely"** — guarded
  evolution with improvement/retention/validity gates over *all* harness
  state, including the prompts and routing rules, not just memories.

Each paper's gap is the other's contribution: HCL has no consolidation and no
model participation; EvoHarness has no retention gate protecting old behavior
from new "knowledge."

---

## Part II — Where fluid and ai-shuffle actually stand

### fluid: the learning loop works, and is invisible by design

The audit confirms the continual pipeline is real and reasonably well built —
and explains completely why no model is ever seen using it.

**What exists.** `fluid/codemode/continual.py` is a background synthesizer:
journal entries (executed cells, user prompts, assistant replies) accumulate
in `~/.fluid/skills/journal.jsonl`; once 8 new entries exist, a single-flight,
tool-less completion (`continual.py:243-255`) reads the journal slice plus
the current library and emits `<skill>` / `<memory>` / delete tags. Skills
are statically validated, then **verified by import + `_selftest()` in a
throwaway kernel** (`fluid/codemode/verify.py`) before landing. Memories are
capped at 40 × 500 chars, secret-screened, injection-screened at render, and
injected next session as a frozen `# Memory` snapshot. Deterministic curation
(stale 30d / archive 90d / pinned exempt / never delete) runs at engine
build. This is genuinely ahead of most harnesses.

**Why it is never seen.** Nine compounding reasons, ranked:

1. **Writes are architecturally not a model action.** The synthesizer is a
   separate out-of-band call; the session model's observable behavior is *no
   behavior*. Working as specified.
2. **The prompt forbids participation.** The one continual-gated bullet
   (`fluid/cli/prompt.py:493-500`) says memory "updates itself… you never
   manage it" and that a user's "remember this" "needs no action beyond
   acknowledging it."
3. **Off by default, twice** — `continual = False` and `code_mode = False`.
4. **`save_skill` is never in the JSON tool schema.** It registers with
   `source="skills"` (`fluid/codemode/session.py:566-604`) while the engine
   emits only `source=="codemode"` specs (`fluid/engine/native.py:1847-1854`).
   It exists only as a kernel stub inside `run_python`.
5. **Its only advertisement is one subordinate clause** mid-way through the
   ~1200-token `run_python` description; the code-mode addendum — the block
   written to teach code-mode behavior — never mentions skills at all.
6. **Terminology collision**: every "skill" bullet in the base prompt means
   prose `SKILL.md` files, a different subsystem.
7. **A `tools.allowed` list silently strips `save_skill`** from the kernel
   projection (the CE12 exemption covers only `codemode`-source specs).
8. **JSON-mode sessions can never import a synthesized skill** even when
   `continual` is on.
9. **Subagents get no memory** — `fluid/cli/host.py:373-383` passes no
   `memories_dir`.

**No retrieval.** Memories are a static whole-list dump each session; there
is no search over the learned store, no usage counting on memories, no
scoring. Cross-session recall (`SessionSearch`/`SessionView`) is a separate,
good subsystem.

### ai-shuffle: four things called "memory," two of them running

The ai-shuffle audit found four unrelated systems sharing the name:

- **A. Project memory dir** (`~/.fluid/projects/<slug>/memory/`, `MEMORY.md`
  index, `agent_memory_files` sync keyed by `link_group_id`): a wired read path
  without a corresponding write tool cannot fulfill its save-a-fact promise.
- **B. The continual learner** (`memories.json` in the harness skills root):
  maintains facts and a journal, but install-global storage lacks project
  scoping and a unified view or edit UI.
- **C. Sessions-as-memory**: `SessionSearch`/`SessionView` over PostgreSQL
  with trigram indexes and RECALL_BLOCK guidance, on the native leg.
- **D. The Memory tab** edits `AGENTS.md`: human-authored instructions,
  not a durable memory store.

**The leg problem dominates everything.** A Claude-family model with no
engine hint routes to the **delegated leg** — the Claude Code CLI subprocess
— and that leg gets *none* of the above: no recall tools, no learner, no
project memory, no sync (`ai-shuffle/app/core/memory_sync.py:410-439`
returns unless `leg == "native"`). Since Claude models are the default, the
primary use case sees zero ai-shuffle memory; the CLI brings its own, which
the app deliberately no longer touches.

**One dead wire with outsized cost:** fluid builds a `ToolJournal` for
JSON-mode continual learning (`fluid/config/loader.py:654-659`) and
`NativeLoop` accepts `journal=` — but ai-shuffle's `NativeLoop` construction
passes only `codemode=`. If code mode is off or fails to construct, the
learner silently stops writing while the model keeps seeing a frozen
`# Memory` snapshot.

---

## Part III — The gap analysis, paper-shaped

Scoring fluid+ai-shuffle against the combined architecture of the two papers:

| Capability | Papers | fluid/ai-shuffle today |
|---|---|---|
| Experience store with curation | Both (LFU eviction, ADD/UPDATE/REMOVE consolidation) | Partial: cap-40 archive on memories, deterministic skill curation; no usage counts on memories, no consolidation pass |
| Verified skill acquisition | HCL (validity checks) | **Ahead**: selftest-verified Python skills |
| Model participation in learning | EvoHarness (`note` → buffer → consolidation) | **Missing**: model told to never participate |
| Retrieval over learned state | EvoHarness (`recall`, scored, usage-counted) | **Missing** for memories/skills; good for sessions |
| Progress externalization | EvoHarness (`commit`, bounded subgoal list) | Present (Note/Ledger) but unconnected to learning |
| Retention gate on harness changes | HCL (anchor set, budget B) | **Missing**: synthesizer commits unconditionally |
| Harness-wide evolution (prompts, routing) | HCL (interface + router are versioned state) | **Missing**: guidance blocks are hand-written code |
| Frozen-model benefit without training | EvoHarness (+22pp prompt-time) | The opportunity |
| Two-timescale consolidation | EvoHarness (epoch boundary) | **Missing**: incremental synthesis only |
| Learning loop visible to the user | (neither paper, but ours needs it) | **Missing**: no UI, no events |

The single most important structural insight: **fluid resolved the tension
"model diligence is unreliable" by removing the model from the loop
entirely.** EvoHarness shows the correct resolution is a two-timescale
design — the model *proposes* through a cheap buffered action while the
context is hot, and a background curator *disposes*. Fluid already has the
curator; it amputated the proposal channel.

The second insight: **HCL's guarded evolution is the mechanism that delivers
"the harness doesn't need modification per new model."** Fluid's per-model
`model_guidance` blocks are hand-written code today. If model-specific
guidance, tool-description phrasing, and routing defaults become *data* that
the learning loop can propose edits to — gated by an anchor-set regression
check — then onboarding a new model is: run it, let the journal capture its
failure modes, let guarded evolution accumulate its guidance block. Fluid
already owns most of the gate infrastructure: selftest verification is HCL's
validity check, the prompt/description regression harness (Phase 4.2 of the
Hermes plan) is an anchor-eval runner waiting for anchors, and the session
store is a natural anchor source.

---

## Part IV — The plan

Three phases, ordered by leverage. Phase 0 costs days and fixes "I've never
seen it work"; Phase 1 is the frontier-memory build; Phase 2 is the
self-evolving harness. Every item preserves fluid's core invariants: the
compiled prompt is frozen per session (learned state lands next session, like
today's memory snapshot), and nothing mutates the prefix mid-session.

### Phase 0 — Make the existing loop reachable, honest, and visible

**0.1 A model-facing `Note`-to-journal channel (the EvoHarness `note`).**
Add a lightweight, always-cheap way for the model to flag an insight as
learning evidence. Concretely: extend the existing `Note` builtin (or add a
`Remember` tool) so entries marked as durable are appended to the continual
journal as first-class, high-salience entries the synthesizer prompt is told
to weight. Writes go to the buffer, never directly to `memories.json` — the
synthesizer keeps its ADD/UPDATE/REMOVE authority. This preserves the
"memory must not depend on model diligence" doctrine while ending the
model's forced silence.

**0.2 Rewrite the "never manage it" instruction.** Replace with: the memory
section curates itself, *and* when the user asks you to remember something or
you learn a durable fact, record it with `<the 0.1 tool>` — the learner will
consolidate it. A user's "remember this" currently being answered with a
no-op acknowledgment is the single worst UX in the system.

**0.3 Fix the `save_skill` surface.** Give it a real bullet in
`compose_code_mode_addendum` (procedure included: it is a kernel function,
`from tools import save_skill`); exempt `skills`-source specs from the
`tools.allowed` strip alongside the codemode exemption; and disambiguate
naming in the prompt (synthesized Python skills vs prose SKILL.md skills).

**0.4 Wire `ToolJournal` in ai-shuffle.** Pass `journal=` to `NativeLoop`
when continual is on and code mode is off or failed. One-line class of fix;
without it the learner is dead in exactly the degraded configurations where
it matters most.

**0.5 Emit learning events.** The synthesizer should publish events
(`skill-saved`, `memory-added/updated/retired`) on fluid's event bus so the
CLI can print a one-liner and ai-shuffle can toast it and render a memory
panel. Invisible learning is indistinguishable from no learning — this study
exists because of that.

**0.6 Resolve ai-shuffle's split-brain memory.** Two decisions to make
explicitly rather than by drift: (a) the **project memory dir** either gets
its write path back (a scoped `Memory` tool writing inside the dir, no
permission prompt) or its prompt section is removed — a standing instruction
the model cannot follow poisons trust in the rest of the prompt; (b) the
**delegated Claude leg** either gets recall + learner via an MCP bridge
(SessionSearch/SessionView and the 0.1 note channel are both trivially
bridgeable) or the app documents that Claude sessions use CLI-native memory
only. Recommendation: bridge recall + note (cheap, high value), keep the
CLI's own declarative memory as-is.

### Phase 1 — Frontier memory (the EvoHarness half)

**1.1 Scored retrieval over the learned store (`recall`).** Replace nothing;
add a `Recall` tool (JSON surface + kernel stub) that keyword-searches
memories, synthesized skills, and prose skills with top-k per category, and
increments per-entry usage counts. Sessions stay covered by SessionSearch.
This is the load-bearing meta-action in EvoHarness — the one that persisted
through annealing.

**1.2 Usage-driven memory lifecycle.** Memories gain `uses` /
`last_recalled_at`. The cap-40 eviction becomes LFU-informed instead of
oldest-first archive; the synthesizer prompt sees usage counts so it retires
what is never recalled and protects what is. This is EvoHarness's
"forgetting" and directly addresses HCL's open consolidation problem.

**1.3 Scoping.** Partition `memories.json` by project (global + per-project
sections, keyed the way ai-shuffle keys the project dir), because the audit
found project-specific facts leaking install-wide. The prompt snapshot
renders global + current-project only. Note the inversion worth fixing in
the same stroke: ai-shuffle's cross-install sync (`agent_memory_files`)
serves only the dead project-memory store, while the live `memories.json`
has no sync at all — once memories are project-partitioned, syncing them
through the same table is natural.

**1.4 Epoch consolidation.** Alongside the incremental 8-entry synthesis,
add a periodic (session-close or N-sessions) consolidation pass: the
synthesizer sees the *whole* store plus usage data and merges duplicates,
generalizes narrow entries, and retires stale ones — the ADD/UPDATE/REMOVE/
SKIP pass from EvoHarness, and the pass HCL names as its missing piece.
Deterministic curation stays as the backstop.

**1.5 Belief/Progress framing in the prompt.** Fluid already has the pieces
(Ledger = progress, Note = scratch, Memory = experience). Adopt the papers'
functional framing in one short prompt block: what each store is *for* and
when to reach for each — the ablations say explicit functional separation is
worth 5-10pp on its own. Extend `memories_dir` to subagents while here.

### Phase 2 — Guarded harness evolution (the HCL half)

**2.1 Anchor sets from the session store.** Define an anchor as a compact
replayable case: a task prompt + a success predicate (exit code, file
assertion, or golden output — the prompt-regression harness format already
exists). Harvest anchors semi-automatically from successful sessions
(the journal knows which sessions ended with passing verifications) and cap
the set (HCL uses ratio-mixed successes/failures; start with ~20).

**2.2 The commit gate.** Any *harness-state* change proposed by the learning
loop — beyond memories/skills, which keep their lighter gates — must pass:
improvement evidence (the failure that motivated it now passes), anchor
retention within budget B (start B=1 per HCL's finding; config knob), and
validity (existing selftest + mentioned-tools invariant + schema checks).
Fail the gate → the change is parked with its evidence, not committed.

**2.3 Model guidance as learned data.** Move `model_guidance` from
hand-written code to a versioned per-model-family data file seeded with
today's content. Teach the synthesizer to propose guidance edits from
journal evidence of model-specific failures (malformed tool calls, ignored
conventions, format drift), committed only through the 2.2 gate. This is the
payoff: a new model ships → run it on real work → the harness accumulates
its guidance block, description phrasings, and defaults automatically, with
anchors guaranteeing older models don't regress. Prompt-time BPE's +22pp on
GPT-4.1 is the ceiling estimate for what harness-side adaptation is worth on
a weak-in-harness model.

**2.4 What not to adopt.** EvoHarness's RL stage (SFT + GRPO) — it trains
weights, ours are frozen; the paper's own prompt-time results justify
skipping it. HCL's per-task validation-set requirement — too heavy for an
interactive harness; anchor replay on *proposed commits only* (not per turn)
keeps cost bounded. Full router evolution — fluid's routing is the compiled
prompt + tool availability, already data-driven; revisit only if 2.3 proves
out.

### Sequencing

| Order | Items | Why |
|---|---|---|
| 1 | 0.1–0.5 | Days of work; converts an invisible system into a visible one |
| 2 | 0.6 | Decisions + a small MCP bridge; unblocks the default (Claude) leg |
| 3 | 1.1–1.3 | The retrieval/lifecycle core; each is small and independent |
| 4 | 1.4–1.5 | Consolidation + framing polish |
| 5 | 2.1–2.2 | Gate infrastructure; reuses regression harness + session store |
| 6 | 2.3 | The self-evolving-harness payoff, last because it rides on everything above |

---

## Implementation status (2026-08-30, this branch)

Phase 0 and Phase 1 landed in full, plus the Phase-2.3 seed, in the commit
series following this document:

- **0.1/0.2** — `Remember` builtin (propose-then-curate: journals a
  high-salience `[remember]` entry, arms the synthesizer's urgent flag so the
  next pass fires on any new entry); the "never manage it" prompt order
  replaced with Remember/Recall guidance and a memory/Note/skills/sessions
  ontology line.
- **0.3** — code-mode addendum teaches `save_skill` with the actual
  procedure; `skills`-source specs joined the CE12 allowlist exemption;
  prose disambiguates synthesized Python skills from prose SKILL.md skills.
- **0.5** — `ContinualUpdate` event + `[learned]` REPL line, flushed even
  when synthesis lands during session close; `.synthesis-log.jsonl` audit
  trail (every pass: consumed count, changes, error, output head on no-ops
  — including abandoned-at-close passes).
- **1.1/1.2** — `Recall` builtin over live + archived memories and the
  skill index; per-entry recall counters; LFU-informed eviction.
- **1.3** — memory scope (`global` | project key from `project_key_for`);
  the snapshot renders global + current project with a hidden-count footer.
- **1.4** — interval-gated whole-store consolidation pass (rewrites and
  deletions only; a `<skill>` tag in its output is ignored).
- **1.5** — subagents receive the memory snapshot.
- **2.3 seed** — `~/.fluid/guidance/<family>.md` user-authored guidance
  blocks matched by stem substring against the model name.

2.1/2.2 landed in a follow-up commit (design round in
[anchor-gate-design.md](anchor-gate-design.md)): `AnchorStore` +
outcome-shaped checks (`fluid/codemode/anchors.py`), the three-stage
`CommitGate` with parking and an evolution log
(`fluid/codemode/evolution.py`), an in-process isolated replay runner with a
compile-only short-circuit (`fluid/cli/replay.py`), and the CLI surfaces
`fluid anchors …` / `fluid evolve …` with a confirmed harvest pass
(`fluid/cli/evolve.py`). `anchor_budget` (default 1) is the B knob.

2.3 landed after the gate's live verification: the synthesizer drafts
`<guidance>` proposals from journal evidence (queue-time validated: stem ⊂
session model, mandatory motivation case; one pending per stem), announced
as `guidance-proposal:<stem>`; `fluid evolve pending`/`run` evaluate the
queue through the same CommitGate with live replays. Shipped family blocks
stay in code as factory defaults — learned guidance owns new families,
which is the mission's actual case (see the design doc's 2.3 section for
the scope argument).

Deferred: ai-shuffle-side wiring (0.4/0.6 live in that repo); optional
auto-evaluation of the pending queue (today `evolve run` is the trigger —
a deliberate cost valve while the loop earns trust).

### E2E verification (z-ai/glm-5.3-flash via OpenRouter)

Twelve unscripted sessions against the live model; every scenario phrased
as a natural situation, never naming a tool. All passed:

- "keep that in mind going forward" → `Remember` called unprompted, clean
  fact phrasing, deliberate scope choice; urgent synthesis distilled it
  into `memories.json` at session close.
- Fresh session answered the remembered fact from `# Memory`, zero tools.
- A fact present only in the *archive* → `Recall` + `SessionSearch` fired
  in parallel, the archived entry was found, and the model verified against
  the project before answering (honoring the recall footer's caveat).
- "what were we working on?" → no-query `SessionSearch` listing, then
  `SessionView` on the substantive session, accurate recap.
- Code mode: composed multi-call cells, correct results, cells journaled.
- Project-specific phrasing ("in this repo…") → `scope="project"` chosen by
  the model and resolved to the project key by the synthesizer.
- A guidance file `glm.md` changed live behavior (house-style line held
  across sessions).
- An abandoned close-time pass (over the 90s budget) lost nothing: the
  cursor design retried the slice next session and landed all pending facts
  — then the `[learned]` line and audit log confirmed it visibly.
- Capstone: a fresh session recapped all nine accumulated facts, grouped
  and hedged, cross-referencing the snapshot with Recall.

Synthesis quality on glm-5.3-flash: correct tag format every observed pass,
correct scope attribution, `NO_CHANGES` restraint on the trivial-session and
consolidation passes.

## Appendix — paper citations

- Kang, Gu, Lv, Li, Wang, Gao. *Harness Continual Learning: Continual
  Adaptation Beyond Model Parameters.* arXiv:2608.19013.
- Meta AI + UIUC. *EvoHarness-RL.* arXiv:2608.05446. Coverage:
  VentureBeat, "Meta researchers taught an 8B AI model to match Claude
  Opus 4.5 without the frontier price tag."
