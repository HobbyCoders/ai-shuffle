"""SessionSearch's maintained text projection, on opt-in disposable PostgreSQL.

Covers the triggers that keep ``session_recall_text`` exact, the resumable
batched backfill and its readiness gate, and the indexed search path: ranked
parity with the legacy query while every term is rarer than the per-term cap,
literal LIKE wildcards, and the newest-first cap for common terms.
"""
import time
from dataclasses import asdict

import pytest
from psycopg2.extras import Json, execute_values

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg

PREFIX = "rp-"


def _wipe(cur):
    for table in ("fluid_session_nodes", "fluid_sessions", "session_store_entries",
                  "session_events", "session_event_counters"):
        cur.execute(f"DELETE FROM {table} WHERE session_id LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM sessions WHERE id LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM session_recall_text WHERE session_id LIKE %s", (PREFIX + "%",))
    # Other suites share this database and must keep the legacy path.
    cur.execute("DELETE FROM session_recall_backfill")


@pytest.fixture(autouse=True)
def clean():
    from app.db import base

    with base.get_db() as conn:
        _wipe(conn.cursor())
    yield
    with base.get_db() as conn:
        _wipe(conn.cursor())


@pytest.fixture
def recall(monkeypatch):
    from app.core.provider import fluid_pg_store

    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(fluid_pg_store, "_worker_name", lambda _: "Test mirror")
    return fluid_pg_store


def _node(role, blocks):
    return Json({"role": role, "content": blocks})


def _projection(cur, source, session_id):
    cur.execute("SELECT row_key, body FROM session_recall_text WHERE source = %s AND session_id = %s "
                "ORDER BY row_key", (source, session_id))
    return {r["row_key"]: r["body"] for r in cur.fetchall()}


def _backfill_all(batch=None):
    from app.db import base, session_recall

    for source in session_recall.SOURCES:
        while True:
            with base.get_db() as conn:
                if session_recall.backfill_step(conn.cursor(), source, batch):
                    break
    with base.get_db() as conn:
        assert session_recall.ready(conn.cursor())


def test_bootstrap_installs_triggers_and_waits_for_backfill():
    from app.db import base, session_recall

    with base.get_db() as conn:
        cur = conn.cursor()
        assert session_recall.installed(cur)
        assert not session_recall.ready(cur)
        assert session_recall.install(cur)  # idempotent


def test_triggers_keep_projection_to_scored_text_on_every_write():
    from app.db import base

    sid = PREFIX + "native"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)", (
            sid, "a", 1.0, _node("assistant", [
                {"kind": "reasoning", "encrypted": "QUOKKA" * 50},
                {"kind": "text", "text": "Visible ANSWER"},
                {"kind": "tooluseblock", "name": "Bash", "input": {"command": "toolinputonly"}},
            ])))
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)", (
            sid, "b", 2.0, _node("user", [
                {"kind": "toolresultblock", "content": [{"kind": "text", "text": "Result LINE"},
                                                        {"kind": "image", "data": "imageonly"}]},
            ])))
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)", (
            sid, "c", 3.0, _node("assistant", [{"kind": "tooluseblock", "name": "Read", "input": {}}])))
        assert _projection(cur, "n", sid) == {"a": "visible answer", "b": "result line"}
        cur.execute("UPDATE fluid_session_nodes SET message = %s WHERE session_id = %s AND node_id = 'a'",
                    (_node("assistant", [{"kind": "text", "text": "Rewritten"}]), sid))
        cur.execute("DELETE FROM fluid_session_nodes WHERE session_id = %s AND node_id = 'b'", (sid,))
        assert _projection(cur, "n", sid) == {"a": "rewritten"}

        sdk = PREFIX + "sdk"
        execute_values(cur, "INSERT INTO session_store_entries(project_key,session_id,subpath,entry) VALUES %s", [
            ("p", sdk, None, Json({"type": "user", "message": {"content": "Main TEXT"}})),
            ("p", sdk, "agent.jsonl", Json({"type": "user", "message": {"content": "sidechain"}})),
            ("p", sdk, None, Json({"type": "user", "isMeta": True, "message": {"content": "meta"}})),
            ("p", sdk, None, Json({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Block one"}, {"type": "tool_use", "input": {"x": "toolinput"}}]}})),
        ])
        assert sorted(_projection(cur, "s", sdk).values()) == ["block one", "main text"]

        remote = PREFIX + "remote"
        execute_values(cur, "INSERT INTO session_events(id,session_id,seq,event_type,payload) VALUES %s", [
            (PREFIX + "e1", remote, 1, "message.user", Json({"content": "Hello WORLD"})),
            (PREFIX + "e2", remote, 2, "assistant.text_delta", Json({"text": ""})),
            (PREFIX + "e3", remote, 3, "assistant.text_delta", Json({"text": "Delta"})),
            (PREFIX + "e4", remote, 4, "message.assistant", Json({"content": "secret", "kind": "thinking"})),
            (PREFIX + "e5", remote, 5, "tool.use", Json({"name": "Bash", "input": {"command": "ls"}})),
        ])
        assert _projection(cur, "e", remote) == {
            PREFIX + "e1": "hello world", PREFIX + "e2": "", PREFIX + "e3": "delta"}
        # Delta compaction deletes a run and moves the head's id onto the last
        # row (app.db.session_events.compact_session_deltas).
        cur.execute("DELETE FROM session_events WHERE session_id = %s AND seq >= 2 AND seq < 3", (remote,))
        cur.execute("UPDATE session_events SET id = %s, payload = %s WHERE session_id = %s AND seq = 3",
                    (PREFIX + "e2", Json({"text": "Merged"}), remote))
        assert _projection(cur, "e", remote) == {PREFIX + "e1": "hello world", PREFIX + "e2": "merged"}


def test_backfill_is_batched_resumable_and_gates_readiness():
    from app.db import base, session_recall

    with base.get_db() as conn:
        cur = conn.cursor()
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", [
            (f"{PREFIX}bf{i % 3}", f"n{i}", float(i), _node("user", [{"kind": "text", "text": f"Row {i}"}]))
            for i in range(7)
        ])
        execute_values(cur, "INSERT INTO session_store_entries(project_key,session_id,entry) VALUES %s", [
            ("p", f"{PREFIX}bfsdk", Json({"type": "user", "message": {"content": f"Entry {i}"}})) for i in range(3)
        ])
        execute_values(cur, "INSERT INTO session_events(id,session_id,seq,event_type,payload) VALUES %s", [
            (f"{PREFIX}bfe{i}", f"{PREFIX}bfr", i, "message.user" if i % 2 else "tool.output",
             Json({"content": f"Event {i}"})) for i in range(6)
        ])
        cur.execute("SELECT source, session_id, row_key, ts, body FROM session_recall_text "
                    "WHERE session_id LIKE %s ORDER BY 1, 2, 3", (PREFIX + "%",))
        written_by_triggers = [dict(r) for r in cur.fetchall()]
        # Rows that predate the triggers: only the backfill can supply them.
        cur.execute("DELETE FROM session_recall_text WHERE session_id LIKE %s", (PREFIX + "%",))
    assert len(written_by_triggers) == 7 + 3 + 3

    steps = 0
    while True:
        with base.get_db() as conn:
            cur = conn.cursor()
            pending = [s for s in session_recall.SOURCES if not session_recall.backfill_step(cur, s, 2)]
            steps += 1
            if not pending:
                break
            assert not session_recall.ready(cur)
    assert steps > 3
    with base.get_db() as conn:
        cur = conn.cursor()
        assert session_recall.ready(cur)
        cur.execute("SELECT source, session_id, row_key, ts, body FROM session_recall_text "
                    "WHERE session_id LIKE %s ORDER BY 1, 2, 3", (PREFIX + "%",))
        assert [dict(r) for r in cur.fetchall()] == written_by_triggers


def test_install_waits_only_briefly_for_a_busy_table():
    from app.db import base, session_recall

    with base.get_db() as conn:
        conn.cursor().execute("DROP TRIGGER trg_session_recall_nodes ON fluid_session_nodes")
    try:
        with base.get_db() as blocker:
            blocker.cursor().execute("LOCK TABLE fluid_session_nodes IN ROW EXCLUSIVE MODE")
            started = time.perf_counter()
            with base.get_db() as conn:
                cur = conn.cursor()
                assert session_recall.install(cur, lock_timeout_ms=50) is False
                assert not session_recall.installed(cur)
                cur.execute("SELECT current_setting('lock_timeout') AS v")
                assert cur.fetchone()["v"] == "0"
            assert time.perf_counter() - started < 2
    finally:
        with base.get_db() as conn:
            assert session_recall.install(conn.cursor())
            assert session_recall.installed(conn.cursor())


def _corpus():
    from app.db import base, mobile_tool_identity

    mobile_tool_identity.init_schema()
    special = "recallneedle quokkaneedle capybaraneedle 東京 50%_complete"
    with base.get_db() as conn:
        cur = conn.cursor()
        sessions, native, delegated, remote = [], [], [], []
        for i in range(40):
            sessions += [(f"{PREFIX}n{i}", f"{PREFIX}ses-n{i}", f"Native {i}", "test-local"),
                         (f"{PREFIX}d{i}", f"{PREFIX}ses-d{i}", f"Delegated {i}", "test-local"),
                         (f"{PREFIX}r{i}", None, f"Remote {i}", "test-remote")]
            for j in range(5):
                words = ["recallneedle", "quokkaneedle", "capybaraneedle"][: (i + j) % 4]
                body = (special if i < 3 and j == 0
                        else f"Filler {i}-{j} " + " ".join(words) + " 50xxcomplete")
                ts = float(i * 100 + j)
                native.append((f"{PREFIX}ses-n{i}", f"{j}", ts, _node("user", [{"kind": "text", "text": body}])))
                delegated.append(("p", f"{PREFIX}ses-d{i}", Json({"type": "user", "message": {"content": body}}), ts))
                remote.append((f"{PREFIX}r{i}-{j}", f"{PREFIX}r{i}", j, "message.user", Json({"content": body}), ts))
        execute_values(cur, "INSERT INTO sessions(id,sdk_session_id,title,owner_device_id) VALUES %s", sessions)
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", native)
        execute_values(cur, "INSERT INTO session_store_entries(project_key,session_id,entry,created_at) VALUES %s",
                       delegated, template="(%s,%s,%s,to_timestamp(%s))")
        execute_values(cur, "INSERT INTO session_events(id,session_id,seq,event_type,payload,created_at) VALUES %s",
                       remote, template="(%s,%s,%s,%s,%s,to_timestamp(%s))")
        cur.execute("ANALYZE session_recall_text")


@pytest.mark.parametrize("terms", [
    ["recallneedle"],
    ["recallneedle", "quokkaneedle", "capybaraneedle"],
    ["東京"],
    ["50%_complete"],
    ["absentneedle"],
])
def test_indexed_search_ranks_exactly_like_the_legacy_query(recall, terms):
    from app.db import base, session_recall

    _corpus()
    with base.get_db() as conn:
        assert not session_recall.ready(conn.cursor())
    legacy = [asdict(h) for h in recall._search_sync(terms, 5, f"{PREFIX}r0")]
    _backfill_all()
    indexed = [asdict(h) for h in recall._search_sync(terms, 5, f"{PREFIX}r0")]
    assert indexed == legacy
    if terms != ["absentneedle"]:
        assert indexed


def test_like_wildcards_in_terms_match_literally(recall):
    from app.db import base

    _corpus()
    _backfill_all()
    sql, params = recall._recall_matches("n", "TRUE", [], [recall._like_pattern("50%_complete")])
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT session_id FROM ({sql}) m", params)
        assert {r["session_id"] for r in cur.fetchall()} == {f"{PREFIX}ses-n{i}" for i in range(3)}


def test_common_term_reads_newest_rows_but_keeps_rare_matches(recall, monkeypatch):
    from app.db import base

    with base.get_db() as conn:
        cur = conn.cursor()
        rows = [(f"{PREFIX}old", "0", 1.0, _node("user", [{"kind": "text", "text": "commonword rareword"}]))]
        rows += [(f"{PREFIX}new{i}", str(j), 100.0 + i * 10 + j,
                  _node("user", [{"kind": "text", "text": "commonword"}]))
                 for i in range(10) for j in range(3)]
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", rows)
        cur.execute("ANALYZE session_recall_text")
    _backfill_all()
    monkeypatch.setattr(recall, "_SEARCH_TERM_ROWS", 4)
    # Only the 4 newest of the 31 rows holding the common term are read...
    common = recall._search_sync(["commonword"], 5, None)
    assert {h.session_id for h in common} == {f"{PREFIX}new9", f"{PREFIX}new8"}
    # ...but a rarer term (and the rows holding every term) is still read in
    # full, so an old session matching both terms ranks first.
    both = recall._search_sync(["commonword", "rareword"], 5, None)
    assert both[0].session_id == f"{PREFIX}old" and both[0].best == 2


def test_terms_across_piece_boundaries_and_longer_than_the_overlap(recall):
    from app.db import base, session_recall

    stride = session_recall.CHUNK_CHARS - session_recall.CHUNK_OVERLAP
    straddler = "boundaryquokka"
    long_term = "longterm" + "abc" * 60  # far longer than the overlap, no whitespace
    text = ("a" * (session_recall.CHUNK_CHARS - 5) + straddler + " "
            + "b" * (2 * stride - session_recall.CHUNK_CHARS - len(straddler) - 30) + long_term + " tail")
    sid = PREFIX + "long"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)",
                    (sid, "0", 1.0, _node("user", [{"kind": "text", "text": text}])))
        cur.execute("SELECT part, length(body) AS n FROM session_recall_text WHERE session_id = %s ORDER BY part",
                    (sid,))
        parts = [dict(r) for r in cur.fetchall()]
        assert len(parts) >= 3 and max(p["n"] for p in parts) <= session_recall.CHUNK_CHARS
    _backfill_all()
    for terms in ([straddler], [long_term], [straddler, long_term]):
        hits = recall._search_sync(terms, 5, None)
        assert [(h.session_id, h.best) for h in hits] == [(sid, len(terms))]


def test_sql_session_cut_keeps_the_python_top_sessions(recall, monkeypatch):
    """Only the arm's best sessions by the scorer's own keys are fetched."""
    terms = ["recallneedle", "quokkaneedle", "capybaraneedle"]
    _corpus()
    legacy = [asdict(h) for h in recall._search_sync(terms, 5, None)]
    _backfill_all()
    monkeypatch.setattr(recall, "_SEARCH_FETCH_SESSIONS", 4)
    indexed = [asdict(h) for h in recall._search_sync(terms, 5, None)]
    # Each arm contributes its top 4; the merged ranking's head is unchanged.
    assert indexed[:4] == legacy[:4]
    assert len({h["session_id"] for h in indexed if h["session_id"].startswith(PREFIX + "ses-n")}) == 4


def _buffers(cur, sql, params):
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
    plan = cur.fetchone()["QUERY PLAN"][0]["Plan"]
    return plan["Shared Hit Blocks"] + plan["Shared Read Blocks"], plan["Actual Rows"]


def test_common_term_over_long_transcripts_reads_bounded_buffers(recall, monkeypatch):
    """The #2084 shape: a common word in many long tool outputs. Legacy recall
    detoasted every matching transcript; the projection reads the newest
    pieces only."""
    from hashlib import md5

    from app.db import base

    with base.get_db() as conn:
        cur = conn.cursor()
        rows = []
        for i in range(400):
            filler = " ".join(md5(f"{i}-{k}".encode()).hexdigest() for k in range(600))  # ~20 KB
            rows.append((f"{PREFIX}big{i % 40}", str(i), float(i), _node("user", [
                {"kind": "reasoning", "encrypted": md5(str(i).encode()).hexdigest() * 200},
                {"kind": "toolresultblock", "content": [{"kind": "text", "text": f"commonword {filler}"}]},
            ])))
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", rows)
        cur.execute("ANALYZE fluid_session_nodes")
        cur.execute("ANALYZE session_recall_text")
    _backfill_all()
    monkeypatch.setattr(recall, "_SEARCH_TERM_ROWS", 50)
    with base.get_db() as conn:
        cur = conn.cursor()
        legacy, legacy_rows = _buffers(
            cur, "SELECT session_id FROM fluid_session_nodes WHERE session_id LIKE %s AND message::text ILIKE %s",
            [PREFIX + "big%", "%commonword%"])
        sql, params = recall._recall_matches("n", "session_id LIKE %s", [PREFIX + "big%"],
                                             [recall._like_pattern("commonword")])
        indexed, indexed_rows = _buffers(cur, f"SELECT * FROM ({sql}) m", params)
    assert legacy_rows == 400 and indexed_rows == 50
    assert indexed * 5 < legacy, (indexed, legacy)
    hits = recall._search_sync(["commonword"], 5, None)
    assert hits and all(h.session_id.startswith(PREFIX + "big") for h in hits)
