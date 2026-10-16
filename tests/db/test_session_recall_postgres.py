"""SessionSearch's maintained text projection, on opt-in disposable PostgreSQL.

Covers the triggers that keep ``session_recall_text`` exact (and never fail a
write, nor cost much on a huge one), their short per-table install, the
resumable batched backfill and its readiness gate, and the indexed search
path: ranked parity with the legacy query while every term is rarer than the
per-term cap, literal LIKE wildcards, the newest-first cap for common terms,
rows too long to index, and scoped readers.
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
    cur.execute("DELETE FROM api_users WHERE id LIKE %s", (PREFIX + "%",))
    cur.execute("DELETE FROM session_recall_text WHERE session_id LIKE %s", (PREFIX + "%",))
    # Other suites share this database and must keep the legacy path.
    cur.execute("DELETE FROM session_recall_backfill")


@pytest.fixture(autouse=True)
def clean():
    from app.db import base, session_recall

    with base.get_db() as conn:
        assert session_recall.install_objects(conn.cursor())
    assert session_recall.install_triggers()
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


def test_bootstrap_creates_only_objects_and_triggers_install_per_table():
    from app.db import base, session_recall

    session_recall.uninstall()
    with base.get_db() as conn:
        cur = conn.cursor()
        # The removal path (python -m app.maintenance.session_recall_projection --remove).
        cur.execute("SELECT COUNT(*) AS n FROM pg_trigger WHERE tgname LIKE 'trg_session_recall%%'")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT to_regclass('session_recall_text') AS t, to_regproc('recall_pieces') AS f")
        assert dict(cur.fetchone()) == {"t": None, "f": None}
        from app.db.schema_postgres import create_postgres_schema

        create_postgres_schema(cur)
        # Bootstrap never locks a transcript table: no trigger yet.
        assert not session_recall.installed(cur)
        assert not session_recall.ready(cur)
    assert session_recall.install_triggers()
    with base.get_db() as conn:
        cur = conn.cursor()
        assert session_recall.installed(cur)
        assert not session_recall.ready(cur)
        assert session_recall.install_objects(cur)  # idempotent
    assert session_recall.install_triggers()


def test_triggers_keep_projection_to_scored_text_on_every_write():
    from app.db import base, session_recall

    sid = PREFIX + "native"
    _backfill_all()
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
        cur.execute("DELETE FROM session_store_entries WHERE session_id = %s", (sdk,))
        assert _projection(cur, "s", sdk) == {}
        # No write above went through the triggers' error handler.
        assert session_recall.ready(cur)


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

    _backfill_all()
    name = session_recall._trigger_name("nodes")
    with base.get_db() as conn:
        conn.cursor().execute(f"DROP TRIGGER {name} ON fluid_session_nodes")
    with base.get_db() as blocker:
        blocker.cursor().execute("LOCK TABLE fluid_session_nodes IN ROW EXCLUSIVE MODE")
        started = time.perf_counter()
        assert session_recall.install_triggers(lock_timeout_ms=50) is False
        assert time.perf_counter() - started < 1
        with base.get_db() as conn:
            cur = conn.cursor()
            assert not session_recall.installed(cur)
            assert not session_recall.ready(cur)
            cur.execute("SELECT current_setting('lock_timeout') AS v")
            assert cur.fetchone()["v"] == "0"
            # The other tables kept (or got) their triggers in their own transactions.
            assert session_recall._source_triggers(cur, "session_events")
    assert session_recall.install_triggers()
    with base.get_db() as conn:
        cur = conn.cursor()
        assert session_recall.installed(cur)
        # Rows written while the trigger was missing are copied again.
        cur.execute("SELECT source, done FROM session_recall_backfill ORDER BY source")
        assert {r["source"]: r["done"] for r in cur.fetchall()} == {"e": True, "n": False, "s": True}


def test_a_failing_projection_never_fails_the_write_and_is_rebuilt():
    from app.db import base, session_recall

    sid = PREFIX + "fail"
    _backfill_all()
    with base.get_db() as conn:
        conn.cursor().execute("DROP TABLE session_recall_text")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)",
                    (sid, "a", 1.0, _node("user", [{"kind": "text", "text": "Survives"}])))
        cur.execute("INSERT INTO session_events(id,session_id,seq,event_type,payload) VALUES (%s,%s,%s,%s,%s)",
                    (PREFIX + "fe", sid, 1, "message.user", Json({"content": "Survives too"})))
        cur.execute("SELECT source, done FROM session_recall_backfill ORDER BY source")
        assert {r["source"]: r["done"] for r in cur.fetchall()} == {"e": False, "n": False, "s": True}
        assert not session_recall.ready(cur)
    assert session_recall.run_backfill(pause_seconds=0)
    with base.get_db() as conn:
        cur = conn.cursor()
        assert _projection(cur, "n", sid) == {"a": "survives"}
        assert _projection(cur, "e", sid) == {PREFIX + "fe": "survives too"}


def test_a_huge_write_costs_about_the_same_as_without_the_trigger():
    """Linear chunking, and nothing past ``MAX_ROW_CHARS`` is indexed."""
    from app.db import base, session_recall

    name = session_recall._trigger_name("nodes")
    timings = {}
    with base.get_db() as conn:
        cur = conn.cursor()
        for size in (session_recall.MAX_ROW_CHARS, 3_000_000):
            text = ("é漢字abcdefg " * (size // 11 + 1))[:size]
            for trigger in (False, True):
                cur.execute(f"ALTER TABLE fluid_session_nodes {'ENABLE' if trigger else 'DISABLE'} TRIGGER {name}")
                started = time.perf_counter()
                cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)", (
                    PREFIX + "huge", f"{size}-{trigger}", 1.0,
                    _node("user", [{"kind": "toolresultblock", "content": [{"kind": "text", "text": text}]}])))
                timings[size, trigger] = time.perf_counter() - started
        cur.execute("SELECT row_key, COUNT(*) AS n, MIN(part) AS first FROM session_recall_text "
                    "WHERE session_id = %s GROUP BY row_key", (PREFIX + "huge",))
        rows = {r["row_key"]: (r["n"], r["first"]) for r in cur.fetchall()}
    cap = session_recall.MAX_ROW_CHARS
    assert rows[f"{cap}-True"][0] > cap // 1000 and rows["3000000-True"] == (1, session_recall.OVERFLOW_PART)
    # A whole row at the cap (pieces, trigram entries) stays well under a second...
    assert timings[cap, True] < timings[cap, False] + 0.5, timings
    # ...and a 3 MB one costs about what the insert costs anyway.
    assert timings[3_000_000, True] < 2 * timings[3_000_000, False] + 0.1, timings


def test_rows_too_long_to_index_are_matched_in_their_source(recall):
    from app.db import base, session_recall

    tail = "x" * (session_recall.MAX_ROW_CHARS + 10) + " overflowneedle tail"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions(id,sdk_session_id,title,owner_device_id) VALUES (%s,%s,%s,%s)",
                    (PREFIX + "ovr", None, "Remote", "test-remote"))
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES (%s,%s,%s,%s)",
                    (PREFIX + "ovn", "0", 1.0, _node("user", [{"kind": "text", "text": tail}])))
        cur.execute("INSERT INTO session_store_entries(project_key,session_id,entry) VALUES (%s,%s,%s)",
                    ("p", PREFIX + "ovs", Json({"type": "user", "message": {"content": tail}})))
        execute_values(cur, "INSERT INTO session_events(id,session_id,seq,event_type,payload) VALUES %s", [
            (PREFIX + "ove1", PREFIX + "ovr", 1, "message.user", Json({"content": "short question"})),
            (PREFIX + "ove2", PREFIX + "ovr", 2, "message.assistant", Json({"content": tail})),
        ])
        cur.execute("SELECT source, part FROM session_recall_text WHERE session_id LIKE %s AND part < 0 "
                    "ORDER BY source", (PREFIX + "ov%",))
        assert [(r["source"], r["part"]) for r in cur.fetchall()] == [("e", -1), ("n", -1), ("s", -1)]
    _backfill_all()
    hits = recall._search_sync(["overflowneedle"], 5, None)
    assert sorted(h.session_id for h in hits) == sorted([PREFIX + "ovn", PREFIX + "ovs", PREFIX + "ovr"])
    # The overflowed event still folds: the session's short message is found too.
    hits = recall._search_sync(["short question"], 5, None)
    assert [h.session_id for h in hits] == [PREFIX + "ovr"]


def test_backfill_never_waits_on_a_row_being_written():
    import psycopg2.errors

    from app.db import base, session_recall

    with base.get_db() as conn:
        cur = conn.cursor()
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", [
            (f"{PREFIX}lk", f"n{i}", float(i), _node("user", [{"kind": "text", "text": f"Row {i}"}]))
            for i in range(3)
        ])
        cur.execute("DELETE FROM session_recall_text WHERE session_id LIKE %s", (PREFIX + "%",))
        cur.execute("DELETE FROM session_recall_backfill")
    with base.get_db() as writer:
        writer.cursor().execute("SELECT 1 FROM fluid_session_nodes WHERE session_id = %s AND node_id = 'n1' "
                                "FOR UPDATE", (PREFIX + "lk",))
        started = time.perf_counter()
        with pytest.raises(psycopg2.errors.LockNotAvailable), base.get_db() as conn:
            session_recall.backfill_step(conn.cursor(), "n", 200)
        assert time.perf_counter() - started < 1
    _backfill_all()
    with base.get_db() as conn:
        assert _projection(conn.cursor(), "n", PREFIX + "lk") == {"n0": "row 0", "n1": "row 1", "n2": "row 2"}


def test_backfill_batches_are_bounded_by_bytes():
    from app.db import base, session_recall

    with base.get_db() as conn:
        cur = conn.cursor()
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", [
            (f"{PREFIX}by", f"n{i}", float(i),
             _node("user", [{"kind": "text", "text": " ".join(str(i * 7919 + k) for k in range(3000))}]))
            for i in range(6)
        ])
        cur.execute("DELETE FROM session_recall_text WHERE session_id LIKE %s", (PREFIX + "%",))
        cur.execute("DELETE FROM session_recall_backfill")
    steps = 0
    while True:
        with base.get_db() as conn:
            steps += 1
            # A batch stops after the row that crosses its byte budget.
            if session_recall.backfill_step(conn.cursor(), "n", 200, max_bytes=1):
                break
    # One row per batch (other suites' rows may share the table), then "done".
    assert steps >= 7
    with base.get_db() as conn:
        assert len(_projection(conn.cursor(), "n", PREFIX + "by")) == 6


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


def test_scoped_reader_keeps_its_older_sessions_under_the_common_term_cap(recall, monkeypatch):
    """The newest-first cap applies to what the reader may see, not to the
    owner's newer sessions it would drop afterwards."""
    from app.db import base

    with base.get_db() as conn:
        cur = conn.cursor()
        sessions = [(f"{PREFIX}own{i}", f"{PREFIX}ownses{i}", None, None, None) for i in range(10)]
        sessions += [(f"{PREFIX}key{i}", f"{PREFIX}keyses{i}", "rp-user", None, None) for i in range(2)]
        sessions += [(f"{PREFIX}keychat", f"{PREFIX}keychatses", "rp-user", None, None),
                     (f"{PREFIX}keywork", f"{PREFIX}keyworkses", None, True, f"{PREFIX}key0")]
        cur.execute("INSERT INTO api_users(id,name,api_key_hash) VALUES ('rp-user','Key','x')")
        execute_values(cur, "INSERT INTO sessions(id,sdk_session_id,api_user_id,is_worker,parent_session_id) "
                       "VALUES %s", sessions)
        rows = [(f"{PREFIX}ownses{i}", str(j), 100.0 + i * 10 + j, _node("user", [{"kind": "text", "text": "commonword"}]))
                for i in range(10) for j in range(3)]
        rows += [(f"{PREFIX}keyses{i}", "0", 1.0 + i, _node("user", [{"kind": "text", "text": "commonword"}]))
                 for i in range(2)]
        rows.append((f"{PREFIX}keyworkses", "0", 5.0, _node("user", [{"kind": "text", "text": "commonword"}])))
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", rows)
    _backfill_all()
    monkeypatch.setattr(recall, "_SEARCH_TERM_ROWS", 4)
    scoped = recall._search_sync(["commonword"], 5, f"{PREFIX}keychat")
    assert {h.session_id for h in scoped} == {f"{PREFIX}keyses0", f"{PREFIX}keyses1", f"{PREFIX}keyworkses"}
    owner = recall._search_sync(["commonword"], 5, None)
    assert {h.session_id for h in owner} == {f"{PREFIX}ownses9", f"{PREFIX}ownses8"}


def _vacuum(*tables):
    """Fresh visibility map and statistics: dead rows other suites left in the
    shared database would otherwise inflate the buffer counts."""
    import os

    import psycopg2

    conn = psycopg2.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        conn.autocommit = True
        for table in tables:
            conn.cursor().execute(f"VACUUM ANALYZE {table}")
    finally:
        conn.close()


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
    _backfill_all()
    _vacuum("fluid_session_nodes", "session_recall_text")
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
