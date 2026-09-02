# Background task parking and wake-up

Fluid treats background workers as host-owned processes with a durable session
lifecycle. A parent model turn may finish or be interrupted without cancelling a
worker that crossed the spawn acknowledgement boundary.

## Host contract

The host dispatcher injected as `ToolCtx.extra["task_dispatch"]` must return one
of these values for `Task(mode="background")`:

- `BackgroundTaskReceipt(task_id=..., worker_session_id=..., description=...)`;
- `ToolOutcome(..., background_task=BackgroundTaskReceipt(...))`; or
- a mapping containing `task_id` and optional `worker_session_id`, `description`,
  `text`, `ui`, and `is_error` fields.

Returning the receipt is the atomic detach point. After it returns, the worker
must not be a child of the originating HTTP request, WebSocket turn, Fluid tool
coroutine, or engine object. User interruption and `TurnComplete` must only stop
the parent model loop.

When the worker reaches a terminal state, the host submits its result to the
parent engine:

```python
from fluid.events import BackgroundTaskFinished

await parent_engine.submit(BackgroundTaskFinished(
    task_id=task.id,
    worker_session_id=task.worker_session_id,
    status="finished",  # or "failed" / "cancelled"
    summary=task.report,
))
```

Fluid emits and persists the following lifecycle:

1. `SubagentEvent(kind="started")` after the detached receipt is accepted.
2. `SessionParked` before a terminal parent-turn event while workers remain.
3. `SubagentEvent(kind="finished" | "failed" | "cancelled")` on completion.
4. `SessionWoken` before a synthetic continuation turn containing the results.

Completion operations are idempotent by `task_id`. Completions arriving during
another turn are queued and delivered after that turn. Multiple queued results
are batched into one continuation. Started and completed state is rebuilt from
the durable event log when an engine is recreated.

Set `wake=False` only when the host wants to persist a terminal task result
without autonomously continuing the parent agent. `wake` is a directive to the
engine, not a record of what happened: `wake=False` means fluid stands down
because the host drives the continuation itself, not that no wake occurred.

Read `SubagentEvent.wake_disposition` when the question is what became of the
wake. It has three values where `wake` has two:

| value | meaning |
| --- | --- |
| `auto` | terminal completion; the engine wakes the parent |
| `suppressed` | terminal completion; a wake is owed, the host performs it |
| `not_applicable` | `started` / `completion_pending`; waking is not yet a question |

It defaults to `auto`, so events persisted before the field existed decode to
the same thing their `wake=True` already meant.

Every persisted event also carries `ts` — wall clock at publish, epoch seconds,
assigned by the `EventBus` alongside `seq`. `0.0` means unstamped: an event that
never went through the bus, or a row written before the field existed.

## Monitor: detached shell commands

The `Monitor` builtin uses the same lifecycle for plain processes the agent
wants to supervise — a CI run, a frontend build, a log watcher. The host
injects an async spawner as `ToolCtx.extra["monitor_spawn"]`; it receives
`(input, ctx)` where `input` carries `command`, optional `cwd` and optional
`serves`, and returns the same receipt shapes as a background Task (a
`BackgroundTaskReceipt`, an `Outcome` carrying one, or a mapping with
`task_id`). The spawner must run the command detached from the turn and buffer
its output; when the command reaches a terminal state, submit
`BackgroundTaskFinished` with the output tail as `summary`. The session parks
if the turn ends first and is woken with the output when the command finishes.

Without an injected spawner, Monitor reports itself unavailable (the same
graceful degradation as Task without a dispatcher).

### Spawn-time lint

Monitor appends a warning to the receipt when the command has no natural end
(`while true`, `until`, `tail -f`, `watch`, a log follow) and carries no time
budget — no `timeout`, deadline arithmetic or iteration cap — or when a glob
like `*""*` collapses to `**` and makes a loop's exit arm dead code. It warns,
never refuses: the spawn still happens and the receipt still carries its task
id. A command that looks like a server is pointed at `serves` instead.

### `serves`: a service, not a job

`serves: true` marks a process that is MEANT to stay up — a dev server, a
tunnel, a queue worker. The receipt sets `BackgroundTaskReceipt.serves`, the
durable `SubagentEvent(kind="started")` carries it, and
`BackgroundLifecycleMixin._park_pending_ids` — the single place park accounting
is decided — excludes it. So a serving task is registered, surfaced, stoppable
and still wakes the session when it dies, but the turn that started it may end
without parking, `SessionParked.pending_task_ids` never lists it, and a turn
whose only live tasks are serving ones emits no `SessionParked` at all. Hosts
read the flag off the receipt to skip whatever time bound they impose on
ordinary Monitor jobs and to settle a deliberate stop as `cancelled` rather
than a failure.

## Standalone CLI host

`fluid chat` and `fluid run` are a first-class host: `fluid.cli.config.make_engine`
attaches a `fluid.cli.host.CliHost` to every engine it builds, which injects
`task_dispatch`, `monitor_spawn`, `agent_roster` and `agent_message` (the host
itself rides along as `ToolCtx.extra["cli_host"]`).

- **Monitor** runs the command as a detached asyncio subprocess, buffers
  stdout+stderr (head + tail, capped), and submits `BackgroundTaskFinished`
  with the exit code and output tail when it exits. Cancellation kills the
  whole process tree (`taskkill /T` on Windows, the process group on POSIX).
- **Task** builds a child `NativeLoop` from the same config and provider
  client, with its own session in the shared store. A named agent definition
  supplies the child's system prompt, tool restriction, and model override;
  otherwise a generic subagent prompt applies. Children never get `Task`
  themselves (no recursive fan-out) but do get `Monitor`. `mode="sync"` runs
  inline; `mode="background"` detaches and reports the child's final assistant
  text as the summary.
- **ListAgents / SendMessage** address the workers the host is holding. A row
  is `name [ref]` — `name` is Task's `label` (or the agent name) and `ref` the
  tail of the task id, needed only when two live workers share a name; a child
  also sees `main`, its parent. Delivery is one `AgentMessage` submit and the
  receiving engine picks the channel: queued for its next tool result while a
  turn runs, an ordinary user-role turn when it is idle (which is what wakes a
  parked session). Messaging a worker that has already finished is an error,
  not a resume — its report comes back on its own.
- **Peer messages are not user messages.** They arrive in their own marker
  (`fluid.context.peer`), never the steering marker, and the prompt block that
  teaches the marker says so: a peer's text is weighed like a colleague's
  suggestion, cannot authorize anything, and never outranks the user. Incoming
  text is defanged on the way in, so a worker cannot forge a `[user
  interjection]` inside its own message.
- **Permissions**: the child inherits the parent's approval policy and rules.
  Its permission prompts route to the host's `permission_responder` — the REPL
  wires in its own y/N prompt; the one-shot `run` path denies with a message
  that names the parent's policy as the reason.
- **Lifecycle in the CLI**: `fluid run` streams through park and wake and only
  exits once no worker is in flight. The REPL returns the prompt while work
  runs, renders `SubagentEvent` / `SessionParked` / `SessionWoken` (and the
  wake turn itself) through a background watcher, and adds `/tasks` and
  `/cancel <task_id>`. Closing the host cancels whatever still runs and
  reports it as `cancelled`.

## Host-app integration requirements

The host's task manager should keep worker records in its session/task store,
not in the turn runner's cancellation scope. Its worker completion callback must
load or recreate the parent Fluid engine by session id and submit
`BackgroundTaskFinished`. It should retain the completion until Fluid has
persisted `SessionWoken`.

A user message racing with a wake is handled using the normal active-turn rule:
whichever turn starts first owns the engine. If the wake starts first, the host
may submit `Interrupt` before forwarding the user turn, exactly as for any other
active model turn. Explicit task cancellation should terminate the worker and
submit a `status="cancelled"` completion; parent-turn interruption must not be
used as task cancellation.

The delegated engine uses the same contract through Fluid's bundled MCP bridge.
The injected host `Task` tool should be preferred over a delegated CLI's own
process-scoped background-task implementation, because only the host-managed
worker has a lifetime independent of the CLI process.
