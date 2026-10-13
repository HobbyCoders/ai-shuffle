"""Helpers for the SSE event-log tail tests (``tests/api/test_events_sse*.py``).

A plain module: import what you need explicitly. The ``gw`` fixture lives in
tests/api/conftest.py. The tests consume the ``event_stream`` generator
directly, because ASGI test transports buffer the never-ending SSE body.
"""
import asyncio
import json


def parse_frame(frame: str) -> dict:
    """Split one SSE frame into its ``id``, ``event`` and JSON ``data``."""
    parsed = {}
    for line in frame.splitlines():
        if line.startswith("id: "):
            parsed["id"] = int(line[4:])
        elif line.startswith("event: "):
            parsed["event"] = line[len("event: "):]
        elif line.startswith("data: "):
            parsed["data"] = json.loads(line[len("data: "):])
    return parsed


async def collect_frames(agen, n, timeout=5.0):
    """Read ``n`` event frames (skipping keepalive comments and the ``caught_up``
    history/live boundary sentinel) from the stream."""
    frames = []

    async def _pump():
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            parsed = parse_frame(chunk)
            if parsed.get("event") == "caught_up":
                continue
            frames.append(parsed)
            if len(frames) >= n:
                return

    await asyncio.wait_for(_pump(), timeout=timeout)
    return frames
