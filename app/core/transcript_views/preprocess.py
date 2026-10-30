"""Crash-safe bounded carry for the existing cold SSE preprocessing pipeline.

The SSE helpers remain the authority. This module only persists their pending
runs, emitted wire frames and a raw mutable attempt for lookahead re-evaluation.
No clipping, alternate progress rules or Python message reducer lives here.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable

from app.core import transcript_replay as serving

MAX_RAW_ROWS = 8192
MAX_WIRE_FRAMES = 1024
MAX_STATE_BYTES = 8 * 1024 * 1024


class TailUnavailable(RuntimeError):
    pass


def replay_flags() -> dict[str, bool]:
    return {
        "progress": serving._perf_switch_on(serving._COALESCE_PROGRESS_ENV),
        "usage": serving._perf_switch_on(serving._SLIM_USAGE_ENV),
    }


def source_row(row: dict[str, Any]) -> dict[str, Any]:
    # Match _wire's timestamp boundary before raw rows become durable JSON.
    # UUID/Decimal handling then matches the serving frame's default=str.
    prepared = {**row, "created_at": serving._wire(row)["created_at"]}
    return json.loads(json.dumps(prepared, default=str))


def lookup_key(turn_id: Any, tool_use_id: Any) -> str | None:
    if not tool_use_id:
        return None
    return json.dumps([turn_id, str(tool_use_id)], separators=(",", ":"), default=str)


def input_lookup_keys(rows: Iterable[dict[str, Any]]) -> list[str]:
    result: set[str] = set()
    for row in rows:
        if row.get("event_type") == serving._TOOL_INPUT_DELTA:
            key = lookup_key(row.get("turn_id"), (row.get("payload") or {}).get("tool_use_id"))
            if key is not None:
                result.add(key)
    if len(result) > MAX_WIRE_FRAMES:
        raise TailUnavailable("tool_lookup_limit")
    return sorted(result)


def tool_use_map(lookups: dict[str, int]) -> dict[tuple[Any, str], int]:
    return {tuple(json.loads(key)): int(seq) for key, seq in lookups.items()}


class ReplayTail:
    def __init__(self) -> None:
        self.raw: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.delta: list[dict[str, Any] | None] = [None]
        self.progress: list[dict[str, Any] | None] = [None]
        self.flags = replay_flags()

    @classmethod
    def restore(cls, text: str) -> "ReplayTail":
        try:
            value = json.loads(text)
            if value["version"] != 1 or value["flags"] != replay_flags():
                raise TailUnavailable("preprocessing_changed")
            result = cls()
            result.raw, result.closed = value["raw"], value["closed"]
            result.delta, result.progress = value["delta"], value["progress"]
            if result.delta[0] is not None:
                result.delta[0]["key"] = tuple(result.delta[0]["key"])
            result._check()
            return result
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise TailUnavailable("invalid_preprocessing_carry") from error

    def _rows(self, rows, pending=None):
        return serving._coalesce_progress(rows, pending or self.progress) if self.flags["progress"] else rows

    def _wire(self, rows):
        return [json.loads(json.dumps(serving._wire(row), default=str)) for row in rows]

    def append(self, rows: Iterable[dict[str, Any]], lookups: dict[str, int]) -> None:
        if self.flags != replay_flags():
            raise TailUnavailable("preprocessing_changed")
        prepared = [source_row(row) for row in rows]
        previous = int(self.raw[-1]["seq"]) if self.raw else 0
        for row in prepared:
            if int(row["seq"]) <= previous:
                raise TailUnavailable("non_monotonic_source")
            previous = int(row["seq"])
        self.raw.extend(prepared)
        self.closed.extend(self._wire(self._rows(serving._coalesce_deltas(prepared, self.delta, tool_use_map(lookups)))))
        self._check()

    def reevaluate(self, lookups: dict[str, int]) -> None:
        # A later full tool.use can change earlier argument-delta elision.
        # Re-evaluate only the capped raw attempt, never committed history.
        raw = self.raw
        self.raw, self.closed, self.delta, self.progress = [], [], [None], [None]
        self.append(raw, lookups)

    def frames(self) -> list[dict[str, Any]]:
        delta, progress = deepcopy(self.delta), deepcopy(self.progress)
        result = list(self.closed)
        result.extend(self._wire(self._rows(serving._flush_pending(delta), progress)))
        result.extend(self._wire(serving._flush_pending_progress(progress)))
        if len(result) > MAX_WIRE_FRAMES:
            raise TailUnavailable("mutable_frame_limit")
        return result

    def acknowledge(self, seq: int) -> None:
        self.raw = [row for row in self.raw if int(row["seq"]) > seq]
        self.closed = [row for row in self.closed if int(row["seq"]) > seq]
        # Only real non-delta attempt boundaries may become stable. Such a
        # boundary necessarily flushed both stages before it was emitted.
        for pending in (self.delta, self.progress):
            if pending[0] is not None and int(pending[0]["first"]["seq"]) <= seq:
                raise TailUnavailable("unsafe_preprocessing_boundary")
        self._check()

    def dump(self) -> str:
        text = json.dumps({"version": 1, "flags": self.flags, "raw": self.raw,
                           "closed": self.closed, "delta": self.delta, "progress": self.progress},
                          separators=(",", ":"), ensure_ascii=True)
        if len(text.encode("utf-8")) > MAX_STATE_BYTES:
            raise TailUnavailable("preprocessing_size_limit")
        return text

    def _check(self) -> None:
        if len(self.raw) > MAX_RAW_ROWS or len(self.closed) > MAX_WIRE_FRAMES:
            raise TailUnavailable("mutable_tail_limit")
        self.dump()


def _prepare_frames(tail: ReplayTail, rows, lookup):
    tail.reevaluate(lookup)
    if rows:
        tail.append(rows, lookup)
    # Preparation owns a separate process; explicitly return the modified tail
    # rather than relying on thread-shared mutations before checkpointing it.
    return tail, tail.frames()


def _checkpoint_tail(tail: ReplayTail, seq: int | None, lookup):
    emitted = []
    if seq is not None:
        # Only unsuperseded input deltas actually committed at this boundary
        # may later need a rebuild when a full tool.use arrives.
        for row in tail.raw:
            if row['event_type'] == serving._TOOL_INPUT_DELTA and int(row['seq']) <= seq:
                key = lookup_key(row.get('turn_id'), (row.get('payload') or {}).get('tool_use_id'))
                if key is not None and lookup.get(key, 0) <= int(row['seq']):
                    emitted.append((key, int(row['seq'])))
        tail.acknowledge(seq)
    return emitted, tail.dump()
