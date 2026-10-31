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
| `state` | `starting`, `pairing` (two workers), `ready`, `restarting`, `app-exited` or `stopping` |
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

## Two real workers on one Linux machine

```bash
python -m scripts.devloop up --workers 2 --host 0.0.0.0 --browser-host 172.17.0.2
python -m scripts.devloop status --wait 180
python -m scripts.devloop down
```

For the owner to open both from another device on the LAN, pin the ports
inside a published range and use the host's LAN address (see "Opening a
branch copy from a phone" below): `--port 18080 --vite-port 18081` pins
worker A, and worker B takes the next pair, `18082`/`18083`.

Run `up` under Monitor, just like the one-worker mode. **Dev worker A** has
its own PostgreSQL, data directory, home and workspace, with the normal
**Dev sandbox** / **Dev other** projects and chats. **Dev worker B** has a
second private PostgreSQL, data directory, home and workspace, and an **empty
seed**: no local projects or chats. Enrollment then brings A's shared records
across through the real network sync; it does not copy A's project directories
into B's workspace. This is the starting state for testing “set up this
project on another worker.” Two-worker mode requires the default seed (not
`--seed long` or `--seed none`).

The launcher creates a network on A, mints a single-use `network:join` grant,
and asks B to enroll through the app's normal API. Possession of that code is
the app's approval path. Both enrollment and peer sync use **protected
loopback listeners**, not the local-owner or preview listeners. Each worker
explicitly advertises its own protected URL, and enrollment sends its real
instance id. The launcher's setup requests to each local-owner listener use
the existing per-launch desktop-listener credential; no Origin header is
fabricated and no CSRF or peer authentication checks are disabled. No live
worker, account, identity key, credential file or database is used.

Wait for the `PAIRED` line / `state: ready`: readiness requires **both**
`GET /api/v1/network/status` and `GET /api/v1/cluster/installs` on **each**
worker to list the other as active, approved and **online**, in the same fully
bootstrapped network. Then open each printed access link once. Both UIs are
reachable at `--browser-host`; on A, **Settings → Devices** shows A as ready
and B as online. The preview cookie remains read-only for ownership,
credentials and network settings. The Devices page's cloud-device
confirmations request may return 403 because it requires a worker-authenticated
owner, not a preview cookie; that does not indicate an offline peer.

Both projects' `file://` bare remotes live in the pair's shared scratch
`remotes/`, **outside both workspaces**, readable by either worker. B can
clone those URLs without GitHub or real Git credentials: setting a project
up on another worker clones the project's own recorded remote, which may be a
local path outside the workspace (typed clone URLs in the Git panel still may
not). On a dev instance the preview cookie may add projects and set them up,
cancel or remove them on the other worker (Projects → Workers); other
owner-only changes stay refused. The two apps run as
the same OS user: this exercises real pairing, encrypted sync, cross-worker
API calls and separate project installations, not host isolation or Unix
permissions between different users.

The status JSON adds:

| Field | Meaning in `--workers 2` mode |
| --- | --- |
| `worker_count`, `workers` | `2`, and both workers' names, access links, URLs, seeds, directories and app/PostgreSQL/Vite process identities |
| `network` | Network id and each worker's observed approved/online peer, or `null` while starting/restarting |
| `remotes_dir` | Shared bare repositories, outside either workspace |
| Top-level `access_link`, `urls`, `app`, `postgres`, `seed`, `data_dir`, `workspace_dir` | A's existing fields, so journey tools still target A |
| `state` | Adds `pairing`; `ready` means both apps and the initial mutual-online check are ready |

`--port` / `--vite-port` select A's ports; B's ports are allocated separately.
Each app gets its own Vite proxy in dev mode. A Python save restarts **both
apps**, keeps both databases/identities/access cookies, and rechecks mutual
presence before returning to `ready`. A source error can be fixed and saved
without tearing the pair down. The TTL, parent loss, stop signals and `down`
end **both** leases; the SIGKILL guardian also covers both apps, Vites,
PostgreSQL clusters and private socket directories. Either supervisor ending
ends the pair. Do not stop only one app by hand; use `down` for the pair.

This cannot simulate a second physical machine, Windows, distinct OS users,
WAN latency or partitions, account/tunnel registration, real GitHub access or
real AI providers. Those still need the live smoke. `--workers 1` (the default)
keeps the original single-worker behavior.

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

## Performance fixture (`--seed long`)

Use `python -m scripts.devloop up --seed long --no-vite` for repeatable
performance measurements: a 1,480-turn / 104,630-event rich long transcript and
two small switch targets instead of the default projects/calls fixture.
The display-only provider makes no model requests. Status includes session ids,
event/byte counts and seed time. See [perf.md](perf.md) for the production build,
real phone shell, journeys, counters and SQL/byte ratchets.

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
* Voice calls on, funded by **Fake voice (dev, GPT Live)** (OpenAI) or **Fake
  voice (dev, Grok)** (xAI), per `up --voice-engine gpt-live|grok`. Both rows
  carry a dummy key and list no chat models; calls on them reach only the
  fake voice provider (below).

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
  `default`, `long`, `slow`, `tool-bash`, `tool-multi`, `tool-background-stream`, `error-500`,
  `rate-limit-429`, `cut`, `ask-question` and `ask-questions` (below), and
  for voice calls `voice-reply` (a three-paragraph reply) and `voice-wait`
  (an update, a background job and a wait; the job's output tags its wake
  turn `voice-wait-done`). Unknown names reply with the list.
* `[script:tool-background-stream]` starts a real session-owned `Monitor` shell
  (`sleep 120`, no inline wait), then streams a reply at six words per second
  while the task remains in `/live`'s `activeTasks` and the running-tasks list.
  Use a native **Dev agent** chat attached to a project (for example the seeded
  **Cleared chat**) so shell tools are available. In Ask mode, approve the
  `run_python` and `Monitor` requests, then switch away while both the task and
  reply are active. Stop or archive the session afterwards to reap the shell;
  otherwise it exits after two minutes. The usual tag options can stretch the
  stream further.
* `[script:ask-question]` asks one question in the question tray (`AskUser`,
  two options) and ends its turn; `[script:ask-questions]` asks two in one
  request. The app queues the answer as a user message (`Answer to your
  question:` …), which the fake AI treats as the next round of the scenario
  that asked, not a new prompt: the reply quotes what it received, `User
  skipped this question.` for a skip. Native (Dev agent) chats only: the fake
  `claude` CLI does not play `AskUserQuestion`. `question-tray.journey.mjs`
  plays both.
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

A step has `text` (with `{summary}`, `{round}`, `{scenarios}` and `{answer}`,
the latest question-tray answer, filled in),
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

## Voice calls: the fake voice provider

`up` also starts a fake realtime voice provider
(`scripts/devloop/fake_voice.py`) and names it to the app in
`SHUFFLE_DEV_FAKE_VOICE_URL`. A call placed from the UI then runs the app's
real call code end to end: the call button and dock, the browser microphone
over the call's audio socket, the server's transport (`VoiceStream` for GPT
Live, `GrokVoiceStream` for Grok), the bridge, and the hand-off to the chat
agent on the fake AI. Only the provider's address changes
(`app/core/voice_endpoints.py`), so it is free and deterministic.

It plays a voice model that does exactly what the app asks: it greets when
told to, says every note the app sends for speech in the chat agent's own
words (once, with the text it was asked to tell), and says nothing for quiet
context. The caller's side comes from a scenario in
`scripts/devloop/voice_scenarios/`, chosen by `[voice:NAME]` in a chat message
before the call (options ride in the tag: `[voice:relay reply_delay_ms=800]`):

| Scenario | What happens |
| --- | --- |
| `default` | No caller script. Type into the call to hand the agent a request. |
| `relay` | The caller asks; the voice hands off with a short ack; the agent's three-paragraph reply (`[script:voice-reply]`) must be said once, in full. |
| `wait-update` | The agent starts a background job, writes an update and waits (`[script:voice-wait]`); the update is said, then the result. |
| `busy` | Placed while the chat answers: that reply is said when it lands, then a new request is relayed. |
| `inactivity` | The provider ends its session for inactivity (once per chat line). |
| `renew` | The provider ends its session as expired while the agent works; the app reconnects. |
| `error` | The provider reports an error mid-call (once per chat line). |
| `drop` | The provider connection is cut without a close. |

Steps (one key each): `wait_audio_ms` (wait for that much speech-level
microphone audio, so a script never runs ahead of a real microphone),
`wait_ms`, `wait_speech` (wait until N notes were said), `caller` (the
caller's words, as the provider's transcript events), `handoff` (with `ack`,
the voice's own words), `say`, `error`, `close` (a reason: `inactive`,
`expired`), `drop`. Options: `word_ms`, `start_delay_ms`, `reply_delay_ms`
(slow provider), `step_timeout_ms`. A session that resumes a call (its history
carries the app's renewal note) plays `default`, so a reconnect never replays
the script. New scenario files are picked up without a restart.

Everything it saw and said is logged as JSON lines: the status file's
`fake_voice.log` (`speech`, `note`, `handoff`, `caller`, `audio`, sessions),
also `GET <fake_voice.control>log?since=N`. `GET …/scenarios` lists the
scenarios; `GET …/arm?scenario=NAME` makes the next new session without a tag
play NAME (for a client that cannot type a tag first). The fake also answers
the GPT-Live-1.1 access check with 404. It runs alone too:
`python -m scripts.devloop.fake_voice --port N [--host 0.0.0.0] [--log PATH]`.

The fake is used only on a dev instance: the URL must be a bare loopback
`ws://host:port` and the data directory must carry the launcher's
`.branch-preview` marker (as for the seed). On that instance, and only there,
the preview cookie may place calls (`attach`, `audio`, `hangup` on
`/api/v1/voice/sessions/<id>/`); everywhere else calls stay owner-only.
`tests/core/test_voice_endpoints.py` proves a normal config cannot reach it.

The journeys place calls with Chromium's fake media
(`frontend/tests/journeys/lib/voice.mjs`): a looped tone with pauses as the
microphone (constant sound with no words would trip the app's
"provider stopped hearing" check), no permission prompt.

### From a phone or an Android emulator

The app always talks to the fake over loopback; a phone or emulator only
needs the app itself. Start the instance on an address the device reaches and
open the printed access link there:

```sh
python -m scripts.devloop up --host 0.0.0.0 --browser-host <worker-IP> \
    --fake-voice-host 0.0.0.0
```

An Android emulator on the same machine reaches the host as `10.0.2.2`
(`--browser-host 10.0.2.2`); on another machine use that machine's LAN IP.
(The emulator path has not been run yet: the first locked-phone test is
expected to confirm these addresses.)
`--fake-voice-host 0.0.0.0` also opens the fake's log and scenario controls
(`/fake/log`, `/fake/arm`) on the LAN, so a test driving the device from
elsewhere can arm a scenario (the native app cannot type a `[voice:…]` tag
before a call on a fresh chat) and read what the voice was asked to say. The
status file's `fake_voice.control` then names the browser-facing address. A
plain-HTTP LAN page is not a secure context, so a browser there gets no
microphone; the native app is not affected.

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

To stretch CLI startup (not just its first token), launch with
`DEVLOOP_CLAUDE_STARTUP_MS=3000 python -m scripts.devloop up`. The fake waits
before emitting its init/session-id frame; auth and version probes are not
delayed. The default is zero. This composes with scenario `latency_ms` and
with `--no-vite` to exercise the built frontend.

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
| `compact-after-tool` | a big Read, then automatic compaction before the model call that reads its result |
| `/compact` | manual compaction: the app's compaction route, then the boundary and the conversation replayed as fresh frames (#679) |

Every compaction plays a replay fixture from `scripts/devloop/fake_claude/compaction/`
(`<trigger>-<outcome>.json`: `auto` or `manual`; `fluid`, `summarizer` or
`skip`, picked by the app's answer). Each fixture lists the frames, hooks
and transcript entries in the real CLI's order. They were derived from
Claude Code 2.1.312 and real transcripts; see
[claude-code-compaction-protocol.md](claude-code-compaction-protocol.md). A
new CLI that prints compaction differently changes a fixture, not the fake's
code.
| `exit-error` | the process exits 1 with stderr mid-turn; the next message resumes |
| `error-500`, `long`, `slow`, ... | the fake AI's scenarios, played by the CLI |

A step may add `"result": "..."` or `"result_chars": N` to a tool call,
`"compact": true`, or `"exit": {"code": 1, "stderr": "..."}`. An MCP call
without `result` really runs on the bridge. Every run is logged with its argv,
the names of its environment variables (never their values), hooks, tools and
compactions in `fake-claude.jsonl` in the scratch directory. What it cannot
model: a real model's choices; built-in tools (Bash, Read and the rest) do not
run; the Claude Code summarizer is a canned summary (sized like a real one,
keeping the newest rows as the CLI does); and there is no
subscription usage, rate limit or real context accounting. The instance's
own `HOME/.local/bin/claude`, which app startup puts first on PATH and the
app's installer writes to, is also linked to the fake. The app therefore sees
Claude Code as installed, and nothing in the instance can come before the fake.

## Ports and teardown

Every port is picked free at random (or `--port` / `--vite-port`); 8000 and
8001 are always refused. The app's local listener stays on loopback; only
the guarded preview listener and Vite bind to `--host`.

### Opening a branch copy from a phone (worker in a Docker container)

A worker in a bridge-networked container publishes only its own port, so a
phone on the LAN cannot reach a random dev port. Publish a small fixed range
on the container once, for example `-p 18080-18089:18080-18089` in the
container's extra parameters (recreating the container restarts the worker,
so do it only when nothing is running in it). Then pin the instance to that
range and use the host's LAN address:

```sh
python -m scripts.devloop up --host 0.0.0.0 --browser-host <host-LAN-IP> \
    --port 18080 --vite-port 18081
```

Open the printed access link once on the phone (it points at the Vite port
and sets the preview cookie); the rest of the visit stays on that port. A
second instance at the same time takes the next pair (`18082`/`18083`). The
guarded port answers 403 without the cookie, which is expected. This is for
judging a UI change by hand on a real phone; it is plain HTTP, so the page
gets no microphone (see the voice section above).

### Teardown

Ctrl-C, SIGTERM,
SIGHUP (the terminal or the Monitor's pty went away), `down`, the TTL
(`--ttl-seconds`, default 1 h, at most 4 h) and loss of the parent process all
stop the app, PostgreSQL, Vite, the fake AI and the fake voice provider and
delete the data. A stop
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
`*.journey.mjs`; with N shards it splits the journey files by measured
hosted runtime from `scripts/devloop/journey_weights.json`. Unmeasured files
use their `--list` case count times the measured median seconds per case.
`python -m scripts.devloop.ci plan --shards N` prints the JSON split, with
estimated minutes in stderr and `$GITHUB_STEP_SUMMARY`. Refresh the medians
with `python -m scripts.devloop.ci refresh-weights RUN_ID [RUN_ID ...]` using
recent green hosted runs ([measurement details](../ci/pr-checks.md#ui-journeys)).
It runs each file of its shard, all of them even after a failure. `--build` builds
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
  settings; two-worker mode also generates a private desktop-listener token and
  an explicit protected-loopback `CLUSTER_ADVERTISE_URL` for each app).
* Never copy a `.env` into the checkout.
* The preview cookie cannot change auth, credentials, settings or providers,
  so seed what you need instead of clicking it into Settings.
