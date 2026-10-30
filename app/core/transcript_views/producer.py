"""Dark, bounded materialization of the real server replay through the TS reducer.

There is deliberately no HTTP read adapter, SSE capability or frontend seed.
Every failure leaves the existing full-replay serving path authoritative.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from typing import Any, Callable
from uuid import uuid4

from app.api import events as serving
from app.db import session_events as source
from app.db import transcript_projection_work as work
from app.db import transcript_projections as store
from .bridge import ReducerBridge, ReducerUnavailable, verified_manifest
from .preprocess import ReplayTail, TailUnavailable, input_lookup_keys, lookup_key, replay_flags


NODE_QUANTUM = 128


class SourceChanged(RuntimeError):
    pass


async def database(call: Callable, *args, **kwargs):
    # A cancelled asyncio.to_thread does not stop its DB transaction. Join our
    # one owned operation before releasing the actor/test DB or shutting down.
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class ProjectionActor:
    def __init__(self, bridge: ReducerBridge, *, fingerprint: str | None = None):
        self.bridge = bridge
        self.fingerprint = fingerprint or verified_manifest()['fingerprint']

    def pipeline(self) -> str:
        value = json.dumps([self.fingerprint, replay_flags()], sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(value.encode()).hexdigest()

    async def _reset(self, previous, watermark) -> bool:
        state = store.ProjectionState(
            session_id=previous.session_id if previous else watermark['session_id'],
            generation=str(uuid4()), pipeline=self.pipeline(), epoch=watermark['epoch'],
            target_seq=watermark['seq'], target_id=watermark['tail_id'],
            preprocess_json=ReplayTail().dump(),
        )
        await database(store.commit, state, previous)
        return True

    async def _witness(self, state, *, cursor: int = 0, identity: str | None = None):
        watermark = await database(source.get_watermark, state.session_id, at_seq=state.target_seq)
        if (not watermark or watermark['epoch'] != state.epoch or
                watermark['seq'] < state.target_seq or watermark['tail_id'] != state.target_id):
            raise SourceChanged()
        if cursor and cursor != state.target_seq:
            seen = await database(source.get_watermark, state.session_id, at_seq=cursor)
            if not seen or seen['epoch'] != state.epoch or seen['tail_id'] != identity:
                raise SourceChanged()
        return watermark

    async def process(self, session_id: str, *, recheck: bool = False) -> bool:
        """One finite quantum; True requests another fair, bounded queue turn."""
        previous = await database(store.load, session_id)
        watermark = await database(source.get_watermark, session_id)
        if watermark is None:
            if previous is not None:
                await database(work.retire_missing, previous)
            return False
        watermark['session_id'] = session_id
        if (previous is None or previous.pipeline != self.pipeline() or
                previous.epoch != watermark['epoch'] or recheck):
            return await self._reset(previous, watermark)
        state = replace(previous)
        try:
            await self._witness(state)
        except SourceChanged:
            return await self._reset(previous, watermark)
        if state.stage == 'unsupported':
            return False  # Do not repeatedly replay a permanently over-budget log.
        try:
            # This legacy serving mode injects an elided-history frame and has a
            # different cold boundary. Do not silently approximate it.
            first_kept, _, _ = await database(serving._compacted_replay_start, session_id)
            if first_kept:
                raise TailUnavailable('compacted_source')
            if state.cleanup_json != '[]':
                await database(work.cleanup, state, previous)
                return True
            if watermark['seq'] > state.target_seq:
                state.target_seq, state.target_id = watermark['seq'], watermark['tail_id']
                state.stage, state.ready = 'index', 0
                await database(store.commit, state, previous)
                return True
            if state.stage == 'index':
                return await self._index(state, previous)
            if state.ready:
                return False
            return await self._fold(state, previous)
        except SourceChanged:
            latest = await database(source.get_watermark, session_id)
            if latest is None:
                await database(work.retire_missing, previous)
                return False
            latest['session_id'] = session_id
            return await self._reset(previous, latest)
        except (TailUnavailable, store.ProjectionStoreUnavailable, ReducerUnavailable) as error:
            if isinstance(error, ReducerUnavailable) and error.code != 'fold_rejected':
                raise  # Runtime failures are retried by the supervisor, not sticky.
            # Diagnostic codes are fixed, never transcript/DB/Node exception text.
            state = replace(previous, ready=0, preview_json=None, stage='unsupported',
                            error_code='fold_rejected' if isinstance(error, ReducerUnavailable) else 'projection_limit')
            await database(store.commit, state, previous)
            return False

    async def _index(self, state, previous) -> bool:
        rows = await database(work.source_index, state.session_id, state.index_seq, state.target_seq)
        additions = []
        for row in rows:
            key = lookup_key(row.get('turn_id'), row.get('tool_use_id'))
            if key is not None:
                additions.append((key, int(row['seq'])))
        if await database(store.changes_committed_input, state, additions):
            watermark = await self._witness(state)
            watermark['session_id'] = state.session_id
            return await self._reset(previous, watermark)
        if rows:
            state.index_seq, state.index_id = int(rows[-1]['seq']), str(rows[-1]['id'])
        if not rows or len(rows) < work.SCAN_ROWS or state.index_seq == state.target_seq:
            if state.index_seq < state.target_seq:
                raise SourceChanged()
            state.stage = 'fold'
        await self._witness(state, cursor=state.index_seq, identity=state.index_id)
        state.ready = 0
        await database(store.commit, state, previous, tool_uses=additions)
        return True

    async def _evaluate(self, state, tail):
        frames = tail.frames()
        temporary = state.base_json
        boundary = None
        loaded: set[str] = set()
        # The durable mutable tail is itself capped. Each IPC packet and each
        # referenced-record lookup is additionally bounded, even within it.
        for offset in range(0, max(1, len(frames)), NODE_QUANTUM):
            chunk = frames[offset:offset + NODE_QUANTUM]
            keys = await self.bridge.request('plan', carry=temporary, frames=chunk)
            records = await database(store.referenced_records, state, keys)
            loaded.update(row['id'] for row in records)
            result = await self.bridge.request('fold', carry=temporary, frames=chunk, records=records)
            temporary = result['preview']['carry']
            if result['boundary'] is not None:
                boundary = result['boundary']
                sealed = {row['id'] for row in boundary['upserts']}
                # Capture ONLY records loaded at/before this boundary. A target
                # hydrated in a later chunk must remain in the stable DB so the
                # next replay of the raw tail can still resolve that target.
                boundary['removed'] = sorted(loaded - sealed)
        return temporary, boundary

    async def _fold(self, state, previous) -> bool:
        await self._witness(state, cursor=state.scan_seq, identity=state.scan_id)
        tail = ReplayTail.restore(state.preprocess_json)
        lookup = await database(store.lookups, state, input_lookup_keys(tail.raw))
        tail.reevaluate(lookup)
        rows = await database(work.source_rows, state.session_id, state.scan_seq, state.target_seq)
        if rows:
            keys = input_lookup_keys(rows)
            lookup.update(await database(store.lookups, state, keys))
            tail.append(rows, lookup)
            state.scan_seq, state.scan_id = int(rows[-1]['seq']), str(rows[-1]['id'])
        elif state.scan_seq < state.target_seq:
            raise SourceChanged()
        preview, boundary = await self._evaluate(state, tail)
        emitted = []
        upserts, removed = [], []
        if boundary is not None:
            seq = int(boundary['lastSeq'])
            # Only unsuperseded argument deltas actually committed in the stable
            # fold can later require a rebuild when a new full tool.use arrives.
            for row in tail.raw:
                if row['event_type'] == serving._TOOL_INPUT_DELTA and int(row['seq']) <= seq:
                    key = lookup_key(row.get('turn_id'), (row.get('payload') or {}).get('tool_use_id'))
                    if key is not None and lookup.get(key, 0) <= int(row['seq']):
                        emitted.append((key, int(row['seq'])))
            state.base_seq, state.base_json = seq, boundary['carry']
            upserts, removed = boundary['upserts'], boundary['removed']
            invalidations = [item for item in boundary['invalidations']
                             if item['foldedBefore'] > state.cleanup_seq]
            if invalidations:
                state.cleanup_json = json.dumps(invalidations, separators=(',', ':'))
                state.stage = 'cleanup'
            tail.acknowledge(seq)
        state.preprocess_json, state.preview_json = tail.dump(), preview
        witness = await self._witness(state, cursor=state.scan_seq, identity=state.scan_id)
        state.ready = int(state.scan_seq == state.target_seq and witness['seq'] == state.target_seq
                          and state.cleanup_json == '[]')
        state.error_code = None
        await database(store.commit, state, previous, emitted_inputs=emitted, upserts=upserts, removed=removed)
        return not bool(state.ready)
