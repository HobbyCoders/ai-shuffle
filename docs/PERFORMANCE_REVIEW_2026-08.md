# AI Shuffle — Full Performance Review (2026-08-28)

Six parallel deep audits were run across the app's core slices, with every finding
traced to code (file:line). This document consolidates them into one report:
symptom → root cause mapping, severity-ranked findings, and a phased remediation
plan. **No recommendation below adds cores, workers, or threads** — every fix is an
efficiency or design correction, in line with the goal of running well on weak
machines.

Slices audited:

1. Session loading & chat-view state machine (frontend)
2. Rendering performance & UI fluidity (frontend)
3. Git / GitHub / PR state pipeline (end-to-end)
4. Realtime plumbing: WebSocket, SSE event feed, event gateway
5. Backend persistence & session data path (Postgres, transcripts, APIs)
6. Idle/background resource footprint (pollers, watchers, subprocesses)

---

## 1. Executive summary

The app's sluggishness is not one bug — it is a small number of systemic patterns
that repeat across slices:

| Pattern | Instances | Consequence |
|---|---|---|
| **Blocking sync work on the asyncio event loop** | `gh`/git subprocesses in `app/api/github.py`; session-list tag/fork enrichment; cost-metadata attachment; JSONL parse in `handle_load_session`; `turn_manager.submit/interrupt`; 50 ms DB hot-poll in `wait_settled` | While any of these runs, **every** request, WebSocket, and SSE stream on the server stalls. This is the single biggest cause of "everything gets slow at once". The repo even ships a `loop_watchdog.py` built to log these stalls. |
| **Refetch-the-world instead of patch-the-delta** | Full uncapped session-list re-page on every `done`/`session_*` WS event; full git reload + `git fetch --prune` per session switch; full transcript re-download from seq 0 per switch; post-mutation git refetch storms that discard inline responses | Network/CPU bursts exactly when the user acts, request-pool contention (HTTP/1.1: 6 conns/host) delaying the very data the user is waiting for. |
| **Reset-to-empty state machines** | Chat surface resets to `messages: []` on switch; git stores reset to `EMPTY_STATE` per session-keyed cache miss; loading surface renders the same hero as an empty chat | The reported empty → loading → expanded flicker; PR/branch buttons walking through intermediate states. |
| **Failure indistinguishable from "no data"** | `gh` failures return `[]` and get cached as "no PR"; `githubAuthenticated` transiently false hides the whole git UI | PR/state UI flipping between contradictory states. |
| **Many writers, no single source of truth** | `isStreaming` has 5 writers across 2 stores; `/live` REST snapshot races the SSE feed with last-write-wins; `sessionProfileId` reset everywhere, written nowhere | UI elements that depend on other components' state update late, disagree, or snap backwards. |
| **Everything in one bundle, everything mounted** | 1.3 MB min / 365 KB gz single chunk; zero dynamic imports; 404 KB docs markdown + 14 Shiki grammars in the entry chunk; all transcript messages mounted, no virtualization | Slow first load and heavy "click-around" cost on weak machines; Build/Notes/Docs feel slow because they remount + refetch inside a giant graph. |
| **Idle churn** | ~53 DB roundtrips/min at idle from empty-read pollers; ~54 HTTP req/min from an idle tab (git 5 s + teams 2 s); up to 8 idle Node SDK subprocesses (1–2 GB RSS) parked ~30 min; notes full-content DB scan every 15 s | High baseline footprint on weak machines even when nothing is happening. |

**Verdict on rebuilds:** nothing needs a ground-up rewrite. The local-git read core
(pygit2 + thread pool + coalescer), the SSE event-log design (dense per-session seq,
batched backfill, cursor resume), and turn persistence batching are well engineered.
What needs **redesign at the edges** is: (a) the GitHub/`gh` path and the frontend's
"seven independent git resources" model → one repo-keyed snapshot pushed over the
existing WS; (b) the sidebar refresh model → row patches instead of full reloads;
(c) the session-switch path → transcript cache + `from_seq` resume instead of
replay-from-zero.

---

## 2. Symptom → root cause map

| Reported symptom | Root causes (finding refs below) |
|---|---|
| "Sessions take time to load, flip empty → loading → expanded" | S1 (1 s catch-up fallback fires before first SSE byte → renders "cleared" hero, then rebuilds message-by-message), S3 (loading surface renders the empty-chat hero), S2 (no transcript cache; full replay from seq 0), S5 (boot waterfall), B1 (multi-MB unpaginated payloads w/ base64 screenshots on the legacy path) |
| "PR and other states show up late / flip between states" | G2 (`gh` failure cached as "no PR" → Merge PR ↔ Create PR flip), G3 (PR/CI never polled after load; CI plumbing entirely dead code), G4 (session-keyed git cache resets to empty; header derives from 4 unsynchronized fetches), G10c (transient auth flip hides git UI) |
| "Sidepanel working / late" | W1 (uncapped full session-list re-page per `done` event), R8 (PR-refresh dedupe key computed but never compared → GitHub call burst per sessions emission), W7 (stale pages clobber WS patches) |
| "UI doesn't feel fluid clicking around; Build/Notes slow" | R5–R7 (one 1.3 MB chunk, no lazy loading, docs+Shiki in entry), R14 (feature panels remount + refetch every visit), R4 (no virtualization → full transcript mount per switch) |
| "General sluggishness" | Loop-blocking set (G1, B2, W6, F2), R1–R3 (every streaming frame re-renders 17 components + full sidebar re-sort), W2 (slow client stalls the turn pump for everyone), B4 (write amplification during streaming) |
| "UI elements don't stay consistent with app state" | S6 (5 writers of `isStreaming`, 2 stores disagree), W7/S-race (`/live` snapshot last-write-wins over fresher SSE data), S7 (`sessionProfileId` never written → history attribution follows live dropdown), S12 (rename clobbered by in-flight list loads) |
| "Needs low resource footprint" | F1 (8 idle Node subprocesses ≈ 1–2 GB), F3/F4 (idle tab = 54 req/min incl. full worktree scans), F5–F9 (constant empty-read DB hum, notes full-content scan, orphan reconciler GROUP-BY every 5 min) |

---

## 3. Findings by slice

Severity: **C** critical · **H** high · **M** medium · **L** low.
Only load-bearing findings are listed here; each was verified by tracing the code.

### 3.1 Session loading & chat state machine (S)

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| S1 | H | 1 s catch-up fallback is idle-based and armed **before** the SSE connection produces its first byte; on a slow backfill it folds an empty reducer, clears `isLoadingSession`, renders the **"Conversation cleared"** hero for a session that has messages, then paints each history frame individually (per-frame repaint + auto-scroll chase + 450 ms reveal animation). This *is* the reported flicker cycle. | `feed-controller.ts:96,105-121,355,398`; `chat-empty-state.ts:46-52`; `ChatView.svelte:212-223` | Don't declare hydration complete with zero reduced events; re-enter batch mode if frames arrive after a premature flip; have the backend emit an immediate `hydrating` control frame so "slow" ≠ "stalled". |
| S2 | H | No transcript cache. Every switch sets `messages: []` and re-downloads + replays the **entire** event log from `from_seq=0` (the `fromSeq` option exists but has no caller). Reducer replay is O(N²) (`appendMessage` copies the array and spreads `messageOriginSeq` per event). A↔B switching pays full cost each direction. | `session-switcher.ts:437`; `feed-controller.ts:311`; `sse-feed.ts:177,321`; `reducer.ts:392-398`; `session-cache.ts:14-37` | LRU of `{reducerState, lastSeq}` per session; fold cached state instantly, resume feed at `from_seq=lastSeq` (same contract `revive()` already relies on). Make `messageOriginSeq` a Map. |
| S3 | H | The 'loading' surface renders MessageArea's **"Start a Conversation"** empty hero — its branch keys purely on `messages.length === 0` with no loading gate. Every switch, even fast ones, reads as empty → content. | `MessageArea.svelte:1366-1400` | Gate on `isLoadingSession \|\| isRestoringSession` → skeleton; with S2's cache the window usually shows real content. |
| S5 | M | Boot waterfall: two **serialized** preference fetches, then the SSE feed waits for the WS handshake even though it is cookie-authed fetch and needs no WS. 3+ serial round trips before history starts. | `session-init.ts:107,128,181`; `active-session.ts:457-471`; `+page.svelte:196-204` | Parallelize the preference reads; start the feed the moment the persisted sessionId is known. |
| S6 | M | `isStreaming` has five writers (feed fold, WS `start`, WS `error`, message-sender ×5 sites, switcher reset) and the sidebar reads a *different* store (`streamingSessions`) fed by different events → turn-start stutter and sidebar/chat disagreement. | `ws-message-handlers.ts:610-616,738`; `feed-controller.ts:209-228`; `message-sender.ts:360-481` | Feed reducer becomes the only writer for the active session; derive the active sidebar dot from the same source. |
| S7 | M | `sessionProfileId` is reset in three places and **written nowhere** → historical message attribution falls back to the currently selected profile; changing the dropdown rewrites history's author names. | `active-session.ts:128,271`; `session-switcher.ts:473,650`; `MessageArea.svelte:229,236-244` | Populate from the session row during switch (row already in hand), or delete the field and use the row. |
| S10 | L | Stale ephemeral title ("New Chat") flashes until `/live` corrects it on deep links / notification opens. | `session-switcher.ts:423-427,592-600` | Prefer appData row title; treat cached "New Chat" as absent. |
| S11 | L | `scrollToBottomUntilStable` runs a 50 ms forced-layout interval up to 3 s, **twice** per switch (once on an empty column). | `MessageArea.svelte:177-220,1008-1012,1278-1281` | Skip while loading; use ResizeObserver-driven settle. |
| S12 | L | `renameSession` optimistic patch lacks `invalidateSessionLoads()` (unlike `setSessionActive`) → in-flight full list loads snap the title back. | `app-data.svelte.ts:490-507` vs `:525-554` | Mirror the `setSessionActive` guard. |

Verified-sound (no action): stale-response guards on switch generation / feed
generations / paginated loads; WS reconnect revives (not restarts) the feed;
WS↔SSE user-message reconcile; durable interaction settling.

### 3.2 Rendering & fluidity (R)

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| R1 | H | Monolithic `activeSession` store notifies **17 subscribing components** on every rAF-coalesced streaming fold (~60/s) — Sidebar, AppTopBar, MainPanel etc. re-run per token frame though they only need the session id. | `active-session.ts:330-346`; `feed-controller.ts:243-265` | Split hot transcript state from cold metadata / fine-grained derived stores; coalesce visible folds to ~50 ms while streaming (250 ms hidden-tab path already exists). |
| R2 | H | Sidebar re-filters + re-sorts the **whole** session list per streaming frame because `rows` reads `$activeSession`; the comparator does a `Date` parse per comparison → thousands of parses per frame at 60 fps for the life of a turn. | `Sidebar.svelte:140-148,260-277`; `session-card-data.ts:71-76` | Depend on `$streamingSessions` only; precompute numeric timestamps. |
| R3 | H | O(N²)-ish per-frame transcript work: new `messages` identity per fold defeats the `displayItems` memo (full rebuild per frame), and per-row `userPromptOccurrence` / `hasLinkedSubagent` scans re-run for every row per fold. Explains "streaming gets choppier as the session grows". | `MessageArea.svelte:316-326,348-384,1517,1578` | Precompute both lookups inside `buildDisplayItems` (it already walks the array); carry on `DisplayItem`. |
| R4 | H | No virtualization: all messages mount as components with full markdown + DOMPurify + Shiki on switch; `content-visibility: auto` skips only paint, not mount. A 500-message session pays hundreds of parse passes before first paint, while a 50 ms scroll-poll forces layout reads. | `MessageArea.svelte:1404-1433,1687-1700` | Windowed mounting: trailing ~50 items, prepend on scroll-up (keyed each + stable ids make this tractable). |
| R5 | H | One route chunk: **~1.3 MB min / 365 KB gz**, zero dynamic imports. MainPanel eagerly imports every feature surface (Settings, Notes, Build, Docs, Stats, Browser, Diff…); vite config raises the warning limit instead of splitting. | `MainPanel.svelte:40-65`; `BuildPage.svelte:23-26`; `vite.config.js:8-16` | Lazy-load each `feature-panel` branch and BuildPage sub-tab via dynamic import + skeleton. |
| R6 | H | Shiki sync core + **14 grammars + 2 themes** statically in the entry chunk, compiled eagerly on first highlight; not even vendor-chunked for caching. | `utils/shiki.ts:20-39`; `vite.config.js:22-30` | Dynamic-import highlighter on first code block (plain text until ready); vendor-chunk it. |
| R7 | H | **404 KB of docs markdown** eagerly glob-imported (`eager: true, ?raw`) into the main bundle; registry built at module init for users who never open Docs. | `lib/docs/index.ts:36-41,80` | Non-eager glob + lazy DocsCard. |
| R8 | H (bug) | Sidebar open-PR effect computes a dedupe `key` and **never compares it** — the effect re-runs on every sessions-store emission (new array identity), firing `getPullRequests(…, 100)` for **every project** each time. With W1's reload storm this is a GitHub API burst per turn completion. | `Sidebar.svelte:193-197,175-191` | Compare `key` to the previous value; debounce a few seconds. |
| R10 | M | Typewriter rAF loop: per-frame reveal → markdown tail parse + sanitize + `{@html}` DOM replace, 60/s on top of feed folds — pure cosmetics doubling streaming cost on weak CPUs. | `typewriter.ts:111-129`; `AssistantMessage.svelte:101-232` | Chunked reveal at ~30 ms; disable on `prefers-reduced-motion` / low-core machines. |
| R12 | M | `updateCurrentTurnIndex` reads O(turns) `getBoundingClientRect` per scroll frame / resize / settle (can force sizing of `content-visibility` rows). | `MessageArea.svelte:404-419,781,1027` | IntersectionObserver on turn anchors. |
| R13 | M | Heavy glass over the scrolling transcript: composer `backdrop-filter: blur(22px) saturate(160%)`, 12 px blurs on rails, sidebar `mix-blend-mode: overlay` grain, infinite pulse animations while streaming — classic scroll-jank on integrated GPUs. | `ChatInput.svelte:949-950`; `TurnNavigator.svelte:149`; `MessageArea.svelte:1728`; `app.css:536-546` | Opaque fallback under a low-end/reduced-transparency signal; blur ≤ 8 px. |
| R14 | M | Build/Notes "slow to load" = unmount + refetch per visit: MainPanel `{#if}`-unmounts panels; BuildPage refires 4 loads per mount; NotesCard **double-fetches** on mount (onMount load + the search effect's initial 300 ms-delayed load), spinner both times. | `BuildPage.svelte:92-97`; `NotesCard.svelte:162-183` | Keep-alive last panel or cache store data with staleness; skip the filter effect's first run. |
| R15 | L | Every SessionCard attaches `<svelte:window>` click/keydown (60+ handlers per document click); 1 s ticker date-parses per card. | `SessionCard.svelte:239,62` | One delegated Sidebar listener; cache timestamps. |

Verified-sound: displayItems identity memo, incremental markdown with tail-only
sanitize, Shiki LRU, one-batch backfill fold, persistent TipTap instance across
switches (editor re-mount is **not** a switch cost), keyed each correctness.

### 3.3 Git / GitHub / PR pipeline (G)

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| G1 | **C** | Blocking `gh`/git subprocesses run **directly on the event loop**: `gh auth status` (10 s timeout) on every PR/actions endpoint; `gh pr list/create/merge`, `list_runs` with **60 s** default timeouts; `is_authenticated()` inline on every `/git/pr-state` cache miss (~15 s cadence per checkout); `/auth/status`'s `gh auth status` inline. While any runs, the whole server serves nothing — the app-wide stall. | `app/api/github.py:108-114,234-476`; `github_service.py:70,102-110`; `git_operations.py:785`; `app/api/auth.py:415` | Wrap all `github_service.*` + auth checks in `asyncio.to_thread`/the git executor (pattern already applied correctly in `git_operations.py:567-784`); cache `gh auth status` for minutes. |
| G2 | **C** | `list_pulls` swallows every failure → `[]` → **failure ≡ "no PR"**, cached 15 s and stored. The `_find_open_pr_failed` sentinel and "don't cache failures" logic are dead code. Header flips Merge PR #N → Create PR → back. | `github_service.py:311-338`; `git_operations.py:195-212,788-808`; `schemas/git.py:143-147` | Let failures raise; add `lookup_failed` to `PrStateResponse`; frontend keeps previous `open_pr` on failure; stale-while-revalidate. |
| G3 | H | PR/CI state is **never polled** after load — only explicit local actions refresh it; remote merges/reviews/checks invisible until a switch. The entire CI/workflow-runs plumbing exists end-to-end but has **zero call sites** (dead), so `githubRepoInfo` is always null and `defaultBranch` falls back to `'main'` (wrong for `master` repos — "Ship It" can show on the default branch). | `MainPanel.svelte:295-305`; `github.svelte.ts:221-240` (no callers); `GitHeaderActions.svelte:106`; `/git/status` already returns `default_branch` (`git.py:200`) | Slow PR/CI poll (30–60 s, only while header visible) or webhooks; use `/git/status`'s `default_branch`; delete the dead github-store paths. |
| G4 | H | Switch flicker: git cache is keyed by `sessionId` (LRU 10), so sessions sharing one checkout never share state — fresh session always flashes `EMPTY_STATE` then fills from **4 unsynchronized fetch cadences** (status / branches+diff / changed-files / pr-state); header action derives from all four → walks intermediate states. | `git.svelte.ts:102,216-220,438-467`; `GitHeaderActions.svelte:124-134` | Key by resolved checkout path; never reset to empty (stale flag); one atomic snapshot (see §4 target design). |
| G5 | H | `git fetch --prune` (network, up to 60 s, one of only 4 GitPool workers) fires on **every** uncached session switch with no TTL — rapid switching queues fetches that starve the status polls. | `git.svelte.ts:381-411`; `git_service.py:243-267`; `git_helpers.py:33-53` | Server-side per-repo fetch TTL (minutes) or `ls-remote` head compare (`check_remote_head` exists); separate executor from read polls. |
| G6 | M | Mutation endpoints compute fresh status+diff inline "for instant frontend update" — the store **ignores it** and re-issues 3–5 refresh requests. Double work + a stale-display window per commit/push/stage. | `git.py:972-979,1100-1104,1175,1249-1251`; `git.svelte.ts:834-838,873-877,934-938` | Apply inline payloads; drop the refetches. |
| G7 | M | No-op suppression never works: TS type says status fields are numbers, wire sends arrays; `_shallowEqual` compares fresh array refs → store update + subscriber cascade **every 2 s poll** during streaming. Same for nested `open_pr`. | `api/git.ts:13-27,218-220`; `git.svelte.ts:125-135,500,805` | Canonical flat shape (counts) at the API layer or deep-compare; fix the TS type. |
| G8 | M (bug) | Idle diff-stats throttle broken: gate compares against the 5 s poll interval instead of the 30 s idle threshold → two full-tree diffs every 5 s forever on idle tabs. One-line fix. | `MainPanel.svelte:300` | Compare against `GIT_IDLE_THRESHOLD_MS`. |
| G9 | M | Sync DB reads (`get_worktree`/`get_session`/cwd resolve, branch repo lookups) on the event loop in every poll-path git endpoint — a network round trip per request on Postgres × 2 s cadence. | `git_helpers.py:153-186`; `git.py:159,419,433-436,785,825` | Cache session→cwd (stable per session) or thread the reads. |
| G10 | L | (a) `core.fsmonitor` configured where pygit2 can't use it (full worktree stat scan remains); (b) CLI branch listing spawns up to 2 subprocesses per mismatched branch; (c) transient `gh auth status` failure flips `githubAuthenticated` false → **entire git UI hidden for up to 2 min**; local git shouldn't require GitHub auth at all. | `git_native.py:62-84`; `git_branch_ops.py:209-228`; `MainPanel.svelte:238-256`; `auth.py:563-567` | Decouple local git UI from GitHub auth; sticky auth state with grace period. |

### 3.4 Realtime plumbing (W)

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| W1 | H | Sidebar refetch storm: every `done` / `session_updated` / `user_session_*` WS event triggers an **uncapped full re-page of all sessions** (100/page, sequential), committing a fresh array **per page** with no equality check — ⌈N/100⌉ HTTP round trips + full sidebar re-derives per completion event, no debounce. | `ws-message-handlers.ts:243,362-364,398-399,420,575,747`; `app-data.svelte.ts:116-176`; `store-utils.ts:107-110` | Row-patch via existing `mergeSessionPatch` for `done`/`session_updated`; debounce full reloads 500–1000 ms; cap initial load with load-more. |
| W2 | H | Turn pump **awaits serial per-device WS sends** (timeout 10 s each). One slow/half-open client (locked phone) stalls SDK event consumption, the recorder, and SSE delivery to every device — repeated 10 s hiccups per dead device. | `session_runtime.py:923-927,288-318`; `sync_engine.py:24,335-351,612-615,800-804` | Per-connection bounded queue + writer task; broadcast = non-blocking enqueue; evict at 2–3 s. |
| W3 | M | Full assistant `text`/`thinking_block` (and unbounded tool inputs) are broadcast to every device on the WS leg and **dropped unhandled** by the frontend (no dispatch case) — O(devices × content) serialize/send/parse for zero UI value; same content already rides SSE. | `query_engine.py:1684`; `session_runtime.py:331-346,923`; `ws-message-handlers.ts:531-1104` | Add them to the `_WS_TRIMMED_RENDER_FRAMES` set; cap tool-input size. |
| W4 | M | SSE live tail: gateway dispatches only `max_seq`; **each client** then runs `get_max_seq` + `get_events` + full re-serialization per append batch → ~14·C DB queries/s during streaming against a **10-connection PG pool** shared with all REST (documented PoolError history). | `event_gateway.py:94-106`; `events.py:288-303`; `session_events.py:236,339-348`; `base.py:104,186-190` | Carry rows through the gateway dispatch: one DB read + one serialization per batch, fan out text. |
| W5 | M | `streaming_status.notify` is an **undamped global** gateway listener: an uncached `runs`+`turns` busy-check per append batch (~7/s per streaming session), which also invalidates the TTL cache other busy-guards rely on. `turn_claim` damps identical ticks; this doesn't. | `streaming_status.py:241-296,320,488` | Per-session 1 s debounce; skip recheck when already-busy and the tick is an ordinary append. |
| W6 | M | Event-loop-blocking sync work in WS handlers: full JSONL transcript parse in `handle_load_session`; sync DB in `attach_cost_metadata`, `has_active_run`, `get_worktree`; `turn_manager.submit` (3 DB writes) and `interrupt` called sync on **every send/stop**. | `chat_ws_handler.py:1466,1501,1506,1528,1248,1323,1946`; `turn_manager.py:180-218` | `run_sync` wrap (the file already does this correctly elsewhere, e.g. `:1015`). |
| W7 | M | `/live` snapshot vs SSE feed is last-write-wins with no sequence gate: a slow `/live` overwrites fresher `contextUsed`/token counters (they jump backwards) and can regress `title`. Same class: stale list pages clobber WS row patches outside the one `setSessionActive` guard. | `session-switcher.ts:544-605`; `app-data.svelte.ts:537-548` | Version-stamp the fold (ignore `/live` usage once feed applied any `turn.usage`); generalize `invalidateSessionLoads()` to all row patches. |
| W8 | L | Reconnect storms: unconditional pending-permissions/questions fetch per reconnect **and** per switch — thundering herd on server restart, compounding W1. Reconnect design otherwise sound (cursor resume, single-feed invariant). | `active-session.ts:452-453`; `session-switcher.ts:539-541` | Fetch once per connection epoch; jitter. |
| W9 | L | Eager f-string debug logging per event per device on broadcast hot paths. | `sync_engine.py:321,337,340` | Lazy `%`-style args. |
| W10 | L/M | Store notification per rAF fold with no derived-value equality short-circuit — every derived subscriber fires unconditionally per fold. | `store-utils.ts:92-135` | `Object.is` short-circuit in `createDerived.subscribe`. |

Verified-sound: 150 ms/2 KB backend delta coalescing; one-paint backfill;
compacted-prefix elision; dense monotonic seq with gap-free cursor resume;
serialize-once in `broadcast_event`; gateway LISTEN resilience.

### 3.5 Backend persistence & session data (B)

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| B1 | H | Session open returns the **entire transcript, unpaginated**, with untruncated `toolInput` (a Write call carries the whole file) and **inline base64 screenshot data URIs** — multi-MB JSON per open and per legacy `/live` poll. No message-level pagination exists anywhere. | `sessions.py:698-908,979-1253,806,1117`; `pg_session_store.py:340-360`; `jsonl_parser.py:218,1308-1417` | Keyset-paginate messages; serve screenshots by artifact-store hash URL (already content-addressed by turn_recorder); cap tool inputs like outputs. |
| B2 | H | Sync psycopg2 **on the event loop** in the hottest handlers: session-list tag/fork enrichment (6–10+ queries incl. `get_api_user` per user in a loop, `get_project` **per distinct project (N+1)**, a full `list_projects_v2` scan and a `turns⋈runs` DISTINCT scan) on every sidebar poll; cost/profile metadata attachment on session open — each cache miss doing a **full `SELECT * FROM session_messages`**; `POST /conversation` and `/projects` listing (incl. **writes** in `_backfill_link_groups`). | `sessions.py:501-686,774,882-885,1145-1194`; `sessions_helpers.py:564-596,668-673`; `cost_utils.py:128,245`; `query.py:277,287`; `projects.py:61-66,152-166` | One `run_sync` around the enrichment pipeline; batch `get_project`/`get_api_user` into `IN (…)` queries. |
| B3 | H | `usage_log` has **no index on `session_id`** → sequential scan on every session open (cache miss) and on delete. | `schema_postgres.py:1337`; `analytics.py:199-205`; `db/sessions.py:1260` | `CREATE INDEX idx_usage_log_session ON usage_log(session_id)`. |
| B4 | H | Streaming write amplification: every ~100 ms SDK flush runs INSERT batch + summary `SELECT…FOR UPDATE` + full summary-JSONB **rewrite** (4–5 statements + commit, ~10×/s, one dead tuple per append), **in parallel with** TurnRecorder's 150 ms `session_events` commit stream. Two per-chunk commit streams per session dominate write load on weak machines. | `pg_session_store.py:129-190`; `turn_recorder.py:42-43`; `session_events.py:55-139` | Fold the summary on a 2 s timer or at turn end (it's derivable; staleness check tolerates lag); consider `synchronous_commit=off` for the mirror (transcript is reconstructible from local JSONL). |
| B5 | M | Parse caches hold up to **50 full transcripts as JSON strings each** (unbounded entry size incl. screenshots — potentially hundreds of MB) and a cache *hit* still `json.loads` + re-transforms the whole transcript per request. | `jsonl_parser.py:39-52,1926` | Cache the transformed immutable message list; byte-bound the cache; exclude screenshots (B1). |
| B6 | M | Transcript persisted up to **3×** (`session_store_entries`, `session_events`, legacy `session_messages`); first SSE open duplicates the legacy transcript into the events log; store/events tables grow forever (maintenance never prunes them). | `history_backfill.py:59-124`; `events.py:230`; `database.py:2113-2168` | Prune/compact `session_events` deltas for idle sessions (compaction boundary machinery exists); skip events-log backfill where the client renders from REST anyway. |
| B7 | M | Hidden-session filter adds a correlated `NOT EXISTS(… turns …)` probe per row to every session list **and its COUNT** — N index probes per sidebar refresh, guarding only pre-fix rows. | `db/sessions.py:266-270,342-357,433-437` | Materialize `sessions.is_worker`; backfill once; drop the subquery. |
| B8 | M | Per-call overheads: `cluster_share_sessions` setting fetched with a dedicated connection checkout + SELECT + commit on **every** session list/search; write helpers re-SELECT after UPDATE. | `db/sessions.py:27,630,803,1007,1118`; `db/settings.py:119-125` | TTL-memoize the setting; `UPDATE … RETURNING`. |
| B9 | M | Empty-store sessions trigger a **full Claude-projects directory scan** (stat per project dir) on every open/poll — no negative memo, unlike `history_backfill`'s `_empty_memo`. | `pg_session_store.py:455-471`; `jsonl_parser.py:305-312` | TTL memo for "no local JSONL". |
| B10 | L | `upsert_team_member_message` N+1 (list ids, re-SELECT each). | `db/sessions.py:1606-1633` | Single query with metadata column. |
| B11 | L | Boot runs the full ~1,900-line DDL + orphan-adoption `SELECT DISTINCT instance_id` scans (unindexed) + notes/attachment backfills synchronously before accepting traffic. | `startup.py:141-160,213-227` | Gate DDL behind a stored `schema_version`. |

Verified-sound: keyset pagination on session lists; hot list indexes; batched
tag/fork enrichment (2 queries); one-transaction turn-end persistence; GIN FTS
indexes; throttled off-loop disk sync; `/live include_messages=false` fast path.

### 3.6 Idle/background footprint (F)

Backend at idle ≈ **53 DB roundtrips/min sustained**; an idle browser tab with a
project session ≈ **54 HTTP requests/min**. Ranked:

| # | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| F1 | H | Up to **8 idle Node SDK subprocesses** (pool default 8, cap 16) parked for 30 min idle timeout + 5-min eviction tick ≈ 35 min worst case; 100–300+ MB RSS each → **1–2 GB idle RAM** after light use. Dominant idle-RAM item. | `config.py:308,315`; `session_pool.py:548`; `resource_monitor.py:517-527` | Default pool 3–4; tiered idle timeout (keep 1–2 hot, evict rest at 5–10 min); evict on last-device-disconnect. |
| F2 | H | `turn_manager.wait_settled`/`wait_session_idle` **hot-poll the DB every 50 ms with sync psycopg2 on the event loop** — one waiting automation = 20–60 blocking queries/s for up to 300 s, stalling every coroutine. | `turn_manager.py:271-311`; `db/turns.py:527-576` | `to_thread` + backoff, or wait on the existing NOTIFY gateway. |
| F3 | H | Idle git polling: status **and** diff-stats every 5 s per visible tab (G8's broken gate), with a 2 s cache TTL guaranteeing a miss → full pygit2 worktree stat scan every poll, forever. | `MainPanel.svelte:274-306`; `git_cache.py:19` | Idle cadence 30–60 s; align TTL; push invalidations over WS (backend already calls `invalidate_prefix` on mutations). |
| F4 | H | TeamStatusBar polls `GET /teams/{sessionId}` **every 2 s unconditionally** for any open session (auth + DB access check per hit), duplicating data the SSE feed already carries as `team.member_*` events; no visibility gate. | `TeamStatusBar.svelte:31-40`; `ChatView.svelte:252-254` | Derive from the feed; poll only while a team is running. |
| F5 | M | Constant empty-read DB hum (~46 q/min) from 4 cross-install pollers (turn_claim 5 s ×2, git_ops 5 s, interaction sweeper 10 s, streaming reap 15 s) — pure overhead on single-install deployments; NOTIFY already provides the latency path. | `turn_claim.py:53,187-197`; `git_ops_runner.py:37,262-280`; `interaction_sweeper.py:27`; `streaming_status.py:42` | One multiplexed 15–30 s sweep; stretch to 60 s when the cluster registry shows no peers. |
| F6 | M | Orphan reconciler every 5 min runs a **GROUP-BY over all of `session_store_entries`** per NULL-link session; ordinary never-used sessions retrigger it for 14 days. | `orphan_reconciler.py:60-144`; `resource_monitor.py:675` | Memoize checked ids per process; hourly cadence (startup pass covers backlog). |
| F7 | M | notes_sync fetches **every note with full content** from the DB every 15 s (fast path skips file reads, not the content read). | `notes_sync.py:210-250`; `config.py:254` | Metadata-only query + content fetch for changed rows; or 60 s+. |
| F8 | M | = W5 (undamped streaming_status recheck per append batch). | | |
| F9 | L | SSE keepalive runs a `get_max_seq` DB read every 15 s **per connected feed** (hidden tabs included); dual WS keepalive mechanisms on the same socket. | `events.py:41,285-307`; `ws-connection.ts:16,109`; `ws_utils.py:73-74` | Skip the DB read when the notify queue was silent; collapse keepalives. |
| F10 | L | Plugin auto-update default-ON (6 h git+network burst); its *disabled* path still wakes every 5 min just to re-read settings. Monitor's pool-capacity warning math still assumes the old pool default 3. | `plugin_updates.py:26-27`; `plugin_auto_update.py:46-52`; `resource_monitor.py:299` | Wait on the reschedule event indefinitely; fix the constant. |

Dormant risk: `action_queue`/`proactive_scheduler` 60 s loops are fully built but
never started (`initialize()` has no callers) — zero cost today, two more ticking
loops if ever wired.

---

## 4. Target architecture changes (the "rebuild" scope)

Three bounded redesigns — everything else is targeted fixes.

### 4.1 Git state: one repo-keyed snapshot service (replaces frontend polling fan-out)

Keep `GitService`/pygit2 as the read engine. Add a backend **GitStateService keyed
by resolved checkout path** that produces a single versioned composite snapshot
`{status, diff_stats, changed_files, branches_summary, default_branch, pr, ci}`:

- **Change detection instead of recompute**: mtime checks on `.git/HEAD`, `.git/index`,
  refs + a debounced filesystem watcher on the worktree; recompute only on change or
  explicit invalidation (all mutation endpoints already call `invalidate_status_cache`).
- **Push over the existing WS** (sync_engine reaches all tabs/devices) with a version
  number; frontend keeps one `Map<checkoutPath, snapshot>` store, renders
  stale-while-revalidate, never resets to empty. Slow 30 s HTTP poll as fallback only.
- **GitHub side**: one async worker per repo refreshing PR + CI on 30–60 s cadence
  through `to_thread`, explicit `unknown` state on failure, jittered backoff; merged
  into the same snapshot. Delete the dead `github.svelte.ts` load paths.
- **Delete**: per-load `git fetch` (TTL'd `ls-remote` instead), post-mutation refetch
  storms (use the inline responses already computed), per-request `gh auth status`
  (cache 5 min), session-keyed frontend cache.

This one change resolves G1–G7, G10c, R8's blast radius, and F3 together.

### 4.2 Session switch: cached transcript + resume (replaces replay-from-zero)

- LRU (`~10`) of `{reducerState, lastSeq}` per session in the feed controller;
  switch-in folds cached state synchronously (no empty flash), then resumes SSE at
  `from_seq=lastSeq` — the exact contract `revive()` already uses.
- Loading surface renders a skeleton (never the empty-chat hero); catch-up fallback
  cannot complete hydration with zero reduced events; backend emits an immediate
  `hydrating` frame.
- Backend: paginate the REST message path, screenshots by URL, and (B4) coalesce the
  summary fold — the switch path then moves KBs, not MBs.

Resolves S1–S3, S5, B1 and most of the perceived load latency.

### 4.3 Sidebar/session list: patch, don't reload

- `done`/`session_updated`/`user_session_*` → `mergeSessionPatch` row patches;
  full reload only on genuine list-shape changes, debounced ≥ 1 s, first page only
  (cursor-paging already exists for the rest).
- `isStreaming` single-writer (feed reducer) + sidebar dot derived from the same
  source; sequence-gate the `/live` snapshot apply.

Resolves W1, W7, S4/S6/S12, R2's trigger frequency, R8's trigger frequency.

---

## 5. Remediation roadmap

### Phase 0 — surgical fixes, days, no design change
Highest value-per-line first:

1. **Off-loop everything** (G1, B2, W6, F2): `to_thread`/`run_sync` wraps in
   `github.py`, session enrichment, WS handlers, `wait_settled` backoff. This alone
   removes the app-wide stalls.
2. One-line/small bugs: G8 idle-throttle gate; R8 unused dedupe key; B3 `usage_log`
   index; S12 rename guard; F10 monitor constant; W9 lazy logging.
3. W3 trim `text`/`thinking_block` from WS broadcast; W2 per-connection send queues.
4. F4 TeamStatusBar gate; F1 pool defaults (3–4) + tiered idle eviction.
5. B4 summary-fold coalescing to 2 s.

### Phase 1 — bundle & render, ~1 week
6. R5–R7: lazy-load feature panels, Shiki, docs (target: entry chunk < 400 KB min).
7. R1–R3: store split + 50 ms streaming fold + precomputed display lookups +
   sidebar decoupled from `$activeSession` with numeric timestamps.
8. R14 keep-alive/caching for Build & Notes; R10 typewriter chunking +
   reduced-motion opt-out; R13 low-end glass fallback.

### Phase 2 — the three redesigns, 2–3 weeks
9. §4.2 transcript cache + `from_seq` resume + skeleton loading surface.
10. §4.3 sidebar patch model + single-writer streaming state + snapshot gating.
11. §4.1 GitStateService + WS-pushed snapshots + GitHub worker with explicit
    failure states.

### Phase 3 — persistence & idle hygiene
12. B1 message pagination + screenshot URLs; B5 byte-bounded transformed cache.
13. F5 multiplexed sweep tick; F6 orphan memo; F7 notes metadata-only sync;
    W4 rows-through-gateway; W5/F8 damped recheck; F9 keepalive DB skip.
14. B6 events-log pruning; B7 `is_worker` materialization; B8 setting memo +
    `RETURNING`; R4 transcript virtualization (last: biggest UI change surface).

### Success criteria (measure before/after on a low-end profile: 4-core, throttled CPU)
- Session switch (500-msg session, warm): < 150 ms to content, zero empty-hero flash.
- Cold load to interactive: entry JS < 400 KB min; TTI cut ≥ 50 %.
- Streaming: main-thread work per second < 20 % on 4-core; sidebar exempt from
  per-frame work.
- Idle (1 tab, 1 project open): ≤ 6 HTTP req/min client, ≤ 10 DB q/min server,
  RSS after 30 min idle ≤ 40 % of current.
- PR header: never flips through intermediate states on switch; failure renders as
  stale-with-indicator, not "Create PR".

---

*All findings verified by code inspection at the referenced locations; items listed
as "verified-sound" were explicitly checked and cleared, and are not worth spending
optimization effort on.*
