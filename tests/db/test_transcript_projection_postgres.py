"""Native-PG access-path and finite large-generation cleanup ratchets."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import time
from uuid import uuid4

import pytest

from app.db import transcript_projections as store
from app.db import transcript_projection_work as work
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def plans(node):
    yield node
    for child in node.get('Plans', []):
        yield from plans(child)


def test_large_generation_alias_upserts_and_cleanup_use_child_key_indexes(monkeypatch):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    monkeypatch.setattr(work, 'GARBAGE_ROWS', 64)
    store.initialize()
    sid, generation = f'projection-pg-{uuid4()}', 'large-generation'
    count, aliases = 10_000, 4
    body = 'opaque-test-record'
    digest = hashlib.sha256(body.encode()).hexdigest()
    state = store.ProjectionState(sid, generation, 'test-pipeline', 'test-epoch', count, 'tail',
                                  ready=1, stage='ready', preview_json='opaque-checkpoint')
    store.commit(state, None)
    with store.get_db() as connection:
        cursor = connection.cursor()
        # Every derived FK needs a LEADING child-key index, not merely a PK
        # containing its columns on the other side of a different key.
        cursor.execute(
            "SELECT c.conrelid,c.conkey,child.relname AS child FROM pg_constraint c "
            "JOIN pg_class child ON child.oid=c.conrelid "
            "WHERE c.contype='f' AND child.relname LIKE 'session_transcript_projection_%'"
        )
        foreign_keys = cursor.fetchall()
        assert foreign_keys
        for foreign_key in foreign_keys:
            cursor.execute('SELECT indkey::text AS keys FROM pg_index WHERE indrelid=%s AND indisvalid AND indisready',
                           (foreign_key['conrelid'],))
            keys = list(foreign_key['conkey'])
            assert any([int(value) for value in row['keys'].split()][:len(keys)] == keys
                       for row in cursor.fetchall()), foreign_key['child']
        cursor.execute(
            "INSERT INTO session_transcript_projection_records "
            "(session_id,generation,id,order_key,origin_seq,folded_through,body,body_sha,body_bytes) "
            "SELECT %s,%s,'row-'||n::text,n,n,n,%s,%s,%s FROM generate_series(1,%s) g(n)",
            (sid, generation, body, digest, len(body.encode()), count),
        )
        cursor.execute('ANALYZE session_transcript_projection_records')
        cursor.execute(
            "INSERT INTO session_transcript_projection_aliases (session_id,generation,alias,record_id) "
            "SELECT %s,%s,'alias-'||n::text||'-'||slot::text,'row-'||n::text "
            "FROM generate_series(1,%s) g(n) CROSS JOIN generate_series(1,%s) a(slot)",
            (sid, generation, count, aliases),
        )
        cursor.execute('ANALYZE session_transcript_projection_aliases')
        cursor.execute(
            "EXPLAIN (FORMAT JSON) DELETE FROM session_transcript_projection_aliases "
            "WHERE session_id=%s AND generation=%s AND record_id=%s",
            (sid, generation, f'row-{count}'),
        )
        plan = cursor.fetchone()['QUERY PLAN'][0]['Plan']
        assert any('record_id' in node.get('Index Cond', '') for node in plans(plan)), plan
    try:
        rows = [{'id': f'row-{index}', 'order': index, 'origin': index, 'foldedThrough': count,
                 'aliases': [f'changed-alias-{index}'], 'body': body} for index in range(1, 33)]
        current = replace(state)
        started = time.perf_counter()
        store.commit(current, state, upserts=rows)
        upsert_seconds = time.perf_counter() - started
        work.retire_missing(current)
        started = time.perf_counter()
        assert work.collect_garbage()
        cleanup_seconds = time.perf_counter() - started
        with store.get_db() as connection:
            cursor = connection.cursor()
            cursor.execute('SELECT COUNT(*) AS n FROM session_transcript_projection_records WHERE session_id=%s', (sid,))
            assert cursor.fetchone()['n'] == count - work.GARBAGE_ROWS
            cursor.execute('SELECT COUNT(*) AS n FROM session_transcript_projection_aliases WHERE session_id=%s', (sid,))
            assert cursor.fetchone()['n'] == (count - work.GARBAGE_ROWS) * aliases
        print(f'projection native PG: records={count} aliases={count * aliases} '
              f'upsert32={upsert_seconds * 1000:.3f}ms cleanup64={cleanup_seconds * 1000:.3f}ms')
        # Free standard runners only. The plan assertion makes the regression
        # deterministic; these budgets also catch trigger/round-trip explosions.
        assert upsert_seconds < 1.0
        assert cleanup_seconds < .5
    finally:
        with store.get_db() as connection:
            cursor = connection.cursor()
            # Only this test's own disposable rows; exercises the FK cascade too.
            cursor.execute('DELETE FROM session_transcript_projection_records WHERE session_id=%s', (sid,))
            cursor.execute('DELETE FROM session_transcript_projection_retired WHERE session_id=%s', (sid,))
            cursor.execute('DELETE FROM session_transcript_projection_state WHERE session_id=%s', (sid,))
