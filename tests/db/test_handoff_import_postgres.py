"""Atomic hand-off import and opt-in 20k-row benchmark on disposable PostgreSQL."""
import json
import os
import random
import time
from typing import Any
from unittest.mock import patch

import pytest

from app.db import base, database
from app.network import handoff
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


@pytest.mark.parametrize("table", ["session_events", "fluid_session_events"])
@pytest.mark.parametrize("fail", [False, True])
def test_import_progress_counts_only_committed_rows(chat, monkeypatch, table, fail):
    data = snapshot(events=401 if table == "session_events" else 0, nodes=0,
                    transcript=401 if table == "fluid_session_events" else 0)
    data["_prepare_history"] = True
    observed = []

    class Timing(handoff._Timing):
        def rows(self, completed, total):
            super().rows(completed, total)
            if self.snapshot()["step"] != "install_history." + table:
                return
            # A second connection sees only committed chunks, not the writer's
            # uncommitted rows. Exercise the real producer, not seeded counts.
            with base.get_db() as conn:
                cur = conn.cursor()
                if table == "session_events":
                    cur.execute("SELECT COALESCE(SUM(jsonb_array_length(rows)),0) AS n "
                                "FROM handoff_history_chunks WHERE token=%s AND table_name=%s",
                                (data["token"], table))
                else:
                    cur.execute("SELECT count(*) AS n FROM fluid_session_events WHERE session_id=("
                                "SELECT sdk_session_id FROM handoff_history_chunks "
                                "WHERE token=%s AND table_name='')", (data["token"],))
                assert cur.fetchone()["n"] == completed
            observed.append((completed, total))

    timing = data["_timing"] = Timing("import", chat)
    original = handoff._HistoryCursor.execute

    def execute(cur, sql, *args, **kwargs):
        result = original(cur, sql, *args, **kwargs)
        if fail and observed == [(0, 401), (200, 401)] and sql.startswith("INSERT INTO "):
            raise RuntimeError("injected rollback before progress")
        return result

    monkeypatch.setattr(handoff._HistoryCursor, "execute", execute)
    if fail:
        with pytest.raises(RuntimeError, match="injected rollback"):
            handoff.install_history(chat, data, "/tmp/handoff-lab")
        assert observed == [(0, 401), (200, 401)]
    else:
        handoff.install_history(chat, data, "/tmp/handoff-lab")
        assert observed == [(0, 401), (200, 401), (400, 401), (401, 401)]
    timing.waiting("owner_commit")
    assert "rows_completed" not in timing.snapshot()
    assert "rows_total" not in timing.snapshot()
    handoff._discard_history(chat, data["token"])


def test_incremental_events_skip_unchanged_and_update_changed(chat):
    data = snapshot()
    first_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT seq,xmin::text AS version FROM session_events WHERE session_id=%s ORDER BY seq", (chat,))
        before = {r["seq"]: r["version"] for r in cur.fetchall()}
    data["history"]["session_events"][0]["payload"] = {"text": "changed"}
    second_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    assert first_sdk != second_sdk  # Never reuse a provider's warm native cache.
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT seq,payload,xmin::text AS version FROM session_events WHERE session_id=%s ORDER BY seq", (chat,))
        rows = cur.fetchall()
        assert len(rows) == 10
        assert rows[0]["payload"] == {"text": "changed"}
        assert rows[0]["version"] != before[1]
        assert all(r["version"] == before[r["seq"]] for r in rows[1:])


def test_staged_chunks_leave_old_mirror_and_owner_until_final_commit(chat):
    old = snapshot()
    sdk = handoff.install_history(chat, old, "/tmp/handoff-lab")
    data = snapshot(events=15)
    data["token"] = "new-lease"
    data["_prepare_history"] = True
    new_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": sdk, "owner_device_id": "source"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 10
    handoff._accept_owner(chat, "target", token="new-lease")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": new_sdk, "owner_device_id": "target"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 15


def test_aborted_chunk_import_keeps_previous_mirror(chat, monkeypatch):
    from app.db import network

    old = snapshot()
    sdk = handoff.install_history(chat, old, "/tmp/handoff-lab")
    data = snapshot(nodes=250)
    data["token"] = "aborted-lease"
    data["_prepare_history"] = True
    original = network._insert_rows

    def fail(cur, table, *args, **kwargs):
        if table == "fluid_session_events":
            raise RuntimeError("injected failure after committed node chunks")
        return original(cur, table, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id FROM sessions WHERE id=%s", (chat,))
        assert dict(cur.fetchone()) == {"sdk_session_id": sdk, "owner_device_id": "source"}
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 10
        cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE token='aborted-lease'")
        assert cur.fetchone()["n"] == 0




def test_divergent_compaction_prunes_stale_ids_before_upsert(chat):
    data = snapshot()
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    survivor = dict(data["history"]["session_events"][-1], id=data["history"]["session_events"][0]["id"])
    data["history"]["session_events"] = [survivor]
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,seq FROM session_events WHERE session_id=%s", (chat,))
        assert [dict(row) for row in cur.fetchall()] == [{"id": survivor["id"], "seq": survivor["seq"]}]


def test_legacy_message_reconciliation_is_batched_and_retains_unchanged_versions(chat, monkeypatch):
    from app.db import network

    data = snapshot(messages=601)
    batches = []
    insert = network._insert_rows

    def record(cur, table, columns, rows, *args, **kwargs):
        if table == "session_messages":
            batches.append(len(rows))
        return insert(cur, table, columns, rows, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", record)
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    assert batches == [601]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,xmin::text AS version,jsonl_uuid FROM session_messages WHERE session_id=%s", (chat,))
        before = {r["jsonl_uuid"]: (r["id"], r["version"]) for r in cur.fetchall()}
    data["history"]["session_messages"][0]["content"] = "edited message"
    data["history"]["session_messages"].pop()
    batches.clear()
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    assert batches == [1]
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,xmin::text AS version,jsonl_uuid,content FROM session_messages WHERE session_id=%s", (chat,))
        rows = cur.fetchall()
        assert len(rows) == 600
        assert next(r for r in rows if r["jsonl_uuid"] == "lab-message-0")["content"] == "edited message"
        assert all((r["id"], r["version"]) == before[r["jsonl_uuid"]] for r in rows if r["jsonl_uuid"] != "lab-message-0")




def test_receipt_capable_staging_survives_lease_then_requires_explicit_receipt(chat, monkeypatch):
    from types import SimpleNamespace
    from psycopg2.extras import Json

    ctx = SimpleNamespace(device_id="target")
    monkeypatch.setattr(handoff.engine, "context", lambda: ctx)
    monkeypatch.setattr(handoff.engine, "require_context", lambda: ctx)
    data = snapshot()
    data["_prepare_history"] = True
    sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source")
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE handoff_history_chunks SET created_at=%s WHERE session_id=%s",
                              ("2000-01-01T00:00:00+00:00", chat))
    assert handoff.recover_history_staging() == 0
    assert handoff.admission_held(chat)
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id='target',placement=%s WHERE id=%s",
                              (Json({"where": "runner:target"}), chat))
    assert handoff.recover_history_staging() == 0  # Missing token is not a receipt.
    assert database.get_session(chat)["sdk_session_id"] is None
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET placement=%s WHERE id=%s",
                              (Json({"where": "runner:target", "handoff": {"token": data["token"]}}), chat))
    assert handoff.recover_history_staging() == 1
    assert database.get_session(chat)["sdk_session_id"] == sdk
    assert not handoff.admission_held(chat)


def test_legacy_receipt_recovers_latest_context_and_retires_older_staging(chat, monkeypatch):
    from types import SimpleNamespace
    from psycopg2.extras import Json

    ctx = SimpleNamespace(device_id="target")
    monkeypatch.setattr(handoff.engine, "context", lambda: ctx)
    monkeypatch.setattr(handoff.engine, "require_context", lambda: ctx)
    data = snapshot()
    data.update(token="older-legacy", _prepare_history=True)
    old_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source", receipt_required=False)
    data["token"] = "newer-legacy"
    sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source", receipt_required=False)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET owner_device_id='target',placement=%s WHERE id=%s",
                    (Json({"where": "runner:target"}), chat))
    assert handoff.recover_history_staging() == 1
    assert database.get_session(chat)["sdk_session_id"] == sdk
    assert not handoff.admission_held(chat)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM handoff_history_chunks WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM fluid_session_nodes WHERE session_id=%s", (old_sdk,))
        assert cur.fetchone()["n"] == 0


def test_receipt_rewind_removes_only_portable_rows(chat):
    with base.get_db() as conn:
        cur = conn.cursor()
        for receipt in ("retained", "rewound"):
            cur.execute("INSERT INTO pending_interactions (id,session_id,kind,state) "
                        "VALUES (%s,%s,'model_switch_approval','answered')", (receipt, chat))
        cur.execute("INSERT INTO pending_interactions (id,session_id,kind,state) "
                    "VALUES ('local-permission',%s,'permission','pending')", (chat,))
    data = {"sdk_session_id": None, "history": handoff._history(database.get_session(chat))}
    data["history"]["pending_interactions"] = [r for r in data["history"]["pending_interactions"] if r["id"] == "retained"]
    handoff.install_history(chat, data, "/tmp/handoff-lab")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM pending_interactions WHERE session_id=%s ORDER BY id", (chat,))
        assert [r["id"] for r in cur.fetchall()] == ["local-permission", "retained"]
        cur.execute("DELETE FROM pending_interactions WHERE session_id=%s", (chat,))


@pytest.mark.parametrize("publisher", ["accept", "finalize"])
@pytest.mark.parametrize("legacy_source", [False, True])
@pytest.mark.parametrize("next_owner", ["target", "newer-owner"])
def test_slow_publish_does_not_hold_session_row_and_revalidates_owner(chat, monkeypatch, next_owner, publisher, legacy_source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.db import network

    previous_sdk = handoff.install_history(chat, snapshot(), "/tmp/handoff-lab")
    data = snapshot(events=15)
    data.update(token="slow-publication", _prepare_history=True)
    new_sdk = handoff.install_history(chat, data, "/tmp/handoff-lab")
    handoff._mark_commit_sent(chat, data["token"], "source", receipt_required=not legacy_source)
    started, release = Event(), Event()
    insert = network._insert_rows

    def pause(cur, table, *args, **kwargs):
        if table == "session_events":
            started.set()
            assert release.wait(10), "publication was not released"
        return insert(cur, table, *args, **kwargs)

    monkeypatch.setattr(network, "_insert_rows", pause)
    if publisher == "finalize":
        from types import SimpleNamespace
        monkeypatch.setattr(handoff.engine, "require_context", lambda: SimpleNamespace(device_id="target"))
        handoff._stage_wakes(chat, "source", data, None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        if publisher == "accept":
            future = executor.submit(handoff._accept_owner, chat, "target", token=data["token"])
        else:
            future = executor.submit(handoff._finalize_wakes, chat, "source", {"token": data["token"]})
        try:
            assert started.wait(10), "publication did not start"
            with base.get_db() as conn:
                cur = conn.cursor()
                cur.execute("SET LOCAL lock_timeout = '250ms'")
                # A real ownership sync can arrive while the cold delta is written.
                cur.execute("UPDATE sessions SET owner_device_id=%s,placement=%s,title='Concurrent write' WHERE id=%s",
                            (next_owner, json.dumps({} if legacy_source else {"handoff": {"token": data["token"]}}), chat))
        finally:
            release.set()
        if next_owner == "target":
            future.result(timeout=10)
        else:
            with pytest.raises(ValueError, match="ownership changed"):
                future.result(timeout=10)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id,owner_device_id,title FROM sessions WHERE id=%s", (chat,))
        row = cur.fetchone()
        assert row["owner_device_id"] == next_owner
        assert row["title"] == "Concurrent write"
        assert row["sdk_session_id"] == (new_sdk if next_owner == "target" else previous_sdk)
        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (chat,))
        assert cur.fetchone()["n"] == (15 if next_owner == "target" else 10)


def snapshot(events=10, nodes=3, transcript=12, messages=0, width=100):
    from fluid.messages import Message, Text

    rng = random.Random(295)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 "
    text = lambda: "".join(rng.choices(alphabet, k=width))
    return {"token": "lab-token", "sdk_session_id": "ses_lab-source", "history": {
        "session_events": [{"id": f"lab-event-{i}", "seq": i + 1,
            "event_type": "message.assistant", "payload": {"content": text(), "kind": "text"},
            "created_at": "2026-10-19T00:00:00Z"} for i in range(events)],
        "fluid_sessions": [{"head_node_id": f"n{nodes-1}", "created_at": 1.0}],
        "fluid_session_nodes": [{"node_id": f"n{i}", "parent_id": f"n{i-1}" if i else None,
            "ts": float(i), "message": Message("assistant", [Text(text())]).to_dict()} for i in range(nodes)],
        "fluid_session_events": [{"seq": i, "event": {"type": "text", "text": text()}}
                                 for i in range(transcript)],
        "session_messages": [{"role": "user" if i % 2 == 0 else "assistant", "content": text(),
                              "metadata": {"lab": i}, "jsonl_uuid": f"lab-message-{i}",
                              "created_at": "2026-10-19T00:00:00Z"} for i in range(messages)],
    }}


@pytest.fixture(scope="module", autouse=True)
def upgraded_schema(pg_schema):
    # Reused disposable databases exercise the same additive column upgrades
    # as an installed worker, not just CREATE TABLE IF NOT EXISTS.
    database.init_database()


@pytest.fixture
def chat():
    sid = "handoff-lab"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM session_events WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM session_event_counters WHERE session_id=%s", (sid,))
        cur.execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Import lab','source')", (sid,))
    yield sid
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT sdk_session_id FROM sessions WHERE id=%s", (sid,))
        sdk = cur.fetchone()["sdk_session_id"]
        cur.execute("DELETE FROM sessions WHERE id=%s", (sid,))
        cur.execute("DELETE FROM session_events WHERE session_id=%s", (sid,))
        cur.execute("DELETE FROM session_event_counters WHERE session_id=%s", (sid,))
        for table in ("fluid_sessions", "fluid_session_nodes", "fluid_session_events", "session_store_entries"):
            cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s,%s)", (sdk, "ses_lab-source"))


def _merge_between_turns(chat: str) -> dict[str, int]:
    """Startup's own pass and gate, with durable run rows opening and closing it."""
    import threading

    from app.core.startup import _no_foreground_turn
    from app.db import history_indexing

    stop = threading.Event()
    turns = 0

    def run_turns() -> None:
        nonlocal turns
        while not stop.wait(7.0):
            turns += 1
            run = f"history-lab-run-{turns}"
            with base.get_db() as conn:
                conn.cursor().execute(
                    "INSERT INTO runs(id,session_id,runner_id,fence_token,state,lease_expires_at) "
                    "VALUES (%s,%s,'lab',1,'active',NOW() + interval '1 minute')", (run, chat))
            stop.wait(3.0)
            with base.get_db() as conn:
                conn.cursor().execute("DELETE FROM runs WHERE id=%s", (run,))

    driver = threading.Thread(target=run_turns)
    driver.start()
    passes = 1
    try:
        deadline = time.monotonic() + 1800
        while not history_indexing.run_pass(can_work=_no_foreground_turn):
            assert time.monotonic() < deadline, "merge exceeded the lab time budget"
            passes += 1
            time.sleep(5)
    finally:
        stop.set()
        driver.join()
    return {"gated_passes": passes, "turns_started": turns}


@pytest.mark.skipif(os.environ.get("HANDOFF_LAB_SEARCH") != "1",
                    reason="opt-in search over a near-full history pending list")
def test_near_full_history_list_search_lab(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import statistics

    from app.core.provider import fluid_pg_store
    from app.db import history_indexing as hi, recall_index_retirement, session_recall, sessions
    from tests.db.test_history_indexing_postgres import finish_migration

    assert session_recall.run_backfill(max_seconds=60.0)
    for _ in range(30):
        if recall_index_retirement.run_pass(seconds=60.0):
            break
    finish_migration()
    index = "idx_session_recall_text_trgm_e_h2"
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")
        # Worst case: nothing else gets to merge the list first.
        cur.execute("ALTER TABLE session_recall_text SET (autovacuum_enabled=false)")

    def pending_kb() -> int:
        with base.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT n_pending_pages * 8 AS kb FROM gin_metapage_info(get_raw_page(%s, 0))",
                        (index,))
            return int(cur.fetchone()["kb"])

    full = dict(events=7823, nodes=1741, transcript=10768, messages=4000, width=3100)
    target = int(hi.HISTORY_PENDING_KB * 0.91)
    imports: list[dict[str, Any]] = []
    gain = 0
    terms_from: dict[str, Any] | None = None
    while (before := pending_kb()) < target:
        # Rounded down, so the last partial import lands just past the target.
        divisor = max(1, gain // (target - before))
        n = len(imports)
        sid = f"handoff-search-lab-{n}"
        data = snapshot(**{k: (v // divisor if k != "width" else v) for k, v in full.items()})
        data["token"] = f"lab-token-{n}"
        data["sdk_session_id"] = f"ses_search-lab-{n}"
        history = data["history"]
        terms_from = terms_from or history
        for row in history["session_events"]:
            row["id"] = f"{sid}-{row['id']}"
        for row in history["session_messages"]:
            row["jsonl_uuid"] = f"{sid}-{row['jsonl_uuid']}"
        with base.get_db() as conn:
            conn.cursor().execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Search lab','lab-remote')",
                                  (sid,))
        started = time.monotonic()
        handoff.install_history(sid, data, "/tmp/handoff-search-lab")
        imports.append({"seconds": round(time.monotonic() - started, 2), "divisor": divisor,
                        "pending_kb": pending_kb()})
        added = imports[-1]["pending_kb"] - before
        assert added > 0, f"the list stopped growing at {before} KB after {imports}"
        gain = max(gain, added * divisor)
        with base.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET sdk_session_id=NULL WHERE id=%s", (sid,))
    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "lab-local")
    monkeypatch.setattr(fluid_pg_store, "_worker_name", lambda _: "Lab mirror")

    assert terms_from is not None
    texts = [row["payload"]["content"] for row in terms_from["session_events"]]
    cut = lambda row, size: texts[row][100:100 + size]  # noqa: E731
    shapes = {
        "one_rare_control": [cut(11, 12)],
        "two_rare": [cut(23, 12), cut(301, 12)],
        "three_mixed": [cut(47, 12), cut(811, 5), cut(1203, 4)],
        "five_short": [cut(n, 4) for n in (3, 99, 512, 2048, 4096)],
        "two_broad": [cut(5, 3), cut(6, 3)],
        "two_absent": ["zzqqxx00zzqq", "qqzz00xxqqzz"],
        "long_phrase": [cut(77, 60), cut(78, 60)],
    }

    def measure() -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for name, terms in shapes.items():
            times: list[float] = []
            hits = 0
            for _ in range(3):
                started = time.perf_counter()
                hits = len(fluid_pg_store._search_sync(terms, 10, None))
                times.append(time.perf_counter() - started)
            started = time.perf_counter()
            listed = len(sessions.search_sessions(" ".join(terms)))
            results[name] = {"seconds": [round(t, 3) for t in times],
                             "median": round(statistics.median(times), 3), "hits": hits,
                             "search_sessions_seconds": round(time.perf_counter() - started, 3),
                             "search_sessions_hits": listed}
        return results

    pending = pending_kb()
    unmerged = measure()
    # A starved merge: the next long chat crosses the limit under production's
    # statement limit and pays PostgreSQL's inline merge itself.
    with base.get_db() as conn:
        conn.cursor().execute("ALTER DATABASE lab SET statement_timeout = '30s'")
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    sid = "handoff-search-lab-crossing"
    data = snapshot(**full)
    data["token"] = "lab-token-crossing"
    data["sdk_session_id"] = "ses_search-lab-crossing"
    for row in data["history"]["session_events"]:
        row["id"] = f"{sid}-{row['id']}"
    for row in data["history"]["session_messages"]:
        row["jsonl_uuid"] = f"{sid}-{row['jsonl_uuid']}"
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions(id,title,owner_device_id) VALUES (%s,'Search lab','lab-remote')",
                              (sid,))
    from tests.db.history_index_lab import LiveInserts
    crossing_sampler = LiveInserts(offset=3_000_000)
    crossing_sampler.start()
    started = time.monotonic()
    try:
        handoff.install_history(sid, data, "/tmp/handoff-search-lab")
        crossing: dict[str, Any] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 — the outcome is the measurement
        crossing = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    elapsed = round(time.monotonic() - started, 2)
    crossing.update(seconds=elapsed, pending_kb_after=pending_kb(),
                    live_latency_seconds=crossing_sampler.finish())
    started = time.monotonic()
    assert hi.flush_pending(quiet=0)
    merge_seconds = time.monotonic() - started
    merged = measure()
    with base.get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id='lab-local' "
                              "WHERE id LIKE 'handoff-search-lab-%%'")
    adopted = measure()
    with capsys.disabled():
        print("HANDOFF_SEARCH_LAB " + json.dumps({
            "limit_kb": hi.HISTORY_PENDING_KB, "pending_kb": pending,
            "full_fraction": round(pending / hi.HISTORY_PENDING_KB, 3),
            "imports": imports, "crossing_import": crossing, "merge_seconds": round(merge_seconds, 1),
            "unmerged": unmerged, "merged": merged, "adopted_merged": adopted}))


@pytest.mark.skipif(os.environ.get("HANDOFF_LAB") != "1", reason="opt-in 20k-row measurement")
def test_import_lab(chat: str, capsys: pytest.CaptureFixture[str]) -> None:
    from app.db import network, session_recall
    from app.db.schema_postgres import create_session_recall_text

    divisor = int(os.environ.get("HANDOFF_LAB_DIVISOR", "1"))
    assert divisor >= 1
    data = snapshot(events=7823 // divisor, nodes=1741 // divisor,
                    transcript=10768 // divisor, messages=4000 // divisor, width=3100)
    payload_bytes = len(json.dumps(data))
    timing = handoff._Timing("lab", chat)
    data["_timing"] = timing
    if os.environ.get("HANDOFF_LAB_RETIRED") == "1":
        # A worker that finished the projection backfill and #1170's retirement.
        from app.db import recall_index_retirement
        assert session_recall.run_backfill(max_seconds=60.0)
        for _ in range(30):
            if recall_index_retirement.run_pass(seconds=60.0):
                break
        else:
            pytest.fail("transcript GIN retirement did not finish")
    else:
        with base.get_db() as conn:
            create_session_recall_text(conn.cursor())
            session_recall.install_objects(conn.cursor())
        session_recall.install_triggers()
    if os.environ.get("HANDOFF_LAB_DEFERRED") == "1":
        from tests.db.test_history_indexing_postgres import finish_migration
        finish_migration()
        from app.db.history_indexing import HISTORY_PENDING_KB, INDEXES
        pending_kb = int(os.environ.get("HANDOFF_LAB_HISTORY_PENDING_KB", HISTORY_PENDING_KB))
        assert 64 <= pending_kb <= HISTORY_PENDING_KB
        with base.get_db() as conn:
            cur = conn.cursor()
            for name in INDEXES:
                cur.execute("SELECT to_regclass(%s) AS name", (name + "_h2",))
                if cur.fetchone()["name"]:
                    cur.execute(f"ALTER INDEX {name}_h2 SET (gin_pending_list_limit={pending_kb})")
    # Reproduce a receiver that already mirrors the owner's event stream.
    from psycopg2.extras import Json
    with base.get_db() as conn:
        columns = ("session_id", *handoff.TABLES["session_events"])
        rows = [(chat, *[Json(row.get(c)) if c in handoff.JSON_COLUMNS and row.get(c) is not None
                         else row.get(c) for c in handoff.TABLES["session_events"]])
                for row in data["history"]["session_events"]]
        if os.environ.get("HANDOFF_LAB_COLD") != "1":
            network._insert_rows(conn.cursor(), "session_events", columns, rows,
                                 "ON CONFLICT DO NOTHING", batch=200)
    if os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") == "1":
        with base.get_db() as conn:
            conn.cursor().execute("DROP INDEX idx_fluid_recall_text_trgm")
    from tests.db.history_index_lab import LiveInserts, wal_snapshot, wal_summary
    idle_sampler = LiveInserts(offset=2_000_000)
    idle_sampler.start()
    try:
        time.sleep(30)
    finally:
        idle_latency = idle_sampler.finish()
    with capsys.disabled():
        print("HANDOFF_IDLE_LAB " + json.dumps({"seconds": 30,
              "live_latency_seconds": idle_latency}))
    measured = {}
    original = network._insert_rows

    def insert(cur: Any, table: str, *args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        result = original(cur, table, *args, **kwargs)
        measured[table] = measured.get(table, 0) + time.monotonic() - start
        return result

    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_current_wal_insert_lsn() AS lsn")
        lsn = cur.fetchone()["lsn"]
    import_wal_start = wal_snapshot()
    sampler = LiveInserts()
    sampler.start()
    start = time.monotonic()
    try:
        with patch.object(network, "_insert_rows", insert):
            handoff.install_history(chat, data, "/tmp/handoff-lab")
        elapsed = time.monotonic() - start
    finally:
        live_latency = sampler.finish()
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s) AS bytes", (lsn,))
        wal = int(cur.fetchone()["bytes"])
    with capsys.disabled():
        print("HANDOFF_LAB " + json.dumps({"rows": sum(len(v) for v in data["history"].values()),
            "bytes": payload_bytes, "seconds": elapsed, "wal_bytes": wal,
            "lab_settings": {"divisor": divisor,
                             "history_pending_kb": os.environ.get("HANDOFF_LAB_HISTORY_PENDING_KB"),
                             "batch_rows": os.environ.get("HANDOFF_LAB_BATCH_ROWS"),
                             "pause_seconds": os.environ.get("HANDOFF_LAB_PAUSE", "0.1"),
                             "checkpoint": os.environ.get("HANDOFF_LAB_CHECKPOINT") == "1"},
            "wal_diagnostics": wal_summary(import_wal_start, "import"),
            "live_latency_seconds": live_latency,
            "deferred": os.environ.get("HANDOFF_LAB_DEFERRED") == "1",
            "cold_receiver": os.environ.get("HANDOFF_LAB_COLD") == "1",
            "tables_seconds": measured, "publish_metrics": timing.steps,
            "import_metrics": data.get("_history_metrics"),
            "duplicate_node_index": os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") != "1"}, sort_keys=True))
    if os.environ.get("HANDOFF_LAB_DEFERRED") == "1":
        from app.db import history_indexing
        if os.environ.get("HANDOFF_LAB_CHECKPOINT") == "1":
            with base.get_db() as conn:
                conn.cursor().execute("CHECKPOINT")
        catchup_wal_start = wal_snapshot()
        catchup_sampler = LiveInserts(offset=1_000_000)
        catchup_sampler.start()
        catchup_start = time.monotonic()
        promoted = 0
        complete = False
        args = {}
        flushed = None
        if os.environ.get("HANDOFF_LAB_FLUSH") == "1":
            from app.db import sessions
            search = time.perf_counter()
            sessions.search_sessions("handofflabneedle")
            searched_pending = time.perf_counter() - search
            flushed = (_merge_between_turns(chat) if os.environ.get("HANDOFF_LAB_GATED") == "1"
                       else history_indexing.flush_pending(quiet=0))
            search = time.perf_counter()
            sessions.search_sessions("handofflabneedle")
            flushed = {"merged": flushed, "search_pending_s": searched_pending,
                       "search_merged_s": time.perf_counter() - search}
        if os.environ.get("HANDOFF_LAB_BATCH_ROWS"):
            args["rows"] = int(os.environ["HANDOFF_LAB_BATCH_ROWS"])
        try:
            while time.monotonic() - catchup_start < 1800:
                n = history_indexing.promote_batch(**args)
                promoted += n
                if not n:
                    complete = True
                    break
                time.sleep(float(os.environ.get("HANDOFF_LAB_PAUSE", "0.1")))
        finally:
            catchup_latency = catchup_sampler.finish()
            elapsed = time.monotonic() - catchup_start
            with base.get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s) AS bytes",
                            (lsn,))
                total_wal = int(cur.fetchone()["bytes"])
                pending = {}
                for table in history_indexing.TABLES:
                    cur.execute(f"SELECT count(*) AS n FROM {table} WHERE history_index_state=1")
                    pending[table] = cur.fetchone()["n"]
            with capsys.disabled():
                print("HANDOFF_CATCHUP_LAB " + json.dumps({"seconds": elapsed,
                      "complete": complete, "pending": pending, "batch_args": args,
                      "promoted_rows": promoted, "flushed_pages": flushed, "total_import_and_catchup_wal": total_wal,
                      "wal_diagnostics": wal_summary(catchup_wal_start, "catchup"),
                      "live_latency_seconds": catchup_latency}))
        assert complete, "catch-up exceeded the lab time budget"
    if os.environ.get("HANDOFF_LAB_DROP_DUPLICATE") == "1":
        with base.get_db() as conn:
            create_session_recall_text(conn.cursor())
