"""
ClaudeAgentAdapter — concrete ProviderAdapter backed by the Claude Agent SDK.

This is the ONE file in the codebase that should import from claude_agent_sdk.
(Exception: MCP tool registration files still import @tool and create_sdk_mcp_server,
and session_sync.py imports list_sessions for session discovery.)

Migration plan:
  Phase 1 (this PR): Adapter wraps lifecycle (connect, disconnect, health, reconnect,
      set_model, set_permission_mode). Streaming loops still access adapter.sdk_client
      directly during the transition.
  Phase 2 (future): Streaming loops use adapter.receive_events() which yields
      ProviderEvent instances, completing the isolation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Optional

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from app.core.expected_shutdown import mark_expected_shutdown
from app.core.provider.protocol import ProviderAdapter
from app.core.provider.types import ProviderEvent, SessionOptions, mcp_servers_equivalent
from app.core.sdk_process_handle import SdkProcessHandle

logger = logging.getLogger(__name__)

# Grace period (seconds) for subprocess to flush JSONL before SIGTERM.
_SUBPROCESS_EXIT_GRACE_SECONDS = 5.0


class ClaudeAgentAdapter(ProviderAdapter):
    """ProviderAdapter implementation for the Claude Agent SDK.

    Wraps a ClaudeSDKClient and isolates all SDK-specific access.
    """

    def __init__(self) -> None:
        self._client: Optional[ClaudeSDKClient] = None
        self._connected: bool = False
        self._sdk_session_id: Optional[str] = None
        self._connect_options: Optional[ClaudeAgentOptions] = None

    # ─── Transition helper ────────────────────────────────────────────
    # During the migration period, streaming loops need direct SDK client
    # access. This property will be removed once receive_events() is complete.

    @property
    def sdk_client(self) -> Optional[ClaudeSDKClient]:
        """Direct access to the underlying SDK client.

        TRANSITIONAL — will be removed once streaming loops use
        adapter.receive_events() instead of client.receive_response().
        """
        return self._client

    @property
    def connect_options(self) -> Optional[ClaudeAgentOptions]:
        """The SDK options used at connect() time.

        TRANSITIONAL — used by session_pool.requires_reconnect() until
        that logic is fully moved into the adapter.
        """
        return self._connect_options

    # ─── Connection Lifecycle ─────────────────────────────────────────

    async def connect(self, options: SessionOptions) -> None:
        """Connect to the Claude SDK subprocess."""
        sdk_options = self._to_sdk_options(options)
        self._client = ClaudeSDKClient(options=sdk_options)
        await self._client.connect()
        self._connected = True
        self._connect_options = sdk_options

    async def connect_with_sdk_options(self, sdk_options: ClaudeAgentOptions) -> None:
        """Connect using pre-built SDK options.

        TRANSITIONAL — allows callers that still build ClaudeAgentOptions
        directly (e.g., build_options_from_profile) to use the adapter
        without converting to SessionOptions first.
        """
        self._client = ClaudeSDKClient(options=sdk_options)
        await self._client.connect()
        self._connected = True
        self._connect_options = sdk_options

    async def disconnect(
        self,
        grace_period: float = _SUBPROCESS_EXIT_GRACE_SECONDS,
        *,
        session_id: Optional[str] = None,
    ) -> None:
        """Disconnect, giving the subprocess time to flush JSONL writes.

        ``session_id`` (the app-level session id, matching the one attributed to
        the SDK reader task via ``set_current_session_id``) lets us flag this as
        an *expected* teardown before force-terminating: the SDK's still-live
        message-reader task logs "Fatal error in message reader" at ERROR when
        its subprocess is killed, and ``error_log_handler`` demotes that flagged
        record to WARNING instead of persisting expected-shutdown noise.
        """
        if not self._client:
            return

        client = self._client
        handle = SdkProcessHandle(client)

        # Wait for natural subprocess exit before forcing termination
        if handle.is_alive():
            try:
                await handle.wait(timeout=grace_period)
                logger.debug(f"[Adapter] Subprocess exited naturally (rc={handle.returncode})")
            except asyncio.TimeoutError:
                # This teardown is intentional; the terminate below will provoke
                # the SDK reader-task fatal. Flag it as expected shutdown BEFORE
                # anything can kill the subprocess so error_log_handler (which
                # runs on a different task/thread) can demote the fatal.
                mark_expected_shutdown(session_id)
                logger.warning(f"[Adapter] Subprocess didn't exit in {grace_period}s, terminating")
                # Close the SDK client/reader FIRST: this sets Query._closed so
                # the reader exits via its clean `if self._closed: break` path and
                # its read task is cancelled (CancelledError, not ProcessError),
                # shrinking the terminate->fatal race window before we force-kill.
                try:
                    await client.disconnect()
                except RuntimeError as e:
                    if "cancel scope" not in str(e):
                        raise
                    # anyio cancel scope issue from asyncio.shield — fall through
                    # to the manual terminate below.
                # Safety net: force-terminate if the SDK close left it alive.
                if handle.is_alive():
                    handle.terminate()
                    try:
                        await handle.wait(timeout=2.0)
                    except asyncio.TimeoutError:
                        handle.kill()
                        logger.warning("[Adapter] Sent SIGKILL to subprocess")

        # SDK disconnect (no-op if already closed above / process already exited)
        try:
            await client.disconnect()
        except RuntimeError as e:
            if "cancel scope" in str(e):
                # anyio cancel scope issue from asyncio.shield — fall back
                SdkProcessHandle(client).terminate()
            else:
                raise
        finally:
            self._connected = False

    def is_alive(self) -> bool:
        """Check if the SDK subprocess is still running."""
        if not self._client:
            return False
        return SdkProcessHandle(self._client).is_alive()

    def is_connected(self) -> bool:
        """Whether connect() has been called and we haven't disconnected."""
        return self._connected and self._client is not None

    # ─── Query Execution (transitional) ───────────────────────────────
    # Phase 1: These are thin wrappers. The streaming loops still call
    # adapter.sdk_client.receive_response() directly.

    async def send_query(self, prompt: str) -> None:
        if not self._client:
            raise RuntimeError("Adapter not connected")
        await self._client.query(prompt)

    async def receive_events(self) -> AsyncIterator[ProviderEvent]:
        """Yield provider events for the current query.

        STUB for Phase 1 — not yet implemented. The streaming loops
        currently call adapter.sdk_client.receive_response() directly.
        Will be implemented in Phase 2 to complete SDK isolation.
        """
        raise NotImplementedError(
            "receive_events() not yet implemented. "
            "Use adapter.sdk_client.receive_response() during Phase 1."
        )
        # Make this a generator so the return type is correct
        yield  # pragma: no cover

    async def interrupt(self) -> None:
        if self._client:
            await self._client.interrupt()

    # ─── Interactive Responses ────────────────────────────────────────

    async def respond_to_permission(
        self,
        tool_use_id: str,
        allow: bool,
        *,
        remember: bool = False,
    ) -> None:
        # Permission responses are handled via the can_use_tool callback,
        # not through the adapter directly. This is a placeholder for Phase 2.
        pass

    async def respond_to_user_question(self, answer: str) -> None:
        # User question responses are handled via the user_question_handler,
        # not through the adapter directly. This is a placeholder for Phase 2.
        pass

    # ─── Runtime Configuration ────────────────────────────────────────

    async def set_model(self, model: str) -> None:
        if not self._client:
            raise RuntimeError("Adapter not connected")
        await self._client.set_model(model)

    async def set_permission_mode(self, mode: str) -> None:
        if not self._client:
            raise RuntimeError("Adapter not connected")
        await self._client.set_permission_mode(mode)

    async def get_context_usage(self) -> Optional[Dict[str, Any]]:
        """Get context window usage by category (SDK 0.1.52+).

        Returns a dict with context usage breakdown, or None if the
        client is not connected or the method is not available.
        """
        if not self._client:
            return None
        get_usage = getattr(self._client, 'get_context_usage', None)
        if get_usage is None:
            return None
        try:
            return await get_usage()
        except Exception as e:
            logger.warning(f"[Adapter] get_context_usage() failed: {e}")
            return None

    # ─── Session Identity ─────────────────────────────────────────────

    @property
    def sdk_session_id(self) -> Optional[str]:
        return self._sdk_session_id

    @sdk_session_id.setter
    def sdk_session_id(self, value: Optional[str]) -> None:
        self._sdk_session_id = value

    # ─── Options Comparison ───────────────────────────────────────────

    def requires_reconnect(self, new_options: SessionOptions) -> bool:
        """Check if changing to new_options requires a fresh connection."""
        if self._connect_options is None:
            return True

        old = self._connect_options

        # These are immutable per-subprocess (CLI args at spawn time)
        if old.system_prompt != (new_options.system_prompt or old.system_prompt):
            return True
        if old.cwd != (new_options.cwd or old.cwd):
            return True
        if not mcp_servers_equivalent(old.mcp_servers, new_options.mcp_servers):
            return True
        if (old.plugins or []) != (new_options.plugins or []):
            return True
        if (old.tools or []) != (new_options.tools or []):
            return True

        # Lazy import at call time is cycle-safe: session_pool imports this
        # module at load, so a module-level import would cycle.
        from app.core.session_pool import provider_env_requires_reconnect
        if provider_env_requires_reconnect(old.env, new_options.env):
            return True

        return False

    def requires_reconnect_sdk(self, new_sdk_options: ClaudeAgentOptions) -> bool:
        """Check reconnect using raw SDK options.

        TRANSITIONAL — used while callers still build ClaudeAgentOptions directly.
        """
        old = self._connect_options
        if old is None:
            return True

        if old.system_prompt != new_sdk_options.system_prompt:
            return True
        if old.cwd != new_sdk_options.cwd:
            return True
        if not mcp_servers_equivalent(old.mcp_servers, new_sdk_options.mcp_servers):
            return True
        if (old.plugins or []) != (new_sdk_options.plugins or []):
            return True
        if (old.tools or []) != (new_sdk_options.tools or []):
            return True

        # Lazy import at call time is cycle-safe: session_pool imports this
        # module at load, so a module-level import would cycle.
        from app.core.session_pool import provider_env_requires_reconnect
        if provider_env_requires_reconnect(old.env, new_sdk_options.env):
            return True

        return False

    # ─── Internal ─────────────────────────────────────────────────────

    @staticmethod
    def _to_sdk_options(options: SessionOptions) -> ClaudeAgentOptions:
        """Convert provider-agnostic SessionOptions to Claude SDK options.

        This is the translation layer — SessionOptions fields map to
        ClaudeAgentOptions fields. New SDK fields should be added here.
        """
        kwargs: Dict[str, Any] = {
            "model": options.model,
        }

        if options.system_prompt is not None:
            kwargs["system_prompt"] = options.system_prompt
        if options.cwd is not None:
            kwargs["cwd"] = options.cwd
        if options.resume_session_id is not None:
            kwargs["resume"] = options.resume_session_id
        if options.tools is not None:
            kwargs["tools"] = options.tools
        if options.allowed_tools is not None:
            kwargs["allowed_tools"] = options.allowed_tools
        if options.disallowed_tools is not None:
            kwargs["disallowed_tools"] = options.disallowed_tools
        if options.mcp_servers is not None:
            kwargs["mcp_servers"] = options.mcp_servers
        if options.plugins is not None:
            kwargs["plugins"] = options.plugins
        if options.thinking is not None:
            kwargs["thinking"] = options.thinking
        if options.effort is not None:
            kwargs["effort"] = options.effort
        if options.permission_mode is not None:
            kwargs["permission_mode"] = options.permission_mode
        if options.include_partial_messages:
            kwargs["include_partial_messages"] = True
        if options.hooks is not None:
            kwargs["hooks"] = options.hooks
        if options.agents is not None:
            kwargs["agents"] = options.agents
        if options.env is not None:
            kwargs["env"] = options.env
        if options.extra_args is not None:
            kwargs["extra_args"] = options.extra_args
        if options.setting_sources is not None:
            kwargs["setting_sources"] = options.setting_sources
        if options.betas is not None:
            kwargs["betas"] = options.betas
        if options.enable_file_checkpointing is not None:
            kwargs["enable_file_checkpointing"] = options.enable_file_checkpointing
        if options.stderr is not None:
            kwargs["stderr"] = options.stderr
        if options.max_buffer_size is not None:
            kwargs["max_buffer_size"] = options.max_buffer_size
        if options.user is not None:
            kwargs["user"] = options.user
        if options.session_id is not None:
            kwargs["session_id"] = options.session_id
        if options.task_budget is not None:
            kwargs["task_budget"] = options.task_budget
        if options.session_store is not None:
            kwargs["session_store"] = options.session_store
        if options.session_store_flush is not None:
            kwargs["session_store_flush"] = options.session_store_flush
        if options.include_hook_events:
            kwargs["include_hook_events"] = True
        if options.strict_mcp_config:
            kwargs["strict_mcp_config"] = True

        # Forward any extra options for forward-compatibility
        kwargs.update(options.extra)

        return ClaudeAgentOptions(**kwargs)
