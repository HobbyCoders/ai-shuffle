"""GetAppErrors and NotifyDevices must not read as "all clear" when they aren't.

Two audited failures, one shape:

* An agent chasing a silent provider 403 called ``GetAppErrors`` and got
  ``{"errors": [], "count": 0}``. The error log persists ERROR+ only, and the
  credit pollers logged their HTTP failures at warning and debug — so the
  evidence existed and the tool could not see it. Another call asked for
  ``limit: 100`` and got 99,984 characters back.
* A monitoring session announced a release through ``NotifyDevices``, which
  answered ``{"sent": 0, "successful": 0}`` because no device was registered.
  The alert reached nobody; the tool said success.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.mcp import builtin_tools as bt


def _builtin_handlers(session_id="session-under-test"):
    import claude_agent_sdk as sdk

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(session_id=session_id)

    return {tool.name: tool for tool in captured["tools"]}


def _body(result):
    return json.loads(result["content"][0]["text"])


def _row(idx: int, level: str = "ERROR", detail: str = "trace") -> dict:
    return {
        "id": f"e{idx}",
        "source": "backend",
        "level": level,
        "message": f"boom {idx}",
        "detail": detail,
        "occurrence_count": 1,
        "created_at": "2026-09-04T00:00:00+00:00",
        "updated_at": "2026-09-04T00:00:00+00:00",
    }


# ─── GetAppErrors: min_level ─────────────────────────────────────────────────


def test_default_min_level_asks_for_errors_only(mock_db):
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("app.db.error_logs.get_recent_errors", side_effect=_fake):
        body = _body(asyncio.run(_builtin_handlers()["GetAppErrors"].handler({})))

    assert captured["levels"] == ["ERROR", "CRITICAL"]
    assert body["min_level"] == "ERROR"


def test_min_level_warning_widens_the_query(mock_db):
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return [_row(1, level="WARNING")]

    with patch("app.db.error_logs.get_recent_errors", side_effect=_fake):
        body = _body(
            asyncio.run(
                _builtin_handlers()["GetAppErrors"].handler({"min_level": "warning"})
            )
        )

    assert captured["levels"] == ["WARNING", "ERROR", "CRITICAL"]
    assert body["min_level"] == "WARNING"
    assert body["count"] == 1


def test_unknown_min_level_is_an_error(mock_db):
    with patch("app.db.error_logs.get_recent_errors", side_effect=lambda **kw: []):
        result = asyncio.run(
            _builtin_handlers()["GetAppErrors"].handler({"min_level": "chatty"})
        )

    assert result.get("isError") is True
    assert "min_level" in _body(result)["error"]


def test_empty_error_result_says_it_is_not_proof(mock_db):
    """count: 0 is what the agent read as 'the app is healthy'."""
    with patch("app.db.error_logs.get_recent_errors", side_effect=lambda **kw: []):
        body = _body(asyncio.run(_builtin_handlers()["GetAppErrors"].handler({})))

    assert body["count"] == 0
    assert 'min_level: "WARNING"' in body["note"]


def test_empty_warning_result_has_no_nudge(mock_db):
    """Nothing left to escalate to — the note would be noise."""
    with patch("app.db.error_logs.get_recent_errors", side_effect=lambda **kw: []):
        body = _body(
            asyncio.run(
                _builtin_handlers()["GetAppErrors"].handler({"min_level": "WARNING"})
            )
        )

    assert "note" not in body


def test_description_names_the_warning_escape_hatch(mock_db):
    description = _builtin_handlers()["GetAppErrors"].description
    assert "WARNING" in description
    assert "not that nothing went wrong" in description.lower()


# ─── GetAppErrors: output cap ────────────────────────────────────────────────


def test_limit_is_capped_server_side(mock_db):
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("app.db.error_logs.get_recent_errors", side_effect=_fake):
        asyncio.run(_builtin_handlers()["GetAppErrors"].handler({"limit": 100}))

    assert captured["limit"] == bt._APP_ERRORS_MAX_LIMIT
    assert bt._APP_ERRORS_MAX_LIMIT < 100


def test_default_limit_is_small(mock_db):
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("app.db.error_logs.get_recent_errors", side_effect=_fake):
        asyncio.run(_builtin_handlers()["GetAppErrors"].handler({}))

    assert captured["limit"] == 10


def test_huge_payload_is_truncated_with_a_note(mock_db):
    rows = [_row(i, detail="x" * 5_000) for i in range(50)]

    with patch("app.db.error_logs.get_recent_errors", side_effect=lambda **kw: rows):
        result = asyncio.run(_builtin_handlers()["GetAppErrors"].handler({"limit": 50}))

    body = _body(result)
    assert body["count"] < len(rows)
    assert body["matched"] == len(rows)
    assert "omitted" in body["truncated"]
    assert "clipped" in body["truncated"]
    # Keep the serialized response envelope within the same order of magnitude as the configured output cap.
    assert len(result["content"][0]["text"]) < 2 * bt._APP_ERRORS_MAX_CHARS


def test_a_single_oversized_entry_is_clipped_not_dropped(mock_db):
    rows = [_row(1, detail="y" * 500_000)]

    with patch("app.db.error_logs.get_recent_errors", side_effect=lambda **kw: rows):
        body = _body(asyncio.run(_builtin_handlers()["GetAppErrors"].handler({})))

    assert body["count"] == 1
    assert len(body["errors"][0]["detail"]) < bt._APP_ERRORS_MAX_DETAIL_CHARS + 100
    assert "clipped" in body["truncated"]


def test_small_result_is_untouched(mock_db):
    rows = [_row(1), _row(2)]

    with patch("app.db.error_logs.get_recent_errors", side_effect=lambda **kw: rows):
        body = _body(asyncio.run(_builtin_handlers()["GetAppErrors"].handler({})))

    assert body["count"] == 2
    assert "truncated" not in body
    assert body["errors"][0]["detail"] == "trace"


# ─── NotifyDevices ───────────────────────────────────────────────────────────


class _Result:
    def __init__(self, success: bool) -> None:
        self.success = success


def _notify(results):
    service = AsyncMock()
    service.send_to_all_devices = AsyncMock(return_value=results)
    with patch(
        "app.services.push_notification.push_notification_service", service
    ):
        return _body(
            asyncio.run(
                _builtin_handlers()["NotifyDevices"].handler(
                    {"title": "Release", "body": "v1.2.3 is live"}
                )
            )
        )


def test_no_registered_devices_is_not_a_green_light(mock_db):
    body = _notify([])

    assert body["sent"] == 0
    assert body["delivered"] is False
    assert body["note"] == (
        "no registered devices — nothing was delivered; tell the user in chat "
        "instead."
    )
    # Still not an exception: there is nothing to retry.
    assert body["success"] is True


def test_every_device_rejecting_is_also_undelivered(mock_db):
    body = _notify([_Result(False), _Result(False)])

    assert body["sent"] == 2
    assert body["successful"] == 0
    assert body["delivered"] is False
    assert "nothing was delivered" in body["note"]


def test_a_real_delivery_carries_no_note(mock_db):
    body = _notify([_Result(True), _Result(False)])

    assert body["sent"] == 2
    assert body["successful"] == 1
    assert body["delivered"] is True
    assert "note" not in body


def test_description_warns_about_the_empty_device_list(mock_db):
    description = _builtin_handlers()["NotifyDevices"].description
    assert "no registered devices" in description.lower()
    assert "chat" in description.lower()

def test_stored_error_trace_preserves_final_exception_and_bounded_detail(mock_db):
    from app.db import error_logs
    detail = "Traceback root cause\n" + "middle frame\n" * 700 + "RuntimeError: final cause"
    error_id = error_logs.record_error("backend", "round8 trace", detail=detail, session_id="trace-session")
    assert error_id
    tool = _builtin_handlers(session_id="trace-session")["GetAppErrors"]
    body = _body(asyncio.run(tool.handler({"this_session_only": True})))
    trace = body["errors"][0]["detail"]
    assert trace.startswith("Traceback root cause")
    assert trace.endswith("RuntimeError: final cause")
    assert len(trace) <= bt._APP_ERRORS_MAX_DETAIL_CHARS
    page = _body(asyncio.run(tool.handler({"error_id": error_id, "offset": 2100, "max_chars": 1200})))
    assert page["detail"] == detail[2100:3300]
    assert page["next_offset"] == 3300
    other = _builtin_handlers(session_id="other")["GetAppErrors"]
    denied = _body(asyncio.run(other.handler({"error_id": error_id, "this_session_only": True})))
    assert denied["success"] is False


def test_logger_to_error_store_keeps_final_exception_beyond_store_cap(mock_db):
    import logging
    import sys
    from app.core.error_log_handler import ErrorLogDBHandler
    try:
        try:
            raise ValueError("original cause")
        except ValueError as cause:
            raise RuntimeError("long frame " * 5000 + "FINAL EXCEPTION") from cause
    except RuntimeError:
        record = logging.LogRecord("round8.logger", logging.ERROR, __file__, 1, "long error", (), sys.exc_info())
    record.session_id = "logger-trace"
    ErrorLogDBHandler().emit(record)
    tool = _builtin_handlers(session_id="logger-trace")["GetAppErrors"]
    body = _body(asyncio.run(tool.handler({"this_session_only": True})))
    trace = body["errors"][0]["detail"]
    assert "original cause" in trace
    assert trace.endswith("FINAL EXCEPTION")
    error_id = body["errors"][0]["id"]
    page = _body(asyncio.run(tool.handler({"error_id": error_id, "offset": 19000, "max_chars": 12000})))
    assert page["detail"].endswith("FINAL EXCEPTION")
    assert page["total_chars"] == 20000
