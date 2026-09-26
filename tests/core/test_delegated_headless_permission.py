"""A delegated turn on a path with no permission callback must finish.

``DelegatedLoop`` passes ``--permission-prompt-tool stdio``, so the claude CLI
sends a ``can_use_tool`` request for every call its mode does not approve, and
blocks until it is answered. Only the WebSocket path wires ``can_use_tool``;
the REST/background paths (``stream_query``, ``start_background_query``,
``execute_query``, account failover) do not. On those paths the adapter
answers from the session's permission mode: Ask denies with the reason, and so
does Accept Edits for anything the CLI asks about (the CLI runs file edits
itself under that mode; the fake asks about a Bash call); Bypass allows.

Hermetic: drives fluid's fake ``claude`` (scenario ``b``: one tool call that
asks ``can_use_tool``) through the real HarnessAdapter.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core.provider.events import ResultMessage
from app.core.provider.types import SessionOptions

FAKE = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"

# Far longer than the fake needs to finish a turn (well under a second).
TURN_BUDGET_SECONDS = 15.0


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


async def _collect_turn(adapter, prompt: str) -> ResultMessage:
    await adapter.send_query(prompt)
    async for message in adapter.receive_events():
        if isinstance(message, ResultMessage):
            return message
    raise AssertionError("turn ended without a result")


@pytest.mark.asyncio
async def test_headless_delegated_turn_with_a_permission_prompt_finishes(monkeypatch, tmp_path):
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    options = SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path),
        # What stream_query / start_background_query / execute_query hand the
        # adapter for a profile in the default ("ask") permission mode.
        permission_mode="default",
        env={"FAKE_CLAUDE_SCENARIO": "b"},
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    # connect() directly: no SDK options, so no can_use_tool callback — the
    # exact state of every non-WebSocket spawn path.
    await adapter.connect(options)
    try:
        assert adapter._can_use_tool is None
        try:
            result = await asyncio.wait_for(
                _collect_turn(adapter, "what is the weather"), TURN_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "delegated turn hung: the CLI's can_use_tool request was never "
                "answered because no permission callback is wired on this path"
            )
        assert isinstance(result, ResultMessage)
    finally:
        await adapter.disconnect()


async def _headless_turn_text(monkeypatch, tmp_path, mode: str) -> str:
    from app.core.provider.events import AssistantMessage, TextBlock

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    options = SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path),
        permission_mode=mode,
        env={"FAKE_CLAUDE_SCENARIO": "b"},
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    texts: list = []
    try:
        await adapter.send_query("what is the weather")

        async def _collect() -> None:
            async for message in adapter.receive_events():
                if isinstance(message, AssistantMessage):
                    texts.extend(
                        b.text for b in message.content or [] if isinstance(b, TextBlock)
                    )
                if isinstance(message, ResultMessage):
                    return

        await asyncio.wait_for(_collect(), TURN_BUDGET_SECONDS)
    finally:
        await adapter.disconnect()
    return "".join(texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, reply", [
    ("default", "Understood, skipping that."),
    ("acceptEdits", "Understood, skipping that."),
    ("bypassPermissions", "It is sunny in Paris."),
])
async def test_the_session_mode_decides_a_prompt_with_no_approver(monkeypatch, tmp_path, mode, reply):
    assert reply in await _headless_turn_text(monkeypatch, tmp_path, mode)


def test_an_interactive_turn_does_not_reuse_a_client_without_an_approver():
    from types import SimpleNamespace

    from app.core.query_engine import pooled_client_lacks_approver

    headless = SimpleNamespace(adapter=ha.HarnessAdapter())
    wired = ha.HarnessAdapter()
    wired._can_use_tool = object()
    broadcast = object()

    assert pooled_client_lacks_approver(headless, broadcast)
    assert not pooled_client_lacks_approver(SimpleNamespace(adapter=wired), broadcast)
    # A headless caller reusing it has no approver to lose.
    assert not pooled_client_lacks_approver(headless, None)
