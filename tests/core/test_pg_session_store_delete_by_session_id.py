"""Bug E regression: deleting a session must delete its store rows.

``sessions`` has NO foreign key into ``session_store_entries`` /
``session_store_summaries``, and the SDK-key delete (``_delete_sync``) needs a
``project_key`` the app cannot reconstruct (the stored key is the on-disk
folder name, which diverges from every cwd-derived key). So the UI delete
removed the sessions row + JSONL and left the FULL transcript orphaned in the
store forever — verified in production: 22 orphaned rows from one deleted
session.

The fix is a session_id-ONLY store purge wired into the delete endpoint's
background cleanup (``_cleanup_session_artifacts``), covering single and batch
delete. These tests run WITHOUT Postgres (RecordingCursor records SQL).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.pg_session_store as pss
from tests.fakes.db import RecordingConn, RecordingCursor


def _patch_db(monkeypatch, rowcounts: List[int]) -> RecordingCursor:
    cur = RecordingCursor(rowcounts=rowcounts)

    @contextmanager
    def _fake_get_db():
        yield RecordingConn(cur)

    monkeypatch.setattr(pss, "get_db", _fake_get_db)
    return cur


class TestDeleteBySessionIdSync:
    def test_deletes_entries_and_summaries_by_session_id_alone(self, monkeypatch):
        """Both tables purged; NO project_key or subpath predicate — every
        subagent transcript and the summary sidecar must go too."""
        cur = _patch_db(monkeypatch, rowcounts=[22, 1])

        removed = pss._delete_session_data_by_session_id_sync("sdk-sess-1")

        assert removed == 23
        deletes = [(sql, params) for sql, params in cur.executed if "DELETE" in sql]
        assert len(deletes) == 2
        tables = {sql.split("FROM")[1].split("WHERE")[0].strip() for sql, _ in deletes}
        assert tables == {"session_store_entries", "session_store_summaries"}
        for sql, params in deletes:
            assert "project_key" not in sql
            assert "subpath" not in sql
            assert params == ("sdk-sess-1",)

    def test_returns_zero_when_nothing_stored(self, monkeypatch):
        _patch_db(monkeypatch, rowcounts=[0, 0])

        assert pss._delete_session_data_by_session_id_sync("ghost") == 0


class TestCleanupSessionArtifactsWiring:
    """The delete endpoint's background cleanup must purge the store rows
    (single AND batch delete both route through _cleanup_session_artifacts)."""

    async def _run_cleanup(self, sdk_sid, purge):
        from app.api.sessions import _cleanup_session_artifacts

        pool = MagicMock()
        pool.remove = AsyncMock()
        with patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch("app.api.sessions.sdk_delete_session", None), \
             patch(
                 "app.core.pg_session_store.delete_session_data_by_session_id",
                 new=purge,
             ):
            await _cleanup_session_artifacts("app-sess-1", sdk_sid, None)

    async def test_purges_store_rows_for_sdk_session(self):
        purge = AsyncMock(return_value=23)

        await self._run_cleanup("sdk-sess-1", purge)

        purge.assert_awaited_once_with("sdk-sess-1")

    async def test_skips_purge_without_sdk_session_id(self):
        purge = AsyncMock()

        await self._run_cleanup(None, purge)

        purge.assert_not_awaited()

    async def test_purge_failure_is_non_fatal(self):
        purge = AsyncMock(side_effect=RuntimeError("pg down"))

        # Must not raise — orphan cleanup is best-effort background work.
        await self._run_cleanup("sdk-sess-1", purge)

        purge.assert_awaited_once()
