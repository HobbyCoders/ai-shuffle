"""A DB-API cursor/connection pair that records SQL instead of running it."""

from typing import Any


class RecordingCursor:
    """Records every ``(sql, params)`` it executes.

    ``rows`` are returned by ``fetchall``/``fetchone`` (default: none).
    ``rowcounts``, when given, sets ``rowcount`` for each successive
    ``execute`` (0 once exhausted); without it the cursor has no
    ``rowcount`` attribute at all.
    """

    def __init__(self, rows: list[dict] | None = None,
                 rowcounts: list[int] | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._rowcounts = rowcounts
        self.executed: list[tuple] = []  # (sql, params)
        if rowcounts is not None:
            self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if self._rowcounts is not None:
            self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None


class RecordingConn:
    """A connection whose ``cursor()`` always returns the one given cursor."""

    def __init__(self, cursor: RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self, *args, **kwargs) -> RecordingCursor:
        return self._cursor
