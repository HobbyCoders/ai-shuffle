"""Render surface of the compaction ladder: render versions and steer receipts.

Version 1 is the original renderer and must stay byte-for-byte, because
decisions persisted before render_version existed replay through it. Version 2
turns large harness notices (origin "system") into receipts once their result
is trimmed, digested or folded; user and peer steers stay verbatim.
"""

import re
from dataclasses import replace

from fluid.context.compaction import (
    OUTRANK_LINE,
    SPAN_DIGEST,
    CompactionConfig,
    Compactor,
    Promotion,
    render_epoch_stub,
    render_span_digest,
    split_spans,
)
from fluid.context.recall import expand_ref
from fluid.context.steer_render import render_steers
from fluid.context.trim import TrimConfig, summarize_cell
from fluid.events import ContextCompaction
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.messages.content import SteerDelivery
from fluid.store.base import SessionNode

NOTICE = (
    "<task-notification>\n<task-id>bg-7</task-id>\n<status>completed</status>\n"
    "<result>" + "report line with detail 42\n" * 1_400 + "</result>\n"
    "</task-notification>"
)
_EXPAND_RE = re.compile(r'HistoryExpand\("(node_[0-9a-f]+)", offset=(\d+)\)')


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _turn(t: int, deliveries: list[SteerDelivery]) -> list[SessionNode]:
    """A user turn whose one tool result carries ``deliveries``, stored the
    way the engine stores them: rendered into the result text as well."""
    text = f"result-{t} " + "x" * 3_000 + "".join(d.render() for d in deliveries)
    return [
        _node(f"node_{t}a", "user", [Text(f"please look at topic {t}")]),
        _node(f"node_{t}b", "assistant", [
            ToolUseBlock(id=f"call{t}", name="Read", input={"file_path": f"f{t}.py"}),
        ]),
        _node(f"node_{t}c", "user", [ToolResultBlock(
            tool_use_id=f"call{t}", content=[Text(text)],
            delivered_steers=deliveries, delivery_metadata_version=1,
        )]),
        _node(f"node_{t}d", "assistant", [Text(f"done with {t}")]),
    ]


def _system(text: str = NOTICE, id: str = "n1") -> SteerDelivery:
    return SteerDelivery(text=text, origin="system", id=id)


def _flat(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        for b in m.content:
            if isinstance(b, Text):
                parts.append(b.text)
            elif isinstance(b, ToolResultBlock):
                parts.extend(c.text for c in b.content if isinstance(c, Text))
    return "\n".join(parts)


def _trim_only(render_version: int = 2) -> Compactor:
    return Compactor(config=CompactionConfig(
        enabled=False, render_version=render_version,
        trim=TrimConfig(keep_recent_tool_results=0, min_result_chars=500),
    ))


def test_user_and_peer_steers_stay_verbatim_in_every_render_site() -> None:
    long = "Keep the release gate closed until QA signs off. " * 60
    user = SteerDelivery(text=long, id="u1")
    peer = SteerDelivery(text=long + " (peer)", sender="planner", id="p1")
    legacy_peer = SteerDelivery(text=long + " (legacy)", origin="peer",
                                sender="planner", id="p2")
    steers = [user, peer, legacy_peer]
    nodes = _turn(0, steers)
    config = CompactionConfig()

    trimmer = _trim_only()
    trimmer.trimmer.trim_batch(nodes)
    digest = render_span_digest(nodes, config)
    epoch = render_epoch_stub(split_spans(nodes), config)
    for view in (trimmer.view(nodes), [digest], [epoch]):
        flat = _flat(view)
        for steer in steers:
            assert steer.render().strip() in flat
        assert "receipt" not in flat


def test_large_system_notice_renders_as_receipt_with_working_ref() -> None:
    assert len(NOTICE) > 36_000
    nodes = _turn(0, [_system()])
    config = CompactionConfig()
    trimmed = _trim_only()
    trimmed.trimmer.trim_batch(nodes)
    views = {
        "trim": trimmed.view(nodes),
        "digest": [render_span_digest(nodes, config)],
        "epoch": [render_epoch_stub(split_spans(nodes), config)],
    }
    for site, view in views.items():
        flat = _flat(view)
        assert NOTICE not in flat, site
        assert "harness notice receipt" in flat, site
        assert f"{len(NOTICE)} chars delivered at node_0c" in flat, site
        assert "<task-notification> <task-id>bg-7</task-id>" in flat, site
        assert len(flat) < 8_000, site
        ref, offset = _EXPAND_RE.search(flat).groups()
        text, is_error = expand_ref(nodes, ref, offset=int(offset))
        assert not is_error
        assert text.split("\n", 1)[1].startswith(NOTICE[:5_000]), site


def test_short_or_unrecoverable_notices_stay_verbatim() -> None:
    short = _system("<task-notification>done</task-notification>")
    nodes = _turn(0, [short])
    digest = render_span_digest(nodes, CompactionConfig())
    assert short.render() in digest.content[0].text

    # Recall off: a receipt would point at a tool the model does not have.
    nodes = _turn(1, [_system()])
    no_recall = render_span_digest(nodes, CompactionConfig(recall_tools=False))
    assert NOTICE in no_recall.content[0].text

    # A notice the stored node does not carry cannot be re-read, so it stays.
    block = nodes[2].message.content[0]
    detached = ToolResultBlock(
        tool_use_id=block.tool_use_id, content=[Text("x" * 3_000)],
        delivered_steers=block.delivered_steers, delivery_metadata_version=1,
    )
    nodes[2] = _node("node_1c", "user", [detached])
    digest = render_span_digest(nodes, CompactionConfig())
    assert NOTICE in digest.content[0].text


def test_identical_notices_delivered_together_collapse_to_one_receipt() -> None:
    other = _system(NOTICE.replace("bg-7", "bg-8"), id="n3")
    user = SteerDelivery(text="stop after this step", id="u1")
    steers = [_system(id="n1"), user, _system(id="n2"), other, user]
    nodes = _turn(0, steers)
    rendered = render_steers(steers, CompactionConfig().steer_render(), node=nodes[2])
    assert len(rendered) == 4
    assert "(2 identical copies delivered together)" in rendered[0]
    assert rendered[1] == user.render()
    assert "bg-8" in rendered[2] and "identical copies" not in rendered[2]
    assert rendered[3] == user.render()

    trimmed = _trim_only()
    trimmed.trimmer.trim_batch(nodes)
    flat = _flat(trimmed.view(nodes))
    assert flat.count("harness notice receipt") == 2
    assert flat.count(user.render().strip()) == 2


def test_v1_renderers_keep_every_steer_verbatim() -> None:
    steers = [_system(id="n1"), _system(id="n2")]
    nodes = _turn(0, steers)
    config = CompactionConfig(render_version=1)
    digest = render_span_digest(nodes, config)
    assert digest.content[0].text.count(NOTICE) == 2
    epoch = render_epoch_stub(split_spans(nodes), config)
    assert [b.text for b in epoch.content[1:]] == [s.render() for s in steers]
    v1 = _trim_only(render_version=1)
    v1.trimmer.trim_batch(nodes)
    assert _flat(v1.view(nodes)).count(NOTICE) == 2


def _persisted(promotions: list[Promotion], render_version: int | None) -> list[Promotion]:
    """Round-trip decisions through ContextCompaction rows the way the engine
    does. ``render_version=None`` writes a row from before the field existed."""
    out: list[Promotion] = []
    for promo in promotions:
        row = event_to_dict(ContextCompaction(
            session_id="s", turn_id="t", tier=promo.tier, reason=promo.reason,
            trimmed=[list(p) for p in promo.trimmed],
            spans=[list(s) for s in promo.spans],
            render_version=render_version or 1,
        ))
        if render_version is None:
            row.pop("render_version")
        event = event_from_dict(row)
        assert isinstance(event, ContextCompaction)
        out.append(Promotion(
            tier=event.tier, reason=event.reason,
            trimmed=tuple((n, t) for n, t in event.trimmed),
            spans=tuple((s, e, k) for s, e, k in event.spans),
            render_version=event.render_version,
        ))
    return out


def test_v1_decisions_resume_byte_identical_under_a_v2_compactor() -> None:
    nodes = [*_turn(0, [_system()]), *_turn(1, []), *_turn(2, [])]
    # The pre-upgrade process: everything it decided rendered at version 1.
    old = Compactor(config=CompactionConfig(
        render_version=1,
        trim=TrimConfig(keep_recent_tool_results=0, min_result_chars=500),
    ))
    trimmed = old.trimmer.trim_batch(nodes)
    old._mark_span("node_1a", "node_1d", SPAN_DIGEST)
    before = old.view(nodes)
    assert _flat(before).count(NOTICE) == 1
    promotions = [Promotion(tier=1, trimmed=tuple(trimmed)),
                  Promotion(tier=2, spans=(("node_1a", "node_1d", SPAN_DIGEST),))]

    legacy_rows = _persisted(promotions, render_version=None)
    assert {p.render_version for p in legacy_rows} == {1}
    resumed = Compactor(config=CompactionConfig(
        trim=TrimConfig(keep_recent_tool_results=0, min_result_chars=500),
    ))
    assert resumed.render_version == 2
    resumed.rehydrate(legacy_rows)
    assert resumed.view(nodes) == before

    # A decision the upgraded process makes afterwards renders at version 2,
    # while the old ones keep their text.
    resumed._mark_span("node_0a", "node_0d", SPAN_DIGEST)
    after = _flat(resumed.view(nodes))
    assert NOTICE not in after and "harness notice receipt" in after

    # Trim-only (the host contract) replays old trims at version 1 too.
    trim_only = _trim_only()
    trim_only.rehydrate(legacy_rows)
    v1_trim_only = _trim_only(render_version=1)
    v1_trim_only.rehydrate(legacy_rows)
    assert trim_only.view(nodes) == v1_trim_only.view(nodes)


def test_v2_decisions_resume_byte_identical() -> None:
    nodes = [*_turn(0, [_system()]), *_turn(1, [_system(id="n9")])]
    live = _trim_only()
    pairs = live.trimmer.trim_batch(nodes)
    before = live.view(nodes)
    assert "harness notice receipt" in _flat(before)
    rows = _persisted([Promotion(tier=1, trimmed=tuple(pairs))], render_version=2)
    resumed = _trim_only()
    resumed.rehydrate(rows)
    assert resumed.view(nodes) == before


def test_rolled_back_rung_restores_span_versions() -> None:
    nodes = _turn(0, [_system()])
    c = Compactor()
    c.rehydrate([Promotion(tier=2, spans=(("node_0a", "node_0d", SPAN_DIGEST),),
                           render_version=1)])
    before = c.view(nodes)
    restore = c._tier_snapshot()
    c._mark_span("node_0a", "node_0d", SPAN_DIGEST)  # re-marked at version 2
    assert c.view(nodes) != before
    restore()
    assert c.view(nodes) == before


# ─── Intent pin and caps (plan item 9, B10) ─────────────────────────────────

# The shape of the 51f38b22 brief: a multi-line request whose deciding
# constraint sits well past the 120 chars version 1 quoted at a fold.
BRIEF = (
    "Build the fluid compaction replay harness from the review report.\n"
    "Use the evidence sessions only as fixtures; never load a live database, "
    "and keep every replay deterministic so two runs compare byte for byte.\n"
    "Split the work into lanes by file ownership and run each lane in its own "
    "worktree. Use sol workers, not opus, for every lane; the budget is "
    "fixed.\nReport per-session numbers in one table, with the summed prompt "
    "and the peak, and flag any regression over two percent."
)
# The shape of the 33dc66c6 request: the release gate sits past char 120.
GATE = (
    "Finish the Android build fixes from yesterday and rerun the device "
    "matrix once the emulator images are refreshed on the lab host. Release "
    "gate: do not tag v0.9 until the smoke run passes on a real Pixel device "
    "and the crash-free rate on the beta channel is at least 99.5 percent."
)


def _pair(t: int, user_text: str) -> list[SessionNode]:
    return [
        _node(f"node_{t:02d}a", "user", [Text(user_text)]),
        _node(f"node_{t:02d}b", "assistant", [Text(f"answer-{t} " + "pad " * 500)]),
    ]


def _wake_texts(size: int) -> list[str]:
    from fluid.context.peer import format_peer_message

    body = "worker report line with numbers 12345 " * (size // 38)
    return [
        # fluid's own unpark wake: one reminder block.
        f"<system-reminder>\nBackground work completed.\n{body}\n</system-reminder>",
        # A peer's message waking an idle receiver.
        format_peer_message("worker-3", body).strip(),
        # The host's consolidated task notification.
        f"<task-notification>\n<task-id>bg-1</task-id>\n{body}\n</task-notification>",
    ]


def _epoch_text(message: Message) -> str:
    return message.content[0].text


def test_fold_keeps_the_brief_verbatim() -> None:
    nodes = _pair(0, BRIEF)
    for t in range(1, 12):
        nodes += _pair(t, f"continue with step {t}")
    config = CompactionConfig(
        trigger_tokens=1_000, target_ratio=0.5, keep_recent_turns=2,
        trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=2,
                        min_result_chars=500),
        max_resident_span_digests=3, epoch_fold_spans=3,
    )
    compactor = Compactor(config=config)
    rendered, promos = compactor.apply(nodes)
    assert [p for p in promos if p.tier == 3]
    epochs = [m for m in rendered if _epoch_text(m).startswith("[epoch node_00a")]
    assert len(epochs) == 1
    stub = _epoch_text(epochs[0])
    assert BRIEF in stub
    assert "original request (node_00a):" in stub
    # Byte-identical on the next render.
    assert compactor.view(nodes) == rendered

    # Version 1 kept 120 chars of it and lost the constraint.
    v1 = render_epoch_stub(split_spans(nodes[:6]), replace(config, render_version=1))
    assert "sol workers, not opus" not in _epoch_text(v1)
    assert "began with:" in _epoch_text(v1)


def test_wake_text_does_not_evict_the_governing_or_recent_requests() -> None:
    # The release gate arrives mid-fold, then wake notices far larger than the
    # whole pin budget arrive after it.
    nodes = _pair(0, BRIEF) + _pair(1, GATE)
    t = 2
    for text in _wake_texts(8_000) * 2:
        nodes += _pair(t, text)
        t += 1
    stub = _epoch_text(render_epoch_stub(split_spans(nodes), CompactionConfig()))
    assert BRIEF in stub and GATE in stub
    assert "worker report line" not in stub
    assert "<task-notification>" not in stub and "[agent message" not in stub
    assert stub.index(BRIEF) < stub.index(GATE)


def test_pin_orders_later_requests_newest_first_within_budget() -> None:
    config = CompactionConfig(intent_pin_tokens=1_000, user_turn_text_cap=1_500)
    nodes = _pair(0, BRIEF)
    for t in range(1, 8):
        nodes += _pair(t, f"request {t}: " + "detail " * 200)
    stub = _epoch_text(render_epoch_stub(split_spans(nodes), config))
    pinned = re.findall(r"later request \((node_\d+a)\)", stub)
    # 4,000 chars of budget: the brief, then as many 1,411-char requests as
    # fit, newest first.
    assert pinned == ["node_07a", "node_06a"]
    assert "(5 older request(s) not pinned for space" in stub
    assert BRIEF in stub
    # Overlong requests are clipped with their size, never silently cut.
    long_config = CompactionConfig(user_turn_text_cap=200)
    long_stub = _epoch_text(render_epoch_stub(split_spans(_pair(0, BRIEF)), long_config))
    assert f"… (+{len(BRIEF) - 200} chars)" in long_stub


def test_digest_keeps_real_user_turn_text_to_the_turn_cap() -> None:
    request = "please do this: " + "carefully " * 180  # about 1,800 chars
    wake = _wake_texts(3_000)[2]
    real = render_span_digest(_pair(0, request), CompactionConfig()).content[0].text
    assert " ".join(request.split()) in real
    woke = render_span_digest(_pair(1, wake), CompactionConfig()).content[0].text
    [wake_line] = [ln for ln in woke.splitlines() if ln.startswith("user (")]
    assert wake_line.endswith(f"… (+{len(' '.join(wake.split())) - 600} chars)")
    # Version 1 keeps the old 600-char cap for every user text.
    v1 = render_span_digest(_pair(0, request), CompactionConfig(render_version=1))
    assert " ".join(request.split()) not in v1.content[0].text
    assert "… (+" in v1.content[0].text


# ─── Code-cell summaries and traceback eligibility (plan item 10, B8, B9) ───

CELL_CODE = (
    "from tools import Read, Skill\nimport json\n\n"
    'spec = Read(file_path="docs/compaction/very/long/directory/name/REPORT.md", limit=400)\n'
    'tools.ToolSearch(query="select:mcp__browser__browser_navigate")\n'
    'guide = Skill(name="release-notes")\n'
    "print(len(spec))\n"
)
CELL_OUT = (
    "line\n" * 900
    + 'Traceback (most recent call last):\n  File "<cell>", line 3\nKeyError: \'x\'\n'
)


def _cell_turn(t: int, out: str = CELL_OUT, *, code: str = CELL_CODE) -> list[SessionNode]:
    return [
        _node(f"n{t}a", "user", [Text(f"look at topic {t}")]),
        _node(f"n{t}b", "assistant", [
            Text("checking"),
            ToolUseBlock(id=f"c{t}", name="run_python", input={"code": code}),
        ]),
        _node(f"n{t}c", "user", [ToolResultBlock(
            tool_use_id=f"c{t}", content=[Text(out)], delivery_metadata_version=1,
        )]),
        _node(f"n{t}d", "assistant", [Text(f"done {t}")]),
    ]


def _results(messages: list[Message]) -> list[str]:
    return [b.content[0].text for m in messages for b in m.content
            if isinstance(b, ToolResultBlock)]


def test_summarize_cell_names_nested_calls_and_skips_imports() -> None:
    line = summarize_cell(CELL_CODE + 'Bash(command="git status --short")\n')
    assert line.startswith('first line: spec = Read(file_path="docs/')
    for call in ('Read(file_path="docs/', 'ToolSearch(query="select:',
                 'Skill(name="release-notes")', 'Bash(command="git status --short")'):
        assert call in line
    # The 60-char hint clips the path, so the full path is listed once more.
    assert "paths: docs/compaction/very/long/directory/name/REPORT.md" in line
    assert "commands:" not in line  # the hint already shows the whole command
    assert "still loaded: ToolSearch browser_navigate; Skill release-notes" in line
    # Imported names that are not tools are never taken for calls.
    assert "calls" not in summarize_cell("p = Path('x')\nCounter([1])")
    # A cell that does not parse still names its tool calls.
    assert "calls Read(…)" in summarize_cell('import tools\nRead("a.py"')
    assert summarize_cell(CELL_CODE) == summarize_cell(CELL_CODE)


def test_digest_and_epoch_name_nested_read_skill_and_toolsearch() -> None:
    nodes = _cell_turn(0) + _cell_turn(1)
    config = CompactionConfig()
    digest = render_span_digest(nodes[:4], config).content[0].text
    lines = digest.split("\n")
    assert lines[1] == OUTRANK_LINE and lines[2] == "tools: run_python×1"
    assert "  → run_python: first line: spec = Read(" in digest
    assert "still loaded: ToolSearch browser_navigate; Skill release-notes" in digest
    assert "✓ result 4573 chars (n0c); last line: \"KeyError: 'x'\"" in digest
    assert '{"code"' not in digest

    epoch = render_epoch_stub(split_spans(nodes), config).content[0].text
    lines = epoch.split("\n")
    assert lines[1] == OUTRANK_LINE
    assert lines[2] == (
        "files read in these turns: docs/compaction/very/long/directory/name/REPORT.md"
    )
    assert lines[3] == (
        "still loaded from these turns: ToolSearch browser_navigate; Skill release-notes"
    )
    assert lines[4].startswith("Pinned intent:")
    no_recall = render_span_digest(nodes[:4], CompactionConfig(recall_tools=False))
    assert "HistoryExpand" not in no_recall.content[0].text


def test_compactor_placeholder_describes_the_cell_from_the_full_call_map() -> None:
    nodes = _cell_turn(0) + _cell_turn(1)
    compactor = _trim_only()
    compactor.trimmer.trim_batch(nodes, keep_recent=0)
    text = _results(compactor.view(nodes))[0]
    assert "This cell ran: first line: spec = Read(" in text
    assert "Skill(name=\"release-notes\")" in text
    assert "Its output ended: \"KeyError: 'x'\"." in text
    # The same pairs persisted at version 1 keep the old, call-less text.
    v1 = _trim_only()
    v1.rehydrate([Promotion(tier=1, trimmed=(("n0c", "c0"),), render_version=1)])
    assert "This cell ran" not in _results(v1.view(nodes))[0]


def test_traceback_result_survives_one_turn() -> None:
    plain = "ok line\n" * 600
    nodes = _cell_turn(0) + _cell_turn(1, plain) + _cell_turn(2, plain)
    trimmer = _trim_only().trimmer
    # One turn start follows the traceback: it stays, plain output goes.
    first = trimmer._eligible(nodes[:8])
    assert ("n0c", "c0") not in first and ("n1c", "c1") in first
    # A second turn start follows it: now it is trimmed like anything else.
    assert ("n0c", "c0") in trimmer._eligible(nodes)
    # A traceback early in a long output is not held.
    early = "Traceback (most recent call last):\n" + "x\n" * 2_000
    assert ("n0c", "c0") in trimmer._eligible(_cell_turn(0, early) + _cell_turn(1))
    # Overflow recovery's keep_recent=0 waives the hold.
    assert ("n0c", "c0") in trimmer._eligible(nodes[:8], keep_recent=0)


# Version 1 output for the cell fixture, captured from origin/main 0163171fe.
V1_DIGEST = (
    '[compacted turn n0a..n0d — mechanical digest; originals intact: '
    'HistoryExpand("n0a..n0d") re-reads verbatim, HistorySearch finds text]\n'
    "tools: run_python×1\n"
    "user (n0a): look at topic 0\n"
    "assistant (n0b): checking\n"
    '  → run_python({"code": "from tools import Read, Skill\\nimport json\\n\\n'
    'spec = Read(file_path=\\"docs/compaction/very/long/directory/name…)\n'
    "  ✓ result 4573 chars (n0c)\n"
    "assistant (n0d): done 0"
)
V1_EPOCH = (
    "[epoch n0a..n1d — 2 older turns folded; HistorySearch finds their "
    'content, HistoryExpand("n0a..n1d") re-reads them; began with: look at topic 0]'
)
V1_PLACEHOLDER = (
    "[old tool result trimmed to save context — 4573 chars removed. It began: \""
    + "line " * 32 + "…\" The rest is NOT in context: do not reconstruct it "
    'from memory; recover it verbatim with HistoryExpand("n0c") when any detail matters]'
)


def test_v1_render_version_output_unchanged_for_code_cells() -> None:
    nodes = _cell_turn(0) + _cell_turn(1)
    v1 = CompactionConfig(render_version=1)
    assert render_span_digest(nodes[:4], v1).content[0].text == V1_DIGEST
    assert [b.text for b in render_epoch_stub(split_spans(nodes), v1).content] == [V1_EPOCH]
    compactor = _trim_only(render_version=1)
    compactor.trimmer.trim_batch(nodes, keep_recent=0)
    assert _results(compactor.view(nodes)) == [
        V1_PLACEHOLDER, V1_PLACEHOLDER.replace("n0c", "n1c"),
    ]
    # Spans persisted at version 1 under a version 2 compactor render the same.
    resumed = Compactor()
    resumed.rehydrate([Promotion(tier=2, spans=(("n0a", "n0d", SPAN_DIGEST),),
                                 render_version=1)])
    assert resumed.view(nodes)[0].content[0].text == V1_DIGEST
