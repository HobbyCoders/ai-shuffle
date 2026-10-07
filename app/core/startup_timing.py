"""Local monotonic agent-startup spans; never a cross-worker delivery clock.

One record per reached stage, correlated by session/turn, without prompt content.
An absent stage is not success (refusal, disconnect and failure can end early).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class TurnStartupTiming:
    def __init__(self, session_id: str, turn_id: str | None = None,
                 *, clock: Callable[[], float] | None = None):
        # Python 3.12 on Windows uses a 15.6 ms GetTickCount64 clock for
        # monotonic(); perf_counter stays monotonic and uses high-resolution QPC.
        self._clock = clock or time.perf_counter
        self._start = self._previous = self._clock()
        self._session_id = session_id
        self._turn_id = turn_id

    def mark(self, stage: str) -> None:
        now = self._clock()
        logger.info(
            "perf.agent_startup session=%s turn=%s stage=%s elapsed_ms=%.3f stage_ms=%.3f",
            self._session_id, self._turn_id, stage,
            (now - self._start) * 1000, (now - self._previous) * 1000,
        )
        self._previous = now
