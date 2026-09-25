"""Opt-in request tracing for every native provider.

Set ``FLUID_TRACE_REQUESTS=<dir>`` and each outbound model request body is
written to ``<dir>/<utc-timestamp>-<n>-<provider>.json`` before it is sent. The files are
the exact JSON the provider receives, so prefix drift between two consecutive
requests (the usual cause of a missed prompt cache) can be diagnosed with a
plain diff. Nothing is written when the variable is unset.
"""

from __future__ import annotations

import itertools
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_counter = itertools.count(1)


def trace_request(provider: str, body: dict[str, Any]) -> None:
    target = os.environ.get("FLUID_TRACE_REQUESTS")
    if not target:
        return
    try:
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")
        path = directory / f"{stamp}-{next(_counter):04d}-{provider}.json"
        path.write_text(
            json.dumps(body, indent=1, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        # Tracing must never break a model call.
        return
