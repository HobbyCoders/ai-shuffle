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
| `state` | `starting`, `ready`, `restarting` or `app-exited` |
| `access_link`, `urls.ui` | The link to open; the UI (Vite) origin |
| `urls.preview_listener` | The app's guarded listener (Vite proxies `/api` to it) |
| `app.pid`, `app.starts`, `app.last_ready_seconds` | The current app process, restart count and its last start time |
| `fake_ai.url`, `fake_ai.request_log` | The fake AI and its JSONL log of every model request |
| `data_dir`, `workspace_dir`, `expires_at` | Throwaway data, the seeded projects, the TTL deadline |
| `seed` | What was seeded (ids of the provider, agent, projects and chats) or the seed error |

`python -m scripts.devloop status` prints it; `python -m scripts.devloop down`
stops the instance.

## Live reload

* **UI**: Vite dev (`frontend/`, HMR). Saving a `.svelte`/`.ts` file updates the
  open page in place.
* **Backend**: saving a `.py` file under `app/` or `fluid/` (not their tests)
  restarts only the app process after a 1.6 s debounce. PostgreSQL, the data
  and the access cookie survive, and the open page reconnects on its own (about
  5-10 s). If the app crashes on start (say, a syntax error), the
  loop logs it and waits for your next save; the access link is printed by
  whichever start first gets ready.
* Saves made during the first start are held until it is ready (that start
  runs the seed, and a restart would cut it off), then applied as one restart.
* `--no-vite` serves the stamped build instead (`(cd frontend && npm run build)
  && python -m app.preview stamp` first).

## What is seeded (`--seed default`; `--seed none` skips it)

* Owner setup done; the agent browser's preview cookie is the owner.
* Model provider **Fake AI (dev)** (custom, OpenAI dialect, dummy key, model
  `fake-ai` discovered through the normal Refresh-models path) and the
  **Dev agent** using it.
* Projects **Dev sandbox** (a real git repo: commits on `main`, branch
  `feature/second`) and **Dev other**, under the instance's workspace.
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

## Ports and teardown

Every port is picked free at random (or `--port` / `--vite-port`); 8000 and
8001 are always refused. The app's local listener stays on loopback; only
the guarded preview listener and Vite bind to `--host`. Ctrl-C, SIGTERM,
`down`, the TTL (`--ttl-seconds`, default 1 h, at most 4 h) and loss of the
parent process all stop the app, PostgreSQL, Vite and the fake AI and delete
the data.

## Safety rules

* Never point it at, or run it beside, the live worker's data, database or
  ports; it never reads them, and it inherits no `DATA_DIR`, `DATABASE_URL`, key
  or DSN (the app's environment is PATH/LANG/LD_LIBRARY_PATH plus its own
  settings; the launcher may add only the dev loop's own `SHUFFLE_DEV_*` settings).
* Never copy a `.env` into the checkout.
* The preview cookie cannot change auth, credentials, settings or providers,
  so seed what you need instead of clicking it into Settings.
