"""Tests for StreamingContext structured logging."""

import logging
import pytest

from app.core.streaming_context import StreamingContext


class TestStreamingContext:
    """Test structured logging context."""

    def test_log_prefix_format(self):
        """Prefix should include truncated IDs."""
        ctx = StreamingContext(
            session_id="session-1234567890",
            device_id="device-abcdefghij",
            message_id="msg-xyz1234567",
            request_id="req12345",
        )
        prefix = ctx.log_prefix
        assert prefix == "[s=session- d=device-a m=msg-xyz1 r=req12345]"

    def test_log_prefix_with_short_ids(self):
        """Short IDs should not be padded or truncated."""
        ctx = StreamingContext(
            session_id="abc",
            device_id="def",
            message_id="ghi",
            request_id="jkl",
        )
        assert ctx.log_prefix == "[s=abc d=def m=ghi r=jkl]"

    def test_log_prefix_with_empty_ids(self):
        """Empty IDs should show '?'."""
        ctx = StreamingContext(
            session_id="",
            device_id="",
            message_id="",
            request_id="req1",
        )
        assert ctx.log_prefix == "[s=? d=? m=? r=req1]"

    def test_default_message_and_request_ids(self):
        """message_id and request_id should auto-generate."""
        ctx = StreamingContext(session_id="s1", device_id="d1")
        assert ctx.message_id  # Not empty
        assert ctx.request_id  # Not empty
        assert len(ctx.request_id) == 8

    def test_debug_logging(self, caplog):
        """debug() should log with prefix at DEBUG level."""
        ctx = StreamingContext(
            session_id="sess1",
            device_id="dev1",
            message_id="msg1",
            request_id="req1",
        )
        logger = logging.getLogger("test.streaming_context")
        with caplog.at_level(logging.DEBUG, logger="test.streaming_context"):
            ctx.debug(logger, "test message")

        assert len(caplog.records) == 1
        assert "[s=sess1 d=dev1 m=msg1 r=req1] test message" in caplog.records[0].message

    def test_warning_logging(self, caplog):
        """warning() should log at WARNING level."""
        ctx = StreamingContext(
            session_id="s1",
            device_id="d1",
            message_id="m1",
            request_id="r1",
        )
        logger = logging.getLogger("test.streaming_context")
        with caplog.at_level(logging.WARNING, logger="test.streaming_context"):
            ctx.warning(logger, "something went wrong")

        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == "WARNING"

    def test_error_logging_with_exc_info(self, caplog):
        """error() should support exc_info parameter."""
        ctx = StreamingContext(session_id="s1", device_id="d1")
        logger = logging.getLogger("test.streaming_context")
        with caplog.at_level(logging.ERROR, logger="test.streaming_context"):
            try:
                raise ValueError("test error")
            except ValueError:
                ctx.error(logger, "caught error", exc_info=True)

        assert len(caplog.records) == 1
        assert caplog.records[0].exc_info is not None

    def test_user_id_optional(self):
        """user_id should default to None."""
        ctx = StreamingContext(session_id="s1", device_id="d1")
        assert ctx.user_id is None

        ctx_with_user = StreamingContext(session_id="s1", device_id="d1", user_id="admin")
        assert ctx_with_user.user_id == "admin"

    def test_two_contexts_have_different_request_ids(self):
        """Each context should get a unique request_id."""
        ctx1 = StreamingContext(session_id="s1", device_id="d1")
        ctx2 = StreamingContext(session_id="s1", device_id="d1")
        assert ctx1.request_id != ctx2.request_id
