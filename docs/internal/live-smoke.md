# Live smoke before a release

The live smoke drives the real workers through the bug classes that unit
tests and CI cannot see: sessions on the other worker, messages racing a
turn end, `/compact`, image edits, subagents across workers. A release tag
needs a passing smoke for the candidate revision.

It runs **on demand before a release only**. Nothing schedules it, and edge
updates do not start it. It spends real provider tokens.

## Where things live

| What | Path |
|---|---|
| Scenarios (single source of truth) | `scripts/live_smoke/scenarios.json` |
| Readable table (generated, do not edit) | `docs/internal/live-smoke-scenarios.md` |
| Runner, recorder, gate | `scripts/live_smoke/` (`python -m scripts.live_smoke`) |
| Release gate | `scripts/ci/ship_release_status.py` (the `smoke` section) |
| Tests | `tests/test_live_smoke.py`, `tests/test_ship_release_status.py` |

## The one command

On the worker, from the repo root, with the Python that has the app's
dependencies (`httpx`, `websockets`):

```bash
python -m scripts.live_smoke run
```

It talks to the worker's own HTTP API on loopback
(`http://127.0.0.1:8000`, `--url` to change). On a native install it uses the
passwordless local access that worker grants its own machine and sends no
credential. The Docker image never treats loopback as the owner (agents'
commands run in the same container), so there the owner signs in
(`POST /api/v1/owner/login` with `"bearer": true`), writes the token to a
private file and passes `--token-file PATH`; the runner sends it as a Bearer
and as the chat socket's `token`, and stores it nowhere. Without it the runner
stops with "the worker refused the request". An agent must not mint or look
for that token itself: running the smoke on Docker is the owner's call. A
worker started by the desktop app only admits its own window on loopback;
record that worker's rows by hand instead (see below). For in-chat live tests,
the live-test skill drives sessions through the agent tools (StartSession,
MessageSession, ArchiveSession) and needs no token.

Useful flags: `--token-cap N`, `--model M` (default: the cheapest-looking
usable model), `--peer NAME` (default: the only other paired worker),
`--rows id,id` (a partial run; its file is ignored by the gate),
`--project NAME`, `--agent ID` (default: the account's default agent, the
one the web UI composer starts new chats with; the runner sends it on every
message, as the UI does). Exit code 0 means pass, 1 fail or incomplete, 2
setup error (worker unreachable, no revision, no agent), 130 interrupted.

Run it under Monitor or in a terminal: a full run takes several minutes.

### Cost and cleanup

- Every session uses the cheapest model and a one-line prompt.
- **Token cap:** 600,000 tokens per run by default (`defaults.token_cap` in
  the scenarios file, or `--token-cap`). Counted tokens are fresh input +
  output + cache writes, summed over every session the run started; cache
  reads are recorded but not counted. Past the cap the run stops the
  current session, marks the current row failed and every remaining row
  "not run: token cap reached", and the run is a fail.
- Every session the run created is archived at the end, also after a
  failure, the cap, Ctrl-C or SIGTERM.

### Offline workers

Rows that need the other worker (`two_worker: true` or a step with
`"on": "peer"`) are written "not run: worker offline: <name>" when the
peer is offline or not paired. No load reaches an offline worker, the run
never waits on it, and a run with such rows is "incomplete", never a pass.
Every wait has a deadline (`within_s`), so a stuck session fails its row
instead of hanging the run.

Those rows block the gate unless the owner excuses them by name with
`--skip-smoke-rows two_worker "<reason>"` (see "The release gate").

## Smoke the candidate without restarting a real worker

The gate needs a result for the candidate revision, but the owner's worker
does not have to run it. Run the same rows against an **isolated preview
instance** of a clean checkout at the candidate commit instead:

1. In a separate checkout (or worktree) of the candidate, clean, at the
   commit you will tag: build the UI and stamp it.

   ```bash
   (cd frontend && npm ci && npm run build)
   python -m app.preview stamp
   ```

2. Start the app from that checkout as an isolated instance: its own data
   folder and managed PostgreSQL (never the running worker's data dir), on a
   port other than 8000/8001, with

   ```bash
   AI_SHUFFLE_PREVIEW=1 SHUFFLE_SOURCE_REVISION="$(git rev-parse HEAD)"
   ```

   in its environment. The instance needs an agent with a working provider
   and, for rows with `needs_project`, the project registered. It must
   answer passwordless loopback requests: start it with `LOCAL_MODE=1` (a
   preview inside the Docker image otherwise refuses loopback like the
   worker itself) and under a supervisor that does not set the desktop's
   listener token, which the runner does not have.
   Never copy a `.env` into the checkout.

3. From that same checkout:

   ```bash
   python -m scripts.live_smoke run --preview --url http://127.0.0.1:<port>
   ```

`--preview` refuses to start unless `GET /api/preview` answers with a stamp
from **this** checkout (the same worktree), at the revision the instance
reports in deploy-status, with no tracked file changed. It checks the
checkout again at the end. The rows then run exactly as on a real worker:
real sessions through the same HTTP and WebSocket API, the real provider,
the same checks, cap and cleanup. The result is written as
`<smoke dir>/<revision>/<label>-preview.json` (never a real worker's file)
with a `preview` block (commit, branch, build hash, `verified_at_end`).

An isolated instance has no paired peer, so its two-worker rows are not run;
excuse them explicitly (see "The release gate") or also run the rows on two
real workers.

## Result file

One file per revision per worker:

```
<smoke dir>/<revision>/<worker>.json
```

The smoke dir is `$AI_SHUFFLE_SMOKE_DIR` when set, otherwise `smoke/` under
the worker's data dir (`/data/smoke` in Docker, `%USERPROFILE%\.ai-shuffle\smoke`
on Windows). Format (`format: 1`):

```json
{
  "format": 1,
  "revision": "<full sha the worker runs>",
  "worker": "ServerWorker",
  "app_version": "…",
  "peer": {"label": "DesktopWorker", "revision": "…", "online": false},
  "run_id": "k3f9qa",
  "partial": false,
  "started_at": "…", "finished_at": "…",
  "model": {"model": "haiku", "why": "cheapest by name ('haiku')"},
  "token_cap": 600000,
  "tokens": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "counted": 0},
  "cap_exceeded": false,
  "outcome": "pass | fail | incomplete | interrupted",
  "rows": [
    {"id": "two-worker-smoke.2", "kind": "script", "check": "Start on other worker",
     "status": "pass | fail | not_run", "reason": null,
     "evidence": {"sessions": {"a": "<session id>"}, "step": 1, "message_index": 1, "message_id": "…"}}
  ],
  "sessions": [{"session_id": "…", "row": "two-worker-smoke.2", "archived": true}]
}
```

Browser and owner rows are written `not_run` with the reason "needs
browser" or "owner step". A `--preview` run adds
`"preview": {"commit", "branch", "build_sha256", "worktree_id", "verified_at_end"}`
and names the file and `worker` `<label>-preview`.

## Adding browser and owner results afterwards

After checking a browser or owner row by hand (or with the browser tools),
add it to the run's file:

```bash
python -m scripts.live_smoke record <revision> --worker ServerWorker \
  --row two-worker-smoke.3 --status pass --evidence "snapshot /tmp/stop.png" --by owner
```

Script rows cannot be recorded; rerun them. A worker whose file was written
elsewhere (the swapped-roles pass on DesktopWorker) is brought in with
`python -m scripts.live_smoke import <file>`.

## The release gate

```bash
python -m scripts.live_smoke check <revision> --worker ServerWorker [--worker DesktopWorker]
python scripts/ci/ship_release_status.py --smoke-worker ServerWorker
```

For each covered worker the gate blocks on: no file for the candidate
revision, a file for another revision, an interrupted or over-cap run, a
failed row, a scenario row missing from the file, a script row not run, or
a browser row not recorded (rows marked `optional` excepted). Owner rows
never block; they are listed as owner to-dos.

A preview result (`<label>-preview`) counts like a worker's file. It also
blocks when its stamp is for another commit or the checkout changed during
the run, and the release notes name it as an isolated preview instance of
the candidate:

```bash
python scripts/ci/ship_release_status.py --smoke-worker ServerWorker-preview \
  --skip-smoke-rows two_worker "isolated preview instance; no second worker"
```

`--skip-smoke-rows ROWS "<reason>"` (repeatable, on `ship_release_status.py`
and `live_smoke check`) excuses rows that were **not run**: `ROWS` is
`two_worker` (every row that needs the other worker) or comma-separated row
ids. A matching row that ran and failed, or is missing from the file, still
blocks; an unknown row id or an empty reason blocks. Each excuse adds a
release-notes line naming the rows, the workers and the reason.

`--skip-smoke "<reason>"` overrides the gate. The report then says
`verdict: skipped`, keeps the blockers it overrode under `would_block`, and
its `release_notes_lines` (`Live smoke skipped (--skip-smoke): <reason>`)
must go into the release notes. The ship-release skill does this.

## Adding a row

Edit `scenarios.json` only. A row:

```json
{
  "id": "two-worker-smoke.16",
  "check": "Short name",
  "kind": "script",
  "how": "what the row does, one line",
  "pass_when": "what passing looks like",
  "past_bug": "the bug it guards against",
  "steps": [
    {"op": "start", "as": "a", "on": "peer", "prompt": "Live test {run}: reply OK, no tools.",
     "expect_reply": "^\\W*OK\\W*$", "expect_owner": "peer"},
    {"op": "message", "session": "a", "prompt": "reply TWO", "expect_reply": "TWO"}
  ]
}
```

- `kind`: `script` (the runner drives it), `browser` or `owner` (no steps;
  recorded by hand). `two_worker: true` marks rows that need the peer;
  `optional: true` keeps a browser row from blocking; `needs_project: true`
  skips a row when the project is not found.
- Ops: `workers`, `start` (`as`, `on: self|peer`, `prompt`, `wait`),
  `message` (`session`, `prompt`, `when: reply_seen`, `wait`), `await`
  (`session`), `compact` (`session`). Each takes `within_s`.
- Checks on `start`, `message` and `await`: `expect_reply` (regex on the
  last assistant text), `expect_tool` (names), `expect_owner: peer`,
  `expect_files`, `user_messages` (exact count), `replies` (regex → exact
  count).
- Prompt placeholders: `{run}`, `{peer}`, `{self}`, `{image}` (a 4x4 PNG the
  runner writes), `{session:<alias>}`.

Then regenerate the table and run the tests:

```bash
python -m scripts.live_smoke render
PYTHONPATH=fluid pytest tests/test_live_smoke.py -q
```

A PR whose description says it was "not tried on real workers" adds a row
here (see `CONTRIBUTING.md`).
