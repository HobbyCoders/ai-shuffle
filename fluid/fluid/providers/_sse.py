"""Minimal SSE line reader shared by provider clients."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


def _decode(data: str) -> dict[str, Any] | None:
    """One frame's payload, or ``None`` when it is not usable.

    A connection cut mid-frame leaves a JSON prefix in the buffer, and the
    trailing flush below would then raise ``JSONDecodeError`` out of the stream
    consumer — losing an entire turn's work over a partial final frame. A frame
    that does not parse is a frame the caller could not have used anyway, so it
    is dropped and the stream ends on its own terms.
    """
    if not data or data == "[DONE]":
        return None
    try:
        decoded = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


#: Event name ``sse_events(..., keepalives=True)`` yields for a comment line.
#: No provider can send it: an SSE event name cannot start with ``:``.
KEEPALIVE_EVENT = ":keepalive"


async def sse_events(
    lines: AsyncIterator[str], *, keepalives: bool = False
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Parse an SSE byte-line stream into (event_name, json_data) pairs.

    Handles multi-line ``data:`` fields and ignores frames that do not carry a
    decodable JSON object. Comment lines (heartbeats) are dropped unless
    ``keepalives`` is set; then each one yields ``(KEEPALIVE_EVENT, {})``, so a
    caller can tell a quiet but open stream from a dead one. A comment does not
    disturb a frame being assembled around it.
    """
    event_name = ""
    data_parts: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if line == "":
            payload = _decode("\n".join(data_parts)) if data_parts else None
            if payload is not None:
                yield event_name or "message", payload
            event_name = ""
            data_parts = []
        elif line.startswith(":"):
            if keepalives:
                yield KEEPALIVE_EVENT, {}
            continue
        elif line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_parts.append(line[len("data:"):].strip())
    if data_parts:
        payload = _decode("\n".join(data_parts))
        if payload is not None:
            yield event_name or "message", payload
