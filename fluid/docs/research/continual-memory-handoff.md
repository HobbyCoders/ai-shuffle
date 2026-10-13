# Handoff — continual harness & memory work (2026-08-30)

For the next session picking this up. Complete context: what was asked, what
was found, what shipped, what's verified, and exactly what remains. Companion
documents: [continual-memory-study.md](continual-memory-study.md) (the full
research + plan + implementation appendix) and the published report artifact
at https://claude.ai/code/artifact/7a7c913b-2e31-4a4c-b834-61ee5c0a7606.

## Where you are

- Repo: `D:\Development\omni-harness` (fluid, the agent harness).
- Branch: `claude/harness-memory-continual-learning-4034cf`, worktree
  `.claude/worktrees/fluid-slash-commands-841724`. Base branch: `main`.
- Two commits carry this work: `57a9f51` (feature) and `41ee683`
  (visibility fixes found by live testing). Both on top of `13b1c43`.
- Tests: `python -m pytest tests/unit` → 1299 passed, 5 skipped. New pins
  live in `tests/unit/test_continual_v3.py` (23 tests).
- Nothing is pushed and no PR exists yet.

## The mission (user's words, condensed)

Frontier-leading memory plus a continually self-evolving harness, so the
harness never needs hand-modification for a new model. Testing philosophy is
non-negotiable: never tell the model which tool to use — create situations
that should trigger the flow. "The goal is when we are done even the
stupidest of local models can use the harness properly and effectively."

## Research that grounds the design (already done, don't redo)

Two papers, read together as complementary halves:

- **HCL** (arXiv 2608.19013): continual learning relocated to the harness;
  names *harness-level forgetting*; guarded evolution — an optimizer proposes
  harness edits, an evaluator commits only on (improvement, anchor-set
  retention within budget B, validity). B=1 beat both extremes. No
  consolidation mechanism (their admitted gap).
- **EvoHarness-RL** (arXiv 2608.05446, Meta+UIUC): Belief/Progress/Experience
  meta-actions (`track/commit/recall/note`); the load-bearing idea is the
  **two-timescale write path** — `note` buffers, an epoch consolidator does
  ADD/UPDATE/REMOVE/SKIP, LFU eviction via recall counts. Prompt-time
  scaffolding alone lifted frozen GPT-4.1 +22pp (the empirical case that the
  harness closes model gaps). No retention gate (their gap).

Audit findings that motivated everything (full detail in the study doc):
fluid's learning loop worked but was invisible *by design* (background
synthesizer only, "never manage it" prompt order, `save_skill` never in any
schema, off by default); ai-shuffle has four things named "memory" of which
two run, none on the delegated Claude leg (its default).

## What shipped in fluid (Phase 0 + 1 + 2.3-seed)

The propose-then-curate doctrine everywhere: the model PROPOSES (cheap,
in-turn), the background synthesizer DISPOSES (verified, out-of-band). No
model-facing call writes `memories.json` directly.

| Piece | Where |
|---|---|
| `Remember` builtin — journals a `[remember]` entry, arms urgent synthesis (any new entry triggers, not 8) | `fluid/tools/builtins/remember.py`, closure wired in `fluid/config/loader.py` (`ctx.extra["continual_journal"]`) |
| `Recall` builtin — scored search over live + archived memories + skill index, bumps recall counters | `fluid/tools/builtins/recall.py` (`ctx.extra["recall_store"]`) |
| LFU eviction (least-recalled first past cap 40), `uses`/`last_recalled_at` on `MemoryEntry` | `fluid/codemode/skills.py` |
| Memory scoping: `scope` = `"global"` \| project key; snapshot renders global + current project + hidden-count footer | `skills.py` (`project_key_for`, `render_memory_section(project)`), `fluid/context/compiler.py` |
| Synthesizer: urgent flag, `[remember]` weighting, `scope="project"` tag resolution, `on_change` callback | `fluid/codemode/continual.py` |
| Epoch consolidation — interval-gated (7d, ≥8 memories) whole-store pass; rewrites/deletes only, `<skill>` tags ignored | `continual.py` (`consolidate`, `_CONSOLIDATE_SYSTEM`) |
| `ContinualUpdate` event + `[learned]` REPL line + late-event flush at close | `fluid/events/model.py`, `fluid/cli/repl.py`, `fluid/cli/__main__.py` (`_close_and_flush_learning`) |
| `.synthesis-log.jsonl` audit trail (every pass: consumed, changed, error, output head when nothing landed, abandoned-at-close records) | `continual.py` (`_log_pass`) |
| Prompt: "never manage it" replaced with Remember/Recall guidance + ontology line; code-mode addendum teaches `save_skill`; `skills`-source exempt from CE12 allowlist strip | `fluid/cli/prompt.py`, `fluid/engine/native.py` (`_tool_permitted`) |
| Subagents get the memory snapshot | `fluid/cli/host.py` |
| Guidance-as-data: `~/.fluid/guidance/<stem>.md` appended when stem ⊂ model name (e.g. `glm.md` → `z-ai/glm-5.3-flash`) | `fluid/cli/prompt.py` (`load_guidance_files`, `PromptProfile.extra_guidance`) |

Three bugs only live testing caught (all fixed in `41ee683`):
1. `build_engine` wired the synthesizer's `on_change` only when a bus was
   passed in; the CLI passes none → the loader now materializes the bus in
   the native path so emitter and engine share one.
2. Close-time synthesis publishes after the event stream ends → `run`/`chat`
   flush late `ContinualUpdate`s as `[learned]` lines after `engine.aclose()`.
3. A failed / NO_CHANGES / abandoned pass was indistinguishable from one
   that never ran → the audit log.

## E2E verification (done — 12 unscripted sessions, all passed)

Model: **`z-ai/glm-5.3-flash`** on OpenRouter (fun fact: this IS ox-alpha,
publicly revealed). Every scenario natural phrasing, zero tool names:

- Remember fired unprompted on "keep that in mind"; urgent synthesis landed
  the fact at close. Fresh session answered it from `# Memory`, zero tools.
- Archived-only fact → Recall + SessionSearch in parallel, found it,
  verified against the project before answering.
- "what were we working on?" → SessionSearch listing → SessionView → recap.
- "in this repo…" → model chose `scope="project"`, synthesizer resolved it.
- `glm.md` guidance file changed live behavior (house-style line held).
- Abandoned close pass (>90s budget) lost nothing — cursor design retried
  the slice next session (m6–m9 landed together, `[learned]` line rendered).
- Capstone: fresh session recapped all nine accumulated facts, grouped,
  hedged, cross-referencing snapshot + Recall.

### How to run the E2E rig again

- The key: `OPENROUTER_API_KEY` is set **user-scope** in Windows env. Fresh
  long-running processes don't inherit `setx`; read it at runtime:
  `[Environment]::GetEnvironmentVariable("OPENROUTER_API_KEY","User")`.
  Never print it.
- The rig from this session lived in the session scratchpad (`e2e/` — fake
  `HOME` with its own `~/.fluid/config.toml`, a `proj/` with `.git`, a
  `run.ps1` wrapper) — scratchpads are session-scoped, so rebuild it; the
  recipe: config.toml with `default_provider="openrouter"`,
  `default_model="z-ai/glm-5.3-flash"`, `continual=true`, provider block
  with `api_key_env="OPENROUTER_API_KEY"`; then
  `python -m fluid.cli run --cwd <proj> "<prompt>"` from the worktree root
  with `HOME`/`USERPROFILE` pointed at the fake home.
- Gotchas: the module is `fluid.cli`, not `fluid`. CLI `--continual` forces
  `code_mode=true`; JSON-mode continual needs `continual=true` in config
  with code mode off. Default approval policy `on_write` + one-shot
  auto-deny means write tools are denied in `run` — use
  `--approval-policy never_ask` for code-mode scenarios in scratch dirs.
  **Set `model = "z-ai/glm-5.3-flash"` INSIDE the `[providers.openrouter]`
  block**, not only `default_model`: `BUILTIN_DEFAULTS` ships
  `providers.openrouter.model = "openai/gpt-5-mini"`, a provider block
  without its own `model` line inherits it via deep-merge, and the
  per-provider model beats `default_model` in `model_for` — the 2026-08-30
  anchor-gate E2E silently ran against gpt-5-mini because of this.
- The key expired late 2026-08-30 and the user refreshed it the same
  session (`setx OPENROUTER_API_KEY …`); the full anchor-gate E2E then ran
  green. If it 401s again ("API key expired"), ask the user for a fresh
  key before burning time — confirm with the `/api/v1/key` endpoint.
- Synthesis on glm takes ~35s/pass; the close budget is 90s
  (`Synthesizer.close_timeout`). Slow passes are abandoned by design and
  retried next session — check `.synthesis-log.jsonl` before assuming loss.

## What remains (in priority order)

1. **Push / PR.** The branch is local-only. The commit series is clean;
   PR against `main` of `HobbyCoders`'s repo when the user wants it.
2. ~~**Phase 2.1/2.2 — anchor sets + commit gate**~~ DONE (2026-08-30, this
   branch): design round in [anchor-gate-design.md](anchor-gate-design.md);
   `fluid/codemode/anchors.py` (store + outcome-shaped checks),
   `fluid/codemode/evolution.py` (validity → improvement → retention gate,
   `parked.jsonl` + `.evolution-log.jsonl`), `fluid/cli/replay.py`
   (in-process isolated runner, compile-only short-circuit),
   `fluid/cli/evolve.py` + `fluid anchors …` / `fluid evolve …` CLI.
   `anchor_budget` config knob (default 1). Pinned in
   `tests/unit/test_anchor_gate.py` (20 tests, offline). Live E2E against
   glm-5.3-flash, all three regimes: **error path** (dead key → provider
   errors surface as failed baselines with detail; also caught the
   unchanged-motivation hole — a family-scoped edit under a mismatched
   model short-circuited its motivating case to `unchanged` and committed
   with zero evidence; fixed and pinned), **happy path** (2/2 anchors
   baselined pass; a Ship-it house-style guidance proposal passed its
   motivation live and committed with 0 regressions, file written, log
   recorded), and **negative path** (a planted "answer monthly, never say
   weekly" edit parked at retention under budget 0, guidance file
   untouched, evidence in parked.jsonl). Authoring lesson from the first
   negative attempt: glm hedged by mentioning both words, so a lone
   `contains` check false-passed — anchors protecting an assertion need a
   paired `--forbid` for the plausible wrong answer (now in the design doc
   and the harvest drafting prompt).
3. ~~**Phase 2.3 full — model_guidance as learned data.**~~ DONE
   (2026-08-31, this branch): the synthesizer drafts `<guidance stem="…">`
   proposals from journal evidence — prompt shows it the session model id +
   current matching guidance file(s); queue-time validation enforces stem ⊂
   session model and a mandatory motivation case; drafts queue to
   `pending-proposals.jsonl` (one per stem, newest wins) and announce as
   `guidance-proposal:<stem>` on the `[learned]` line. `fluid evolve
   pending` lists them; `fluid evolve run` evaluates through the 2.2 gate
   (live replays; the synthesizer never gets the no-motivation waiver).
   Shipped family blocks stay in code as factory defaults — learned
   guidance owns new families (scope argument in the design doc). Shared
   guidance mechanics moved to `fluid/context/guidance.py`. Pinned in
   `tests/unit/test_guidance_evolution.py`. Live-verified end to end on
   glm: seeded failure evidence → drafted proposal on the `[learned]` line
   → `evolve run` → committed through live replays. Four live-caught fixes
   (channel rule in the synth prompt, ≥2-char tool-directive names,
   parked records keep the motivation, motivation-authoring rules) — the
   design doc's 2.3 section has the details.
4. **ai-shuffle wiring** (separate repo, `D:\Development\ai-shuffle`) —
   now spelled out for a fresh session in
   [ai-shuffle-wiring-handoff.md](ai-shuffle-wiring-handoff.md); the raw
   audit items, for reference:
   - 0.4: pass `journal=` to its `NativeLoop` construction
     (`app/core/provider/harness_adapter.py` ~line 597–660) — today the
     learner silently dies when code mode is off/fails.
   - 0.6a: the `# Project memory` prompt section promises a write path that
     doesn't exist (dir empty, tool removed in #997) — restore a write path
     or remove the section.
   - 0.6b: the delegated Claude leg (the app's default) has zero app
     memory — bridge Recall/SessionSearch + the Remember channel via MCP,
     or document CLI-native memory as the answer.
   - Re-vendor fluid (vendor copy + VENVDORED hash flow — see the
     ai-shuffle-vendoring memory note) to pick up these commits, and wire
     `memories_dir`'s new project scoping (its `memories.json` is
     install-global today; entries m4/m5 there leak across projects).
5. **Small fluid polish, optional:** `[learned]` rendering in the TUI
   (`fluid/cli/tui.py` untouched); Recall over prose-skill index; surfacing
   `.synthesis-log.jsonl` via a CLI command.

## Testing doctrine reminder for anything new

Never instruct the model to use a feature in tests. Build the situation
("remember that…", "what was X again?", "what were we working on?") and
assert the flow fired + the artifact landed (journal entry, memories.json,
log line). If a competent-but-small model doesn't trigger the flow, the fix
is prompt/tool-description work in the harness, not a better test prompt.
