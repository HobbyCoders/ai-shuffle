"""Remember scope capabilities and runner-local lifetime, through real prompts."""
import asyncio

import pytest

from app.core import permission_handler as module
from app.core.permission_handler import PermissionHandler
from app.db import database


async def start_prompt(
    handler: PermissionHandler, tool: str = "Bash", provider_agent: dict | None = None,
    *, profile_id: str | None = "default", request_id: str = "scope-request",
    skip_saved_rules: bool = False, safety_check: bool = False,
) -> tuple[asyncio.Task, dict]:
    ready = asyncio.Event()
    frames: list[dict] = []

    async def broadcast(frame: dict) -> None:
        frames.append(frame)
        ready.set()

    task = asyncio.create_task(handler.request_permission(
        request_id, "scope-session", profile_id, tool, {"command": "echo ok"},
        broadcast, provider_agent=provider_agent,
        skip_saved_rules=skip_saved_rules, safety_check=safety_check,
    ))
    await asyncio.wait_for(ready.wait(), 15)
    return task, frames[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("remember", [None, "none", "session", "profile"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_scopes_through_producing_path(remember: str | None, decision: str) -> None:
    handler = PermissionHandler()
    task, frame = await start_prompt(handler)
    try:
        # Ordinary payloads and live WS frames keep their exact legacy shapes.
        assert "remember_scopes" not in frame
        assert handler.get_pending_requests("scope-session")[0]["remember_scopes"] == (
            ["none", "session", "profile"]
        )
        result = await handler.respond("scope-request", "scope-session", decision, remember=remember)
        assert result["resolved"]
        assert (await task).behavior == decision
        assert len(handler.get_session_rules("scope-session")) == (remember == "session")
        assert len(database.get_permission_rules(profile_id="default")) == (remember == "profile")
        rule = handler._check_rules("scope-session", "default", "Bash", {"command": "different"})
        assert (rule is not None) == (remember in ("session", "profile"))
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("remember", ["session", "profile"])
async def test_peer_scope_advice_does_not_reject_remembered_answers(remember: str) -> None:
    runner, other = PermissionHandler(), PermissionHandler()
    task, _ = await start_prompt(runner)
    try:
        assert other.get_pending_requests("scope-session")[0]["remember_scopes"] == ["none"]
        result = await other.respond(
            "scope-request", "scope-session", "allow", remember=remember,
        )
        assert result["resolved"]
        assert (await task).behavior == "allow"
        assert len(runner.get_session_rules("scope-session")) == (remember == "session")
        assert len(database.get_permission_rules(profile_id="default")) == (remember == "profile")
        rule = runner._check_rules("scope-session", "default", "Bash", {"command": "next"})
        assert rule is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,tag", [
    ("ModelSwitch", None),
    ("Bash", {"remote_approval_id": "approval", "worker_session_id": "child"}),
])
async def test_model_switch_and_remote_proxy_are_one_shot(tool: str, tag: dict | None) -> None:
    handler = PermissionHandler()
    task, frame = await start_prompt(handler, tool, tag)
    try:
        assert "remember_scopes" not in frame
        assert handler.get_pending_requests("scope-session")[0]["remember_scopes"] == ["none"]
        assert (await handler.respond(
            "scope-request", "scope-session", "allow", remember="profile",
        ))["resolved"]
        assert (await task).behavior == "allow"
        assert not handler.get_session_rules("scope-session")
        assert not database.get_permission_rules(profile_id="default")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unpatterned_session_hint_replayed_and_cleared_on_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = PermissionHandler()
    task, _ = await start_prompt(handler)
    try:
        # A shared-row answer carries no input pattern. Replay on the actual runner.
        from app.db import interactions
        row = interactions.get_pending_interactions("scope-session")[0]
        interactions.answer_interaction(row["id"], {
            "decision": "allow", "remember": "session", "pattern": None,
        })
        from app.core import interaction_wait
        interaction_wait.notify_changed("scope-session")
        await task
        assert len(handler.get_session_rules("scope-session")) == 1
        # Repeated turns on the same pooled runner retain it; a new process does not.
        assert handler._check_rules("scope-session", "default", "Bash", {}) is not None
        assert not PermissionHandler().get_session_rules("scope-session")
        monkeypatch.setattr(module, "permission_handler", handler)
        from app.api import ws_cleanup
        monkeypatch.setattr(ws_cleanup, "permission_handler", handler)
        await ws_cleanup.cleanup_session_handlers("scope-session")
        assert not handler.get_session_rules("scope-session")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_invalid_scope_or_decision_leaves_request_pending() -> None:
    handler = PermissionHandler()
    task, _ = await start_prompt(handler)
    try:
        assert await handler.respond("scope-request", "scope-session", "bad") == {
            "error": "Invalid permission decision",
        }
        assert await handler.respond(
            "scope-request", "scope-session", "allow", remember="bad",
        ) == {"error": "Invalid remember scope"}
        assert handler.get_queue_count("scope-session") == 1
        await handler.respond("scope-request", "scope-session", "allow")
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_turn_clears_session_remembrance() -> None:
    from contextlib import ExitStack
    from unittest.mock import patch

    import app.core.query_background as background
    from app.db import sessions as sessions_db
    from tests.core.engine_harness import StubPool
    from tests.core.test_query_background_recording import (
        _bg_patches,
        _fake_state,
        _run,
    )

    sessions_db.create_session("scope-session", "default")
    handler = PermissionHandler()
    task, _ = await start_prompt(handler)
    try:
        await handler.respond("scope-request", "scope-session", "allow", remember="session")
        await task
        assert handler.get_session_rules("scope-session")
        with ExitStack() as stack:
            _bg_patches(stack, StubPool(_fake_state()))
            stack.enter_context(patch.object(background, "permission_handler", handler))
            await _run("scope-session")
        assert not handler.get_session_rules("scope-session")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_remembered_local_answer_cannot_batch_answer_remote_proxy() -> None:
    from app.db import interactions

    handler = PermissionHandler()
    task, _ = await start_prompt(handler)
    try:
        proxy_payload = handler._build_permission_payload(
            "remote-request", "default", "Bash", {"command": "echo remote"},
            None, None, None, None, None, None, None,
            provider_agent={"remote_approval_id": "remote", "worker_session_id": "child"},
        )
        proxy = interactions.create_interaction("scope-session", "permission", proxy_payload, 300)
        await handler.respond("scope-request", "scope-session", "allow", remember="session")
        await task
        pending = interactions.get_pending_interactions("scope-session")
        assert [r["id"] for r in pending] == [proxy["id"]]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip,safety", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("remember", ["session", "profile"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_safety_answer_never_saves_or_resolves_siblings(
    skip: bool, safety: bool, remember: str, decision: str,
) -> None:
    from app.db import interactions

    handler = PermissionHandler()
    task, _ = await start_prompt(handler, skip_saved_rules=skip, safety_check=safety)
    try:
        row = interactions.get_pending_interactions("scope-session")[0]
        assert bool(row["payload"].get("skip_saved_rules")) == skip
        assert bool(row["payload"].get("safety_check")) == safety
        assert handler.get_pending_requests("scope-session")[0]["remember_scopes"] == ["none"]
        sibling_payload = handler._build_permission_payload(
            "sibling", "default", "Bash", {"command": "echo sibling"},
            None, None, None, None, None, None, None,
        )
        sibling = interactions.create_interaction("scope-session", "permission", sibling_payload, 300)
        result = await handler.respond(
            "scope-request", "scope-session", decision, remember=remember,
        )
        assert result == {"resolved": True, "auto_resolved_count": 0, "auto_resolved_ids": []}
        assert (await task).behavior == decision
        assert not handler.get_session_rules("scope-session")
        assert not database.get_permission_rules(profile_id="default")
        assert [r["id"] for r in interactions.get_pending_interactions("scope-session")] == (
            [sibling["id"]]
        )
        assert "remember" not in interactions.get_interaction(row["id"])["answer"]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip,safety", [(True, False), (False, True), (True, True)])
async def test_safety_sibling_never_batch_resolved(skip: bool, safety: bool) -> None:
    handler = PermissionHandler()
    ordinary, _ = await start_prompt(handler)
    guarded, _ = await start_prompt(
        handler, request_id="guarded", skip_saved_rules=skip, safety_check=safety,
    )
    try:
        result = await handler.respond("scope-request", "scope-session", "allow", remember="session")
        assert result["auto_resolved_ids"] == []
        await ordinary
        assert not guarded.done()
        pending = handler.get_pending_requests("scope-session")
        assert [r["request_id"] for r in pending] == ["guarded"]
        assert pending[0]["remember_scopes"] == ["none"]
        await handler.respond("guarded", "scope-session", "deny", remember="profile")
        assert (await guarded).behavior == "deny"
        assert len(handler.get_session_rules("scope-session")) == 1
        assert not database.get_permission_rules(profile_id="default")
    finally:
        for task in (ordinary, guarded):
            if not task.done():
                task.cancel()
        await asyncio.gather(ordinary, guarded, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", [None, ""])
async def test_profile_remember_without_agent_leaves_prompt_answerable(
    profile_id: str | None,
) -> None:
    handler = PermissionHandler()
    task, _ = await start_prompt(handler, profile_id=profile_id)
    try:
        scopes = handler.get_pending_requests("scope-session")[0]["remember_scopes"]
        assert scopes == ["none", "session"]
        result = await handler.respond(
            "scope-request", "scope-session", "allow", remember="profile",
        )
        assert result == {"error": "Remember scope unavailable", "remember_scopes": scopes}
        assert handler.get_queue_count("scope-session") == 1
        assert not task.done()
        assert not handler.get_session_rules("scope-session")
        assert not database.get_permission_rules()
        await handler.respond("scope-request", "scope-session", "allow", remember="session")
        assert (await task).behavior == "allow"
        assert len(handler.get_session_rules("scope-session")) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip,safety", [(True, False), (False, True), (True, True)])
async def test_safety_runner_ignores_legacy_shared_session_hint(skip: bool, safety: bool) -> None:
    from app.core import interaction_wait
    from app.db import interactions

    handler = PermissionHandler()
    task, _ = await start_prompt(handler, skip_saved_rules=skip, safety_check=safety)
    try:
        row = interactions.get_pending_interactions("scope-session")[0]
        interactions.answer_interaction(row["id"], {
            "decision": "allow", "remember": "session", "pattern": None,
        })
        interaction_wait.notify_changed("scope-session")
        assert (await task).behavior == "allow"
        assert not handler.get_session_rules("scope-session")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip,safety", [(True, False), (False, True), (True, True)])
async def test_safety_prompt_bypasses_preexisting_saved_rules(skip: bool, safety: bool) -> None:
    handler = PermissionHandler()
    handler._add_session_rule_idempotent("scope-session", "Bash", None, "allow")
    task, _ = await start_prompt(handler, skip_saved_rules=skip, safety_check=safety)
    try:
        assert handler.get_queue_count("scope-session") == 1
        await handler.respond("scope-request", "scope-session", "deny", remember="session")
        assert (await task).behavior == "deny"
        rules = handler.get_session_rules("scope-session")
        assert len(rules) == 1
        assert rules[0]["decision"] == "allow"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
