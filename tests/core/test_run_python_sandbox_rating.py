"""S2-1 residual: the app's sessions rate ``run_python`` by the OS sandbox.

Without bubblewrap (the stock Docker compose) the code-mode kernel's only
boundary is the in-process guard, so ``run_python`` is a write-class tool: an
Ask session (``default``) prompts for it; Accept Edits and Bypass approve it
exactly as before. Behind the sandbox it stays ``safe`` and nothing prompts.
The delegated leg's grant list (safe-rated in-proc tools only) never carries it.
"""

from __future__ import annotations

import pytest

from app.core.provider.harness_adapter import (
    HarnessAdapter,
    _delegated_permission_grants,
)
from app.core.provider.types import SessionOptions
from app.core.session_modes import mode_approves_without_prompt
from fluid.codemode.session import CodeModeSession, register_code_mode_tools
from fluid.tools.registry import ToolRegistry


def _run_python_spec(monkeypatch, active: bool):
    monkeypatch.setattr(
        "fluid.codemode.session.os_sandbox_active", lambda _config=None: active
    )
    registry = ToolRegistry()
    register_code_mode_tools(registry, CodeModeSession(registry))
    return next(s for s in registry.list_tools(loaded_only=False) if s.name == "run_python")


def _decision(mode: str, spec) -> str:
    """What a session in ``mode`` does with one call: engine verdict, then the
    app approver for an ``ask``."""
    engine = HarnessAdapter()._build_permissions(SessionOptions(model="m", permission_mode=mode))
    verdict = engine.evaluate(spec, {"code": "print(1)"})
    if verdict != "ask":
        return verdict
    return "allow" if mode_approves_without_prompt(mode, spec.name) else "prompt"


@pytest.mark.parametrize(
    "active,mode,expected",
    [
        (False, "default", "prompt"),
        (False, "acceptEdits", "allow"),
        (False, "bypassPermissions", "allow"),
        (True, "default", "allow"),
        (True, "acceptEdits", "allow"),
        (True, "bypassPermissions", "allow"),
    ],
)
def test_ask_prompts_for_run_python_only_without_the_sandbox(monkeypatch, active, mode, expected):
    spec = _run_python_spec(monkeypatch, active)
    assert spec.effective_risk({"code": "print(1)"}) == ("safe" if active else "write")
    assert _decision(mode, spec) == expected


@pytest.mark.parametrize("active", [False, True])
def test_delegated_safe_grants_never_carry_run_python(monkeypatch, tmp_path, active):
    _run_python_spec(monkeypatch, active)
    options = SessionOptions(
        model="fable",
        cwd=str(tmp_path),
        permission_mode="default",
        mcp_servers={"builtins": {"type": "sdk", "name": "builtins", "instance": object()}},
        allowed_tools=["run_python", "mcp__builtins__run_python", "mcp__builtins__Recall"],
    )
    grants = _delegated_permission_grants(options) or []
    assert not any(g.endswith("run_python") for g in grants)
    assert "mcp__fluid_bundled__mcp__builtins__Recall" in grants


# ─── R13-6: the Ask prompt for a cell says what it asks about ────────────────


async def _bridged_context(risk: str, tool_input: dict, name: str = "run_python"):
    """The context the app's can_use_tool callback receives for one prompt."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core.agent_types import PermissionResultDeny
    from app.core.provider.types import PermissionRequestEvent

    adapter = HarnessAdapter()
    adapter._engine = SimpleNamespace(_registry_spec=lambda _n: None)
    adapter._can_use_tool = AsyncMock(return_value=PermissionResultDeny(message="no"))
    adapter.respond_to_permission = AsyncMock()
    await adapter._bridge_permission(
        PermissionRequestEvent(tool_use_id="call", name=name, input=tool_input, risk=risk)
    )
    args = adapter._can_use_tool.await_args.args
    assert args[0] == name and args[1] == tool_input
    return args[2]


@pytest.mark.asyncio
async def test_unsandboxed_cell_prompt_has_a_title_reason_and_plain_code():
    code = 'import os\nprint("a\\tb", os.getcwd())\n' + "".join(f"x{i} = {i}\n" for i in range(40))
    ctx = await _bridged_context("write", {"code": code})
    assert ctx.title == "Run Python code" and ctx.display_name == "Run Python code"
    reason, preview = ctx.description.split("\n\n", 1)
    assert "sandbox is not active" in reason
    # Plain text, not JSON: real newlines and quotes, no \n / \" escapes.
    assert preview.startswith('import os\nprint("a\\tb", os.getcwd())\nx0 = 0\n')
    assert '\\"' not in preview and "{\"code\"" not in preview
    lines = preview.splitlines()
    assert lines[:20] == code.splitlines()[:20]
    assert lines[-1] == "… (22 more lines not shown)"
    assert ctx.tool_use_id == "call"


@pytest.mark.asyncio
async def test_long_single_line_cell_is_cut_at_the_char_budget():
    from fluid.codemode.session import PROMPT_PREVIEW_CHARS

    ctx = await _bridged_context("write", {"code": "y = '" + "z" * 5000 + "'"})
    preview = ctx.description.split("\n\n", 1)[1]
    assert preview.endswith(" … (truncated)")
    assert len(preview) <= PROMPT_PREVIEW_CHARS + len(" … (truncated)")


@pytest.mark.asyncio
async def test_sandboxed_cell_prompt_has_no_sandbox_reason_and_other_tools_none():
    ctx = await _bridged_context("safe", {"code": "print(1)"})
    assert ctx.title == "Run Python code"
    assert ctx.description == "print(1)"
    other = await _bridged_context("write", {"command": "ls"}, name="Bash")
    assert not hasattr(other, "title") and not hasattr(other, "description")


@pytest.mark.asyncio
@pytest.mark.parametrize("risk", ["write", "safe"])
async def test_a_workers_cell_prompt_keeps_its_tag_out_of_the_code_block(risk):
    """R15-4: a ProviderAgent worker's approval prepended "Requested by
    ProviderAgent worker ..." to the description, which the app shows whole as
    the "Code to run" block. The tag now rides the title; the description is
    exactly what the prompt builder produced."""
    from unittest.mock import AsyncMock

    from app.core import provider_agent_approvals as approvals

    ctx = await _bridged_context(risk, {"code": "print(1)\n"})
    route = approvals.WorkerApprovalRoute(
        worker_session_id="worker-1", parent_session_id="parent-1",
        label="reviewer", provider_id="prov-openai", model_id="gpt-5.5",
    )
    kwargs = dict(
        request_id="perm-1", session_id="worker-1", profile_id="default",
        tool_name="run_python", tool_input={"code": "print(1)\n"},
        broadcast_func=AsyncMock(), title=ctx.title,
        display_name=ctx.display_name, description=ctx.description,
    )
    out = approvals.redirect_kwargs(route, kwargs)

    assert out["description"] == ctx.description
    assert out["description"].endswith("print(1)")
    assert "Requested by" not in out["description"] and "reviewer" not in out["description"]
    assert out["title"] == "reviewer (prov-openai · gpt-5.5) — Run Python code"
    assert out["provider_agent"]["label"] == "reviewer"
    # Any other tool keeps the tag line in its description.
    bash = approvals.redirect_kwargs(route, {**kwargs, "tool_name": "Bash",
                                             "title": None, "description": None})
    assert bash["title"] == "reviewer"
    assert bash["description"].startswith("Requested by ProviderAgent worker “reviewer”")
