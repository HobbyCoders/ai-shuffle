"""Bash — run a shell command with a timeout and output truncation."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fluid.tools.builtins._util import _TRUNCATE_LIMIT, strip_ansi, truncate
from fluid.tools.builtins.bash_risk import classify as classify_bash_risk
from fluid.tools.builtins.bash_risk import session_scope as bash_session_scope
from fluid.tools.fragments import CWD_PARAM, FRESH_PROCESS
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec
from fluid.tools.shell import resolve_shell

DEFAULT_TIMEOUT_S = 120.0


def describe(tools: frozenset[str] | None = None) -> str:
    """Bash's description, specialized to the session's tool surface.

    ``tools=None`` means the full builtin surface (the module-constant SPEC).
    Cross-tool routing clauses are emitted only when the named tool exists in
    the surface — prose about an absent tool is hallucinated-call bait.
    """
    has = (lambda name: tools is None or name in tools)
    alternatives = []
    if has("Read"):
        alternatives.append("Read instead of cat/head/tail")
    if has("Grep"):
        alternatives.append("Grep instead of grep/rg")
    if has("Glob"):
        alternatives.append("Glob instead of find/ls")
    if has("Edit") and has("Write"):
        alternatives.append("Edit/Write instead of sed or echo redirection")
    elif has("Edit"):
        alternatives.append("Edit instead of sed")
    elif has("Write"):
        alternatives.append("Write instead of echo redirection")
    prefer_bullet = (
        "- Prefer the dedicated tools over their shell equivalents: "
        + ", ".join(alternatives)
        + ". They are bounded, their results integrate with the permission "
        "gates, and they stay referenceable later in the session.\n"
        if alternatives
        else ""
    )
    background_bullet = (
        "- Never start background work with a trailing `&`, nohup, or "
        "setsid — the process outlives the call untracked."
        + (
            " Use the Monitor tool so completion wakes the session.\n"
            if has("Monitor")
            else "\n"
        )
    )
    read_windows = "Redirect to a file" + (
        " and Read it in windows" if has("Read") else ""
    )
    return (
        "Execute a shell command and return its combined stdout+stderr.\n"
        "\n"
        "The command runs through a POSIX shell (sh/bash on POSIX, Git Bash "
        "on Windows) unless the Shell line in the environment block says "
        "otherwise; that line names the real shell and the syntax to write "
        "for it, so follow it exactly. Use this tool for git, builds, tests, "
        "package managers, and other command-line programs.\n"
        "\n"
        "Usage notes:\n"
        f"- {FRESH_PROCESS}\n"
        + prefer_bullet +
        "- Provably read-only commands (file listings, git status/log/diff, "
        "plain searches) run without a permission prompt; a command that "
        "writes, redirects, substitutes (`$(...)`), or touches credential "
        "files may ask the user first. An \"always allow\" answer covers the "
        "programs that command ran, not every future command.\n"
        "- There is no stdin: commands that prompt for input will hang until "
        "the timeout. Pass non-interactive flags (e.g. --yes) where needed; "
        "interactive git flags (e.g. `git rebase -i`) are not supported.\n"
        f"- Output is capped at ~{_TRUNCATE_LIMIT // 1000}KB (head and tail "
        f"kept, middle dropped). {read_windows} when you expect more. "
        "NEVER pipe a build or test command through tail/head/cat to shorten "
        "output (e.g. `cargo build | tail -20`): the cap already handles "
        "size, and the pipe makes the exit code report the LAST pipeline "
        "command's status (tail's 0), masking real failures — run the "
        "command bare. `cmd || echo failed` masks the exit code the same "
        "way.\n"
        + background_bullet +
        "- A non-zero exit code appends an [exit code N] line to the output and "
        "is flagged as an error — read the output before retrying. In code mode "
        "Bash() returns that text instead of raising, so exit codes can be "
        "tested in Python (git merge-base, grep -q, test).\n"
        "- A command that prints nothing returns `(no output) [exit code N]` — "
        "the status is stated because there is no output to judge by. A "
        "silent `[exit code 0]` means the command SUCCEEDED; do not re-run it "
        "to check, least of all when it was irreversible (merge, push, "
        "delete).\n"
        "- That no-output form is a NON-EMPTY string, so in code mode test "
        "`'[exit code' in out` (or `'(no output)' in out`), never `if out:` or "
        "`out or fallback` — those are always true and will misfire.\n"
        "- Quote file paths that contain spaces. Under Git Bash, write Windows "
        "paths with forward slashes or inside quotes so backslashes survive.\n"
    )


SPEC = ToolSpec(
    name="Bash",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute."},
            "timeout": {
                "type": "number",
                "description": (
                    "Seconds before the command is killed and reported as a "
                    f"timeout error (default {DEFAULT_TIMEOUT_S:g})."
                ),
            },
            "cwd": {
                "type": "string",
                "description": CWD_PARAM,
            },
        },
        "required": ["command"],
    },
    risk="write",
    # Read-only commands (git status, ls, grep ...) are reported as safe per
    # call so the on_write policy does not prompt for them. See bash_risk.py.
    classify_risk=classify_bash_risk,
    session_scope=bash_session_scope,
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    command = input["command"]
    timeout = float(input.get("timeout", DEFAULT_TIMEOUT_S))
    cwd = input.get("cwd") or ctx.cwd
    proc = await asyncio.create_subprocess_exec(
        *resolve_shell().argv_prefix,
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        # Ask the child not to colourise; anything that ignores the hint gets
        # its escapes stripped from the captured output below.
        env=dict(os.environ, NO_COLOR="1", FORCE_COLOR="0"),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        # Killed, so nothing partial survives — say what the retry should
        # change rather than leaving the model to re-run the same command.
        return ToolOutcome(
            text=(
                f"command timed out after {timeout:g}s and was killed. Raise "
                "the timeout parameter for a genuinely long command, narrow "
                "the command's scope, or run it detached (append ' &' and "
                "poll its log) if it is meant to keep running."
            ),
            is_error=True,
        )
    text = truncate(strip_ansi(stdout.decode(errors="replace")))
    code = proc.returncode or 0
    if not text.strip():
        # A bare "(no output)" is the one result that says nothing about what
        # happened: a silent success and a silent failure read identically, and
        # a `gh pr merge` that already went through invites a retry. Whenever
        # there is no output to judge by, the status is the whole answer.
        return ToolOutcome(
            text=f"(no output) [exit code {code}]",
            is_error=bool(code),
            soft_error=bool(code),
        )
    if code:
        return ToolOutcome(
            text=f"{text.rstrip()}\n[exit code {code}]",
            is_error=True,
            soft_error=True,
        )
    return ToolOutcome(text=text)
