# In-app terminals: backend contract (phase 1)

Issue #1945, phase 1. This is the wire contract between the worker and any
client that shows a terminal (the web UI, desktop, Android). Code:
`app/core/terminals.py` (service) and `app/api/terminals.py` (routes, socket,
authorization). Tests: `tests/core/test_terminals.py`,
`tests/api/test_terminals_api.py`.

Terminals support local Unix PTYs and Windows ConPTY, online enrolled peer
workers, and explicitly selected private-access SSH machine grants. Peer
input/output uses the authenticated control transport, with a target-bound
opaque lease checked back against the initiating owner's credential on every
operation. A lost relay expires and kills its peer shell within 60 seconds.
The source holds the original credential only in memory, never in the wire
payload, UI response, or an agent-facing tool.

SSH targets require an active, writable machine grant for this chat (including
grants approved from saved connections). They reuse the private-access SSH
client and its pinned host-key validation; there is no new credential store.
SSH opens the approved account's login shell/home, with no supplied command,
folder, environment or forwarding. Expiry, revocation or ownership change ends
the connection. Remote terminals require a new explicit target selection after
exit rather than the local Restart action. Multi-line remote paste is refused:
remote output alone cannot prove that a line editor will hold it unexecuted.

The Files-open and read-only Monitor APIs are documented below.

## Capability

| Where | Field | Values |
| --- | --- | --- |
| Runner capabilities (`app/core/runner_capabilities.py`) | `terminal` | `2` = owner-checked peer relay, `1` = local-only legacy, `0` = none |
| `GET /api/v1/terminals/targets` | `supported` | `true` / `false` |

`terminal` is `0` on a worker with no usable PTY/shell. `/targets` lists only
online peers advertising version 2 or later, plus approved SSH grants; legacy
peers remain local-only. `supported` is true when any target is usable.
Creating a local terminal on an unsupported worker returns `501 not_supported`
with the message "Terminals are not supported on this worker yet." Clients
should hide or disable the terminal button when the flag is off and show
`unsupported_reason`.

## Who may use terminals

Every route and the WebSocket admit exactly:

- the owner's password session on the local listener: the `session` cookie,
  or the same token as `Authorization: Bearer` or `?token=`;
- a paired device whose account owner is the install administrator
  (`account_owner_admin`), the same rule `require_admin` applies;
- the desktop app's own loopback requests carrying its per-launch listener
  token (`X-Shuffle-Desktop-Listener`).

Everything else is refused (`403`, or WebSocket close `4001`):

- passwordless local access (plain loopback, trusted LAN), which
  `require_admin` alone would admit. An agent's shell reaches the API over
  loopback, so that path must never open a shell;
- an agent-preview cookie (`ais_preview`), even when an owner credential
  rides along;
- API keys, non-owner paired devices, peer-worker device tokens and share
  links;
- owner sessions on the remote (protected) listener.

Authority is held for the **life of the socket**, not just the handshake:
every 15 seconds an open socket re-runs the same check with the same
credential. Signing out, an expired owner session, or a paired device that
lost owner admin closes the socket with `4001` ("Signed out"); a check that
cannot reach a verdict (account hub down) fails closed with `1013`. Input
that arrives once authority is lost is dropped, not written to the shell.

No tool, hook, automation or agent-facing API imports the terminal service; a
test (`test_no_model_facing_path_reaches_terminals`) parses every module in
`app/` and `fluid/`, resolves every import spelling (relative, aliased,
`from pkg import terminals`) and every string naming the module or its routes,
and fails if anything outside the wiring reaches it. A second test checks that
every terminal route registered in the real app depends on
`require_terminal_owner`.

What this does **not** protect against: the shell runs as the app's own OS
user, and so do agents. Anything that user can read on disk (the data
directory, credential stores) is readable from a terminal and from an
agent's Bash tool alike; the owner session itself is only stored as a hash.

### Origin

- HTTP: a state-changing request carrying the owner **cookie** must be
  same-origin (`owner_auth.same_origin`), as for every owner route.
- WebSocket: when the credential is the ambient `session` cookie (or the
  desktop loopback path), the `Origin` header must match the host. A missing
  or foreign Origin closes with `4001`. An explicit `?token=` is a
  credential no other site can attach, so it is not Origin-checked (the same
  rule as `app/api/ws_auth.py`). A wrong explicit token never falls back to
  the cookie.
- uvicorn logs a WebSocket path with its query string when it refuses a
  socket before accept; the app's uvicorn log filter (`app/main.py`) redacts
  `token=`, `key=`, `sig=` and similar query values, so a `?token=` never
  reaches a log line.

## Terminal object

```json
{
  "id": "term_3f9c0a1b2c4d5e6f",
  "target": {"kind": "worker", "worker_id": "dev_example_worker"},
  "shell": "bash",
  "cwd": "/workspace/ai-shuffle/.fluid/worktrees/task-1234",
  "folder_source": "worktree",
  "session_id": "8d2e6c1a-...",
  "state": "live",
  "exit_code": null,
  "created_at": "2026-10-06T09:12:44.120Z",
  "exited_at": null,
  "busy": false,
  "cols": 120,
  "rows": 32,
  "attached": 1
}
```

| Field | Meaning |
| --- | --- |
| `id` | `term_` + 16 hex. Unique on this worker for its lifetime. |
| `target` | `{"kind": "worker", "worker_id": <local or peer worker>}` or `{"kind": "ssh", "grant_id": <active machine handle>}`. |
| `shell` | `bash` or `sh`. |
| `cwd` | The folder the shell started in (not its current directory). |
| `folder_source` | `requested`, `worktree`, `project`, `workspace` or `home`. |
| `session_id` | The chat it was opened for, or `null`. |
| `state` | See States. |
| `exit_code` | Set when `state` is `exited`. A shell killed by signal N reports `128+N`. |
| `busy` | A command (not the shell) holds the foreground. Updated about once a second. |
| `attached` | Number of open WebSocket attachments. |

### States

| State | Meaning |
| --- | --- |
| `starting` | The shell process started but has not written anything yet. |
| `live` | The shell has written output (usually its prompt), or has run for 2 s. |
| `exited` | The shell ended; `exit_code` and `exited_at` are set. The terminal stays listed, with its scrollback, until it is closed or restarted. |

A closed terminal is gone: it is no longer listed and its id returns
`404 not_found`.

## Folders

Without `cwd`, a new terminal opens in:

1. the chat's worktree on this worker (`folder_source: "worktree"`), else
2. the chat's project folder (`project`), else
3. the workspace folder (`workspace`), else
4. the home folder (`home`).

A requested `cwd` must be an absolute path to an existing directory within
Files' workspace root. Missing paths and files return `400 bad_folder`;
paths outside the root return `400` with the Files boundary error. The Files
validator resolves symlinks before checking confinement, so traversal and
symlinks cannot select a folder outside that root. There is no silent fallback.
The resolved name is not used as spawn authority: create and restart reopen it
without following links and hold directory handles through process creation.
On POSIX a fresh exec trampoline checks the held folder's inode ancestry and
uses `fchdir` before the isolation runner or shell; it never passes the mutable
folder pathname as the subprocess cwd. On Windows every directory component
is pinned without delete sharing and reparse points are refused through ConPTY
creation. A substitution detected before spawn returns `400 bad_folder`; a
folder moved outside the held root during process creation makes the child
exit without launching the shell. Restart retains this Files-root policy.

`POST /api/v1/terminals/from-files` accepts `{"path": "relative/folder"}`
(empty path means the Files root), resolves it with the same Files validator,
and returns `201 {"terminal": <Terminal object>}`. It is owner-only, just like
opening a terminal with an absolute `cwd`.
A chat that runs on another worker is refused with `409 chat_on_other_worker`.

A terminal created without `session_id` belongs to no chat and opens in the
workspace folder, else home. The Terminal page (sidebar More, the search
palette, and Settings > Providers > Claude) uses this so the owner can sign in
to a CLI before any chat exists. The frontend keys that scope as
`WORKER_TERMINALS` and keeps only terminals with `session_id === null`, since a
list without `session_id` returns every terminal on the worker. Auth, the
preview-cookie refusal and the environment scrub are the same as for chat
terminals.

## Read-only Monitor watching

All watch routes use the same terminal owner checks, including refusing agents,
passwordless local requests and preview cookies. They expose only retained local
Monitor/background-shell buffers for the requested session, not subagent reports.

- `GET /api/v1/terminals/monitor?session_id=...` returns `{"tasks": [...]}`.
  Each task has `id`, `session_id`, `subject`, `status`, `started_at`, `exit_code`.
- `GET /api/v1/terminals/monitor/{task_id}?session_id=...` returns one output
  snapshot. Unknown tasks and tasks from a different session return `404`.
- `WS /api/v1/ws/terminals/monitor/{task_id}?session_id=...` accepts the same
  optional `token` credential as interactive sockets. It sends a snapshot on
  attachment and when retained output or status changes (250 ms refresh).

Each JSON frame is `{"type":"output","readonly":true,"task":{...},
"lines":[...],"truncated":false,"finished":false}`. **Replace** the displayed
lines on every frame; do not append them. The producer retains its existing
bounded line buffer; snapshots additionally retain at most 256 Ki characters
of its newest output (`truncated` indicates this additional cap). Output is
plain text from Monitor's existing stdout reader, not a PTY byte stream. There
is no input, resize, paste or stop operation. Any incoming WebSocket data closes
with `1008`; disconnecting never stops the command. Authority is rechecked for
the socket's lifetime. Auth refusal closes with `4001`, missing/wrong-session
tasks with `4004`. After stdout drains, `finished:true` is sent and the socket
closes normally (`1000`). Finished tasks remain readable while their in-memory
Monitor buffer is retained; an app restart loses those buffers.

## Limits

| Limit | Value | When exceeded |
| --- | --- | --- |
| Terminals per worker (soft cap, counts exited ones too) | 8 | `409 limit_reached`: "8 terminals are open on <worker>. Close one to open another." Restart never hits the cap. |
| Scrollback per terminal | 5,000 lines or 2 MB, whichever is smaller | Oldest output dropped at a line boundary. |
| Output queued for one attachment | 1 MB | Socket closed `4408`; reattach to resume from the replay. |
| Output frame size | 64 KiB | Larger output is split into several frames. |
| Client message size | 64 KiB | Close `4400`. |
| Client messages | 200 burst, 100/s sustained | Close `4429`. |
| Input bytes | 256 KiB burst, 128 KiB/s sustained | Close `4429`. |
| Paste | 256 Ki characters | `413 paste_too_large`. |
| `cols` / `rows` | 10–500 / 2–200 (default 80×24) | Clamped. |

## HTTP routes

All under `/api/v1/terminals`. Errors from the terminal service have the
shape `{"detail": "<message>", "code": "<code>"}`. Authorization errors are
`401`/`403` with `{"detail": "<message>"}` only. Request-body validation
errors are FastAPI's usual `422`.

### `GET /api/v1/terminals[?session_id=<chat>]`

```json
{
  "terminals": [ {"id": "term_…", "…": "…"} ],
  "open": 3,
  "running": 2,
  "limit": 8,
  "worker": {"id": "dev_example…", "name": "ServerWorker", "platform": "linux", "container": true}
}
```

`terminals` is filtered by chat when `session_id` is given; `open` and
`running` always count the whole worker.

### `GET /api/v1/terminals/targets[?session_id=<chat>]`

What a "new terminal" picker needs.

```json
{
  "worker": {"id": "dev_example…", "name": "ServerWorker", "platform": "linux", "container": true},
  "supported": true,
  "unsupported_reason": null,
  "targets": [{"kind": "worker", "worker_id": "dev_example…", "name": "ServerWorker", "available": true}],
  "shells": [{"name": "bash", "default": true}, {"name": "sh", "default": false}],
  "default_shell": "bash",
  "default_folder": {"path": "/workspace/ai-shuffle/.fluid/worktrees/task-1234", "source": "worktree"},
  "limit": 8,
  "open": 3
}
```

Only shells that exist on the worker are listed. On an unsupported worker:
`supported: false`, `unsupported_reason: "Terminals are not supported on
this worker yet."`, `shells: []`, `default_shell: null`,
`default_folder: null`, `available: false`. An unknown chat returns
`404 chat_not_found`.

### `POST /api/v1/terminals`

```json
{
  "session_id": "8d2e6c1a-…",
  "cwd": "/workspace/my-project",
  "shell": "bash",
  "cols": 120,
  "rows": 32,
  "target": {"kind": "worker", "worker_id": "dev_example…"}
}
```

Every field is optional. `201 {"terminal": {…}}`. Possible errors:
`not_supported`, `bad_shell`, `bad_folder`, `chat_not_found`,
`chat_on_other_worker`, `target_unavailable`, `limit_reached`,
`start_failed`.

### `GET /api/v1/terminals/{id}`

`200 {"terminal": {…}}` or `404 not_found`.

### `DELETE /api/v1/terminals/{id}`

Kills the shell's whole process session: SIGHUP and SIGCONT to every process
in it (background jobs included), then SIGKILL after a 1 s grace. Every
attachment is closed with `4410`.

```json
{"closed": true, "id": "term_3f9c0a1b2c4d5e6f"}
```

### `POST /api/v1/terminals/{id}/restart`

Only for an `exited` terminal. Starts the same shell in the same folder under
the same id; the old scrollback is kept and new output follows it. Attached
sockets stay attached and get a `state` message.
`200 {"terminal": {…}}`. Errors: `not_exited`, `bad_folder` (the folder was
removed), `not_supported`, `start_failed`, `not_found`.

### `POST /api/v1/terminals/{id}/paste`

```json
{"text": "git log --oneline -5\n"}
```

Puts text at the prompt **without running it**. The client sends the raw
clipboard text and nothing else; the server does all the cleaning:

1. CRLF and lone CR become LF.
2. Control characters are removed: C0 except TAB and LF, DEL, and C1. This
   includes ESC, so the text cannot close the bracketed-paste wrapper or send
   ^C/^D.
3. Trailing newlines are stripped.
4. The server waits (up to 5 s) for the shell to be at its prompt, then
   refuses with `409 terminal_busy` if a command holds the terminal.
5. The text is sent as `ESC[200~` + text + `ESC[201~` only when it is
   certain bash's readline will hold it. Embedded newlines then stay in the
   line editor and nothing runs until the user presses Enter.
   `mode: "bracketed"`. All of these must be true at paste time:
   - the terminal was created as `bash` (the shell kind recorded at
     creation; `sh` never gets the wrapper);
   - the most recent bracketed-paste mode sequence in the output was "on"
     (`ESC[?2004h`; readline sends it at every prompt and `ESC[?2004l` when
     a line is accepted, and both are tracked);
   - the PTY is in readline's input mode (ICANON and ECHO off), read from the
     terminal itself;
   - on Linux, the shell process is still that bash binary
     (`/proc/<pid>/exe`), not something it `exec`ed.

   Output alone is never trusted: any program can print `ESC[?2004h` (a
   `cat` of a file, a `printf`, a program killed with the mode on). A shell
   that does not understand the markers runs every pasted line but the last,
   so `sh` with such output, bash with line editing off (`set +o emacs`) and
   bash after `exec sh` all fall through to rule 6. The PTY mode is the part
   output cannot fake; the one gap left is a bash whose owner has turned
   line editing off *and* put the PTY into raw mode by hand (`stty -icanon
   -echo`) before a program printed the "on" sequence.
6. Otherwise a single line is written with tabs turned into spaces, so it
   cannot trigger completion. `mode: "plain"`. A multi-line paste is refused
   with `422 paste_multiline_unsupported` and nothing is written. For `sh`:
   "This shell can't hold a multi-line command without running it. Paste one
   line, or use bash." For bash (bracketed paste or the line editor is off):
   "This terminal can't hold a multi-line command without running it right
   now (bash's line editor or its bracketed paste is off). Paste one line."

```json
{"mode": "bracketed", "terminal": {"id": "term_…", "…": "…"}}
```

Errors: `paste_too_large`, `terminal_exited`, `terminal_busy`,
`paste_multiline_unsupported`, `terminal_unresponsive`, `not_found`.

Clients must use this route for clipboard paste. Text sent as WebSocket
`input` is typed, so a newline in it runs the line.

## WebSocket

`GET /api/v1/ws/terminals/{id}[?token=<owner or paired session token>]`

The server accepts the connection, then authenticates. Server→client output
is **binary** frames of raw terminal bytes (UTF-8 from the shell, possibly
split mid-character; feed them to the emulator as bytes). Everything else is
JSON text frames.

### Handshake

1. `{"type": "attached", …}`:

   ```json
   {
     "type": "attached",
     "protocol": 1,
     "terminal": {"id": "term_…", "state": "live", "…": "…"},
     "replay_bytes": 18234,
     "limits": {
       "max_message_bytes": 65536,
       "input_burst_bytes": 262144,
       "input_bytes_per_second": 131072,
       "message_burst": 200,
       "messages_per_second": 100,
       "cols": [10, 500],
       "rows": [2, 200]
     }
   }
   ```

2. Zero or more binary frames: the scrollback (`replay_bytes` in total).
3. `{"type": "replayed"}`. From here on, binary frames are live output. The
   snapshot and the live stream are taken together, so nothing is lost or
   repeated.

Clients should reset the emulator before writing the replay.

### Client → server

| Message | Effect |
| --- | --- |
| `{"type": "input", "data": "ls -la\r"}` | Keystrokes, written as UTF-8 exactly as given. Enter is `\r`. |
| `{"type": "resize", "cols": 120, "rows": 32}` | Sets the PTY size (clamped). Integers only. The size is shared by every attachment; last writer wins. |
| `{"type": "ping"}` | Server replies `{"type": "pong"}`. |
| `{"type": "pong"}` | Reply to a server ping; ignored. |

Anything else (binary frames, non-object JSON, unknown `type`, wrong field
types) closes the socket with `4400`.

### Server → client (JSON)

| Message | When |
| --- | --- |
| `{"type": "state", "terminal": {…}}` | State, `busy` or `exit_code` changed, or a restart. |
| `{"type": "error", "code": "terminal_exited", "message": "This terminal has exited. Restart it to type again."}` | Input could not be written (`terminal_exited` or `terminal_unresponsive`). The socket stays open. |
| `{"type": "ping"}` | After 30 s with nothing to send. Reply `pong` (optional). |
| `{"type": "pong"}` | Reply to a client ping. |

### Close codes

| Code | Reason | Client should |
| --- | --- | --- |
| `4001` | Authentication failed / Signed out | Stop; sign in again. |
| `4004` | Terminal not found | Drop the tab; refresh the list. |
| `4400` | Protocol violation / Message too large | Fix the client; do not retry in a loop. |
| `4408` | Too far behind; reattach | Reconnect at once (the replay covers the gap). |
| `4410` | Terminal closed | Drop the tab. |
| `4429` | Input too fast | Reconnect and slow down. |
| `1001` | Worker stopping | Show the terminal as gone; the worker restarts with none. |
| `1011` | Send failed | Reconnect. |
| `1013` | Authorization unavailable; retry later (at the handshake or a later re-check) | Retry with backoff. |

Several sockets may attach to one terminal (two tabs, phone and desktop).
All see the same output; input from any of them goes to the same shell.

### Reattach after a browser reload

1. Keep the open terminal ids per chat in client storage (or simply call
   `GET /api/v1/terminals?session_id=<chat>` on load).
2. For each id, open `GET /api/v1/ws/terminals/{id}`.
3. On `attached`, reset the emulator and apply `terminal.cols/rows` or send
   your own `resize`.
4. Write the binary frames that arrive before `replayed`: this restores the
   screen from the scrollback (up to 5,000 lines / 2 MB).
5. After `replayed`, keep writing binary frames as live output and send
   `input`/`resize` as usual.
6. If the socket closes with `4004` or `4410`, remove the tab. After `1001`
   or a failed reconnect, call the list route: an empty list means the worker
   restarted.

## Lifetime and app restarts

Terminals live in the worker process only. Nothing is written to the
database. When the app stops, every shell's process session is killed (in the
app lifespan, with an `atexit` fallback) and attached sockets close with
`1001`. **After a restart the terminal list is always empty**; clients must
treat any stored ids as gone.

## Shell environment

The shell gets the worker's own environment **minus every secret**, plus
`TERM=xterm-256color`, `COLORTERM=truecolor`, `SHELL`, and defaults for
`LANG`, `HOME` and `PS1`. Dropped: anything `fluid.tools.child_env` treats as
a host secret, the provider credentials that the Bash tool is allowed to pass
through (they are dropped here too), any name containing SECRET, TOKEN,
PASSW, CREDENTIAL, PRIVATE, AUTH, COOKIE, SESSION, SALT, SIGNING, WEBHOOK or
APIKEY or ending in `_KEY`/`_KEYS`/`_DSN`, any URL value with a
`user:password@` part, and any URL value with a credential in its query
(`token=`, `key=`, `sig=`, `secret=`, `password=`, `auth=` and the like). The
shell runs as the worker's own OS user in its own session (`setsid`), with
the PTY as its controlling terminal.

`AI_SHUFFLE_SERVER_RUN` is kept on purpose. It is not a credential (agents'
processes carry it too); it is how the process reaper (#1913) finds a shell
that a crashed server left behind and kills it at the next start, while a
live server's shells are never reaped.

This scrub is defence in depth. Because the shell runs as the app's own user,
it can read whatever that user can read on disk, including the data
directory; only the server process's own environment is out of reach (the
server is non-dumpable, so `/proc/<server pid>/environ` is unreadable).

## Logging

At INFO: `Terminal opened` (id, shell, folder, chat, who), `Terminal
restarted`, `Terminal exited` (id, code, shell, folder), `Terminal closed`
(id, shell, folder, who), and the shutdown count. "who" is `owner`,
`paired:<client id>` or `desktop`, never a credential. Keystrokes, pasted
text and output are never logged.

## Error codes

| Code | HTTP | Message |
| --- | --- | --- |
| `not_supported` | 501 | Terminals are not supported on this worker yet. |
| `bad_shell` | 400 | <shell> isn't available on this worker. Choose one of: bash, sh. |
| `bad_folder` | 400 | The folder must be a full path, for example /workspace/my-project. / The folder <path> doesn't exist on this worker. / <path> is a file, not a folder. / The folder <path> doesn't exist on this worker anymore. (restart) |
| `chat_not_found` | 404 | That chat doesn't exist. |
| `chat_on_other_worker` | 409 | That chat runs on another worker. Terminals open on this worker only for now. |
| `target_unavailable` | 400 | Choose an online terminal worker or an approved SSH grant. |
| `limit_reached` | 409 | 8 terminals are open on <worker>. Close one to open another. |
| `start_failed` | 500 | The shell couldn't start on this worker. |
| `not_found` | 404 | That terminal is closed or never existed. |
| `not_exited` | 409 | This terminal is still running. Close it or wait for it to exit. |
| `terminal_exited` | 409 | This terminal has exited. Restart it to type again. |
| `terminal_busy` | 409 | A command is running in this terminal. Paste when it's back at the prompt. |
| `terminal_unresponsive` | 409 | The terminal isn't reading input right now. Try again in a moment. |
| `paste_too_large` | 413 | That's too much text to paste at once (limit 256 KB). |
| `paste_multiline_unsupported` | 422 | This shell can't hold a multi-line command without running it. Paste one line, or use bash. (sh) / This terminal can't hold a multi-line command without running it right now (bash's line editor or its bracketed paste is off). Paste one line. (bash) |
| (auth) | 403 | Only the owner can use terminals. Sign in with the owner password. |
| (auth) | 403 | Agent previews can't use terminals. |
| (auth) | 401/403 | Owner access required (from `require_admin`). |

## Remote implementation

`app/network/terminal_relay.py` handles the `terminal` and `terminal-owner`
control actions. Membership alone cannot open a shell: an opaque lease is
issued only after the UI owner check, bound to one target worker, and checked
back at the source against the still-valid original credential. Bounded
viewer output is base64-encoded for the JSON control transport; the UI still
receives binary WebSocket output. A missing/old capability never enables a
relay optimistically. No credentials or connections are replicated.

`app/core/terminal_targets.py` adapts peer/SSH streams to the existing bounded
terminal registry. SSH tasks belong to the private-access grant and close the
connection on revocation, even if cancellation precedes the reader's start.
