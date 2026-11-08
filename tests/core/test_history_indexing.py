"""History maintenance never bypasses the database router or foreground gate."""
from __future__ import annotations

from typing import Any
import pytest

from app.db import history_indexing as hi
from app.maintenance.session_recall_cache import ManagedDataDirectoryError


def test_online_migration_cannot_open_a_database_outside_the_pool(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_connect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("migration bypassed the test database router")

    monkeypatch.setattr(hi.psycopg2, "connect", forbidden_connect)
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        hi.migrate()


def test_active_turn_gate_prevents_background_work(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hi.get_db() as conn:
        conn.cursor().execute("INSERT INTO system_settings(key,value) VALUES (%s,'building')",
                              (hi.KEY,))

    def forbidden_migration() -> bool:
        pytest.fail("migration ran while the worker had an active turn")

    monkeypatch.setattr(hi, "migrate", forbidden_migration)
    assert not hi.run_pass(can_work=lambda: False)


def test_migration_stamp_is_worker_local() -> None:
    from app.network.policy import is_shared_setting_key

    assert not is_shared_setting_key(hi.KEY)


@pytest.mark.parametrize("recall_ready", [True, False])
def test_background_indexing_starts_itself_after_recall_is_ready(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch, recall_ready: bool,
) -> None:
    from app.db import session_recall

    migrations: list[int] = []

    def migrate() -> bool:
        migrations.append(1)
        return False

    monkeypatch.setattr(session_recall, "ready", lambda _: recall_ready)
    monkeypatch.setattr(hi, "migrate", migrate)
    assert hi.run_pass() is not recall_ready
    assert len(migrations) == int(recall_ready)
    with hi.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key=%s", (hi.KEY,))
        marker = cur.fetchone()
        assert marker == ({"value": "building"} if recall_ready else None)
        # Building is not ready: imports stay synchronous until the swap finishes.
        assert not hi.enabled(cur)


@pytest.mark.parametrize(
    ("waiting", "running", "free"),
    [(0, False, True), (1, False, False), (0, True, False)],
)
def test_waiting_and_running_turns_both_hold_background_work(
    monkeypatch: pytest.MonkeyPatch, waiting: int, running: bool, free: bool,
) -> None:
    from app.core import start_admission, startup, streaming_status

    monkeypatch.setattr(streaming_status, "has_any_active_run", lambda: running)
    monkeypatch.setattr(start_admission.StartAdmission, "queued", property(lambda _: waiting))
    assert startup._no_foreground_turn() is free


def test_pending_lists_fit_every_platform_postgresql() -> None:
    # Windows builds of PostgreSQL cap kilobyte options at INT_MAX / 1024.
    assert 1_048_576 <= hi.HISTORY_PENDING_KB <= 2_097_151


def test_merge_waits_for_the_move_to_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_database() -> Any:
        pytest.fail("merge opened a database while the move was still writing")

    monkeypatch.setattr(hi, "get_db", forbidden_database)
    monkeypatch.setattr(hi, "_unmerged_at", hi.time.monotonic())
    assert not hi.flush_pending()
    assert not hi.flush_pending(quiet=0, can_work=lambda: False)


def test_nothing_to_merge_opens_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_database() -> Any:
        pytest.fail("idle pass reopened a connection with nothing to merge")

    monkeypatch.setattr(hi, "get_db", forbidden_database)
    monkeypatch.setattr(hi, "_unmerged_at", None)
    assert hi.flush_pending()


@pytest.mark.parametrize("budget", ["rows", "bytes", "time", "foreground"])
def test_promotion_budget_is_global_and_yields_between_rows(
    monkeypatch: pytest.MonkeyPatch, budget: str,
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    updates: list[str] = []

    class Cursor:
        def execute(self, sql: str, args: Any = None) -> None:
            if sql.startswith("UPDATE"):
                updates.append(sql)

        def fetchall(self) -> list[dict[str, int]]:
            return [{"id": 1, "bytes": 10}, {"id": 2, "bytes": 10}]

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    @contextmanager
    def database() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(hi, "get_db", database)
    monkeypatch.setattr(hi, "enabled", lambda _: True)
    monkeypatch.setattr(hi, "TABLES", {"first": ("id", "body"), "second": ("id", "body")})
    monkeypatch.setattr(hi.time, "monotonic", lambda: float(len(updates)))
    count = hi.promote_batch(
        rows=2 if budget == "rows" else 8,
        byte_budget=10 if budget == "bytes" else 128_000,
        seconds=1 if budget == "time" else 100,
        can_work=lambda: budget != "foreground" or not updates,
    )
    assert count == (2 if budget == "rows" else 1)
    assert len(updates) == count
    assert all("UPDATE first " in sql for sql in updates)


def test_no_promotion_opens_database_during_active_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_database() -> Any:
        pytest.fail("promotion borrowed a connection while foreground work was active")

    monkeypatch.setattr(hi, "get_db", forbidden_database)
    assert hi.promote_batch(can_work=lambda: False) == 0


GIB = 1024 ** 3


@pytest.mark.parametrize(("volumes", "safe"), [
    # A 4 GiB index on one volume needs a copy, its WAL and the reserve.
    ([("data", 1, 1 * GIB, 4 * GIB), ("WAL", 1, 1 * GIB, 4 * GIB)], False),
    ([("data", 1, 8 * GIB, 4 * GIB), ("WAL", 1, 8 * GIB, 4 * GIB)], False),
    ([("data", 1, 9 * GIB, 4 * GIB), ("WAL", 1, 9 * GIB, 4 * GIB)], True),
    # On separate volumes each pays only its own share, and either can be short.
    ([("data", 1, 5 * GIB, 4 * GIB), ("WAL", 2, 5 * GIB, 4 * GIB)], True),
    ([("data", 1, 5 * GIB, 4 * GIB), ("WAL", 2, 2 * GIB, 4 * GIB)], False),
])
def test_rebuild_needs_room_for_the_copy_its_wal_and_a_reserve(
    volumes: list[tuple[str, int, int, int]], safe: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hi, "_volumes", lambda cur, name: volumes)
    problem = hi._space_problem(None, "idx")
    assert (problem is None) == safe
    if problem:
        assert "free some disk space" in problem


def test_rebuild_is_refused_when_the_disk_cannot_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unmeasurable(cur: object, name: str) -> list[tuple[str, int, int, int]]:
        raise ManagedDataDirectoryError("no managed cluster")

    monkeypatch.setattr(hi, "_volumes", unmeasurable)
    assert "cannot be measured" in hi._space_problem(None, "idx")


def test_folded_text_keeps_its_separators() -> None:
    from app.core.provider.fluid_pg_store import _fold_events

    def event(event_type: str, **payload: object) -> dict[str, object]:
        return {"event_type": event_type, "payload": payload, "created_at": None}

    rows = [event("assistant.text_delta", text="a"), event("assistant.text_delta", text="b"),
            event("message.assistant", content="c", kind="text"),
            event("assistant.text_delta", text="d"),
            event("message.user", content="q"),
            event("message.assistant", content="r", kind="text")]
    assert [(m.role, m.text) for m in _fold_events(rows)] == [
        ("assistant", "ab\nc\nd"), ("user", "q"), ("assistant", "r"),
    ]
