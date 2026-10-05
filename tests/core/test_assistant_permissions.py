"""The owner's assistant runs in Bypass; the app asks the owner for high-risk calls."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import assistant_permissions as ap
from app.core import assistant_service as service
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.db import database
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401 — fixture

ROOT = os.path.abspath("assistant-project")
OUTSIDE = os.path.abspath(os.path.join(os.sep, "owner-private", "notes.txt"))
SCOPE = {"session_id": "s", "home_session_id": "h", "roots": [ROOT], "cwd": ROOT}


def inside(*parts):
    return os.path.join(ROOT, *parts)


# ---------------------------------------------------------------------------
# Every built-in tool has a declared level
# ---------------------------------------------------------------------------

def _builtin_tool_names():
    from app.core import tool_catalog
    from app.core.cli_tool_surface import KNOWN_CLI_TOOLS
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_builtins(registry)
    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    names |= set(KNOWN_CLI_TOOLS) | {"run_python"}
    for rows in (tool_catalog.harness_tools(), tool_catalog.app_tools(), tool_catalog.app_config_tools(),
                 tool_catalog.ai_tools()[0], tool_catalog.computer_use_tools()[0]):
        names |= {row["name"] for row in rows}
    from app.mcp import chat_access_sdk
    names |= {f"mcp__chat-access__{name}" for name in (
        "RequestChatAccess", "ChatResources", "ChatListFiles", "ChatReadFile", "ChatWriteFile",
        "ChatRunCommand", "ChatHttpRequest")}
    assert chat_access_sdk  # The chat-access server these names come from.
    return names


def test_every_builtin_tool_declares_a_level():
    missing = sorted(name for name in _builtin_tool_names() if not ap.declared(name))
    assert missing == [], f"Declare a risk level in assistant_permissions.TOOL_LEVELS for: {missing}"


def test_an_undeclared_tool_asks_and_a_failing_rule_asks(monkeypatch):
    assert ap.level("mcp__someones-server__do_thing", {}, SCOPE) == ap.HIGH
    assert ap.level("BrandNewBuiltin", {}, SCOPE) == ap.HIGH

    def broken(data, scope):
        raise RuntimeError("bad rule")
    monkeypatch.setitem(ap.TOOL_LEVELS, "Bash", broken)
    assert ap.level("Bash", {"command": "ls"}, SCOPE) == ap.HIGH


def test_transport_prefixes_name_the_same_tool():
    for name in ("Task", "mcp__builtins__Task", "mcp__fluid_bundled__Task",
                 "mcp__fluid_bundled__mcp__builtins__Task"):
        assert ap.level(name, {}, SCOPE) == ap.LOW


# ---------------------------------------------------------------------------
# Ordinary work goes ahead; each class on the fixed list asks
# ---------------------------------------------------------------------------

LOW_CALLS = [
    ("Read", {"file_path": inside("src", "app.py")}),
    ("Grep", {"pattern": "def ", "path": inside("src")}),
    ("Grep", {"pattern": "def "}),
    ("Glob", {"pattern": "**/*.py"}),
    ("Write", {"file_path": inside("src", "new.py")}),
    ("Edit", {"file_path": inside("README.md")}),
    ("Bash", {"command": "pytest -q tests/core"}),
    ("Bash", {"command": "git status && git add -A && git commit -m 'Fix the bug'"}),
    ("Bash", {"command": "git checkout -b fix/sidebar && git push -u origin fix/sidebar"}),
    ("Bash", {"command": "gh pr create --title 'Fix sidebar' --body done"}),
    ("Bash", {"command": "gh pr view 12 --json state"}),
    ("Bash", {"command": "npm ci && npm run build"}),
    ("Bash", {"command": "curl -s https://example.com/docs"}),
    ("PowerShell", {"command": "Get-ChildItem src"}),
    ("Task", {"subagent_type": "general-purpose", "prompt": "Fix it"}),
    ("StartSession", {"title": "Fix", "prompt": "Fix", "request_id": "r"}),
    ("StartSession", {"title": "Fix", "prompt": "Fix", "request_id": "r", "worker": "Unraid", "project": "ai-shuffle"}),
    ("WebSearch", {"query": "svelte runes"}),
    ("WebFetch", {"url": "https://example.com"}),
    ("Remember", {"text": "Owner likes short replies"}),
    ("MessageSession", {"session_id": "x", "message": "hi", "request_id": "r"}),
    ("mcp__browser__browser_navigate", {"url": "http://127.0.0.1:5173"}),
    ("mcp__computer-use__left_click", {"coordinate": [1, 2]}),
    ("run_python", {"code": "hits = Grep(pattern='x')\nprint(len(hits))"}),
]

HIGH_CALLS = [
    # Spending outside the model calls.
    ("mcp__ai-tools__generateImage", {"prompt": "a cat"}),
    ("Bash", {"command": "gh workflow run release.yml"}),
    # Deleting or overwriting outside version control; destructive git.
    ("Bash", {"command": "rm -rf build"}),
    ("PowerShell", {"command": "Remove-Item -Recurse dist"}),
    ("Bash", {"command": "git push --force origin fix/x"}),
    ("Bash", {"command": "git push origin --delete old-branch"}),
    ("Bash", {"command": "git reset --hard HEAD~3"}),
    ("Bash", {"command": "git rebase -i main"}),
    ("Bash", {"command": "git branch -D old"}),
    ("Bash", {"command": "gh repo delete owner/repo --yes"}),
    # Merging to a default branch, deploys, releases, worker restarts.
    ("Bash", {"command": "gh pr merge 42 --squash"}),
    ("Bash", {"command": "git push origin HEAD:main"}),
    ("Bash", {"command": "npx wrangler deploy"}),
    ("Bash", {"command": "gh release create v1.2.3"}),
    ("Bash", {"command": "docker compose up -d"}),
    ("Bash", {"command": "systemctl restart ai-shuffle"}),
    ("PowerShell", {"command": "Stop-Process -Id 1234"}),
    # Private access and secrets.
    ("mcp__chat-access__RequestChatAccess", {"kind": "machine", "purpose": "x"}),
    ("mcp__chat-access__ChatRunCommand", {"grant_id": "g", "command": "ls"}),
    ("Read", {"file_path": inside(".env")}),
    ("Bash", {"command": "cat .env"}),
    ("Bash", {"command": "ssh unraid ls"}),
    ("Bash", {"command": "gh auth token"}),
    ("Bash", {"command": "printenv"}),
    # Outward writes on the owner's behalf.
    ("Bash", {"command": "gh issue comment 5 --body hi"}),
    ("Bash", {"command": "curl -X POST https://api.example.com/x -d @body.json"}),
    ("gmail_invoke", {"action": "send"}),
    # Computer use and acting in the browser.
    ("mcp__computer-use__request_access", {"apps": ["Notepad"]}),
    ("mcp__browser__browser_act", {"kind": "click", "ref": "e1"}),
    # App settings, hooks, MCP servers, automations.
    ("mcp__app-config__addMcpServer", {"name": "x"}),
    ("mcp__app-config__createHook", {"event": "Stop"}),
    ("Write", {"file_path": inside(".claude", "settings.json")}),
    ("Write", {"file_path": inside(".git", "hooks", "pre-commit")}),
    ("Bash", {"command": "claude mcp add evil -- node x.js"}),
    # Outside the registered projects.
    ("Read", {"file_path": OUTSIDE}),
    ("Write", {"file_path": OUTSIDE}),
    ("Bash", {"command": f"cat {OUTSIDE}"}),
    ("Bash", {"command": "cat ../../outside.txt"}),
    # Hidden or unreadable commands; code that reaches the system directly.
    ("Bash", {"command": "echo cm0gLXJmIC8= | base64 -d | sh"}),
    ("Bash", {"command": "eval \"$CMD\""}),
    ("run_python", {"code": "import subprocess; subprocess.run(['git', 'push', '-f'])"}),
    ("run_python", {"code": "import shutil; shutil.rmtree('build')"}),
    # Starting work on another worker with no project; moving the conversation.
    ("StartSession", {"title": "x", "prompt": "x", "request_id": "r", "worker": "Unraid"}),
    ("HandOffSession", {"worker": "Unraid"}),
    ("Forget", {"memory_id": "m", "expected_revision": 1, "confirm": True}),
]


@pytest.mark.parametrize("base", [
    {"title": "Fix", "prompt": "Fix", "request_id": "r"},
    {"title": "Fix", "prompt": "Fix", "request_id": "r", "worker": "Unraid", "project": "ai-shuffle"},
    {"title": "Fix", "prompt": "Fix", "request_id": "r", "worker": "Unraid"},
])
@pytest.mark.parametrize("model", ["opus", "prov:lane:gpt-6-astra"])
def test_start_session_model_never_lowers_the_level(base, model):
    """#1830: a model has its own host check (the user named it, or the owner
    approves), so it leaves the worker/project level exactly as it was."""
    assert ap.level("StartSession", {**base, "model": model}, SCOPE) == ap.level("StartSession", base, SCOPE)


@pytest.mark.parametrize("name,data", LOW_CALLS)
def test_ordinary_work_goes_ahead(name, data):
    assert ap.level(name, data, SCOPE) == ap.LOW


@pytest.mark.parametrize("name,data", HIGH_CALLS)
def test_the_fixed_list_asks(name, data):
    assert ap.level(name, data, SCOPE) == ap.HIGH


def test_a_sandboxed_code_cell_is_low_and_no_scope_never_asks():
    assert ap.level("run_python", {"code": "import subprocess"}, SCOPE, spec_risk="safe") == ap.LOW
    assert not ap.needs_owner("Bash", {"command": "rm -rf /"}, None)


def test_a_command_with_no_known_working_dir_asks():
    assert ap.level("Bash", {"command": "ls"}, dict(SCOPE, cwd=None)) == ap.HIGH


# ---------------------------------------------------------------------------
# Scope: the home, chats it starts, and Task workers under them
# ---------------------------------------------------------------------------

def test_scope_covers_the_home_its_chats_and_their_workers(assistant_fixture, monkeypatch):
    home = assistant_fixture["id"]
    assert ap.home_of(home) == home
    worker = database.create_session("home-worker", service.AGENT_ID, parent_session_id=home, is_worker=True)
    assert ap.home_of(worker["id"]) == home
    chat = database.create_session("started-chat", service.AGENT_ID, is_active=True)
    nested = database.create_session("chat-worker", service.AGENT_ID, parent_session_id=chat["id"], is_worker=True)
    from app.core import session_orchestration
    monkeypatch.setattr(session_orchestration, "started_by_home",
                        lambda sid: home if sid == chat["id"] else None)
    assert ap.home_of(chat["id"]) == home and ap.home_of(nested["id"]) == home
    ordinary = database.create_session("ordinary", "assistant-agent", is_active=True)
    assert ap.home_of(ordinary["id"]) is None
    # A fork is not a worker: its parent link does not make it assistant work.
    fork = database.create_session("fork", "assistant-agent", parent_session_id=home)
    assert ap.home_of(fork["id"]) is None
    scope = ap.scope_for_session(home, cwd=ROOT)
    assert scope["home_session_id"] == home and scope["cwd"] == ROOT and scope["roots"]


def test_the_home_runs_in_bypass_and_an_ordinary_session_keeps_its_mode(assistant_fixture):
    from app.core.query_options import build_options_from_profile
    home = assistant_fixture["id"]
    database.set_session_run_binding(home, permission_mode="default")
    options, _ = build_options_from_profile(database.get_profile(service.AGENT_ID), session_id=home)
    assert options.permission_mode == "bypassPermissions"
    assert options.aih_assistant_scope["home_session_id"] == home
    ordinary = database.create_session("ordinary-chat", "assistant-agent", is_active=True)
    database.set_session_run_binding(ordinary["id"], permission_mode="default")
    options, _ = build_options_from_profile(database.get_profile("assistant-agent"), session_id=ordinary["id"])
    assert options.permission_mode == "default" and options.aih_assistant_scope is None


# ---------------------------------------------------------------------------
# The runtime seams
# ---------------------------------------------------------------------------

def _spec(name, risk="write"):
    from fluid.tools.registry import ToolSpec
    return ToolSpec(name=name, description="", input_schema={}, risk=risk)


def test_native_engine_asks_only_for_high_calls_under_bypass():
    from fluid.permissions.rules import Rule
    engine = ha._AssistantPermissionEngine(SCOPE, approval_policy="never_ask")
    assert engine.decide(_spec("Bash"), {"command": "pytest"}, cwd=ROOT).decision == "allow"
    assert engine.decide(_spec("Bash"), {"command": "gh pr merge 1"}, cwd=ROOT).decision == "ask"
    denying = ha._AssistantPermissionEngine(SCOPE, approval_policy="never_ask",
                                            rules=[Rule(tool="Bash", decision="deny")])
    assert denying.decide(_spec("Bash"), {"command": "gh pr merge 1"}, cwd=ROOT).decision == "deny"


def test_build_permissions_uses_the_assistant_engine_only_in_scope():
    adapter = ha.HarnessAdapter()
    scoped = SessionOptions(model="m", permission_mode="bypassPermissions", extra={"assistant_scope": SCOPE})
    plain = SessionOptions(model="m", permission_mode="bypassPermissions")
    assert isinstance(adapter._build_permissions(scoped), ha._AssistantPermissionEngine)
    assert type(adapter._build_permissions(plain)) is ha.PermissionEngine


def _adapter(scope, answer=None):
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="m", permission_mode="bypassPermissions",
                                      extra={"assistant_scope": scope} if scope else {})
    adapter._can_use_tool = AsyncMock(return_value=answer) if answer is not None else None
    return adapter


@pytest.mark.parametrize("answer,denied", [(PermissionResultAllow(), False),
                                           (PermissionResultDeny(message="Declined by the owner"), True)])
async def test_delegated_cli_tool_waits_for_the_owner(answer, denied):
    adapter = _adapter(SCOPE, answer)
    policy = adapter._delegated_policy_with_saved_rules(None)
    result = await policy("Bash", {"command": "gh pr merge 3"})
    assert bool(result) is denied
    context = adapter._can_use_tool.await_args.args[2]
    assert context.assistant_owner is True
    # Ordinary work does not ask.
    adapter._can_use_tool.reset_mock()
    assert await policy("Bash", {"command": "pytest"}) is None
    adapter._can_use_tool.assert_not_awaited()


async def test_delegated_cli_tool_with_nobody_to_ask_is_denied_and_plain_sessions_are_unchanged():
    adapter = _adapter(SCOPE)
    policy = adapter._delegated_policy_with_saved_rules(None)
    assert await policy("Bash", {"command": "gh pr merge 3"}) == ap.NO_OWNER_DENIAL
    plain = _adapter(None, PermissionResultDeny(message="x"))
    assert await plain._delegated_policy_with_saved_rules(None)("Bash", {"command": "gh pr merge 3"}) is None
    plain._can_use_tool.assert_not_awaited()


async def test_bundled_tools_on_the_cli_leg_ask_at_dispatch():
    from fluid.tools.registry import ToolOutcome, ToolRegistry
    registry = ToolRegistry()
    ran = []

    async def handler(data, ctx):
        ran.append(data)
        return ToolOutcome(text="done")
    registry.register_builtin(_spec("HandOffSession"), handler)
    registry.register_builtin(_spec("ListSessions", "safe"), handler)
    adapter = _adapter(SCOPE, PermissionResultDeny(message="Declined by the owner"))
    adapter._guard_assistant_dispatch(registry, adapter._options)
    denied = await registry.dispatch("HandOffSession", {"worker": "Unraid"})
    assert denied.is_error and "Declined" in denied.text and not ran
    assert not (await registry.dispatch("ListSessions", {})).is_error and ran


async def test_bridged_native_prompt_is_flagged_for_the_owner():
    from app.core.provider.types import PermissionRequestEvent
    adapter = _adapter(SCOPE, PermissionResultAllow())
    adapter.respond_to_permission = AsyncMock()
    await adapter._bridge_permission(PermissionRequestEvent(
        tool_use_id="t1", name="Bash", input={"command": "gh pr merge 3"}, risk="write"))
    assert adapter._can_use_tool.await_args.args[2].assistant_owner is True
    await adapter._bridge_permission(PermissionRequestEvent(
        tool_use_id="t2", name="Bash", input={"command": "pytest"}, risk="write"))
    assert adapter._can_use_tool.await_args.args[2].assistant_owner is False


async def test_native_prompt_with_no_approver_is_denied_not_allowed_by_bypass():
    from app.core.provider.types import PermissionRequestEvent
    adapter = _adapter(SCOPE)
    adapter.respond_to_permission = AsyncMock()
    await adapter._allow_without_approver(PermissionRequestEvent(
        tool_use_id="t1", name="Bash", input={"command": "gh pr merge 3"}, risk="write"))
    args, kwargs = adapter.respond_to_permission.await_args
    assert args[1] is False and kwargs["message"] == ap.NO_OWNER_DENIAL


async def test_approver_puts_the_call_to_the_owner_past_bypass_and_saved_rules(tmp_path):
    from tests.core.test_computer_use_request_access_callback import _callback
    callback = await _callback(tmp_path, "bypassPermissions")
    prompt = AsyncMock(return_value=PermissionResultDeny(message="Declined"))
    with patch("app.core.provider_agent_approvals.route_permission", new=prompt):
        result = await callback("Bash", {"command": "gh pr merge 3"},
                                SimpleNamespace(tool_use_id="t", agent_id=None, assistant_owner=True))
        assert type(result).__name__ == "PermissionResultDeny"
        prompt.assert_awaited_once()
        assert prompt.await_args.args[0]["skip_saved_rules"] is True
        # Without the flag, Bypass approves on its own as before.
        prompt.reset_mock()
        result = await callback("Bash", {"command": "gh pr merge 3"},
                                SimpleNamespace(tool_use_id="t", agent_id=None))
        assert type(result).__name__ == "PermissionResultAllow"
        prompt.assert_not_awaited()


def test_the_home_is_told_what_will_ask():
    assert ap.ASK_LIST_SUMMARY in service.HOME_INSTRUCTIONS


# ---------------------------------------------------------------------------
# A push that names no branch pushes the current one
# ---------------------------------------------------------------------------

def _checkouts(tmp_path, *, default="main", origin_head="main"):
    """A repository on ``default`` and a linked worktree on a feature branch."""
    repo = tmp_path / "repo"
    common = repo / ".git"
    (common / "refs" / "remotes" / "origin").mkdir(parents=True)
    (common / "HEAD").write_text(f"ref: refs/heads/{default}\n")
    if origin_head:
        (common / "refs" / "remotes" / "origin" / "HEAD").write_text(f"ref: refs/remotes/origin/{origin_head}\n")
    git_dir = common / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/fix/sidebar\n")
    (git_dir / "commondir").write_text("../..\n")
    worktree = tmp_path / "feature"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")
    return repo, worktree


def _scope(tmp_path, cwd):
    return {"session_id": "s", "home_session_id": "h", "roots": [str(tmp_path)], "cwd": str(cwd)}


@pytest.mark.parametrize("command", ["git push", "git push origin", "git push -u origin HEAD", "git push --tags origin"])
def test_a_plain_push_from_the_default_branch_asks(tmp_path, command):
    repo, _ = _checkouts(tmp_path)
    assert ap.level("Bash", {"command": command}, _scope(tmp_path, repo)) == ap.HIGH


@pytest.mark.parametrize("command", ["git push", "git push origin", "git push -u origin HEAD"])
def test_a_plain_push_from_a_feature_branch_goes_ahead(tmp_path, command):
    _, worktree = _checkouts(tmp_path)
    # From a subfolder too: the checkout is found by walking up.
    assert ap.level("Bash", {"command": command}, _scope(tmp_path, worktree / "src")) == ap.LOW


def test_the_remotes_default_branch_counts_and_doubt_asks(tmp_path):
    repo, worktree = _checkouts(tmp_path, default="stable", origin_head="stable")
    assert ap.level("Bash", {"command": "git push"}, _scope(tmp_path, repo)) == ap.HIGH
    # git -C points the push at another checkout.
    assert ap.level("Bash", {"command": f"git -C {repo} push"}, _scope(tmp_path, worktree)) == ap.HIGH
    assert ap.level("Bash", {"command": f"git -C {worktree} push"}, _scope(tmp_path, repo)) == ap.LOW
    # No repository (or a detached head) cannot be read, so it asks.
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert ap.level("Bash", {"command": "git push"}, _scope(tmp_path, plain)) == ap.HIGH
    (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert ap.level("Bash", {"command": "git push"}, _scope(tmp_path, repo)) == ap.HIGH
