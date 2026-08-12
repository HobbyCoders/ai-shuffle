"""Bash — run a shell command with a timeout and output truncation."""

from __future__ import annotations

import asyncio
from typing import Any

from omni.tools.builtins._util import truncate
from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

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
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolOutcome(text=f"command timed out after {timeout}s", is_error=True)
    text = truncate(stdout.decode(errors="replace"))
    return ToolOutcome(text=text or "(no output)", is_error=proc.returncode != 0)
