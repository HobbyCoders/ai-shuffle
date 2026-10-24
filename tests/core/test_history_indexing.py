"""History maintenance never bypasses the database router or foreground gate."""
from __future__ import annotations

from typing import Any
import pytest

from app.db import history_indexing as hi


def test_online_migration_cannot_open_a_database_outside_the_pool(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_connect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("migration bypassed the test database router")

    monkeypatch.setattr(hi.psycopg2, "connect", forbidden_connect)
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        hi.migrate()


def test_active_turn_gate_prevents_background_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_migration() -> bool:
        pytest.fail("migration ran while the worker had an active turn")

    monkeypatch.setattr(hi, "migrate", forbidden_migration)
    assert not hi.run_pass(can_work=lambda: False)


def test_migration_stamp_is_worker_local() -> None:
    from app.network.policy import is_shared_setting_key

    assert not is_shared_setting_key(hi.KEY)


def test_background_indexing_is_off_without_opt_in(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_migration() -> bool:
        pytest.fail("experimental migration ran without opt-in")

    monkeypatch.setattr(hi, "migrate", forbidden_migration)
    assert hi.run_pass()


def test_cli_without_apply_never_opens_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import base
    from app.maintenance.history_indexing import main

    def forbidden_init(*args: Any, **kwargs: Any) -> None:
        pytest.fail("CLI opened a database without --apply")

    monkeypatch.setattr(base, "init_pool", forbidden_init)
    assert main([]) == 0


def test_cli_requests_restart_safe_worker_local_migration(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import base, session_recall
    from app.maintenance.history_indexing import main

    monkeypatch.setattr(base, "init_pool", lambda **kwargs: None)
    monkeypatch.setattr(base, "close_pool", lambda: None)
    monkeypatch.setattr(session_recall, "ready", lambda _: True)
    assert main(["--apply"]) == 0
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key=%s", (hi.KEY,))
        assert cur.fetchone()["value"] == "building"
        assert not hi.enabled(cur)
    monkeypatch.setattr(hi, "migrate", lambda: False)
    assert not hi.run_pass()
