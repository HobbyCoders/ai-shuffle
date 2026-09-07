"""One ``subagent.started`` row per app-dispatched spawn.

On the native/fluid leg an app-dispatched sub-agent spawn had TWO announcers:

* the streaming loop in ``app/core/query_engine.py``, which emitted a GENERIC
  ``subagent_start`` (agent_type "Agent", no agent name, task_id = the model's
  Task ``tool_use`` id), plus a redundant generic ``subagent_done`` via the
  defer / end-of-turn-flush machinery; and
* ``WorkerFeed.started()`` in ``app/core/provider_agent_feed.py``, which emits
  the CANONICAL row — real agent name, model slug in agent_type, task_id = the
  worker session id, ``launch_tool_use_id`` joining it back to the tool_use.

WorkerFeed's row wins, so the streaming loop must stay silent for app-dispatched
spawns ONLY. The discriminator is the tool input's SHAPE: fluid's bare-named
"Task" dispatch builtin (``fluid/fluid/tools/builtins/task.py``) takes
``{agent, prompt, mode, label}`` — always an ``agent`` key, never
``subagent_type`` — while the CLI's genuine Task/Agent sub-agent tools carry
``subagent_type`` / ``description`` / ``prompt`` and never ``agent``.

Harness is borrowed wholesale from
``tests/core/test_cross_turn_background_subagent_routing.py`` (no Postgres, no
real SDK).
"""

from contextlib import ExitStack

import app.core.query_engine as qe
from app.core.task_registry import task_registry

from app.core.provider.events import (
    AssistantMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from tests.core.test_cross_turn_background_subagent_routing import (
    _StubPool,
    _chat_patches,
    _collect,
    _fake_state,
)


APP_DISPATCH_INPUT = {"agent": "explore", "prompt": "go", "mode": "sync"}
CLI_TASK_INPUT = {"subagent_type": "explore", "description": "d", "prompt": "go"}
CLI_AGENT_INPUT = {"description": "d", "prompt": "go"}


def _launch(tool_id, name, tool_input):
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name=name, input=tool_input)],
        model="claude",
    )


def _result(tool_id, content, is_error=False):
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)]
    )


async def _run(session_id, messages):
    state = _fake_state(messages)
    pool = _StubPool(state)
    with ExitStack() as stack:
        _chat_patches(stack, pool)
        return await _collect(qe.stream_to_websocket("prompt", session_id, "claude-code"))


class TestAppDispatchSubagentStartSuppressed:
    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    async def test_app_dispatch_task_emits_no_generic_subagent_start(self):
        events = await _run(
            "sess-app-dispatch", [_launch("toolu_ad", "Task", APP_DISPATCH_INPUT)]
        )
        assert not any(e.get("type") == "subagent_start" for e in events), (
            "app-dispatched spawn emitted a generic subagent_start; WorkerFeed's "
            f"row is canonical: {events}"
        )

    async def test_cli_shaped_task_still_emits_subagent_start(self):
        events = await _run(
            "sess-cli-task", [_launch("toolu_cli", "Task", CLI_TASK_INPUT)]
        )
        starts = [e for e in events if e.get("type") == "subagent_start"]
        assert len(starts) == 1, f"CLI-shaped Task lost its subagent_start: {events}"
        assert starts[0]["tool_id"] == "toolu_cli"
        assert starts[0]["agent_type"] == "explore"

    async def test_agent_without_subagent_type_still_emits_subagent_start(self):
        # description+prompt but NO subagent_type and NO "agent" key — a CLI
        # launch, not an app dispatch.
        events = await _run(
            "sess-cli-agent", [_launch("toolu_desc", "Agent", CLI_AGENT_INPUT)]
        )
        starts = [e for e in events if e.get("type") == "subagent_start"]
        assert len(starts) == 1, f"description-only Agent lost its start: {events}"
        assert starts[0]["agent_type"] == "d"


class TestAppDispatchSubagentDoneSuppressed:
    """The terminal half: neither the end-of-turn drain flush nor the
    launch-failure ``emit`` arm may fire a generic ``subagent_done`` for an
    app-dispatched spawn."""

    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    async def test_app_dispatch_sync_result_not_flushed_as_subagent_done(self):
        # mode="sync": the dispatcher returns the worker's real report inline.
        # No background evidence and no ACK phrase → the gate DEFERS, and the
        # end-of-turn flush would normally emit. It must not, here.
        events = await _run("sess-ad-sync", [
            _launch("toolu_ad2", "Task", APP_DISPATCH_INPUT),
            _result("toolu_ad2", "The worker finished: STEP-1 and STEP-2 done."),
        ])
        assert not any(e.get("type") == "subagent_done" for e in events), (
            f"app-dispatched spawn emitted a generic subagent_done: {events}"
        )

    async def test_cli_shaped_sync_result_still_flushes_subagent_done(self):
        events = await _run("sess-cli-sync", [
            _launch("toolu_cli2", "Task", CLI_TASK_INPUT),
            _result("toolu_cli2", "The worker finished: STEP-1 and STEP-2 done."),
        ])
        dones = [e for e in events if e.get("type") == "subagent_done"]
        assert len(dones) == 1, f"CLI-shaped Task lost its subagent_done: {events}"
        assert dones[0]["tool_id"] == "toolu_cli2"

    async def test_app_dispatch_launch_failure_emits_nothing_generic(self):
        # An is_error ToolResult is positive terminal evidence → the `emit` arm.
        # Suppressed for app dispatch, but the tracking cleanup must still run
        # (no stale entry left for the end-of-turn flush to re-emit).
        events = await _run("sess-ad-error", [
            _launch("toolu_ad3", "Task", APP_DISPATCH_INPUT),
            _result("toolu_ad3", "unknown agent 'explore'", is_error=True),
        ])
        assert not any(e.get("type") == "subagent_done" for e in events), (
            f"app-dispatched launch failure emitted a generic subagent_done: {events}"
        )

    async def test_cli_shaped_launch_failure_still_emits_subagent_done(self):
        events = await _run("sess-cli-error", [
            _launch("toolu_cli3", "Task", CLI_TASK_INPUT),
            _result("toolu_cli3", "unknown agent 'explore'", is_error=True),
        ])
        dones = [e for e in events if e.get("type") == "subagent_done"]
        assert len(dones) == 1, f"CLI-shaped launch failure lost its terminal: {events}"
        assert dones[0]["is_error"] is True
