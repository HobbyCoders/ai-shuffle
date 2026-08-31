# Anchor sets and the commit gate — Phase 2.1/2.2 design

Design date: 2026-08-30. This is the deferred core of the continual plan
([continual-memory-study.md](continual-memory-study.md), Phase 2): HCL's
guarded evolution, sized to fluid. Read the study's Part I for why the gate
exists; this document decides the shapes.

## The problem, restated

The learning loop can already change harness state that models *see*
(memories, skills) under lightweight gates (selftest verification, caps,
screening). Phase 2.3 wants it to change harness state that models *obey* —
per-model guidance blocks, and eventually description phrasings and defaults.
A bad memory wastes a prompt line; a bad guidance edit rewires behavior for
every future session of that model family. HCL's answer: no harness-state
change commits unless a candidate passes improvement, retention, and validity
checks, where retention is measured by replaying a compact **anchor set** of
previously-working cases and tolerating at most **B** regressions (their
result: B=1 beats both B=0 and B=∞).

## Anchors (2.1)

An anchor is a compact replayable case: a task prompt plus success checks,
proving one behavior the harness currently gets right.

```json
{
  "id": "a3",
  "title": "recalls archived facts instead of claiming absence",
  "prompt": "what did we decide about the deploy cadence?",
  "checks": [{"kind": "tool_called", "name": "Recall"},
             {"kind": "not_contains", "pattern": "no (record|memory)"}],
  "setup": {"AGENTS.md": "…optional workspace seed files…"},
  "tools": null,
  "model": null,
  "max_model_calls": 8,
  "source": "session:4f2a…",
  "baseline": "pass",
  "created_at": 0.0, "last_run_at": 0.0, "last_result": "pass"
}
```

Decisions:

- **Storage**: `anchors.json` in the skills root, same `{next_id, entries}`
  doc shape as `memories.json`, ids `a<n>`. Anchors are harness state; they
  live with the rest of it.
- **Cap 20** (HCL keeps anchor sets compact; replay cost is linear in the
  set). Adding past the cap is an error naming the pruning command, not a
  silent eviction — anchors are load-bearing and few; a human or the
  harvester decides which twenty.
- **Checks are outcome-shaped, not transcript-shaped**: `contains` /
  `not_contains` (regex over the final assistant text), `tool_called` (a
  named tool fired during the turn), `file` (path exists in the replay
  workspace, optional content regex). This is the testing doctrine encoded:
  the *prompt* never names a tool; the *assertion* may check the flow fired.
- **Anchors that protect an assertion must discriminate.** A lone
  `contains` check false-passes on hedging replies: live testing planted
  guidance saying "answer monthly, never mention weekly", and glm replied
  by flagging the contradiction — mentioning *both* words, satisfying both
  the anchor's `weekly` check and the harmful edit's `monthly` motivation
  at once. Pair `contains` with a `not_contains` for the wrong answer
  (`--expect weekly --forbid monthly`); with the pair in place the same
  harmful edit parked at retention.
- **`baseline`** is the recorded status from the anchor's last plain replay
  (`pass` / `fail` / `unknown`). Retention compares candidate replays against
  this recorded baseline — no fresh control run per gate evaluation, which
  would double the cost. `fluid anchors run` refreshes baselines explicitly.
- **`model`** optionally pins the replay model (per-family anchors, the 2.3
  need: "older models don't regress"). Default: the session's configured
  model.
- **Harvest is semi-automatic.** The session store records no success labels
  (verified during design: `SessionMeta`/`SessionNode` carry none), so fully
  automatic harvesting has nothing to key on. `fluid anchors harvest` shows
  recent project sessions to the continual model (first user message, final
  assistant text, tools used) in one tool-less completion that drafts
  candidate anchors; the user confirms each before it lands. Same
  propose-then-curate doctrine, with the human as curator.

## The replay runner

Replays run **in-process** through the same recipe `CliHost._build_child` /
`_run_child` uses for subagents: `build_engine` with an injected provider,
drive one `UserTurn` off the bus, read the final assistant text off the
store. Not a subprocess — the in-process seam lets unit tests inject a
scripted provider, and the subagent path is already the proven headless
recipe.

Isolation and fidelity, per replay:

- **Workspace**: a fresh temp dir as `cwd`, seeded from the anchor's `setup`
  files. Deleted afterwards.
- **Store**: an in-memory session store. Replays never land in the user's
  session history.
- **Skills root**: a temp *copy* of the real one, so the prompt's memory
  snapshot and mounted skills are faithful while Recall counters, journal
  writes, and any synthesis all hit the copy. `continual` stays as
  configured — an anchor may assert the Remember/Recall flow fires.
- **Tool surface**: the anchor's `tools` list via `ToolsConfig.allowed`
  (CE12 does the gating); default surface is the file/search/memory set —
  no `Bash`, no web tools. Permission requests are auto-allowed (the surface
  and the throwaway workspace are the sandbox).
- **Bounds**: `max_model_calls` from the anchor plus a wall-clock timeout;
  a hung replay is a failed replay.
- **Candidate overlay**: a guidance proposal replays with a staged guidance
  home — the real `~/.fluid/guidance` copied to a temp home with the
  proposed edit applied — threaded through `load_guidance_files(model,
  home=…)` into the compiled prompt. The base prompt is otherwise composed
  exactly as a real session would.
- **Compile-only short-circuit**: before spending a model call, the runner
  compiles the prompt with and without the candidate. Byte-identical prompts
  (e.g. a `glm.md` edit replayed under a Claude-family anchor) return
  `unchanged` — the anchor cannot have regressed, no completion is made.
  This keeps retention affordable: a family-scoped proposal pays live
  replays only for anchors of that family.

## The commit gate (2.2)

`CommitGate.evaluate(proposal)` runs three stages in cost order; the first
failure parks the proposal.

1. **Validity** (free): per-kind validators. For guidance: stem shape,
   size cap (the existing 4 000-char guidance cap), injection-screen clean
   (`screen_findings`), and no tool-directive to an unknown name ("call X" /
   "the X tool" where X is not a registered builtin) — `mentioned_tool_names`
   scans for known names only, so the hallucinated-tool shape is caught at
   the directive verb instead.
2. **Improvement**: the proposal carries a `motivation` — an inline
   anchor-shaped case reproducing the failure that motivated the change.
   Replayed WITH the candidate, it must pass — and it must actually *run*:
   a motivation that short-circuits as `unchanged` (the candidate does not
   alter its compiled prompt, e.g. a family-scoped edit under a mismatched
   model) parks the proposal instead of counting as evidence. Live testing
   caught exactly that hole. A proposal without a motivation is parked as
   unevidenced (a CLI flag can waive this for deliberate human proposals;
   the synthesizer never gets the waiver).
3. **Retention**: every anchor whose baseline is `pass` replays with the
   candidate; regressions must be ≤ **B** (`anchor_budget` config knob,
   default 1 per HCL).

Verdicts are durable either way:

- **Committed** → the change is applied (for guidance: the file is written)
  and appended to `.evolution-log.jsonl` in the skills root — timestamp,
  proposal, per-anchor results, budget spent.
- **Parked** → appended to `parked.jsonl` with the failing stage, reasons,
  and per-anchor results. Parked proposals are evidence for a later, better
  proposal — never silently dropped. `fluid evolve parked` lists them.

Layering: the generic gate (`Proposal`, stages, parking, logging) lives in
`fluid/codemode/evolution.py` with validators and the apply step injected;
guidance-specific validation and application live at the CLI layer, which
owns the prompt vocabulary. `codemode` keeps not importing `cli`.

## Surfaces

- `fluid anchors list | add | rm | run [id] | harvest` — manage the set,
  refresh baselines, draft from sessions.
- `fluid evolve propose --stem <family> --file <md> --why <text>
  [--motivation-prompt … --motivation-expect …] [--no-motivation]` — the
  gate's first client: a human-proposed guidance edit that commits only if
  it survives the gate. Phase 2.3 makes the synthesizer the second proposer
  through the identical path.
- `fluid evolve parked | log` — the audit surfaces.
- Config: `anchor_budget` (int, default 1). B=0 is strict stability; the
  knob exists because HCL's optimum is task-mix-dependent.

## What this deliberately does not do

- No per-turn replays — the gate runs only when something proposes a commit
  (HCL's cost lesson).
- No automatic anchor harvesting without confirmation — no success labels
  exist in the store, and a bad anchor poisons every future gate decision.
- No gating of memories/skills — their existing lighter gates (selftest
  verification, caps, screening, consolidation) stay; the commit gate is for
  state that *instructs*, not state that *informs*.
- No 2.3 synthesizer wiring yet — that lands as its own change once the gate
  has soaked, per the plan's sequencing.
