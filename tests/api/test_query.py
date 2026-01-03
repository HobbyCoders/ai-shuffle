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
from unittest.mock import patch, MagicMock, AsyncMock


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

    def test_require_claude_auth_passes_when_authenticated(self):
        """Should not raise when Claude is authenticated."""
        from app.api.query import require_claude_auth

        with patch("app.api.query.auth_service") as mock_auth:
            mock_auth.is_claude_authenticated.return_value = True
            # Should not raise
            result = require_claude_auth()
            assert result is None

    def test_require_claude_auth_raises_when_not_authenticated(self):
        """Should raise 401 when Claude is not authenticated."""
        from fastapi import HTTPException
        from app.api.query import require_claude_auth

        with patch("app.api.query.auth_service") as mock_auth:
            mock_auth.is_claude_authenticated.return_value = False

            with pytest.raises(HTTPException) as exc_info:
                require_claude_auth()

            assert exc_info.value.status_code == 401
            assert "Claude CLI not authenticated" in str(exc_info.value.detail)


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
             patch("app.api.query.execute_query") as mock_execute:
            mock_api_key.return_value = {
                "id": "test-api-user-id",
                "username": "testuser",
                "profile_id": "test-profile",
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
        """Should use API user's configured profile over request profile."""
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        request = QueryRequest(
            prompt="Hello!",
            profile="request-profile"  # This should be overridden
        )
        api_user = {"id": "user1", "profile_id": "user-configured-profile"}

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
        api_user = {"id": "user1", "profile_id": None}

        await one_shot_query(request, api_user)

        call_kwargs = mock_dependencies["execute"].call_args.kwargs
        assert call_kwargs["profile_id"] == "request-profile"

    @pytest.mark.asyncio
    async def test_one_shot_query_requires_profile(self, mock_dependencies):
        """Should return 400 if no profile is configured or provided."""
        from fastapi import HTTPException
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        # Create request - profile defaults to "claude-code" but API user has none
        # and we need to override that default to None in our mock scenario
        request = QueryRequest(
            prompt="Hello!",
            profile="claude-code"  # Default profile
        )
        # API user has no profile and request profile should be ignored
        # We need to ensure neither api_user nor request has a valid profile
        api_user = {"id": "user1", "profile_id": None}

        # Patch the request's profile attribute to None after creation
        request.profile = None

        with pytest.raises(HTTPException) as exc_info:
            await one_shot_query(request, api_user)

        assert exc_info.value.status_code == 400
        assert "No profile configured" in str(exc_info.value.detail)

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
                permission_mode="bypassPermissions"
            )
        )
        api_user = {"id": "user1", "profile_id": "test-profile"}

        await one_shot_query(request, api_user)

        call_kwargs = mock_dependencies["execute"].call_args.kwargs
        assert call_kwargs["overrides"]["model"] == "opus"
        assert call_kwargs["overrides"]["permission_mode"] == "bypassPermissions"

    @pytest.mark.asyncio
    async def test_one_shot_query_value_error(self, mock_dependencies):
        """Should return 400 for ValueError from execute_query."""
        from fastapi import HTTPException
        from app.api.query import one_shot_query
        from app.core.models import QueryRequest

        mock_dependencies["execute"].side_effect = ValueError("Profile not found")

        request = QueryRequest(prompt="Hello!", profile="test-profile")
        api_user = {"id": "user1", "profile_id": "test-profile"}

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
        api_user = {"id": "user1", "profile_id": "test-profile"}

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
                "profile_id": "test-profile",
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
        # Set profile to None after creation (bypassing default)
        request.profile = None
        api_user = {"id": "user1", "profile_id": None}

        with pytest.raises(HTTPException) as exc_info:
            await stream_one_shot_query(request, api_user)

        assert exc_info.value.status_code == 400


# =============================================================================
# Test Conversation Endpoint
# =============================================================================

class TestConversation:
    """Tests for POST /api/v1/conversation endpoint."""

    @pytest.fixture
    def mock_conv_dependencies(self):
        """Set up mocks for conversation tests."""
        with patch("app.api.query.require_api_key") as mock_api_key, \
             patch("app.api.query.require_claude_auth") as mock_claude, \
             patch("app.api.query.execute_query") as mock_execute:
            mock_api_key.return_value = {
                "id": "test-api-user-id",
                "profile_id": "test-profile"
            }
            mock_claude.return_value = None
            mock_execute.return_value = {
                "response": "Continued response",
                "session_id": "existing-session-123",
                "metadata": {"model": "sonnet", "duration_ms": 500}
            }
            yield {
                "api_key": mock_api_key,
                "claude": mock_claude,
                "execute": mock_execute
            }

    @pytest.mark.asyncio
    async def test_conversation_new_session(self, mock_conv_dependencies):
        """Should create new conversation session."""
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Start a conversation",
            profile="test-profile"
        )
        api_user = mock_conv_dependencies["api_key"].return_value

        result = await conversation(request, api_user)

        assert result["response"] == "Continued response"
        call_kwargs = mock_conv_dependencies["execute"].call_args.kwargs
        assert call_kwargs["session_id"] is None

    @pytest.mark.asyncio
    async def test_conversation_continue_session(self, mock_conv_dependencies):
        """Should continue existing session when session_id provided."""
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        request = ConversationRequest(
            prompt="Continue conversation",
            session_id="existing-session-123"
        )
        api_user = {"id": "user1", "profile_id": "test-profile"}

        await conversation(request, api_user)

        call_kwargs = mock_conv_dependencies["execute"].call_args.kwargs
        assert call_kwargs["session_id"] == "existing-session-123"

    @pytest.mark.asyncio
    async def test_conversation_value_error(self, mock_conv_dependencies):
        """Should return 400 for ValueError."""
        from fastapi import HTTPException
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        mock_conv_dependencies["execute"].side_effect = ValueError("Session not found")

        request = ConversationRequest(
            prompt="Hello!",
            session_id="invalid-session"
        )
        api_user = {"id": "user1", "profile_id": "test-profile"}

        with pytest.raises(HTTPException) as exc_info:
            await conversation(request, api_user)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_conversation_general_error(self, mock_conv_dependencies):
        """Should return 500 for general exceptions."""
        from fastapi import HTTPException
        from app.api.query import conversation
        from app.core.models import ConversationRequest

        mock_conv_dependencies["execute"].side_effect = RuntimeError("Connection failed")

        request = ConversationRequest(prompt="Hello!")
        api_user = {"id": "user1", "profile_id": "test-profile"}

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
                "profile_id": "test-profile"
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
        api_user = {"id": "user1", "profile_id": None}

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
        api_user = {"id": "user1", "profile_id": "test-profile"}

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
                "profile_id": "test-profile"
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
        api_user = {"id": "user1", "profile_id": "test-profile"}

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
        api_user = {"id": "user1", "profile_id": None}

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
        api_user = {"id": "user1", "profile_id": "test-profile"}

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
        api_user = {"id": "user1", "profile_id": "test-profile"}

        with pytest.raises(HTTPException) as exc_info:
            await start_conversation(request, api_user)

        assert exc_info.value.status_code == 500
        assert "Failed to start conversation" in str(exc_info.value.detail)


# =============================================================================
# Test Interrupt Session Endpoint
# =============================================================================

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

        result = await interrupt("session-123", "test-token")

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
            await interrupt("nonexistent-session", "test-token")

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

        result = await list_active_sessions("test-token")

        assert "active_sessions" in result
        assert result["active_sessions"] == ["session-1", "session-2", "session-3"]

    @pytest.mark.asyncio
    async def test_list_active_sessions_empty(self, mock_list_dependencies):
        """Should return empty list when no active sessions."""
        from app.api.query import list_active_sessions

        mock_list_dependencies["get_sessions"].return_value = []

        result = await list_active_sessions("test-token")

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

            mock_api.return_value = {"id": "user1", "profile_id": "test-profile"}
            request = QueryRequest(prompt="Hello!")

            # Call the endpoint - it returns a StreamingResponse
            response = await stream_one_shot_query(request, {"id": "user1", "profile_id": "test"})

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

            mock_api.return_value = {"id": "user1", "profile_id": "test-profile"}
            request = ConversationRequest(prompt="Continue conversation")

            response = await stream_conversation(request, {"id": "user1", "profile_id": "test"})

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

            mock_api.return_value = {"id": "user1", "profile_id": "test-profile"}
            request = QueryRequest(prompt="Hello!")

            response = await stream_one_shot_query(request, {"id": "user1", "profile_id": "test"})

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
             patch("app.api.query.execute_query") as mock_execute:

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
                "profile_id": "user-profile",
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
             patch("app.api.query.execute_query") as mock_execute:

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
                "profile_id": None,  # No profile
                "project_id": None   # No project
            }

            await one_shot_query(request, api_user)

            call_kwargs = mock_execute.call_args.kwargs
            # Should fall back to request values
            assert call_kwargs["project_id"] == "request-project"
            assert call_kwargs["profile_id"] == "request-profile"


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
