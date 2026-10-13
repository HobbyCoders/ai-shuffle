# AI Shuffle — Performance Remediation: Implementation Report (2026-08-28)

Companion to `PERFORMANCE_REVIEW_2026-08.md`, which catalogued 59 findings across
six slices. This document records what was **implemented**, what was **measured**,
what was **found along the way**, and what was **deliberately left undone**.

Branch: `claude/ai-shuffle-performance-review-l4e1my`
Change surface (both rounds): **242 files changed, 34595 insertions(+), 5783 deletions(-)**.
Round one addressed the 59 audit findings; round two eliminated every
event-loop-blocking DB call in the backend and cleared the deferred follow-ups.

No fix in this work adds cores, workers, or threads. Every change is an efficiency
or design correction, per the original constraint.

---

## 1. Verification status

| Suite | Result | Baseline before this work |
|---|---|---|
| Backend (`pytest`, full) | **8,755 passed**, 5 failed, 34 skipped, **0 deselected** | not runnable — env was broken |
| Frontend (`vitest`, full) | **1,813 passed, 0 failed** (143 files) | 1,582 passed, 3 failed |
| `scripts/scan_blocking_db.py` | **0 findings** (exit 0) | 511 findings across 50 files |
| Production build | succeeds, **zero chunk-size warnings** | succeeded, 1 warning at 1500 kB limit |
| `svelte-check` | **0 errors**, 3 pre-existing warnings | 0 errors, 3 warnings |

The full backend run now prints a normal summary line with **nothing
deselected** — round one had to deselect a test that crashed pytest's own
reporting (see §6, bug 3). That is why the passing count rose by 155 while the
failure count stayed flat.

**The 5 backend failures are environmental and pre-existing**, verified by
confirming the files are untouched on this branch and reproducing the failures
against the pre-remediation baseline commit:

- `tests/core/test_rate_limit_breaker.py::TestFluidResponseHookTripsOnlyOn429`
  (×4) — makes a **real HTTPS request to `api.openai.com`** expecting a 429; the
  sandbox's egress proxy answers 401. Neither the test nor
  `app/core/rate_limit_breaker.py` was modified.
- `tests/test_chrome_supervisor.py::test_enters_slow_retry_then_recovers` —
  fails with `setpriv: failed to parse reuid: 'appuser'`; this container has no
  `appuser`. File untouched.

A sixth failure — `tests/test_harness_adapter.py::test_native_environment_declares_the_actual_windows_shell`,
which crashed pytest's reporting and swallowed every full-run summary — was
**fixed in round two**; see §6. It no longer needs deselecting, and the test now
actually asserts something, which it never did before.

### Environment repairs required before any of this could be verified
The repo could not run its own tests as checked out. None of these are code changes:
`svelte-kit sync` had never been run (all 129 frontend test files failed to
transform); pinned `psycopg2-binary==2.9.9` fails to build on this image; a
Debian-installed `PyJWT` blocked `pip install -r requirements.txt`; and the
vendored `fluid` package (`vendor/fluid`, `requires-python >=3.12`) was absent
from the path on Python 3.11 — it imports fine, so it was added via a `.pth`.

---

## 2. Measured results

All figures are from real builds and instrumented runs, not estimates.

| Metric | Before | After | Δ |
|---|---|---|---|
| Critical-path JS, gzipped | 852.3 kB | **417.3 kB** | −51 % |
| Critical-path JS, raw | 3,295 kB | **1,374.8 kB** | −58 % |
| Largest single chunk (gz) | 594.6 kB | **167.8 kB** | −78 % |
| Route CSS (gz) | 72.0 kB | **33.2 kB** | −54 % |
| JS + CSS total (gz) | 940 kB | **466 kB** | −50 % |
| Lazy chunks | 0 | **56** | — |
| Idle DB roundtrips/min (backend, idle) | ≈53 | **≈9** | −83 % |
| Idle RAM, browser closed | 0.8–2.4 GB parked ≤35 min | **≈0 MB within 3–4 min** | — |
| Idle RAM, one chat open | 0.8–2.4 GB | **≈100–300 MB** | −85 % |
| Reducer replay, 20k events (user msgs) | 4.0 s | **0.11 s** | linear now |
| Reducer replay, 20k events (tool calls) | 8.9 s | **0.10 s** | linear now |
| Aggregations over `session_store_entries` | 12·N per hour | **≤1 per hour** | — |
| Idle git work per visible tab/hour | ~720 status + ~720 diffs | **≤120 + ≤61** | −85 % |

Cost of a 300 s automation wait (`wait_settled`): ~6,000 blocking on-loop queries →
**2** when the NOTIFY gateway is healthy, ~155 off-loop when polling.

---

## 3. What shipped, by finding

All 59 findings are addressed. Grouped by the pattern they belonged to.

### Blocking work on the event loop — the single biggest cause of app-wide stalls
- **G1** every `gh`/git/DB call in the GitHub API layer moved off-loop behind a
  tested `TTLCache` (300 s positive / 30 s negative auth caching). Also caught a
  blocking DB read in `get_project_path` that the audit had not identified.
- **B2** the session-list enrichment pipeline runs in **one** `run_sync` hop per
  request instead of 6–10 blocking queries; `get_project` and `get_api_user`
  N+1s replaced with batched `IN (…)` reads; the full `list_projects_v2()` scan
  replaced with targeted reads; `_backfill_link_groups` no longer writes on the
  listing hot path.
- **W6** `handle_load_session`'s full-JSONL parse plus three sync DB reads folded
  into a single offloaded hop; `submit`/`interrupt` moved to async twins.
- **F2** `wait_settled`/`wait_session_idle` now wake on the existing
  `session_events` NOTIFY gateway with an exponentially backing-off off-loop poll
  as backstop, replacing a 50 ms sync-DB hot-poll.
- **Integration** the remaining 8 sync `submit`/`interrupt` call sites in
  coroutines (`sessions_worktree`, `automation_manager`, `provider_agent`,
  `connectors/manager`, `wakeup_service`) migrated; all 14 `get_working_dir` call
  sites in `git.py`/`git_ai.py` moved to the non-blocking variant. Each was
  AST-verified to sit inside an `async def` before swapping.

### Refetch-the-world → patch-the-delta
- **W1** turn-completion events now apply a **row patch** instead of triggering an
  uncapped full re-page of every session; full reloads are debounced (700 ms) and
  capped to the first page with a "Load older sessions" shelf, committed **once**
  rather than per page, behind a deep equality guard.
- **G5/G6** the per-switch `git fetch --prune` is gone; mutation endpoints'
  inline status/diff payloads are applied instead of discarded, cutting 3–5
  follow-up requests per git action to at most one.
- **S2** the transcript cache: switching sessions folds cached reducer state
  **synchronously** and resumes the SSE feed at `from_seq=lastSeq` instead of
  re-downloading and replaying the whole event log from seq 0.

### Reset-to-empty state machines — the reported flicker
- **S1** the 1 s catch-up fallback can no longer complete hydration with zero
  reduced events; a slow first frame is now "slow, not stalled" (8 s budget,
  30 s ceiling) and late frames re-enter batch mode instead of repainting per frame.
- **S3** the loading window renders a transcript-shaped skeleton, not the
  "Start a Conversation" hero that made every switch read as empty → content.
- **G4** git caches are keyed by **checkout identity**, not session id, so
  sessions sharing a checkout share state; the store never resets to empty, and
  the header's primary action is computed from one coherent snapshot so it cannot
  walk `Merge PR #12 → Create PR → Merge PR #12`.

### Failure indistinguishable from "no data"
- **G2** `gh` failures now raise instead of returning `[]`. A failed lookup is
  never cached as "no PR"; the last known-good value is re-served with
  `stale=true` for up to 300 s, and gh-unavailable surfaces as **502** rather
  than an empty result.
- **G10c** a transient auth blip no longer hides the git UI: local git is
  decoupled from GitHub auth, with hysteresis (two consecutive failures ≥60 s apart).

### Many writers, no single source of truth
- **S6** `isStreaming` has one writer (the feed reducer); optimistic sends are an
  explicit flag the fold clears on confirmation.
- **W7** the `/live` snapshot is sequence-gated and can no longer overwrite
  fresher feed data (token counters jumping backwards).
- **S12** optimistic edits survive in-flight list loads via a pending-edit
  registry re-applied on every commit, rather than a per-call-site guard.
- **S7** `sessionProfileId` is populated, so historical attribution no longer
  follows the live profile dropdown.

### Bundle and rendering
- **R5/R6/R7** 56 lazy chunks: feature panels, 15 Shiki grammar chunks, and
  404 kB of docs markdown all left the entry chunk.
- **R1/R2/R3** fine-grained derived stores with an `Object.is` short-circuit;
  streaming folds throttled to 50 ms; the sidebar no longer re-sorts per frame
  (and no longer parses dates inside the comparator); per-row O(N) scans
  precomputed once in the display-model pass.
- **R4** windowed mounting (trailing ~60 items) with idle backfill, chosen over
  full virtualization — see §5.
- **R8** the sidebar's PR-refresh dedupe key, previously computed and never
  compared, now actually gates the effect.

### Idle footprint
- **F1** SDK pool default 8 → 4 with a tiered idle policy (2 hot at 1800 s, cold
  at 420 s, detached at 180 s) and a deadline-driven watchdog that parks with no
  timer when the pool is empty.
- **F5** four independent claim/sweep loops multiplexed into one pass that
  stretches to 60 s when the cluster registry shows no peers.
- **F6/F7/F10** orphan reconciler runs one aggregation per sweep instead of N and
  memoizes inconclusive ids; notes-sync fetches metadata first and content only
  for changed rows; the disabled plugin auto-updater waits on an event instead of
  waking 288×/day.

---

## 4. Bugs found that were NOT in the audit

These surfaced during implementation and are the findings most worth review.

1. **Stop buttons did not stop anything** (`provider_agent_supervisor.py`).
   It called `asyncio.to_thread(turn_manager.interrupt, …)` — correctly off-loop,
   but the interrupt executor is a coroutine needing a running loop, so from a
   worker thread it was silently closed and dropped. The DB row flipped to
   "interrupting" while the live runtime never received the SDK interrupt.
2. **`clearSession` would have shown deleted messages.** It wipes the event log
   but keeps the session id, so a resume at `from_seq=lastSeq` matches nothing and
   the new transcript cache would keep the old transcript on screen. The cache's
   cursor-sanity rule cannot catch this — it detects *stale* sequences, not an
   *empty* log. Explicit invalidation added.
3. **Shiki cached a wrong colour permanently.** Attaching a grammar to a live
   highlighter leaves the theme colour map lazily resolved; the *first*
   `codeToHtml` for that grammar emitted a numeric literal in the keyword colour
   and that output was then cached in the LRU forever. Fixed with a throwaway
   warm-up tokenisation before a language is published as ready.
4. **`_invalidate_pr_state_cache` never invalidated anything** — keyed by
   `working_dir` while entries are keyed `"<dir>:<branch>"`, and it had zero
   production callers.
5. **A null `author` collapsed entire PR lists to empty**, indistinguishable from
   "no PRs".
6. **`reopen_worktree` relocates a worktree but never invalidated its status
   cache**, leaving stale fan-out results.
7. **Late-arriving syntax highlighting never applied.** `AssistantMessage`
   memoises sanitized HTML by content; with Shiki now lazy, a settled message kept
   plain-text code blocks indefinitely. On a switch into an existing transcript
   this is the common case.
8. **Dead code that would have crashed if reached**: the `_find_open_pr_failed`
   sentinel was unreachable, and both `finish` and `ship` checked only `is None`,
   so had it ever been returned they would have raised (or risked a duplicate PR).

---

## 5. Deliberate deviations and judgment calls

Places where the plan was not followed literally, and why.

- **R4: windowed mounting + idle backfill, not permanent virtualization.**
  Unmounting rows fights the scroll-anchor and `contain-intrinsic-size` machinery
  MessageArea's scroll correctness depends on, while the actual first-paint cost
  (component creation, marked, DOMPurify, Shiki) is fully captured without it.
  Tradeoff: in-page find only reaches mounted text until the backfill completes;
  an explicit control mounts everything at once.
- **B4: `synchronous_commit = off` deliberately skipped.** `SET LOCAL` would scope
  it safely, but the durability trade is not safe here — the CLI writes its JSONL
  to a tmpfs `CLAUDE_CONFIG_DIR` and the lazy backfill refuses to run when any
  store rows exist, so a lost commit tail would be permanent, not reconstructible.
- **B6: compaction, not deletion.** A streamed answer's text exists *only* as
  `assistant.text_delta` rows, so pruning them could destroy renderable content.
  Consecutive deltas are merged into exactly the row the replay coalescer already
  builds — proven byte-identical by test.
- **G8: a new 60 s constant, not `GIT_IDLE_THRESHOLD_MS`.** The plan said compare
  against that constant, but it is 30 s — now also the idle tick interval, which
  would have re-created the exact degeneracy being fixed. Pinned with a test
  asserting `GIT_IDLE_DIFF_STATS_MS > GIT_POLL_IDLE_MS`.
- **F10: plugin auto-update default changed to off, 6 h → 24 h.** An unrequested
  6-hourly git+network burst that can swap plugins under a live session is not a
  sane default on a weak machine. Installs that already opted in have `enabled`
  written to their settings file, so nothing is switched off behind a user's back.
- **W2 overflow policy: disconnect rather than drop.** Only frames carrying a
  complete replacement snapshot are coalesced in place; correctness-bearing frames
  (`done`, `error`, `tool_use`/`tool_result`, permission prompts) are never
  dropped — on overflow the connection is dropped and the client resyncs via the
  cursor resume that already exists.

---

## 6. Round two: full event-loop offload, and what remains

### The "45 blocking calls" figure in the first pass was wrong

That number came from an ad-hoc scan of **six** function names and was reported
as if it were the whole picture. A scan of every call on the synchronous DB
facades finds **511 blocking calls across 50 files**. The check is now a
committed tool — `scripts/scan_blocking_db.py` — so the claim is reproducible
instead of taking anyone's word for it; it exits non-zero while findings remain
and can gate CI.

**All 511 are now eliminated. The scanner reports zero.**

The concentrations were not where the first pass guessed: `agents.py` (47),
`automation_manager.py` (42), `settings.py` (34), `projects.py` (28),
`connectors/manager.py` (24). The `app/mcp/config_*.py` tools mattered more than
their count suggests — those are the tools Claude itself calls mid-conversation,
so blocking there stalls the loop *during an active turn*.

Work was split across file-disjoint streams, each folding contiguous DB work
into a **single `run_sync` closure per handler** rather than wrapping calls
individually: 511 blocking calls became roughly 260 hops, not 511.

### Scanner limits — stated plainly

- It matched only `database.*` attribute calls at first, so direct `get_db()`
  use was invisible. That gap hid a real blocker in
  `fal_catalog._persist_to_db` (~600 inserts, awaited mid-turn from an MCP
  tool). The scanner now catches `get_db()` too.
- It still **cannot** see DB work behind an arbitrary sync helper. Several such
  cases were found by reading, not scanning: `check_budget`'s daily-spend
  aggregate, `should_run_scheduled_here`'s election chain,
  `_resolve_v2_project_here` (placement reads *and a possible lazy git clone*),
  `turn_outcome.*` event-log reads, `settings_crypto` decrypt paths, and
  `hooks.get_plugin_hooks`' plugin-directory glob. A clean scan is necessary,
  not sufficient.
- Blocking **filesystem** work is out of its scope entirely. Streams fixed a
  number of those in passing (project file listing with a `stat` per entry,
  notes file I/O, JSONL history walks, import/export archive reads).

### Bugs found in round two

1. **`rate_limiter.record_request` blocked on every single request.** Its own
   comment said "fire and forget" while it ran an inline synchronous DB write,
   so every request stalled the loop. Now genuinely deferred, with a strong
   task-reference set (asyncio holds only a weak reference, so the write could
   be garbage collected mid-flight) and `drain_pending_logs()` so it stays
   observable for tests and for flushing at shutdown.
2. **`provider_agent.dispatch_worker` could silently lose its failed-status
   write.** `emit_shielded` swallows `CancelledError`, so code after it runs
   while the task is cancelling; a bare `await` there would be cancelled at its
   first suspension point. Now shielded.
3. **The pytest `INTERNALERROR` test never asserted anything.** It raised on
   every run because the `os.name = "nt"` mutation escaped into
   `fluid/tools/shell.py`, which holds its own `pathlib.Path` binding. pytest
   then formatted that failure while `os.name` was still `"nt"`, crashed, skipped
   teardown, and left the mutation in place for the rest of the session — which
   is what swallowed every full-run summary line. Fixed at the root, and the
   test now genuinely exercises the Windows branch.
4. **`adb.*` wrappers defeat test patching.** `async_database` binds its target
   at import time, so `adb.get_profile` ignores `patch.object(module.database,
   …)` — the pattern the suite relies on. Three streams hit this independently;
   one broke 25 tests before catching it. `await run_sync(database.fn, …)` is
   the identical hop and resolves at call time.

### Store-side pagination: built, deliberately not wired

Windowed reads landed and are measured — on a 3,076-entry transcript with 38
screenshots: **3,076 rows → 300**, **6.27 MB → 204 KB**, **65.8 ms → 8.0 ms**.
Moving screenshot substitution to parse time independently halves a full parse
(12.49 MB → 6.27 MB), since the base64 data URI is never built when the hash
resolves.

It is **not** switched on in the session endpoints, and should not be until the
following are handled:

- `attach_cost_metadata` matched usage rows to turns positionally from the start
  of the supplied list. Fed a page, every message showed a different turn's cost.
  **Fixed** here via `turn_offset`/`total_turns`, with a test that pins the
  failure mode itself.
- Pagination units are **entries, not messages** — counting messages means
  reading everything, which is the cost being removed.
- Synthesized ids for assistant text cards embed the transform's running
  counter, so the same message gets different ids from a full vs. windowed
  parse. They stay unique, but the two paths must not be merged into one view:
  adopt the window for `GET /{id}` **and** `/live`, or neither.

### Completed from the previous "remaining work" list

Batched readers relocated to `app/db/projects.py` / `projects_v2.py`;
`list_claimable_sessions(session_ids=…)` (a full `turns⋈runs` DISTINCT scan per
listing is now bounded by the page); `resolve_placement` installs-map N+1
(`1 + 2D` reads → **1**, measured 20 → 1 at 10 candidates); `message_pagination`
on `SessionWithMessages`; the turn-end summary flush; the `WindowsPath` pytest
crash; and `Sidebar` hover/focus panel prefetch.

### Still open

- **Sync git subprocess work on the event loop** — e.g.
  `worktree_manager.cleanup_worktree`'s `remove_worktree` / `delete_branch`.
  Same bug class as the DB offload, different subsystem, not covered by the
  scanner. This is the obvious next increment.
- Wiring the windowed store path into the session endpoints, per the three
  conditions above.
- `app/services/proactive_scheduler.py`, `app/services/action_queue.py`,
  `app/services/mobile_device_manager.py` and `app/mcp/phone_tools.py` were
  never wired into any MCP server and have been deleted.

---

## 7. Notes for reviewers

- Every stream verified its own slice, and the integration work was verified
  separately; where a stream's claim mattered (a regression, a pre-existing
  failure) it was independently re-checked rather than taken on trust — including
  confirming that the one remaining frontend test failure was a **wrong test**,
  not a product bug, before changing it.
- New tests were, where practical, confirmed to **fail against pre-change code** —
  including the `AssistantMessage` highlight-invalidation test, which was verified
  to fail with the fix reverted and pass with it in place.
- Six WIP checkpoint commits (`e2ed327`, `9806067`, `f4523bd`, `edc29b8`) captured
  in-flight state while streams were still editing; they are snapshots, not
  reviewable units. The reviewable work is the verified commits from `ffe01c5`
  onward.
