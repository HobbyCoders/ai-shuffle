"""The replay uses the producing route/usage metadata, including explicit null TTLs."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compaction_replay.py"
spec = importlib.util.spec_from_file_location("compaction_replay_cache_ttl", SCRIPT)
replay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay
spec.loader.exec_module(replay)


@pytest.mark.parametrize("provider,model,raw,ttl", [
    (None, None, {}, 300),  # pre-route exports retain the documented fallback
    ("anthropic", "claude-sonnet-5", {}, 300),
    ("openai_responses", "gpt-5.6", {}, 1800),
    ("openai_responses", "gpt-5.6", {"endpoint": "https://chatgpt.com/backend-api/codex/responses"}, None),
    ("openai_responses", "grok", {"endpoint": "https://api.x.ai/v1/responses"}, 300),
    ("openrouter", "openai/gpt-5.6", {}, 600),
    ("openai_responses", "gpt-5.6", {"cache_ttl_seconds": None}, None),
    ("openai_responses", "gpt-5.6", {"cache_ttl_seconds": 300}, 300),
    (None, None, {"compaction_decision": {"cache_signal": {"ttl_seconds": None}}}, None),
    (None, None, {"provider": "openai_responses", "provider_type": "chatgpt"}, None),
])
def test_load_and_replay_supply_route_ttl(tmp_path, monkeypatch, provider, model, raw, ttl):
    events = []
    if provider:
        events.append({"_cls": "TurnStarted", "provider": provider, "model": model})
    for at in (100, 1100):
        events.append({"_cls": "Usage", "ts": at, "input_tokens": 100,
                       "cache_read_tokens": 900, "raw": raw})
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps({"seq": i, "event": event})
                              for i, event in enumerate(events)), encoding="utf-8")
    session = replay.load(path)
    assert [call.ttl_seconds for call in session.calls] == [ttl, ttl]
    signals = []
    apply = replay.Compactor.apply

    def capture(compactor, nodes, **kwargs):
        signals.append(kwargs["cache"])
        return apply(compactor, nodes, **kwargs)

    monkeypatch.setattr(replay.Compactor, "apply", capture)
    replay.replay(session, "off", window=272_000, reserve=32_000, scale=1.0)
    assert [signal.ttl_seconds for signal in signals] == [ttl, ttl]
    assert signals[1].gap_s == 1000
    assert signals[1].cold_now is (ttl is not None and ttl < 1000)
