"""Round-7 production audit — the tool-surface half.

One day of gpt-6-astra sessions, and the same handful of tool surfaces cost
whole turns each. What is asserted here is the behaviour those sessions needed:

* ``ToolSearch(select:…)`` accepting the LEAF names the code surface teaches
  (six sessions, one aborted cell apiece);
* ``Task`` registering a background worker the HOST upgraded, not only one the
  model asked for (turns that ended with a live worker and no park);
* ``Ledger`` answering add/remove with a delta instead of the whole ledger
  (1.18 MB of tool output in one session) and rotating a section in one call;
* ``Monitor``'s receipt agreeing with ``Task``'s about when to end the turn;
* ``WebFetch`` saying so when a page is mostly navigation.
"""

from __future__ import annotations

from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
)

# ─── 1. ToolSearch: select: speaks leaf names ────────────────────────────────


def _registry() -> ToolRegistry:
    from fluid.tools.registry import _Entry

    registry = ToolRegistry()
    for server, tool in (
        ("browser", "browser_navigate"),
        ("browser", "browser_screenshot"),
        ("browser", "browser_resize"),
    ):
        spec = ToolSpec(
            name=f"mcp__{server}__{tool}",
            description=f"{tool} via {server}",
            input_schema={"type": "object", "properties": {}},
            source="mcp",
        )
        registry._entries[spec.name] = _Entry(spec=spec, server=server)
    return registry


async def test_select_accepts_the_leaf_names_search_tools_prints() -> None:
    from fluid.tools.builtins.tool_search import handler

    registry = _registry()
    ctx = ToolCtx(extra={"tool_registry": registry})
    out = await handler(
        {"query": "select:browser_navigate,browser_screenshot"}, ctx
    )
    assert not out.is_error, out.text
    loaded = {s.name for s in registry.list_tools(loaded_only=True)}
    assert loaded == {
        "mcp__browser__browser_navigate",
        "mcp__browser__browser_screenshot",
    }
    # On the JSON surface the call still has to use the wire name, so say so.
    assert "browser_navigate → mcp__browser__browser_navigate" in out.text


async def test_select_mixes_leaf_and_wire_names_and_dedupes() -> None:
    from fluid.tools.builtins.tool_search import handler

    registry = _registry()
    ctx = ToolCtx(extra={"tool_registry": registry})
    out = await handler(
        {
            "query": (
                "select:browser_navigate,mcp__browser__browser_navigate,"
                "browser_resize"
            )
        },
        ctx,
    )
    assert not out.is_error
    assert out.text.count('"name": "mcp__browser__browser_navigate"') == 1
    assert '"name": "mcp__browser__browser_resize"' in out.text


async def test_select_suggests_the_closest_match_like_read_does() -> None:
    from fluid.tools.builtins.tool_search import handler

    registry = _registry()
    ctx = ToolCtx(extra={"tool_registry": registry})
    out = await handler({"query": "select:browser_navigat"}, ctx)
    assert out.is_error
    assert "did you mean: browser_navigate" in out.text
    assert "leaf name" in out.text


async def test_select_from_the_kernel_prints_the_import_line_for_a_leaf() -> None:
    from fluid.tools.builtins.tool_search import handler

    registry = _registry()
    ctx = ToolCtx(extra={"tool_registry": registry, "in_kernel_call": True})
    out = await handler({"query": "select:browser_navigate"}, ctx)
    assert not out.is_error
    assert "from tools.browser_navigate import browser_navigate" in out.text
    # In a cell the leaf IS the callable name, so no rename advice is wanted.
    assert "call these by their full name" not in out.text


async def test_select_respects_the_session_tool_filter_through_leaves() -> None:
    from fluid.tools.builtins.tool_search import handler

    registry = _registry()
    ctx = ToolCtx(
        extra={
            "tool_registry": registry,
            "tool_filter": lambda name: name.endswith("browser_navigate"),
        }
    )
    out = await handler({"query": "select:browser_screenshot"}, ctx)
    # A filtered tool is reported exactly like one that does not exist.
    assert out.is_error and "browser_screenshot" in out.text
    assert not registry.list_tools(loaded_only=True)


# ─── 2. Task: the host may upgrade the mode ──────────────────────────────────


async def test_background_receipt_from_a_sync_request_is_registered() -> None:
    """ai-shuffle upgrades read-only agents (Plan) to background.

    fluid branched on the MODEL's ``mode``, so an upgraded worker never entered
    ``_background_pending``: the turn ended with a live worker and no park, and
    the completion arrived through the "finished a task we never saw start"
    path. The branch is on the returned receipt now.
    """
    from fluid.tools.builtins.task import handler

    started: list[BackgroundTaskReceipt] = []
    receipt = BackgroundTaskReceipt(task_id="t-1", worker_session_id="w-1")

    async def dispatch(input: dict, ctx: ToolCtx) -> ToolOutcome:
        # The host upgraded a mode-less (read-only) request to background.
        return ToolOutcome(text="worker started", background_task=receipt)

    ctx = ToolCtx(
        extra={
            "task_dispatch": dispatch,
            "background_task_started": started.append,
        }
    )
    out = await handler({"agent": "Plan", "prompt": "find it"}, ctx)
    assert not out.is_error
    assert out.background_task is receipt
    assert started == [receipt]


async def test_sync_dispatch_without_a_receipt_is_unchanged() -> None:
    from fluid.tools.builtins.task import handler

    started: list = []

    async def dispatch(input: dict, ctx: ToolCtx) -> str:
        return "the report"

    ctx = ToolCtx(
        extra={"task_dispatch": dispatch, "background_task_started": started.append}
    )
    out = await handler({"agent": "Plan", "prompt": "p"}, ctx)
    assert out.text == "the report" and not started


async def test_an_errored_upgrade_is_not_registered() -> None:
    from fluid.tools.builtins.task import handler

    started: list = []
    receipt = BackgroundTaskReceipt(task_id="t-2")

    async def dispatch(input: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="nope", is_error=True, background_task=receipt)

    ctx = ToolCtx(
        extra={"task_dispatch": dispatch, "background_task_started": started.append}
    )
    out = await handler({"agent": "Plan", "prompt": "p"}, ctx)
    assert out.is_error and not started


async def test_a_sync_dict_receipt_is_registered_too() -> None:
    from fluid.tools.builtins.task import handler

    started: list[BackgroundTaskReceipt] = []

    async def dispatch(input: dict, ctx: ToolCtx) -> dict:
        return {"ok": True, "mode": "background", "task_id": "t-3"}

    ctx = ToolCtx(
        extra={"task_dispatch": dispatch, "background_task_started": started.append}
    )
    out = await handler({"agent": "Plan", "prompt": "p"}, ctx)
    assert out.background_task is not None
    assert [r.task_id for r in started] == ["t-3"]


# ─── 3. Ledger: a delta per mutation, and rotation in one call ───────────────


async def test_mutations_return_a_delta_not_the_whole_ledger(tmp_path) -> None:
    """205 Ledger calls returned 1.18 MB in one audited session.

    Every action echoed the full rendered ledger — state the engine already
    recites onto every user turn — so bookkeeping was the third-largest source
    of tool-result bytes in the session.
    """
    from fluid.tools.builtins.ledger import handler

    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    await handler(
        {"action": "add", "section": "constraints", "text": "never touch prod"}, ctx
    )
    out = await handler(
        {"action": "add", "section": "state", "text": "PR #1 open"}, ctx
    )
    assert out.text == 'added 1 state entry ("PR #1 open") — 1 in state, 2 total'
    # The other section's contents are NOT echoed back.
    assert "never touch prod" not in out.text
    assert "# Session ledger" not in out.text

    out = await handler(
        {"action": "remove", "section": "state", "text": "PR #1 open"}, ctx
    )
    assert "never touch prod" not in out.text and "0 in state, 1 total" in out.text

    # show is the one action that still renders it.
    out = await handler({"action": "show"}, ctx)
    assert "never touch prod" in out.text and out.text.startswith("# Session ledger")


async def test_replace_rotates_a_section_in_one_call(tmp_path) -> None:
    """The audited rotation idiom cost 15-21 calls: show, remove per line, re-add."""
    from fluid.tools.builtins.ledger import handler

    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    for text in ("step 1 done", "step 2 running", "step 3 queued"):
        await handler({"action": "add", "section": "state", "text": text}, ctx)
    await handler(
        {"action": "add", "section": "constraints", "text": "never touch prod"}, ctx
    )

    out = await handler(
        {
            "action": "replace",
            "section": "state",
            "entries": ["step 2 done", "step 3 running"],
        },
        ctx,
    )
    assert out.text == "replaced state: 3 entries out, 2 in — 2 in state, 3 total"
    shown = (await handler({"action": "show"}, ctx)).text
    assert "step 1 done" not in shown and "step 3 running" in shown
    # Untouched sections survive a rotation.
    assert "never touch prod" in shown


async def test_rotate_is_an_accepted_spelling_and_takes_a_text_blob(tmp_path) -> None:
    from fluid.tools.builtins.ledger import handler

    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    await handler({"action": "add", "section": "threads", "text": "old"}, ctx)
    out = await handler(
        {"action": "rotate", "section": "threads", "text": "- one\n- two\n"}, ctx
    )
    assert not out.is_error
    shown = (await handler({"action": "show"}, ctx)).text
    assert "- one" in shown and "- two" in shown and "old" not in shown


async def test_replace_can_clear_a_section_and_still_validates(tmp_path) -> None:
    from fluid.tools.builtins.ledger import handler

    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    await handler({"action": "add", "section": "threads", "text": "old"}, ctx)
    out = await handler({"action": "replace", "section": "threads", "entries": []}, ctx)
    assert not out.is_error and "0 in threads, 0 total" in out.text
    assert (await handler({"action": "show"}, ctx)).text.endswith("(empty)")

    assert (await handler({"action": "replace", "entries": ["x"]}, ctx)).is_error
    assert (await handler({"action": "replace", "section": "threads"}, ctx)).is_error


# ─── 4. Monitor: one doctrine, two receipts ──────────────────────────────────


def test_monitor_and_task_receipts_agree_about_ending_the_turn() -> None:
    """Monitor forbade unconditionally what Task explicitly permitted.

    An audited session started a Monitor, kept working on disjoint work (right),
    and read its output afterwards (also right) — both literal violations of
    Monitor's receipt and both endorsed by Task's.
    """
    from fluid.tools.builtins.monitor import started_text
    from fluid.tools.builtins.task import describe

    receipt = started_text("mon-1")
    assert "unless you have genuinely disjoint work" in receipt
    assert "End the turn now —" not in receipt
    # The shared fragment Task's description carries says the same thing.
    assert "do not sleep, poll" in describe().lower()


def test_monitor_receipt_asks_for_every_independent_job_first() -> None:
    from fluid.tools.builtins.monitor import describe, started_text

    assert "one park can cover several" in started_text("mon-2")
    assert "one park can cover several" in describe()


def test_monitor_receipt_permits_reading_the_buffer_after_completion() -> None:
    from fluid.tools.builtins.monitor import started_text

    receipt = started_text("mon-3")
    assert "reading the full output is expected, not polling" in receipt
    # The blanket prohibition that covered it is gone.
    assert "check on it yourself" not in receipt


# ─── 5. WebFetch: name a navigation page and offer the next hop ──────────────


def _nav_html(n: int = 20) -> str:
    items = "\n".join(
        f'<li><a href="/docs/guide/topic-{i}">Configuring topic {i} in depth</a></li>'
        for i in range(n)
    )
    return (
        "<html><head><title>Docs index</title></head><body>"
        '<nav><a href="/">Home</a><a href="/docs">Docs</a>'
        '<a href="https://twitter.com/x">Twitter</a></nav>'
        f"<ul>{items}</ul><p>Browse the guides.</p></body></html>"
    )


def test_navigation_note_lists_candidate_detail_links() -> None:
    from fluid.tools.builtins.web import _extract, navigation_note

    url = "https://example.com/docs"
    extractor = _extract(_nav_html(), url)
    note = navigation_note(extractor, extractor.text(), url)
    assert "mostly navigation" in note
    assert "candidate detail links" in note
    assert "https://example.com/docs/guide/topic-0" in note
    # Site chrome and off-site links are not offered as next hops.
    assert "twitter.com" not in note
    assert "- Home —" not in note


def test_a_prose_page_gets_no_navigation_note() -> None:
    from fluid.tools.builtins.web import _extract, navigation_note

    url = "https://example.com/article"
    body = "The pricing is $3 per million tokens. " * 60
    html = f'<html><body><p>{body}</p><a href="/other">Another article</a></body></html>'
    extractor = _extract(html, url)
    assert navigation_note(extractor, extractor.text(), url) == ""


async def test_fetch_appends_the_note_after_the_offset_marker() -> None:
    import httpx

    from fluid.tools.builtins.web import fetch_handler

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=_nav_html(200).encode("utf-8"),
        )

    ctx = ToolCtx(extra={"web_transport": httpx.MockTransport(respond)})
    out = await fetch_handler(
        {"url": "https://example.com/docs", "max_chars": 500}, ctx
    )
    assert not out.is_error
    # The paging marker survives; the note rides after it.
    assert "continue with offset=" in out.text
    assert out.text.index("continue with offset=") < out.text.index(
        "mostly navigation"
    )
    assert "candidate detail links" in out.text
