# Fluid architecture

Fluid separates operations from events. A host calls `ExecutionEngine.submit`
with an op such as `UserTurn` or `Interrupt` and subscribes to events for the
result. The standalone CLI uses `cli.config.make_engine` to assemble instructions,
call `config.loader.build_engine`, and attach a `CliHost`. See
[Engine capabilities](engines.md) for what changes when the model loop runs in
an external CLI or ACP agent. The rest of this page describes the native loop.

## The life of a native turn

1. **Accept the op.** `NativeLoop.submit` rejects a second active `UserTurn` or
   `Resume` and otherwise starts `_run_turn` as an asyncio task. `_run_turn`
   starts the progress watchdog. `_run_turn_inner` opens or creates the session,
   connects configured MCP servers, and rehydrates state such as the deferred
   catalog announcements, compaction decisions and last request fingerprint.
   For a new prompt it prepares the user message, emits `TurnStarted`, checks
   the turn-start hook, and appends the node under the selected parent (the
   head by default). `Resume` normally reuses the unanswered user node; a
   supplied context note may append a new node. A manual compact request takes
   the `_run_manual_compaction` path rather than the ordinary tool loop.
2. **Assemble context.** `_start_and_loop` runs `_agent_loop`. Each iteration
   reads the current branch through `_context_path`. The loop sizes its system
   and tool overhead and uses the last measured prompt when available.
   `Compactor.apply` considers window pressure, reserved output space, and the
   cost of rewriting a warm prefix. It can trim tool results, digest spans,
   and fold older spans into epoch stubs. `_apply_pending_checkpoint` can use a
   prepared summary; `_checkpoint_for_compaction` can wait for one on a deep
   size-driven batch. `_admission_promotions` checks that the resulting view
   fits a known window and can refuse a request before sending it. Compaction
   changes the **request view**, not the original stored nodes. `_emit_compaction`
   records the promotions so `_rehydrate_compaction` can recover them.
3. **Call the model.** `_agent_loop` settles tool-result pointers against the
   current request view, counts a logical call, and invokes `_stream_once`.
   `_build_request` combines the rendered history, system prompt, tool schemas,
   and model settings. The provider client serializes it, handles the wire
   protocol, and yields normalized `StreamChunk`s. `_stream_once_inner` assembles
   the assistant message, tool calls, usage and stop reason, and emits live
   deltas. `_provider_chunks_interruptible` races reads against the interrupt
   signal. One logical call can include multiple HTTP attempts inside a client.
4. **Persist the reply and process tools.** On the normal path `_append_node`
   saves the assistant message and the loop publishes `Usage`. With a registry,
   completed tool requests are handled even when the provider did not use a
   tool-specific stop reason. `_dispatch_tool_interruptible` wraps dispatch;
   `_gated_call` checks availability and inputs, runs pre-tool hooks, applies
   permission policy (including a host response when needed), dispatches and
   runs post-tool hooks. `_dispatch_tool` renders the outcome and emits
   `ToolResult`. The loop appends the results together as a user-role node and
   uses that node as the next request's parent. A call cap can close tool cards
   with error results instead of executing tools the model cannot follow up on.
5. **Stop or park.** A reply with no dispatchable tools normally ends the turn.
   Output exhaustion, queued peer messages, or a stop hook can instead lead to
   another call, subject to the continuation and call limits. The loop emits
   `TurnComplete` with its stop reason, total usage, cost when available, and
   call count. A stopped or failed turn can emit `Interrupted` or `ErrorEvent`
   with partial accounting. `_emit_terminal_with_park` can emit `SessionParked`
   before the terminal event if background jobs are still pending. The terminal
   event ends this turn, not the session. `_run_turn` clears the watchdog and
   turn-local delivery state on exit.

### Failure and recovery paths

- **Retryable HTTP status or pre-stream transport failure.** The provider client
  applies its own policy; `providers/_retry.py` defines shared retry machinery
  and distinguishes temporary rate limiting from a usage/quota gate. Native
  relays retry and rate-limit events from the stream. An auth failure, usage
  gate, or exhausted client retry is not retried just because the engine sees
  an error. It proceeds to the loop's applicable result-specific path.
- **Empty completion or stream cut mid-tool arguments.** `_is_empty_completion`
  and `cut_in_tool_args` select separate bounded retry counters and backoff in
  `_agent_loop`. Retry keeps the same turn and can drop cache affinity on a
  provider that opts into that behavior. The exhausted path becomes an error.
  A runaway argument stream (`arg_stream_error`) has its own retry budget;
  when spent, Fluid returns an argument error tool result if it can, otherwise
  a turn error. An ordinary malformed completed payload goes back as a tool
  error rather than being executed.
- **Replayable transport drop.** `_is_replayable_drop` gates a bounded resend
  before tool dispatch. `_agent_loop` records failed-call usage and closes
  any tool cards from the discarded attempt. Repeated long drops can get one
  more attempt at lower reasoning effort if a lower setting is available.
- **Context overflow or rejected image payload.** `_is_context_overflow` can
  force the next reclaimable `Compactor.escalate` batch before retrying;
  `_is_payload_rejection` instead tries `consume_images_after_rejection` for
  previously seen images. These paths share a bounded retry budget and fall
  through if the view cannot be reduced further by the applicable path.
- **Invalid encrypted reasoning.** On `invalid_encrypted_content`, the loop
  latches a flag and retries with opaque reasoning stripped from the request
  view. It does not retry that error a second time on the same engine.
- **Guardrail refusal.** `_is_guardrail_block` routes the result through
  `_guardrail_pause`, which accounts for the declined call and can ask the host
  to approve a one-turn model change. Only an approved, applicable change
  retries; the original model is restored at turn cleanup.
- **Unrecovered provider error.** `_agent_loop` keeps visible text and closed
  tool requests, if any, in an assistant node marked interrupted, not a finished
  reply. It closes any such tool cards as not executed and emits `ErrorEvent`
  with partial accounting and provider status/message when available.
  `_start_and_loop` also surfaces exceptions from the loop; the watchdog in
  `_run_turn` handles a stalled turn by cancellation. User `Interrupt` is a
  separate path, not a provider retry.

## Where state lives

- **Session store.** `SessionMeta` holds the head; `SessionNode` records a message
  and its parent. `SessionStore.append`, `load_path`, and `set_head` maintain and
  read the tree. The standalone CLI uses `JsonlStore`; tests can use
  `MemoryStore`. `_emit` assigns live event sequence numbers through `EventBus`
  and persists events separately for replay. Reconstructible streaming deltas
  can be lost on a write failure without aborting the turn; failures to persist
  load-bearing events are surfaced. The store holds original messages even
  when a compacted view no longer sends them.
- **In-memory engine and turn state.** The asyncio turn task, interrupt signal,
  pending permission futures, steers, current stream assembly and retry counters
  live on the loop or its stack. The compactor's current view, request probes,
  and provider routing state are also held in memory; rehydration restores
  selected bookkeeping, not a running call or its futures.
- **Background tasks.** The host owns execution (for the CLI, `CliHost` owns
  its workers). `BackgroundLifecycleMixin` keeps pending receipts, completions
  that arrive before their receipt, terminal task IDs, and owed wakes in memory.
  `_background_task_started` and `_finish_background_task` record lifecycle
  events, and `_rehydrate_background_lifecycle_unlocked` uses persisted evidence
  to recover that bookkeeping. `_run_background_wake` starts a synthetic
  `UserTurn(source="unpark")`; `_mark_background_woken` records delivery after
  its user node is appended. Rehydrating bookkeeping does not restart a host
  process. A host with its own turn queue can submit a completion with
  `wake=False` and schedule the continuation itself.

## Module map

- `cli/`: entry points (`run_once`, `main`), REPL streaming (`stream_turn`),
  Textual TUI, prompt assembly, and `CliHost` for CLI task execution.
- `config/`: `HarnessConfig`, layered `load_config`, and `build_engine` wiring
  for providers, permissions, hooks, tools, and stores passed by callers.
- `engine/`: `ExecutionEngine` and backend implementations; provider-facing
  turn loop in `native.py`, stream-JSON bridge in `delegated.py` and
  `delegated_proto.py`, ACP transport and event translation under `acp/`.
  Shared lifecycle code includes `background.py` and `questions.py`.
- `context/`: instruction and settings discovery, prompt sizing and cache
  instrumentation, history compaction, and the rendered request view.
- `messages/`, `events/`, `store/`: message/block types, operation/event types
  and `EventBus`, and the session tree and event persistence interfaces.
- `providers/`: `ProviderClient`, `ProviderRequest`, per-dialect serializers and
  clients, retries and pricing. `FLUID_TRACE_REQUESTS` traces outbound request
  bodies, not usage or terminal status.
- `tools/`, `permissions/`, `hooks/`: `ToolRegistry`, built-ins and MCP bridges;
  `PermissionEngine` policy checks; `HookBus` and configured command handlers.
- `codemode/`, `builtin_skills/`: `CodeModeSession`, `SubprocessKernel`, the
  journal and continual skill pipeline, and shipped skill documents.
- `state.py`, `_filelock.py`, `swarm.py`: state and scratch path helpers,
  advisory file locking, and `SwarmCoordinator` for multi-agent coordination.
