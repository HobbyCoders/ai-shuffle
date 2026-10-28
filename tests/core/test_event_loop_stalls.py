"""Regressions for event-loop stalls under disk pressure (#2112).

Formatting a traceback stats every frame's source file, and a blocking DB
read on the loop waits on the disk too. Neither may run on the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import traceback
import weakref
from unittest.mock import MagicMock, patch

from app.core import error_log_handler as elh


def _record_with_exc() -> logging.LogRecord:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_info = sys.exc_info()
    return logging.LogRecord("app.test", logging.ERROR, __file__, 1, "failed %s", ("x",), exc_info)


def test_queued_handler_does_not_format_tracebacks_on_the_producer():
    # Initialize every registered logging.Handler field, but leave the
    # consumer stopped so it cannot race the producer-only queue assertions.
    with patch.object(logging.handlers.QueueListener, "start"):
        handler = elh._QueuedErrorLogHandler()
    try:
        with patch.object(traceback, "format_exception", side_effect=AssertionError("formatted on producer")), \
                patch.object(elh._formatter, "formatException", side_effect=AssertionError("formatted on producer")):
            handler.emit(_record_with_exc())
        queued = handler.queue.get_nowait()
        assert queued.exc_info is not None
        assert queued.getMessage() == "failed x"
    finally:
        handler.close()
    # The traceback can retain this handler until GC. A subsequent uvicorn
    # dictConfig/shutdown must safely close it again, regardless of GC timing.
    logging.shutdown([weakref.ref(handler)])
    assert handler.listener._thread is None


def test_listener_thread_persists_the_traceback(monkeypatch):
    captured = {}
    monkeypatch.setattr(elh, "_capture_level", lambda: logging.WARNING)
    fake = MagicMock()
    fake.record_error.side_effect = lambda **kw: captured.update(kw, thread=threading.current_thread())
    monkeypatch.setitem(sys.modules, "app.db.error_logs", fake)
    import app.db as app_db
    monkeypatch.setattr(app_db, "error_logs", fake, raising=False)
    handler = elh._QueuedErrorLogHandler()
    try:
        handler.emit(_record_with_exc())
    finally:
        handler.close()
    assert "RuntimeError: boom" in captured["detail"]
    assert captured["thread"] is not threading.main_thread()


def test_client_disconnect_is_not_logged_with_a_traceback(caplog):
    from starlette.requests import ClientDisconnect

    from app.main import unhandled_exception_handler

    request = MagicMock()
    request.method, request.url.path = "POST", "/api/v1/browser/panel/control/report"
    with caplog.at_level(logging.INFO):
        response = asyncio.run(unhandled_exception_handler(request, ClientDisconnect()))
    assert response.status_code == 400
    assert not any(r.exc_info for r in caplog.records)


