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


def node(text="ExampleWorker tomorrow", ts="2026-11-08T23:00:00+00:00"):
    return SessionNode("node_0123456789ab", None, Message(role="user", content=[Text(text)]), ts=dt(ts).timestamp())


@pytest.mark.parametrize("accepted_at", [None, 1_700_000_000.0, 0.0])
def test_legacy_delivery_bytes_and_real_acceptance_times_round_trip(accepted_at):
    from fluid.messages.content import SteerDelivery, ToolResultBlock

    delivery = SteerDelivery(text="keep this", id="queued", accepted_at=accepted_at)
    message = Message(role="user", content=[ToolResultBlock(
        tool_use_id="call", content=[Text("ok")], delivered_steers=[delivery],
        delivery_metadata_version=1)])
    data = message.to_dict()
    expected = {"text": "keep this", "origin": "user", "sender": None, "id": "queued"}
    if accepted_at is not None:
        expected["accepted_at"] = accepted_at
    assert data["content"][0]["delivered_steers"] == [expected]
    restored = Message.from_dict(data)
    assert restored.to_dict() == data
    assert restored.content[0].delivered_steers == [delivery]


def test_clock_est_and_dst_fold():
    assert clock.turn_time_lines(zone="America/New_York", now=dt("2026-11-09T17:48:00+00:00")) == [
        "Current time: Mon Nov 9, 2026, 12:48 PM EST (America/New_York)"]
    before = clock.absolute_time(dt("2026-11-01T05:30:00+00:00"), "America/New_York")
    after = clock.absolute_time(dt("2026-11-01T06:30:00+00:00"), "America/New_York")
    assert "01:30:00 EDT -0400" in before
    assert "01:30:00 EST -0500" in after
    assert clock.absolute_time(dt("2026-11-01T05:30:00Z"), "America/New_York", compact=True) == "2026-11-01T01:30:00-04:00"
    assert clock.absolute_time(dt("2026-11-01T06:30:00Z"), "America/New_York", compact=True) == "2026-11-01T01:30:00-05:00"


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
    assert delegated_wake_time(host, {"time_context": context}, "user", host_time_note=True) == host
    # An old quoted note is content, not host provenance.
    assert len(delegated_wake_time(host, {"time_context": context}, "unpark")) == 2
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
    assert n.message.text() == "ExampleWorker tomorrow"
    assert render_span_digest([n], CompactionConfig(render_version=3)).text() == old.text()



def test_v4_all_interjection_lanes_have_stable_times_and_old_versions_do_not():
    from fluid.context.compaction import render_epoch_stub, told_on_path, merge_told, still_missing, render_told
    from fluid.messages.content import SteerDelivery, ToolResultBlock, ToolUseBlock
    first = node("Opening request tomorrow")
    tool = SessionNode("node_111111111111", first.node_id,
                       Message(role="assistant", content=[ToolUseBlock(id="tool", name="Bash", input={})]), ts=first.ts + 1)
    sent = dt("2026-11-09T04:55:00Z").timestamp()
    delivery = SteerDelivery(text="Interjection tomorrow", id="queued", accepted_at=sent)
    result = SessionNode("node_222222222222", tool.node_id,
                         Message(role="user", content=[ToolResultBlock(tool_use_id="tool", content=[Text("ok")],
                                 delivered_steers=[delivery], delivery_metadata_version=1)]), ts=sent + 900)
    path = [first, tool, result]
    stored = [(n.node_id, n.ts, n.message.to_dict()) for n in path]
    # Baseline a2f658935: pin the complete v1–v3 digest + epoch bytes.
    legacy_hashes = {
        1: "98f66acb54d4f69953519fba14390355ebb16e8a9c668de6e050307db3807e28",
        2: "506a62bddc09f01898fac484e9fd8e571cedc52beccdc0684819df40c4ccb393",
        3: "864a7b7df0d16acdbcd549f66395ffc26a6a92c2ba1117f9e042f7e578868431",
    }
    for version in (1, 2, 3, 4):
        cfg = CompactionConfig(render_version=version)
        digest = render_span_digest(path, cfg).text()
        epoch = render_epoch_stub([path], cfg).text()
        if version < 4:
            from hashlib import sha256
            assert sha256("\0".join((digest, epoch)).encode()).hexdigest() == legacy_hashes[version]
        assert "Interjection tomorrow" in digest
        assert ("[sent/delivered 2026-11-09 04:55:00 UTC +0000" in digest) is (version == 4)
        assert ("[sent/delivered 2026-11-09 04:55:00 UTC +0000" in epoch) is (version == 4)
        assert render_span_digest(path, cfg).text() == digest
        assert render_epoch_stub([path], cfg).text() == epoch
        from fluid.context.compaction import Compactor
        compactor = Compactor(config=cfg)
        pin = compactor._checkpoint_pin(first.node_id, result.node_id, path)
        summary = compactor._summary_message(first.node_id, result.node_id, "summary", pin).text()
        assert ("[sent/delivered 2026-11-09 04:55:00 UTC +0000" in summary) is (version == 4)
        if version == 4:
            # A rolling checkpoint can carry an original brief whose node was
            # already windowed out. Its stored time must travel with the words.
            pin["brief"][0] = "pruned-opening"
            compactor._summaries[(first.node_id, tool.node_id)] = "prior"
            compactor._summary_pins[(first.node_id, tool.node_id)] = pin
            rolled = compactor._checkpoint_pin(first.node_id, result.node_id, path)
            assert rolled["brief_at"] == pin["brief_at"] == first.ts
    brief, told = told_on_path(path)
    # The CLI can restore from the persisted path or from the live receipt.
    live = merge_told([], [("tool", delivery, result.ts)])
    for items in (told, live):
        restored = render_told(brief, still_missing(items, "", dated=True), brief_at=first.ts)
        assert "2026-11-08 23:00:00 UTC +0000" in restored
        assert "2026-11-09 04:55:00 UTC +0000" in restored
    assert [(n.node_id, n.ts, n.message.to_dict()) for n in path] == stored


@pytest.mark.parametrize("reachable", [True, False])
@pytest.mark.parametrize("budget", [512, 4096])
def test_legacy_checkpoint_promotion_dates_reachable_and_windowed_words(reachable, budget):
    import json
    from fluid.context.compaction import Compactor, Promotion
    from fluid.messages.content import SteerDelivery, ToolResultBlock, ToolUseBlock
    first = node("Opening tomorrow")
    tool = SessionNode("node_111111111111", first.node_id,
                       Message(role="assistant", content=[ToolUseBlock(id="tool", name="Bash", input={})]), ts=first.ts + 1)
    sent = dt("2026-11-09T04:55:00Z").timestamp()
    carrier = SessionNode("node_222222222222", tool.node_id,
                          Message(role="user", content=[ToolResultBlock(tool_use_id="tool", content=[Text("ok")],
                                  delivered_steers=[SteerDelivery(text="Earlier " + "x" * 180, id="old"),
                                                    SteerDelivery(text="Do it tomorrow", id="new", accepted_at=sent)],
                                  delivery_metadata_version=1)]), ts=sent + 900)
    covered = SessionNode("node_333333333333", carrier.node_id, Message(role="assistant", content=[Text("done")]), ts=sent + 960)
    later = SessionNode("node_444444444444", covered.node_id, Message(role="assistant", content=[Text("next")]), ts=sent + 1800)
    last = SessionNode("node_555555555555", later.node_id, Message(role="assistant", content=[Text("last")]), ts=sent + 3600)
    original = [first, tool, carrier, covered]
    legacy = Compactor(config=CompactionConfig(render_version=3))
    pin = legacy._checkpoint_pin(first.node_id, covered.node_id, original)
    pin.pop("brief_at")  # Persisted v3 decisions predate this field.
    if not reachable:
        pin["brief"][0] = "windowed-opening"
    snapshot = json.dumps(pin, sort_keys=True)
    legacy_bytes = legacy._summary_message(first.node_id, covered.node_id, "old", pin).text()
    old = Promotion(tier=4, render_version=3, spans=((first.node_id, covered.node_id, "summary"),),
                    summary_text="old", summary_pin=pin)
    compactor = Compactor(config=CompactionConfig(render_version=4, peer_pin_chars=budget))
    compactor.rehydrate([old])
    assert compactor._summary_message(first.node_id, covered.node_id, "old").text() == legacy_bytes
    path = original + [later, last] if reachable else [first, covered, later, last]
    promoted = compactor._checkpoint_pin(first.node_id, later.node_id, path)
    texts = "\n".join(text for _, text in promoted["messages"])
    assert "Do it tomorrow" in texts
    if reachable:
        assert "[sent/delivered 2026-11-09 04:55:00 UTC +0000" in texts
    else:
        assert "[sent/delivered before 2026-11-09 05:11:00 UTC +0000" in texts
        assert "exact time unavailable" in texts
        assert "[sent/delivered before" in promoted["brief"][1]
    assert sum(len(text) for _, text in promoted["messages"]) <= budget
    if budget == 512:
        assert promoted["left_out"] > 0
    new = Promotion(tier=4, render_version=4, spans=((first.node_id, later.node_id, "summary"),),
                    summary_text="new", summary_pin=promoted)
    compactor.rehydrate([new])
    frozen = compactor._summary_message(first.node_id, later.node_id, "new").text()
    restored = Compactor(config=compactor.config)
    restored.rehydrate([old, new])
    assert restored._summary_message(first.node_id, later.node_id, "new").text() == frozen
    rolled = restored._checkpoint_pin(first.node_id, last.node_id, path)
    assert rolled["messages"] == promoted["messages"]
    assert rolled["brief"] == promoted["brief"]
    assert all(text.count("[sent/delivered") == 1 for _, text in rolled["messages"])
    assert json.dumps(pin, sort_keys=True) == snapshot
    assert restored._summary_message(first.node_id, covered.node_id, "old").text() == legacy_bytes


def test_recall_per_message_zone_and_expansion_offsets_are_stable():
    n = node()
    text, ok = expand_ref([n], n.node_id, zone="America/New_York")
    utc, _ = expand_ref([n], n.node_id, zone="UTC")
    assert not ok and "2026-11-08 18:00:00 EST -0500" in text
    assert text.split("\n", 1)[1] == utc.split("\n", 1)[1]
    assert "18:00:00 EST" in render_hits(search_path([n], "ExampleWorker"), "ExampleWorker", "America/New_York")
    msg = RecallMessage(role="user", text=n.message.text(), ts=n.ts)
    for show in ["all", "conversation"]:
        assert "[sent 2026-11-08T18:00:00-05:00]" in "\n".join(_render(0, msg, show, "America/New_York"))

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
        accepted = dt("2026-11-09T04:55:00Z").timestamp()
        await loop.submit(UserTurn(content="hello", source="user", accepted_at=accepted))
        await loop.wait_turn()
        path = await loop.store.load_path(loop.session_id)
        assert next(n.ts for n in path if n.message.role == "user") == accepted
        before = [n.message.text() for n in path]
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


@pytest.mark.parametrize("quoted_note", [False, True])
async def test_delegated_producing_loop_has_internal_wake_clock(tmp_path, quoted_note):
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
        accepted = dt("2026-11-09T04:55:00Z").timestamp()
        await loop.submit(UserTurn(content="owner queue", source="queue", accepted_at=accepted, host_time_note=True))
        await loop.wait_turn()
        opening = await loop.store.load_path(loop.session_id)
        assert next(n.ts for n in opening if n.message.role == "user") == accepted
        text = "job completed"
        if quoted_note:
            text += "\nQuoted: Environment facts that change while a session runs\nCurrent time: stale"
        await loop.submit(UserTurn(content=text, source="unpark"))
        await loop.wait_turn()
        path = await loop.store.load_path(loop.session_id)
        user = [n.message.text() for n in path if n.message.role == "user"][-1]
        assert user.count("Current time:") == (2 if quoted_note else 1)
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
    second = SessionNode("node_abcdef012345", first.node_id, Message(role="user", content=[Text("ExampleWorker today")]),
                         ts=dt("2026-11-09T23:00:00+00:00").timestamp())
    for n in (first, second):
        await store.append(sid, n)
    hits = await store.search(["exampleworker"])
    hit = hits[0]
    snippets = "\n".join(_snippet_lines(hit.texts, ["exampleworker"], hit.text_times, "America/New_York"))
    assert "[sent 2026-11-08T18:00:00-05:00]" in snippets
    assert "[sent 2026-11-09T18:00:00-05:00]" in snippets


@pytest.mark.parametrize("layout", ["one_text", "split_text", "image"])
def test_delegated_clock_keeps_the_host_pin_and_original_content(layout):
    from fluid.messages import Image

    owner = "Owner's question — café 😀\n\n"
    host = "trusted memory snapshot"
    image = Image(media_type="image/png", data="AAAA")
    if layout == "one_text":
        blocks = [Text(owner + host)]
    elif layout == "split_text":
        blocks = [Text(owner), Text(host)]
    else:
        blocks = [Text(owner), image, Text(host)]
    original = Message(role="user", content=blocks).pin_context(host)
    saved = original.to_dict()
    values = {"zone": "UTC", "now": dt("2026-11-09T18:00:00+00:00")}
    result = delegated_wake_time(
        blocks, {"time_context": lambda _source: values}, "unpark",
        before_context=original.context_pin[0],
    )
    rendered = Message(role="user", content=result)
    added = len(rendered.text()) - len(original.text())
    rendered.context_pin = tuple(n + added for n in original.context_pin)
    assert original.to_dict() == saved
    assert rendered.text().startswith(owner)
    assert rendered.pinned_text() == host
    assert rendered.text().count("Current time:") == 1
    assert rendered.text().endswith(host)
    assert [b for b in result if isinstance(b, Image)] == ([image] if layout == "image" else [])


def test_epoch_pins_have_absolute_send_times_without_rewriting_the_owner():
    from fluid.context.compaction import render_epoch_stub
    owner = node()
    current = render_epoch_stub([[owner]], CompactionConfig()).text()
    assert "ExampleWorker tomorrow" in current and "2026-11-08 23:00:00 UTC +0000" in current
    legacy = render_epoch_stub([[owner]], CompactionConfig(render_version=3)).text()
    assert "ExampleWorker tomorrow" in legacy and "[sent " not in legacy

@pytest.mark.parametrize("reachable", [True, False])
@pytest.mark.parametrize("multiple", [False, True])
def test_clipped_legacy_pin_dates_its_carrier_without_restoring_the_full_text(reachable, multiple):
    from copy import deepcopy
    from fluid.context.compaction import Compactor, Promotion, _clip_verbatim
    from fluid.messages.content import SteerDelivery, ToolResultBlock, ToolUseBlock

    first = node("Opening request")
    sent = dt("2026-11-09T04:55:00Z").timestamp()
    tool = SessionNode("tool", first.node_id, Message(role="assistant", content=[
        ToolUseBlock(id="call", name="Bash", input={})]), ts=sent)
    long = "Tomorrow " + "x" * 13_000
    steers = [SteerDelivery(text=long, accepted_at=sent)]
    if multiple:
        steers.insert(0, SteerDelivery(text="Another message", accepted_at=sent - 60))
    carrier = SessionNode("carrier", tool.node_id, Message(role="user", content=[
        ToolResultBlock(tool_use_id="call", content=[Text("ok")], delivered_steers=steers)]), ts=sent + 900)
    covered = SessionNode("covered", carrier.node_id, Message(role="assistant", content=[Text("done")]), ts=sent + 960)
    later = SessionNode("later", covered.node_id, Message(role="assistant", content=[Text("next")]), ts=sent + 1800)
    last = SessionNode("last", later.node_id, Message(role="assistant", content=[Text("last")]), ts=sent + 3600)
    original = [first, tool, carrier, covered]
    old = Compactor(config=CompactionConfig(render_version=3, peer_pin_chars=4096))
    pin = old._checkpoint_pin(first.node_id, covered.node_id, original)
    clipped = _clip_verbatim(steers[-1].render(), 4096)
    assert pin["messages"] == [[carrier.node_id, clipped]]
    if multiple:
        # Saved legacy pins identify messages only by their shared carrier.
        pin["messages"].insert(0, [carrier.node_id, steers[0].render()])
    saved = deepcopy(pin)
    legacy = Promotion(tier=4, render_version=3,
        spans=((first.node_id, covered.node_id, "summary"),), summary_text="old", summary_pin=pin)
    current = Compactor(config=CompactionConfig(render_version=4, peer_pin_chars=5000))
    current.rehydrate([legacy])
    frozen_old = current._summary_message(first.node_id, covered.node_id, "old").text()
    path = original + [later, last] if reachable else [first, covered, later, last]
    promoted = current._checkpoint_pin(first.node_id, later.node_id, path)
    text = promoted["messages"][-1][1]
    assert text.endswith(clipped)
    assert len(text) == len(clipped) + len(text.split("\n", 1)[0]) + 1
    if reachable:
        assert text.startswith("[sent/delivered 2026-11-09 04:55:00 UTC +0000 (UTC)]\n")
        if multiple:
            assert promoted["messages"][0][1].startswith("[sent/delivered 2026-11-09 04:54:00 UTC +0000 (UTC)]\n")
    else:
        assert text.startswith("[sent/delivered before 2026-11-09 05:11:00 UTC +0000 (UTC); exact time unavailable]\n")
    assert sum(len(t) for _, t in promoted["messages"]) <= 5000
    assert pin == saved
    newer = Promotion(tier=4, render_version=4,
        spans=((first.node_id, later.node_id, "summary"),), summary_text="new", summary_pin=promoted)
    current.rehydrate([newer])
    replay = Compactor(config=current.config)
    replay.rehydrate([legacy, newer])
    assert replay._summary_message(first.node_id, later.node_id, "new").text() == current._summary_message(first.node_id, later.node_id, "new").text()
    assert replay._checkpoint_pin(first.node_id, last.node_id, path)["messages"] == promoted["messages"]
    assert replay._summary_message(first.node_id, covered.node_id, "old").text() == frozen_old

@pytest.mark.parametrize("case", ["empty", "literal_marker", "duplicates", "ambiguous", "missing_time"])
def test_legacy_pin_carrier_edge_cases(case):
    from fluid.context.compaction import Compactor, Promotion
    from fluid.messages.content import SteerDelivery, ToolResultBlock, ToolUseBlock

    sent = dt("2026-11-09T04:55:00Z").timestamp()
    text = {"empty": "", "literal_marker": "Real words … (+123 chars)"}.get(case, "same words")
    deliveries = [SteerDelivery(text=text, accepted_at=None if case == "missing_time" else sent)]
    if case in {"duplicates", "ambiguous"}:
        deliveries.append(SteerDelivery(text=text, accepted_at=sent + 60 if case == "ambiguous" else sent))
    first = node("Opening request")
    tool = SessionNode("tool", first.node_id, Message(role="assistant", content=[ToolUseBlock("call", "Bash", {})]), ts=sent)
    carrier = SessionNode("carrier", tool.node_id, Message(role="user", content=[
        ToolResultBlock("call", [Text("ok")], delivered_steers=deliveries)]), ts=sent + 900)
    covered = SessionNode("covered", carrier.node_id, Message(role="assistant", content=[Text("done")]), ts=sent + 960)
    later = SessionNode("later", covered.node_id, Message(role="assistant", content=[Text("next")]), ts=sent + 1800)
    path = [first, tool, carrier, covered, later]
    old = Compactor(config=CompactionConfig(render_version=3))
    pin = old._checkpoint_pin(first.node_id, covered.node_id, path[:-1])
    current = Compactor(config=CompactionConfig(render_version=4))
    current.rehydrate([Promotion(tier=4, render_version=3,
        spans=((first.node_id, covered.node_id, "summary"),), summary_text="old", summary_pin=pin)])
    result = current._checkpoint_pin(first.node_id, later.node_id, path)
    assert len(result["messages"]) == len(deliveries)
    if case == "ambiguous":
        prefix = "[sent/delivered before 2026-11-09 05:11:00 UTC +0000 (UTC); exact time unavailable]\n"
    elif case == "missing_time":
        prefix = "[sent/delivered 2026-11-09 05:10:00 UTC +0000 (UTC)]\n"
    else:
        prefix = "[sent/delivered 2026-11-09 04:55:00 UTC +0000 (UTC)]\n"
    assert result["messages"] == [[carrier.node_id, prefix + d.render()] for d in deliveries]


def test_legacy_clip_validation_does_not_strip_literal_or_forged_markers():
    from fluid.context.compaction import _clip_verbatim, _matches_pinned_text
    assert _matches_pinned_text("", "")
    assert _matches_pinned_text("real … (+123 chars)", "real … (+123 chars)")
    assert not _matches_pinned_text("real … (+123 chars)", "real more words")
    assert _matches_pinned_text(_clip_verbatim("x" * 13000, 4096), "x" * 13000)
    twice = _clip_verbatim(_clip_verbatim("x" * 13000, 4096), 4096)
    assert twice != _clip_verbatim("x" * 13000, 4096) and _matches_pinned_text(twice, "x" * 13000)
    assert not _matches_pinned_text(twice, "y" * 13000)
    assert not _matches_pinned_text("… (+5 chars)", "anything")


def test_partly_retained_old_clip_marker_never_takes_another_receipts_time():
    from fluid.context.compaction import Compactor, Promotion, _clip_verbatim
    from fluid.messages.content import SteerDelivery, ToolResultBlock, ToolUseBlock

    sent = dt("2026-11-09T04:55:00Z").timestamp()
    newest = "Tomorrow " + "x" * 13_000
    rendered = SteerDelivery(text=newest).render()
    shared = 12_000 - rendered.index(newest)  # the wrapper counts toward the clip
    steers = [SteerDelivery(text=newest[:shared] + "… but a different message", accepted_at=sent - 60),
              SteerDelivery(text=newest, accepted_at=sent)]
    first = node("Opening request")
    tool = SessionNode("tool", first.node_id, Message(role="assistant", content=[ToolUseBlock("call", "Bash", {})]), ts=sent)
    carrier = SessionNode("carrier", tool.node_id, Message(role="user", content=[
        ToolResultBlock("call", [Text("ok")], delivered_steers=steers)]), ts=sent + 900)
    covered = SessionNode("covered", carrier.node_id, Message(role="assistant", content=[Text("done")]), ts=sent + 960)
    later = SessionNode("later", covered.node_id, Message(role="assistant", content=[Text("next")]), ts=sent + 1800)
    last = SessionNode("last", later.node_id, Message(role="assistant", content=[Text("last")]), ts=sent + 3600)

    once = Compactor(config=CompactionConfig(render_version=3, peer_pin_chars=12_000))._checkpoint_pin(
        first.node_id, covered.node_id, [first, tool, carrier, covered])
    legacy_once = Promotion(tier=4, render_version=3,
        spans=((first.node_id, covered.node_id, "summary"),), summary_text="old", summary_pin=once)
    grown = Compactor(config=CompactionConfig(render_version=3, peer_pin_chars=12_001))
    grown.rehydrate([legacy_once])
    twice = grown._checkpoint_pin(first.node_id, later.node_id, [first, tool, carrier, covered, later])
    stored = twice["messages"][-1][1]
    assert stored == _clip_verbatim(once["messages"][-1][1], 12_001)
    assert stored[:12_001] == rendered[:12_000] + "…" == steers[0].render()[:12_001]
    legacy_twice = Promotion(tier=4, render_version=3,
        spans=((first.node_id, later.node_id, "summary"),), summary_text="older", summary_pin=twice)

    current = Compactor(config=CompactionConfig(render_version=4, peer_pin_chars=13_000))
    current.rehydrate([legacy_once, legacy_twice])
    promoted = current._checkpoint_pin(first.node_id, last.node_id, [first, tool, carrier, covered, later, last])
    assert promoted["messages"][-1][1] == (
        f"[sent/delivered before 2026-11-09 05:25:00 UTC +0000 (UTC); exact time unavailable]\n{stored}")


def test_legacy_pin_clipped_by_two_checkpoints_keeps_its_exact_send_time():
    from fluid.context.compaction import Compactor, Promotion, _clip_verbatim
    from fluid.messages.content import SteerDelivery, ToolResultBlock, ToolUseBlock

    sent = dt("2026-11-09T04:55:00Z").timestamp()
    steers = [SteerDelivery(text="Another message", accepted_at=sent - 60),
              SteerDelivery(text="Tomorrow " + "x" * 13_000, accepted_at=sent)]
    first = node("Opening request")
    tool = SessionNode("tool", first.node_id, Message(role="assistant", content=[ToolUseBlock("call", "Bash", {})]), ts=sent)
    carrier = SessionNode("carrier", tool.node_id, Message(role="user", content=[
        ToolResultBlock("call", [Text("ok")], delivered_steers=steers)]), ts=sent + 900)
    covered = SessionNode("covered", carrier.node_id, Message(role="assistant", content=[Text("done")]), ts=sent + 960)
    later = SessionNode("later", covered.node_id, Message(role="assistant", content=[Text("next")]), ts=sent + 1800)
    last = SessionNode("last", later.node_id, Message(role="assistant", content=[Text("last")]), ts=sent + 3600)

    v3 = CompactionConfig(render_version=3, peer_pin_chars=4096)
    old = Compactor(config=v3)
    once = old._checkpoint_pin(first.node_id, covered.node_id, [first, tool, carrier, covered])
    legacy_once = Promotion(tier=4, render_version=3,
        spans=((first.node_id, covered.node_id, "summary"),), summary_text="old", summary_pin=once)
    old.rehydrate([legacy_once])
    twice = old._checkpoint_pin(first.node_id, later.node_id, [first, tool, carrier, covered, later])
    stored = twice["messages"][-1][1]
    assert stored == _clip_verbatim(once["messages"][-1][1], 4096) != once["messages"][-1][1]
    legacy_twice = Promotion(tier=4, render_version=3,
        spans=((first.node_id, later.node_id, "summary"),), summary_text="older", summary_pin=twice)

    current = Compactor(config=CompactionConfig(render_version=4, peer_pin_chars=5000))
    current.rehydrate([legacy_once, legacy_twice])
    promoted = current._checkpoint_pin(first.node_id, last.node_id, [first, tool, carrier, covered, later, last])
    assert promoted["messages"][-1][1] == f"[sent/delivered 2026-11-09 04:55:00 UTC +0000 (UTC)]\n{stored}"
