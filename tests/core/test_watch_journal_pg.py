"""Journal ordering against private PostgreSQL, never the configured app database."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from typing import Any
from uuid import uuid4

import pytest

from app.db import watches as store
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture(scope="module")
def journal_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("journal ordering requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("watch-journal-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn:
            store.create_schema(conn.cursor())
        yield descriptor


@pytest.mark.parametrize("operation", ["append", "page", "baseline"])
def test_journal_serializes_reversed_commit_attempts(
    journal_cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    # Writer one has allocated a BIGSERIAL but cannot commit yet. Writer two
    # attempts to overtake its commit; a reader must not checkpoint past it.
    connector = "journal-" + uuid4().hex
    inserted = threading.Event()
    release = threading.Event()
    state = threading.local()
    pids: queue.Queue[int] = queue.Queue()

    @contextmanager
    def database() -> Iterator[Any]:
        with closing(pg_connect(journal_cluster)) as conn:
            conn.autocommit = False
            conn.cursor().execute("SET statement_timeout TO 10000")
            if getattr(state, "waiter", False):
                pids.put(conn.get_backend_pid())
            try:
                yield conn
                if getattr(state, "hold", False):
                    inserted.set()
                    assert release.wait(10), "test did not release the first commit"
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    monkeypatch.setattr(store, "get_db", database)

    def first_writer() -> None:
        state.hold = True
        store.record_connector_event(connector, "first", {"platform_chat_id": "123"})

    def waiter() -> Any:
        state.waiter = True
        if operation == "append":
            store.record_connector_event(connector, "second", {"platform_chat_id": "123"})
            return None
        return store.connector_page(connector, "123", {} if operation == "baseline" else {"seq": 0})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_writer)
        second = None
        try:
            assert inserted.wait(10)
            second = pool.submit(waiter)
            pid = pids.get(timeout=5)
            deadline = time.monotonic() + 5
            with closing(pg_connect(journal_cluster)) as observer:
                while True:
                    c = observer.cursor()
                    c.execute(
                        "SELECT 1 FROM pg_locks WHERE pid=%s "
                        "AND locktype='advisory' AND NOT granted", (pid,),
                    )
                    if c.fetchone():
                        break
                    assert time.monotonic() < deadline, "journal operation bypassed ingress lock"
                    time.sleep(0.01)
                # This is the attempted reversed commit: without the lock the
                # second insertion/read finishes while the first is invisible.
                assert not second.done()
        finally:
            release.set()
        first.result(timeout=15)
        assert second is not None
        answer = second.result(timeout=15)
    events, cursor = store.connector_page(connector, "123", {"seq": 0})
    expected = ["first", "second"] if operation == "append" else ["first"]
    assert [key for key, _ in events] == expected
    if operation == "page":
        assert answer == (events, cursor)
    if operation == "baseline":
        assert answer == ([], cursor)
    assert store.connector_page(connector, "123", cursor) == ([], cursor)
