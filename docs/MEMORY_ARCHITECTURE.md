# Memory architecture

Decided 2026-08-31, closing the continual-memory audit (vendored fluid
`docs/research/continual-memory-study.md` Part II and the wiring handoff in
`docs/research/ai-shuffle-wiring-handoff.md`). The audit's core complaint was
that four different things were named "memory" with undocumented boundaries.
This file is the map. If you change a boundary, change this file in the same
PR.

## The systems

### 1. Continual learner (the durable-facts system)

The fluid continual harness, rooted at `<data_dir>/fluid/skills`
(`FLUID_SKILLS_DIR` relocates it): `journal.jsonl` (episodic record),
`memories.json` (distilled facts), saved skills, `.synthesis-log.jsonl`
(audit trail of every synthesis pass).

- **Write path**: sessions journal automatically — through `CodeModeSession`
  when code mode is up, through `ToolJournal` otherwise (code mode off, no
  registry, or failed construction; wired in
  `app/core/provider/harness_adapter.py`). The model participates through
  the `Remember` builtin (propose-then-curate: a high-salience journal entry
  plus an urgent synthesis kick — never a direct write to `memories.json`).
- **Curation**: only the background `Synthesizer` writes `memories.json`. It
  is scoped with `project_key_for(cwd)`, so a project-tagged fact lands under
  that project's key instead of leaking into every session on the install.
- **Read path**: the `# Memory` prompt snapshot (compiled per session,
  global + current-project entries only, hidden-count footer) and the
  `Recall` builtin (explicit retrieval over every scope, archived entries
  and skills included).
- **Legs**: native legs get all of it via fluid's builtins. The delegated
  (claude CLI) leg gets `Remember`/`Recall` through the builtins MCP bridge
  (below).

### 2. Session recall (the transcripts system)

Past conversations, in Postgres (`fluid_session_nodes` + the SDK mirror),
served by `PgFluidStore`'s recall hooks. Tools: `SessionSearch` (keyword
search / recent listing, project-scoped by default) and `SessionView` (a
message window from one session). Native legs get fluid's builtins via
`ctx.extra`; the delegated leg gets the bridged MCP copies. This system
stores nothing the transcript does not already contain — it is retrieval,
not learning.

### 3. Claude Code CLI's own memory

The delegated leg runs the claude CLI, which keeps its own native memory
(`~/.claude/projects/<slug>/memory/`, CLAUDE.md, auto-memory). The app does
not read, write, or sync it — deliberately (the 2026-08-23 native-only-memory
ruling). The CLI leg is therefore the one place two memory systems coexist:
the CLI's own, plus the app's via the MCP bridge. They do not overlap in
storage; a fact Remembered through the bridge is app memory, a fact the CLI
saves itself stays CLI memory.

### 4. `agent_memory_files` DB sync (legacy mirror)

`app/core/memory_sync.py` mirrors per-project `*.md` memory dirs across
installs sharing a git remote, keyed by `link_group_id`. Since the removal
below, no prompt section points at those dirs and no tool writes them — the
sync now serves only pre-existing content and the explicit per-project
import (`app/core/memory_import_service.py`).

## Decisions

**The `# Project memory` prompt section is removed** (audit item 0.6a). Its
write path — the per-agent Memory tool — was removed in PR #997, so the
section promised "save one fact per file" while nothing could keep the
promise. A standing instruction the model cannot follow poisons trust in the
rest of the prompt. One memory system with a real write path (the continual
learner) beats two where one is a lie. `aih_memory_dir` and
`extra["memory_dir"]` are gone with it; `memory_sync` and the import service
keep resolving their own dirs.

**The delegated leg gets a bridge, not a disclaimer** (audit item 0.6b; the
study's recommended option). `Remember`, `Recall`, `SessionSearch` and
`SessionView` ride the `mcp__builtins__*` server
(`app/mcp/builtin_tools.py`), backed by the same stores as the native
builtins — the fluid handlers themselves run under a locally built
`ToolCtx`, so semantics, screening and formatting cannot drift. On native
legs the bridged copies are shadowed (`NATIVE_SHADOWED_BUILTINS`) in favor
of fluid's bare-named originals. One leg-inherent difference: with no engine
there is no live synthesizer, so a delegated `Remember` journals the fact
and the next native session's synthesis pass distills it.

**Syncing `memories.json` through `agent_memory_files` is deferred.** The
audit noted the inversion (the DB sync serves the dead store while the live
`memories.json` has no sync). Now that memories are project-partitioned,
syncing them through that table is the natural follow-up — but it changes
multi-install semantics (concurrent synthesizer writes, last-write-wins on a
single JSON file) and was explicitly not bolted onto this wiring PR.

**Phase 2 guidance proposals are wired.** This is the fifth thing the harness
learns, and the only one that changes what a model *obeys* rather than what it
knows — so it carries a heavier gate than memories do.

The state is a per-model-family guidance file, `<home>/.fluid/guidance/<stem>.md`,
where the stem matches as a case-insensitive substring of the session's model id
(`glm.md` matches `z-ai/glm-5.3-flash`). `home` is the user's home by default,
fluid's own location, so an operator running `fluid evolve run` against this
install's skills root commits into the directory the app reads;
`FLUID_GUIDANCE_HOME` moves both halves together. Fluid's *shipped* family
blocks stay in code as factory defaults — learned files append after them and
fully own any new family.

The loop, in `app/core/guidance_surface.py` plus the adapter:

- **Read.** `_compile_prompt_sections` appends the matching files (screened and
  capped by fluid's `load_guidance_files`) after the shipped blocks, native legs
  only. Without this half the gate would write files no session ever reads.
- **Propose.** The `Synthesizer` is constructed with `session_model=`
  (`options.model` — the same string the read path keys on, so a proposal's stem
  can actually match) and `guidance_snapshot=` (the raw, unscreened, untruncated
  text of those same files, so an amendment does not silently drop the tail it
  replaces). A pass that sees a *recurring* model-specific mechanical failure in
  the journal may emit one `<guidance>` draft. Fluid validates it at queue time
  and appends it to `pending-proposals.jsonl` in the skills root, one per stem, a
  newer draft superseding. Nothing writes the guidance directory here; the
  `[learned]` line announces `guidance-proposal:<stem>`.
- **Gate.** `evaluate_pending()` runs fluid's `CommitGate` — validity (stem
  shape, size cap, injection screening, no directives to non-existent tools),
  then improvement (the proposal's motivating case must pass when replayed with
  the change applied), then retention (every anchor whose baseline passes
  replays; at most `anchor_budget` regressions, default 1). A survivor is
  written to `<stem>.md` and logged to `.evolution-log.jsonl`; anything else
  lands in `parked.jsonl` with the failing stage and its evidence. The
  synthesizer never gets the no-motivation waiver.
- **Disposition is out-of-band and explicit.** Gate evaluation replays live
  model turns, so it never rides a background synthesis pass or a session close
  — nothing in a turn's path calls it. `pending_proposals()`, `parked_proposals()`,
  `evolution_log()` and `anchors()` are the read surfaces; `reject_pending()`
  dismisses a draft by parking it (never deleting — parked records are the
  evidence a better proposal is built from). What a human approves is submission
  to the gate, not the edit: the gate decides on replay evidence.

The anchor set itself is managed with fluid's `fluid anchors` CLI (harvest needs
a human curator; `run` records baselines with live replays). The app reads
anchors, and does not write them.
