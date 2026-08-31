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

**Phase 2 guidance proposals stay unwired.** The app's `Synthesizer` is
constructed without `session_model=`/`guidance_snapshot=`, which disables
the guidance-proposal channel entirely — fluid's documented safe default for
embedding hosts. Wiring it (plus a surface for the anchors/evolution commit
gate) is future work, gated on the rest of this architecture proving out.
