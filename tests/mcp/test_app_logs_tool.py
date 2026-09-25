"""GetAppLogs — the app's own runtime log, in-session (round-7 audit).

``GetAppErrors`` reads the durable ERROR/WARNING table. Everything below
WARNING — which is where the app narrates what it actually did — lived only in
the in-memory ring buffer behind ``GET /api/v1/logs``, and that endpoint takes
an admin session COOKIE, which a Bearer API key cannot satisfy. An audited
session that needed those lines spent user round-trips and needless money driving
a browser through a Cloudflare login to read them.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.mcp.tool_helpers import tool_result_json


def _handlers(session_id="session-logs"):
    import app.mcp.inproc as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id=session_id, is_admin=True)

    return {tool.name: tool for tool in captured["tools"]}


def _get(**args):
    return tool_result_json(asyncio.run(_handlers()["GetAppLogs"].handler(args)))


@pytest.fixture
def buffered(monkeypatch):
    """Put known records in the ring buffer the tool reads.

    The handler is muted for the duration: it is attached to the ROOT logger,
    so without this every line the app logs while the test runs (building the
    tool server, for one) lands in the buffer under test.
    """
    import logging

    from app.api import logs as logs_api

    monkeypatch.setattr(logs_api.ring_handler, "level", logging.CRITICAL)

    def _record(message, level="INFO", logger_name="app.core.turn_scheduler",
                minutes_ago=0):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return logs_api._LogRecord(
            timestamp=stamp.isoformat(), level=level,
            logger_name=logger_name, message=message,
        )

    def _fill(records):
        monkeypatch.setattr(logs_api.ring_handler, "buffer", list(records))

    return _record, _fill


# ─── it is registered ────────────────────────────────────────────────────────


def test_the_tool_is_registered_and_allow_listed(mock_db):
    from app.mcp.builtin_tools import get_builtin_tool_names

    assert "GetAppLogs" in _handlers()
    assert "mcp__builtins__GetAppLogs" in get_builtin_tool_names()


def test_it_is_classified_as_a_safe_read(mock_db):
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP

    assert BUILTINS_RISK_MAP["GetAppLogs"] == "safe"


def test_the_description_points_at_the_gap_it_fills(mock_db):
    description = _handlers()["GetAppLogs"].description

    assert "GetAppErrors" in description
    assert "INFO" in description
    assert "search" in description


# ─── it reads, filters and bounds ────────────────────────────────────────────


def test_it_returns_the_newest_lines_first(mock_db, buffered):
    record, fill = buffered
    fill([record(f"line {i}") for i in range(5)])

    body = _get()

    assert body["success"] is True
    assert [line["message"] for line in body["lines"]][0] == "line 4"
    assert body["buffered"] == 5


def test_info_is_reachable_which_is_the_whole_point(mock_db, buffered):
    record, fill = buffered
    fill([
        record("SessionRuntime: self-healed 1 stale parked turn(s)", level="INFO"),
        record("something broke", level="ERROR"),
    ])

    body = _get(level="INFO")

    assert [line["message"] for line in body["lines"]] == [
        "SessionRuntime: self-healed 1 stale parked turn(s)"
    ]


def test_search_is_a_case_insensitive_substring(mock_db, buffered):
    record, fill = buffered
    fill([record("wake for fe4915c3 failed"), record("unrelated chatter")])

    body = _get(search="FE4915C3")

    assert len(body["lines"]) == 1
    assert "fe4915c3" in body["lines"][0]["message"]


def test_since_minutes_drops_older_lines(mock_db, buffered):
    record, fill = buffered
    fill([record("ancient", minutes_ago=120), record("recent", minutes_ago=1)])

    body = _get(since_minutes=10)

    assert [line["message"] for line in body["lines"]] == ["recent"]


def test_an_unknown_level_is_refused_by_name(mock_db, buffered):
    record, fill = buffered
    fill([record("x")])

    body = _get(level="LOUD")

    assert body["success"] is False
    assert "LOUD" in body["error"]


def test_the_limit_is_capped_server_side(mock_db, buffered):
    from app.mcp import builtin_tools as bt

    record, fill = buffered
    fill([record(f"line {i}") for i in range(bt._APP_LOGS_MAX_LIMIT + 50)])

    body = _get(limit=100_000)

    assert len(body["lines"]) == bt._APP_LOGS_MAX_LIMIT
    assert "more matching line(s) omitted" in body["truncated"]


def test_the_payload_has_a_character_ceiling(mock_db, buffered):
    """A full-text log is the easiest way there is to fill a context window."""
    from app.mcp import builtin_tools as bt

    record, fill = buffered
    fill([record("x" * 1_500) for _ in range(200)])

    body = _get(limit=200)

    total = sum(len(line["message"]) for line in body["lines"])
    assert total <= bt._APP_LOGS_MAX_CHARS + bt._APP_LOGS_MAX_LINE_CHARS
    assert body["matched"] == 200
    assert "truncated" in body


def test_one_enormous_line_is_clipped_not_dropped(mock_db, buffered):
    from app.mcp import builtin_tools as bt

    record, fill = buffered
    fill([record("y" * 50_000)])

    body = _get()

    assert len(body["lines"]) == 1
    assert "characters clipped" in body["lines"][0]["message"]
    assert len(body["lines"][0]["message"]) < bt._APP_LOGS_MAX_LINE_CHARS + 200


def test_an_empty_answer_says_the_buffer_is_not_history(mock_db, buffered):
    record, fill = buffered
    fill([])

    body = _get(search="nothing matches this")

    assert body["lines"] == []
    assert "empty after a restart" in body["note"]

def test_logger_long_chained_exception_preserves_tail_and_detail_pages(mock_db, monkeypatch):
    import logging
    from app.api import logs
    handler = logs._RingBufferHandler()
    try:
        try:
            raise ValueError("root cause")
        except ValueError as cause:
            raise RuntimeError("padding " * 1000 + "FINAL EXCEPTION") from cause
    except RuntimeError:
        record = logging.LogRecord("test.round8", logging.ERROR, __file__, 1, "failed", (), __import__("sys").exc_info())
    handler.emit(record)
    monkeypatch.setattr(logs.ring_handler, "buffer", handler.buffer)
    # Other suites enable root logging, so MCP registration can add records.
    body = _get(search="FINAL EXCEPTION")
    (line,) = body["lines"]
    assert "root cause" in line["message"]
    assert line["message"].endswith("FINAL EXCEPTION")
    assert len(line["message"]) <= 2000
    pieces, offset = [], 0
    while True:
        page = _get(record_id=line["record_id"], offset=offset, max_chars=1000)
        pieces.append(page["detail"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pieces) == handler.buffer[0].message
    handler.buffer.clear()
    assert "unavailable" in _get(record_id=line["record_id"])["error"]
