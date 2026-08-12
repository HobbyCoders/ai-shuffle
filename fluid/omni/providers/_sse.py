"""Minimal SSE line reader shared by provider clients."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Parse an SSE byte-line stream into (event_name, json_data) pairs.

    Handles multi-line ``data:`` fields and ignores comments/heartbeats.
    """
    event_name = ""
    data_parts: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if line == "":
            if data_parts:
                data = "\n".join(data_parts)
                if data != "[DONE]":
                    yield event_name or "message", json.loads(data)
            event_name = ""
            data_parts = []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_parts.append(line[len("data:"):].strip())
    if data_parts:
        data = "\n".join(data_parts)
        if data != "[DONE]":
            yield event_name or "message", json.loads(data)
