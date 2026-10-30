"""Test-only SQL/ASGI byte counters; no production hooks or timing thresholds.

``count_statements`` is the existing SQLite trace counter, shared rather than
copied from test_hot_path_query_counts. ``measure_sql`` adds request-scoped
counting on the PostgreSQL wrapper AND that same shim. Use a bare router app
(no lifespan) to keep warm-up/background work outside a ratchet.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

_DML = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.IGNORECASE)


@contextlib.contextmanager
def count_statements(raw_conn):
    """Collect DML statements from one SQLite connection (legacy count helper)."""
    statements: list[str] = []

    def trace(sql: str) -> None:
        if _DML.match(sql):
            statements.append(sql)

    raw_conn.set_trace_callback(trace)
    try:
        yield statements
    finally:
        raw_conn.set_trace_callback(None)


@dataclass
class RequestCost:
    statements: list[str] = field(default_factory=list)
    response_bytes: int = 0
    status: int | None = None
    complete: bool = False
    active: bool = True

    @property
    def sql_count(self) -> int:
        return len(self.statements)


_current: contextvars.ContextVar[RequestCost | None] = contextvars.ContextVar(
    "perf_request_cost", default=None)


def _record(sql: Any) -> None:
    cost = _current.get()
    if cost is not None and cost.active:
        # SQL text only, never bound parameters; failures count as attempts too.
        cost.statements.append(str(sql))


class _Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, *args, **kwargs):
        _record(sql)
        result = self._cursor.execute(sql, *args, **kwargs)
        return self if result is self._cursor else result

    def executemany(self, sql, params, *args, **kwargs):
        # Count statements, not Python calls: a parameter set is one execution.
        # This also preserves laziness for callers that pass a generator.
        def rows():
            for row in params:
                _record(sql)
                yield row

        result = self._cursor.executemany(sql, rows(), *args, **kwargs)
        return self if result is self._cursor else result

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):
        if name == "_cursor":
            object.__setattr__(self, name, value)
        else:
            setattr(self._cursor, name, value)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)


@contextlib.contextmanager
def measure_sql(connection_types=None):
    """Temporarily wrap cursor creation; combine with ``RequestMeasurements``.

    Contextvars follow ``run_sync``/AnyIO thread offloads. Unrelated requests,
    work outside an HTTP request, and work after its end cannot inflate counts.
    Avoid nested installs; use one wrapper around the TestClient's lifetime.
    """
    if connection_types is None:
        from app.db.base import _PgDictConnection
        from tests.pg_shim import ShimConnection

        connection_types = (_PgDictConnection, ShimConnection)

    def wrap(original):
        def cursor(connection, *args, **kwargs):
            return _Cursor(original(connection, *args, **kwargs))
        return cursor

    with contextlib.ExitStack() as stack:
        for cls in connection_types:
            stack.enter_context(patch.object(cls, "cursor", wrap(cls.cursor)))
        yield


class RequestMeasurements:
    """Pure-ASGI wrapper: body bytes are counted without buffering a stream.

    Attach around the app whose response you want to measure. Counts are bytes
    at THIS boundary (before gzip if gzip is outside it), not HTTP wire headers.
    ``requests`` keeps objects in request-start order; disconnected/error paths
    retain partial bytes and ``complete=False``. Never use this as live middleware.
    """
    def __init__(self, app):
        self.app = app
        self.requests: list[RequestCost] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cost = RequestCost()
        self.requests.append(cost)
        token = _current.set(cost)

        async def measured_send(message):
            if message["type"] == "http.response.start":
                cost.status = message["status"]
            elif message["type"] == "http.response.body":
                cost.response_bytes += len(message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body":
                cost.complete = not message.get("more_body", False)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            cost.active = False
            _current.reset(token)
