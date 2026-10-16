"""Slow log output must not borrow the caller's event-loop thread."""
import asyncio
import io
import logging
import sys
import threading
from unittest.mock import patch

import pytest

from app.core.queued_logging import QueuedHandler


def record(message="message", level=logging.ERROR, exc_info=None):
    return logging.LogRecord("test.queued", level, __file__, 1, message, (), exc_info)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["console", "ring"])
async def test_tracebacks_are_formatted_off_loop(kind):
    from app.api.logs import _RingBufferHandler

    output = io.StringIO()
    sink = logging.StreamHandler(output) if kind == "console" else _RingBufferHandler()
    seen = []
    loop_thread = threading.current_thread()

    class Formatter(logging.Formatter):
        def formatException(self, info):
            seen.append(threading.current_thread())
            return super().formatException(info)

    sink.setFormatter(Formatter())
    handler = QueuedHandler(sink)
    try:
        try:
            raise RuntimeError("preserved traceback")
        except RuntimeError:
            entry = record(exc_info=sys.exc_info())
        handler.handle(entry)
        await asyncio.to_thread(handler.flush)
        assert seen and all(thread is not loop_thread for thread in seen)
        assert entry.exc_text is None  # formatting must not mutate another sink's record
        text = output.getvalue() if kind == "console" else sink.buffer[0].message
        assert "RuntimeError: preserved traceback" in text
    finally:
        await asyncio.to_thread(handler.close)


def test_full_queue_drops_without_waiting_for_sink_and_reports_loss():
    entered, release = threading.Event(), threading.Event()
    messages = []

    class SlowSink(logging.Handler):
        def emit(self, entry):
            entered.set()
            assert release.wait(5)
            messages.append(entry.getMessage())

    sink = SlowSink()
    handler = QueuedHandler(sink, capacity=1)
    try:
        handler.handle(record("first"))
        assert entered.wait(1)
        # The consumer holds the sink's Handler lock, but the producer can still
        # enqueue one record, then drop another. No sleeps/timing threshold.
        with patch.object(handler.queue, "put", wraps=handler.queue.put) as put:
            handler.handle(record("second"))
            handler.handle(record("dropped"))
            assert all(call.kwargs.get("block") is False for call in put.call_args_list)
        assert handler.dropped == 1
        release.set()
        handler.flush()
        assert messages == ["first", "Logging sink queue full; dropped 1 records", "second"]
    finally:
        release.set()
        handler.close()
    assert not handler._thread.is_alive()
    handler.close()  # idempotent, and no late records reopen the file
    handler.handle(record("too late"))
    assert handler.queue.empty()


def test_sink_failure_does_not_kill_consumer_or_recurse():
    messages = []

    class FailingSink(logging.Handler):
        def emit(self, entry):
            if entry.msg == "bad":
                raise OSError("disk unavailable")
            messages.append(entry.getMessage())

    handler = QueuedHandler(FailingSink())
    try:
        handler.handle(record("bad"))
        handler.handle(record("good"))
        handler.flush()
        assert handler.failed == 1
        assert messages == ["good"]
    finally:
        handler.close()


def test_sink_level_changes_are_respected_and_arguments_are_snapshotted():
    output = io.StringIO()
    sink = logging.StreamHandler(output)
    handler = QueuedHandler(sink)
    try:
        sink.setLevel(logging.CRITICAL)
        handler.handle(record("hidden"))
        handler.flush()
        assert not output.getvalue()
        sink.setLevel(logging.INFO)
        args = {"value": "before"}
        entry = logging.LogRecord("test.queued", logging.INFO, __file__, 1, "%(value)s", (args,), None)
        handler.handle(entry)
        args["value"] = "after"
        handler.flush()
        assert output.getvalue() == "before\n"
    finally:
        handler.close()


def test_installation_is_idempotent_and_replaces_a_closed_consumer():
    from app.core.queued_logging import install_queued_handler

    logger = logging.Logger("isolated-console", level=logging.INFO)
    output = io.StringIO()
    sink = logging.StreamHandler(output)
    sink.set_name("isolated-console-sink")
    logger.addHandler(sink)
    first = install_queued_handler(logger, sink)
    try:
        assert install_queued_handler(logger, sink) is first
        assert logger.handlers == [first]
        logger.info("first lifespan")
        first.flush()
        first.close()  # logging.config.dictConfig closes registered handlers
        second = install_queued_handler(logger, sink)
        try:
            assert second is not first
            assert second.get_name() == sink.get_name()
            assert logger.handlers == [second]
            logger.info("second lifespan")
            second.flush()
            assert output.getvalue() == "first lifespan\nsecond lifespan\n"
        finally:
            second.close()
    finally:
        first.close()
