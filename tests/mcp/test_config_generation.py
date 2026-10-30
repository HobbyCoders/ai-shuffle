"""Conversational picks use the same store/binding as the card and scheduled runs."""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core import generation_choices as choices
from app.core import preference_identity, query_options
from app.db import database
from app.mcp.app_config_sdk import collect_app_config_tools, get_app_config_tool_names
from app.mcp.config_generation import _create_generation_tools

OWNER = ("account", "origin#owner")
PICK = {
    "source": "fal",
    "account": "fal-ai",
    "worker": "worker-a",
    "model": "fixture-model",
    "options": {"size": "square"},
}


@pytest.fixture(autouse=True)
def setup(mock_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "owner")
    monkeypatch.setattr(
        preference_identity, "account_identity", lambda account: ("account", "origin#" + account)
    )


async def call(args: dict, *, admin: bool = True, writable: bool = True) -> dict:
    tool = _create_generation_tools(admin, writable=writable)[0]
    return json.loads((await tool.handler(args))["content"][0]["text"])


def context(job_id: str | None = None) -> Any:
    return query_options._generation_context(
        {"image_generation": True},
        {"id": "session", "automation_id": job_id},
        None,
        session_id="session",
        unattended=True,
    )


@pytest.mark.asyncio
async def test_unattended_cannot_save_its_own_choice_even_with_confirmation() -> None:
    result = await call(
        {"action": "remember", "kind": "image", "choice": PICK, "confirm": True},
        writable=False,
    )
    assert result["success"] is False
    assert choices.load(OWNER, run_migration=False)["remembered"] == {}
    # Catalog/voice callers that omit the trusted flag default to read-only.
    tool = next(t for t in collect_app_config_tools(True) if t.name == "generationPreferences")
    result = json.loads(
        (
            await tool.handler(
                {"action": "remember", "kind": "image", "choice": PICK, "confirm": True}
            )
        )["content"][0]["text"]
    )
    assert result["success"] is False


@pytest.mark.parametrize("action", ["get", "remember", "forget", "other", None])
def test_permission_level_requires_host_approval_for_writes(action: str | None) -> None:
    from app.core import background_permissions as permissions

    name = "mcp__app-config__generationPreferences"
    assert permissions.declared(name)
    expected = permissions.LOW if action == "get" else permissions.HIGH
    # confirm=true is only the tool's acknowledgment flag, not a host-permission bypass.
    assert permissions.level(name, {"action": action, "confirm": True}, {}) == expected


def test_tool_is_registered_and_advertised() -> None:
    assert "mcp__app-config__generationPreferences" in get_app_config_tool_names()
    assert "generationPreferences" in {tool.name for tool in collect_app_config_tools(True)}


@pytest.mark.asyncio
async def test_conversational_writer_binds_production_context_and_forget() -> None:
    result = await call({"action": "remember", "kind": "image", "choice": PICK, "confirm": True})
    assert result["success"] is True
    assert result["choice"]["options"] == PICK["options"]
    # Saved by the conversational tool, not from the choice card.
    assert result["choice"]["origin"] == "conversation"
    assert choices.load(OWNER, run_migration=False)["remembered"]["image"]["origin"] == "conversation"
    assert context().remembered_for("image")["model"] == PICK["model"]
    assert (await call({"action": "get", "kind": "image"}))["choices"]["image"] == result["choice"]
    assert choices.load(("api_user", "other"))["remembered"] == {}
    assert (await call({"action": "forget", "kind": "image", "confirm": True}))["success"]
    assert context().remembered_for("image") is None
    # Forgetting does not erase the optional "Same as last time" suggestion.
    assert context().last_for("image")["model"] == PICK["model"]


@pytest.mark.asyncio
async def test_job_writer_is_bound_only_to_existing_automation() -> None:
    from app.core.automation_service import create_automation

    automation = await create_automation(
        name="Fixture",
        trigger_type="interval",
        trigger_config={"seconds": 300},
        profile_id="default",
        prompt="hi",
    )
    job_id = automation["id"]
    assert job_id.startswith("auto-")
    assert not automation["is_enabled"]
    assert (await call({"action": "get", "job_id": job_id}))["choices"] == {}
    args = {
        "action": "remember",
        "job_id": job_id,
        "kind": "image",
        "choice": PICK,
        "confirm": True,
    }
    assert (await call(args))["success"] is True
    assert context(job_id).job_choices == {"image": PICK}
    assert context().job_choices is None
    assert context().remembered_for("image") is None
    assert (await call({"action": "get", "job_id": job_id}))["choices"] == {"image": PICK}
    # A missing choice is NOT the save_job(None) deletion sentinel.
    del args["choice"]
    assert (await call(args))["success"] is False
    assert context(job_id).job_choices == {"image": PICK}
    assert (await call({"action": "forget", "job_id": job_id, "kind": "image", "confirm": True}))[
        "success"
    ]
    assert context(job_id).job_choices == {}


@pytest.mark.parametrize("action", ["get", "remember", "forget"])
@pytest.mark.parametrize("job_id", ["auto-ffffffff", "../auto-12345678", ""])
@pytest.mark.asyncio
async def test_job_actions_reject_missing_or_unsafe_automations(action: str, job_id: str) -> None:
    result = await call(
        {"action": action, "job_id": job_id, "kind": "image", "choice": PICK, "confirm": True}
    )
    assert result["success"] is False
    assert choices.load(OWNER, run_migration=False)["remembered"] == {}


@pytest.mark.parametrize(
    "args",
    [
        {"action": "remember", "kind": "image", "choice": PICK},
        {"action": "forget", "kind": "image", "confirm": "true"},
        {"action": "remember", "kind": "unknown", "choice": PICK, "confirm": True},
        {"action": "remember", "kind": "image", "choice": {}, "confirm": True},
        {"action": "get", "job_id": "not-a-uuid"},
        {"action": "other"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_changes_never_write(args: dict) -> None:
    assert (await call(args))["success"] is False
    assert choices.load(OWNER, run_migration=False)["remembered"] == {}


@pytest.mark.asyncio
async def test_non_admin_never_resolves_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(principal: Any) -> None:
        raise AssertionError("must not resolve owner")

    monkeypatch.setattr(preference_identity, "principal_preference_identity", denied)
    assert (await call({"action": "get"}, admin=False))["success"] is False
    assert (await call({"action": "forget", "kind": "image", "confirm": True}, admin=False))[
        "success"
    ] is False


@pytest.mark.asyncio
async def test_read_does_not_migrate_and_identity_errors_are_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.set_system_setting("image_model", "legacy-model")
    assert (await call({"action": "get"}))["choices"] == {}
    assert database.get_user_preference(*OWNER, choices.MIGRATED_KEY) is None

    def fail(principal: Any) -> None:
        raise RuntimeError("private-connection-detail")

    monkeypatch.setattr(preference_identity, "principal_preference_identity", fail)
    result = await call({"action": "get"})
    assert result["success"] is False
    assert "private-connection-detail" not in json.dumps(result)


@pytest.mark.asyncio
async def test_saved_pick_still_needs_daily_cap_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.generation_resolver import Backend, Candidate, Resolver

    await call({"action": "remember", "kind": "image", "choice": PICK, "confirm": True})
    price = {
        "currency": "USD",
        "unit": "item",
        "amount": "0.01",
        "total": "0.01",
        "status": "estimated",
    }
    candidate = Candidate(
        source="fal",
        account="fal-ai",
        worker="worker-a",
        model="fixture-model",
        kind="image",
        verified=True,
        options=PICK["options"],
        price=price,
    )
    # Optional account-following hooks must not be invented by an unspecced mock.
    backend = AsyncMock(spec=Backend)
    backend.catalog.return_value = [candidate]
    backend.prepare.return_value = candidate
    backend.reserve.return_value = None
    output = await Resolver(backend).generate("image", {"prompt": "a tree"}, context())
    assert output["reason"] == "daily_cap"
    backend.reserve.assert_awaited_once()
    backend.execute.assert_not_awaited()


def session_context(*, unattended: bool) -> Any:
    return query_options._generation_context(
        {"image_generation": True}, {"id": "session"}, None,
        session_id="session", unattended=unattended,
    )


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.asyncio
async def test_pick_saved_then_forgotten_applies_to_the_next_turn_of_the_same_session(
    monkeypatch: pytest.MonkeyPatch, unattended: bool
) -> None:
    from app.mcp import ai_tools_sdk as sdk
    from app.services import generation_sources
    from tests.core.test_generation_resolver import FakeBackend, candidate

    saved = candidate(account=PICK["account"], model=PICK["model"])
    backend = FakeBackend([candidate(), saved])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    # ONE tool server for the whole session, bound before any pick existed:
    # the Claude CLI keeps its MCP servers across turns.
    tools = {t.name: t for t in sdk._create_unified_tools(None, session_context(unattended=unattended))}

    async def next_turn() -> dict:
        response = await tools["generateImage"].handler({"prompt": "a tree"})
        return json.loads(response["content"][0]["text"])

    first = await next_turn()
    assert first["status"] == ("skipped" if unattended else "choose")

    assert (await call({"action": "remember", "kind": "image", "choice": PICK, "confirm": True}))["success"]
    used = await next_turn()
    assert used["status"] == "completed" and used["remembered"] is True
    assert used["choice"]["model"] == PICK["model"]
    assert ("execute", PICK["account"]) in backend.calls

    assert (await call({"action": "forget", "kind": "image", "confirm": True}))["success"]
    backend.calls.clear()
    after = await next_turn()
    # The forgotten pick is never used or charged again.
    if unattended:
        assert after["status"] == "skipped" and after["reason"] == "no_stored_choice"
    else:
        assert after["status"] == "choose"
    assert not any(call[0] in ("reserve", "execute") for call in backend.calls)


@pytest.mark.asyncio
async def test_unreadable_choices_on_a_later_turn_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp import ai_tools_sdk as sdk
    from app.services import generation_sources
    from tests.core.test_generation_resolver import FakeBackend, candidate

    await call({"action": "remember", "kind": "image", "choice": PICK, "confirm": True})
    backend = FakeBackend([candidate(account=PICK["account"], model=PICK["model"])])
    monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
    tools = {t.name: t for t in sdk._create_unified_tools(None, session_context(unattended=True))}

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store offline")

    # The pick was readable when the session began, not on this turn.
    monkeypatch.setattr(choices, "load", fail)
    output = json.loads((await tools["generateImage"].handler({"prompt": "a tree"}))["content"][0]["text"])
    assert output["status"] == "skipped" and output["reason"] == "choices_unreadable"
    assert backend.calls == []


@pytest.mark.asyncio
async def test_native_alias_reads_the_current_pick_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid.tools.registry import ToolCtx

    from app.core.provider import native_generation_aliases as aliases
    from tests.core.test_generation_resolver import FakeBackend, candidate

    plan = {"source": "chatgpt", "account": "plan-a", "worker": "worker-a",
            "model": "default", "options": {}}
    backend = FakeBackend([candidate(source="chatgpt", account="plan-a", model="default"),
                           candidate(source="chatgpt", account="plan-b", model="default")])
    monkeypatch.setattr(aliases, "Sources", lambda *args, **kw: backend)
    _, handler = aliases.make_alias("image", session_context(unattended=False))

    async def next_turn() -> dict:
        return json.loads((await handler({"prompt": "a tree"}, ToolCtx())).text)

    assert (await next_turn())["status"] == "choose"
    assert (await call({"action": "remember", "kind": "image", "choice": plan, "confirm": True}))["success"]
    assert (await next_turn())["choice"]["account"] == "plan-a"
    assert (await call({"action": "forget", "kind": "image", "confirm": True}))["success"]
    assert (await next_turn())["status"] == "choose"
