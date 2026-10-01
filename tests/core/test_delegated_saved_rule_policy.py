"""Saved Deny rules on the delegated leg's PreToolUse policy.

The claude CLI approves some calls without asking ``can_use_tool`` (in-cwd
edits under Accept Edits, classifier approvals under Auto, tools granted by
``--allowed-tools``), so no approver ever sees them. PreToolUse sees every
call: the adapter composes the chat-lane policy (``_delegated_tool_policy``)
with a saved-rule lookup, so a call the user saved as Deny is refused there,
watched or not.

The lookup reads the DB, so the composed policy is async and is only passed
when fluid's DelegatedLoop says it awaits one (``supports_async_tool_policy``).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from fluid.store.base import MemoryStore

import app.core.provider.harness_adapter as ha
from app.core.provider.types import SessionOptions

APP_SID = "app-saved-rule-policy"


@pytest.fixture
def saved_rules():
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


@pytest.fixture
def async_policies(monkeypatch):
    monkeypatch.setattr(ha.DelegatedLoop, "supports_async_tool_policy", True, raising=False)


def _adapter(mode: str) -> ha.HarnessAdapter:
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(
        model="opus", permission_mode=mode,
        extra={"app_session_id": APP_SID, "profile_id": "p-none"},
    )
    return adapter


def _no_denial(name: str, tool_input: dict) -> None:
    return None


def _lookup_must_not_run(monkeypatch) -> None:
    from app.core.permission_handler import permission_handler

    def boom(*args: Any, **kwargs: Any):
        raise AssertionError("the saved-rule lookup ran")

    monkeypatch.setattr(permission_handler, "saved_rule", boom)


async def _decide(policy, name: str = "Edit", tool_input: Any = None):
    result = policy(name, tool_input or {"file_path": "src/app.py"})
    return await result if inspect.isawaitable(result) else result


@pytest.mark.asyncio
async def test_a_saved_deny_refuses_an_edit_the_cli_approves_itself(
    mock_db, saved_rules, async_policies,
):
    saved_rules("Edit", "deny")
    policy = _adapter("acceptEdits")._delegated_policy_with_saved_rules(_no_denial)
    assert await _decide(policy) == "Denied by saved rule"


@pytest.mark.asyncio
async def test_no_saved_rule_leaves_the_call_to_the_cli(mock_db, saved_rules, async_policies):
    policy = _adapter("acceptEdits")._delegated_policy_with_saved_rules(_no_denial)
    assert await _decide(policy) is None


@pytest.mark.asyncio
async def test_a_saved_allow_grants_nothing_here(mock_db, saved_rules, async_policies):
    saved_rules("Edit", "allow")
    policy = _adapter("default")._delegated_policy_with_saved_rules(_no_denial)
    assert await _decide(policy) is None


@pytest.mark.asyncio
async def test_the_inner_denial_wins(monkeypatch, async_policies):
    _lookup_must_not_run(monkeypatch)

    def inner(name: str, tool_input: dict) -> str:
        return f"Tool '{name}' is disabled in this profile"

    policy = _adapter("auto")._delegated_policy_with_saved_rules(inner)
    assert await _decide(policy) == "Tool 'Edit' is disabled in this profile"


@pytest.mark.asyncio
async def test_bypass_skips_the_lookup_at_call_time(monkeypatch, async_policies):
    _lookup_must_not_run(monkeypatch)
    adapter = _adapter("acceptEdits")
    policy = adapter._delegated_policy_with_saved_rules(_no_denial)
    # The live mode is read when the call happens, not when the policy is built.
    adapter._options.permission_mode = "bypassPermissions"
    assert await _decide(policy) is None


@pytest.mark.asyncio
async def test_a_failed_lookup_denies(monkeypatch, async_policies):
    from app.core.permission_handler import permission_handler

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(permission_handler, "saved_rule", boom)
    policy = _adapter("auto")._delegated_policy_with_saved_rules(_no_denial)
    denial = await _decide(policy, "Bash", {"command": "ls"})
    assert denial and "saved permission rules could not be read" in denial


def test_an_older_fluid_gets_the_sync_policy_unchanged(monkeypatch):
    monkeypatch.delattr(ha.DelegatedLoop, "supports_async_tool_policy", raising=False)
    assert _adapter("auto")._delegated_policy_with_saved_rules(_no_denial) is _no_denial


# ─── what connect hands DelegatedLoop ────────────────────────────────────────


class _CapturingLoop:
    captured: dict = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured = dict(kwargs)
        self.store = kwargs["store"]
        self._session_id = kwargs.get("session_id")

    async def _ensure_session(self):
        return await self.store.create_session(self._session_id)

    async def aclose(self) -> None:
        pass


async def _connect_policy(monkeypatch, tmp_path, *, supports: bool):
    loop_cls = type("Loop", (_CapturingLoop,), {})
    if supports:
        loop_cls.supports_async_tool_policy = True
    monkeypatch.setattr(ha, "DelegatedLoop", loop_cls)
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore()))
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude-not-spawned"])
    adapter = ha.HarnessAdapter()
    await adapter.connect(SessionOptions(
        model="opus", cwd=str(tmp_path), permission_mode="acceptEdits",
        extra={"app_session_id": APP_SID},
    ))
    return loop_cls.captured["tool_policy"]


@pytest.mark.asyncio
async def test_connect_passes_the_async_policy_when_fluid_awaits_it(monkeypatch, tmp_path):
    policy = await _connect_policy(monkeypatch, tmp_path, supports=True)
    assert asyncio.iscoroutinefunction(policy)


@pytest.mark.asyncio
async def test_connect_passes_the_sync_policy_to_an_older_fluid(monkeypatch, tmp_path):
    policy = await _connect_policy(monkeypatch, tmp_path, supports=False)
    assert callable(policy) and not asyncio.iscoroutinefunction(policy)
    assert policy("Edit", {"file_path": "x"}) is None
