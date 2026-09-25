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
