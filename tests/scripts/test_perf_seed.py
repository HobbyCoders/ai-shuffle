"""Count-based guardrails for the performance seed (no database or model calls)."""


def test_import_profile_parser_and_nearest_rank():
    from scripts.perf.profile_imports import parse_imports, summarize

    rows = parse_imports("noise\nimport time:       123 |        456 |   app.main\n"
                         "import time: self [us] | cumulative | imported package")
    assert rows == [{"self_us": 123, "cumulative_us": 456, "module": "app.main", "depth": 3}]
    assert summarize([4, 1, 3, 2]) == {"n": 4, "p75": 3, "min": 1, "max": 4}
    assert summarize([])["p75"] is None

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
        assert content.count("```python") == 5
        assert content.count("| snapshot |") == 5
        assert content.count("  - ") == 20
        assert len(content) >= 6500


def test_small_fixture_is_byte_for_byte_deterministic():
    first = fixture.build_transcript("small", 3).events
    seed.seed_id("turn")  # unrelated previous seeding must not affect ids
    assert fixture.build_transcript("small", 3).events == first
    assert first[-1]["type"] == "turn.completed"
    other = fixture.build_transcript("other", 2).events
    assert {e["turn_id"] for e in first}.isdisjoint(e["turn_id"] for e in other)
