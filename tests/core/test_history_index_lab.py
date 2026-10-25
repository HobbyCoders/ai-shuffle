"""Diagnostic WAL ranges are exact; sampled statistics are not substituted for them."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.db import history_index_lab as lab


def test_wal_summary_retains_exact_range_and_optional_record_dump(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    start = {"lsn": "0/100", "wal_bytes": 100, "wal_fpi": 5}
    end = {"lsn": "0/200", "wal_bytes": 300, "wal_fpi": 8}
    monkeypatch.setattr(lab, "wal_snapshot", lambda: end)
    calls: list[tuple[str, Any]] = []

    class Cursor:
        def execute(self, sql: str, args: Any) -> None:
            calls.append((sql, args))

        def fetchone(self) -> dict[str, int]:
            return {"bytes": 256}

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    @contextmanager
    def database() -> Iterator[Connection]:
        yield Connection()

    def decode(command: list[str], **kwargs: Any) -> SimpleNamespace:
        assert command == ["pg_waldump", "--path", "/disposable/wal", "--start", "0/100",
                           "--end", "0/200", "--stats=record"]
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 120
        return SimpleNamespace(stdout="Gin/INSERT record and FPI summary")

    monkeypatch.setattr(lab.base, "get_db", database)
    monkeypatch.setenv("HANDOFF_LAB_WAL_DIR", "/disposable/wal")
    monkeypatch.setenv("HANDOFF_LAB_ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(lab.subprocess, "run", decode)
    result = lab.wal_summary(start, "catchup")
    assert result == {"wal_bytes": 200, "wal_fpi": 3, "lsn_bytes": 256,
                      "start_lsn": "0/100", "end_lsn": "0/200"}
    assert calls[0][1] == ("0/200", "0/100")
    assert (tmp_path / "catchup-waldump.txt").read_text() == (
        "Gin/INSERT record and FPI summary"
    )
    assert lab.json.loads((tmp_path / "catchup-wal.json").read_text()) == result
