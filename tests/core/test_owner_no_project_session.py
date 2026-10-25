"""#641: the owner's own no-project chat is a normal session in the workspace's no-project folder.

Each caller type is built through the real option producer
(``query_options.build_options_from_profile``) and, where it matters, the
harness adapter that turns those options into the runtime's session options.
"""
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import AsyncMock

import pytest

from app.core import session_orchestration as orch
from app.core.session_modes import (
    OWNER_CHAT_DIRNAME,
    WATCH_SESSION_PREFIX,
    chat_uploads_dir,
    owner_chat_dir,
    owner_workspace_chat,
    runs_in_owner_chat_folder,
    runtime_session_mode,
)
from app.core.tool_access import tool_permitted
from app.db import database
from tests.core.test_session_orchestration import chat

CODE_TOOLS = ("Bash", "Monitor", "Edit", "Write", "run_python")
PREVIEW = {"owner": "install-admin", "kind": "admin", "principal_id": "preview"}


@pytest.fixture
def workspace(mock_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from app.core import config, query_options

    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    return tmp_path / "workspace"


def _build(session_id: str, **kwargs: Any) -> Any:
    from app.core import query_options

    options, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), session_id=session_id, **kwargs,
    )
    return options


def _has(options: Any, tool: str) -> bool:
    return tool_permitted(tool, options.aih_tool_allowlist, options.disallowed_tools)


@pytest.mark.parametrize("permission", ["default", "auto", "acceptEdits", "bypassPermissions"])
async def test_owner_chat_is_a_normal_session_in_its_own_folder(
    workspace: Path, permission: str,
) -> None:
    from app.core.provider.harness_adapter import HarnessAdapter, _chat_mode_rules

    row, _ = chat("owner-chat", mode="chat")
    database.set_session_run_binding(row["id"], model="sonnet", permission_mode=permission)
    assert owner_workspace_chat(database.get_session(row["id"]))
    options = _build(row["id"])

    # Its own folder, not the uploads folder; uploads stay readable as an
    # added directory (the delegated CLI's in-directory read approval).
    assert options.cwd == str(owner_chat_dir())
    assert Path(options.cwd) == workspace.resolve() / OWNER_CHAT_DIRNAME
    assert Path(options.cwd).is_dir()
    assert str(chat_uploads_dir()) in options.add_dirs
    assert Path(chat_uploads_dir()).is_dir()
    assert options.aih_session_mode == "code"
    assert not options.aih_owner_coordinator
    # No coordinator owner gate: the permission mode governs, as in a project chat.
    assert options.aih_assistant_scope is None
    assert options.permission_mode == permission
    for tool in CODE_TOOLS:
        assert _has(options, tool), tool
    prompt = str(options.system_prompt)
    assert "# No project" in prompt
    assert "# No-project coordination" not in prompt
    assert "cannot run a worker shell" not in prompt

    adapter = HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    session_options = adapter.connect.await_args.args[0]
    assert session_options.extra["session_mode"] == "code"
    assert "assistant_scope" not in session_options.extra
    assert _chat_mode_rules(session_options) is None
    assert session_options.cwd == str(owner_chat_dir())
    assert str(chat_uploads_dir()) in (session_options.add_dirs or [])


@pytest.mark.parametrize("caller", ["api-key", "non-owner", "preview", "watch"])
def test_other_no_project_chats_keep_the_ceiling(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caller: str,
) -> None:
    sid = {"watch": WATCH_SESSION_PREFIX + "event-1"}.get(caller, caller)
    fields = {"api_user_id": "key-user"} if caller == "api-key" else {}
    row, _ = chat(sid, mode="chat", **fields)
    principal = {"non-owner": {"owner": "account:someone-else", "kind": "paired",
                               "principal_id": "phone", "account_id": "someone-else"},
                 "preview": PREVIEW}.get(caller)
    monkeypatch.setattr(orch, "_principal", lambda _sid: principal)

    session = database.get_session(sid)
    assert not owner_workspace_chat(session)
    assert runtime_session_mode(session) == "chat"
    options = _build(sid, **({"api_user_id": "key-user"} if caller == "api-key" else {}))
    assert options.aih_session_mode == "chat"
    # Unchanged: the restricted lane still runs in the uploads folder, with no
    # added directory widening the delegated CLI's read approval.
    assert options.cwd == str(chat_uploads_dir())
    assert not getattr(options, "add_dirs", None)
    for tool in CODE_TOOLS:
        assert not _has(options, tool), tool
    # The owner's ceiling chats still coordinate; the others get the basic surface.
    owner_kind = caller in ("preview", "watch")
    assert bool(options.aih_owner_coordinator) is owner_kind
    assert _has(options, "StartSession") is owner_kind


def test_unattended_automation_keeps_its_own_policy(workspace: Path) -> None:
    from app.db import automations

    automations.create_automation("auto-1", "Nightly", "cron", {}, "agent")
    row, _ = chat("automation-run", automation_id="auto-1")
    session = database.get_session(row["id"])
    # Unchanged by #641: not the owner's chat, not moved into either app folder.
    assert not owner_workspace_chat(session)
    assert not runs_in_owner_chat_folder(session)
    assert runtime_session_mode(session) == "code"
    assert _build(row["id"]).cwd not in (str(chat_uploads_dir()), str(owner_chat_dir()))


def test_watch_alert_summary_stays_tool_free() -> None:
    from app.core.generation_options import build_generation_options

    # The options the watch manager's summarize() builds for an alert.
    options = build_generation_options(
        {"model": "haiku", "thinking_mode": "disabled", "effort": "low"}, max_turns=1,
    )
    assert options.tools == []
    assert not options.mcp_servers


def test_preview_cookie_principal_is_marked() -> None:
    from app.core import mobile_tool_identity, owner_auth

    value = mobile_tool_identity.preview_principal()
    assert value["principal_id"] == mobile_tool_identity.PREVIEW_PRINCIPAL_ID
    # Same owner and credential as the local listener: nothing it may see changes.
    local = mobile_tool_identity.principal(
        "admin", "install", "local-transport:" + owner_auth.settings.effective_instance_id)
    assert value["owner"] == local["owner"] == "install-admin"
    assert value["credential_hash"] == local["credential_hash"]


@pytest.mark.parametrize("token, preview", [("preview:s1", True), ("local-owner", False)])
def test_rest_send_principal_marks_only_the_preview_cookie(token: str, preview: bool) -> None:
    from starlette.requests import Request

    from app.api.chat_query import _request_query_principal

    request = Request({"type": "http", "headers": [], "state": {"auth_token": token}})
    value = _request_query_principal(request)
    assert value["owner"] == "install-admin"
    assert (value["principal_id"] == "preview") is preview


def test_subagent_without_project_runs_in_the_no_project_folder(
    workspace: Path, tmp_path: Path,
) -> None:
    from app.core.provider_agent import _parent_cwd

    parent, _ = chat("owner-parent", mode="chat")
    worker = database.create_session(
        "owner-worker", "agent", parent_session_id=parent["id"], is_worker=True,
    )
    assert _parent_cwd(database.get_session(parent["id"])) == str(owner_chat_dir())
    assert runs_in_owner_chat_folder(worker)
    assert runtime_session_mode(worker) == "code"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A later turn without the dispatch's cwd, or one naming another folder,
    # still runs in the no-project folder: the worker cannot leave it for its cwd.
    for overrides in ({}, {"cwd": str(elsewhere)}):
        options = _build(worker["id"], overrides=overrides)
        assert options.cwd == str(owner_chat_dir())
        assert str(chat_uploads_dir()) in options.add_dirs
        assert options.aih_session_mode == "code"
        assert _has(options, "Bash")


def test_subagent_with_a_project_is_not_moved(workspace: Path, tmp_path: Path) -> None:
    parent, _ = chat("owner-parent-2", mode="chat")
    database.create_project("named", "Named", None, str(tmp_path))
    worker = database.create_session(
        "named-worker", "agent", parent_session_id=parent["id"], is_worker=True,
        project_id="named",
    )
    assert not runs_in_owner_chat_folder(worker)


def test_ceiling_chat_worker_keeps_the_default_cwd(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, _ = chat("preview-parent", mode="chat")
    monkeypatch.setattr(orch, "_principal", lambda _sid: PREVIEW)
    worker = database.create_session(
        "preview-worker", "agent", parent_session_id=parent["id"], is_worker=True, mode="chat",
    )
    assert not runs_in_owner_chat_folder(worker)
    assert runtime_session_mode(worker) == "chat"



async def _session_options(session_id: str) -> Tuple[Any, Any]:
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(_build(session_id))
    return adapter, adapter.connect.await_args.args[0]


@pytest.mark.parametrize("permission", ["default", "acceptEdits"])
async def test_uploads_are_readable_under_ask_on_both_legs(
    workspace: Path, permission: str,
) -> None:
    from fluid.tools.builtins import read as read_tool

    row, _ = chat("owner-uploads", mode="chat")
    database.set_session_run_binding(row["id"], model="sonnet", permission_mode=permission)
    adapter, session_options = await _session_options(row["id"])
    upload = chat_uploads_dir() / "photo.png"  # the build created the folder
    upload.write_bytes(b"png")
    # Delegated leg: the CLI auto-approves reads in its cwd and every
    # --add-dir; the adapter hands it chat-uploads (DelegatedLoop.add_dirs).
    assert session_options.cwd == str(owner_chat_dir())
    assert str(chat_uploads_dir()) in (session_options.add_dirs or [])
    # Native leg: the permission engine allows the read without asking.
    engine = adapter._build_permissions(session_options)
    verdict = engine.decide(read_tool.SPEC, {"file_path": str(upload)}, cwd=session_options.cwd)
    assert verdict.decision == "allow"


def test_the_no_project_folder_is_never_a_project(workspace: Path) -> None:
    from fastapi import HTTPException
    from app.api.projects import refuse_nested_repository
    from app.core.session_modes import owner_chat_cwd

    folder = owner_chat_cwd()
    for path in (folder, folder / "notes"):
        path.mkdir(exist_ok=True)
        with pytest.raises(HTTPException) as refused:
            refuse_nested_repository(path)
        assert refused.value.status_code == 400
        assert "chats without a project run" in refused.value.detail
    sibling = workspace / "no-project-ideas"
    sibling.mkdir()
    refuse_nested_repository(sibling)  # a name that only shares the prefix is fine


async def test_the_folder_picker_hides_the_no_project_folder(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import system
    from app.core import platform
    from app.core.session_modes import owner_chat_cwd

    owner_chat_cwd()
    (workspace / "real-project").mkdir()
    monkeypatch.setattr(platform, "is_local_mode", lambda: False)
    listing = await system.browse_filesystem(path=str(workspace.resolve()), token="t")
    names = [entry.name for entry in listing.entries]
    assert "real-project" in names
    assert OWNER_CHAT_DIRNAME not in names
    inside = await system.browse_filesystem(path=str(owner_chat_dir()), token="t")
    assert inside.entries == []
