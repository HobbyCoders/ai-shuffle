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
