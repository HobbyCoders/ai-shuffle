"""Owner-local clock, immutable history times and the internal delegated-wake path."""
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfoNotFoundError

import pytest

from fluid.context import time_awareness as clock
from fluid.context.compaction import CompactionConfig, render_span_digest
from fluid.context.environment import environment_block, environment_note
from fluid.context.recall import expand_ref, render_hits, search_path
from fluid.context.steer_render import CURRENT_RENDER_VERSION
from fluid.messages import Message, Text
from fluid.store.base import RecallMessage, SessionNode
from fluid.tools.builtins.session_view import _render
from fluid.tools.time_context import delegated_wake_time


def dt(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def node(text="GamingPC tomorrow", ts="2026-11-08T23:00:00+00:00"):
    return SessionNode("node_0123456789ab", None, Message(role="user", content=[Text(text)]), ts=dt(ts).timestamp())


def test_clock_est_and_dst_fold():
    assert clock.turn_time_lines(zone="America/New_York", now=dt("2026-11-09T17:48:00+00:00")) == [
        "Current time: Mon Nov 9, 2026, 12:48 PM EST (America/New_York)"]
    before = clock.absolute_time(dt("2026-11-01T05:30:00+00:00"), "America/New_York")
    after = clock.absolute_time(dt("2026-11-01T06:30:00+00:00"), "America/New_York")
    assert "01:30:00 EDT -0400" in before
    assert "01:30:00 EST -0500" in after


@pytest.mark.parametrize("zone", [None, "local", "", "Mars/NotAZone", "../bad"])
def test_fallback_is_labelled(zone):
    line = clock.turn_time_lines(zone=zone, now=dt("2026-11-09T01:00:00+00:00"))[0]
    assert "1:00 AM UTC" in line and "fallback" in line


def test_missing_zone_database_never_fails(monkeypatch):
    def missing(_):
        raise ZoneInfoNotFoundError("no tzdata")
    monkeypatch.setattr(clock, "ZoneInfo", missing)
    assert "fallback" in clock.turn_time_lines(zone="America/New_York")[0]


@pytest.mark.parametrize("now,last,gap", [
    ("2026-11-09T18:00:00+00:00", "2026-11-09T17:00:01+00:00", False),
    ("2026-11-09T18:00:00+00:00", "2026-11-09T17:00:00+00:00", True),
    ("2026-11-09T05:05:00+00:00", "2026-11-09T04:55:00+00:00", True),
    ("2026-11-09T18:00:00+00:00", "2026-11-09T19:00:00+00:00", False),
])
def test_gap_threshold_and_local_date(now, last, gap):
    lines = clock.turn_time_lines(zone="America/New_York", now=dt(now), last_owner_at=dt(last))
    assert (len(lines) == 2) is gap
    if last.endswith("04:55:00+00:00"):
        assert "Sun Nov 8, 2026, 11:55 PM EST (10 minutes ago)" in lines[1]


def test_rule_is_fixed_once_and_clock_is_only_in_new_tail(tmp_path):
    fixed = environment_block(tmp_path, include_volatile=False)
    assert fixed.count(clock.RELATIVE_TIME_RULE) == 1
    assert "Current time:" not in fixed
    one = environment_note(tmp_path, time_context={"zone": "America/New_York", "now": dt("2026-11-09T17:00:00+00:00")})
    two = environment_note(tmp_path, time_context={"zone": "America/New_York", "now": dt("2026-11-09T19:00:00+00:00")})
    assert "12:00 PM EST" in one and "2:00 PM EST" in two
    assert environment_block(tmp_path, include_volatile=False) == fixed
    assert clock.RELATIVE_TIME_RULE not in one


def test_internal_delegated_wake_gets_time_and_gap_without_adapter():
    values = {"zone": "America/New_York", "now": dt("2026-11-09T17:00:00+00:00"),
              "last_owner_at": dt("2026-11-09T02:14:00+00:00")}
    sources = []
    def context(source):
        sources.append(source)
        return values
    blocks = [Text("background job completed")]
    rendered = delegated_wake_time(blocks, {"time_context": context}, "unpark")
    assert len(blocks) == 1
    assert "Current time:" in rendered[-1].text and "14 hours ago" in rendered[-1].text
    assert sources == ["unpark"]
    # A host note is already current; do not add a second one.
    host = [Text("Environment facts that change while a session runs\nCurrent time: now")]
    assert delegated_wake_time(host, {"time_context": context}, "user") == host
    assert delegated_wake_time([Text("/compact")], {"time_context": context}, "user") == [Text("/compact")]


def test_failed_host_read_falls_back_on_internal_wake():
    def unavailable(_):
        raise RuntimeError("unavailable")
    text = delegated_wake_time([], {"time_context": unavailable}, "unpark")[-1].text
    assert "UTC; fallback" in text


def test_digest_v4_dates_and_old_versions_stay_byte_stable():
    n = node()
    new = render_span_digest([n], CompactionConfig(render_version=CURRENT_RENDER_VERSION))
    old = render_span_digest([n], CompactionConfig(render_version=3))
    assert "2026-11-08 23:00:00 UTC +0000" in new.text()
    assert "[sent " not in old.text()
    assert n.message.text() == "GamingPC tomorrow"
    assert render_span_digest([n], CompactionConfig(render_version=3)).text() == old.text()


def test_recall_per_message_zone_and_expansion_offsets_are_stable():
    n = node()
    text, ok = expand_ref([n], n.node_id, zone="America/New_York")
    utc, _ = expand_ref([n], n.node_id, zone="UTC")
    assert not ok and "2026-11-08 18:00:00 EST -0500" in text
    assert text.split("\n", 1)[1] == utc.split("\n", 1)[1]
    assert "18:00:00 EST" in render_hits(search_path([n], "GamingPC"), "GamingPC", "America/New_York")
    msg = RecallMessage(role="user", text=n.message.text(), ts=n.ts)
    for show in ["all", "text"]:
        assert "2026-11-08 18:00:00 EST -0500" in "\n".join(_render(0, msg, show, "America/New_York"))

async def test_native_producing_loop_owner_and_idle_wake(tmp_path):
    from fluid.context.compiler import PromptInputs, compile_prompt
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore

    class Provider:
        name = "anthropic"
        caps = ProviderCaps()
        requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "done"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"})
            yield StreamChunk("message_stop", {})

    provider = Provider()
    compiled = compile_prompt(PromptInputs(base="fixture", cwd=str(tmp_path), include_env=True,
                              include_global_instructions=False, include_project_instructions=False,
                              include_skills=False, include_agents=False, include_plugins=False))
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m", system_sections=compiled)
    loop.ctx.cwd = str(tmp_path)
    values = {"zone": "America/New_York", "now": dt("2026-11-09T17:00:00+00:00"),
              "last_owner_at": dt("2026-11-09T02:14:00+00:00")}
    sources = []
    def context(source):
        sources.append(source)
        return values
    loop.ctx.extra["time_context"] = context
    try:
        await loop.submit(UserTurn(content="hello", source="user"))
        await loop.wait_turn()
        before = [n.message.text() for n in await loop.store.load_path(loop.session_id)]
        values["now"] = dt("2026-11-09T19:00:00+00:00")
        await loop.submit(UserTurn(content="job completed", source="unpark"))
        await loop.wait_turn()
        assert sources == ["user", "unpark"]
        tails = [r.messages[-1].text() for r in provider.requests]
        assert "12:00 PM EST" in tails[0] and "14 hours ago" in tails[0]
        assert "2:00 PM EST" in tails[1] and "16 hours ago" in tails[1]
        after = [n.message.text() for n in await loop.store.load_path(loop.session_id)]
        assert after[:len(before)] == before
        assert "Current time:" not in compiled.text
    finally:
        await loop.aclose()


async def test_delegated_producing_loop_has_internal_wake_clock(tmp_path):
    import sys
    from pathlib import Path
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events import UserTurn
    from fluid.store.base import MemoryStore

    fake = str(Path(__file__).parents[1] / "fixtures/fake_claude.py")
    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, fake],
                         env={"FAKE_CLAUDE_SCENARIO": "a"})
    loop.ctx.cwd = str(tmp_path)
    loop.ctx.extra["time_context"] = lambda _source: {
        "zone": "America/New_York", "now": dt("2026-11-09T17:00:00+00:00"),
        "last_owner_at": dt("2026-11-09T02:14:00+00:00")}
    try:
        await loop.submit(UserTurn(content="job completed", source="unpark"))
        await loop.wait_turn()
        path = await loop.store.load_path(loop.session_id)
        user = next(n.message.text() for n in path if n.message.role == "user")
        assert "12:00 PM EST" in user and "14 hours ago" in user
        assert clock.RELATIVE_TIME_RULE in user
    finally:
        await loop.aclose()


async def test_search_snippets_preserve_individual_send_times(tmp_path):
    from fluid.store.jsonl import JsonlStore
    from fluid.tools.builtins.session_search import _snippet_lines

    store = JsonlStore(tmp_path / "history")
    sid = (await store.create_session()).session_id
    first = node()
    second = SessionNode("node_abcdef012345", first.node_id, Message(role="user", content=[Text("GamingPC today")]),
                         ts=dt("2026-11-09T23:00:00+00:00").timestamp())
    for n in (first, second):
        await store.append(sid, n)
    hits = await store.search(["gamingpc"])
    hit = hits[0]
    snippets = "\n".join(_snippet_lines(hit.texts, ["gamingpc"], hit.text_times, "America/New_York"))
    assert "2026-11-08 18:00:00 EST" in snippets and "2026-11-09 18:00:00 EST" in snippets


def test_epoch_pins_have_absolute_send_times_without_rewriting_the_owner():
    from fluid.context.compaction import render_epoch_stub
    owner = node()
    current = render_epoch_stub([[owner]], CompactionConfig()).text()
    assert "GamingPC tomorrow" in current and "2026-11-08 23:00:00 UTC +0000" in current
    legacy = render_epoch_stub([[owner]], CompactionConfig(render_version=3)).text()
    assert "GamingPC tomorrow" in legacy and "[sent " not in legacy


