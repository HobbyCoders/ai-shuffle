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
(`http://127.0.0.1:8000`, `--url` to change) with the same passwordless
local access the worker grants its own machine. It sends no credential and
stores none. A worker started by the desktop app only admits its own window
on loopback, so the runner gets "refused passwordless local access" there;
record that worker's rows by hand instead (see below).

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
browser" or "owner step".

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
