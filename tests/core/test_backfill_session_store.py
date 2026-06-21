"""Unit tests for scripts/backfill_session_store.py.

These run WITHOUT Postgres. The real ``PgSessionStore`` is replaced by a
``FakeStore`` that captures appended rows in memory and reproduces the store's
idempotency contract (skip a (project_key, session_id, subpath) that already
has rows, since ``session_store_entries`` has no unique constraint). JSONL is
read from a real temporary ``~/.claude/projects`` tree, mirroring
``tests/core/test_jsonl_parser.py``.

Coverage:
* main-only session  -> one main key (subpath None) with all main rows
* nested subagents    -> main key + one key per ``subagents/agent-<id>``
* idempotency         -> running twice writes rows once
* missing / unreadable JSONL -> skipped, never fatal
* --dry-run           -> nothing written
* verify mode reuses app/core/session_store_verify.py helpers
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from scripts import backfill_session_store as bf


# ---------------------------------------------------------------------------
# Fake store — captures rows, honors the idempotency contract.
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for PgSessionStore.backfill / count_entries."""

    def __init__(self) -> None:
        # key tuple (project_key, session_id, subpath) -> list[entry dict]
        self.rows: Dict[Tuple[str, str, Optional[str]], List[Dict[str, Any]]] = {}
        self.backfill_calls: List[Tuple[Tuple, int]] = []

    @staticmethod
    def _tuple(key: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
        sp = key.get("subpath")
        return (key["project_key"], key["session_id"], sp if sp else None)

    async def count_entries(self, key: Dict[str, Any]) -> int:
        return len(self.rows.get(self._tuple(key), []))

    async def backfill(
        self,
        key: Dict[str, Any],
        entries: List[Dict[str, Any]],
        *,
        skip_if_present: bool = True,
        batch_size: int = 500,
    ) -> int:
        if not entries:
            return 0
        t = self._tuple(key)
        self.backfill_calls.append((t, len(entries)))
        if skip_if_present and self.rows.get(t):
            return 0  # idempotent skip — already populated
        self.rows.setdefault(t, []).extend(dict(e) for e in entries)
        return len(entries)


# ---------------------------------------------------------------------------
# Fixtures: a tmp ~/.claude/projects tree + settings patch.
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


@pytest.fixture
def projects_tree():
    """Yield (projects_dir, project_dir) under a -workspace project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir) / "projects"
        project_dir = projects_dir / "-workspace"
        project_dir.mkdir(parents=True)
        yield projects_dir, project_dir


@pytest.fixture
def patched_jsonl(projects_tree, monkeypatch):
    """Point jsonl_parser at the tmp tree and the script's working_dir resolver
    at /workspace (so project_key resolves to '-workspace')."""
    projects_dir, project_dir = projects_tree
    import app.core.jsonl_parser as jp

    class _S:
        get_claude_projects_dir = projects_dir

    monkeypatch.setattr(jp, "settings", _S())
    # The script resolves working_dir via the DB; stub it out.
    monkeypatch.setattr(bf, "_resolve_working_dir", lambda session: "/workspace")
    return projects_dir, project_dir


def _make_session(sid: str) -> Dict[str, Any]:
    return {"id": f"s-{sid}", "sdk_session_id": sid, "project_id": None,
            "worktree_id": None}


def _main_entries(sid: str, n: int = 3) -> List[Dict[str, Any]]:
    return [
        {"type": "user", "uuid": f"{sid}-u{i}", "sessionId": sid,
         "message": {"role": "user", "content": f"hello {i}"}}
        for i in range(n)
    ]


def _agent_entries(sid: str, agent_id: str, n: int = 2) -> List[Dict[str, Any]]:
    return [
        {"type": "assistant", "uuid": f"{sid}-{agent_id}-{i}", "sessionId": sid,
         "message": {"role": "assistant", "content": [{"type": "text",
                                                        "text": f"sub {i}"}]}}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_main_only_writes_subpath_none(patched_jsonl):
    """A main-only session writes one main key (subpath None) and no subagents."""
    _projects_dir, project_dir = patched_jsonl
    sid = "11111111-1111-1111-1111-111111111111"
    _write_jsonl(project_dir / f"{sid}.jsonl", _main_entries(sid, 3))

    store = FakeStore()
    res = asyncio.run(
        bf._backfill_one(store, _make_session(sid), batch_size=500, dry_run=False)
    )

    assert res.status == "written"
    assert res.project_key == "-workspace"
    assert res.main_rows == 3
    assert res.subagent_count == 0
    # Exactly one captured key, with subpath None.
    keys = list(store.rows.keys())
    assert keys == [("-workspace", sid, None)]
    assert len(store.rows[("-workspace", sid, None)]) == 3


def test_nested_subagents_write_main_plus_each_subpath(patched_jsonl):
    """Nested subagents produce the main key plus one key per agent subpath."""
    _projects_dir, project_dir = patched_jsonl
    sid = "22222222-2222-2222-2222-222222222222"
    _write_jsonl(project_dir / f"{sid}.jsonl", _main_entries(sid, 2))
    subagents = project_dir / sid / "subagents"
    _write_jsonl(subagents / "agent-aaa.jsonl", _agent_entries(sid, "aaa", 2))
    _write_jsonl(subagents / "agent-bbb.jsonl", _agent_entries(sid, "bbb", 3))

    store = FakeStore()
    res = asyncio.run(
        bf._backfill_one(store, _make_session(sid), batch_size=500, dry_run=False)
    )

    assert res.status == "written"
    assert res.main_rows == 2
    assert res.subagent_count == 2
    assert res.subagent_rows == 5  # 2 + 3

    assert ("-workspace", sid, None) in store.rows
    assert ("-workspace", sid, "subagents/agent-aaa") in store.rows
    assert ("-workspace", sid, "subagents/agent-bbb") in store.rows
    assert len(store.rows[("-workspace", sid, "subagents/agent-aaa")]) == 2
    assert len(store.rows[("-workspace", sid, "subagents/agent-bbb")]) == 3


def test_idempotent_second_run_writes_nothing(patched_jsonl):
    """Running twice against the same fake store does not duplicate rows."""
    _projects_dir, project_dir = patched_jsonl
    sid = "33333333-3333-3333-3333-333333333333"
    _write_jsonl(project_dir / f"{sid}.jsonl", _main_entries(sid, 4))
    subagents = project_dir / sid / "subagents"
    _write_jsonl(subagents / "agent-ccc.jsonl", _agent_entries(sid, "ccc", 2))

    store = FakeStore()
    session = _make_session(sid)

    first = asyncio.run(
        bf._backfill_one(store, session, batch_size=500, dry_run=False)
    )
    assert first.status == "written"
    assert first.main_rows == 4 and first.subagent_rows == 2

    second = asyncio.run(
        bf._backfill_one(store, session, batch_size=500, dry_run=False)
    )
    assert second.status == "already"
    assert second.main_rows == 0 and second.subagent_rows == 0

    # Row counts unchanged after the second run.
    assert len(store.rows[("-workspace", sid, None)]) == 4
    assert len(store.rows[("-workspace", sid, "subagents/agent-ccc")]) == 2


def test_missing_main_jsonl_is_skipped_not_fatal(patched_jsonl):
    """A session with no JSONL on disk is skipped, not an error."""
    sid = "44444444-4444-4444-4444-444444444444"  # never written to disk
    store = FakeStore()
    res = asyncio.run(
        bf._backfill_one(store, _make_session(sid), batch_size=500, dry_run=False)
    )
    assert res.status == "skipped"
    assert "no main JSONL" in res.detail
    assert store.rows == {}


def test_unreadable_main_jsonl_is_skipped_not_fatal(patched_jsonl):
    """An empty/unparseable main transcript is skipped, never raises."""
    _projects_dir, project_dir = patched_jsonl
    sid = "55555555-5555-5555-5555-555555555555"
    # File exists (so get_session_jsonl_path finds it) but yields no entries:
    # blank lines + a corrupt line are both swallowed by parse_jsonl_file.
    (project_dir / f"{sid}.jsonl").write_text("\n\nnot json at all\n",
                                              encoding="utf-8")

    store = FakeStore()
    res = asyncio.run(
        bf._backfill_one(store, _make_session(sid), batch_size=500, dry_run=False)
    )
    assert res.status == "skipped"
    assert "empty/unreadable" in res.detail
    assert store.rows == {}


def test_dry_run_writes_nothing(patched_jsonl):
    """--dry-run reports counts but writes no rows."""
    _projects_dir, project_dir = patched_jsonl
    sid = "66666666-6666-6666-6666-666666666666"
    _write_jsonl(project_dir / f"{sid}.jsonl", _main_entries(sid, 3))
    subagents = project_dir / sid / "subagents"
    _write_jsonl(subagents / "agent-ddd.jsonl", _agent_entries(sid, "ddd", 2))

    store = FakeStore()
    res = asyncio.run(
        bf._backfill_one(store, _make_session(sid), batch_size=500, dry_run=True)
    )

    assert res.status == "written"  # "would write"
    assert res.main_rows == 3
    assert res.subagent_rows == 2
    # Crucially: nothing was actually written to the store.
    assert store.rows == {}
    assert store.backfill_calls == []


def test_run_backfill_end_to_end_with_fake_store(patched_jsonl, monkeypatch):
    """run_backfill enumerates, backfills, and summarizes via the fake store."""
    _projects_dir, project_dir = patched_jsonl
    sid_a = "aaaaaaa1-0000-0000-0000-000000000001"
    sid_b = "bbbbbbb2-0000-0000-0000-000000000002"
    _write_jsonl(project_dir / f"{sid_a}.jsonl", _main_entries(sid_a, 2))
    _write_jsonl(project_dir / f"{sid_b}.jsonl", _main_entries(sid_b, 3))

    sessions = [_make_session(sid_a), _make_session(sid_b)]
    monkeypatch.setattr(bf, "_enumerate_sessions",
                        lambda only_session=None, limit=None: list(sessions))

    fake = FakeStore()
    monkeypatch.setattr(bf, "PgSessionStore", lambda: fake, raising=False)
    # PgSessionStore is imported inside run_backfill; patch the source module too.
    import app.core.pg_session_store as pss
    monkeypatch.setattr(pss, "PgSessionStore", lambda: fake)

    args = bf._build_parser().parse_args(
        ["--batch-size", "500", "--sleep-ms", "0"]
    )
    summary = asyncio.run(bf.run_backfill(args))

    assert summary.total_sessions == 2
    assert summary.written == 2
    assert summary.skipped == 0
    assert summary.errors == 0
    assert summary.main_rows == 5
    assert ("-workspace", sid_a, None) in fake.rows
    assert ("-workspace", sid_b, None) in fake.rows


def test_verify_reuses_session_store_verify_helpers(patched_jsonl, monkeypatch):
    """verify_session_store_backfill classifies OK vs DRIFT via the verifier's
    own DB helpers (patched) + the real JSONL readers."""
    _projects_dir, project_dir = patched_jsonl
    sid_ok = "ccccccc3-0000-0000-0000-000000000003"
    sid_drift = "ddddddd4-0000-0000-0000-000000000004"
    _write_jsonl(project_dir / f"{sid_ok}.jsonl", _main_entries(sid_ok, 3))
    _write_jsonl(project_dir / f"{sid_drift}.jsonl", _main_entries(sid_drift, 3))

    import app.core.session_store_verify as ssv

    # DB side: sid_ok matches JSONL uuids exactly; sid_drift is short one row.
    db_main = {
        sid_ok: [f"{sid_ok}-u0", f"{sid_ok}-u1", f"{sid_ok}-u2"],
        sid_drift: [f"{sid_drift}-u0", f"{sid_drift}-u1"],  # count drift
    }
    monkeypatch.setattr(ssv, "_db_main_uuids", lambda sid: list(db_main.get(sid, [])))
    monkeypatch.setattr(ssv, "_db_subagent_subpaths", lambda sid: set())

    summary = bf.verify_session_store_backfill(
        [_make_session(sid_ok), _make_session(sid_drift)]
    )

    assert summary["total"] == 2
    assert sid_ok in summary["ok"]
    assert any(sid_drift in d for d in summary["drift"])


def test_verify_empty_when_no_db_rows(patched_jsonl, monkeypatch):
    """A session with zero DB rows is classified EMPTY (pre-mirror), not DRIFT."""
    _projects_dir, project_dir = patched_jsonl
    sid = "eeeeeee5-0000-0000-0000-000000000005"
    _write_jsonl(project_dir / f"{sid}.jsonl", _main_entries(sid, 2))

    import app.core.session_store_verify as ssv
    monkeypatch.setattr(ssv, "_db_main_uuids", lambda s: [])
    monkeypatch.setattr(ssv, "_db_subagent_subpaths", lambda s: set())

    summary = bf.verify_session_store_backfill([_make_session(sid)])
    assert summary["empty"] == [sid]
    assert summary["ok"] == []
    assert summary["drift"] == []


def test_verify_elsewhere_when_db_rows_but_no_local_jsonl(patched_jsonl, monkeypatch):
    """DB rows present but no local JSONL on disk -> ELSEWHERE, not DRIFT.

    The ``session_store_entries`` table is shared across machines while JSONL
    transcripts are local, so a session whose transcript lives on another host
    must NOT be flagged as corruption/drift.
    """
    # Deliberately never write a JSONL file for this sid, so
    # get_session_jsonl_path returns None (no local transcript on disk).
    sid = "fffffff6-0000-0000-0000-000000000006"

    import app.core.session_store_verify as ssv
    # DB has rows for this session (backfilled by another machine).
    monkeypatch.setattr(
        ssv, "_db_main_uuids", lambda s: [f"{sid}-u0", f"{sid}-u1"]
    )
    monkeypatch.setattr(ssv, "_db_subagent_subpaths", lambda s: set())

    summary = bf.verify_session_store_backfill([_make_session(sid)])

    assert summary["total"] == 1
    assert any(sid in e for e in summary["elsewhere"])
    assert summary["ok"] == []
    assert summary["empty"] == []
    assert summary["drift"] == []
