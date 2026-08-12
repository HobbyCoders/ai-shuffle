"""ExecutionEngine protocol — one interface over the native and delegated loops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from loom.events.model import Event
from loom.events.ops import Op


@dataclass(frozen=True)
class EngineCaps:
    code_mode: bool = False
    inproc_tools: bool = False
    schema_plan_mode: bool = False
    fork: bool = False
    rewind: bool = False
    subagents: bool = False
    cache_metrics: bool = False


class ExecutionEngine(Protocol):
    caps: EngineCaps

    async def submit(self, op: Op) -> None: ...
    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]: ...
    async def aclose(self) -> None: ...
