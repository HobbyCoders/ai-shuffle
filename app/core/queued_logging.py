"""Bounded logging bridge: slow sinks and traceback formatting never run on callers."""
from __future__ import annotations

import copy
import logging
import queue
import threading
import time


class QueuedHandler(logging.Handler):
    """Give one sink its own daemon, with bounded loss instead of blocking producers.

    Unlike QueueHandler.prepare, enqueue does NOT format exceptions (linecache
    can stat files on a saturated disk). Flush/close wait at most two seconds;
    the daemon retains ownership of the sink until its pending writes finish.
    """

    def __init__(self, sink: logging.Handler, *, capacity: int = 1024):
        if capacity < 1:
            raise ValueError("A logging queue must have a positive capacity")
        super().__init__(sink.level)
        self.sink = sink
        self.queue: queue.Queue = queue.Queue(maxsize=capacity)
        self.dropped = 0
        self.failed = 0
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._consume, name="log-sink", daemon=True)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        if self._stopping.is_set() or record.levelno < self.sink.level:
            return
        try:
            prepared = copy.copy(record)
            prepared.msg, prepared.args = record.getMessage(), None
            prepared.exc_text = None
            self.queue.put_nowait(prepared)
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.failed += 1

    def _consume(self) -> None:
        reported = 0
        try:
            while not self._stopping.is_set() or not self.queue.empty():
                try:
                    record = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if isinstance(record, threading.Event):
                        self.sink.flush()
                        record.set()
                    elif record.levelno >= self.sink.level:
                        self.sink.handle(record)
                        if self.dropped != reported:
                            count = self.dropped
                            self.sink.handle(logging.LogRecord(
                                __name__, logging.WARNING, __file__, 0,
                                "Logging sink queue full; dropped %s records", (count - reported,), None,
                            ))
                            reported = count
                except Exception:
                    # No recursive logging or producer-side stderr fallback.
                    self.failed += 1
                finally:
                    self.queue.task_done()
        finally:
            self.sink.close()

    def flush(self) -> None:
        if not hasattr(self, "_thread") or not self._thread.is_alive():
            return
        if threading.current_thread() is self._thread:
            return
        deadline = time.monotonic() + 2
        barrier = threading.Event()
        try:
            self.queue.put(barrier, timeout=2)
        except queue.Full:
            return
        barrier.wait(max(0, deadline - time.monotonic()))

    def close(self) -> None:
        # Serialize with Handler.handle so no producer can enqueue after the
        # consumer observes a stopped, empty queue. Never take the sink lock.
        self.acquire()
        try:
            self._stopping.set()
        finally:
            self.release()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
        super().close()


def install_queued_handler(logger: logging.Logger, sink: logging.Handler) -> QueuedHandler:
    """Wrap a configured sink once, after server logging configuration.

    dictConfig closes registered handlers, including queue consumers, even with
    disable_existing_loggers=False. Repeated lifespan startup must replace such
    a stopped bridge rather than mistake its name for an active consumer.
    """
    for handler in list(logger.handlers):
        if isinstance(handler, QueuedHandler) and handler.sink is sink:
            if not handler._closed:
                return handler
            logger.removeHandler(handler)
    bridge = QueuedHandler(sink)
    bridge.set_name(sink.get_name())
    logger.removeHandler(sink)
    logger.addHandler(bridge)
    return bridge
