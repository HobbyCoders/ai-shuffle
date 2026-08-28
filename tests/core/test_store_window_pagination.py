"""Store-side transcript pagination, parse-time screenshot substitution.

Three things are proved here, in order of how badly they would hurt if wrong:

1. **Equivalence.** A windowed read returns the same messages, in the same
   order, that slicing a FULL read at the same entry range returns — including
   across the window's edges, where a ``tool_result`` has to find the
   ``tool_use`` card of an entry outside the window.
2. **Cache separation.** A windowed result is never written to, or served from,
   the full-transcript cache entry, and vice versa. Getting this wrong shows a
   user a page of their session where the whole session should be.
3. **The default is untouched.** No pagination arguments means the exact SQL
   and the exact transcript the store served before any of this existed.

Plus: a screenshot whose bytes are NOT in the artifact store still renders
inline, so nothing can disappear from a transcript.

No Postgres here — ``get_db()`` is replaced with a fake connection over an
in-memory row list that really applies ``ORDER BY``/``LIMIT``/``OFFSET``, which
is what makes the window arithmetic testable at all.
"""

from __future__ import annotations

import base64
import re
from contextlib import contextmanager
from typing import Any, List, Optional

import pytest

import app.core.pg_session_store as pss
from app.core import jsonl_parser as jp


# ---------------------------------------------------------------------------
# A fake session_store_entries table that honours the SQL we actually issue.
# ---------------------------------------------------------------------------


class FakeTableCursor:
    """Executes the handful of statements the read path uses, for real.

    Only the shapes this module issues are understood: the COUNT/MAX version
    probe and the windowed / unwindowed entry selects. Anything else raises, so
    a changed query cannot silently pass these tests.
    """

    def __init__(self, rows: List[dict]) -> None:
        # rows: [{"id": int, "session_id": str, "subpath": str|None, "entry": dict}]
        self._rows = rows
        self.executed: List[tuple] = []
        self._result: List[dict] = []
        #: How many ``entry`` payloads this cursor has actually handed back —
        #: the thing store-side pagination is supposed to shrink.
        self.entry_rows_read = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        params = tuple(params or ())
        norm = " ".join(sql.split())
        session_id = params[0]
        rows = [r for r in self._rows if r["session_id"] == session_id]
        if "subpath IS NULL" in norm:
            rows = [r for r in rows if r["subpath"] is None]
            rest = params[1:]
        elif "subpath = %s" in norm:
            rows = [r for r in rows if r["subpath"] == params[1]]
            rest = params[2:]
        else:
            rows = [r for r in rows if r["subpath"] is None]
            rest = params[1:]

        if norm.startswith("SELECT COUNT(*)"):
            ids = [r["id"] for r in rows]
            self._result = [{"n": len(rows), "max_id": max(ids) if ids else None}]
            return

        if norm.startswith("SELECT entry FROM session_store_entries"):
            if "id <= %s" in norm:
                max_id = rest[0]
                rows = [r for r in rows if r["id"] <= max_id]
                rest = rest[1:]
            rows.sort(key=lambda r: r["id"], reverse="ORDER BY id DESC" in norm)
            if "LIMIT %s OFFSET %s" in norm:
                limit, offset = rest[0], rest[1]
                rows = rows[offset:offset + limit]
            self._result = [{"entry": r["entry"]} for r in rows]
            self.entry_rows_read += len(self._result)
            return

        raise AssertionError(f"unexpected SQL in this test: {norm}")

    def fetchall(self) -> List[dict]:
        return list(self._result)

    def fetchone(self) -> Optional[dict]:
        return self._result[0] if self._result else None


class FakeConn:
    def __init__(self, cursor: FakeTableCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeTableCursor:
        return self._cursor


def _install_fake_db(monkeypatch, entries, session_id="sess-window", subpath=None):
    rows = [
        {"id": i + 1, "session_id": session_id, "subpath": subpath, "entry": e}
        for i, e in enumerate(entries)
    ]
    cur = FakeTableCursor(rows)

    @contextmanager
    def _get_db():
        yield FakeConn(cur)

    monkeypatch.setattr(pss, "get_db", lambda: _get_db())
    return cur


# ---------------------------------------------------------------------------
# Transcript fixtures
# ---------------------------------------------------------------------------


def _text_transcript(n_turns: int) -> List[dict]:
    """``n_turns`` plain user/assistant exchanges — 2 entries, 2 messages each."""
    out: List[dict] = []
    for i in range(n_turns):
        out.append({
            "type": "user",
            "uuid": f"u-{i}",
            "timestamp": f"2024-01-01T00:{i:02d}:00Z",
            "message": {"role": "user", "content": f"question {i}"},
        })
        out.append({
            "type": "assistant",
            "uuid": f"a-{i}",
            "timestamp": f"2024-01-01T00:{i:02d}:30Z",
            "message": {
                "role": "assistant",
                "model": "claude-x",
                "content": [{"type": "text", "text": f"answer {i}"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        })
    return out


def _tool_pair(i: int) -> List[dict]:
    """An assistant tool_use entry followed by its tool_result entry."""
    tool_id = f"toolu-{i}"
    return [
        {
            "type": "assistant",
            "uuid": f"t-{i}",
            "timestamp": f"2024-01-02T00:{i:02d}:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-x",
                "content": [{
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": f"echo {i}"},
                }],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        },
        {
            "type": "user",
            "uuid": f"r-{i}",
            "timestamp": f"2024-01-02T00:{i:02d}:05Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"out {i}",
                }],
            },
        },
    ]


def _full_loader(entries):
    def _loader(session_id: str, subpath: Optional[str] = None):
        return list(entries) if subpath is None else []
    return _loader


def _version_loader(entries):
    def _loader(session_id: str):
        return (len(entries), len(entries))
    return _loader


def _slice_of_full_parse(entries, start: int, end: int):
    """The messages a FULL parse produces for entries[start:end]."""
    boundaries: List[int] = []
    messages, _usage = jp._transform_entries(
        list(entries), "sess-window", "/workspace",
        entry_message_boundaries=boundaries,
    )
    lo = jp._boundary(boundaries, start, len(messages))
    hi = jp._boundary(boundaries, end, len(messages))
    return messages[lo:hi]


_COUNTER_ID = re.compile(r"^(text|thinking)-(?P<uuid>.+)-\d+$")


def _identity(msg: dict) -> dict:
    """A message with its parse-order-dependent id suffix normalised away.

    ``text-<uuid>-<n>`` embeds the transform's running counter, which by
    construction counts the whole transcript in a full parse and only the window
    in a windowed one. Everything else — uuid, role, type, content, tool merges,
    metadata, ordering — must match exactly, and that is what the comparison
    below checks.
    """
    out = dict(msg)
    m = _COUNTER_ID.match(str(out.get("id", "")))
    if m:
        out["id"] = f"<counter>-{m.group('uuid')}"
    return out


def _identities(msgs):
    return [_identity(m) for m in msgs]


def setup_function(_f):
    jp.invalidate_parse_cache()


def teardown_function(_f):
    jp.invalidate_parse_cache()


# ---------------------------------------------------------------------------
# 1. The default path is unchanged
# ---------------------------------------------------------------------------


def test_default_load_sql_has_no_limit_and_binds_only_session_id(monkeypatch):
    """No pagination arguments -> byte-for-byte the query that shipped."""
    entries = _text_transcript(3)
    cur = _install_fake_db(monkeypatch, entries)

    got = pss._load_entries_by_session_id_sync("sess-window", None)

    assert got == entries
    sql, params = cur.executed[-1]
    assert "LIMIT" not in sql
    assert "OFFSET" not in sql
    assert "ORDER BY id ASC" in sql
    assert "subpath IS NULL" in sql
    assert "project_key" not in sql
    assert params == ("sess-window",)


def test_default_parse_returns_the_whole_transcript(monkeypatch):
    """The unwindowed parse still yields every message, and caches under the
    bare session-id key."""
    entries = _text_transcript(4)
    messages, usage = jp.parse_session_history_from_store(
        "sess-window", "/workspace", _full_loader(entries),
        store_version_loader=_version_loader(entries),
    )
    assert len(messages) == 8
    assert usage["total_tokens_out"] == 20
    assert "windowed" not in usage
    assert "sess-window" in jp._store_parse_cache.keys()


# ---------------------------------------------------------------------------
# 2. The store window reads only the rows it returns
# ---------------------------------------------------------------------------


def test_window_reads_only_the_windows_rows(monkeypatch):
    entries = _text_transcript(25)  # 50 entries
    cur = _install_fake_db(monkeypatch, entries)

    win = pss._load_entries_window_by_session_id_sync(
        "sess-window", None, limit=10, context=0
    )

    assert win["total"] == 50
    assert win["window_start"] == 40
    assert win["window_end"] == 50
    assert win["start"] == 40
    assert win["entries"] == entries[40:50]

    sql, params = cur.executed[-1]
    assert "LIMIT %s OFFSET %s" in " ".join(sql.split())
    assert "ORDER BY id DESC" in sql
    assert "id <= %s" in sql
    # (session_id, max_id, limit, offset) — newest page starts at offset 0.
    assert params == ("sess-window", 50, 10, 0)


def test_window_walks_backwards_with_the_cursor(monkeypatch):
    entries = _text_transcript(25)
    _install_fake_db(monkeypatch, entries)

    first = pss._load_entries_window_by_session_id_sync(
        "sess-window", None, limit=10, context=0
    )
    second = pss._load_entries_window_by_session_id_sync(
        "sess-window", None, limit=10, before_seq=first["window_start"], context=0
    )

    assert second["entries"] == entries[30:40]
    assert second["window_start"] == 30
    assert second["window_end"] == 40


def test_window_includes_merge_context_on_both_sides(monkeypatch):
    entries = _text_transcript(25)
    _install_fake_db(monkeypatch, entries)

    win = pss._load_entries_window_by_session_id_sync(
        "sess-window", None, limit=10, before_seq=30, context=3
    )

    assert win["window_start"] == 20 and win["window_end"] == 30
    assert win["start"] == 17
    assert win["entries"] == entries[17:33]


def test_window_clamps_a_stale_or_absurd_cursor(monkeypatch):
    entries = _text_transcript(5)  # 10 entries
    _install_fake_db(monkeypatch, entries)

    beyond = pss._load_entries_window_by_session_id_sync(
        "sess-window", None, limit=4, before_seq=9999, context=0
    )
    assert beyond["entries"] == entries[6:10]

    at_zero = pss._load_entries_window_by_session_id_sync(
        "sess-window", None, limit=4, before_seq=0, context=0
    )
    assert at_zero["entries"] == []
    assert at_zero["window_start"] == 0 and at_zero["window_end"] == 0


def test_window_on_an_empty_transcript(monkeypatch):
    _install_fake_db(monkeypatch, [])
    win = pss._load_entries_window_by_session_id_sync("sess-window", None, limit=10)
    assert win == {
        "entries": [], "total": 0, "start": 0, "window_start": 0, "window_end": 0,
    }


def test_window_supports_subagent_subpaths(monkeypatch):
    entries = _text_transcript(6)
    cur = _install_fake_db(monkeypatch, entries, subpath="subagents/agent-1")

    win = pss._load_entries_window_by_session_id_sync(
        "sess-window", "subagents/agent-1", limit=4, context=0
    )
    assert win["entries"] == entries[8:12]
    assert "project_key" not in cur.executed[-1][0]


@pytest.mark.asyncio
async def test_async_window_wrapper(monkeypatch):
    entries = _text_transcript(10)
    _install_fake_db(monkeypatch, entries)
    store = pss.PgSessionStore()
    win = await store.load_entries_window_by_session_id(
        "sess-window", None, limit=5, context=0
    )
    assert win["entries"] == entries[15:20]


# ---------------------------------------------------------------------------
# 3. Equivalence: a window == the same slice of a full parse
# ---------------------------------------------------------------------------


def _window_loader_over(entries):
    """A window loader backed by the real store helper and the fake table."""
    def _loader(session_id, subpath=None, *, limit, before_seq=None, context=None):
        return pss._load_entries_window_by_session_id_sync(
            session_id, subpath, limit=limit, before_seq=before_seq, context=context
        )
    return _loader


def test_every_page_matches_the_same_slice_of_the_full_parse(monkeypatch):
    """Walk a multi-page transcript backwards; each page equals its slice."""
    entries = _text_transcript(30)  # 60 entries, 60 messages
    _install_fake_db(monkeypatch, entries)
    loader = _window_loader_over(entries)

    cursor = None
    seen_pages = 0
    covered: List[dict] = []
    while True:
        messages, usage, pagination = jp.parse_session_window_from_store(
            "sess-window", "/workspace", loader,
            _full_loader(entries),
            limit=13, before_seq=cursor,
            store_version_loader=_version_loader(entries),
        )
        expected = _slice_of_full_parse(
            entries, pagination["first_seq"] or 0, (pagination["last_seq"] or -1) + 1
        )
        assert _identities(messages) == _identities(expected)
        assert usage["windowed"] is True
        covered = list(messages) + covered
        seen_pages += 1
        if not pagination["has_more"]:
            break
        cursor = pagination["next_before_seq"]
        assert seen_pages < 20, "cursor is not walking backwards"

    assert seen_pages == 5  # 60 entries / 13 per page
    full, _ = jp.parse_session_history_from_store(
        "sess-window", "/workspace", _full_loader(entries),
        store_version_loader=_version_loader(entries),
    )
    assert _identities(covered) == _identities(full)


def test_tool_result_whose_tool_use_is_outside_the_window_still_merges(monkeypatch):
    """The boundary case the merge context exists for.

    Without context, the tool_result at the start of the window cannot find its
    tool_use card and the transform emits a free-standing ``tool_result``
    message that a full parse never produces.
    """
    entries: List[dict] = []
    for i in range(12):
        entries.extend(_tool_pair(i))
    _install_fake_db(monkeypatch, entries)

    # Window starts on a tool_RESULT entry (odd index), so its tool_use is out.
    messages, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=7, before_seq=16,
        store_version_loader=_version_loader(entries),
    )
    assert pagination["first_seq"] == 9  # a tool_result entry

    expected = _slice_of_full_parse(entries, 9, 16)
    assert _identities(messages) == _identities(expected)
    # And concretely: no orphaned result card, and the truncated card is gone
    # rather than duplicated.
    assert not any(m.get("type") == "tool_result" for m in messages)


def test_tool_use_at_the_newest_edge_gets_its_result_from_the_after_context(monkeypatch):
    """An older page ending on a tool_use must still show it as complete."""
    entries: List[dict] = []
    for i in range(12):
        entries.extend(_tool_pair(i))
    _install_fake_db(monkeypatch, entries)

    messages, _usage, _p = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=5, before_seq=13,  # ends on entry 12 == a tool_use
        store_version_loader=_version_loader(entries),
    )
    last_tool = [m for m in messages if m.get("type") == "tool_use"][-1]
    assert last_tool["toolStatus"] == "complete"
    assert last_tool["toolResult"] == "out 6"
    assert _identities(messages) == _identities(_slice_of_full_parse(entries, 8, 13))


def test_window_larger_than_the_transcript_is_the_whole_transcript(monkeypatch):
    entries = _text_transcript(3)
    _install_fake_db(monkeypatch, entries)

    messages, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=5000,
        store_version_loader=_version_loader(entries),
    )
    full, _ = jp.parse_session_history_from_store(
        "sess-window", "/workspace", _full_loader(entries),
        store_version_loader=_version_loader(entries),
    )
    assert _identities(messages) == _identities(full)
    assert pagination["has_more"] is False
    assert pagination["next_before_seq"] is None
    assert pagination["total"] == 6


def test_uuidless_entries_fall_back_to_the_full_parse(monkeypatch):
    """Slower-but-correct: an entry with no uuid gets the identity the full
    parse would give it, because the whole transcript is parsed."""
    entries = _text_transcript(8)
    del entries[3]["uuid"]  # a legacy/hand-built entry
    _install_fake_db(monkeypatch, entries)

    messages, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=6,
        store_version_loader=_version_loader(entries),
    )
    expected = _slice_of_full_parse(entries, 10, 16)
    # Full-parse identities, including the counter-derived ids: no normalisation.
    assert messages == expected
    assert pagination["first_seq"] == 10


def test_a_windowed_open_reads_only_the_windows_rows(monkeypatch):
    """The point of the whole exercise, measured.

    A full open materialises every row of the transcript; a windowed open
    materialises the window plus the constant merge context, and never calls the
    whole-transcript loader at all.
    """
    entries = _text_transcript(500)  # 1000 entries
    cur = _install_fake_db(monkeypatch, entries)

    full_calls = []

    def _counting_full_loader(session_id, subpath=None):
        full_calls.append(subpath)
        return list(entries) if subpath is None else []

    jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _counting_full_loader,
        limit=100,
        store_version_loader=_version_loader(entries),
    )

    assert full_calls == []  # the whole transcript was never loaded
    # window (100) + merge context (200 before; none after — this is the newest
    # page, there is nothing newer).
    assert cur.entry_rows_read == 300

    cur.entry_rows_read = 0
    pss._load_entries_by_session_id_sync("sess-window", None)
    assert cur.entry_rows_read == 1000


def test_a_result_arriving_far_past_the_merge_context_takes_the_slow_path(monkeypatch):
    """A backgrounded tool whose result lands beyond the context window.

    The merge context cannot reach it, so the page must fall back to the whole
    transcript rather than show a completed tool as still running.
    """
    entries: List[dict] = []
    entries.append({
        "type": "assistant",
        "uuid": "bg-use",
        "timestamp": "2024-01-04T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "id": "toolu-bg",
                "name": "Bash", "input": {"command": "sleep 999"},
            }],
        },
    })
    entries.extend(_text_transcript(200))  # 400 entries of unrelated traffic
    entries.append({
        "type": "user",
        "uuid": "bg-result",
        "timestamp": "2024-01-04T09:00:00Z",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": "toolu-bg",
                "content": "finally done",
            }],
        },
    })
    _install_fake_db(monkeypatch, entries)

    # An OLDER page containing the tool_use but not, within 200 entries, its
    # result.
    messages, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=5, before_seq=5,
        store_version_loader=_version_loader(entries),
    )
    assert pagination["first_seq"] == 0
    card = [m for m in messages if m.get("toolId") == "toolu-bg"][0]
    assert card["toolStatus"] == "complete"
    assert card["toolResult"] == "finally done"
    assert messages == _slice_of_full_parse(entries, 0, 5)


def test_an_orphan_result_near_the_start_is_kept_when_we_read_from_the_start(monkeypatch):
    """A genuinely orphaned tool_result (no tool_use anywhere) survives.

    The provability check must not turn a real transcript feature into an
    endless fallback: when the window starts at entry 0 there is nothing older
    to have missed, so the orphan is served as-is.
    """
    entries: List[dict] = [{
        "type": "user",
        "uuid": "orphan",
        "timestamp": "2024-01-05T00:00:00Z",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result", "tool_use_id": "toolu-ghost",
                "content": "result with no call",
            }],
        },
    }]
    entries.extend(_text_transcript(2))
    _install_fake_db(monkeypatch, entries)

    messages, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=50,
        store_version_loader=_version_loader(entries),
    )
    assert pagination["first_seq"] == 0
    assert [m["type"] for m in messages][0] == "tool_result"
    assert messages == _slice_of_full_parse(entries, 0, len(entries))


# ---------------------------------------------------------------------------
# 4. The cache trap
# ---------------------------------------------------------------------------


def test_a_windowed_read_never_populates_the_full_transcript_entry(monkeypatch):
    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)

    jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=4,
        store_version_loader=_version_loader(entries),
    )

    keys = jp._store_parse_cache.keys()
    assert "sess-window" not in keys
    assert ("window", "sess-window", 4, None) in keys

    # A full read after it still gets the WHOLE transcript, not the page.
    full, _ = jp.parse_session_history_from_store(
        "sess-window", "/workspace", _full_loader(entries),
        store_version_loader=_version_loader(entries),
    )
    assert len(full) == 40


def test_a_full_read_is_never_served_to_a_windowed_reader(monkeypatch):
    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)

    full, _ = jp.parse_session_history_from_store(
        "sess-window", "/workspace", _full_loader(entries),
        store_version_loader=_version_loader(entries),
    )
    assert len(full) == 40
    assert "sess-window" in jp._store_parse_cache.keys()

    messages, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries),
        limit=4,
        store_version_loader=_version_loader(entries),
    )
    assert len(messages) == 4
    assert pagination["returned"] == 4


def test_a_windowed_hit_is_scoped_to_its_own_cursor(monkeypatch):
    """Two different pages must not share a cache entry."""
    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)
    loader = _window_loader_over(entries)
    full = _full_loader(entries)
    kwargs = dict(store_version_loader=_version_loader(entries))

    newest, _u, _p = jp.parse_session_window_from_store(
        "sess-window", "/workspace", loader, full, limit=6, **kwargs)
    older, _u2, _p2 = jp.parse_session_window_from_store(
        "sess-window", "/workspace", loader, full, limit=6, before_seq=34, **kwargs)
    newest_again, _u3, _p3 = jp.parse_session_window_from_store(
        "sess-window", "/workspace", loader, full, limit=6, **kwargs)

    assert [m["uuid"] for m in newest] != [m["uuid"] for m in older]
    assert [m["uuid"] for m in newest_again] == [m["uuid"] for m in newest]


def test_a_windowed_hit_is_dropped_when_the_store_version_moves(monkeypatch):
    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)
    loader = _window_loader_over(entries)

    first, _u, _p = jp.parse_session_window_from_store(
        "sess-window", "/workspace", loader,
        _full_loader(entries), limit=4,
        store_version_loader=lambda _sid: (40, 40),
    )
    # A turn lands: same call, newer version stamp -> re-read, not the old page.
    entries.extend(_text_transcript(1))
    _install_fake_db(monkeypatch, entries)
    second, _u2, _p2 = jp.parse_session_window_from_store(
        "sess-window", "/workspace", loader,
        _full_loader(entries), limit=4,
        store_version_loader=lambda _sid: (42, 42),
    )
    assert [m["uuid"] for m in second] != [m["uuid"] for m in first]
    assert second[-1]["uuid"] == "a-0"  # the freshly appended turn


def test_invalidate_drops_windowed_entries_too(monkeypatch):
    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)

    jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries), limit=4,
        store_version_loader=_version_loader(entries),
    )
    assert any(isinstance(k, tuple) for k in jp._store_parse_cache.keys())

    jp.invalidate_parse_cache("sess-window")
    assert not any(isinstance(k, tuple) for k in jp._store_parse_cache.keys())


def test_pre_seed_drops_windowed_entries_for_that_session(monkeypatch):
    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)

    jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries), limit=4,
        store_version_loader=_version_loader(entries),
    )
    jp.parse_session_window_from_store(
        "other-sess", "/workspace",
        lambda *_a, **_k: {"entries": [], "total": 0, "start": 0,
                           "window_start": 0, "window_end": 0},
        _full_loader([]), limit=4,
        store_version_loader=lambda _s: (0, 0),
    )

    monkeypatch.setattr(jp, "get_session_jsonl_path", lambda *a, **k: None)
    jp.pre_seed_parse_cache(
        "sess-window", [], {}, store_version_loader=_version_loader(entries),
    )

    keys = jp._store_parse_cache.keys()
    assert ("window", "sess-window", 4, None) not in keys
    assert ("window", "other-sess", 4, None) in keys


def test_window_entries_respect_the_byte_bounded_lru(monkeypatch):
    """Windowed pages live in the same cache and obey the same budgets."""
    entries = _text_transcript(4)
    _install_fake_db(monkeypatch, entries)
    monkeypatch.setattr(jp._store_parse_cache, "max_entry_bytes", 1)

    jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries), limit=2,
        store_version_loader=_version_loader(entries),
    )
    assert jp._store_parse_cache.keys() == []
    assert jp._store_parse_cache.current_bytes == 0


# ---------------------------------------------------------------------------
# 5. Screenshots resolved at parse time
# ---------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-pixels" * 8
B64 = base64.b64encode(PNG).decode()


def _screenshot_transcript() -> List[dict]:
    return [
        {
            "type": "assistant",
            "uuid": "s-a",
            "timestamp": "2024-01-03T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use", "id": "toolu-shot",
                    "name": "chrome_screenshot", "input": {},
                }],
            },
        },
        {
            "type": "user",
            "uuid": "s-r",
            "timestamp": "2024-01-03T00:00:05Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu-shot",
                    "content": [
                        {"type": "text", "text": "captured"},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": B64,
                        }},
                    ],
                }],
            },
        },
    ]


def _patch_artifact_store(monkeypatch, present: bool):
    import app.core.artifact_store as store
    monkeypatch.setattr(
        store, "artifact_path",
        lambda h: ("/artifacts/" + h) if present else None,
    )


def test_screenshot_missing_from_the_artifact_store_stays_inline(monkeypatch):
    """The fallback that must never break: no stored bytes -> the data URI, so
    the screenshot still renders."""
    _patch_artifact_store(monkeypatch, present=False)
    entries = _screenshot_transcript()

    messages, _usage = jp._transform_entries(entries, "sess-shot", "/workspace")
    shot = [m for m in messages if m.get("toolScreenshot")][0]["toolScreenshot"]
    assert shot == f"data:image/png;base64,{B64}"


def test_screenshot_already_stored_becomes_an_artifact_url(monkeypatch):
    """The whole point: the multi-megabyte string is never built."""
    import hashlib
    _patch_artifact_store(monkeypatch, present=True)
    entries = _screenshot_transcript()

    messages, _usage = jp._transform_entries(entries, "sess-shot", "/workspace")
    shot = [m for m in messages if m.get("toolScreenshot")][0]["toolScreenshot"]
    assert shot == f"/api/v1/artifacts/{hashlib.sha256(PNG).hexdigest()}"


def test_screenshot_substitution_matches_the_api_layer_rewrite(monkeypatch):
    """Parse-time and API-time substitution must agree exactly — the API rewrite
    still runs on the page and must be a no-op on what the parser produced."""
    from app.api.sessions_helpers import compact_messages_for_display

    import hashlib
    _patch_artifact_store(monkeypatch, present=True)
    messages, _usage = jp._transform_entries(
        _screenshot_transcript(), "sess-shot", "/workspace"
    )
    before = [m.get("toolScreenshot") for m in messages]
    compact_messages_for_display(messages)
    assert [m.get("toolScreenshot") for m in messages] == before
    assert f"/api/v1/artifacts/{hashlib.sha256(PNG).hexdigest()}" in before


def test_undecodable_or_odd_screenshot_payloads_stay_inline(monkeypatch):
    """Anything the artifact address cannot be computed for keeps the data URI."""
    _patch_artifact_store(monkeypatch, present=True)
    # A mime the API-side data-URI regex would reject must not be rewritten
    # either, or the two paths would disagree.
    content = [{"type": "image", "data": B64, "mimeType": "image/png;weird"}]
    assert jp.extract_screenshot_ref(content).startswith("data:image/png;weird;base64,")

    # Empty payload -> no image at all.
    assert jp.extract_screenshot_ref([{"type": "image", "source": {"data": ""}}]) is None
    assert jp.extract_screenshot_ref("not a list") is None


def test_screenshot_lookup_failure_never_breaks_the_parse(monkeypatch):
    """An artifact store that raises degrades to the inline data URI."""
    import app.core.artifact_store as store

    def _boom(_h):
        raise RuntimeError("artifact store is on fire")

    monkeypatch.setattr(store, "artifact_path", _boom)
    messages, _usage = jp._transform_entries(
        _screenshot_transcript(), "sess-shot", "/workspace"
    )
    shot = [m for m in messages if m.get("toolScreenshot")][0]["toolScreenshot"]
    assert shot == f"data:image/png;base64,{B64}"


def test_subagent_child_screenshots_go_through_the_same_resolver(monkeypatch):
    import hashlib
    _patch_artifact_store(monkeypatch, present=True)
    children = jp.transform_agent_entries(_screenshot_transcript())
    shot = [c for c in children if c.get("toolScreenshot")][0]["toolScreenshot"]
    assert shot == f"/api/v1/artifacts/{hashlib.sha256(PNG).hexdigest()}"


# ---------------------------------------------------------------------------
# 6. message_pagination on the response model
# ---------------------------------------------------------------------------


def _session_row(**over):
    import datetime
    row = {
        "id": "sess-1",
        "created_at": datetime.datetime(2024, 1, 1),
        "updated_at": datetime.datetime(2024, 1, 1),
        "messages": [],
    }
    row.update(over)
    return row


def test_message_pagination_defaults_to_none_so_the_shape_is_compatible():
    """Every existing caller builds this model without the new field."""
    from app.core.models import SessionWithMessages

    model = SessionWithMessages.model_validate(_session_row())
    assert model.message_pagination is None
    assert model.model_dump()["message_pagination"] is None


def test_message_pagination_accepts_the_dict_the_api_already_builds():
    """``paginate_messages`` returns a superset of the six fields; the extras
    are ignored rather than rejected, so it can be assigned straight through."""
    from app.api.sessions_helpers import paginate_messages
    from app.core.models import SessionWithMessages

    _page, pagination = paginate_messages([{"id": f"m{i}"} for i in range(50)], 20)
    model = SessionWithMessages.model_validate(
        _session_row(message_pagination=pagination)
    )
    assert model.message_pagination.total == 50
    assert model.message_pagination.returned == 20
    assert model.message_pagination.limit == 20
    assert model.message_pagination.has_more is True
    assert model.message_pagination.first_seq == 30
    assert model.message_pagination.next_before_seq == 30


def test_message_pagination_accepts_the_windowed_store_descriptor(monkeypatch):
    """The other producer: the entry-space descriptor from a windowed read."""
    from app.core.models import SessionWithMessages

    entries = _text_transcript(20)
    _install_fake_db(monkeypatch, entries)
    _msgs, _usage, pagination = jp.parse_session_window_from_store(
        "sess-window", "/workspace", _window_loader_over(entries),
        _full_loader(entries), limit=8,
        store_version_loader=_version_loader(entries),
    )
    model = SessionWithMessages.model_validate(
        _session_row(message_pagination=pagination)
    )
    assert model.message_pagination.total == 40
    assert model.message_pagination.next_before_seq == 32
    assert model.message_pagination.has_more is True


# ---------------------------------------------------------------------------
# 7. The boundary tracker is observational only
# ---------------------------------------------------------------------------


def test_boundary_tracking_does_not_change_the_transform():
    entries = _text_transcript(6)
    plain, plain_usage = jp._transform_entries(entries, "s", "/workspace")
    boundaries: List[int] = []
    tracked, tracked_usage = jp._transform_entries(
        entries, "s", "/workspace", entry_message_boundaries=boundaries
    )
    assert plain == tracked
    assert plain_usage == tracked_usage
    assert len(boundaries) == len(entries)
    assert boundaries == sorted(boundaries)
    assert boundaries[0] == 0
