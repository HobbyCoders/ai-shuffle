"""Bash — run a shell command with a timeout and output truncation."""

from __future__ import annotations

import asyncio
from typing import Any

from fluid.tools.builtins._util import truncate
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Bash",
    description="Run a shell command and return combined stdout/stderr.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number", "description": "Seconds (default 120)."},
            "cwd": {"type": "string"},
        },
        "required": ["command"],
    },
    risk="write",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    command = input["command"]
    timeout = float(input.get("timeout", 120))
    cwd = input.get("cwd") or ctx.cwd
    # Claude Code settings.json ``env``, when the session picked one up. Layered
    # over a copy of the inherited environment — os.environ is never mutated, so
    # concurrent sessions can carry different overlays. None → plain inherit.
    settings_env = ctx.extra.get("settings_env")
    env = None
    if settings_env:
        from fluid.context.claude_settings import layered_env

        env = layered_env(settings_env)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolOutcome(text=f"command timed out after {timeout}s", is_error=True)
    text = truncate(stdout.decode(errors="replace"))
    return ToolOutcome(text=text or "(no output)", is_error=proc.returncode != 0)
