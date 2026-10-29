"""Count-based guardrails for the performance seed (no database or model calls)."""
from collections import Counter

from scripts.devloop import seed
from scripts.devloop import seed_long as fixture


def test_long_is_opt_in():
    assert set(seed.SEEDS) == {"default", "long"}
    assert seed.LONG_TURNS == 70  # the normal dev fixture must stay small


def test_long_fixture_event_and_density_ratchet():
    transcript = fixture.build_transcript("large", fixture.TURNS)
    counts = Counter(event["type"] for event in transcript.events)
    assert counts["message.user"] == 1480
    assert len(transcript.events) == 104630
    assert counts["subagent.started"] == counts["subagent.completed"] == 410
    assert counts["subagent.progress"] == 410 * 181
    assert len({event["id"] for event in transcript.events}) == len(transcript.events)
    # Every part of the history, including the last rendered window, is rich.
    for event in [e for e in transcript.events if e["type"] == "message.assistant"]:
        content = event["payload"]["content"]
        assert content.count("```python") == 8
        assert content.count("| snapshot |") == 8
        assert content.count("  - ") == 32
        assert len(content) >= 10000


def test_small_fixture_is_byte_for_byte_deterministic():
    first = fixture.build_transcript("small", 3).events
    seed.seed_id("turn")  # unrelated previous seeding must not affect ids
    assert fixture.build_transcript("small", 3).events == first
    assert first[-1]["type"] == "turn.completed"
