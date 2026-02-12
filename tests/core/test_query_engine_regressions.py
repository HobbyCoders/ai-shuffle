"""
Regression tests for query_engine.py

Bug references:
- Commit 1531f472: SDK subprocess JSONL not flushed before SIGTERM
- Commit 519be578: tool_result name='unknown' from missing tool_id→name mapping
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# =============================================================================
# Bug: _graceful_disconnect (commit 1531f472)
#
# stream_to_websocket() called disconnect() immediately after receiving
# ResultMessage, which sent SIGTERM to the subprocess. The subprocess writes
# to stdout and JSONL independently — stdout completed first, but the JSONL
# write for the final assistant message was still in-flight. ~35% of sessions
# with tool use lost their final assistant message.
#
# Fix: _graceful_disconnect() waits up to 5s for subprocess to exit naturally.
# =============================================================================

class TestGracefulDisconnect:

    async def test_graceful_disconnect_waits_for_subprocess(self):
        """Should wait for subprocess to exit naturally before calling disconnect."""
        from app.core.query_engine import _graceful_disconnect

        # Mock the SDK client internals
        mock_process = MagicMock()
        mock_process.returncode = None  # Still running

        # Process exits after 0.5s
        async def mock_wait():
            await asyncio.sleep(0.5)
            mock_process.returncode = 0

        mock_process.wait = mock_wait

        mock_transport = MagicMock()
        mock_transport._process = mock_process

        mock_query = MagicMock()
        mock_query.transport = mock_transport

        mock_client = MagicMock()
        mock_client._query = mock_query
        mock_client.disconnect = AsyncMock()

        await _graceful_disconnect(mock_client, "test-session")

        # disconnect() should have been called
        mock_client.disconnect.assert_awaited_once()
        # Process should have exited naturally
        assert mock_process.returncode == 0

    async def test_graceful_disconnect_timeout_on_hanging_subprocess(self):
        """Should force disconnect after timeout if subprocess doesn't exit."""
        from app.core.query_engine import _graceful_disconnect, _SUBPROCESS_EXIT_GRACE_SECONDS

        mock_process = MagicMock()
        mock_process.returncode = None

        async def mock_wait():
            # Hang forever (longer than grace period)
            await asyncio.sleep(100)

        mock_process.wait = mock_wait

        mock_transport = MagicMock()
        mock_transport._process = mock_process

        mock_query = MagicMock()
        mock_query.transport = mock_transport

        mock_client = MagicMock()
        mock_client._query = mock_query
        mock_client.disconnect = AsyncMock()

        # Should complete within grace period + small buffer
        await asyncio.wait_for(
            _graceful_disconnect(mock_client, "test-session"),
            timeout=_SUBPROCESS_EXIT_GRACE_SECONDS + 2
        )

        # disconnect() should still be called (forced)
        mock_client.disconnect.assert_awaited_once()

    async def test_graceful_disconnect_process_already_exited(self):
        """Should handle the case where process already exited."""
        from app.core.query_engine import _graceful_disconnect

        mock_process = MagicMock()
        mock_process.returncode = 0  # Already exited

        mock_transport = MagicMock()
        mock_transport._process = mock_process

        mock_query = MagicMock()
        mock_query.transport = mock_transport

        mock_client = MagicMock()
        mock_client._query = mock_query
        mock_client.disconnect = AsyncMock()

        await _graceful_disconnect(mock_client, "test-session")

        # Should still call disconnect (no-op if already exited)
        mock_client.disconnect.assert_awaited_once()

    async def test_graceful_disconnect_no_process_access(self):
        """Should handle when internal SDK structure can't be accessed."""
        from app.core.query_engine import _graceful_disconnect

        # Client with no _query attribute (SDK API changed)
        mock_client = MagicMock(spec=[])
        mock_client._query = None
        mock_client.disconnect = AsyncMock()

        # Should not raise, should still call disconnect
        await _graceful_disconnect(mock_client, "test-session")
        mock_client.disconnect.assert_awaited_once()

    async def test_graceful_disconnect_introspection_error(self):
        """Should handle exceptions during SDK introspection."""
        from app.core.query_engine import _graceful_disconnect

        mock_client = MagicMock()
        # getattr on _query raises
        type(mock_client)._query = PropertyMock(side_effect=AttributeError("no attribute"))
        mock_client.disconnect = AsyncMock()

        # Should not raise
        await _graceful_disconnect(mock_client, "test-session")
        mock_client.disconnect.assert_awaited_once()

    def test_grace_period_constant(self):
        """Grace period should be reasonable (not too short, not too long)."""
        from app.core.query_engine import _SUBPROCESS_EXIT_GRACE_SECONDS

        assert _SUBPROCESS_EXIT_GRACE_SECONDS >= 3, "Grace period too short - JSONL writes need time"
        assert _SUBPROCESS_EXIT_GRACE_SECONDS <= 30, "Grace period too long - blocking user"


# =============================================================================
# Bug: truncate_large_payload with base64 images
#
# Large base64 images from screenshots should be detected and replaced with
# a size indicator rather than being passed through (which would overwhelm
# the WebSocket and UI).
# =============================================================================

class TestLargePayloadTruncation:

    def test_base64_png_detected(self):
        """PNG base64 content should be detected and summarized."""
        from app.core.query_engine import truncate_large_payload, MAX_TOOL_OUTPUT_SIZE

        # PNG base64 signature (starts with iVBOR) - must exceed MAX_TOOL_OUTPUT_SIZE
        large_png = "iVBOR" + "A" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(large_png)
        assert "[Image/binary content" in result
        assert "KB" in result

    def test_base64_jpeg_detected(self):
        """JPEG base64 content should be detected and summarized."""
        from app.core.query_engine import truncate_large_payload, MAX_TOOL_OUTPUT_SIZE

        large_jpeg = "/9j/" + "A" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(large_jpeg)
        assert "[Image/binary content" in result

    def test_small_content_passes_through(self):
        """Small content should not be truncated."""
        from app.core.query_engine import truncate_large_payload

        small = "Hello, this is a tool result"
        result = truncate_large_payload(small)
        assert result == small

    def test_large_text_truncated_with_indicator(self):
        """Large text content should be truncated with size indicator."""
        from app.core.query_engine import truncate_large_payload

        # 500KB of text
        large_text = "x" * 500000
        result = truncate_large_payload(large_text)
        assert "truncated" in result.lower() or len(result) < len(large_text)
