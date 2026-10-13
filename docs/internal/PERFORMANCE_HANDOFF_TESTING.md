# Performance Remediation — Testing Handoff

**For the next session.** This is what changed, what to test first, what is most
likely to be broken, and what was deliberately left alone.

Read alongside:
- `docs/PERFORMANCE_REVIEW_2026-08.md` — the original 59-finding audit
- `docs/PERFORMANCE_REMEDIATION_2026-08.md` — what shipped, measured results, deviations

---

## 1. State

- Work branch: `claude/ai-shuffle-performance-review-l4e1my`, merged to `main`.
- `origin/main` was merged **into** the branch first (5 commits: browser-panel
  point-and-prompt, compaction fix). Only two files overlapped
  (`app/core/config.py`, `tests/test_harness_adapter.py`); both auto-merged and
  were hand-verified.
- Change surface: ~245 files, roughly +35k / −6k lines.

**Nothing here has been run against a live instance.** Verification was tests,
builds, type-checks and static analysis only. That is the single biggest gap and
the reason this handoff exists.

### Automated status at handoff

| Check | Result |
|---|---|
| `python -m pytest` | **8,771 passed**, 5 failed (all environmental — see §5), 34 skipped |
| `cd frontend && npx vitest run` | **1,828 passed, 0 failed** (144 files) |
| `python scripts/scan_blocking_db.py` | 0 findings (exit 0) |
| `cd frontend && npm run build` | succeeds, no chunk-size warnings |
| `cd frontend && npm run check` | 0 errors, 3 pre-existing warnings |

---

## 2. Environment — read this first, the repo does not test clean out of the box

None of these are code problems; they will bite you on a fresh container:

```bash
# Frontend: without this, ALL 143 test files fail to transform
cd frontend && npx svelte-kit sync

# Backend: the pinned psycopg2-binary==2.9.9 does not build on this image
pip install psycopg2-binary

# A Debian-installed PyJWT blocks the requirements install
pip install --ignore-installed PyJWT -r requirements.txt -r requirements-dev.txt

# vendor/fluid declares requires-python >=3.12 but imports fine on 3.11;
# it must be on the path or 9 test modules fail to collect
echo "/home/user/ai-shuffle/vendor/fluid" > \
  "$(python -c 'import site; print(site.getsitepackages()[0])')/aishuffle-vendor-fluid.pth"
```

---

## 3. Manual testing priority — highest risk first

Ordered by *blast radius × likelihood of a subtle break*, not by how much code changed.

### P0 — session loading and the chat surface
The most-changed user-visible path, and the one the original complaint was about.

1. **Open a long existing session (500+ messages).** Expect: no "Start a
   Conversation" hero, no "Conversation cleared" flash, a skeleton then content.
   Watch for the transcript rebuilding message-by-message — that was the old bug.
2. **Switch A → B → A rapidly.** The transcript cache should make the return to A
   instant. Watch for a *stale* transcript (worse than a slow one) — cache
   invalidation rules are in `feed-controller.ts` (R1–R6).
3. **Clear a session, then reopen it.** Must be empty. If deleted messages
   reappear, the cache invalidation on clear regressed (this was a real bug found
   and fixed — see §4).
4. **Rewind / fork a session, then reopen.** Same class of risk.
5. **Send a message and watch the streaming indicator** at turn start. It should
   not stutter on/off — `isStreaming` now has a single writer.
6. **Scroll up through a long transcript.** Windowed mounting loads ~60 items and
   backfills on idle; check scroll position does not jump when older items mount,
   and that in-page find works after the backfill completes.

### P1 — git / PR header
The second-biggest complaint, and the area with the most state-machine change.

1. **Switch between two sessions on the same checkout.** The header must not
   flash empty, and must not walk `Merge PR #12 → Create PR → Merge PR #12`.
2. **Break GitHub deliberately** (`gh auth logout`, or block network). Expect:
   local git (commit/push/pull/branch) keeps working; Create PR / Finish /
   Ship It hide; the PR state shows last-known with a "GitHub unreachable"
   marker rather than reverting to "Create PR".
3. **On a `master`-default repo**, confirm Ship It / Create PR do *not* appear on
   the default branch (this was wrong before — it assumed `main`).
4. **Commit, push, and open a PR** and confirm the header updates without a
   full-page refresh.

### P2 — the offload work (511 call sites)
Mechanically similar changes across 50 files. Individually low risk, collectively
the largest surface. Focus on **mutation endpoints**, where a read-decide-write
was folded into one closure:

- Create/rename/delete a **project**, **note**, **tag**, **template**, **profile**,
  **subagent**, **hook**, **automation**, **connector**.
- **Import/export** settings and sessions (bulk loops were folded whole).
- **File browser**: upload, download, create folder, rename, delete (including a
  large folder — `rmtree` is now offloaded).
- **Settings**: API keys, voice/TTS, media, credential policies.
- **MCP config tools** — ask Claude *in a session* to list/create/update a
  profile, subagent, hook or automation. These run mid-turn; a break here shows
  up as a failed tool call.

### P3 — background behaviour (needs time, not clicks)
1. **Leave the app idle 10 minutes with the browser closed.** SDK subprocesses
   should be reclaimed within ~3–4 min (`ps aux | grep node`). Then reopen a
   session and confirm it resumes correctly — resume-after-eviction is the risk.
2. **Leave a session open and idle.** Confirm git polling backs off (30s idle,
   not 5s) and stops when the git UI is not visible.
3. **Run an automation** end to end, including one that hits its budget cap and
   one that times out (the interrupt path was changed).
4. **Stop button during a long turn** — verify it actually stops the runtime, not
   just the UI. This was genuinely broken before (see §4).

---

## 4. Bugs found and fixed — regression-test these specifically

Each was a real defect discovered during the work, not a hypothetical:

1. **Stop buttons did not stop anything.** `provider_agent_supervisor` called
   `to_thread(turn_manager.interrupt, …)`; the interrupt executor is a coroutine
   needing a running loop, so from a worker thread it was silently dropped. The
   DB row flipped to "interrupting" while the runtime kept going.
   → *Test: stop a long-running turn on a delegated worker.*
2. **`clearSession` would have shown deleted messages** once the transcript cache
   landed — same session id, emptied log, so a cursor resume matched nothing.
   → *Test: clear a session and reopen it.*
3. **Late-arriving syntax highlighting never applied.** Messages memoise
   sanitized HTML by content; with Shiki now lazy, a settled message kept plain
   code blocks forever. → *Test: open a session containing code, in a fresh tab.*
4. **Shiki cached a wrong colour permanently** — the first render after a grammar
   attached emitted a numeric literal in the keyword colour, then cached it.
   → *Test: view code in several languages.*
5. **`rate_limiter.record_request` blocked on every request** while its comment
   claimed fire-and-forget. → *Test: sustained request load stays responsive.*
6. **PR-state cache invalidator never invalidated** (keyed by directory, entries
   keyed directory+branch) and had zero callers.
7. **A null `author` collapsed entire PR lists to empty**, indistinguishable from
   "no PRs".
8. **`reopen_worktree` never invalidated its status cache.**
9. **A pytest test that never asserted anything** — it raised on every run, and
   the crash swallowed the summary line of every full-suite run.

---

## 5. Known failures — do not chase these

**Backend (5, all environmental, files untouched on this branch):**
- `tests/core/test_rate_limit_breaker.py::TestFluidResponseHookTripsOnlyOn429`
  (×4) — makes a **real HTTPS call to `api.openai.com`** expecting 429; the
  sandbox proxy answers 401. Will pass on a machine with real egress.
- `tests/test_chrome_supervisor.py::test_enters_slow_retry_then_recovers` —
  `setpriv: failed to parse reuid: 'appuser'`; no such user in this container.

**Frontend:** none.

Both were verified by reproducing them at the pre-work baseline commit.

---

## 6. Deliberately not done — and why

1. **Store-side transcript pagination is built but NOT wired.** Measured
   3,076 rows → 300 and 65.8 ms → 8.0 ms, but enabling it needs two more things:
   - Pagination units are **entries, not messages** (counting messages means
     reading everything, which is the cost being removed).
   - Synthesized ids for assistant text cards embed a running counter, so the
     same message gets different ids from a full vs. windowed parse. Adopt it for
     `GET /{id}` **and** `/live`, or neither — never merge results from both.
   The cost-attribution blocker (positional turn matching) **is** fixed:
   `attach_cost_metadata` now takes `turn_offset` / `total_turns`.
2. **Two blocking calls knowingly left:** `resource_monitor`'s `/proc` fd listdir
   (a few ms on a 5-minute tick) and `core/auth`'s `subprocess.Popen`, which
   spawns without waiting.
3. **Behaviour defaults changed on purpose** — verify these suit you:
   - SDK session pool 8 → **4** (hot tier 2), idle eviction much sooner.
   - Plugin auto-update default **off**, interval 6 h → 24 h. Existing opt-ins
     keep their stored setting.
4. **Schema is now v84** — three additive/nullable columns plus a one-shot
   backfill (`is_worker`, `folded_through_id`, `usage_log(session_id)` index),
   exercised against a populated pre-v84 database in tests. **Back up before
   first run on real data.**

---

## 7. Tools left behind

```bash
# Static check: sync DB calls on the event loop. Exit 1 while findings remain,
# so it can gate CI. Run it after any backend change.
python scripts/scan_blocking_db.py                 # whole app
python scripts/scan_blocking_db.py app/api/x.py    # detailed, per file
```

**Its limits, stated plainly** — a clean scan is necessary, not sufficient:
- It cannot see DB work behind an arbitrary sync helper. Several such cases were
  found only by reading, including a possible lazy **git clone** inside a
  placement check.
- It does not cover blocking **filesystem** or **subprocess** work at all. A
  one-off AST sweep found and fixed 5 of those; the remaining known ones are in
  §6.2.

---

## 8. Next increment

Sync **git subprocess** work still on the event loop — e.g.
`worktree_manager.cleanup_worktree`'s `remove_worktree` / `delete_branch`. Same
bug class as everything above, different subsystem, invisible to the scanner.
Worth its own pass with tests rather than a quick patch.
