"""
Regression tests for JSON serialization in builtin-tool result envelopes.

The DB layer (psycopg) returns real ``datetime`` objects. CronList surfaces an
automation's ``last_run_at`` (a TIMESTAMPTZ) straight into its ``_ok`` payload,
so a plain ``json.dumps`` there crashed any CronList call once an automation had
ever run with 'Object of type datetime is not JSON serializable'.

Mirrors the datetime-serialization regressions added in PR #464 for
app.mcp.config_helpers, but for app.mcp.builtin_tools._ok / _err.
"""

import json
from datetime import datetime, timezone

from app.mcp.builtin_tools import _ok, _err


def _decode(result: dict) -> dict:
    """Decode the MCP tool result envelope."""
    return json.loads(result["content"][0]["text"])


def test_ok_serializes_datetime_payload():
    """_ok must ISO-normalize datetime values instead of crashing — this is the
    exact shape CronList emits ({"last_run_at": <datetime>})."""
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    result = _ok({"crons": [{"id": "a", "last_run_at": dt}], "count": 1})

    body = _decode(result)
    assert body["success"] is True
    assert body["count"] == 1
    assert body["crons"][0]["last_run_at"].startswith("2026-01-02T03:04:05")


def test_err_serializes_datetime_extra():
    """_err takes **extra kwargs that may carry DB-sourced datetimes too."""
    dt = datetime(2026, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
    result = _err("boom", last_run_at=dt)

    body = _decode(result)
    assert body["success"] is False
    assert body["error"] == "boom"
    assert body["last_run_at"].startswith("2026-06-07T08:09:10")


def test_ok_plain_payload_still_works():
    """No regression for the common all-primitive payload."""
    body = _decode(_ok({"count": 0, "crons": []}))
    assert body == {"success": True, "count": 0, "crons": []}


def test_ai_tools_result_serializes_datetime():
    """ai_tools_sdk._tool_result shares the same bare-json.dumps failure class."""
    from app.mcp.ai_tools_sdk import _tool_result

    dt = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    body = _decode(_tool_result({"success": True, "created_at": dt}))
    assert body["success"] is True
    assert body["created_at"].startswith("2026-03-04T05:06:07")


def test_connector_tools_result_serializes_datetime():
    """connector_tools_sdk._tool_result shares the same bare-json.dumps failure class."""
    from app.mcp.connector_tools_sdk import _tool_result

    dt = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    body = _decode(_tool_result({"success": True, "updated_at": dt}))
    assert body["success"] is True
    assert body["updated_at"].startswith("2026-03-04T05:06:07")
