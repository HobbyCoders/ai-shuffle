"""A small concurrent-ingest ratchet, on the configured isolated test DB.

Real-PG CI/probes use the same test; the ordinary isolated SQLite job also
catches accidental event-loop stalls. No source append hook calls the actor.
"""
from __future__ import annotations

import asyncio
import math
import time

import pytest

from app.core.transcript_views.bridge import ReducerBridge
from app.core.transcript_views.producer import ProjectionActor, database
from app.db import session_events as source
from app.db import transcript_projections as store
from app.db.profiles import create_profile
from app.db.sessions import create_session


@pytest.mark.asyncio
async def test_background_projection_preserves_ingest_p95():
    store.initialize()
    create_profile('ingest-fixture', 'Fixture', None, {}, if_absent=True)
    sid = 'projection-ingest-fixture'
    create_session(sid, 'ingest-fixture', derive_provider_from_profile=False)
    seed = []
    for index in range(64):
        seed += [{'type': 'message.user', 'payload': {'content': f'Request {index}'}},
                 {'type': 'tool.use', 'payload': {'tool_use_id': f'tool-{index}', 'name': 'Read', 'input': {'path': 'fixture'}}},
                 {'type': 'tool.result', 'payload': {'tool_use_id': f'tool-{index}', 'content': 'fixture result ' * 160}},
                 {'type': 'turn.completed', 'payload': {}}]
    source.append_events(sid, seed)
    bridge = ReducerBridge()
    actor = ProjectionActor(bridge)
    stop = asyncio.Event()
    quanta = 0
    task = None
    async def ingest():
        values = []
        for _ in range(75):
            start = time.perf_counter()
            await database(source.append_events, sid, [{'type': 'assistant.text_delta', 'payload': {'text': 'small delta'}}])
            values.append(time.perf_counter() - start)
        return values
    async def background():
        nonlocal quanta
        for _ in range(2000):
            if stop.is_set():
                return
            await actor.process(sid)
            quanta += 1
            await asyncio.sleep(0)
    try:
        for _ in range(32):
            if not await actor.process(sid):
                break
        assert store.load(sid).ready
        baseline = await ingest()
        task = asyncio.create_task(background())
        concurrent = await ingest()
        stop.set()
        await task
        assert quanta > 0
        p95 = lambda values: sorted(values)[math.ceil(len(values) * .95) - 1]
        before, during = p95(baseline), p95(concurrent)
        # Tight absolute allowance catches a blocking 10-100ms fold while
        # accommodating the scheduler noise of free shared CI runners.
        limit = max(before * 1.35, before + .003)
        print(f'projection ingest p95: baseline={before * 1000:.3f}ms '
              f'concurrent={during * 1000:.3f}ms limit={limit * 1000:.3f}ms quanta={quanta}')
        assert during <= limit
    finally:
        stop.set()
        if task is not None:
            await task
        await bridge.close()
