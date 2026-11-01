"""Tools that put their own consent in front of the owner are not gated twice (#527).

In an owner chat, a high-risk call waits for the owner's typed yes
(``background_permissions``). Some tools already ask the owner themselves: the
private access card, a grant the owner approved, the broker's one-shot card,
the screen-access card. Asking a yes/no before those was a second gate for one
decision. ``OWN_CONSENT_TOOLS`` lists them; the gate stands aside when their
own consent covers the call and still asks where the mode skips it.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import background_permissions as ap
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.chat_access import AccessDenied
from app.core.provider import harness_adapter as ha
from app.core.provider.delegated_saved_rules import SAVED_RULE_DENIAL
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.mcp.computer_use import access as cu_access
from tests.core.test_assistant_permissions import ROOT, SCOPE, _spec
from tests.core.test_chat_access import broker, pending, ssh_target  # noqa: F401 — fixtures

CHAT = "mcp__chat-access__"
REQUEST_ACCESS = "mcp__computer-use__request_access"

# Calls whose own card or grant is the owner's consent outside Bypass.
OWN_CONSENT_CALLS = [
    (CHAT + "RequestChatAccess", {"kind": "machine", "purpose": "Check the GPU", "saved_id": "s1"}),
    (CHAT + "ChatListFiles", {"grant_id": "g", "path": ""}),
    (CHAT + "ChatReadFile", {"grant_id": "g", "path": "notes.txt"}),
    (CHAT + "ChatHttpRequest", {"grant_id": "g", "method": "GET", "path": "/zones", "body": "", "content_type": ""}),
    (CHAT + "ChatHttpRequest", {"grant_id": "g", "method": "POST", "path": "/x", "body": "{}", "content_type": ""}),
    (CHAT + "ChatRunCommand", {"grant_id": "g", "command": "docker inspect tdarr"}),
    (CHAT + "ChatWriteFile", {"grant_id": "g", "path": "notes.txt", "content": "x"}),
    (REQUEST_ACCESS, {"apps": ["Notepad"], "reason": "Type the notes"}),
]


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,data", OWN_CONSENT_CALLS)
@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
def test_ask_mode_does_not_ask_before_a_tool_that_asks_itself(name, data, mode):
    assert ap.level(name, data, SCOPE) == ap.HIGH  # the level itself is unchanged
    assert not ap.needs_owner(name, data, SCOPE, permission_mode=mode)
    # Every transport spelling is the same tool.
    assert not ap.needs_owner("mcp__fluid_bundled__" + name, data, SCOPE, permission_mode=mode)


@pytest.mark.parametrize("name,data,asks", [
    # The private card and the grant apply in every mode.
    (CHAT + "RequestChatAccess", {"kind": "machine", "purpose": "x"}, False),
    (CHAT + "ChatReadFile", {"grant_id": "g", "path": "a"}, False),
    (CHAT + "ChatListFiles", {"grant_id": "g", "path": ""}, False),
    (CHAT + "ChatHttpRequest", {"grant_id": "g", "method": "head", "path": "/"}, False),
    # Bypass skips the broker's one-shot card and repeat screen-access cards,
    # so the owner gate still asks for these.
    (CHAT + "ChatRunCommand", {"grant_id": "g", "command": "ls"}, True),
    (CHAT + "ChatWriteFile", {"grant_id": "g", "path": "a", "content": "x"}, True),
    (CHAT + "ChatHttpRequest", {"grant_id": "g", "method": "DELETE", "path": "/x"}, True),
    (REQUEST_ACCESS, {"apps": ["Notepad"]}, True),
])
def test_bypass_keeps_the_gate_where_the_tools_own_card_is_skipped(name, data, asks):
    assert ap.needs_owner(name, data, SCOPE, permission_mode="bypassPermissions") is asks


def test_auto_keeps_the_gate_for_screen_access_only():
    assert ap.needs_owner(REQUEST_ACCESS, {"apps": ["x"]}, SCOPE, permission_mode="auto")
    assert not ap.needs_owner(CHAT + "ChatRunCommand", {"command": "ls"}, SCOPE, permission_mode="auto")


def test_ordinary_tools_still_ask_and_questions_never_did():
    for mode in ("default", "acceptEdits", "auto", "bypassPermissions"):
        assert ap.needs_owner("Bash", {"command": "gh pr merge 3"}, SCOPE, permission_mode=mode)
        assert ap.needs_owner("mcp__browser__browser_act", {"ref": "e1"}, SCOPE, permission_mode=mode)
        assert not ap.needs_owner("AskUserQuestion", {"questions": []}, SCOPE, permission_mode=mode)
    assert not ap.has_own_consent("Bash") and not ap.has_own_consent(CHAT + "ChatResources")


def test_a_failing_rule_keeps_the_owners_yes(monkeypatch):
    def broken(data, mode):
        raise RuntimeError("bad rule")
    monkeypatch.setitem(ap.OWN_CONSENT_TOOLS, CHAT + "ChatRunCommand", broken)
    assert ap.needs_owner(CHAT + "ChatRunCommand", {"command": "ls"}, SCOPE, permission_mode="default")


def test_every_allowlisted_tool_is_a_declared_tool():
    assert all(ap.declared(name) for name in ap.OWN_CONSENT_TOOLS)


# ---------------------------------------------------------------------------
# The native engine and the delegated dispatch
# ---------------------------------------------------------------------------

def test_native_engine_leaves_the_tools_own_consent_alone():
    ask = ha._AssistantPermissionEngine(SCOPE, approval_policy="on_write")
    # The app rates chat-access "safe": the broker is its gate, so nothing asks.
    assert ask.decide(_spec(CHAT + "ChatRunCommand", "safe"), {"command": "ls"}, cwd=ROOT).decision == "allow"
    assert ask.decide(_spec(CHAT + "RequestChatAccess", "safe"), {"kind": "machine"}, cwd=ROOT).decision == "allow"
    # The access request still reaches the approver, which shows its own card.
    assert ask.decide(_spec(REQUEST_ACCESS), {"apps": ["x"]}, cwd=ROOT).decision == "ask"
    assert ask.decide(_spec("Bash"), {"command": "gh pr merge 1"}, cwd=ROOT).decision == "ask"
    bypass = ha._AssistantPermissionEngine(SCOPE, approval_policy="never_ask")
    assert bypass.decide(_spec(CHAT + "ChatReadFile", "safe"), {"path": "a"}, cwd=ROOT).decision == "allow"
    assert bypass.decide(_spec(CHAT + "ChatRunCommand", "safe"), {"command": "ls"}, cwd=ROOT).decision == "ask"


def _adapter(mode, answer=None, *, delegated=True):
    adapter = ha.HarnessAdapter()
    adapter._is_delegated = delegated
    adapter._options = SessionOptions(model="m", permission_mode=mode, extra={"assistant_scope": SCOPE})
    adapter._can_use_tool = AsyncMock(return_value=answer or PermissionResultAllow())
    adapter._saved_rule_decision = AsyncMock(return_value=None)
    return adapter


def _registry(adapter, *names, native=False, handler=None):
    from fluid.tools.registry import ToolOutcome, ToolRegistry
    registry = ToolRegistry()
    ran = []

    async def record(data, ctx):
        ran.append(data)
        return ToolOutcome(text="done")
    for name in names:
        registry.register_builtin(_spec(name, "safe"), handler or record)
    adapter._guard_assistant_dispatch(registry, adapter._options, native=native)
    return registry, ran


async def test_a_granted_machine_runs_several_commands_without_a_yes_no():
    adapter = _adapter("default")
    registry, ran = _registry(adapter, CHAT + "ChatRunCommand")
    for command in ("lscpu", "free -g", "docker inspect tdarr"):
        assert not (await registry.dispatch(CHAT + "ChatRunCommand", {"grant_id": "g", "command": command})).is_error
    assert [r["command"] for r in ran] == ["lscpu", "free -g", "docker inspect tdarr"]
    adapter._can_use_tool.assert_not_awaited()


async def test_bypass_still_asks_the_owner_before_a_command():
    adapter = _adapter("bypassPermissions", PermissionResultDeny(message="Declined by the owner"))
    registry, ran = _registry(adapter, CHAT + "ChatRunCommand", CHAT + "ChatReadFile")
    denied = await registry.dispatch(CHAT + "ChatRunCommand", {"grant_id": "g", "command": "rm -rf x"})
    assert denied.is_error and "Declined" in denied.text and not ran
    assert adapter._can_use_tool.await_args.args[2].assistant_owner is True
    adapter._can_use_tool.reset_mock()
    assert not (await registry.dispatch(CHAT + "ChatReadFile", {"grant_id": "g", "path": "a"})).is_error
    adapter._can_use_tool.assert_not_awaited()


@pytest.mark.parametrize("native", [False, True])
async def test_a_saved_deny_on_an_exempted_tool_still_denies(native):
    adapter = _adapter("default", delegated=not native)
    adapter._saved_rule_decision = AsyncMock(return_value="deny")
    registry, ran = _registry(adapter, CHAT + "RequestChatAccess", "ListSessions", native=native)
    denied = await registry.dispatch(CHAT + "RequestChatAccess", {"kind": "machine", "purpose": "x"})
    assert denied.is_error and denied.text == SAVED_RULE_DENIAL and not ran
    # Only the exempted tools are looked up here; ordinary low tools run as before.
    assert not (await registry.dispatch("ListSessions", {})).is_error and ran
    adapter._can_use_tool.assert_not_awaited()


async def test_a_failed_rule_lookup_denies_an_exempted_tool():
    adapter = _adapter("default")
    adapter._saved_rule_decision = AsyncMock(side_effect=RuntimeError("db down"))
    registry, ran = _registry(adapter, CHAT + "ChatReadFile")
    denied = await registry.dispatch(CHAT + "ChatReadFile", {"grant_id": "g", "path": "a"})
    assert denied.is_error and not ran


async def test_the_bridge_flags_screen_access_for_the_owner_only_in_bypass():
    for mode, flagged in (("default", False), ("bypassPermissions", True)):
        adapter = _adapter(mode, delegated=False)
        adapter.respond_to_permission = AsyncMock()
        await adapter._bridge_permission(PermissionRequestEvent(
            tool_use_id="t1", name=REQUEST_ACCESS, input={"apps": ["Notepad"]}, risk="write"))
        assert adapter._can_use_tool.await_args.args[2].assistant_owner is flagged


# ---------------------------------------------------------------------------
# The tools' own cards still ask the owner
# ---------------------------------------------------------------------------

async def test_request_chat_access_shows_only_its_private_card(broker):  # noqa: F811
    adapter = _adapter("default")

    async def request(data, ctx):
        from fluid.tools.registry import ToolOutcome
        await broker.request("chat", data["kind"], data["purpose"])
        return ToolOutcome(text="granted")
    registry, _ = _registry(adapter, CHAT + "RequestChatAccess", handler=request)
    call = asyncio.create_task(registry.dispatch(CHAT + "RequestChatAccess", {"kind": "machine", "purpose": "x"}))
    async with asyncio.timeout(3):
        while not broker.requests:
            await asyncio.sleep(0)
    adapter._can_use_tool.assert_not_awaited()  # no yes/no before the card
    card = next(iter(broker.requests.values()))
    await broker.decline("chat", card.id)
    assert (await call).is_error  # declining the card refuses the access


async def test_each_command_still_waits_for_the_brokers_own_card(broker, ssh_target):  # noqa: F811
    """Ask mode: the one-shot card is the only consent for each command; an
    expired grant refuses as before."""
    spec, commands, _ = ssh_target
    grant = await broker.create("chat", spec)
    adapter = _adapter("default")

    async def run(data, ctx):
        from fluid.tools.registry import ToolOutcome
        return ToolOutcome(text=str(await broker.run("chat", data["grant_id"], "command", data)))
    registry, _ = _registry(adapter, CHAT + "ChatRunCommand", handler=run)
    for command in ("one", "two"):
        call = asyncio.create_task(registry.dispatch(CHAT + "ChatRunCommand",
                                                     {"grant_id": grant["id"], "command": command}))
        action = await pending(broker)
        assert command not in commands  # nothing runs before the owner's card
        await broker.decide("chat", action.id, True)
        assert "remote:" + command in (await call).text
    assert commands == ["one", "two"]
    adapter._can_use_tool.assert_not_awaited()
    broker.grants[grant["id"]].expires_at = 0
    refused = await registry.dispatch(CHAT + "ChatRunCommand", {"grant_id": grant["id"], "command": "three"})
    assert refused.is_error and commands == ["one", "two"] and not broker.actions
    with pytest.raises(AccessDenied):
        await broker.get("chat", grant["id"])


@pytest.fixture
def _screen(monkeypatch):
    from tests.core.test_computer_use_request_access_callback import SID
    cu_access.revoke(SID)
    monkeypatch.setattr("app.api.computer_use.is_dedicated_desktop", lambda: False)
    yield SID
    cu_access.revoke(SID)


@pytest.mark.parametrize("mode,owner,granted", [
    ("default", False, False), ("default", False, True),
    ("bypassPermissions", True, False), ("bypassPermissions", True, True),
])
async def test_screen_access_is_asked_once_with_its_own_card(tmp_path, _screen, mode, owner, granted):
    from tests.core.test_computer_use_request_access_callback import _callback
    if granted:
        cu_access.grant(_screen, ["Notepad"])
    callback = await _callback(tmp_path, mode)
    prompt = AsyncMock(return_value=PermissionResultAllow())
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback(REQUEST_ACCESS, {"apps": ["Notepad"], "reason": "r"},
                                SimpleNamespace(tool_use_id="t", agent_id=None, assistant_owner=owner))
    assert type(result).__name__ == "PermissionResultAllow"
    prompt.assert_awaited_once()
    kwargs = prompt.await_args.args[0]
    assert kwargs["title"] == "Computer control access"
    assert "Notepad" in kwargs["description"]
    if owner:
        # Past saved rules, as the owner gate always was.
        assert kwargs["skip_saved_rules"] is True
    # The owner's allow on the first request is the first-use confirmation, so
    # the tool does not ask a second time.
    assert cu_access.consume_first_use_confirmation(_screen) is (not granted)


async def test_ordinary_tools_still_get_their_permission_prompt_in_ask_mode(tmp_path, _screen):
    from tests.core.test_computer_use_request_access_callback import _callback
    callback = await _callback(tmp_path, "default")
    prompt = AsyncMock(return_value=PermissionResultDeny(message="no"))
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback("Bash", {"command": "ls"}, SimpleNamespace(tool_use_id="t", agent_id=None))
    assert type(result).__name__ == "PermissionResultDeny"
    prompt.assert_awaited_once()
    assert prompt.await_args.args[0]["tool_name"] == "Bash"


def test_the_screen_access_card_reads_plainly_in_words():
    from app.core import owner_confirm
    assert owner_confirm.describe(REQUEST_ACCESS, {"apps": ["Notepad", "Edge"]}) == (
        "control Notepad, Edge on this computer")


def test_a_safety_check_is_asked_in_plain_words():
    """An owner chat asks a provider safety check as one: never as
    "use ModelGuardrail with {…}" (docs/internal/guardrail-contract.md)."""
    from app.core import owner_confirm
    data ={"guardrail": True, "provider": "anthropic", "provider_label": "Anthropic",
            "category": "reasoning_extraction", "requested_model": "claude-opus-4-8",
            "suggested_model": "claude-opus-4-7"}
    assert owner_confirm.describe("ModelGuardrail", data) == (
        "continue this turn on claude-opus-4-7; Anthropic's safety check blocked "
        "claude-opus-4-8 (requests for its hidden reasoning)")
    # The same plain words as the chat card; an unknown category reads as-is.
    assert owner_confirm.describe("ModelGuardrail", {**data, "category": "content_filter"}).endswith(
        "(content policy)")
    assert owner_confirm.describe("ModelGuardrail", {**data, "category": "self-harm"}).endswith(
        "(self harm)")
    assert owner_confirm.describe("ModelGuardrail", {"guardrail": True}) == (
        "continue this turn on another model; the provider's safety check blocked the model")
    question = owner_confirm.question("ModelGuardrail", data)
    assert "ModelGuardrail" not in question and "{" not in question
