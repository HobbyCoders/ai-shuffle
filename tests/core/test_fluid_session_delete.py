"""Deleting a fluid session must purge the fluid_* rows AND the CLI JSONL.

``sessions`` has no foreign key into ``fluid_sessions`` /
``fluid_session_nodes`` / ``fluid_session_events``, so the delete endpoint's
background cleanup left a fluid-native session's entire transcript orphaned.
Worse, it handed the ``ses_<hex12>`` id straight to the SDK's JSONL delete,
which validates a UUID — every fluid delete logged ``Invalid session_id`` and
the delegated-CLI transcript file (named by ``fluid_sessions.cli_session_id``,
a real uuid) leaked too.

The fix routes on the id format. These tests run WITHOUT Postgres (FakeCursor
records SQL).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.provider.fluid_transcript as ft

FLUID_SID = "ses_7f41e2151f6b"
CLI_SID = "6f5f6e0c-6c22-4a41-9a2e-7d1d9c6b1a10"
LEGACY_SID = "0c1f2b3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


class FakeCursor:
    def __init__(self, rowcounts: List[int]) -> None:
        self._rowcounts = rowcounts
        self.executed: List[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0

    def fetchone(self) -> Optional[dict]:
        return None

    def fetchall(self) -> List[dict]:
        return []


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return self._cursor


def _patch_db(monkeypatch, rowcounts: List[int]) -> FakeCursor:
    """fluid_transcript lazy-imports get_db per call — patch it at the source."""
    import app.db.base as db_base

    cur = FakeCursor(rowcounts)

    @contextmanager
    def _fake_get_db():
        yield FakeConn(cur)

    monkeypatch.setattr(db_base, "get_db", _fake_get_db)
    return cur


class TestDeleteFluidSessionData:
    def test_purges_all_three_fluid_tables(self, monkeypatch):
        """Nodes, events and the session row itself — the row LAST, since
        ``cli_session_id`` lives on it and callers read it first."""
        cur = _patch_db(monkeypatch, rowcounts=[41, 12, 1])

        removed = ft.delete_fluid_session_data(FLUID_SID)

        assert removed == 54
        deletes = [(sql, params) for sql, params in cur.executed if "DELETE" in sql]
        assert len(deletes) == 3
        tables = [sql.split("FROM")[1].split("WHERE")[0].strip() for sql, _ in deletes]
        assert set(tables) == {
            "fluid_session_nodes",
            "fluid_session_events",
            "fluid_sessions",
        }
        assert tables[-1] == "fluid_sessions"
        for _sql, params in deletes:
            assert params == (FLUID_SID,)

    def test_legacy_uuid_touches_no_sql(self, monkeypatch):
        cur = _patch_db(monkeypatch, rowcounts=[9, 9, 9])

        assert ft.delete_fluid_session_data(LEGACY_SID) == 0
        assert cur.executed == []


class TestCleanupSessionArtifactsFluidRouting:
    """``_cleanup_session_artifacts`` runs for single AND batch delete."""

    async def _run_cleanup(
        self,
        sdk_sid,
        *,
        cli_sid=None,
        purge=None,
        sdk_delete=None,
        project_id=None,
    ):
        from app.api.sessions import _cleanup_session_artifacts

        pool = MagicMock()
        pool.remove = AsyncMock()
        purge = purge if purge is not None else MagicMock(return_value=54)
        with patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch("app.api.sessions.sdk_delete_session", sdk_delete), \
             patch(
                 "app.core.pg_session_store.delete_session_data_by_session_id",
                 new=AsyncMock(return_value=0),
             ), \
             patch(
                 "app.core.provider.fluid_transcript.get_cli_session_id",
                 MagicMock(return_value=cli_sid),
             ), \
             patch(
                 "app.core.provider.fluid_transcript.delete_fluid_session_data",
                 purge,
             ):
            await _cleanup_session_artifacts("app-sess-1", sdk_sid, project_id)
        return purge

    async def test_fluid_session_deletes_rows_and_cli_jsonl(self):
        """The SDK delete only ever sees the uuid handle — never the ses_ id."""
        sdk_delete = MagicMock()

        purge = await self._run_cleanup(
            FLUID_SID, cli_sid=CLI_SID, sdk_delete=sdk_delete
        )

        purge.assert_called_once_with(FLUID_SID)
        sdk_delete.assert_called_once_with(session_id=CLI_SID, directory=None)

    async def test_fluid_session_without_cli_handle_skips_sdk_delete(self):
        sdk_delete = MagicMock()

        purge = await self._run_cleanup(
            FLUID_SID, cli_sid=None, sdk_delete=sdk_delete
        )

        purge.assert_called_once_with(FLUID_SID)
        sdk_delete.assert_not_called()

    async def test_legacy_uuid_keeps_the_old_path(self):
        sdk_delete = MagicMock()
        purge = MagicMock()

        await self._run_cleanup(LEGACY_SID, purge=purge, sdk_delete=sdk_delete)

        purge.assert_not_called()
        sdk_delete.assert_called_once_with(session_id=LEGACY_SID, directory=None)

    async def test_purge_runs_without_the_sdk_helper(self):
        """``sdk_delete_session`` is an optional import — None must not crash."""
        purge = await self._run_cleanup(FLUID_SID, cli_sid=CLI_SID, sdk_delete=None)

        purge.assert_called_once_with(FLUID_SID)

    async def test_missing_jsonl_is_not_an_error(self):
        sdk_delete = MagicMock(side_effect=FileNotFoundError("gone"))

        purge = await self._run_cleanup(
            FLUID_SID, cli_sid=CLI_SID, sdk_delete=sdk_delete
        )

        purge.assert_called_once_with(FLUID_SID)
        sdk_delete.assert_called_once()
