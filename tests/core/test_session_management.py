"""Exercise session tools through their native and delegated producers."""
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fluid.tools.registry import ToolCtx
from fluid.tools.builtins.session_orchestration import SPECS

from app.core import session_management as management
from app.core import session_orchestration as orch
from app.db import database, sessions, turns
from app.db.base import get_db


@pytest.fixture
def chats(mock_db, monkeypatch):
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    for sid in ("caller", "target"):
        sessions.create_session(sid, "agent", title=sid, is_active=True)
    return ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: "caller")})


async def invoke(ctx, name, **data):
    _, handler = next((spec, handler) for spec, handler in SPECS if spec.name == name)
    return await handler(data, ctx)


async def test_native_round_trip_keeps_work_and_transcript(chats, monkeypatch):
    """Settling never touches work; archiving stops it (#1822: an archived chat
    is read-only, so nothing may be left running to wake it)."""
    from app.core import turn_scheduler
    from app.core.sync_engine import sync_engine
    kick = Mock()
    monkeypatch.setattr(turn_scheduler, "kick", kick)
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    turn = turns.enqueue_turn("target", "user", prompt="Keep this conversation", profile_id="agent")
    assert turns.start_turn(turn["id"])
    before = turns.get_turn(turn["id"])
    for name, active, archived in (("SettleSession", False, False), ("SettleSession", False, False),
                                    ("UnsettleSession", True, False), ("UnsettleSession", True, False),
                                    ("ArchiveSession", False, True), ("ArchiveSession", False, True)):
        outcome = await invoke(chats, name, session_id="target")
        assert not outcome.is_error, outcome.text
        receipt = json.loads(outcome.text)
        row = sessions.get_session("target")
        assert bool(row["is_active"]) is active
        assert (row["status"] == "archived") is archived
        assert receipt["is_active"] is active
        assert receipt["status"] == row["status"]
        if not archived:
            assert turns.get_turn(turn["id"]) == before
        assert broadcast.await_args.kwargs["updates"] == {"is_active": active, "status": row["status"]}
    # The archive stopped the running turn the way the Stop button does.
    assert turns.get_turn(turn["id"])["state"] in turns.TERMINAL_STATES
    assert "stopped" in receipt["note"]
    kick.assert_not_called()
    for name in ("SettleSession", "UnsettleSession"):
        outcome = await invoke(chats, name, session_id="target")
        assert outcome.is_error and "Restore" in outcome.text
        assert sessions.get_session("target")["status"] == "archived"


async def test_settle_self_then_unsettle_self(chats):
    for name in ("SettleSession", "UnsettleSession"):
        result = await invoke(chats, name, session_id="caller")
        assert not result.is_error, result.text
    assert sessions.get_session("caller")["is_active"]


async def test_list_producer_finds_settled_and_archived(chats):
    await invoke(chats, "SettleSession", session_id="target")
    assert orch.list_sessions("caller", {})["sessions"] == []
    listing = orch.list_sessions("caller", {"state": "settled"})
    assert [r["id"] for r in listing["sessions"]] == ["target"]
    assert not listing["sessions"][0]["is_active"]
    assert not (await invoke(chats, "UnsettleSession", session_id=listing["sessions"][0]["id"])).is_error
    assert [r["id"] for r in orch.list_sessions("caller", {})["sessions"]] == ["target"]
    await invoke(chats, "ArchiveSession", session_id="target")
    assert orch.list_sessions("caller", {"state": "settled"})["sessions"] == []
    for state in ("archived", "all"):
        assert [r["id"] for r in orch.list_sessions("caller", {"state": state})["sessions"]] == ["target"]
    assert (await invoke(chats, "ListSessions", state="invalid")).is_error


@pytest.mark.parametrize("field,value", [("hidden", True), ("is_worker", True),
    ("parent_session_id", "caller"), ("mode", "voice"), ("instance_id", "foreign")])
@pytest.mark.parametrize("name", ["SettleSession", "UnsettleSession", "ArchiveSession"])
async def test_ineligible_targets_never_mutate(chats, field, value, name):
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE sessions SET {field}=%s WHERE id=%s", (value, "target"))
    result = await invoke(chats, name, session_id="target")
    assert result.is_error
    assert sessions.get_session("target")["is_active"]
    assert orch.list_sessions("caller", {"state": "all"})["sessions"] == []


@pytest.mark.parametrize("name", ["SettleSession", "UnsettleSession", "ArchiveSession"])
async def test_owner_and_revocation_checks(chats, monkeypatch, name):
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": sid})
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", lambda p: None)
    result = await invoke(chats, name, session_id="target")
    assert result.is_error and "accessible" in result.text
    assert orch.list_sessions("caller", {"state": "all"})["sessions"] == []
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": "same", "sid": sid})
    def revoked(principal):
        if principal["sid"] == "target":
            raise PermissionError("revoked")
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", revoked)
    result = await invoke(chats, name, session_id="target")
    assert result.is_error and "revoked" in result.text
    assert sessions.get_session("target")["is_active"]


async def test_api_user_scope(chats):
    database.create_api_user("other", "Other", "hash")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id=%s WHERE id=%s", ("other", "target"))
    assert (await invoke(chats, "ArchiveSession", session_id="target")).is_error
    assert orch.list_sessions("caller", {"state": "all"})["sessions"] == []


@pytest.mark.parametrize("sid", [None, "", " ", "missing", 123, "x" * 201])
async def test_invalid_ids(chats, sid):
    assert (await invoke(chats, "SettleSession", session_id=sid)).is_error


async def test_stopped_target_organization_does_not_resume_it(chats, monkeypatch):
    from app.core.task_registry import task_registry
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "target")
    for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
        result = await invoke(chats, name, session_id="target")
        assert not result.is_error, result.text
        assert task_registry.is_user_stopped("target")
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "caller")
    assert (await invoke(chats, "ArchiveSession", session_id="target")).is_error


async def test_assistant_home_is_protected_by_mutation_guard(chats, monkeypatch):
    from app.core import assistant_service as service
    monkeypatch.setattr(service, "_guard_version", 0)
    service.configure({"agent_id": "agent", "enabled": True})
    home = service.open_home()
    ctx = ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: home["id"])})
    # Drive the real home producer, then try to settle/archive that caller.
    for name in ("SettleSession", "ArchiveSession"):
        result = await invoke(ctx, name, session_id=home["id"])
        assert result.is_error and "persistent" in result.text
        assert sessions.get_session(home["id"])["is_active"]


async def test_mcp_registration_executes_same_tools(chats, monkeypatch):
    from app.mcp.builtin_tools import create_builtin_tools_server, get_builtin_tool_names
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP, NATIVE_SHADOWED_BUILTINS
    import app.mcp.inproc as inproc
    captured = {}
    real_create = inproc.create_tool_server
    def capture(**kwargs):
        captured.update({item.name: item for item in kwargs["tools"]})
        return real_create(**kwargs)
    monkeypatch.setattr(inproc, "create_tool_server", capture)
    create_builtin_tools_server(session_id="caller", profile_id="agent")
    for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
        assert BUILTINS_RISK_MAP[name] == "write"
        assert name in NATIVE_SHADOWED_BUILTINS
        assert f"mcp__builtins__{name}" in get_builtin_tool_names()
        result = await captured[name].handler({"session_id": "target"})
        assert not result.get("isError"), result
    assert sessions.get_session("target")["status"] == "archived"


async def test_host_capability_required():
    for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
        spec = next(spec for spec, _ in SPECS if spec.name == name)
        assert spec.risk == "write" and spec.requires_extra == ("session_orchestration",)
        assert (await invoke(ToolCtx(), name, session_id="target")).is_error


@pytest.mark.parametrize("name", ["SettleSession", "UnsettleSession", "ArchiveSession"])
async def test_code_mode_write_permission_gate(chats, name):
    from fluid.codemode.bridge import HostBridge
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.registry import ToolRegistry
    from fluid.tools.builtins import register_builtins
    registry = ToolRegistry()
    register_builtins(registry)
    bridge = HostBridge(registry, PermissionEngine("on_write"), ctx=chats)
    assert (await bridge.on_host_call(name, {"session_id": "target"})).denied
    assert sessions.get_session("target")["is_active"]
    assert sessions.get_session("target")["status"] != "archived"
    allowed = HostBridge(registry, PermissionEngine("never_ask"), ctx=chats)
    assert not (await allowed.on_host_call(name, {"session_id": "target"})).denied
    row = sessions.get_session("target")
    assert bool(row["is_active"]) is (name == "UnsettleSession")
    assert (row["status"] == "archived") is (name == "ArchiveSession")


@pytest.mark.parametrize("field,value", [("is_worker", True), ("hidden", True),
    ("parent_session_id", "target"), ("status", "archived")])
async def test_ineligible_callers_cannot_manage_chats(chats, field, value):
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE sessions SET {field}=%s WHERE id=%s", (value, "caller"))
    result = await invoke(chats, "ArchiveSession", session_id="target")
    assert result.is_error
    assert sessions.get_session("target")["is_active"]


async def test_missing_writer_result_is_not_success(chats, monkeypatch):
    monkeypatch.setattr(database, "set_session_active", lambda *args: None)
    result = await invoke(chats, "SettleSession", session_id="target")
    assert result.is_error and "Failed to update" in result.text



# ---------------------------------------------------------------------------
# Bulk forms: session_ids on all three tools, all_settled on ArchiveSession.
# ---------------------------------------------------------------------------
TOOLS = {"settle": "SettleSession", "unsettle": "UnsettleSession", "archive": "ArchiveSession"}


def _state(sid):
    row = sessions.get_session(sid)
    return bool(row["is_active"]), row["status"]


def _set(sid, **fields):
    with get_db() as conn:
        for field, value in fields.items():
            conn.cursor().execute(f"UPDATE sessions SET {field}=%s WHERE id=%s", (value, sid))


async def bulk(ctx, name, **data):
    outcome = await invoke(ctx, name, **data)
    assert not outcome.is_error, outcome.text
    return json.loads(outcome.text)


async def test_single_id_keeps_original_receipt(chats):
    receipt = await bulk(chats, "ArchiveSession", session_id="target")
    assert set(receipt) == {"session_id", "is_active", "status", "action", "note"}
    assert receipt["status"] == "archived" and _state("target") == (False, "archived")


@pytest.mark.parametrize("action", ["settle", "unsettle", "archive"])
async def test_ids_mix_processes_each_item_independently(chats, monkeypatch, action):
    from app.core import assistant_service as service
    database.create_api_user("other", "Other", "hash")
    for sid in ("ok1", "ok2", "sub", "voice", "worker", "foreign", "mobile"):
        sessions.create_session(sid, "agent", title=sid, is_active=action != "unsettle")
    _set("sub", parent_session_id="caller")
    _set("voice", mode="voice")
    _set("worker", is_worker=True)
    _set("foreign", api_user_id="other")
    # A chat bound to another mobile owner, without revocation.
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": "phone"} if sid == "mobile" else None)
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", lambda principal: None)
    monkeypatch.setattr(service, "_guard_version", 0)
    service.configure({"agent_id": "agent", "enabled": True})
    home = service.open_home()["id"]
    if action == "unsettle":
        _set(home, is_active=False)  # legacy row; the tool must still refuse to touch it
    before = {sid: _state(sid) for sid in ("sub", "voice", "worker", "foreign", "mobile", home)}
    ids = ["ok1", "sub", "voice", "worker", "foreign", "mobile", home, "missing", "ok2", " ok1 "]
    result = await bulk(chats, TOOLS[action], session_ids=ids)
    assert result["requested"] == 9 and result["duplicates_ignored"] == 1
    assert result[management.DONE[action]] == 2
    assert result["already_in_state"] == 0 and result["failed"] == 0
    refused = {issue["session_id"]: issue["reason"] for issue in result["issues"]}
    assert result["refused"] == len(refused)
    expected = {"sub", "voice", "worker", "foreign", "mobile", "missing"} | ({home} if action != "unsettle" else set())
    assert expected <= set(refused) <= expected | {home}
    assert all(issue["outcome"] == "refused" for issue in result["issues"])
    for sid in ("sub", "voice", "worker", "foreign", "mobile", "missing"):
        assert refused[sid] == management.INACCESSIBLE  # unknown and foreign look the same
    want = {"settle": (False, "active"), "unsettle": (True, "active"), "archive": (False, "archived")}[action]
    assert _state("ok1") == _state("ok2") == want
    assert {sid: _state(sid) for sid in before} == before
    # Idempotent re-run: nothing changes, the same chats are refused.
    again = await bulk(chats, TOOLS[action], session_ids=ids)
    assert again[management.DONE[action]] == 0 and again["already_in_state"] == 2
    assert {i["session_id"] for i in again["issues"]} == set(refused)
    assert _state("ok1") == want


async def test_ids_settle_refuses_archived_and_counts_already(chats):
    sessions.create_session("gone", "agent", title="gone", is_active=True)
    await bulk(chats, "ArchiveSession", session_ids=["gone"])
    await bulk(chats, "SettleSession", session_id="target")
    result = await bulk(chats, "SettleSession", session_ids=["gone", "target", "caller"])
    assert (result["settled"], result["already_in_state"], result["refused"]) == (1, 1, 1)
    assert result["issues"] == [{"session_id": "gone", "outcome": "refused", "reason": management.ARCHIVED}]
    assert _state("caller") == (False, "active")  # explicit self is allowed, as with session_id


async def test_ids_use_one_set_based_update(chats, monkeypatch):
    for sid in ("a", "b", "c"):
        sessions.create_session(sid, "agent", title=sid, is_active=True)
    calls = []
    real = sessions.set_sessions_active
    monkeypatch.setattr(sessions, "set_sessions_active", lambda *a: calls.append(a) or real(*a))
    monkeypatch.setattr(database, "set_session_active", Mock(side_effect=AssertionError("per-row write")))
    result = await bulk(chats, "ArchiveSession", session_ids=["a", "b", "c"])
    assert result["archived"] == 3 and len(calls) == 1 and calls[0][0] == ["a", "b", "c"]


async def test_batch_failure_falls_back_and_one_failure_does_not_abort(chats, monkeypatch):
    from app.network import session_active
    for sid in ("a", "b", "c"):
        sessions.create_session(sid, "agent", title=sid, is_active=True)
    monkeypatch.setattr(sessions, "set_sessions_active", Mock(side_effect=RuntimeError("batch broke")))
    real = session_active.update_membership

    async def flaky(row, active, archived=None):
        if row["id"] == "b":
            raise session_active.MembershipUpdateFailed("Failed to update active status")
        return await real(row, active, archived)
    monkeypatch.setattr(session_active, "update_membership", flaky)
    result = await bulk(chats, "ArchiveSession", session_ids=["a", "b", "c"])
    assert (result["archived"], result["failed"], result["refused"]) == (2, 1, 0)
    assert result["issues"] == [{"session_id": "b", "outcome": "failed", "reason": "Failed to update active status"}]
    assert _state("a")[1] == _state("c")[1] == "archived" and _state("b") == (True, "active")


async def test_all_settled_project_then_all_scope_never_touches_caller(chats):
    for project in ("p1", "p2"):
        database.create_project(project, project, None, f"/tmp/{project}")
    for sid, project in (("here", "p1"), ("there", "p2"), ("open", "p1")):
        sessions.create_session(sid, "agent", title=sid, is_active=True)
        _set(sid, project_id=project)
    _set("caller", project_id="p1")
    _set("target", project_id="p1")
    for sid in ("here", "there", "caller"):
        await bulk(chats, "SettleSession", session_id=sid)
    result = await bulk(chats, "ArchiveSession", all_settled=True)
    assert (result["matched"], result["archived"], result["scope"], result["remaining"]) == (1, 1, "project", False)
    assert _state("here") == (False, "archived")
    assert _state("there") == (False, "active") and _state("open") == (True, "active")
    assert _state("caller") == (False, "active")  # settled, but the current chat is never selected
    result = await bulk(chats, "ArchiveSession", all_settled=True, scope="all")
    assert (result["matched"], result["archived"]) == (1, 1) and _state("there") == (False, "archived")
    again = await bulk(chats, "ArchiveSession", all_settled=True, scope="all")
    assert (again["matched"], again["archived"], again["issues"]) == (0, 0, [])
    assert _state("caller") == (False, "active") and _state("target") == (True, "active")


async def test_all_settled_skips_inaccessible_rows_silently(chats):
    database.create_api_user("other", "Other", "hash")
    for sid in ("mine", "theirs", "sub"):
        sessions.create_session(sid, "agent", title=sid, is_active=False)
    _set("theirs", api_user_id="other")
    _set("sub", parent_session_id="caller")
    result = await bulk(chats, "ArchiveSession", all_settled=True)
    assert (result["matched"], result["archived"], result["issues"]) == (1, 1, [])
    assert _state("theirs") == _state("sub") == (False, "active")


async def test_all_settled_is_capped_and_resumable(chats, monkeypatch):
    monkeypatch.setattr(management, "MAX_SESSIONS_PER_CALL", 2)
    for sid in ("s1", "s2", "s3"):
        sessions.create_session(sid, "agent", title=sid, is_active=False)
    first = await bulk(chats, "ArchiveSession", all_settled=True)
    assert (first["archived"], first["remaining"]) == (2, True) and "repeat" in first["note"]
    second = await bulk(chats, "ArchiveSession", all_settled=True)
    assert (second["archived"], second["remaining"]) == (1, False)
    assert all(_state(sid)[1] == "archived" for sid in ("s1", "s2", "s3"))


async def test_bound_and_bounded_output(chats):
    limit = management.MAX_SESSIONS_PER_CALL
    assert limit == 500
    over = await invoke(chats, "ArchiveSession", session_ids=[f"x{i}" for i in range(limit + 1)])
    assert over.is_error and "1 to 500" in over.text
    # The raw list is bounded before deduplication.
    assert (await invoke(chats, "ArchiveSession", session_ids=["target"] * (limit + 1))).is_error
    assert _state("target") == (True, "active")
    result = await bulk(chats, "ArchiveSession", session_ids=[f"x{i}" for i in range(limit)])
    assert result["refused"] == limit and len(result["issues"]) == management.MAX_LISTED_ISSUES
    assert result["issues_omitted"] == limit - management.MAX_LISTED_ISSUES
    assert len(json.dumps(result)) < 10_000


@pytest.mark.parametrize("name,data", [
    ("ArchiveSession", {}),
    ("ArchiveSession", {"session_id": "target", "session_ids": ["target"]}),
    ("ArchiveSession", {"session_ids": ["target"], "all_settled": True}),
    ("ArchiveSession", {"all_settled": False}),
    ("ArchiveSession", {"session_ids": ["target"], "scope": "all"}),
    ("ArchiveSession", {"all_settled": True, "scope": "everywhere"}),
    ("ArchiveSession", {"session_ids": []}),
    ("ArchiveSession", {"session_ids": "target"}),
    ("ArchiveSession", {"session_ids": ["target", 123]}),
    ("ArchiveSession", {"session_ids": ["target", " "]}),
    ("SettleSession", {"all_settled": True}),
    ("UnsettleSession", {"session_id": "target", "extra": 1}),
])
async def test_selector_must_be_unambiguous(chats, name, data):
    assert (await invoke(chats, name, **data)).is_error
    assert _state("target") == (True, "active")


def test_bulk_schemas():
    specs = {spec.name: spec for spec, _ in SPECS}
    for name in TOOLS.values():
        props = specs[name].input_schema["properties"]
        assert "required" not in specs[name].input_schema  # exactly-one is enforced by the host
        assert props["session_ids"]["maxItems"] == management.MAX_SESSIONS_PER_CALL
        assert ("all_settled" in props) is (name == "ArchiveSession")
        assert "session_ids" in specs[name].description
    assert "all_settled=true" in specs["ArchiveSession"].description
    assert "session_ids" in specs["ListSessions"].description


async def test_mcp_bridge_accepts_session_ids(chats, monkeypatch):
    from app.mcp import inproc
    from app.mcp.builtin_tools import create_builtin_tools_server
    captured = {}
    real_create = inproc.create_tool_server

    def capture(**kwargs):
        captured.update({item.name: item for item in kwargs["tools"]})
        return real_create(**kwargs)
    monkeypatch.setattr(inproc, "create_tool_server", capture)
    create_builtin_tools_server(session_id="caller", profile_id="agent")
    result = await captured["SettleSession"].handler({"session_ids": ["target", "missing"]})
    assert not result.get("isError"), result
    summary = json.loads(result["content"][0]["text"])
    assert (summary["settled"], summary["refused"]) == (1, 1)
    assert _state("target") == (False, "active")

async def _settle_prompt_via(chats, monkeypatch, via):
    if via == "ui":
        from starlette.requests import Request
        from app.api import sessions as api
        await api.set_session_active(Request({"type": "http", "headers": []}), "target",
                                     api.SessionActiveUpdate(is_active=False), token="admin")
    elif via == "mcp":
        from app.mcp import inproc
        from app.mcp.builtin_tools import create_builtin_tools_server
        captured = {}
        real_create = inproc.create_tool_server

        def capture(**kwargs):
            captured.update({item.name: item for item in kwargs["tools"]})
            return real_create(**kwargs)
        monkeypatch.setattr(inproc, "create_tool_server", capture)
        create_builtin_tools_server(session_id="caller", profile_id="agent")
        result = await captured["SettleSession"].handler({"session_id": "target"})
        assert not result.get("isError"), result
    else:
        selector = {"session_ids": ["target"]} if via == "bulk" else {"session_id": "target"}
        result = await invoke(chats, "SettleSession", **selector)
        assert not result.is_error, result.text


@pytest.mark.parametrize("via", ["ui", "native", "bulk", "mcp"])
@pytest.mark.parametrize("kind", ["permission", "question", "private", "prose"])
async def test_settle_dismisses_owner_wait_producers(chats, monkeypatch, via, kind):
    import asyncio
    from app.core import chat_access, interaction_events, query_session_lifecycle
    from app.core.global_events import global_events
    from app.core.permission_handler import permission_handler
    from app.core.sync_engine import sync_engine
    from app.core.user_question_handler import user_question_handler
    from app.db import interactions, session_events

    frames = AsyncMock()
    lifecycle = Mock()
    stop_all = AsyncMock(side_effect=AssertionError("Settle must not sweep background work"))
    monkeypatch.setattr(sync_engine, "_broadcast_user_message", frames)
    monkeypatch.setattr(global_events, "lifecycle", lifecycle)
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", stop_all)
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, "worker", None)))
    turn = turns.enqueue_turn("target", "user", prompt="Run", profile_id="agent")
    turns.start_turn(turn["id"])
    waiter = None
    if kind == "permission":
        registered = asyncio.Event()
        emit = interaction_events.emit_interaction_requested

        async def requested(*args, **kwargs):
            await emit(*args, **kwargs)
            registered.set()
        monkeypatch.setattr(interaction_events, "emit_interaction_requested", requested)
        waiter = asyncio.create_task(permission_handler.request_permission(
            "request", "target", "agent", "Bash", {"command": "true"}, AsyncMock()))
        await asyncio.wait_for(registered.wait(), 5)
    elif kind == "question":
        await user_question_handler.post_question(request_id="request", tool_use_id="tool",
            session_id="target", questions=[{"question": "Proceed?", "header": "Confirm"}],
            broadcast_func=AsyncMock())
        turns.park_turn(turn["id"])
    elif kind == "private":
        await broker.request("target", "service", "Read a service")
    else:
        session_events.append_events("target", [{"type": "message.assistant", "payload": {"content": "Proceed?"}}])
        turns.park_turn(turn["id"])
        assert sessions.get_session("target")["last_assistant_question_seq"] is not None
    pending = interactions.get_pending_interactions("target")
    try:
        await _settle_prompt_via(chats, monkeypatch, via)
        assert not sessions.get_session("target")["is_active"]
        assert not interactions.get_pending_interactions("target")
        assert not broker.pending_count("target")
        stop_all.assert_not_called()
        sent = [c.args[1] if len(c.args) > 1 else c.kwargs["message"] for c in frames.call_args_list]
        assert any(f["type"] == "session_attention" and f["count"] == 0 for f in sent)
        if kind == "permission":
            assert (await asyncio.wait_for(waiter, 5)).behavior == "deny"
            assert interactions.get_interaction(pending[0]["id"])["answer"]["decision"] == "deny"
            assert any(f["type"] == "permission_queue_update" and f["resolved_ids"] == ["request"]
                       and f["remaining_count"] == 0 for f in sent)
            assert turns.get_turn(turn["id"])["state"] == "aborted"
        elif kind == "question":
            assert interactions.get_interaction(pending[0]["id"])["state"] == "cancelled"
            assert turns.get_turn(turn["id"])["state"] == "completed"
            assert any(f["type"] == "user_question_timeout" and f["request_id"] == "request" for f in sent)
        elif kind == "private":
            assert not broker.requests
            assert any(f["type"] == "chat_access_changed" and f["pending"] == 0 for f in sent)
        else:
            assert turns.get_turn(turn["id"])["state"] == "parked"
            assert sessions.get_session("target")["last_assistant_question_seq"] is None
            assert any(c.args[1].get("updates", {}).get("last_assistant_question_seq", "missing") is None
                       for c in lifecycle.call_args_list)
            # Later non-prose activity must not resurrect the dismissed question.
            session_events.append_events("target", [{"type": "system.note", "payload": {"subtype": "background_tasks_changed"}}])
            assert sessions.get_session("target")["last_assistant_question_seq"] is None
        assert not (await invoke(chats, "UnsettleSession", session_id="target")).is_error
        assert sessions.get_session("target")["is_active"]
        # Reopen is fresh: a genuinely NEW prose question still needs the owner.
        session_events.append_events("target", [{"type": "message.assistant", "payload": {"content": "Try again?"}}])
        assert sessions.get_session("target")["last_assistant_question_seq"] is not None
    finally:
        if waiter and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        broker.dismiss_pending("target")
        await asyncio.gather(*broker._notifications)

async def test_settle_prompt_keeps_background_agent_and_monitor(chats, monkeypatch):
    from app.core.task_registry import task_registry
    from app.core.user_question_handler import user_question_handler
    from app.core import query_session_lifecycle

    stop_all = AsyncMock(side_effect=AssertionError("No background sweep"))
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", stop_all)
    turn = turns.enqueue_turn("target", "user", prompt="Run", profile_id="agent")
    turns.start_turn(turn["id"])
    for task_id, kind in (("shell", "shell"), ("child", "agent")):
        task_registry.start_task("target", task_id, kind, task_id)
    turns.park_turn(turn["id"], ["shell", "child"])
    await user_question_handler.post_question(request_id="request", tool_use_id="tool",
        session_id="target", questions=[{"question": "Proceed?"}], broadcast_func=AsyncMock())
    before = task_registry.get_parking_background_work("target")
    assert before
    assert not (await invoke(chats, "SettleSession", session_id="target")).is_error
    assert task_registry.get_parking_background_work("target") == before
    assert turns.get_turn(turn["id"])["state"] == "parked"
    assert not task_registry.is_user_stopped("target")
    stop_all.assert_not_called()


async def test_settle_answered_prose_is_idle_noop(chats, monkeypatch):
    from app.core import turn_manager
    from app.db import session_events
    session_events.append_events("target", [
        {"type": "message.assistant", "payload": {"content": "Proceed?"}},
        {"type": "message.user", "payload": {"content": "Yes"}},
    ])
    before = session_events.get_max_seq("target")
    attention_keys = ("last_assistant_question_seq", "last_assistant_question_epoch",
                      "last_user_message_seq", "last_user_message_epoch")
    before_attention = {key: sessions.get_session("target")[key] for key in attention_keys}
    interrupt = AsyncMock()
    monkeypatch.setattr(turn_manager, "interrupt_async", interrupt)
    assert not (await invoke(chats, "SettleSession", session_id="target")).is_error
    # Persist the idempotency receipt, but do not clear attention or interrupt.
    assert session_events.get_max_seq("target") == before + 1
    receipt = session_events.get_latest_system_note("target", "session_settled")
    assert receipt["empty_snapshot"] is True
    assert receipt["through_seq"] == 0
    assert {key: sessions.get_session("target")[key] for key in attention_keys} == before_attention
    interrupt.assert_not_called()


async def test_settle_denies_private_action_without_revoking_access(chats, monkeypatch):
    import asyncio
    import time
    from app.core import chat_access

    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_bypasses_confirmation", AsyncMock(return_value=False))
    grant = chat_access.Grant("grant", "target", (None, "worker", None), "Service", "service",
                              "example", time.time() + 60)
    broker.grants[grant.id] = grant
    registered = asyncio.Event()
    monkeypatch.setattr(broker, "_notify", lambda *args, **kwargs: registered.set())
    waiter = asyncio.create_task(broker.approve(grant, "http", {"method": "POST"}))
    await asyncio.wait_for(registered.wait(), 5)
    assert not (await invoke(chats, "SettleSession", session_id="target")).is_error
    with pytest.raises(chat_access.AccessDenied, match="denied"):
        await asyncio.wait_for(waiter, 5)
    assert not broker.pending_count("target")
    assert broker.grants[grant.id] is grant and not grant.revoked

@pytest.mark.parametrize("kind", ["permission", "private"])
async def test_settle_redirected_wait_interrupts_only_waiting_child(chats, monkeypatch, kind):
    import asyncio
    from app.core import chat_access
    from app.db import interactions

    sessions.create_session("child-wait", "agent", parent_session_id="target")
    root_turn = turns.enqueue_turn("target", "user", prompt="Keep working", profile_id="agent")
    child_turn = turns.enqueue_turn("child-wait", "user", prompt="Ask", profile_id="agent")
    turns.start_turn(root_turn["id"])
    turns.start_turn(child_turn["id"])
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, "worker", None)))
    if kind == "permission":
        interactions.create_interaction("target", "permission", {
            "request_id": "child-request", "tool_name": "Bash", "tool_input": {},
            "provider_agent": {"worker_session_id": "child-wait"},
        }, 300)
    else:
        await broker.request("child-wait", "service", "Read service")
        broker._main["child-wait"] = "target"
    try:
        assert not (await invoke(chats, "SettleSession", session_id="target")).is_error
        assert turns.get_turn(root_turn["id"])["state"] == "running"
        assert turns.get_turn(child_turn["id"])["state"] == "aborted"
        assert not broker.pending_count("target")
    finally:
        broker.dismiss_pending("target")
        await asyncio.gather(*broker._notifications)

async def test_settle_snapshot_preserves_successor_and_all_new_prompts(chats, monkeypatch):
    import asyncio
    from app.core import chat_access, session_settlement, turn_manager
    from app.db import interactions, runs, session_events

    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, "worker", None)))
    old = turns.enqueue_turn("target", "user", prompt="Old", profile_id="agent")
    lease = runs.claim_run("target", "runner", 60)
    turns.start_turn(old["id"], lease["id"], "runner")
    old_permission = interactions.create_interaction("target", "permission", {"request_id": "same-id"}, 300)
    old_question = interactions.create_interaction("target", "question", {"request_id": "same-question"}, 300)
    await broker.request("target", "service", "Old access")
    session_events.append_events("target", [{"type": "message.assistant", "payload": {"content": "Old question?"}}])
    real_run = session_settlement.run_sync
    successor = {}
    signals = Mock()
    monkeypatch.setattr(turn_manager, "_fire_interrupt_executor", signals)

    async def interleave(fn, *args, **kwargs):
        value = await real_run(fn, *args, **kwargs)
        if fn is session_settlement._capture_and_dismiss:
            assert value is not None
            assert turns.get_turn(old["id"])["state"] == "interrupting"
            # Old runtime exits, owner reopens, then fresh work arrives before
            # any post-commit notifications/private releases/runtime signal.
            turns.settle_turn(old["id"], "completed")
            sessions.set_session_active("target", True)
            new_turn = turns.enqueue_turn("target", "user", prompt="Fresh", profile_id="agent")
            turns.start_turn(new_turn["id"], lease["id"], "runner")
            successor["turn"] = new_turn
            successor["permission"] = interactions.create_interaction("target", "permission", {"request_id": "same-id"}, 300)
            successor["question"] = interactions.create_interaction("target", "question", {"request_id": "same-question"}, 300)
            broker.dismiss_pending("target", snapshot=value["private"])
            successor["private"] = await broker.request("target", "service", "Fresh access")
            session_events.append_events("target", [{"type": "message.assistant", "payload": {"content": "Fresh question?"}}])
        return value

    monkeypatch.setattr(session_settlement, "run_sync", interleave)
    try:
        assert not (await invoke(chats, "SettleSession", session_id="target")).is_error
        assert turns.get_turn(successor["turn"]["id"])["state"] == "running"
        signals.assert_called_once_with("target", "stop", old["id"])
        assert interactions.get_interaction(old_permission["id"])["answer"]["decision"] == "deny"
        assert interactions.get_interaction(old_question["id"])["state"] == "cancelled"
        assert interactions.get_interaction(successor["permission"]["id"])["state"] == "pending"
        assert interactions.get_interaction(successor["question"]["id"])["state"] == "pending"
        assert successor["private"]["request_id"] in broker.requests
        assert broker.pending_count("target") == 1
        assert sessions.get_session("target")["is_active"]
        assert sessions.get_session("target")["last_assistant_question_seq"] is not None
    finally:
        broker.dismiss_pending("target")
        await asyncio.gather(*broker._notifications)


async def test_settlement_runtime_interrupt_does_not_sweep_new_permissions(chats, monkeypatch):
    from app.core import session_runtime, query_session_lifecycle
    from app.db import interactions

    turn = turns.enqueue_turn("target", "user", prompt="Old", profile_id="agent")
    turns.start_turn(turn["id"])
    turns.interrupt_turn("target", "stop", turn_id=turn["id"], source="session_settled")
    fresh = interactions.create_interaction("target", "permission", {"request_id": "fresh"}, 300)

    async def signal(_sid):
        turns.settle_turn(turn["id"], "aborted")
    monkeypatch.setattr(query_session_lifecycle, "interrupt_session", signal)
    monkeypatch.setattr(session_runtime, "record_interrupting", AsyncMock())
    await session_runtime.SessionRuntime(turn).interrupt(escalation_timeout=0.02)
    assert interactions.get_interaction(fresh["id"])["state"] == "pending"


async def test_local_delayed_settlement_revision_cannot_dismiss_a_new_settle(chats):
    from app.db import interactions
    from app.network import session_active

    stale = session_active.write_shared("target", False)
    session_active.write_shared("target", True)
    session_active.write_shared("target", False)
    fresh = interactions.create_interaction("target", "permission", {"request_id": "fresh"}, 300)
    await session_active._apply_settle_change(stale, False)
    assert interactions.get_interaction(fresh["id"])["state"] == "pending"

@pytest.mark.parametrize("initial_wait", [True, False])
async def test_repeated_settle_revision_preserves_post_snapshot_wait(chats, monkeypatch, initial_wait):
    from app.core import user_question_handler
    from app.core.session_settlement import dismiss_owner_waits
    from app.db import interactions
    from app.network import session_active

    turn = turns.enqueue_turn("target", "user", prompt="Streaming", profile_id="agent")
    turns.start_turn(turn["id"])
    attention = AsyncMock()
    monkeypatch.setattr(user_question_handler, "publish_session_attention", attention)
    if initial_wait:
        interactions.create_interaction("target", "permission", {"request_id": "old"}, 300)
    row = session_active.write_shared("target", False)
    revision = row["_settled_revision"]
    assert await dismiss_owner_waits("target", expected_revision=revision) is initial_wait
    if not initial_wait:
        assert turns.get_turn(turn["id"])["state"] == "running"
        attention.assert_not_called()
    turn_after_first_delivery = turns.get_turn(turn["id"])
    fresh = interactions.create_interaction("target", "permission", {"request_id": "fresh"}, 300)
    assert not await dismiss_owner_waits("target", expected_revision=revision)
    assert interactions.get_interaction(fresh["id"])["state"] == "pending"
    assert turns.get_turn(turn["id"]) == turn_after_first_delivery
    assert attention.await_count == int(initial_wait)
