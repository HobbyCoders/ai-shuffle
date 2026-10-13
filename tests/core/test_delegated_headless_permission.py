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


# ─── saved rules on a prompt with no approver ────────────────────────────────
#
# The watched approver applies saved rules before the mode (a remembered Deny
# beats Auto and Accept Edits, a remembered Allow skips the Ask prompt). A
# headless delegated prompt must decide the same way.

APP_SID = "app-headless-saved-rules"


@pytest.fixture
def saved_rules():
    """Install session rules for APP_SID in the real permission handler."""
    from app.core.permission_handler import (
        PermissionDecision,
        PermissionRule,
        permission_handler,
    )

    def install(tool_name: str, decision: str) -> None:
        permission_handler._session_rules.setdefault(APP_SID, []).append(PermissionRule(
            id=f"rule-{tool_name}-{decision}", session_id=APP_SID, profile_id=None,
            tool_name=tool_name, tool_pattern=None,
            decision=PermissionDecision(decision),
        ))

    yield install
    permission_handler.clear_session_rules(APP_SID)


def _headless_adapter(mode: str):
    from app.core.provider.types import PermissionRequestEvent

    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(
        model="opus", permission_mode=mode,
        extra={"app_session_id": APP_SID, "profile_id": "p-none"},
    )
    adapter._is_delegated = True
    answers: list = []

    async def respond(tool_use_id, allow, *, message=None, **_kw):
        answers.append((tool_use_id, allow, message))

    adapter.respond_to_permission = respond
    event = PermissionRequestEvent(
        tool_use_id="tu-1", name="Bash", input={"command": "rm -rf build"},
    )
    return adapter, event, answers


@pytest.mark.asyncio
async def test_auto_with_a_saved_deny_denies_by_saved_rule(mock_db, saved_rules):
    saved_rules("Bash", "deny")
    adapter, event, answers = _headless_adapter("auto")
    await adapter._answer_without_approver(event)
    assert answers == [("tu-1", False, "Denied by saved rule")]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
async def test_a_saved_allow_allows_where_the_mode_would_ask(mock_db, saved_rules, mode):
    saved_rules("Bash", "allow")
    adapter, event, answers = _headless_adapter(mode)
    await adapter._answer_without_approver(event)
    assert answers == [("tu-1", True, None)]


@pytest.mark.asyncio
async def test_without_a_saved_rule_the_mode_still_decides(mock_db, saved_rules):
    adapter, event, answers = _headless_adapter("default")
    await adapter._answer_without_approver(event)
    assert answers == [("tu-1", False, ha._NO_APPROVER_DENIAL)]


@pytest.mark.asyncio
async def test_a_failed_rule_lookup_denies(monkeypatch):
    from app.core.permission_handler import permission_handler

    def boom(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(permission_handler, "saved_rule", boom)
    adapter, event, answers = _headless_adapter("auto")
    await adapter._answer_without_approver(event)
    assert len(answers) == 1
    tool_use_id, allow, message = answers[0]
    assert (tool_use_id, allow) == ("tu-1", False)
    assert "saved permission rules could not be read" in message


@pytest.mark.asyncio
async def test_bypass_skips_the_rule_lookup(monkeypatch):
    from app.core.permission_handler import permission_handler

    def boom(*args, **kwargs):
        raise AssertionError("bypass must not look up saved rules")

    monkeypatch.setattr(permission_handler, "saved_rule", boom)
    adapter, event, answers = _headless_adapter("bypassPermissions")
    await adapter._answer_without_approver(event)
    assert answers == [("tu-1", True, None)]
