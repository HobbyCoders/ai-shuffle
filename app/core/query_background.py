"""
Background query execution for fire-and-forget streaming.

Extracted from query_engine.py to isolate background task management.
"""

import logging
import uuid
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timezone

# Provider-agnostic mirrors of the SDK message vocabulary (identical names —
# see app/core/provider/events.py). The stream below arrives pre-translated
# through adapter.receive_events(), so no claude_agent_sdk message class is
# dispatched here.
from app.core.provider.events import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage, RateLimitEvent, HookEventMessage,
)

from app.db import database
from app.db import turns as turns_db
from app.db.async_database import run_sync
from app.core.session_title import derive_session_title
from app.core.session_pool import (
    SessionState, get_session_pool, is_subprocess_alive,
    requires_reconnect,
    reconnect_option_names,
)
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import permission_handler
from app.core.session_context import set_current_session_id

from app.core.query_persistence import (
    _wrap_stderr_capture,
    _extract_connection_error,
    _persist_turn_results,
    USAGE_PROVIDER_KEY,
    capture_usage_identity,
    schedule_context_usage_capture,
    log_captured_cli_stderr,
)
from app.core.query_streaming import (
    is_context_snapshot_only as _is_context_snapshot_only,
    truncate_large_payload,
)
from app.core.memory_sync import (
    memory_leg_for_options, reconcile_memory, push_memory,
)
from app.core.turn_finalize import persist_turn_end_state

logger = logging.getLogger(__name__)

# Pending background tasks awaiting pool entry creation
_pending_background_tasks: Dict[str, asyncio.Task] = {}


def _store_background_task(session_id: str):
    """Store pending background task reference on the pool state entry.

    Called from _run_background_query after pool.put() ensures the state exists.
    """
    task = _pending_background_tasks.pop(session_id, None)
    if task:
        state = get_session_pool().peek(session_id)
        if state:
            state.background_task = task


async def _run_background_query(
    session_id: str,
    prompt: str,
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    overrides: Optional[Dict[str, Any]],
    resume_id: Optional[str],
    device_id: Optional[str],
    api_user_id: Optional[str],
):
    """Recording tap around ``_run_background_query_impl``.

    A ``TurnRecorder`` records the initial user message and every pump-shaped
    event the impl produces into the typed session event log; ``close()`` runs
    in the ``finally`` so the crash/abandonment path still records a terminal
    ``turn.aborted``. When no recorder is available this wrapper is a pure
    passthrough.
    """
    from app.core.turn_recorder import TurnRecorder

    recorder = await TurnRecorder.maybe_create(session_id)
    if recorder:
        await recorder.start(prompt, profile_id=profile.get("id"))
    try:
        await _run_background_query_impl(
            session_id=session_id,
            prompt=prompt,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_id=resume_id,
            device_id=device_id,
            api_user_id=api_user_id,
            recorder=recorder,
        )
    finally:
        if recorder:
            await recorder.close()


async def _run_background_query_impl(
    session_id: str,
    prompt: str,
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    overrides: Optional[Dict[str, Any]],
    resume_id: Optional[str],
    device_id: Optional[str],
    api_user_id: Optional[str],
    recorder=None,
):
    """
    Run a streaming query in the background, independent of HTTP connection.

    This allows work to continue even when:
    - User locks their phone
    - Browser tab is backgrounded
    - HTTP connection is interrupted

    Events are recorded to the v2 session event log via the TurnRecorder
    created by the ``_run_background_query`` wrapper.
    """
    # Extract profile_id for per-query tracking
    profile_id = profile.get("id")
    turn_started_at = datetime.now(timezone.utc)

    # Store user message and broadcast to other devices
    user_msg = await run_sync(
        database.add_session_message,
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    await run_sync(
        database.add_sync_log,
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options + wire plugin/DB hooks (incl. blocking PreToolUse guards)
    # through the shared sdk_options seam, the same as the WebSocket path.
    from app.core.sdk_options import build_session_options
    from app.core.query_options import WorktreeUnavailableError, ensure_pending_checkout
    session_row = await run_sync(database.get_session, session_id)
    # Async wait for an in-flight ensure_worktree. Must stay on the event
    # loop: a sync poll would occupy a run_sync worker for 30s.
    if session_row:
        await ensure_pending_checkout(session_row)
    try:
        # Off the event loop — the build can run git fetch/worktree add and
        # several DB reads synchronously (see query_engine.execute_query).
        options, agents_dict = await asyncio.to_thread(
            build_session_options,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_session_id=resume_id,
            api_user_id=api_user_id,
            session_id=session_id,
            hook_project_id=project.get("id") if project else None,
        )
    except (WorktreeUnavailableError, ValueError) as e:
        # A misconfigured/deleted/disabled model provider raises
        # ProviderConfigError (a ValueError); a flagged worktree raises
        # WorktreeUnavailableError. Either escaping this create_task'd coroutine
        # produces a messageless turn.aborted plus an unretrieved-exception log.
        # Surface it as a turn error and stop — mirror the connect-failure path.
        logger.error(f"[Background] Cannot build session options for {session_id}: {e}")
        if recorder:
            await recorder.on_event({"type": "error", "message": str(e)})
        return

    # Credential gate (see query_engine._stream_to_websocket_impl).
    from app.core import credential_gate

    _refusal = await credential_gate.refusal_for_options_async(options, api_user_id)
    if _refusal:
        logger.warning(f"[Background] Turn refused for session {session_id}: {_refusal}")
        if recorder:
            await recorder.on_event(credential_gate.refusal_event(_refusal))
        return

    # --- Session Pool: Acquire or create the pooled adapter ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        # Busy-write guard backstop: never write a second prompt into a client
        # that is mid-turn (kills the CLI, loses the whole in-flight turn).
        # Fail safe like the connect-failure path — but do NOT cleanup the live
        # session store here: the active turn owns it and is still streaming.
        if existing_state.is_streaming:
            error_msg = (
                "Session is busy with another response. "
                "Wait for it to finish, then send your message again."
            )
            logger.warning(
                f"[Background] Pooled client for session {session_id} is mid-turn — "
                f"refusing concurrent background query (busy-write guard)"
            )
            if recorder:
                await recorder.on_event({"type": "error", "message": error_msg})
            return
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            # CLAIM at the reuse decision point (no await since the is_streaming
            # check above) so two near-simultaneous entrants can't both pass the
            # gate. The finally below clears it on every exit path.
            state.is_streaming = True
            # Claim-to-try guard: any exit between this claim and the main
            # try/finally (the set_model await below is a suspension point) must
            # clear the flag — a stranded is_streaming=True bricks the pooled
            # client (busy error forever, never idle-evicted).
            try:
                # Runtime-changeable options (model, permission_mode) are baked
                # into the CLI subprocess at spawn. requires_reconnect() lets them
                # change without a respawn, so a mid-session switch must be pushed
                # into the live subprocess here. Never raise: an exception between
                # the is_streaming claim and the main try/finally would strand the
                # flag and brick the pooled client.
                # The adapter writes each applied model / permission-mode change
                # back to these options (HarnessAdapter._record_applied), so they
                # hold the live values, not the connect-time ones.
                old_opts = state.connect_options
                if old_opts:
                    new_model = getattr(options, 'model', None)
                    old_model = getattr(old_opts, 'model', None)
                    # Same-provider-only hot-swap — a provider change is
                    # caught by requires_reconnect() and respawns instead; a
                    # set_model() across providers would send an unknown model id
                    # to the wrong upstream.
                    _same_provider = getattr(old_opts, 'aih_provider_id', None) == getattr(options, 'aih_provider_id', None)
                    if new_model and new_model != old_model and _same_provider:
                        try:
                            # Live hot-swap stays within the session's current provider;
                            # a provider change is caught by requires_reconnect() and
                            # forces a respawn instead of reaching this set_model() call.
                            await state.adapter.set_model(new_model)
                            logger.debug(f"[Background] Updated model to {new_model} for reused session {session_id}")
                            # Persist the switched model so launched_model never
                            # goes stale — the host-op guard reads this column.
                            try:
                                await run_sync(database.update_session, session_id, launched_model=new_model)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"[Background] Failed to set model on reused session {session_id}: {e}")

                    new_perm_mode = getattr(options, 'permission_mode', None)
                    old_perm_mode = getattr(old_opts, 'permission_mode', None)
                    if new_perm_mode and new_perm_mode != old_perm_mode:
                        try:
                            await state.adapter.set_permission_mode(new_perm_mode)
                            logger.debug(f"[Background] Updated permission_mode to {new_perm_mode} for reused session {session_id}")
                        except Exception as e:
                            logger.warning(f"[Background] Failed to set permission_mode on reused session {session_id}: {e}")

                logger.info(f"[Background] Reusing pooled client for session {session_id}")
            except BaseException:
                state.is_streaming = False
                state.streaming_started_at = None
                raise
        else:
            logger.info("[Background] Options changed for session %s (%s), creating fresh client",
                        session_id, ",".join(reconnect_option_names(existing_state, options)))
            await pool.remove(session_id, preserve_session=True)
            existing_state = None
    elif existing_state:
        logger.warning(f"[Background] Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Admission backpressure: refuse a NEW session if the box is at its
        # process/RAM/pool ceiling, rather than killing in-flight work later.
        # psutil process-tree walk — off the loop.
        # Lane-aware: the pool's subprocess cap counts DELEGATED sessions
        # only, and deciding the leg can read the provider row, so both
        # run in the same worker thread.
        refusal = await asyncio.to_thread(
            lambda: pool.admission_check(memory_leg_for_options(options))
        )
        if refusal is not None:
            logger.warning(f"[Admission] Refusing new background session {session_id}: {refusal}")
            raise RuntimeError(f"Server is at capacity — {refusal}. Please retry shortly.")

        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"[Background] Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Attribute this session to any task created inside adapter.connect()
        # below: it inherits this context, so an error such a task logs (e.g.
        # "Fatal error in message reader") carries the session id into error_log.
        set_current_session_id(session_id)

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        # Pull shared agent-memory from the DB before the CLI reads it.
        await reconcile_memory(
            project.get("id") if project else None,
            getattr(options, "cwd", None),
            memory_leg_for_options(options),
        )

        # Backend selection via the provider seam (see
        # app/core/provider/factory.py): make_adapter() always returns the fluid
        # HarnessAdapter.
        from app.core.provider.factory import make_adapter
        adapter = make_adapter()

        try:
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            logger.debug(f"[Background] Connected to Claude SDK for session {session_id}")
        except asyncio.TimeoutError:
            logger.error(f"[Background] Timed out connecting to Claude SDK for session {session_id} (60s)")
            raise RuntimeError("Connection timed out — an MCP server may be unreachable")
        except Exception as e:
            error_msg = _extract_connection_error(e, cli_stderr_lines)
            logger.error(f"[Background] Failed to connect to Claude SDK for session {session_id}: {error_msg}")
            if recorder:
                await recorder.on_event({"type": "error", "message": error_msg})
            return

        state = SessionState(
            adapter=adapter,
            sdk_session_id=resume_id,
            is_connected=True,
            connect_options=options,
            cli_stderr_lines=cli_stderr_lines,
        )
        pool.put(session_id, state)

    # Mark as streaming
    state.is_streaming = True
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Claim-to-try guard: is_streaming is claimed but the protective
    # try/finally hasn't started yet — any exit here must clear the flag or
    # the pooled client is bricked (busy error forever, never idle-evicted).
    try:
        # Store pending background task reference now that pool entry exists
        _store_background_task(session_id)

        # This lane bypasses the turn queue, so there is no live turn row and
        # the identity falls back to a fresh per-run key. Without one,
        # _persist_turn_results refuses to book and the run's usage is lost.
        metadata = {
            "usage_settlement_key": await capture_usage_identity(session_id),
            USAGE_PROVIDER_KEY: getattr(options, "aih_provider_id", None),
        }
        sdk_session_id = resume_id
        interrupted = False
    except BaseException:
        state.is_streaming = False
        state.streaming_started_at = None
        raise

    try:
        prepare_completions = getattr(
            state.adapter, "prepare_background_completions", None
        )
        if callable(prepare_completions):
            prepared = prepare_completions(
                getattr(options, "aih_background_task_completions", None)
            )
            if asyncio.iscoroutine(prepared):
                await prepared
        set_turn_source = getattr(state.adapter, "set_turn_source", None)
        if callable(set_turn_source):
            set_turn_source(getattr(options, "aih_turn_source", None))
        await state.adapter.send_query(prompt)

        async for message in state.adapter.receive_events():
            if isinstance(message, SystemMessage):
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"[Background] Captured SDK session ID: {sdk_session_id}")
                    # Stamp the effective launched model on EVERY spawn/init so
                    # the Unraid host-op guard can resolve THIS session's model
                    # authoritatively from sessions.launched_model without waiting
                    # for turn-end session_messages.metadata['model']. The other
                    # spawn paths (execute_query / stream_query /
                    # stream_to_websocket) do the same. A None
                    # model is a no-op in update_session (never clobbers). The
                    # sdk_session_id link stays owned by end-of-turn
                    # _persist_turn_results on this path (unchanged).
                    try:
                        await run_sync(
                            database.update_session,
                            session_id,
                            launched_model=getattr(options, "model", None),
                        )
                    except Exception as link_err:
                        logger.warning(
                            f"[Background] launched_model persist failed for "
                            f"{session_id}: {link_err}"
                        )

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        if recorder:
                            await recorder.on_event({
                                "type": "chunk",
                                "content": block.text,
                            })

                    elif isinstance(block, ToolUseBlock):
                        if recorder:
                            await recorder.on_event({
                                "type": "tool_use",
                                "name": block.name,
                                "id": getattr(block, 'id', None),
                                "input": block.input if isinstance(block.input, dict) else {},
                            })

                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)

                        if recorder:
                            await recorder.on_event({
                                "type": "tool_result",
                                "name": getattr(block, 'name', 'unknown'),
                                "tool_use_id": getattr(block, 'tool_use_id', None),
                                "output": output,
                                "is_error": getattr(block, 'is_error', False),
                            })

                metadata["model"] = message.model

                # Track per-API-call usage so we can persist the LAST call's
                # values (= current context window size). ResultMessage.usage
                # is cumulative across the agent loop and would inflate.
                #
                # Subagent (sidechain) messages carry parent_tool_use_id and run
                # their own, separate context window — counting them here would
                # overwrite the parent session's last-call figures with an
                # unrelated number, so they are skipped entirely.
                if getattr(message, "usage", None) and not getattr(message, "parent_tool_use_id", None):
                    # A snapshot-only record describes a call that was already
                    # billed, so its zeroes must not blank the last-call size —
                    # but it still reaches the recorder, to carry the breakdown.
                    if not _is_context_snapshot_only(message.usage):
                        metadata["last_call_tokens_in"] = message.usage.get("input_tokens", 0)
                        metadata["last_call_cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                        metadata["last_call_cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
                    if recorder:
                        await recorder.on_event({
                            "type": "turn_usage",
                            "usage": message.usage,
                        })

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        logger.debug(f"[Background] UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, content length: {len(str(block.content) if block.content else '')}")

                        if recorder:
                            await recorder.on_event({
                                "type": "tool_result",
                                "name": "unknown",
                                "tool_use_id": block.tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False),
                            })

            elif isinstance(message, RateLimitEvent):
                logger.debug(f"[Background] Rate limit event: status={message.rate_limit_info.status}, type={message.rate_limit_info.rate_limit_type}")
                from app.core.query_engine import _apply_rate_limit_live_event
                await _apply_rate_limit_live_event(message.rate_limit_info)

            elif isinstance(message, HookEventMessage):
                logger.debug(f"[Background] Hook event: {getattr(message, 'hook_event_name', None)}")
                if recorder:
                    await recorder.on_event({
                        "type": "hook_event",
                        "hook_event_name": getattr(message, "hook_event_name", None),
                        "matcher": getattr(message, "matcher", None),
                        "decision": getattr(message, "decision", None),
                        "reason": getattr(message, "reason", None),
                        "system_message": getattr(message, "system_message", None),
                        "tool_name": getattr(message, "tool_name", None),
                        "session_id": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                metadata["stop_reason"] = getattr(message, 'stop_reason', None)
                # Terminal reason + typed per-model usage pass-through
                metadata["terminal_reason"] = getattr(message, "terminal_reason", None)
                metadata["model_usage"] = getattr(message, "model_usage", None)
                # Surface provider-reported errors
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"[Background] ResultMessage contains errors: {result_errors}")
                # Surface HTTP error status code; no WS yield — background loop has no direct consumer
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    # A 401 on the delegated leg is an expired Claude Code
                    # sign-in that only the user can fix, and nobody watches
                    # a background turn, so it still gets the sign-in notice.
                    # Other statuses keep the silent background behaviour.
                    from app.core.query_engine import (
                        _adapter_is_delegated,
                        _emit_api_error_notification,
                    )

                    if (
                        str(api_err) == "401"
                        and _adapter_is_delegated(state.adapter)
                    ):
                        await _emit_api_error_notification(
                            api_user_id, session_id, api_err, delegated=True,
                        )
                # Extract token counts from usage dict.
                # ResultMessage.usage is CUMULATIVE across every API call in
                # the agent loop — safe for increment totals, unsafe as a
                # context-window snapshot. The live store is already updated
                # per-AssistantMessage above with the correct per-call values.
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
                    # Any call this turn with harness-estimated token counts
                    # (the adapter stamps the terminal usage); booked as a
                    # flag on the ledger row, never subtracted.
                    metadata["usage_estimated"] = bool(message.usage.get("usage_estimated"))

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[Background] Query interrupted for session {session_id}")

    except Exception as e:
        logger.error(f"[Background] Stream query error for session {session_id}: {e}")
        # Surface the subprocess's own stderr (exit code / stack trace it printed
        # before dying) so a mid-stream exit-1 on the background path records WHY
        # instead of leaking as "Task exception was never retrieved".
        log_captured_cli_stderr(session_id, state.cli_stderr_lines, context="background mid-stream")
        state.is_connected = False
        metadata["error"] = str(e)

    finally:
        # Mark as not streaming — keep in pool for reuse
        state.is_streaming = False
        state.streaming_started_at = None
        state.last_activity = datetime.now(timezone.utc)
        state.background_task = None

        # Remove dead clients from pool
        if not state.is_connected:
            await pool.remove_dead(session_id)

        # Record the terminal event so the turn closes cleanly in the event
        # log (the wrapper's recorder.close() only appends a crash-path
        # turn.aborted when NO terminal was recorded).
        if recorder:
            if interrupted:
                await recorder.on_event({
                    "type": "interrupted",
                    "message": "Query was interrupted",
                    "metadata": {"terminal_reason": metadata.get("terminal_reason")},
                })
            elif metadata.get("error"):
                await recorder.on_event({
                    "type": "error",
                    "message": metadata["error"],
                })
            else:
                await recorder.on_event({
                    "type": "done",
                    "session_id": session_id,
                    "metadata": metadata,
                })

        # Durable end-of-turn writes (parked marker, branch resync, summary flush).
        await persist_turn_end_state(session_id)

        # Clean up per-session state that is no longer needed.
        # Permission rules and pending requests accumulate per session and are
        # never cleared otherwise, causing unbounded memory growth.
        permission_handler.clear_session_rules(session_id)
        await permission_handler.cancel_all_requests(session_id)

    # Persist session data to database in a batched write.
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
        log_prefix="[Background] ",
    )

    # Terminal history row for a turn that ran outside the scheduler — written
    # after the fact, never live (see turns.record_adhoc_turn). The early
    # returns above (busy-write guard, option-build failure, connect failure)
    # record nothing: they spent no provider tokens.
    try:
        _is_error = bool(metadata.get("is_error"))
        _aborted = interrupted or _is_error or not state.is_connected
        await run_sync(
            turns_db.record_adhoc_turn,
            session_id,
            "api_background",
            outcome="aborted" if _aborted else "completed",
            prompt=prompt,
            profile_id=profile_id,
            project_id=project.get("id") if project else None,
            stop_reason="interrupted" if interrupted else metadata.get("stop_reason"),
            error=str(metadata["errors"]) if (_is_error and metadata.get("errors")) else None,
            started_at=turn_started_at,
            # Context trail for scheduler-enqueued successors: an unpark wake
            # inherits this turn's overrides and api_user attribution via
            # turn_scheduler._enqueue_context_wake, so it runs on the same leg
            # with the same credential scoping as the turn that parked.
            overrides=overrides or None,
            payload={"api_user_id": api_user_id} if api_user_id else None,
        )
    except Exception as e:
        logger.warning(f"[Background] Failed to record ad-hoc turn for {session_id}: {e}")

    # Capture the context-usage breakdown so the endpoint can serve a
    # persisted snapshot post-eviction. Runs DETACHED (self-guards on the pool's
    # live adapter) so it never delays completion.
    schedule_context_usage_capture(session_id, log_prefix="[Background] ")

    # Push agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(
        project.get("id") if project else None,
        getattr(options, "cwd", None),
        memory_leg_for_options(options),
    )

    logger.debug(f"[Background] Query completed for session {session_id}")


async def start_background_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Start a streaming query in a background task.

    Returns immediately with session info. Work continues in background.
    Use interrupt_session() to stop.
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session in database
    resume_id = None

    if session_id:
        session = await run_sync(database.get_session, session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        # This lane bypasses SessionRuntime.execute's archive guard (#1822).
        from app.core import archive_guard
        if archive_guard.is_archived_row(session):
            archive_guard.log_dropped(session_id, "api", "background query refused")
            raise archive_guard.SessionArchived(session_id)
        resume_id = session.get("sdk_session_id")
        logger.debug(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        title = derive_session_title(
            prompt, max_chars=50, word_boundary=False
        )
        session = await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id,
            is_active=True,
        )
        logger.debug(f"Created new session {session_id} with title: {title}")

    # Persist the run binding the API caller picked. The chat lane does
    # this in turn_manager._submit_blocking for every human-facing source —
    # whose _BINDING_SOURCES already include "api" — but this lane bypasses
    # turn_manager entirely. Without it a scheduler-enqueued successor (an
    # unpark wake, auto-continue, failover) would build its options with no
    # binding and empty overrides, fall back to the bare launched_model with no
    # provider, and fail on the wrong leg with the wrong credentials.
    if overrides:
        try:
            await run_sync(
                database.set_session_run_binding,
                session_id,
                model=overrides.get("model"),
                effort=overrides.get("effort"),
                permission_mode=overrides.get("permission_mode"),
                fast=overrides.get("fast"),
                workspace_project_ids=overrides.get("workspace_project_ids"),
            )
            if overrides.get("context_window"):
                # The picker's check, against the model just saved: a window
                # the model does not offer is ignored, neither saved as the pin
                # nor run on this turn.
                from app.core.model_registry import offered_context_window

                row = await run_sync(database.get_session, session_id)
                window = await run_sync(
                    offered_context_window, row, overrides["context_window"]
                )
                if window is None:
                    overrides = {k: v for k, v in overrides.items() if k != "context_window"}
                else:
                    await run_sync(database.set_session_run_binding, session_id,
                                   context_window=window)
        except Exception:
            logger.exception(
                f"[Background] run-binding persist failed for session {session_id}"
            )

    # Generate a message ID for the API response contract (clients key the
    # in-flight turn on it; the runner itself does not consume it).
    assistant_msg_id = f"streaming-{session_id}-{datetime.now(timezone.utc).timestamp()}"

    # Start background task
    task = asyncio.create_task(
        _run_background_query(
            session_id=session_id,
            prompt=prompt,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_id=resume_id,
            device_id=device_id,
            api_user_id=api_user_id,
        )
    )

    # Store task reference for interrupt support
    # The background task picks this up after pool.put() creates the state entry
    _pending_background_tasks[session_id] = task

    # Safety net: if the task completes without _store_background_task being
    # called (e.g., connection failure or early return), remove the orphaned entry.
    def _cleanup_pending(t: asyncio.Task):
        _pending_background_tasks.pop(session_id, None)
    task.add_done_callback(_cleanup_pending)

    return {
        "session_id": session_id,
        "status": "started",
        "message_id": assistant_msg_id
    }
