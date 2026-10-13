"""Bug C regression: lazy store backfill on a main-transcript store miss.

The postgres_only cutover assumed ``scripts/backfill_session_store.py`` had been
run first. On installs where it wasn't, every pre-cutover session has its
transcript in the on-disk JSONL (and the legacy ``session_messages`` table) but
ZERO ``session_store_entries`` rows — so under postgres_only the chat rendered
empty and SDK resume got blank context.

The fix backfills the store from the local JSONL on a store miss, guarded so it
can never fire for live or already-stored sessions:

  1. store count == 0 for the session (cheap, indexed);
  2. NO ``session_store_summaries`` row — the summary row is the tombstone
     proving the session WAS store-recorded (a rewound-to-empty transcript must
     not be resurrected from a stale JSONL);
  3. the main JSONL exists, has entries, and is idle (mtime older than the
     freshness window) so a first-turn eager flush can't race a double-insert;
  4. a pg advisory xact lock + in-lock count re-check serializes concurrent
     backfillers (``session_store_entries`` has NO unique constraint).

These tests run WITHOUT Postgres: the DB seams are monkeypatched module
functions, ``transaction()`` is faked, and the JSONL fixtures are real temp
files (exercising the real ``parse_jsonl_file`` and mtime logic).
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app.core.pg_session_store as pss
from tests.fakes.db import RecordingConn, RecordingCursor


@contextmanager
def _fake_transaction(cur: RecordingCursor):
    yield RecordingConn(cur)


SID = "11111111-2222-3333-4444-555555555555"


def _write_jsonl(path: Path, entries: list[dict], *, age_s: float = 3600.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    old = time.time() - age_s
    os.utime(path, (old, old))


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    d = tmp_path / "-workspace-my-project"
    d.mkdir()
    return d


@pytest.fixture
def seams(monkeypatch, project_dir: Path):
    """Patch every DB/JSONL seam; return a namespace of the mocks.

    Default state: store miss (count 0, no summary row), main JSONL with two
    idle entries, no subagents, lock acquired via a recorded fake transaction.
    """
    main_path = project_dir / f"{SID}.jsonl"
    _write_jsonl(main_path, [{"uuid": "u0"}, {"uuid": "u1"}])

    cur = RecordingCursor()
    mocks = MagicMock()
    mocks.cursor = cur
    mocks.main_path = main_path

    monkeypatch.setattr(pss, "_store_version_by_session_id_sync", mocks.version)
    mocks.version.return_value = (0, 0)
    monkeypatch.setattr(pss, "_has_summary_by_session_id_sync", mocks.has_summary)
    mocks.has_summary.return_value = False
    monkeypatch.setattr(pss, "_count_entries_sync", mocks.count)
    mocks.count.return_value = 0
    monkeypatch.setattr(pss, "_backfill_entries_sync", mocks.backfill)
    mocks.backfill.side_effect = lambda key, entries, **kw: len(entries)
    monkeypatch.setattr(pss, "transaction", lambda: _fake_transaction(cur))

    import app.core.jsonl_parser as jp

    monkeypatch.setattr(jp, "get_session_jsonl_path", mocks.jsonl_path)
    mocks.jsonl_path.return_value = main_path
    monkeypatch.setattr(jp, "get_agent_jsonl_paths", mocks.agent_paths)
    mocks.agent_paths.return_value = {}
    return mocks


class TestGuards:
    def test_skips_when_store_has_rows(self, seams):
        """Guard 1: any existing store rows -> no backfill, no disk access."""
        seams.version.return_value = (5, 42)

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.jsonl_path.assert_not_called()
        seams.backfill.assert_not_called()

    def test_skips_when_summary_row_exists(self, seams):
        """Guard 2 (tombstone): a summaries row means the session WAS
        store-recorded — e.g. rewound to empty. A stale JSONL must NOT
        resurrect the rewound-away transcript."""
        seams.has_summary.return_value = True

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.jsonl_path.assert_not_called()
        seams.backfill.assert_not_called()

    def test_skips_when_no_jsonl(self, seams):
        seams.jsonl_path.return_value = None

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.backfill.assert_not_called()

    def test_skips_when_jsonl_empty(self, seams, project_dir):
        empty = project_dir / f"{SID}.jsonl"
        empty.write_text("", encoding="utf-8")
        old = time.time() - 3600
        os.utime(empty, (old, old))

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.backfill.assert_not_called()

    def test_skips_when_jsonl_recently_modified(self, seams, project_dir):
        """Guard 3: a fresh mtime means the CLI may still be writing this
        transcript (first turn of a NEW session before the eager store flush
        lands) — backfilling now would double-insert those entries."""
        _write_jsonl(project_dir / f"{SID}.jsonl", [{"uuid": "u0"}], age_s=1.0)

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.backfill.assert_not_called()

    def test_in_lock_recheck_aborts(self, seams):
        """Guard 5: a concurrent backfiller that committed first is detected by
        the in-lock re-check — no duplicate insert (no unique constraint
        exists to catch it otherwise)."""
        seams.version.side_effect = [(0, 0), (3, 30)]

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.backfill.assert_not_called()

    def test_in_lock_recheck_is_session_wide_across_project_keys(self, seams):
        """The concurrent backfiller may be ANOTHER install whose project_key
        derivation differs. A per-key recheck (this install's key has 0 rows)
        would insert the whole transcript AGAIN — and reads merge by
        session_id alone, so the history would render doubled. The re-check
        must count by session_id, ignoring project_key."""
        seams.version.side_effect = [(0, 0), (210, 999)]
        seams.count.return_value = 0  # this install's project_key: no rows

        assert pss._lazy_backfill_from_jsonl_sync(SID) == 0
        seams.backfill.assert_not_called()


class TestBackfill:
    def test_backfills_main_transcript(self, seams, project_dir):
        written = pss._lazy_backfill_from_jsonl_sync(SID)

        assert written == 2
        (key, entries) = seams.backfill.call_args_list[0].args[:2]
        assert key == {"project_key": project_dir.name, "session_id": SID}
        assert [e["uuid"] for e in entries] == ["u0", "u1"]

    def test_takes_advisory_xact_lock_on_session_id(self, seams):
        pss._lazy_backfill_from_jsonl_sync(SID)

        lock_calls = [
            (sql, params)
            for sql, params in seams.cursor.executed
            if "pg_advisory_xact_lock" in sql
        ]
        assert lock_calls, "backfill must serialize via a pg advisory xact lock"
        assert lock_calls[0][1] == (SID,)

    def test_threads_lock_connection_into_db_helpers(self, seams, project_dir):
        """Re-checks and inserts must run on the SAME connection that
        holds the advisory xact lock, or the serialization is meaningless."""
        agent_path = project_dir / SID / "subagents" / "agent-a1.jsonl"
        _write_jsonl(agent_path, [{"uuid": "s0"}])
        seams.agent_paths.return_value = {"a1": agent_path}

        pss._lazy_backfill_from_jsonl_sync(SID)

        # The session-wide in-lock recheck (last version call) holds the lock
        # connection; the pre-lock guard (first call) deliberately does not.
        in_lock_version = seams.version.call_args_list[-1]
        conn = in_lock_version.kwargs.get("_conn")
        assert conn is not None and conn.cursor() is seams.cursor
        assert seams.count.call_args_list, "subagent recheck should have run"
        for call in seams.count.call_args_list + seams.backfill.call_args_list:
            conn = call.kwargs.get("_conn")
            assert conn is not None and conn.cursor() is seams.cursor

    def test_backfills_subagent_transcripts(self, seams, project_dir):
        agent_path = project_dir / SID / "subagents" / "agent-a1.jsonl"
        _write_jsonl(agent_path, [{"uuid": "s0"}])
        seams.agent_paths.return_value = {"a1": agent_path}

        written = pss._lazy_backfill_from_jsonl_sync(SID)

        assert written == 3
        sub_key = seams.backfill.call_args_list[1].args[0]
        assert sub_key == {
            "project_key": project_dir.name,
            "session_id": SID,
            "subpath": "subagents/agent-a1",
        }

    def test_wrapper_never_raises(self, seams):
        """The public wrapper guards every read path — a backfill failure must
        degrade to 'no heal', never break the read."""
        seams.version.side_effect = RuntimeError("pg down")

        assert pss.lazy_backfill_session_from_jsonl(SID) == 0


class TestLoadSyncSelfHeal:
    """SDK resume seam: _load_sync returning None hands the SDK a blank
    context. A main-transcript miss must attempt the lazy backfill and
    re-read."""

    def test_main_miss_heals_and_rereads(self, monkeypatch):
        loads: list = [[], [{"uuid": "u0"}]]
        monkeypatch.setattr(
            pss, "_load_entries_by_session_id_sync", lambda sid, sp: loads.pop(0)
        )
        heal = MagicMock(return_value=2)
        monkeypatch.setattr(pss, "lazy_backfill_session_from_jsonl", heal)

        rows = pss._load_sync({"project_key": "pk", "session_id": SID})

        assert rows == [{"uuid": "u0"}]
        heal.assert_called_once_with(SID)

    def test_main_miss_without_heal_stays_none(self, monkeypatch):
        monkeypatch.setattr(
            pss, "_load_entries_by_session_id_sync", lambda sid, sp: []
        )
        heal = MagicMock(return_value=0)
        monkeypatch.setattr(pss, "lazy_backfill_session_from_jsonl", heal)

        assert pss._load_sync({"project_key": "pk", "session_id": SID}) is None
        heal.assert_called_once()

    def test_subpath_miss_does_not_backfill(self, monkeypatch):
        monkeypatch.setattr(
            pss, "_load_entries_by_session_id_sync", lambda sid, sp: []
        )
        heal = MagicMock()
        monkeypatch.setattr(pss, "lazy_backfill_session_from_jsonl", heal)

        key = {"project_key": "pk", "session_id": SID, "subpath": "subagents/agent-1"}
        assert pss._load_sync(key) is None
        heal.assert_not_called()


class TestParseViaStoreHook:
    """HTTP read seam: parse_session_history_via_store serves BOTH
    GET /sessions/{id} and /live under postgres_only — a store miss must
    trigger the lazy backfill BEFORE parsing."""

    def _run(self, version):
        from app.api.sessions_helpers import parse_session_history_via_store

        with patch(
            "app.core.pg_session_store._store_version_by_session_id_sync",
            return_value=version,
        ), patch(
            "app.core.pg_session_store.lazy_backfill_session_from_jsonl"
        ) as heal, patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=([], {}),
        ) as parse:
            parse_session_history_via_store(SID, "/workspace")
        return heal, parse

    def test_store_miss_triggers_backfill_before_parse(self):
        heal, parse = self._run(version=(0, 0))
        heal.assert_called_once_with(SID, "/workspace")
        parse.assert_called_once()

    def test_populated_store_skips_backfill(self):
        heal, parse = self._run(version=(7, 99))
        heal.assert_not_called()
        parse.assert_called_once()
