# Comparable performance measurements

Use production UI builds, the same fixture, run counts and machine load before
and after a change. Wall clocks are observations, **not CI thresholds**. Ratchet
queries, bytes, replay counts and nested DOM work instead. Never put an access
URL, cookie, owner device name, address or field session id in a checked-in file
or public report. Raw results belong outside the checkout.

## Setup and long fixture (lab)

Use a checkout-local Python environment with the app requirements, Node 20+,
`npm ci` in `frontend/` and `browser-mcp/`, and Chromium (`PERF_CHROME` overrides
`/usr/bin/chromium`). The build includes the **real phone shell**, not a narrow
desktop viewport:

```sh
(cd frontend && npm run build)
python -m app.preview stamp
python -m scripts.devloop up --no-vite --seed long
```

Run the last command under a managed Monitor. For a remote browser use the
host/browser-host options in [dev-loop.md](dev-loop.md). One owned instance per
agent, unique ports, never the live worker. `--seed long` also works with Vite;
use `--no-vite` for measurements. The normal `default` seed is unchanged.

The opt-in seed writes through the real contract/event-log producer: **1,480
turns, 104,630 events**, plus three-/two-turn switch targets. Every answer has
five fenced Python blocks, five markdown tables and nested lists; four tools
per turn have code/search/JSON/test results. 410 completed subagents and their
progress events are distributed over the whole history, including the tail.
Content/event ids are deterministic (database-assigned timestamps are not).
The display-only provider never calls an API; do not send messages to it.
The ordinary disposable-data/once-only seed guard still applies. Status has
`sessions.{small,large,other}`, `sizes` and `seed_seconds`. Record the actual seed
duration and window DOM count with results; this is a load model, not a claim
that all real chats have identical density. A shared Linux lab validation took
26.186 seconds to seed and mounted about 54k elements in a 120-row window
(desktop 54,113; phone 53,917), close to the Phase-1 rich field windows.

## Chat journeys (lab and read-only field)

Set `PERF_ACCESS_URL` **only in the process environment** to an approved access
link. The rigs never accept an access file, write storage state or log that URL.
A field run is read-only: do not use the original rig's `send` journey on it.
Use `--field` to label field results, supplying existing ids privately on the
command line; output uses cell labels, never those ids or the worker origin.

```sh
node scripts/perf/chat-journeys.mjs --platform both --runs 10 \
  --small "$SMALL" --large "$LARGE" --other "$OTHER" \
  --journeys cold,warm,switch,scroll --out /tmp/chat-perf.json

# Connected remote-owned chats; local small target for the return trip.
node scripts/perf/chat-journeys.mjs --platform both --runs 10 --field \
  --journeys remote --remote "$REMOTE_ONE,$REMOTE_TWO" --small "$SMALL" \
  --out /tmp/remote-perf.json

# Repeated bidirectional virtual-window shifts, not repeated top-of-chat no-ops.
node scripts/perf/chat-journeys.mjs --platform both --runs 1 --journeys scroll \
  --large "$LARGE" --scrolls 20 --scrollMode alternate --out /tmp/scroll-perf.json
```

* **Cold**: fresh Chromium context, no HTTP/IndexedDB chat cache, direct link.
* **Warm**: load the long chat, visit small, dispatch the cached long return in
  the same page. This separates transcript cost from sidebar input handling.
* **Switch**: open small, click unvisited long (and optional other), then cached
  small/long. Desktop sidebar clicks and phone **Open menu** drawer clicks are
  real. `--switches 20` repeats the cached bidirectional path.
* **Older scroll**: scripted wheel + edge scroll through the production virtual
  window handler; ends after the range index changes and two animation frames.
  Default three shifts per run; an exhausted history fails, never a zero sample.
* **Remote**: cold open and cached return for each supplied remote-owned chat.
  Verify actual ownership and a connected peer before calling this a remote
  result; using a local id tests only the runner. A one-worker seed is not a
  network/mirror fixture. The standing paired-worker check is
  `harness-basics.perf-remote-open-rig` in the live-smoke catalog.

Desktop is **1280x800, 1x CPU**; phone is **390x844, touch, `/mobile/`, 4x CPU**.
No artificial network shaping; report topology separately. Each cell has raw
runs and nearest-rank p75/min/max/n (via `stats.mjs`). Failed runs are counted,
not folded into zero latency; any failure makes the process fail. Setup failures
are persisted as failed samples in `<platform>.<journey>.setup` cells (or `setup`
for global authentication/setup), with no timing sample. This keeps failed
attempts visible even when no journey reaches its milestone. Output destinations
are opened without truncation before resources start; a later write failure still
closes the browser and makes the command fail.

The chat milestone matches Phase 1: new non-skeleton rows + enabled visible
composer + 120 ms of unchanged scroll geometry. The quiet observation is not
added to the timestamp. This is **not** a promise that every background request
has finished. `surfaces`, `bottomGap`, mounted rows and composer identity help
spot loading flashes or incorrect positions. Counts include the fixed 200 ms
post-milestone observation:

| Counter | Definition |
| --- | --- |
| `sseBytes` | Raw UTF-8 bytes consumed by live fetch SSE streams; subtract each stream's byte snapshot at interaction start, including pre-existing streams |
| `fromSeqZeroReplays` | New SSE requests with exact `from_seq=0` in the interaction window (not `01` or a resumed tail) |
| `apiRequests` | All browser HTTP requests to `/api/` started in the window, including still-open streams; excludes auth setup and WebSocket frames |
| `longTaskMs` | Sum of >50 ms Long Tasks overlapping the observation window, clipped at both ends |
| `liveDomNodes` | All live **element** nodes in the document at collection, including nested markdown/tool/code content, not only virtual outer rows |

`browser-helpers.mjs` owns the exported `initScript`, `collect`, selectors,
Playwright loader and counter math used by these rigs and the original
`journeys.mjs` (launch/send/dependency-delay probing). No source-string eval or
private scratch imports. The original rig's existing CLI remains compatible.

## Startup and imports (lab only)

Stop your dev instance first (`python -m scripts.devloop down`). Stamp again
after a commit. The startup rig owns one disposable database/preview lease,
excludes schema/seed warmup, then restarts **only its own app**, sequentially:

```sh
PERF_PYTHON=python node scripts/perf/startup-repeat.mjs --runs 10 --warmup 1 \
  --platform both --seed long --session "$LARGE" --out /tmp/startup-perf.json
python -m scripts.perf.profile_imports --runs 10 --out /tmp/import-perf.json
```

Startup includes restart request -> health ready -> rendered/typeable composer
(and long transcript for `--seed long`), with separate process-spawn time and
the same byte/DOM/request counters. It includes prior-process teardown; browser
launch and the initial schema/seed warmup are excluded from the summary.
`--seed none` omits chats for comparison with the original empty-start baseline.
The supervisor bounds failures and tears down its own PostgreSQL on exit.
Startup succeeds only if the supervisor exits normally with code zero; nonzero,
signalled or timed-out shutdowns set `failed`, leave `cleanedUp=false`, retain
`previewExitCode`/`previewExitSignal` and fail the command.
Imports use fresh interpreters and an isolated environment, report `-X importtime`
self/cumulative costs, elapsed p75/min/max and `sys.modules` count/list for
module-count ratchets. Neither rig touches the live app or calls a model.

## SQL and response-byte ratchets in tests

Reuse `tests.performance.metrics`: the existing shim `count_statements` helper
was extracted from `test_hot_path_query_counts.py`, not duplicated. The optional
request-scoped cursor hook works on that shim and the real PostgreSQL wrapper,
including `run_sync`/AnyIO offloads. Do not install it in production.

```python
from fastapi.testclient import TestClient
from tests.performance.metrics import RequestMeasurements, measure_sql

# bare_router_app: the actual router with test auth/DB overrides, no lifespan
measured = RequestMeasurements(bare_router_app)
with measure_sql(), TestClient(measured) as client:
    client.get("/api/v1/sessions/example/live?include_messages=false")  # warm up
    response = client.get("/api/v1/sessions/example/live?include_messages=false")
cost = measured.requests[-1]
assert cost.complete and response.status_code == 200
assert cost.sql_count <= QUERY_BUDGET
assert cost.response_bytes <= BYTE_BUDGET
```

Count each attempted `execute` and each consumed `executemany` parameter set,
including failed statements; preserve cursor iteration/context management.
Bytes are the sum of streamed ASGI body chunks **at the wrapper boundary**, not
headers or necessarily compressed wire bytes. Failed/disconnected streams keep
partial bytes with `complete=False`. Contextvars isolate overlapping requests;
outside/finished-request work does not count. The legacy trace helper counts
DML only. Compare small/large histories after warmup to expose N+1 work, and
report middleware/compression placement with every byte budget.

### Detail message windows (native PostgreSQL)

`tests/db/test_session_message_window_postgres.py` exercises the actual detail
router on the same 104,630-event seed. Its one-message ratchet allows at most
12 statements, two event-read statements and 12 source rows crossing into Python,
at both small and large history sizes. It runs in the existing native PostgreSQL
CI job, not the SQLite shim. Wall-clock samples remain observations, not gates.

For event-backed detail responses, one SQL snapshot counts message boundaries
and assistant-bearing user intervals in the prefix, then reads a complete,
capped suffix starting at a turn barrier before the requested messages. The
existing REST fold renders that suffix, verifies its ordered identities, and
keeps later tool updates. Cost and per-content user-row attribution retain their
whole-transcript offsets without transferring the prefix. The aggregate still
visits relevant prefix events; this bounds transferred rows and round trips,
not the database work of every aggregate independently of history size.

This does **not** enable the default-off canonical TypeScript transcript view.
Local Fluid/store, legacy readers and SDK-ID aliases remain unchanged. Rewinds, approval retry
cards, reminder-bearing user matching, cross-boundary tool reopens, malformed
fold payloads (including note/compaction data and tool fragments/results), and suffixes
over 8,192 relevant source rows fall back to full replay. Long histories without
a usable turn barrier and deep older pages can exceed that cap too. Differential
checks compare complete messages and pagination headers against the full path,
including these fallbacks, streamed deltas, late tool results, answers, compaction,
repeated prompts, profiles and cost attribution.

## Backend heap reclamation

The main app sweeps already-free glibc heap pages off the event loop, at most
once per minute. Cold resumes share that same budget. This is on by default,
not a cache eviction or a garbage-collection pass; live transcripts stay live.
Windows, macOS, musl and libc builds without `mallinfo2` are safe no-ops.
A sweep skips when glibc reports less than 32 MiB free, or when neither RSS nor
free-heap bytes changed by 32 MiB since the previous trim. Free arena address
space can stay high after pages have been returned, so it alone is not a leak.

Each attempted trim logs an INFO `[MemoryReclaim]` line with
`reclaimed_bytes`, before/after RSS, free-heap bytes and `trim_ms`, without
session content. Reclaimed bytes are the observed nonnegative RSS decrease;
concurrent allocations can offset it. The work is off-loop but glibc can still
stall allocating threads: compare its duration and allocating-thread latency
under the same burst before changing the cadence. In a private dev instance,
sample RSS alongside `tracemalloc` and `mallinfo2`: low live traced bytes plus
high free-heap bytes, followed by an RSS fall on trim without a Python-heap
fall, distinguishes allocator retention from live data. Do not enable diagnostic
routes or profile the owner's database to collect a baseline.

Native cold hydration uses a 32-row server cursor so libpq does not hold a
second complete raw transcript beside the decoded tree. It still loads all
nodes; this does not window away branches or change resume semantics.

## Phase-1 baseline (2026-10-29)

These are historical p75 observations, **not thresholds**. Seed shape and phone
shell differ for the remote row as noted; do not present a richer-fixture run
as an app speedup. Shared Linux runner unless stated; no network shaping.

| Journey | Desktop | Phone | Kind / runs |
| --- | ---: | ---: | --- |
| Real long chat open (5–9k messages, 100k+ events) | 30.8 s | 36.8 s | field, 3; phone shell 4x CPU |
| Seeded long chat open (104k events, old simpler content) | 7.7 s | 11.6 s | lab, 10 |
| Cached seeded long return | 0.47 s | 2.2 s | lab, 10 |
| Older-window scroll, real chat | 0.82 s | 3.5 s | field, 9 |
| Short-chat return | 95 ms | 520–620 ms | field, 3 |
| Remote-worker chat open | 0.94 s | 2.1 s | field, 10; old phone-size browser, not `/mobile/` |
| Cold app process to usable | 9.3 s | — | lab, 10; empty-data startup after schema warmup |
| Preference reaches peer | up to 60 s | — | lab component observation |
| Container update machine time | ~353 s | — | field, 1 |
| Desktop update machine time | 272 s (105 s downtime) | — | field, 5; desktop OS worker |

Phase 1 found ~50k element nodes in real 120-row windows versus ~8.6k in the
old synthetic fixture. Use the new rich fixture's measured DOM count alongside
latency so a cheaper synthetic window cannot masquerade as a field win.

## Cross-worker sync (lab)

The measurement-only `scripts/perf/sync_bench.py` starts the real
`python -m scripts.devloop up --workers 2` lease with its fake AI provider,
separate disposable databases and isolated homes. It never attaches to an
existing worker. Linux only (`scripts.devloop up` rejects other platforms). First
install the documented devloop dependencies and build/stamp the desktop UI:

```sh
(cd frontend && npm ci && SHUFFLE_SKIP_MOBILE_UI=1 npm run build)
python -m app.preview stamp
python -m scripts.perf.sync_bench run --runs 10 --long-seed \
  --catchup 1000 10000 --idle-seconds 600 --output /tmp/sync-bench.json
```

The JSON contains raw samples, nearest-rank p50/p75/max/n (same convention as
`stats.mjs`), signed-operation-correlated stages, envelope bytes, seed sizes,
per-scope SQL counts/elapsed time and thread-offload CPU. Failed/censored attempts
have null latency and explicit per-journey `counts` and summary failure counts;
they are never zero-valued successes or silently omitted. A failed attempt is
saved before its error propagates. The bounded plain-project probe records its
censoring and continues the other journeys, but the run still exits non-zero.
The adjacent `<output filename>.lab.log` and `teardown_exit` record cleanup;
non-zero/signal exits and cleanup timeouts fail the command after saving diagnostics.
URL-bearing announcements/readiness lines are dropped before any log write;
other log lines have query tokens redacted. Interruption also shuts down the owned
lease. Internal `lab`/`child` modes are not entrypoints for an installed worker.
No production instrumentation, protocol, timing constant or feature flag is
changed: all measurement hooks exist only in the disposable child process.

Writers are production stores/services; streaming uses normal chat intake
(`ChatWebSocketHandler.handle_query`), the queued native runner, and both
workers' session-event SSE. No synthetic socket is registered. API visibility
is polled every 100 ms; that sampling delay is included, not subtracted. Publication and apply
are timed after their database commits. The exchange boundary is the matching
push dispatch (or pull response receipt), not a guessed whole-round-trip time.
Memory's start is file-save return, an upper bound on its durable write time.
`project_create` measures the actual creation commit to B's catalog visibility,
without seeding missing catalog rows, and retains censored three-second attempts.
The separate `project_resolve` stage uses the same origin-access resolution as
starting a first chat, against an independent pre-upgrade legacy-row fixture.
That fixture has no identity or checkout: after eager creation is fixed,
resolving the already-visible create journey would otherwise measure a read,
not publication. These are intentionally not the same journey. Use
`--projects-only --runs 10` to omit unrelated mutation, streaming, catch-up and
idle phases. Compare before/after with this same fixture; the historical
resolution baseline below used the plain-create row after its observation
window instead.

Two-worker devloop currently rejects `--seed long`, so `--long-seed` loads the
unchanged `seed_long()` fixture **after pairing**, waits for B's durable event
counts to match, then waits 90 seconds before idle sampling. Each idle window
has no API polling; private probe snapshots bracket it. Full scans are assigned
by completion time, including scans that cross a snapshot boundary. CPU is whole app-process
CPU, with separately scoped `asyncio.to_thread` CPU (not exact async-task CPU or
PostgreSQL CPU). SQL counts/times cover default dict cursors, not explicit cursor
factories, PostgreSQL internals or database-server CPU. Catch-up pauses only B's
owned child process with `SIGSTOP`, writes real durable transcript events on A,
waits ten seconds with normal publication enabled, then resumes B and waits for
its durable count; this is not a severed-network or restart experiment.

This is same-host loopback on the shared Unraid container, not WAN or field
hardware. API/SSE visibility is exercised in running disposable apps; browser
rendering, composer/tool dispatch overhead and full hand-off `install_history`
are not measured. Historical Phase-1 “up to 60 s” preferences are not a threshold
or directly comparable to an already-active, hooked mutation on current main.

### Baseline, 2026-11-07

Unraid Linux container, x86-64, kernel `6.18.38-Unraid`, 32 logical CPUs,
base `d1299844b`, fake provider, built desktop shell, no WAN shaping. Mutation
journeys use the default seed with the scanner already active. The idle phase
then adds the rich long seed: **104,630 events / 57,354,952 payload bytes / 1,480
user turns** in the large chat, plus 36 and 24 events in its two companion chats.
The earlier 20 catch-up runs add 110,000 durable events as well. Startup and
fixture catch-up are excluded from idle cost. These are baselines, not gates.

| Running-app API/SSE journey | p50 ms | p75 ms | max ms | n |
| --- | ---: | ---: | ---: | ---: |
| Last-selected-model preference | 210.0 | 210.7 | 315.3 | 10 |
| New chat in peer list | 222.9 | 224.4 | 331.1 | 10 |
| Rename | 222.5 | 224.2 | 225.5 | 10 |
| Settle / remove from active list | 129.9 | 131.9 | 147.0 | 10 |
| Project after first-chat identity resolution | 1196.4 | 1315.0 | 2127.8 | 10 |
| Saved fact recallable on peer | 2052.8 | 2059.5 | 2062.6 | 10 |
| Catch-up: 1,000 events | 1515.2 | 1519.6 | 2031.2 | 10 |
| Catch-up: 10,000 events | 13613.1 | 15102.6 | 24250.8 | 10 |

Plain project creation was **not visible in B's cluster catalog in 10/10
three-second observation windows** (3.00–3.07 s); there is no finite latency
percentile to report for that case. A separate smoke waited 150 s without a
catalog match. Resolving the project for its first chat makes it visible; the
separate row above measures that production resolution, not a fixture upsert.

| Idle cost / worker / minute | p50 | p75 | max | n |
| --- | ---: | ---: | ---: | ---: |
| A app CPU seconds | 0.335 | 0.354 | 0.362 | 10 |
| B app CPU seconds | 0.296 | 0.303 | 0.311 | 10 |
| A all instrumented SQL statements | 598.2 | 608.2 | 617.1 | 10 |
| B all instrumented SQL statements | 576.2 | 590.2 | 599.2 | 10 |
| A scoped sync/peer/scan SQL statements | 181.7 | 189.7 | 199.7 | 10 |
| B scoped sync/peer/scan SQL statements | 173.8 | 181.7 | 191.7 | 10 |
| A all instrumented SQL elapsed seconds | 0.238 | 0.249 | 0.258 | 10 |
| B all instrumented SQL elapsed seconds | 0.207 | 0.223 | 0.239 | 10 |

Exactly ten full `scan_all` passes were observed per worker over ten idle
minutes: **118 statements/pass on A**, **110 on B** (p50/p75/max identical).
A's scan wall time was 39.2 / 47.1 / 56.2 ms (p50/p75/max); B's was
29.6 / 31.1 / 37.5 ms. Scanner thread CPU was 24.4 / 25.3 / 36.1 ms on A and
16.5 / 17.4 / 21.8 ms on B, n=10 each. Most idle SQL is in unclassified
maintenance, not scoped sync; do not call every app statement “sync cost”.
The full scan is already incremental for transcripts, not a 104k-row replay.

A review audit found all ten samples in every published mutation/catch-up cell,
all ten separate native streaming runs (201 matched deltas), and one full scan
in each of the ten idle windows on each worker. The only recorded censored cell
is the plain-project probe described above; published percentiles are unchanged.
The original idle JSON did not retain snapshot boundaries or unselected events,
so an extra scan straddling the outer measurement boundary cannot be ruled out
retroactively. New runs retain window start/end timestamps and count scans by
completion time. No full-lab rerun was needed for these accounting fixes.

### Where latency goes

Independent p75 stages in milliseconds, n=10 each (percentiles of separate
columns do **not** add up to the total's percentile):

| Journey | commit → publish | publish → exchange boundary | exchange → apply entry | apply / commit | apply → API visible |
| --- | ---: | ---: | ---: | ---: | ---: |
| Preference | 111.34 | 72.02 | 3.91 | 10.41 | 37.52 |
| New chat | 114.92 | 80.08 | 4.26 | 14.44 | 27.45 |
| Rename | 115.61 | 63.12 | 3.90 | 10.93 | 46.05 |
| Settle | 0.02 | 55.55 | 3.83 | 10.18 | 63.41 |
| Project resolution | 1086.05 | 94.76 | 4.57 | 12.77 | 47.04 |
| Memory | 1951.09 | 56.27 | 4.43 | 11.66 | 54.04 |

- Preferences and chat metadata are already post-commit hooked through
  `app/network/startup.py:208–237`; their roughly 110–116 ms publication stage
  includes the 100 ms wake debounce in `app/network/transport.py:55,1570`.
  Settlement records its envelope in the explicit shared-membership writer
  (`app/network/session_active.py:53–87`), so that stage is effectively zero.
- A new plain project is only a legacy row (`app/db/projects.py:157–175`).
  The peer selector requires a logical identity and active base checkout
  (`app/api/cluster.py:170–202`). Those are lazily seeded by project resolution
  (`app/core/project_resolver.py:118–169`). That resolution is not in the
  registered project resource mutations, so publication still waits for a full
  scan rather than a targeted wake. This is a producer/catalog gap, not WAN lag.
- Fact files are detected/published on full scans only
  (`app/network/engine.py:1215–1224`,
  `app/network/memory_records.py:57–121`). The measured warm publication wait
  was 1.95 s at p75; the scanner's idle ceiling is 60 s
  (`app/network/transport.py:60,1511,1537–1542`). The idle ceiling is a code
  bound, **not** a measured cold-memory percentile. Imported memory ids are
  origin-qualified (`fluid/fluid/codemode/facts.py:1477–1487`); recall checks
  unique fact text, not source-local `mN` aliases.
- Catch-up's 1.52 s / 15.10 s p75 is **resume → durable event-count convergence**,
  sampled every 500 ms. It combines remaining publication, exchange and apply;
  this run does not attribute their individual shares. Relevant bounded work:
  `app/network/catalog.py:192–198,304–336` (event publication budgets),
  `app/network/transport.py:695–710` (pull loop), and
  `app/network/engine.py:2182–2279` (batch decode/verify/apply). Do not claim
  signature checking or SQL alone caused that total without another profile.
- The periodic full scan is not the dominant observed idle cost. It performs
  collection fingerprint comparison (`app/network/engine.py:1065–1148`) and
  incremental catalog/event probes (`app/network/catalog.py:179–199`), with
  memory/plugin file checks on full passes. Removing reconciliation would trade
  a small measured saving for correctness risk.

### Native streaming baseline

The native chat-intake probe ran in a separate fresh paired lab, default seed,
ten replies using `[script:default tps=12]` (no real provider calls). The original
legacy background API probe only recorded the final text and is **excluded**
from streaming results. The final harness rejects fewer than two matched delta
boundaries instead of silently calling a final-message delay “steady lag”.

| Running-app SSE metric | p50 ms | p75 ms | max ms | n |
| --- | ---: | ---: | ---: | ---: |
| Send intake → first text visible on B | 1338.6 | 1615.1 | 2449.7 | 10 |
| B first text behind A first text | 214.3 | 370.1 | 683.9 | 10 |
| B delta boundary behind the same seq on A | 237.9 | 333.2 | 760.1 | 201 |

There were 201 matched delta boundaries across ten replies (18–23 per reply;
includes their first chunks). Lag percentiles pool those correlated chunks;
**n=201 is not 201 independent replies**. Full first-text latency includes turn
queueing, native startup, fake-provider generation and persistence, not just
sync. Chunk replication lag is the cleaner sync metric. Recorder coalescing
(`app/core/turn_recorder.py:15–20,52`) and the scanner's 100 ms wake debounce
(`app/network/transport.py:1570`) are concrete batching points; this run does
not separately time those streaming stages or prove either explains all lag.
To rerun just this producing path in another disposable lab:

```sh
python -m scripts.perf.sync_bench run --runs 10 --stream-only \
  --output /tmp/sync-stream.json
```

### Project creation fix, 2026-11-07

Same-host paired Linux lab, default seed, production creation and peer catalog
API, 100 ms visibility polling, ten requested and ten recorded attempts per
journey on each side. Before: `a21aebffb`; after: `d46fba315` (backend change).
Both use `--projects-only` and the independent pre-upgrade resolution fixture
above. No provider calls or browser rendering are measured.

| Journey | Before p50 / p75 / max (ms) | After p50 / p75 / max (ms) |
| --- | ---: | ---: |
| Plain creation → peer catalog | not visible in 10/10 three-second windows | 264.4 / 385.7 / 1130.1 |
| Legacy first-chat resolution → peer catalog | 1204.5 / 1212.5 / 1548.7 | 372.5 / 375.4 / 495.1 |
| Legacy resolution commit → publication | 1018.4 / 1026.6 / 1336.1 | 123.6 / 126.3 / 204.4 |

All ten creations were visible after the fix (nine under a second); the first
run's 1.13 s maximum includes 924 ms before publication. All twenty operations
after the fix published in targeted passes. The earlier legacy resolver's
roughly one-second publication wait was its missing resource wake, not a
transport/protocol delay. Successful labs both exited zero with both apps and
PostgreSQL down; their process lists were also checked for lease leftovers.
An initial before-run listener collision was a setup failure, not a sample,
and its lease also shut down. Timing is observational, not a CI threshold.

### Ranked follow-up candidates (baseline estimates; project item now implemented)

Gains below are **estimates**, not demonstrated optimizations. Preserve full
reconciliation and mixed-version compatibility; measure again before ratcheting.

| Rank / candidate | Affected journey; possible gain | Risk / rough size / flag |
| --- | --- | --- |
| 1. Publish and wake on committed fact-file changes | Memory: remove most of the observed 1.95 s p75 publication wait; avoid the code's possible 60 s idle wait | Medium: file-transaction completion, owner routing, imports and echo suppression; small/medium (2–4 files). Local scheduling only, no wire change; retain periodic scan, no new flag required for a narrow post-commit hook. |
| 2. Complete project catalog identity at the actual create boundary, and register its post-commit wake | Plain creation currently has no finite result; resolved projects could save about 1.1 s p75 publication wait | Medium: origin/relink/deletion rules, base-checkout identity; medium (3–5 files). Additive existing-schema rows are compatible with older peers; no new flag needed, preserve lazy fallback. |
| 3. Profile and batch catch-up work that remains on the hot path | 10k replay is 15.1 s p75 / 24.3 s max; a 20–40% reduction would be 3–6 s, but attribution is not established | Medium/high: ordering, holes, pending/retry, signature verification and partial commits; medium/large. No wire change if transaction/storage batching only; a new wire format would need capability negotiation and a flag. Do not remove per-envelope verification. |
| 4. Reduce redundant metadata/token wake batching, not the periodic safety scan | Hooked metadata has ~111–116 ms p75 commit→publish; at most ~100 ms available from wake debounce. Native delta lag is 333 ms p75, so a selective wake path may help too | Medium: burst amplification, extra signing/SQL during streams; small/medium. Mixed-version-safe local scheduling; use the existing performance rollback mechanism where applicable, or a measured flag for adaptive timing. |
| 5. Only then consider cheaper idle reconciliation / broader maintenance | Full scan's maximum possible saving is only ~118/110 statements and ~25/17 ms thread CPU per minute at p75, far below all app idle work | Medium correctness risk for a small observed benefit; medium size. Prefer batched fingerprint reads/invalidation coverage, never disable reconciliation; no wire flag needed. Profile the unclassified maintenance separately before calling it sync. |

Gaps: no browser-rendered timing, cold-after-60s mutation distribution, physical
worker/WAN/restart test, end-to-end project-creation UI, hand-off import timing,
PostgreSQL-server CPU, or separately attributed streaming/catch-up stages.
Per-cursor timers/counters and per-op JSON/event-recording overhead are included;
there is no uninstrumented A/B calibration of that overhead. API/SSE paths ran against producing code, not only seeded consumer fixtures.
Every completed lab reported `teardown_exit: 0`; the live installed app was not
used. Only `tests/scripts/test_sync_bench.py` is run locally for this change.
