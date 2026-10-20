"""Request drain deadlines yield only between chunks; background finishes safely."""
import json
from types import SimpleNamespace

import pytest

from app.db import network as store
from app.network import engine
from tests.core.network.test_pending_apply_budget import backlog, pg_workers, private_cluster  # noqa: F401


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("count,chunk_seconds", [(20, 0.1), (193, 0.25)], ids=["small", "expired"])
def test_request_drain_time_budget_hands_off_and_preserves_state(
        request, monkeypatch, use_postgres, count, chunk_seconds):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, count)
    now, chunks, wakes = [50.0], [], []
    real_chunk, real_one = engine._apply_pending_chunk, engine._apply_one

    def chunk(*args, **kwargs):
        locked = real_chunk(*args, **kwargs)
        if locked:
            chunks.append(True)
            now[0] += chunk_seconds
        return locked

    def one(*args, **kwargs):
        # The deadline covers continuation only, not initial wire apply.
        real_one(*args, **kwargs)
        now[0] += 100.0

    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(engine, "_apply_pending_chunk", chunk)
    monkeypatch.setattr(engine, "_apply_one", one)
    monkeypatch.setattr(engine, "_pending_kick", lambda: wakes.append(True))
    with b.active():
        first = engine.apply_batch(ops[:1])
        assert len(chunks) == 1
        assert first.retry == first.rejected == 0
        if count > engine.APPLY_CHUNK:
            assert first.applied == 1 + engine.APPLY_CHUNK
            assert first._pending_more and wakes == [True]
            with store.get_db() as conn:
                cursor = conn.cursor()
                assert store.get_watermark(cursor, a.device_id, 1) == ops[engine.APPLY_CHUNK]["origin_seq"]
                assert store.count_pending(cursor, a.device_id, 1) == len(ops) - first.applied
        else:
            assert first.applied == len(ops)
            assert not first._pending_more and not wakes
        total, part = first.applied, first
        for _ in range(len(ops)):
            if not part._pending_more:
                break
            part = engine.resume_pending()
            assert part.retry == part.rejected == 0
            total += part.applied
        assert not part._pending_more and total == len(ops)
        with store.get_db() as conn:
            cursor = conn.cursor()
            assert store.count_pending(cursor, a.device_id, 1) == 0
            assert store.get_watermark(cursor, a.device_id, 1) == ops[-1]["origin_seq"]
            assert store.bump_clock(cursor) > max(int(op["clock"]) for op in ops)
        assert engine.apply_batch(ops).duplicate == len(ops)
        rows = b.sql("SELECT seq, event_type, payload FROM session_events WHERE session_id='chat' ORDER BY seq")
        assert [row["seq"] for row in rows] == list(range(1, count + 1))
        assert all(row["event_type"] == "message.user" for row in rows)
        payloads = [json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                    for row in rows]
        assert payloads == [{"content": f"message {i}"} for i in range(count)]


def test_expired_budget_preserves_unvisited_streams(monkeypatch):
    streams = [(f"origin{i}", 1) for i in range(6)]
    now, visited, wakes = [0.0], [], []

    def one(data, ctx, result):
        result._pending_drains.update(streams)

    def chunk(origin, incarnation, ctx, result):
        visited.append(origin)
        result.applied += 1
        now[0] += 0.25
        return True

    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(engine, "_apply_one", one)
    monkeypatch.setattr(engine, "_apply_pending_chunk", chunk)
    monkeypatch.setattr(engine, "_pending_kick", lambda: wakes.append(True))
    ctx = SimpleNamespace(device_id="me", identity=SimpleNamespace(incarnation=1))
    result = engine.apply_batch([{"collection": "x", "op": "upsert"}], ctx=ctx)
    assert visited == ["origin0"]
    assert result.applied == 1 and result._pending_more and wakes == [True]
