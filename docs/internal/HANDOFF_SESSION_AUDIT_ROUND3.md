# Handoff — Session-Audit Round 3

**Written 2026-08-27 by the round-1/round-2 coordinating session. Audience: a fresh
session with no prior context, tasked with fixing every remaining issue below.**

---

## 1. Context you need before touching anything

**Two repos, one pipeline.**
- `D:\Development\omni-harness` = upstream source of the **fluid** harness
  (github.com/HobbyCoders/fluid, package in `fluid/`). Fluid fixes land HERE first.
- `D:\Development\ai-shuffle` = the app (github.com/HobbyCoders/ai-shuffle). It vendors
  fluid at `vendor/fluid` — **never hand-edit vendor/fluid expecting it to stick**: a CI
  bot (`.github/workflows/vendor-sync.yml`) replaces the whole tree from fluid main on
  every fluid push (commit style `vendor(fluid): sync to <sha>`). For local testing
  before the fluid PR merges, you MAY mirror changed fluid files into vendor/fluid as a
  clearly-labeled commit — the sync bot will overwrite it with identical content after
  the fluid PR lands (this was verified twice: the sync commits diffed only `VENDORED`).
- Landing order when both repos change: **merge the ai-shuffle PR first, then the fluid
  PR** — the sync bot then fires and replaces the mirror.

**Test invocation.**
- fluid: `uv sync --extra dev` then `uv run pytest tests/unit -q` (Python ≥3.12).
  Green baseline at handoff: **1,253 passed, 2 skipped**.
- ai-shuffle (Windows host): `$env:PYTHONPATH="vendor/fluid"; python -m pytest <paths>
  -o addopts="" -q -p no:cacheprovider`. No `--timeout` (pytest-timeout absent). Green
  baseline: **7,661 passed** with these known non-failures:
  `tests/core/test_file_logging.py` (env: subprocess drops PYTHONPATH — can HANG the
  runner, exclude it with `--ignore`), `test_idle_reader.py::
  test_completed_task_updated_plus_notification_single_effective_wake` (ordering flake,
  passes alone), `test_encryption.py::TestReEncryptAllSecrets::
  test_re_encrypt_logs_error_on_failure` (ordering flake). Tests run on a SQLite
  pg-shim (`tests/pg_shim.py`) — keep runtime SQL portable; Postgres-only DDL goes in
  guarded blocks; the shim maps single-key `->>'x'` to json_extract.
- Frontend: worktrees need `node_modules` (junction from the main checkout works) +
  `npx svelte-kit sync` before `svelte-check`/vitest.

**Live database** (for audits and the backfill): Postgres on the ServerWorker host; URL in the
`DATABASE_URL=` line of `D:\Development\ai-shuffle\.env`. SELECT-only unless a task
explicitly says otherwise; never print the URL/password. Key tables:
`sessions`, `fluid_session_nodes(session_id,seq,node)`,
`fluid_session_events(session_id,seq,event)` (`event->>'_cls'`), `usage_log`, `turns`,
`background_tasks`, `pending_interactions`.

**Windows gotcha:** `git merge` immediately after `git worktree add` can fail with
"could not write index / fatal: stash failed" — just retry. Worktree removal can hit
file locks from live venvs; `git worktree prune` + delete the dir later.

**Process that worked (recommended):** fan work out to parallel Opus subagents, each in
its own git worktree + branch with a tightly scoped file-ownership list (prevents merge
conflicts); merge branches into one landing branch per repo; run the full suites; PR and
merge (app first, fluid second). Branch naming: `claude/<slug>`. Conventional Commits.

---

## 2. Already fixed — do NOT redo (rounds 1 & 2, all merged)

PRs: ai-shuffle **#1006**, **#1007**; fluid **#25**, **#26**. Verified engaging in
production by the round-2 audit:

- 16,384 stdout cap + **fair-share truncation** with named drops; ResultRef `_refs`
  registry + `_`; repeat-read → pointer (whole-file, unchanged only); mid-cell error
  manifest ("calls completed before the error").
- Grep: negated globs in the Python fallback (or hard-error); SessionSearch project
  scoping + honest counts; Bash empty output carries `[exit code N]`.
- Edit refuses `.py` edits that break a parseable file; Note/Ledger remove is exact-match;
  Monitor output ANSI-free **in fluid's supervisor** (app path still broken — item R8).
- Sync Task reports as file (`report_path` + 2,000-char `report_head`); TaskList/TaskGet
  park-don't-poll descriptions + still-running nudge; read-only agents default
  `mode=background` (in `app/core/task_dispatch.py:356-378` — models defeat it, item R4);
  task notifications: one payload, one frame per task_id.
- Plan mode: chat affirmatives resolve the gate (StopPlanning only); approved exit
  confirms and unlocks — root cause was `stop_handler` re-checking `is_active()` on the
  MRTR retry leg; stale-gate superseding, newest-gate routing, model-initiated plan exit
  sync, bypass-mode restore. 29 regression tests across both repos.
- Accounting on **completed** turns: cumulative `total_cache_read_tokens`,
  `total_cache_creation_tokens`, `total_model_calls`; `turn_count` counts turns;
  `usage_log` cache columns; backfill script `scripts/backfill_usage_from_events.py`
  (NEVER auto-run; `--dry-run` first).
- Model-family guidance blocks (glm/gpt/grok) injected on native legs; code-mode
  contracts (bind-then-slice, park-after-delegating, one-cell-per-step, recall budget)
  in `_code_mode_section` (`app/core/provider/harness_adapter.py` ~:2996-3022).
- Worker image: ripgrep, requirements-dev, `~/.local/bin` on PATH, git identity seeding;
  project memory dir mkdir; `ToolCtx.session_id` write-back; steer double-delivery fix;
  fluid_session_events delta retention (`fluid_event_retention_days`, default 7);
  commit/PR text sanitizer (`app/core/git_text.py`) on the APP's git surfaces (it cannot
  see model-driven `gh` calls in the worker — known limitation, not a bug);
  autocomplete/Tiptap 640→1080px gates; read-only background default; node worktree
  provisioning.

**Audit evidence (why the remaining list is what it is).** Round-2 audited five real
glm-5.3-flash sessions post-fix, one Opus analyst each, against the 2026-08-26 baseline:
redundant re-reads eliminated (0 repeat file fetches vs 11 files ×2-4 before), polling
cells 19/43 → 0, notifications 26KB → 5.3KB, 3+ clean park→wake cycles, all 5 sessions
shipped (4 merged PRs) for ~$0.27 total. What did NOT move: batching (0.72–1.38
calls/cell in 3 parents; 35-46% of cells were missed batches), ResultRef discipline is
only half-adopted (bind then slice-print across cells), sleep-polling survives Monitor,
and every Explore spawn hand-wrote `mode="sync"`. Full reports: this repo's history +
the round-2 report file `fluid-session-audit-round2.md` delivered to the user.

---

## 3. THE BACKLOG — every open issue, ranked

Severity: ■ = fix first (correctness/measurement), □ = high value, ○ = worthwhile.

### ■ R1 — Interrupted/killed turns book zero model calls; killed children flush nothing
**Layer:** fluid + app. Two analysts hit it independently: a session with 167 real model
calls shows `total_model_calls=11` (its two big turns were user-interrupted); another
shows 14 of 21; a user-killed sync child shows 0 calls / 0 tokens / $0 against a real
$0.00214 call, with `turns.state='completed'` and `sessions.status='active'` for a task
marked `killed`.
**Fix:** (a) fluid `fluid/events/model.py`: add `num_model_calls` to `Interrupted` (and
the error-terminal event); populate at the interrupt emission in `engine/native.py` (the
counter is in scope where `TurnComplete` is built, ~line 1719). (b) app
`app/core/provider/harness_adapter.py:1899-1937`: the `Interrupted`/`ErrorEvent`
branches build a ResultMessage with usage but no `num_turns` — carry the partial count so
`app/core/query_persistence.py` (~:442 `model_calls_increment`) books it. (c) app task
kill path (`app/core/task_registry.py` + provider_agent): flush the child's last Usage
event and mark `turns.state`/`sessions.status` terminal on kill.
**Accept:** an interrupted turn's calls/tokens land; a killed child's partial usage lands;
audit queries reconcile with `fluid_session_events` Usage rows.

### ■ R2 — Grep silently returns "(no matches)" for directory-prefixed globs under `.fluid` worktrees
**Layer:** fluid (`fluid/tools/builtins/grep.py::_ripgrep`, ~:210). rg is invoked with an
absolute root that sits under a gitignored ancestor (ai-shuffle `.gitignore` has
`/.fluid`); rg anchors `--glob frontend/src/**` against the OUTER repo root, so it never
matches — 46 of 66 searches in one audited child were false "(no matches)". Reproduced:
same tree + `.fluid/` ignore rule → prefixed glob exits 1; `**/`-leading glob matches.
**Fix:** run rg with `cwd=root` and search target `.` so globs anchor at the search root
(or normalize any `/`-containing glob that doesn't start with `**/` or `/` to
`**/<glob>`). Mirror the semantics in the Python fallback. Add a regression test with a
gitignored parent dir.
**Accept:** `Grep(path=<worktree root>, glob="frontend/src/**/*.ts")` returns hits from a
`.fluid` worktree.

### ■ R3 — Session loads are slow (PR #1005 regressions, measured)
**Layer:** app. Three causes, measured against the live DB:
(a) `app/api/events.py::_compacted_replay_start` → `fluid_transcript.latest_compaction_boundary`
reads the ENTIRE fluid event log on every open (`SELECT … ORDER BY seq`, 517ms +
disk-spill on a 97k-row session) to find a `ContextCompaction` boundary that exists **0
times in the whole database**. Fix: filter server-side (`event->>'_cls' IN
('ContextCompaction','TurnStarted')` → 26ms) or better, read the app's own
`session_events` `system.compaction_boundary` rows (`MAX(seq)` on an indexed pair, 3ms).
(b) PR #1005 deleted `CATCHUP_FALLBACK_MS` from `frontend/.../feed-controller.ts` and the
`isRestoringSession` clear from `active-session.ts:onConnected` — nothing paints until the
whole backfill folds; a stall = blank screen forever. Restore an idle fallback fold.
(c) `applyMetadataSnapshot` fetches `/sessions/{id}/live?since_update_id=0` on every open;
`get_live_session` (`app/api/sessions.py:979`) builds and ships the full transcript (up to
16MB) which the frontend discards. Add `include_messages=false` and pass it.
Then (designed change, optional): extend `_COALESCE_TYPES` with `tool.input_delta`,
collapse historical `subagent.progress` to last-per-agent and `turn.usage` to last-only.
**Accept:** opening the heaviest session paints progressively and drops >400ms of DB time;
`/live` on open returns no message bodies.

### ■ R4 — Task `mode` is model-settable and models always set it
**Layer:** app. All four Explore spawns in the audit passed `mode="sync"` explicitly,
defeating the shipped background default; one user Stop then destroyed a sync child's
finished research along with its parent.
**Fix:** in `app/core/agent_provider_models.py` (`build_task_contract`, ~:300-340):
(a) document the sync return shape — *"a sync result is JSON with `report_head`
(bounded), `report_chars`, `report_path` — there is NO `report` key; Read report_path
(offset/limit, in ONE cell) only if the head is insufficient"* — every audited session
burned 2-3 cells on `KeyError: 'report'`; (b) either drop `mode` from the input schema
when the resolved target has a background default (structural), or add: *"for a
read-only research agent, omit `mode`; pass sync only when you cannot take a single
further step without the report."*
**Accept:** a fresh glm session spawning a read-only agent without guidance dispatches background;
no shape-discovery cells in the transcript.

### ■ R5 — Empty-output sentinel broke truthiness; Grep documents an empty result it never returns
**Layer:** fluid. `(no output) [exit code 0]` is never falsy → `if out:` /
`out or 'FALLBACK'` misfire (one session "found" 20 stale refs where 7 existed; another
tried deleting a nonexistent branch). `grep.py:63` docstring says "an empty result means
no matches" while returning `searched via …\n(no matches)`.
**Fix:** keep the exit-code contract (it killed the silent-gh-success class — clear net
win); align the docs and teach the test: one line in Bash + Grep descriptions — *"the
no-result form is a non-empty string; test `'(no matches)' in out` / `'[exit code' in
out`, never `if out:`"* — and fix Grep's docstring. Optional deeper fix: have the kernel
expose a `.empty`/`.exit_code` attribute on Bash/Grep results (they're str subclasses in
codemode) so models can test semantically.
**Accept:** descriptions and behavior agree; a regression test asserts the documented
test idioms work.

### □ R6 — Monitor receipt doesn't command the park; sleep-polling survives
**Layer:** app + fluid. One session sleep-polled after 9 of 10 Monitor spawns (33.7% of
its 11.7M cache reads; 24 minutes polling for an already-queued notification); another
reproduced Monitor's own docstring poll-loop example as a foreground Bash.
**Fix:** (a) Monitor's receipt: `Monitoring started (task <id>). End the turn now — you
will be woken when it comes to rest.` (mirror the TaskList nudge that worked); app path
`app/core/monitor_spawn.py` / `app/mcp/builtin_tools.py`, fluid `tools/builtins/monitor.py`.
(b) Move the `gh pr checks` wait recipe OUT of Monitor's description and INTO the app's
GITHUB_BLOCK (`harness_adapter.py` ~:3254) rendered as a `Monitor(...)` call: *"CI takes
minutes; never wait in Bash."* (c) Consider a soft-error when a cell runs `time.sleep`
in a loop wrapping a host tool call — "park instead; a wake is scheduled for task <id>".
**Accept:** a session that starts a Monitor ends its turn in the same or next cell.

### □ R7 — Repeat-read pointer misses offset/limit re-reads; Explore children fight the cap
**Layer:** fluid. The pointer keys on exact `(path, offset, limit, mtime, size)`; audited
children re-read the same file 6-8× at different windows (77KB redundant in one), hitting
the 16KB cap on 50-60% of cells.
**Fix:** key on `(path, mtime, size)` with window-subset logic — an unchanged file already
held whole in `_refs` answers ANY window from the ref (return the slice, or the pointer
with the slicing expression spelled out). Fluid `codemode/truncate.py` + `_runner.py`.
Pair with worker search guidance: *"print
only what goes in the report — the cap is a backstop, not a budget; bind, check len(),
print slices."*
**Accept:** a child re-reading an unchanged file at a new offset gets the ref answer, not
a second payload.

### □ R8 — ai-shuffle's Monitor summary path still ships ANSI
**Layer:** app. Fluid's supervisor strips ANSI, but ai-shuffle overrides supervision
(ruling A13): `app/core/monitor_spawn.py::_summarize` and
`app/mcp/builtin_tools._spawn_background` head/tail-slice the raw buffer. The baseline's
ANSI-regex-fail → full-vitest-rerun failure recurred verbatim (34s wasted).
**Fix:** import `fluid.tools.builtins._util.strip_ansi`, apply before slicing, both
sites; regression test asserts no `\x1b[` in summaries.

### □ R9 — In-stream provider 520s are hard-coded non-retriable
**Layer:** fluid. `fluid/providers/_chat_completions.py:292` returns `retriable: False`
for every in-stream OpenRouter error chunk regardless of embedded code;
`_retry.py:14 RETRIABLE_STATUSES = {408,429,500,502,503,529}` omits Cloudflare 520-524.
Cost: a Z.AI 520 burned $0.007 + 6 calls and required a human "Resume".
**Fix:** classify stream error chunks by embedded code; add 520-524 to retriable set.

### □ R10 — Batching prompt tweaks (the round-2 downtick is mostly missed batching)
**Layer:** app prompt (`harness_adapter.py` `_code_mode_section`). Evidence: 35-46%
missed-batch cells across three parents; "one cell per step" read as "one tool per cell";
edit-then-verify split 6× in one session; bound-variable slices printed across empty
cells (12 of 16 misses in one session).
**Fix — add three concrete lines:** (a) *"a fixed setup→check→test recipe you already
know end-to-end is ONE step, not three; before ending a cell ask whether the next cell
touches the same file or working tree — if so it belongs in this one"*; (b) *"after
binding a large result don't print it in slices across cells — locate in-cell:
`i = txt.find('needle'); print(txt[i-200:i+800])`"*; (c) keep the existing
edit+verify-same-cell line but put it adjacent to these. Keep total addition tight;
prompt text is cache-sensitive.

### ○ R11 — Glob (codemode) returns a string but reads as a list
**Layer:** fluid `tools/builtins/glob.py::describe()`. "Returns matching paths… sorted"
led a model to `len(paths)` (=char count) and `paths[:300]` (=300 chars). Fix the
description ("returns one newline-joined string with a header line") or return a real
list in codemode.

### ○ R12 — Credential scrubbing in persisted transcripts
**Layer:** app. A model pasted the full `DATABASE_URL` (password included) into cell
text now permanent in `fluid_session_nodes`. Add a scrub of
`postgres(ql)?://[^:]+:[^@]+@` (and similar URL-credential patterns) in the node-persist
path; consider retro-scrubbing existing rows (coordinate with the user first — it's a
data migration). ALSO: rotate that Postgres password (tell the user; don't do it
unilaterally).

### ○ R13 — Kill/delete lifecycle hygiene
**Layer:** app. Deleting a session orphans its children's rows (`turns`,
`session_events`, `runs`, `usage_log`, `background_tasks`, and children's
`parent_session_id` dangle); a killed child reads `turns.state='completed'`,
`sessions.status='active'`; `SessionParked` followed by `Interrupted` reads as a park in
the event stream (the `turns` table is authoritative — fine, but worth a marker event).
Fix cascade/cleanup on delete + terminal states on kill (overlaps R1c — same agent).

### ○ R14 — Shared git-stash hazard in worktrees
**Layer:** prompt (+ optional app guard). A model's mis-scoped `git stash` (+
unconditional `pop`) popped ANOTHER session's autostash into its worktree — 5 merge
conflicts. Worktrees share the main checkout's stash stack. Add a prompt rule: never
bare `git stash`/`pop` in a worktree; use `git stash push -u -m "<tag>"`, apply by SHA,
or prefer a WIP commit. Optional: Bash soft-warning on bare `git stash pop`.

### ○ R15 — Bulk-branch-delete guardrail
**Layer:** prompt (GITHUB_BLOCK). A model nearly deleted another live session's branch:
it applied a stale `git branch --merged` listing minutes later (saved only by git
refusing to delete a checked-out branch). Add: *"re-read `git worktree list` immediately
before any bulk branch delete — another session may have created one since your
listing."*

### ○ R16 — Notification template cosmetics
**Layer:** app (`turn_scheduler.py` `_build_task_notification_prompt`). Empty
`<tool-use-id>`, `<output-file>`, `<usage>` tags render for shell monitors; a shell
monitor is labeled a "subagent". Omit empty fields; label monitors as monitors. Also 8
of 9 monitor notifications carried `(no output; exit code 0/None)` because the model
redirected output to a log file — consider including the log tail when the buffer is
empty.

### ○ R17 — Parked items (need the user, do not start unprompted)
- Run `python -m scripts.backfill_usage_from_events --dry-run` against prod, review, then
  apply (fixes historical accounting; R1 should land first so new data is right).
- Continual-harness check: whether codemode skill synthesis produces anything models
  import (server-side journal/skills dir inspection).
- The retention window (`fluid_event_retention_days=7`) deletes ~0 rows today because
  all delta rows belong to recent sessions; after R3 lands, discuss shortening the
  window or pruning deltas at session-close instead.
- Leftover locked dir: `D:\Development\omni-harness\.claude\worktrees\audit-integration`
  (and possibly `\landing`) — inert, delete when file locks clear.

---

## 4. How to verify (the user's standard — behavior, not features)

Unit tests prove mechanics; the REAL test is live glm-5.3-flash sessions doing real
coding work, audited from the DB. Method that the user endorsed: run several real tasks,
then reconstruct each session from `fluid_session_nodes`/`fluid_session_events` (one
Opus analyst per session) and grade **engagement** against these round-2 numbers:

| Metric | Round-2 value (target: improve) |
|---|---|
| Repeat file fetches (parent) | 0 — keep at 0 |
| Missed-batch share of cells | 35-46% in 3 parents — drive down |
| Sleep-poll after Monitor | 9/10 spawns in worst session — should be ~0 after R6 |
| Explore spawns passing mode="sync" | 4/4 — should be 0 after R4 |
| Task shape-discovery cells | 2-3 per delegating session — 0 after R4 |
| Child cap-hit rate | 50-60% of cells — down after R7 |
| Interrupted-turn call accounting | 11 vs true 167 — exact after R1 |
| Grep false "(no matches)" | 46/66 in one child — 0 after R2 |

Feature-level checks (secondary) live in `docs/AUDIT_FIX_VERIFICATION.md` (V1-V10).
Do not run live model tests without telling the user first — they want to watch.

## 5. Useful session ids (round-2 evidence, in the live DB)

Baseline (pre-fix, 2026-08-26 report): `ses_2976e0dd`, `ses_83a88dd4`, `ses_cc1664e4`,
`ses_9a4074ab`, `ses_8dbd7280`, `ses_67ac0208`.
Round-2 (post-fix): `7dda6c26` (worktree cleanup; best batching 5.26/cell; truthiness
bugs), `d8ea7bc5` (+child `2895e9c1`; slice-print pattern; Grep glob bug), `8c584b12`
(+child `fd73b11a`; sleep-poll 33.7%; stash incident; credential paste; interrupted
accounting), `0856011a` (+child `5da982e2`; ANSI recurrence; parks), `53b4427f`
(+forensic `d577b0b5` killed child; 520 retry; Stop-kills-sync-child), `3311ca86`
(clean utility session).
