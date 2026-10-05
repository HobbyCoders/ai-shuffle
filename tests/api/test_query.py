"""
Tests for Query API endpoints.

This module tests the query API routes in app/api/query.py including:
- One-shot queries (POST /api/v1/query)
- Streaming one-shot queries (POST /api/v1/query/stream)
- Conversation endpoints (POST /api/v1/conversation)
- Streaming conversation (POST /api/v1/conversation/stream)
- Background conversation start (POST /api/v1/conversation/start)
- Session interrupt (POST /api/v1/session/{session_id}/interrupt)
- Active streaming sessions (GET /api/v1/streaming/active)
"""

import json
import pytest
from unittest.mock import AsyncMock, patch


# =============================================================================
# Test Module Imports
# =============================================================================

class TestQueryModuleImports:
    """Verify query module can be imported correctly."""

    def test_query_module_imports(self):
        """Query module should import without errors."""
        from app.api import query
        assert query is not None

    def test_query_router_exists(self):
        """Query router should exist."""
        from app.api.query import router
        assert router is not None

    def test_query_router_has_correct_prefix(self):
        """Query router should have correct prefix."""
        from app.api.query import router
        assert router.prefix == "/api/v1"

    def test_require_claude_auth_function_exists(self):
        """require_claude_auth dependency function should exist."""
        from app.api.query import require_claude_auth
        assert callable(require_claude_auth)


# =============================================================================
# Test require_claude_auth Dependency
# =============================================================================

class TestRequireClaudeAuth:
    """Tests for the require_claude_auth dependency."""

    async def test_require_claude_auth_passes_when_licensed(self):
        """Only the license is checked here, not the Claude credential."""
        from app.api.query import require_claude_auth
        from app.core import credential_gate
        from app.core import license as license_mod

        with patch.object(license_mod, "license_allows_agent_runs", return_value=(True, None)), \
                patch.object(credential_gate, "claude_credential_ready",
                             return_value=(False, credential_gate.NOT_SIGNED_IN_MESSAGE)):
            assert await require_claude_auth() is None

    async def test_require_claude_auth_raises_without_a_license(self):
        """Should raise 503 with the license reason when runs are not licensed."""
        from fastapi import HTTPException
        from app.api.query import require_claude_auth
        from app.core import license as license_mod

        with patch.object(license_mod, "license_allows_agent_runs",
                          return_value=(False, "This worker has no license.")):
            with pytest.raises(HTTPException) as exc_info:
                await require_claude_auth()

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "This worker has no license."


# =============================================================================
# The Claude credential is checked per leg, not for every run
# =============================================================================

@pytest.mark.real_agent_gate
class TestPerLegCredentialGate:
    """Subscription mode with the Claude Code CLI signed out: a run on a
    ChatGPT provider row starts, a run on a Claude model is refused with the
    setup message, and a missing license still refuses everything."""

    @pytest.fixture
    def signed_out(self, mock_db):
        from app.core import claude_auth_mode, claude_cli_status
        from app.core import license as license_mod
        from app.db import database

        claude_auth_mode.set_mode("subscription")
        database.create_model_provider(
            "chatgpt", "ChatGPT", "openai", "https://example.invalid/v1",
            models=[{"id": "gpt-5.6-luna"}, {"id": "gpt-5.6-sol"}],
            default_model="gpt-5.6-luna",
        )
        database.create_profile("gpt-agent", "GPT Agent", None,
                                {"provider": "chatgpt", "model": "gpt-5.6-luna"})
        database.create_profile("claude-agent", "Claude Agent", None, {"model": "opus"})
        not_signed_in = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=False)
        with patch.object(claude_cli_status, "get_status", return_value=not_signed_in), \
                patch.object(license_mod, "license_allows_agent_runs", return_value=(True, None)) as lic, \
                patch("app.api.query.start_background_query",
                      new=AsyncMock(return_value={"session_id": "s", "status": "started"})) as start:
            yield lic, start

    @staticmethod
    async def _start(profile, overrides=None):
        from app.api.query import require_claude_auth, start_conversation
        from app.core.models import ConversationRequest, QueryOverrides

        await require_claude_auth()
        request = ConversationRequest(
            prompt="Hi", profile=profile,
            overrides=QueryOverrides(**overrides) if overrides else None,
        )
        return await start_conversation(request, {"id": "u1", "profile_ids": None})

    async def test_a_provider_row_agent_starts_without_a_claude_sign_in(self, signed_out):
        _, start = signed_out
        result = await self._start("gpt-agent")
        assert result["status"] == "started"
        start.assert_awaited_once()

    async def test_a_provider_model_pick_starts_without_a_claude_sign_in(self, signed_out):
        _, start = signed_out
        result = await self._start("claude-agent", {"model": "prov:chatgpt:gpt-5.6-sol"})
        assert result["status"] == "started"
        start.assert_awaited_once()

    async def test_a_claude_agent_is_refused_with_the_setup_message(self, signed_out):
        from fastapi import HTTPException
        from app.core import credential_gate

        _, start = signed_out
        with pytest.raises(HTTPException) as exc_info:
            await self._start("claude-agent")
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == credential_gate.NOT_SIGNED_IN_MESSAGE
        assert exc_info.value.headers == {"X-Error-Code": credential_gate.SETUP_CODE}
        start.assert_not_awaited()

    async def test_a_claude_pick_on_a_provider_agent_is_refused(self, signed_out):
        from fastapi import HTTPException

        _, start = signed_out
        with pytest.raises(HTTPException) as exc_info:
            await self._start("gpt-agent", {"model": "sonnet"})
        assert exc_info.value.status_code == 503
        start.assert_not_awaited()

    async def test_a_missing_license_still_refuses(self, signed_out):
        from fastapi import HTTPException

        lic, start = signed_out
        lic.return_value = (False, "This worker has no license.")
        with pytest.raises(HTTPException) as exc_info:
            await self._start("gpt-agent")
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "This worker has no license."
        start.assert_not_awaited()

    @pytest.mark.parametrize("endpoint,model", [
        ("one_shot_query", "QueryRequest"),
        ("stream_one_shot_query", "QueryRequest"),
        ("conversation", "ConversationRequest"),
        ("stream_conversation", "ConversationRequest"),
        ("start_conversation", "ConversationRequest"),
    ])
    async def test_every_route_refuses_a_claude_run_before_the_engine(
        self, signed_out, endpoint, model,
    ):
        from fastapi import HTTPException
        from app.api import query
        from app.core import models

        request = getattr(models, model)(prompt="Hi", profile="claude-agent")
        with patch("app.core.query_engine.execute_query", new=AsyncMock()) as execute, \
                patch("app.api.query._stream_query_with_failover") as stream, \
                patch("app.core.turn_manager.submit_async", new=AsyncMock()) as submit:
            with pytest.raises(HTTPException) as exc_info:
                await getattr(query, endpoint)(request, {"id": "u1", "profile_ids": None})
        assert exc_info.value.status_code == 503
        execute.assert_not_awaited()
        stream.assert_not_called()
        submit.assert_not_awaited()


# =============================================================================
# Test One-Shot Query Endpoint
# =============================================================================

class TestOneShotQuery:
    """Tests for POST /api/v1/query endpoint."""

    @pytest.fixture
    def mock_dependencies(self):
        """Set up common mocks for query tests."""
        with patch("app.api.query.require_api_key") as mock_api_key, \
             patch("app.api.query.require_claude_auth") as mock_claude, \
             patch("app.core.query_engine.execute_query") as mock_execute:
            mock_api_key.return_value = {
                "id": "test-api-user-id",
                "username": "testuser",
                "profile_ids": ["test-profile"],
                "project_id": "test-project"
            }
            mock_claude.return_value = None
            mock_execute.return_value = {
                "response": "Test response",
                "session_id": "test-session-123",
                "metadata": {
                    "model": "claude-sonnet-4-20250514",
                    "duration_ms": 1000,
                    "total_cost_usd": 0.01,
                    "tokens_in": 100,
                    "tokens_out": 50
                }
            }
            yield {
                "api_key": mock_api_key,
                "claude": mock_claude,
                "execute": mock_execute
            }

    @pytest.mark.asyncio
    async def test_one_shot_query_success(self, mock_dependencies):
        """Should execute a one-shot query successfully."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(
            prompt="Hello, Claude!",
            profile="test-profile",
            project="test-project"
        )
        api_user = mock_dependencies["api_key"].return_value

        result = await one_shot_query(request, api_user)

        assert result["response"] == "Test response"
        assert result["session_id"] == "test-session-123"
        mock_dependencies["execute"].assert_called_once()

    @pytest.mark.asyncio
    async def test_one_shot_query_uses_api_user_profile(self, mock_dependencies):
        """An omitted profile runs the API user's only assigned agent."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_ids": ["user-configured-profile"]}

        await one_shot_query(request, api_user)

        call_kwargs = mock_dependencies["execute"].call_args.kwargs
        assert call_kwargs["profile_id"] == "user-configured-profile"

    @pytest.mark.asyncio
    async def test_one_shot_query_falls_back_to_request_profile(self, mock_dependencies):
        """Should fall back to request profile if API user has none."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(
            prompt="Hello!",
            profile="request-profile"
        )
        api_user = {"id": "user1", "profile_ids": None}

        await one_shot_query(request, api_user)

        call_kwargs = mock_dependencies["execute"].call_args.kwargs
        assert call_kwargs["profile_id"] == "request-profile"

    @pytest.mark.asyncio
    async def test_one_shot_query_requires_profile(self, mock_dependencies):
        """Should return 400 if no profile is configured or provided."""
        from fastapi import HTTPException
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        # Create request - the profile comes from the request because the
        # API user has none
        request = QueryRequest(
            prompt="Hello!",
            profile="claude-code"  # supplied explicitly; no longer a model default
        )
        # API user has no profile and request profile should be ignored
        # We need to ensure neither api_user nor request has a valid profile
        api_user = {"id": "user1", "profile_ids": None}

        # Patch the request's profile attribute to None after creation
        request.profile = None

        with pytest.raises(HTTPException) as exc_info:
            await one_shot_query(request, api_user)

        assert exc_info.value.status_code == 400
        assert "No profile configured" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_query_omitted_profile_returns_400(self, mock_dependencies):
        """Should return 400, not 500, when profile is omitted entirely and
        the API user has none configured (regression: the "claude-code"
        sentinel default used to make request.profile always truthy)."""
        from fastapi import HTTPException
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_ids": None}

        with pytest.raises(HTTPException) as exc_info:
            await one_shot_query(request, api_user)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_one_shot_query_with_overrides(self, mock_dependencies):
        """Should pass overrides to execute_query."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest, QueryOverrides

        request = QueryRequest(
            prompt="Hello!",
            profile="test-profile",
            overrides=QueryOverrides(
                model="opus",
                permission_mode="bypassPermissions",
                context_window=200_000,
            )
        )
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        await one_shot_query(request, api_user)

        call_kwargs = mock_dependencies["execute"].call_args.kwargs
        assert call_kwargs["overrides"]["model"] == "opus"
        assert call_kwargs["overrides"]["permission_mode"] == "bypassPermissions"
        assert call_kwargs["overrides"]["context_window"] == 200_000

    @pytest.mark.asyncio
    async def test_one_shot_query_value_error(self, mock_dependencies):
        """Should return 400 for ValueError from execute_query."""
        from fastapi import HTTPException
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        mock_dependencies["execute"].side_effect = ValueError("Profile not found")

        request = QueryRequest(prompt="Hello!", profile="test-profile")
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        with pytest.raises(HTTPException) as exc_info:
            await one_shot_query(request, api_user)

        assert exc_info.value.status_code == 400
        assert "Profile not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_one_shot_query_general_error(self, mock_dependencies):
        """Should return 500 for general exceptions."""
        from fastapi import HTTPException
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        mock_dependencies["execute"].side_effect = RuntimeError("Unexpected error")

        request = QueryRequest(prompt="Hello!", profile="test-profile")
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        with pytest.raises(HTTPException) as exc_info:
            await one_shot_query(request, api_user)

        assert exc_info.value.status_code == 500
        assert "Query execution failed" in str(exc_info.value.detail)


# =============================================================================
# Test Streaming One-Shot Query Endpoint
# =============================================================================

class TestStreamOneShotQuery:
    """Tests for POST /api/v1/query/stream endpoint."""

    @pytest.fixture
    def mock_stream_dependencies(self):
        """Set up mocks for streaming query tests."""
        with patch("app.api.query.require_api_key") as mock_api_key, \
             patch("app.api.query.require_claude_auth") as mock_claude, \
             patch("app.api.query.stream_query") as mock_stream:
            mock_api_key.return_value = {
                "id": "test-api-user-id",
                "profile_ids": ["test-profile"],
                "project_id": None
            }
            mock_claude.return_value = None

            # Mock async generator
            async def mock_stream_gen(*args, **kwargs):
                yield {"type": "text", "content": "Hello"}
                yield {"type": "done", "session_id": "test-session"}

            mock_stream.return_value = mock_stream_gen()
            yield {
                "api_key": mock_api_key,
                "claude": mock_claude,
                "stream": mock_stream
            }

    @pytest.mark.asyncio
    async def test_stream_query_returns_streaming_response(self, mock_stream_dependencies):
        """Should return StreamingResponse with correct headers."""
        from fastapi.responses import StreamingResponse
        from app.api.query import stream_one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(prompt="Hello!", profile="test-profile")
        api_user = mock_stream_dependencies["api_key"].return_value

        result = await stream_one_shot_query(request, api_user)

        assert isinstance(result, StreamingResponse)
        assert result.media_type == "text/event-stream"
        assert result.headers.get("Cache-Control") == "no-cache"
        assert result.headers.get("Connection") == "keep-alive"
        assert result.headers.get("X-Accel-Buffering") == "no"

    @pytest.mark.asyncio
    async def test_stream_query_requires_profile(self, mock_stream_dependencies):
        """Should return 400 if no profile configured."""
        from fastapi import HTTPException
        from app.api.query import stream_one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_ids": None}

        with pytest.raises(HTTPException) as exc_info:
            await stream_one_shot_query(request, api_user)

        assert exc_info.value.status_code == 400


# =============================================================================
# Test Conversation Endpoint
# =============================================================================

class TestConversation:
    """Tests for POST /api/v1/conversation endpoint."""

    @pytest.fixture
    def mock_conv_dependencies(self, mock_db):
        """Mocks for conversation tests.

        The endpoint no longer calls the query engine: it submits a
        ``source=api`` turn and reads the settled outcome out of the durable
        event log, so the seams to stub are ``turn_manager`` (the off-loop
        ``submit_async`` twin) and ``turn_outcome``.
        """
        with patch("app.api.query.require_api_key") as mock_api_key, \
             patch("app.api.query.require_claude_auth") as mock_claude, \
             patch("app.core.turn_manager.submit_async",
                   new=AsyncMock(return_value="turn-1")) as mock_submit, \
             patch("app.core.turn_manager.wait_session_idle",
                   new=AsyncMock(return_value=True)) as mock_wait, \
             patch("app.core.turn_outcome.final_turn_outcome",
                   return_value={"usage": {"model": "sonnet"}, "error": None,
                                 "interrupted": False}), \
             patch("app.core.turn_outcome.extract_final_assistant_text",
                   return_value="Continued response"):
            mock_api_key.return_value = {
                "id": None,
                "profile_ids": ["default"]
            }
            mock_claude.return_value = None
            yield {
                "api_key": mock_api_key,
                "claude": mock_claude,
                "submit": mock_submit,
                "wait": mock_wait,
            }

    @pytest.mark.asyncio
    async def test_conversation_new_session(self, mock_conv_dependencies):
        """Should create the session up front, then submit an ``api`` turn."""
        from app.api.query import conversation
        from app.core.models import ConversationRequest
        from app.db import database

        request = ConversationRequest(
            prompt="Start a conversation",
            profile="default"
        )
        api_user = mock_conv_dependencies["api_key"].return_value

        result = await conversation(request, api_user)

        assert result["response"] == "Continued response"
        submit = mock_conv_dependencies["submit"]
        submit.assert_called_once()
        assert submit.call_args.args[1] == "api"
        assert submit.call_args.args[0] == result["session_id"]
        assert database.get_session(result["session_id"]) is not None

    @pytest.mark.asyncio
    async def test_conversation_session_db_calls_are_offloaded(
        self, mock_conv_dependencies
    ):
        """B2: the session lookup/create must not run on the event loop.

        ``database.get_session`` / ``create_session`` are synchronous psycopg2
        calls; run inline they stall every other request while this endpoint
        sets up.
        """
        from app.api.query import conversation
        from app.core.models import ConversationRequest
        from app.db import async_database, database

        offloaded = []
        real_run_sync = async_database.run_sync

        async def tracking_run_sync(fn, *args, **kwargs):
            offloaded.append(getattr(fn, "__name__", str(fn)))
            return await real_run_sync(fn, *args, **kwargs)

        request = ConversationRequest(prompt="Offload me", profile="default")
        api_user = mock_conv_dependencies["api_key"].return_value

        with patch.object(async_database, "run_sync", tracking_run_sync):
            result = await conversation(request, api_user)

        assert "create_session" in offloaded
        assert database.get_session(result["session_id"]) is not None

    @pytest.mark.asyncio
    async def test_conversation_continue_session(self, mock_conv_dependencies):
        """Should continue existing session when session_id provided."""
        from app.api.query import conversation
        from app.core.models import ConversationRequest
        from app.db import database

        database.create_api_user("user1", "Key", "hash-user1")
        database.create_session(session_id="existing-session-123",
                                profile_id="default", api_user_id="user1")
        request = ConversationRequest(
            prompt="Continue conversation",
            session_id="existing-session-123"
        )
        api_user = {"id": "user1", "profile_ids": ["default"]}

        await conversation(request, api_user)

        submit = mock_conv_dependencies["submit"]
        assert submit.call_args.args[0] == "existing-session-123"

    @pytest.mark.asyncio
    async def test_conversation_unknown_session(self, mock_conv_dependencies):
        """Should return 400 for a session id with no row (was a ValueError
        out of the engine; the endpoint now refuses it before submitting)."""
        from fastapi import HTTPException
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Hello!",
            session_id="invalid-session"
        )
        api_user = {"id": None, "profile_ids": ["default"]}

        with pytest.raises(HTTPException) as exc_info:
            await conversation(request, api_user)

        assert exc_info.value.status_code == 400
        mock_conv_dependencies["submit"].assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_refuses_an_archived_session(self, mock_conv_dependencies):
        """An archived chat is read-only until restored (#1822): 409, no turn."""
        from fastapi import HTTPException
        from app.api.query import conversation
        from app.core.models import ConversationRequest
        from app.db import database

        database.create_api_user("user1", "Key", "hash-user1")
        database.create_session(session_id="archived-session", profile_id="default",
                                api_user_id="user1")
        database.update_session("archived-session", status="archived")
        request = ConversationRequest(prompt="Wake up", session_id="archived-session")

        with pytest.raises(HTTPException) as exc_info:
            await conversation(request, {"id": "user1", "profile_ids": ["default"]})

        assert exc_info.value.status_code == 409
        assert "archived" in exc_info.value.detail
        mock_conv_dependencies["submit"].assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_omitted_profile_returns_400(self, mock_conv_dependencies):
        """Should return 400, not 500, when profile is omitted entirely and
        the API user has none configured (regression: the "claude-code"
        sentinel default used to make request.profile always truthy)."""
        from fastapi import HTTPException
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_ids": None}

        with pytest.raises(HTTPException) as exc_info:
            await conversation(request, api_user)

        assert exc_info.value.status_code == 400
        mock_conv_dependencies["submit"].assert_not_called()

    @pytest.mark.asyncio
    async def test_conversation_general_error(self, mock_conv_dependencies):
        """Should return 500 for general exceptions."""
        from fastapi import HTTPException
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        mock_conv_dependencies["submit"].side_effect = RuntimeError("Connection failed")

        request = ConversationRequest(prompt="Hello!")
        api_user = {"id": None, "profile_ids": ["default"]}

        with pytest.raises(HTTPException) as exc_info:
            await conversation(request, api_user)

        assert exc_info.value.status_code == 500
        assert "Conversation failed" in str(exc_info.value.detail)


# =============================================================================
# Test Streaming Conversation Endpoint
# =============================================================================

class TestStreamConversation:
    """Tests for POST /api/v1/conversation/stream endpoint."""

    @pytest.fixture
    def mock_stream_conv_dependencies(self):
        """Set up mocks for streaming conversation tests."""
        with patch("app.api.query.require_api_key") as mock_api_key, \
             patch("app.api.query.require_claude_auth") as mock_claude, \
             patch("app.api.query.stream_query") as mock_stream:
            mock_api_key.return_value = {
                "id": "test-api-user-id",
                "profile_ids": ["test-profile"]
            }
            mock_claude.return_value = None

            async def mock_stream_gen(*args, **kwargs):
                yield {"type": "text", "content": "Streaming..."}
                yield {"type": "done", "session_id": "session-123"}

            mock_stream.return_value = mock_stream_gen()
            yield {
                "api_key": mock_api_key,
                "claude": mock_claude,
                "stream": mock_stream
            }

    @pytest.mark.asyncio
    async def test_stream_conversation_returns_streaming_response(self, mock_stream_conv_dependencies):
        """Should return StreamingResponse for streaming conversation."""
        from fastapi.responses import StreamingResponse
        from app.api.query import stream_conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Stream a conversation",
            device_id="device-123"
        )
        api_user = mock_stream_conv_dependencies["api_key"].return_value

        result = await stream_conversation(request, api_user)

        assert isinstance(result, StreamingResponse)
        assert result.media_type == "text/event-stream"

    @pytest.mark.asyncio
    async def test_stream_conversation_requires_profile(self, mock_stream_conv_dependencies):
        """Should require profile to be configured."""
        from fastapi import HTTPException
        from app.api.query import stream_conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(prompt="Hello!", profile=None)
        api_user = {"id": "user1", "profile_ids": None}

        with pytest.raises(HTTPException) as exc_info:
            await stream_conversation(request, api_user)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_stream_conversation_passes_device_id(self, mock_stream_conv_dependencies):
        """Should pass device_id to stream_query for cross-device sync."""
        from app.api.query import stream_conversation
        from app.core.models import ConversationRequest

        # Create new async generator for this test
        async def mock_stream_gen(*args, **kwargs):
            yield {"type": "done", "session_id": "test"}

        mock_stream_conv_dependencies["stream"].return_value = mock_stream_gen()

        request = ConversationRequest(
            prompt="Test",
            device_id="my-device-id"
        )
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        # Execute the endpoint
        result = await stream_conversation(request, api_user)

        # We can't easily check the generator was called with device_id
        # since the call happens inside event_generator, but we can verify
        # the response type
        assert result.media_type == "text/event-stream"


# =============================================================================
# Test Start Background Conversation Endpoint
# =============================================================================

class TestStartConversation:
    """Tests for POST /api/v1/conversation/start endpoint."""

    @pytest.fixture
    def mock_start_dependencies(self):
        """Set up mocks for start conversation tests."""
        with patch("app.api.query.require_api_key") as mock_api_key, \
             patch("app.api.query.require_claude_auth") as mock_claude, \
             patch("app.api.query.start_background_query") as mock_start:
            mock_api_key.return_value = {
                "id": "test-api-user-id",
                "profile_ids": ["test-profile"]
            }
            mock_claude.return_value = None
            mock_start.return_value = {
                "session_id": "new-session-123",
                "status": "started",
                "message_id": "msg-123"
            }
            yield {
                "api_key": mock_api_key,
                "claude": mock_claude,
                "start": mock_start
            }

    @pytest.mark.asyncio
    async def test_start_conversation_success(self, mock_start_dependencies):
        """Should start a background conversation."""
        from app.api.query import start_conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Start background query",
            device_id="device-123"
        )
        api_user = mock_start_dependencies["api_key"].return_value

        result = await start_conversation(request, api_user)

        assert result["session_id"] == "new-session-123"
        assert result["status"] == "started"
        mock_start_dependencies["start"].assert_called_once()

    @pytest.mark.asyncio
    async def test_start_conversation_with_existing_session(self, mock_start_dependencies):
        """Should continue existing session in background."""
        from app.api.query import start_conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Continue in background",
            session_id="existing-session"
        )
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        await start_conversation(request, api_user)

        call_kwargs = mock_start_dependencies["start"].call_args.kwargs
        assert call_kwargs["session_id"] == "existing-session"

    @pytest.mark.asyncio
    async def test_start_conversation_requires_profile(self, mock_start_dependencies):
        """Should require profile to be configured."""
        from fastapi import HTTPException
        from app.api.query import start_conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(prompt="Hello!", profile=None)
        api_user = {"id": "user1", "profile_ids": None}

        with pytest.raises(HTTPException) as exc_info:
            await start_conversation(request, api_user)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_start_conversation_value_error(self, mock_start_dependencies):
        """Should return 400 for ValueError."""
        from fastapi import HTTPException
        from app.api.query import start_conversation
        from app.core.models import ConversationRequest

        mock_start_dependencies["start"].side_effect = ValueError("Profile not found")

        request = ConversationRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        with pytest.raises(HTTPException) as exc_info:
            await start_conversation(request, api_user)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_start_conversation_general_error(self, mock_start_dependencies):
        """Should return 500 for general exceptions."""
        from fastapi import HTTPException
        from app.api.query import start_conversation
        from app.core.models import ConversationRequest

        mock_start_dependencies["start"].side_effect = RuntimeError("Background task failed")

        request = ConversationRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_ids": ["test-profile"]}

        with pytest.raises(HTTPException) as exc_info:
            await start_conversation(request, api_user)

        assert exc_info.value.status_code == 500
        assert "Failed to start conversation" in str(exc_info.value.detail)


# =============================================================================
# Test Interrupt Session Endpoint
# =============================================================================

def _owner_request():
    """An owner request (no API user): no per-session filtering applies."""
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(api_user=None))


class TestInterruptSession:
    """Tests for POST /api/v1/session/{session_id}/interrupt endpoint."""

    @pytest.fixture
    def mock_interrupt_dependencies(self):
        """Set up mocks for interrupt tests."""
        with patch("app.api.query.require_auth") as mock_auth, \
             patch("app.api.query.interrupt_session") as mock_interrupt:
            mock_auth.return_value = "test-token"
            mock_interrupt.return_value = True
            yield {
                "auth": mock_auth,
                "interrupt": mock_interrupt
            }

    @pytest.mark.asyncio
    async def test_interrupt_session_success(self, mock_interrupt_dependencies):
        """Should interrupt active session successfully."""
        from app.api.query import interrupt

        result = await interrupt(_owner_request(), "session-123", "test-token")

        assert result["status"] == "interrupted"
        assert result["session_id"] == "session-123"
        mock_interrupt_dependencies["interrupt"].assert_called_once_with("session-123")

    @pytest.mark.asyncio
    async def test_interrupt_session_not_found(self, mock_interrupt_dependencies):
        """Should return 404 when session not found or already completed."""
        from fastapi import HTTPException
        from app.api.query import interrupt

        mock_interrupt_dependencies["interrupt"].return_value = False

        with pytest.raises(HTTPException) as exc_info:
            await interrupt(_owner_request(), "nonexistent-session", "test-token")

        assert exc_info.value.status_code == 404
        assert "No active session found" in str(exc_info.value.detail)


# =============================================================================
# Test List Active Sessions Endpoint
# =============================================================================

class TestListActiveSessions:
    """Tests for GET /api/v1/streaming/active endpoint."""

    @pytest.fixture
    def mock_list_dependencies(self):
        """Set up mocks for list active sessions tests."""
        # The get_streaming_sessions is imported inside the function from query_engine
        with patch("app.api.query.require_auth") as mock_auth, \
             patch("app.core.query_engine.get_streaming_sessions") as mock_get:
            mock_auth.return_value = "test-token"
            mock_get.return_value = ["session-1", "session-2", "session-3"]
            yield {
                "auth": mock_auth,
                "get_sessions": mock_get
            }

    @pytest.mark.asyncio
    async def test_list_active_sessions_success(self, mock_list_dependencies):
        """Should return list of active streaming sessions."""
        from app.api.query import list_active_sessions

        result = await list_active_sessions(_owner_request(), "test-token")

        assert "active_sessions" in result
        assert result["active_sessions"] == ["session-1", "session-2", "session-3"]

    @pytest.mark.asyncio
    async def test_list_active_sessions_empty(self, mock_list_dependencies):
        """Should return empty list when no active sessions."""
        from app.api.query import list_active_sessions

        mock_list_dependencies["get_sessions"].return_value = []

        result = await list_active_sessions(_owner_request(), "test-token")

        assert result["active_sessions"] == []


# =============================================================================
# Test SSE Event Generation
# =============================================================================

class TestSSEEventGeneration:
    """Tests for SSE event format in streaming endpoints."""

    @pytest.mark.asyncio
    async def test_sse_event_format_text(self):
        """SSE events should have correct format for text events."""
        # Test the format that would be generated
        event = {"type": "text", "content": "Hello"}
        event_type = event.get("type", "message")
        data = json.dumps(event)

        expected = f"event: {event_type}\ndata: {data}\n\n"
        assert "event: text" in expected
        assert '"content": "Hello"' in expected

    @pytest.mark.asyncio
    async def test_sse_event_format_tool_use(self):
        """SSE events should have correct format for tool_use events."""
        event = {"type": "tool_use", "name": "Read", "input": {"path": "/test"}}
        event_type = event.get("type", "message")
        data = json.dumps(event)

        sse_output = f"event: {event_type}\ndata: {data}\n\n"
        assert "event: tool_use" in sse_output
        assert '"name": "Read"' in sse_output

    @pytest.mark.asyncio
    async def test_sse_event_format_done(self):
        """SSE events should have correct format for done events."""
        event = {
            "type": "done",
            "session_id": "session-123",
            "metadata": {"duration_ms": 1000}
        }
        event_type = event.get("type", "message")
        data = json.dumps(event)

        sse_output = f"event: {event_type}\ndata: {data}\n\n"
        assert "event: done" in sse_output
        assert '"session_id": "session-123"' in sse_output


# =============================================================================
# Test Streaming Generator Error Handling
# =============================================================================

class TestStreamingGeneratorErrorHandling:
    """Tests for error handling in streaming generators."""

    @pytest.mark.asyncio
    async def test_stream_query_generator_with_error(self):
        """The event generator should handle errors from stream_query."""
        from app.api.query import stream_one_shot_query
        from app.core.models import QueryRequest

        async def error_stream_gen(*args, **kwargs):
            yield {"type": "text", "content": "Starting..."}
            raise RuntimeError("Stream connection lost")

        with patch("app.api.query.require_api_key") as mock_api, \
             patch("app.api.query.require_claude_auth"), \
             patch("app.api.query.stream_query", return_value=error_stream_gen()):

            mock_api.return_value = {"id": "user1", "profile_ids": ["test-profile"]}
            request = QueryRequest(prompt="Hello!")

            # Call the endpoint - it returns a StreamingResponse
            response = await stream_one_shot_query(request, {"id": "user1", "profile_ids": ["test"]})

            # Consume the generator to trigger the error handling
            chunks = []
            async for chunk in response.body_iterator:
                # Generator yields strings, not bytes
                if isinstance(chunk, bytes):
                    chunks.append(chunk.decode("utf-8"))
                else:
                    chunks.append(chunk)

            # The generator should have produced text and then an error event
            output = "".join(chunks)
            assert "text" in output or "error" in output

    @pytest.mark.asyncio
    async def test_stream_conversation_generator_with_error(self):
        """The conversation event generator should handle errors."""
        from app.api.query import stream_conversation
        from app.core.models import ConversationRequest

        async def error_stream_gen(*args, **kwargs):
            yield {"type": "text", "content": "Working..."}
            raise Exception("Connection timeout")

        with patch("app.api.query.require_api_key") as mock_api, \
             patch("app.api.query.require_claude_auth"), \
             patch("app.api.query.stream_query", return_value=error_stream_gen()):

            mock_api.return_value = {"id": "user1", "profile_ids": ["test-profile"]}
            request = ConversationRequest(prompt="Continue conversation")

            response = await stream_conversation(request, {"id": "user1", "profile_ids": ["test"]})

            # Consume the generator
            chunks = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunks.append(chunk.decode("utf-8"))
                else:
                    chunks.append(chunk)

            output = "".join(chunks)
            # Should contain either content or error handling
            assert len(output) > 0

    @pytest.mark.asyncio
    async def test_stream_query_formats_events_correctly(self):
        """Stream query should format SSE events correctly."""
        from app.api.query import stream_one_shot_query
        from app.core.models import QueryRequest

        async def mock_stream_gen(*args, **kwargs):
            yield {"type": "text", "content": "Hello"}
            yield {"type": "tool_use", "name": "Read", "input": {"path": "/test"}}
            yield {"type": "done", "session_id": "sess-123", "metadata": {}}

        with patch("app.api.query.require_api_key") as mock_api, \
             patch("app.api.query.require_claude_auth"), \
             patch("app.api.query.stream_query", return_value=mock_stream_gen()):

            mock_api.return_value = {"id": "user1", "profile_ids": ["test-profile"]}
            request = QueryRequest(prompt="Hello!")

            response = await stream_one_shot_query(request, {"id": "user1", "profile_ids": ["test"]})

            chunks = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunks.append(chunk.decode("utf-8"))
                else:
                    chunks.append(chunk)

            output = "".join(chunks)

            # Check SSE format
            assert "event: text" in output
            assert "event: tool_use" in output
            assert "event: done" in output
            assert "data:" in output


# =============================================================================
# Test API User Configuration Precedence
# =============================================================================

class TestApiUserConfigurationPrecedence:
    """Tests for profile/project configuration precedence."""

    @pytest.mark.asyncio
    async def test_api_user_project_takes_precedence(self):
        """API user's project_id should take precedence over request project."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        with patch("app.api.query.require_api_key"), \
             patch("app.api.query.require_claude_auth"), \
             patch("app.core.query_engine.execute_query") as mock_execute:

            mock_execute.return_value = {
                "response": "OK",
                "session_id": "sess",
                "metadata": {}
            }

            request = QueryRequest(
                prompt="Hello",
                profile="user-profile",
                project="request-project"
            )

            api_user = {
                "id": "user1",
                "profile_ids": ["user-profile"],
                "project_id": "user-project"
            }

            await one_shot_query(request, api_user)

            call_kwargs = mock_execute.call_args.kwargs
            # API user's project should take precedence
            assert call_kwargs["project_id"] == "user-project"
            assert call_kwargs["profile_id"] == "user-profile"

    @pytest.mark.asyncio
    async def test_fallback_to_request_when_api_user_has_no_project(self):
        """Should fall back to request project when API user has none."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        with patch("app.api.query.require_api_key"), \
             patch("app.api.query.require_claude_auth"), \
             patch("app.core.query_engine.execute_query") as mock_execute:

            mock_execute.return_value = {
                "response": "OK",
                "session_id": "sess",
                "metadata": {}
            }

            request = QueryRequest(
                prompt="Hello",
                profile="request-profile",
                project="request-project"
            )

            api_user = {
                "id": "user1",
                "profile_ids": None,  # No profile
                "project_id": None   # No project
            }

            await one_shot_query(request, api_user)

            call_kwargs = mock_execute.call_args.kwargs
            # Should fall back to request values
            assert call_kwargs["project_id"] == "request-project"
            assert call_kwargs["profile_id"] == "request-profile"


# =============================================================================
# API-user agent restriction (api_user_profiles)
# =============================================================================

_ALL_PROFILE_ENDPOINTS = [
    ("one_shot_query", "QueryRequest"),
    ("stream_one_shot_query", "QueryRequest"),
    ("conversation", "ConversationRequest"),
    ("stream_conversation", "ConversationRequest"),
    ("start_conversation", "ConversationRequest"),
]


class TestApiUserAgentRestriction:
    """An API user's assigned agents (``profile_ids``) are enforced server-side
    on every query path, not just filtered out of GET /agents."""

    @staticmethod
    def _call(endpoint, model, profile, api_user):
        from app.api import query
        from app.core import models

        request = getattr(models, model)(prompt="Hi", profile=profile)
        return getattr(query, endpoint)(request, api_user)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint,model", _ALL_PROFILE_ENDPOINTS)
    async def test_disallowed_agent_is_403(self, endpoint, model):
        from fastapi import HTTPException

        api_user = {"id": "u1", "profile_ids": ["allowed-a"]}
        with pytest.raises(HTTPException) as exc_info:
            await self._call(endpoint, model, "other-agent", api_user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint,model", _ALL_PROFILE_ENDPOINTS)
    async def test_omitted_with_several_allowed_is_400_naming_them(self, endpoint, model):
        from fastapi import HTTPException

        api_user = {"id": "u1", "profile_ids": ["allowed-a", "allowed-b"]}
        with pytest.raises(HTTPException) as exc_info:
            await self._call(endpoint, model, None, api_user)
        assert exc_info.value.status_code == 400
        assert "allowed-a" in exc_info.value.detail
        assert "allowed-b" in exc_info.value.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile,allowed,expected", [
        ("allowed-b", ["allowed-a", "allowed-b"], "allowed-b"),  # allowed agent
        (None, ["allowed-a"], "allowed-a"),  # omitted → the only agent
        ("any-agent", None, "any-agent"),  # unrestricted: unchanged
    ])
    async def test_resolved_agent_reaches_the_engine(self, profile, allowed, expected):
        from app.api.query import start_conversation
        from app.core.models import ConversationRequest

        with patch("app.api.query.start_background_query",
                   new=AsyncMock(return_value={"session_id": "s", "status": "started"})) as start:
            await start_conversation(
                ConversationRequest(prompt="Hi", profile=profile),
                {"id": "u1", "profile_ids": allowed},
            )
        assert start.call_args.kwargs["profile_id"] == expected

    @pytest.mark.asyncio
    async def test_one_shot_runs_the_allowed_agent(self):
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        with patch("app.core.query_engine.execute_query",
                   new=AsyncMock(return_value={"response": "ok", "session_id": "s",
                                               "metadata": {}})) as execute:
            await one_shot_query(
                QueryRequest(prompt="Hi", profile="allowed-a"),
                {"id": "u1", "profile_ids": ["allowed-a"]},
            )
        assert execute.call_args.kwargs["profile_id"] == "allowed-a"

    def test_owner_is_unrestricted(self):
        from app.api.sessions_helpers import resolve_api_user_profile

        assert resolve_api_user_profile(None, "any-agent") == "any-agent"
        assert resolve_api_user_profile(None, None) is None

    def test_legacy_profile_id_column_is_not_a_restriction_source(self):
        """The dead ``api_users.profile_id`` column no longer picks the agent."""
        from app.api.sessions_helpers import resolve_api_user_profile

        user = {"id": "u1", "profile_id": "legacy", "profile_ids": ["allowed-a"]}
        assert resolve_api_user_profile(user, None) == "allowed-a"


# =============================================================================
# Continuing a session: the caller must be able to access it
# =============================================================================

_CONTINUING_ENDPOINTS = ["conversation", "stream_conversation", "start_conversation"]


class TestContinuedSessionAccess:
    """A ``session_id`` naming another principal's session, or one outside the
    key's scope, is refused before any turn runs; the key's own in-scope
    session continues as before."""

    @staticmethod
    async def _call(endpoint, session, api_user):
        from app.api import query
        from app.core.models import ConversationRequest
        from app.db import database

        request = ConversationRequest(prompt="Hi", profile="mine", session_id="s1")
        with patch.object(database, "get_session", return_value=session), \
                patch("app.api.query.start_background_query",
                      new=AsyncMock(return_value={"session_id": "s1", "status": "started"})) as start, \
                patch("app.api.query._stream_query_with_failover") as stream, \
                patch("app.core.turn_manager.submit_async", new=AsyncMock()) as submit, \
                patch("app.core.turn_manager.wait_session_idle", new=AsyncMock(return_value=True)), \
                patch("app.core.turn_outcome.final_turn_outcome",
                      return_value={"usage": {}, "error": None, "interrupted": False}), \
                patch("app.core.turn_outcome.extract_final_assistant_text", return_value="ok"):
            await getattr(query, endpoint)(request, api_user)
        return start, stream, submit

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", _CONTINUING_ENDPOINTS)
    @pytest.mark.parametrize("session", [
        {"id": "s1", "api_user_id": "u1", "profile_id": "someone-elses-agent"},  # agent outside the key's set
        {"id": "s1", "api_user_id": "u1", "profile_id": "mine", "project_id": "other-project"},  # other project
        {"id": "s1", "profile_id": "mine", "project_id": "my-project"},  # the owner's session
        {"id": "s1", "api_user_id": "u2", "profile_id": "mine", "project_id": "my-project"},  # another key's
        {"id": "s1", "profile_id": "mine"},  # the owner's projectless chat
    ])
    async def test_foreign_session_is_403(self, endpoint, session):
        from fastapi import HTTPException

        api_user = {"id": "u1", "profile_ids": ["mine"], "project_id": "my-project"}
        with pytest.raises(HTTPException) as exc_info:
            await self._call(endpoint, session, api_user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", _CONTINUING_ENDPOINTS)
    async def test_accessible_session_continues(self, endpoint):
        api_user = {"id": "u1", "profile_ids": ["mine"], "project_id": "my-project"}
        session = {"id": "s1", "api_user_id": "u1", "profile_id": "mine", "project_id": "my-project"}
        start, _stream, submit = await self._call(endpoint, session, api_user)
        if endpoint == "start_conversation":
            assert start.call_args.kwargs["session_id"] == "s1"
        elif endpoint == "conversation":
            assert submit.call_args.args[0] == "s1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", _CONTINUING_ENDPOINTS)
    async def test_project_key_continues_its_own_projectless_session(self, endpoint):
        api_user = {"id": "u1", "profile_ids": ["mine"], "project_id": "my-project"}
        session = {"id": "s1", "api_user_id": "u1", "profile_id": "mine"}
        start, _stream, submit = await self._call(endpoint, session, api_user)
        if endpoint == "start_conversation":
            assert start.call_args.kwargs["session_id"] == "s1"
        elif endpoint == "conversation":
            assert submit.call_args.args[0] == "s1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", _CONTINUING_ENDPOINTS)
    async def test_unrestricted_key_cannot_continue_the_owners_session(self, endpoint):
        from fastapi import HTTPException

        api_user = {"id": "u1", "profile_ids": [], "project_id": None}
        with pytest.raises(HTTPException) as exc_info:
            await self._call(endpoint, {"id": "s1", "profile_id": "mine"}, api_user)
        assert exc_info.value.status_code == 403


# =============================================================================
# Integration-style Tests with TestClient
# =============================================================================

class TestQueryEndpointsIntegration:
    """Integration tests that verify route registration and basic behavior."""

    def test_query_endpoint_exists(self):
        """Query endpoint should be registered in the router."""
        from app.api.query import router

        # Check that the route exists (paths include the router prefix /api/v1)
        routes = [route.path for route in router.routes]
        assert "/api/v1/query" in routes

    def test_query_stream_endpoint_exists(self):
        """Query stream endpoint should be registered."""
        from app.api.query import router

        routes = [route.path for route in router.routes]
        assert "/api/v1/query/stream" in routes

    def test_conversation_endpoint_exists(self):
        """Conversation endpoint should be registered."""
        from app.api.query import router

        routes = [route.path for route in router.routes]
        assert "/api/v1/conversation" in routes

    def test_conversation_stream_endpoint_exists(self):
        """Conversation stream endpoint should be registered."""
        from app.api.query import router

        routes = [route.path for route in router.routes]
        assert "/api/v1/conversation/stream" in routes

    def test_conversation_start_endpoint_exists(self):
        """Conversation start endpoint should be registered."""
        from app.api.query import router

        routes = [route.path for route in router.routes]
        assert "/api/v1/conversation/start" in routes

    def test_interrupt_endpoint_exists(self):
        """Session interrupt endpoint should be registered."""
        from app.api.query import router

        routes = [route.path for route in router.routes]
        assert "/api/v1/session/{session_id}/interrupt" in routes

    def test_streaming_active_endpoint_exists(self):
        """Streaming active endpoint should be registered."""
        from app.api.query import router

        routes = [route.path for route in router.routes]
        assert "/api/v1/streaming/active" in routes

    def test_all_endpoints_have_tags(self):
        """All endpoints should have the Query tag."""
        from app.api.query import router

        assert router.tags == ["Query"]


# =============================================================================
# Test QueryMetadata Model
# =============================================================================

class TestQueryMetadataModel:
    """Tests for QueryMetadata model construction."""

    def test_query_metadata_from_dict(self):
        """Should construct QueryMetadata from result dict."""
        from app.core.models import QueryMetadata

        metadata_dict = {
            "model": "claude-sonnet-4-20250514",
            "duration_ms": 1500,
            "total_cost_usd": 0.025,
            "tokens_in": 150,
            "tokens_out": 75,
            "num_turns": 2
        }

        metadata = QueryMetadata(**metadata_dict)

        assert metadata.model == "claude-sonnet-4-20250514"
        assert metadata.duration_ms == 1500
        assert metadata.total_cost_usd == 0.025
        assert metadata.tokens_in == 150
        assert metadata.tokens_out == 75
        assert metadata.num_turns == 2

    def test_query_metadata_optional_fields(self):
        """QueryMetadata should handle optional fields."""
        from app.core.models import QueryMetadata

        # Minimal metadata
        metadata = QueryMetadata()

        assert metadata.model is None
        assert metadata.duration_ms is None
        assert metadata.total_cost_usd is None


# =============================================================================
# Test Request Model Validation
# =============================================================================

class TestRequestModelValidation:
    """Tests for request model validation."""

    def test_query_request_requires_prompt(self):
        """QueryRequest should require prompt."""
        from pydantic import ValidationError
        from app.core.models import QueryRequest

        with pytest.raises(ValidationError):
            QueryRequest(profile="test")

    def test_query_request_prompt_min_length(self):
        """QueryRequest prompt should have minimum length."""
        from pydantic import ValidationError
        from app.core.models import QueryRequest

        with pytest.raises(ValidationError):
            QueryRequest(prompt="", profile="test")

    def test_conversation_request_allows_session_id(self):
        """ConversationRequest should allow session_id."""
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Continue",
            session_id="existing-session-123"
        )

        assert request.session_id == "existing-session-123"

    def test_conversation_request_device_id(self):
        """ConversationRequest should accept device_id."""
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Hello",
            device_id="my-device-id-123"
        )

        assert request.device_id == "my-device-id-123"

    def test_query_overrides_validation(self):
        """QueryOverrides should validate permission_mode."""
        from app.core.models import QueryOverrides

        overrides = QueryOverrides(
            model="opus",
            permission_mode="bypassPermissions",
            max_turns=10
        )

        assert overrides.model == "opus"
        assert overrides.permission_mode == "bypassPermissions"
        assert overrides.max_turns == 10


# =============================================================================
# Test Error Response Format
# =============================================================================

class TestErrorResponseFormat:
    """Tests for error response format consistency."""

    def test_value_error_response_format(self):
        """ValueError should result in 400 response with detail."""
        from fastapi import HTTPException

        error = ValueError("Profile not found: test-profile")

        exc = HTTPException(
            status_code=400,
            detail=str(error)
        )

        assert exc.status_code == 400
        assert "Profile not found" in exc.detail

    def test_general_error_response_format(self):
        """General exceptions should result in 500 with wrapped detail."""
        from fastapi import HTTPException

        error = RuntimeError("Connection timeout")

        exc = HTTPException(
            status_code=500,
            detail=f"Query execution failed: {str(error)}"
        )

        assert exc.status_code == 500
        assert "Query execution failed" in exc.detail
        assert "Connection timeout" in exc.detail
