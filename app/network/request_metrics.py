"""Bounded, privacy-safe server timings for peer sync routes and controls.

At most one INFO summary per minute while requests arrive; no periodic work
at idle. Quantiles are exact up to 1,024 calls per endpoint/window, then use
uniform reservoir samples (sample_count is reported). Never pass raw URLs,
identifiers, bodies or exception messages as endpoint labels.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import math
import random
import threading
import time

logger = logging.getLogger(__name__)
WINDOW_SECONDS = 60.0
MAX_ENDPOINTS = 64
MAX_SAMPLES = 1024


@dataclass
class _Bucket:
    count: int = 0
    errors: int = 0
    samples: list[float] = field(default_factory=list)


class RequestMetrics:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._random = random.Random()
        self._started = clock()
        self._timer = None
        self._loop = None

    def record(self, transport: str, endpoint: str, status: int, seconds: float) -> None:
        milliseconds = max(0.0, seconds * 1000)
        logger.debug("Peer request transport=%s endpoint=%s status=%d elapsed_ms=%.3f",
                     transport, endpoint, status, milliseconds)
        loop = asyncio.get_running_loop()
        with self._lock:
            key = (transport, endpoint)
            # Reserve the last slot for all overflow, even if the caller's
            # normalization regresses or a plugin adds unbounded actions.
            if key not in self._buckets and len(self._buckets) >= MAX_ENDPOINTS - 1:
                key = ("other", "other")
            bucket = self._buckets.setdefault(key, _Bucket())
            bucket.count += 1
            bucket.errors += int(status >= 400)
            if len(bucket.samples) < MAX_SAMPLES:
                bucket.samples.append(milliseconds)
            else:
                index = self._random.randrange(bucket.count)
                if index < MAX_SAMPLES:
                    bucket.samples[index] = milliseconds
            if self._timer is None or self._loop.is_closed():
                if self._timer is not None:
                    self._timer.cancel()
                self._loop = loop
                self._started = self._clock()
                self._timer = loop.call_later(WINDOW_SECONDS, self.flush)

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = None
            buckets, self._buckets = self._buckets, {}
            elapsed = self._clock() - self._started
        if not buckets:
            return
        summary = []
        for (transport, endpoint), bucket in sorted(buckets.items()):
            ordered = sorted(bucket.samples)
            summary.append({
                "transport": transport, "endpoint": endpoint,
                "count": bucket.count, "errors": bucket.errors,
                "sample_count": len(ordered),
                "p50_ms": round(ordered[math.ceil(len(ordered) * .50) - 1], 3),
                "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
            })
        logger.info("Peer request summary window_s=%.3f endpoints=%s", elapsed,
                    json.dumps(summary, separators=(",", ":")))


metrics = RequestMetrics()
