"""Per-stream retry backoff must not starve healthy buffered heads."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.db import network as store
from app.network import engine
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: F401


def stalled_heads(factory, monkeypatch, count=4):
    worker = factory("pending-backoff")
    blocked = {f"stuck{i}" for i in range(count)}
    attempts = []
    now = [0.0]
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now[0]))
    with worker.active():
        engine.create_network("Backoff")
        with store.get_db() as conn:
            cur = conn.cursor()
            for index, origin in enumerate([f"stuck{i}" for i in range(count)] + ["good"]):
                cur.execute("INSERT INTO sync_watermarks (origin_device_id, incarnation, last_seq, updated_at) "
                            "VALUES (%s, 1, 5, %s)", (origin, f"2026-01-01T00:00:{index:02d}+00:00"))
                store.stash_pending(cur, {"origin_device_id": origin, "origin_incarnation": 1, "origin_seq": 6,
                                          "collection": "probe", "record_id": "head"})

    def apply(cursor, env, *, ctx, result):
        # This probe isolates scheduling from membership/signature validation.
        # Real envelopes and PostgreSQL error rollback are covered separately.
        origin = env["origin_device_id"]
        attempts.append(origin)
        assert engine.applying_batch()
        if origin in blocked:
            raise RuntimeError("blocked buffered head")
        store.set_watermark(cursor, origin, 1, env["origin_seq"])
        return "applied"

    monkeypatch.setattr(engine, "apply_envelope", apply)
    return worker, blocked, attempts, now


def warnings(caplog):
    return [row for row in caplog.records if row.name == engine.__name__ and row.exc_info]


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_four_failed_heads_cannot_starve_fifth_and_skipped_pass_is_not_applying(request, use_postgres, monkeypatch, caplog):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    worker, blocked, attempts, now = stalled_heads(factory, monkeypatch)
    caplog.set_level(logging.WARNING, logger=engine.__name__)
    apply = Mock(wraps=engine.apply_batch)
    monkeypatch.setattr(engine, "apply_batch", apply)
    with worker.active():
        first = engine.resume_pending()
        assert first.retry == 4 and first.applied == 0
        assert len(warnings(caplog)) == 4
        # Even when all four retries are due, an untried healthy head gets a
        # probe slot and is selected before them, rather than waiting forever.
        now[0] = 30
        second = engine.resume_pending()
        assert second.applied == 1 and attempts[4] == "good"
        assert len(warnings(caplog)) == 7  # three of the due heads also retried
        engine.resume_pending()  # the fourth due head
        count = len(attempts)
        entered = apply.call_count
        for _ in range(5):
            assert engine.resume_pending().retry == 0
        assert len(attempts) == count and apply.call_count == entered
        assert not engine.applying_batch()
        # A peer replay may retry explicitly, but does not defeat log backoff.
        engine.apply_batch((), _pending_streams=[("stuck0", 1)])
        assert len(warnings(caplog)) == 8
        with store.get_db() as conn:
            assert store.get_watermark(conn.cursor(), "good", 1) == 6


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_retry_doubles_caps_and_resets_on_progress(request, use_postgres, monkeypatch, caplog):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    worker, blocked, attempts, now = stalled_heads(factory, monkeypatch, count=1)
    caplog.set_level(logging.WARNING, logger=engine.__name__)
    with worker.active():
        ctx = engine.require_context()
        for count, delay in enumerate([30, 60, 120, 240, 480, 600, 600], start=1):
            result = engine.resume_pending()
            assert result.retry == 1
            retry = ctx._pending_retries[("stuck0", 1)]
            assert retry.delay == delay and retry.until == now[0] + delay
            entered = len(attempts)
            # Frequent replay attempts within the window retain the deadline,
            # delay and single traceback rather than logging each failed pass.
            engine.apply_batch((), _pending_streams=[("stuck0", 1)])
            assert len(warnings(caplog)) == count
            now[0] = retry.until - .01
            assert engine.resume_pending().retry == 0
            assert len(attempts) == entered + 1
            now[0] = retry.until
        blocked.clear()
        assert engine.resume_pending().applied == 1
        assert ("stuck0", 1) not in ctx._pending_retries
        blocked.add("stuck0")
        with store.get_db() as conn:
            store.stash_pending(conn.cursor(), {"origin_device_id": "stuck0", "origin_incarnation": 1,
                                                "origin_seq": 7, "collection": "probe", "record_id": "next"})
        assert engine.resume_pending().retry == 1
        assert ctx._pending_retries[("stuck0", 1)].delay == 30


def test_inactive_retry_entries_expire_without_new_apply(workers, monkeypatch):
    worker, blocked, attempts, now = stalled_heads(workers, monkeypatch, count=1)
    with worker.active():
        engine.resume_pending()
        ctx = engine.require_context()
        with store.get_db() as conn:
            store.clear_pending_stream(conn.cursor(), "stuck0", 1)
        now[0] = 30 + engine.PENDING_RETRY_MAX
        assert engine.resume_pending().applied == 0
        assert not ctx._pending_retries
