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
without autonomously continuing the parent agent.

## AI Shuffle integration requirements

The AI Shuffle task manager should keep worker records in its session/task store,
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
