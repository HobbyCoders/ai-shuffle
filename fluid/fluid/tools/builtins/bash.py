"""Bash — run a shell command with a timeout and output truncation."""

from __future__ import annotations

import asyncio
from typing import Any

from fluid.tools.builtins._util import truncate
from fluid.tools.builtins.bash_risk import classify as classify_bash_risk
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Bash",
    description=(
        "Execute a shell command and return its combined stdout+stderr.\n"
        "\n"
        "The command runs in the platform's default shell — the Shell line in "
        "the environment block tells you which syntax to use; follow it "
        "exactly. Use this tool for git, builds, tests, package managers, and "
        "other command-line programs.\n"
        "\n"
        "Usage notes:\n"
        "- Each call is a fresh process: environment variables, shell "
        "functions, and `cd` do NOT persist between calls. Pass `cwd` or use "
        "absolute paths instead of relying on an earlier `cd`.\n"
        "- Prefer the dedicated tools over their shell equivalents: Read "
        "instead of cat/head/tail, Grep instead of grep/rg, Glob instead of "
        "find, and Write/Edit instead of echo/sed redirection. They are "
        "bounded, faster, and their results integrate with the session.\n"
        "- There is no stdin: commands that prompt for input will hang until "
        "the timeout. Pass non-interactive flags (e.g. --yes) where needed; "
        "interactive git flags (e.g. `git rebase -i`) are not supported.\n"
        "- Output is capped at ~30KB (head and tail kept, middle dropped). "
        "Redirect to a file and Read it in windows when you expect more.\n"
        "- A non-zero exit code is reported as an error together with the "
        "output — read the output before retrying.\n"
        "- Quote file paths that contain spaces.\n"
        "\n"
        "Git discipline: never commit, push, or open a PR unless the user "
        "asks. When they do, write commit messages that explain the why, and "
        "never skip hooks or force-push unless explicitly told to."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute."},
            "timeout": {
                "type": "number",
                "description": (
                    "Seconds before the command is killed and reported as a "
                    "timeout error (default 120)."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory for this command (default: the session "
                    "cwd). Set it instead of prefixing the command with cd."
                ),
            },
        },
        "required": ["command"],
    },
    risk="write",
    # Read-only commands (git status, ls, grep ...) are reported as safe per
    # call so the on_write policy does not prompt for them. See bash_risk.py.
    classify_risk=classify_bash_risk,
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
