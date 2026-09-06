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
    """ai-shuffle upgrades read-only agents (Explore) to background.

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
    out = await handler({"agent": "Explore", "prompt": "find it"}, ctx)
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
    out = await handler({"agent": "Explore", "prompt": "p"}, ctx)
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
    out = await handler({"agent": "Explore", "prompt": "p"}, ctx)
    assert out.is_error and not started


async def test_a_sync_dict_receipt_is_registered_too() -> None:
    from fluid.tools.builtins.task import handler

    started: list[BackgroundTaskReceipt] = []

    async def dispatch(input: dict, ctx: ToolCtx) -> dict:
        return {"ok": True, "mode": "background", "task_id": "t-3"}

    ctx = ToolCtx(
        extra={"task_dispatch": dispatch, "background_task_started": started.append}
    )
    out = await handler({"agent": "Explore", "prompt": "p"}, ctx)
    assert out.background_task is not None
    assert [r.task_id for r in started] == ["t-3"]
