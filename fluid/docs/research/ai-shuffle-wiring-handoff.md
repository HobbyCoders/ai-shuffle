# Handoff — wire the continual harness into ai-shuffle (2026-08-31)

For a fresh session. The fluid side of the continual-memory plan is done and
merged to `main` (Phases 0, 1, and 2 — see
[continual-memory-handoff.md](continual-memory-handoff.md) for what shipped
and [continual-memory-study.md](continual-memory-study.md) Part II for the
ai-shuffle audit this work list comes from). What remains is entirely in the
**ai-shuffle repo: `D:\Development\ai-shuffle`** — do the work there, on a
branch off its `main`.

Line numbers below are from the 2026-08-30 audit; verify before editing —
ai-shuffle moves.

## What fluid now provides (already on fluid main)

The vendored fluid inside ai-shuffle predates all of this; nothing below
works there until the re-vendor (step 1).

- `Remember` / `Recall` builtins, gated on `continual=True` via
  `ctx.extra["continual_journal"]` / `ctx.extra["recall_store"]` — wired
  automatically by `build_engine`.
- **JSON-mode journaling**: `fluid.codemode.journal.ToolJournal` feeds the
  synthesizer when code mode is off. `build_engine` constructs it; a host
  building `NativeLoop` directly must pass `journal=` itself.
- **Memory scoping**: `MemoryEntry.scope` = `"global"` | project key from
  `fluid.codemode.skills.project_key_for(cwd)`; the prompt snapshot
  (`render_memory_section(project=...)`) renders global + current project
  with a hidden-count footer.
- **Visibility**: `ContinualUpdate` events on the bus; `.synthesis-log.jsonl`
  audit trail in the skills root.
- **Phase 2 (optional for ai-shuffle, see step 6)**: anchor sets + commit
  gate (`fluid/codemode/anchors.py`, `fluid/codemode/evolution.py`), the
  synthesizer's guidance-proposal channel (`session_model=` +
  `guidance_snapshot=` params — omitting them disables proposals, which is
  the safe embedding-host default), `fluid/context/guidance.py` for the
  shared guidance-file mechanics.

## Work list, in order

### 1. Re-vendor fluid

Follow the vendor-copy + VENDORED-hash flow (no rsync; the flow is described
in the repo's vendoring docs and the `ai-shuffle-vendoring` memory note:
vendor copy, venv, VENDORED hash file). Pick up fluid main through commit
`3ab8e4a`. The unraid Docker image needs a manual docker-publish dispatch
afterwards — do not forget it, nothing auto-publishes.

### 2. Fix the dead journal wire (audit item 0.4)

`app/core/provider/harness_adapter.py` (~597–660 at audit time) constructs
`NativeLoop` passing only `codemode=`. When code mode is off or fails to
construct, the learner silently dies while the model keeps seeing a frozen
`# Memory` snapshot. Build a `ToolJournal(skills, synthesizer)` and pass
`journal=` whenever continual is on and codemode is not carrying the
journal. Mirror fluid's own loader (`fluid/config/loader.py`, the
`config.continual` block) — it is the reference wiring for skills store,
synthesizer, remember-channel closure, and journal.

Verification (the testing doctrine — never tell the model which tool to
use): with code mode OFF, run a session saying "remember that <fact>",
close it, and assert the journal got the `[remember]` entry and
`memories.json` gained the fact. Check `.synthesis-log.jsonl` before
assuming a silent failure — an abandoned close pass retries next session.

### 3. Wire project scoping for the shared memory store

ai-shuffle's `memories.json` is install-global; project-specific facts leak
across projects (audit found entries m4/m5 doing exactly this). Pass
`project_key_for(<project cwd>)` as the synthesizer's `project=` and render
the snapshot with that project key, the way fluid's loader and
`fluid/context/compiler.py` do. While there: the audit noted the inversion
that `agent_memory_files` DB sync (keyed by `link_group_id`) serves only the
dead project-memory store while the live `memories.json` has no sync — once
memories are project-partitioned, syncing them through that same table is
the natural follow-up (its own decision; do not bolt it on silently).

### 4. Resolve the project-memory prompt lie (audit item 0.6a)

The `# Project memory` prompt section (`~/.fluid/projects/<slug>/memory/`,
`MEMORY.md` index) tells the model to "save one fact per file" but the
write tool was removed in ai-shuffle PR #997 — the dir is empty on the dev
machine and the promise is kept by nothing. Decide explicitly, don't drift:
either restore a scoped write path (a Memory tool that writes only inside
the dir, no permission prompt), or remove the section. Given the fluid
learner + Remember now covers durable facts, **removing the section is the
recommended default** — one memory system with a real write path beats two
where one is a lie. A standing instruction the model cannot follow poisons
trust in the rest of the prompt.

### 5. The delegated Claude leg (audit item 0.6b)

The app's default leg for Claude-family models is the Claude Code CLI
subprocess, and it gets none of the app's memory: no Recall, no
SessionSearch, no learner (`app/core/memory_sync.py` ~410–439 returns
unless `leg == "native"`; the 2026-08-23 `ai-shuffle-native-only-memory`
change made the claude leg explicitly import-only). Two options from the
study, decide one:

- **Bridge (recommended in the study)**: expose Recall + SessionSearch +
  the Remember channel to the delegated leg via a small MCP server backed
  by the same stores. Cheap, high value — the default leg finally sees
  app memory.
- **Document**: declare that Claude sessions use the CLI's own native
  memory, and make the app UI say so.

Whichever you pick, write the decision down in the repo (the audit's core
complaint was four things named "memory" with undocumented boundaries).

### 6. Optional — Phase 2 surfaces

Only after 1–5 are green. Wiring `session_model=` and `guidance_snapshot=`
into ai-shuffle's Synthesizer construction enables guidance proposals
(queued to `pending-proposals.jsonl` in the skills root; they commit ONLY
through the gate). The gate/anchors CLI (`fluid anchors`, `fluid evolve`)
is fluid-CLI-layer; ai-shuffle would need its own surface or can leave
gating to the CLI on the same skills root. Leaving 2.3 unwired is safe and
explicitly supported — no proposals are drafted without `session_model`.

## Verification & gotchas

- Testing doctrine everywhere: build the situation, never name the tool;
  assert the flow fired plus the artifact landed (journal entry,
  memories.json, log line, event).
- ai-shuffle's test suite runs ~35 min locally and has one env-dependent
  failure (event_gateway DSN) — pre-existing, not yours.
- Windows: `OPENROUTER_API_KEY` lives in user-scope env; long-running
  processes don't inherit `setx` — read it at runtime via
  `[Environment]::GetEnvironmentVariable(..., "User")`. If it 401s "API
  key expired", ask the user for a fresh key before burning time.
- Fluid E2E rig recipe (fake HOME, config.toml, provider-block `model`
  gotcha) is in [continual-memory-handoff.md](continual-memory-handoff.md)
  — useful as the shape for ai-shuffle-side live verification too.
