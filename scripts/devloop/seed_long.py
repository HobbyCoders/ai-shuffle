"""Opt-in performance fixture: stable content, real contract events, no providers called."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from scripts.devloop.seed import DEV_AGENT_ID, Transcript, ensure_dev_agent

TURNS = 1480
SUBAGENT_TURNS = 410
PROGRESS_CHUNKS = 181
SESSIONS = {name: str(uuid.uuid5(uuid.NAMESPACE_URL, f"shuffle-perf-long/{name}"))
            for name in ("small", "large", "other")}


def rich_markdown(number: int) -> str:
    """Exercise nested markdown, syntax highlighting, tables and tool summaries.

    Deliberately repeated structure, not random filler. A window must cost like
    a real engineering chat even when tool groups are collapsed by default.
    """
    sections = []
    for section in range(5):
        sections.append(f"""### Check {number}.{section}: replay and rendering

The **request boundary** keeps `session_id`, `from_seq` and **ownership** separate.
A [local reference](#check-{section}) describes the measured path, not a live service.

- **Read path**: inspect `src/transcript/window.py` before changing the query.
  - Keep the durable cursor and the visible window independent.
  - Verify **cold**, **cached** and **remote** navigation separately.
- **Write path**: preserve ordering and report errors without dropping rows.
  - Include a regression test for `older_cursor` and the empty window.
  - Do not substitute a cache hit for a successful ownership check.

| Stage | Input | Expected result |
| --- | --- | --- |
| identity | `session_id` | **bounded** metadata |
| snapshot | `limit=120` | recent rows only |
| stream | `from_seq=42` | ordered durable tail |
| scroll | `before=120` | older window |
| return | cached rows | no replacement replay |

```python
def read_window(store, session_id: str, before: int = 120):
    rows = store.read(session_id, before=before, limit=120)
    result = []
    for index, row in enumerate(rows):
        if row["visible"] and index < 120:
            result.append({{"id": row["id"], "text": row["content"]}})
    assert len(result) <= 120, "window grew unexpectedly"
    return {{"rows": result, "cursor": before - len(result)}}
```

> Review note: **measure the producing path**, then compare statements, bytes,
> nested DOM nodes and long tasks. A passing serializer test is not enough.
""")
    return f"## Investigation {number}\n\n" + "\n".join(sections)


class LongTranscript(Transcript):
    def __init__(self, *args):
        super().__init__(*args)
        self._ids: dict[str, int] = {}

    def _id(self, kind: str) -> uuid.UUID:
        self._ids[kind] = self._ids.get(kind, 0) + 1
        return uuid.uuid5(uuid.UUID(self.session_id), f"{kind}:{self._ids[kind]}")

    def _event(self, cls: type, turn_id: str, **fields) -> None:
        super()._event(cls, turn_id, **fields)
        index = len(self.events)
        self.events[-1].update(
            id=str(uuid.uuid5(uuid.UUID(self.session_id), f"event:{index}")),
            created_at=(datetime(2026, 1, 1, tzinfo=timezone.utc)
                        + timedelta(milliseconds=index)).isoformat(),
        )


def build_transcript(name: str, turns: int) -> LongTranscript:
    """Build independently of the DB; tests can ratchet event/content counts."""
    from app.contract import events as ev

    transcript = LongTranscript(SESSIONS[name], DEV_AGENT_ID, "perf-model")
    for number in range(turns):
        result = rich_markdown(number)
        tools = [
            ("Read", {"file_path": f"src/window_{number}.py"}, result[:2800], False),
            ("Grep", {"pattern": "from_seq|cursor", "path": "src"},
             "\n".join(f"src/window.py:{i}: cursor = read_window(store, session_id)"
                        for i in range(1, 25)), False),
            ("run_python", {"code": "print(measure_request('recent-window'))"},
             json.dumps({"statements": 4, "bytes": 23814, "samples": list(range(32))}), False),
            ("Bash", {"command": "python -m pytest tests/test_window.py -q"},
             "\n".join(f"tests/test_window.py::test_case_{i} PASSED" for i in range(24)), False),
        ]
        transcript.turn(
            f"Investigate window {number}. Preserve ordering, error handling and mixed-worker "
            "compatibility. Report the read path, query counts and a focused regression test.",
            tools=tools, answer=result,
        )
        # Child activity belongs inside the turn, not after its terminal event.
        completed = transcript.events.pop()
        turn_id = completed["turn_id"]
        if name == "large":
            for call in range(8):
                transcript._event(ev.TurnUsage, turn_id, usage={
                    "input_tokens": 1800, "output_tokens": 240, "model_call_index": call,
                    "cost_usd": 0})
            if ((number + 1) * SUBAGENT_TURNS // turns
                    != number * SUBAGENT_TURNS // turns):
                task = f"perf-review-{number}"
                transcript._event(ev.SubagentStarted, turn_id, task_id=task,
                                  description="Review the bounded transcript read path")
                for chunk in range(PROGRESS_CHUNKS):
                    transcript._event(ev.SubagentProgress, turn_id, task_id=task, kind="chunk",
                                      payload={"text": f"Check {chunk}: " + result[:320]})
                transcript._event(ev.SubagentCompleted, turn_id, task_id=task,
                                  status="completed", result_preview=result[:1600])
        # Rebuild to retain unique deterministic ids after pop().
        transcript._event(ev.TurnCompleted, turn_id, stop_reason="end_turn",
                          usage=completed["payload"]["usage"])
    return transcript


async def seed_long() -> dict:
    from app.db import database
    from app.db.async_database import run_sync
    from app.db.model_providers import create_model_provider

    started = time.perf_counter()
    await run_sync(ensure_dev_agent)
    # A display-only provider. No credential and no model request during seeding.
    await run_sync(create_model_provider, "perf-synthetic", "Synthetic performance fixture",
                   "custom", "http://127.0.0.1:9/v1",
                   models=[{"id": "perf-model", "label": "Synthetic fixture"}],
                   default_model="perf-model", api_dialect="openai")
    sizes = {}
    for name, turns in (("small", 3), ("large", TURNS), ("other", 2)):
        await run_sync(database.create_session, session_id=SESSIONS[name],
                       profile_id=DEV_AGENT_ID, title=f"Performance {name} (seeded)",
                       is_active=True, model="prov:perf-synthetic:perf-model")
        transcript = await run_sync(build_transcript, name, turns)
        count = await run_sync(transcript.save)
        sizes[name] = {"id": SESSIONS[name], "user_turns": turns, "events": count,
                       "event_payload_bytes": sum(len(json.dumps(e["payload"]).encode())
                                                  for e in transcript.events)}
    return {"sessions": SESSIONS, "sizes": sizes,
            "seed_seconds": round(time.perf_counter() - started, 3)}
