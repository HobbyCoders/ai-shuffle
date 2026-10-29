# Dev loop: a private, live-reloading copy of your branch

One command gives you a running copy of the checkout you are in: its own
throwaway PostgreSQL and data, realistic seed data, a free deterministic fake
AI, Vite hot reload for the UI, and an app restart on every Python change.
Use it to exercise a change in the real app before you push.

```bash
python -m scripts.devloop up                                          # local browser
python -m scripts.devloop up --host 0.0.0.0 --browser-host 172.17.0.2  # remote agent browser
```

Linux only (it reuses the `app.preview_run` lease). Run it under a Monitor
task and never daemonize it. When it is ready it prints one access link: open
it **once** in the browser and stay on that port. It also prints the path of a
status JSON (default `$XDG_RUNTIME_DIR/ai-shuffle-devloop/<checkout hash>.json`,
or `$TMPDIR/ai-shuffle-devloop-<uid>/` without `XDG_RUNTIME_DIR`; never in the
repo) that tools and journey tests read. Its directory must be yours and
private (0700): `up` refuses a directory another user could reach or swap,
and `down`/`status` refuse a status file you do not own, so pass `--status` only
inside a private directory:

| Field | What it is |
| --- | --- |
| `state` | `starting`, `ready`, `restarting`, `app-exited` or `stopping` |
| `pid`, `pid_identity` | The launcher and its start time (a reused pid is not the launcher) |
| `mode` | `dev`, or `ci` for `--ci` |
| `access_link`, `urls.ui` | The link to open; the UI (Vite, or the stamped build) origin |
| `urls.preview_listener` | The app's guarded listener (Vite proxies `/api` to it) |
| `app.pid`, `app.starts`, `app.last_ready_seconds` | The current app process, restart count and its last start time |
| `postgres`, `vite`, `guardian` | Those processes (pid and start time), for the guardian's cleanup |
| `fake_ai.url`, `fake_ai.request_log` | The fake AI and its JSONL log of every model request |
| `data_dir`, `workspace_dir`, `expires_at` | Throwaway data, the seeded projects, the TTL deadline |
| `seed` | What was seeded (ids of the provider, agent, projects, remotes and chats) or the seed error |

`python -m scripts.devloop status` prints it (exit 0 while the launcher
runs); `status --wait SECONDS` waits until the instance is `ready` and exits 1
if it stops, its app exits or the time runs out. `python -m scripts.devloop
down` stops the instance. A status file whose launcher is gone (same pid and
start time no longer running, an unreaped zombie included) is **stale**:
`status` and `down` say so and remove it, and the journey runner refuses a
status whose pid is gone.

UI journeys (`frontend/tests/journeys/*.journey.mjs`) read that status file,
drive the instance in Chromium and fail on any flight-recorder anomaly; see
`frontend/tests/journeys/README.md`.

## Live reload

* **UI**: Vite dev (`frontend/`, HMR). Saving a `.svelte`/`.ts` file updates the
  open page in place.
* **Backend**: saving a `.py` file under `app/` or `fluid/` (not their tests)
  restarts only the app process after a 1.6 s debounce. PostgreSQL, the data
  and the access cookie survive, and the open page reconnects on its own (about
  5-10 s). If the app crashes on start (say, a syntax error), the
  loop logs it and waits for your next save; the access link is printed by
  whichever start first gets ready.
* Saves made while a start that has not yet been ready runs are held until
  it is ready (that start runs the seed, and a restart would cut it off), then
  applied as one restart. A start that crashed (or hangs past 3 minutes) lets
  the next save restart it, but the start that save triggers is held again: if
  the very first start crashed before seeding, the fixed start seeds, and a
  second save during it waits. After the first ready start, saves restart at once.
* `--no-vite` serves the stamped build instead (`(cd frontend && npm run build)
  && python -m app.preview stamp` first).

## What is seeded (`--seed default`; `--seed none` skips it)

* Owner setup done; the agent browser's preview cookie is the owner.
* Model provider **Fake AI (dev)** (custom, OpenAI dialect, dummy key, model
  `fake-ai` discovered through the normal Refresh-models path) and the
  **Dev agent** using it. The Dev agent is the composer's default for the
  owner (the `composer.profile` preference, written by the seed through the
  app's own preference storage, since the preview cookie may not write
  preferences) and each seeded project's default agent, so a new chat runs on
  the fake AI without a click.
* Projects **Dev sandbox** (a real git repo: commits on `main`, branch
  `feature/second`) and **Dev other**, under the instance's workspace. Each
  has an `origin` remote: a local bare repository (`file://`, under the
  scratch directory's `remotes/`, never in the workspace) with every branch
  pushed and tracked, so Fetch, Pull and ahead/behind work offline. The seed
  records each repository the way the Git panel's first poll does, so the
  project's shared row carries the remote's identity from the start.
* The seed's own ids (chats, turns, tool calls) are fixed (UUIDv5 from a
  constant namespace and a counter), so every instance seeds the same ids; the
  branch session's id and the access session's come from the app.
* Chats: a 280-row transcript with tool cards, code blocks and errors; one whose
  last turn ends on an unfinished tool call; a branch session checked out under
  `.fluid/worktrees/feature-second`; a cleared chat; one in Dev other; and one
  with no project.

Seeding runs inside the app at startup through the app's own functions, at
most once per data directory. It refuses to run unless the data directory
carries the launcher's `.branch-preview` marker, so it can never touch a real
install. A seed that failed, or was cut off by a stop, is never re-run on top
of what it already wrote: later starts report it as the `seed` error (`partial
seed from an interrupted start`); run `down` then `up` for a fresh instance.

## The fake AI

It is chosen by the request alone, so it behaves the same after restarts:

* Plain messages get a short streamed echo of what you wrote.
* `[script:NAME]` anywhere in your message plays `scripts/devloop/scenarios/NAME.json`:
  `default`, `long`, `slow`, `tool-bash`, `tool-multi`, `error-500`,
  `rate-limit-429`, `cut`. Unknown names reply with the list.
* Options ride in the tag: `[script:slow tps=2 latency_ms=1500]`.

A scenario is a list of `steps`. The step played is the number of tool rounds
the request already carries after your message, so step 1 can call a tool and
step 2 (whose request contains the tool result) can answer:

```json
{"steps": [
  {"text": "I'll run it.", "tool_calls": [{"name": "Bash", "input": {"command": "echo hi"}}]},
  {"text": "Done."}
]}
```

A step has `text` (with `{summary}`, `{round}` and `{scenarios}` filled in),
optional `tool_calls`, or `error` (`{"status": 429, "message": "...",
"retry_after": "1"}`); `cut_after_chars` drops the stream mid-reply. Top-level
`tps` and `latency_ms` set its pace. When the request offers the tool directly
it is called directly; when the engine runs in code mode (only `run_python`
offered, the native default) the calls go into one Python cell, the way a real
model would do it. New scenario files are picked up without a restart.

Timing: `up --tps N --latency-ms N` stretch every reply; a scenario's own
values and then the tag override them. The fake AI also runs alone:
`python -m scripts.devloop.fake_ai --port N [--scenarios DIR] [--log PATH]`.
It answers `GET /v1/models` and streaming `/v1/chat/completions`,
`/v1/messages` and `/v1/responses`. The native-leg tests use the same wire
code (`scripts/devloop/fake_wire.py`).

## Claude Code chats: the fake `claude` CLI (`--claude-cli fake`, the default)

The Fake AI only drives the native harness. Claude Code chats run the `claude`
CLI instead, so `up` writes a fake one into the instance's scratch directory
(`fake-claude-bin/claude`, which runs `scripts/devloop/fake_claude/cli.py`) and
puts it first on the app's PATH. Only the instance's PATH changes. Any other
PATH directory that holds a `claude` (`~/.local/bin`, an npm prefix) is
replaced by a mirror of its other programs, so a missing shim shows "Claude
Code is not installed" and the real CLI is never reached. The seed then adds
the **Dev (Claude Code)** agent and the **Claude Code CLI (seeded)** chat, but
only if the `claude` the app resolves is the fake. The fake reports a sign-in
for `fake-claude@devloop.invalid`. It holds no credential, opens no network
connection other than the app's own loopback compaction bridge, and refuses
`auth login`, `update` and everything except `--version`, `auth status` and
`-p` runs. `--claude-cli none` leaves PATH alone and seeds no Claude Code agent.

It speaks stream-json like Claude Code 2.1. It calls the hooks the app
registers and obeys a PreToolUse deny. It asks `can_use_tool` unless the mode,
`--allowed-tools` or a read-only tool allows the call, and removes
`--disallowed-tools` from its tool list. It really connects the app's MCP
bridge from `--mcp-config` and keeps a transcript under the instance's
`CLAUDE_CONFIG_DIR`, so `--resume` works and an unknown id exits 1. Scenarios
use the fake AI's format and the same `[script:NAME]` tag. The CLI's own
scenarios in `scripts/devloop/fake_claude/scenarios/` come first, then the
fake AI's:

| Tag | What it exercises |
| --- | --- |
| (none) / `default` | a streamed echo |
| `tool-bash` | one Bash call with a permission prompt (the output is scripted; nothing runs) |
| `tool-mcp` | a real `ListAgents` call through the app's MCP bridge |
| `long-result` | a 60,000-character MCP result, replaced by Claude Code's `<persisted-output>` stub |
| `bulk-read` | a 45,000-character Read result (no prompt), to grow the context quickly |
| `compact` | automatic compaction mid-turn, then the answer |
| `/compact` | manual compaction: the app's compaction route, the boundary, then the conversation replayed as fresh frames (#679) |
| `exit-error` | the process exits 1 with stderr mid-turn; the next message resumes |
| `error-500`, `long`, `slow`, ... | the fake AI's scenarios, played by the CLI |

A step may add `"result": "..."` or `"result_chars": N` to a tool call,
`"compact": true`, or `"exit": {"code": 1, "stderr": "..."}`. An MCP call
without `result` really runs on the bridge. Every run is logged with its argv,
the names of its environment variables (never their values), hooks, tools and
compactions in `fake-claude.jsonl` in the scratch directory. What it cannot
model: a real model's choices; built-in tools (Bash, Read and the rest) do not
run; the Claude Code summarizer is a canned summary; and there is no
subscription usage, rate limit or real context accounting. The instance's
own `HOME/.local/bin/claude`, which app startup puts first on PATH and the
app's installer writes to, is also linked to the fake. The app therefore sees
Claude Code as installed, and nothing in the instance can come before the fake.

## Ports and teardown

Every port is picked free at random (or `--port` / `--vite-port`); 8000 and
8001 are always refused. The app's local listener stays on loopback; only
the guarded preview listener and Vite bind to `--host`. Ctrl-C, SIGTERM,
SIGHUP (the terminal or the Monitor's pty went away), `down`, the TTL
(`--ttl-seconds`, default 1 h, at most 4 h) and loss of the parent process all
stop the app, PostgreSQL, Vite and the fake AI and delete the data. A stop
first rewrites the status as `stopping`, before the slow teardown, so nothing
reads a dying instance as ready.

Stopping the Monitor task (TaskStop, or the stop button) SIGTERMs the task's
process group and about a second later SIGKILLs every process under it,
`up` included, long before the app and PostgreSQL are down; a SIGKILL runs no
cleanup. So `up` first starts a **guardian** (`scripts/devloop/guardian.py`):
a detached process (its own session, reparented away from the task) that
waits for the launcher to be gone, then kills what it recorded (app, Vite and
PostgreSQL, each only while its pid still has the recorded start time),
removes the status file if it still names the dead launcher, and removes the
scratch directory and PostgreSQL's socket directory. It writes one line to
`<status>.guardian.log` when it had to do that; after a normal teardown it
finds nothing and exits.

## CI mode and the journey driver

`python -m scripts.devloop up --ci` (or `DEVLOOP_CI=1`) is the
non-interactive form: loopback only, no Vite (it serves this checkout's
stamped production build, checked before anything starts: `(cd frontend &&
SHUFFLE_SKIP_MOBILE_UI=1 npm run build) && python -m app.preview stamp`), no
file watching or restarts, the default seed and an instant fake AI. Once
ready it prints one line, `DEVLOOP_READY {"status": ..., "ui": ..., "pid":
..., "seconds": ...}`; an instance that cannot be used (the app exits before
ready, no access link, a failed seed) prints `DEVLOOP_FAILED {..., "error":
...}`, tears down and exits 1.

`python -m scripts.devloop.ci journeys [--shard I/N] [--out DIR] [--build]`
is what CI runs: it starts `up --ci` as its own child (so the instance's
parent lease is the driver), waits for the ready line and `status`, runs the
journeys, then `down`. With one shard it runs
`frontend/tests/journeys/run-all.mjs` when there is one, else every
`*.journey.mjs`; with N shards it splits the journey files by case count
(`python -m scripts.devloop.ci plan --shards N` prints the split) and runs
each file of its shard, all of them even after a failure. `--build` builds
and stamps the UI first when the stamp is missing or stale. It writes
`devloop.log`, `logs/<journey>.log` and the per-case timelines (and contact
sheets) under `--out`, prints the failing timelines, appends a summary to
`$GITHUB_STEP_SUMMARY`, and exits 1 if anything failed. Run it locally the
same way (PostgreSQL 16 binaries on `PATH`, Chromium via `CHROMIUM_PATH`).
See [PR checks](../ci/pr-checks.md#ui-journeys-and-visual-regression).

## Safety rules

* Never point it at, or run it beside, the live worker's data, database or
  ports; it never reads them, and it inherits no `DATA_DIR`, `DATABASE_URL`, key
  or DSN (the app's environment is PATH/LANG/LD_LIBRARY_PATH plus its own
  settings; the launcher may add only the dev loop's own `SHUFFLE_DEV_*` settings).
* Never copy a `.env` into the checkout.
* The preview cookie cannot change auth, credentials, settings or providers,
  so seed what you need instead of clicking it into Settings.
