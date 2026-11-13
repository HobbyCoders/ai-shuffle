"""Bash — run a shell command with a timeout and output truncation."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
from typing import Any

from fluid.context.settings_stack import layered_env
from fluid.tools import agent_runner
from fluid.tools.builtins._util import _TRUNCATE_LIMIT, strip_ansi
from fluid.tools.builtins.bash_risk import classify as classify_bash_risk
from fluid.tools.builtins.bash_risk import session_scope as bash_session_scope
from fluid.tools.child_env import scrub_child_env
from fluid.tools.fragments import CWD_PARAM, FRESH_PROCESS
from fluid.tools.hardening import fence_shell_output
from fluid.tools.priority import (
    lower_process_group_priority,
    priority_creationflags,
    raise_oom_score_adj,
)
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec
from fluid.tools.shell import resolve_shell
from fluid.tools.spill import SpillStream, cut_streamed

DEFAULT_TIMEOUT_S = 120.0
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 600.0
# Bytes of a command's output kept in memory while it runs: the first half
# and the last half, the middle dropped as it streams. Far above what the
# model sees inline; the spill file gets the whole output as it streams.
MAX_CAPTURE_BYTES = 1024 * 1024
# Share of the inline budget given to the head; the tail gets the rest, since
# errors, tracebacks and exit summaries come last.
_HEAD_SHARE = 1 / 3
# After a timeout kill, how long to read what the command wrote just before it.
_DRAIN_AFTER_KILL_S = 2.0
# The host this process runs on; describe() reads it at call time so a
# measurement fixture can declare the platform it renders for.
HOST_IS_WINDOWS = sys.platform == "win32"


def describe(tools: frozenset[str] | None = None, *, windows: bool | None = None) -> str:
    """Bash's description, specialized to the session's tool surface.

    ``tools=None`` means the full builtin surface (the module-constant SPEC).
    ``windows`` (default: this process's platform) adds the Git Bash notes,
    which only describe a Windows host; elsewhere they are dead prompt.
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
    readers = " or ".join(name for name in ("Read", "Grep") if has(name))
    read_back = f" — {readers} that file" if readers else " — read that file"
    ps1_route = (
        "save the script to a .ps1 file with Write"
        if has("Write")
        else "put the script in a .ps1 file"
    )
    if windows is None:
        windows = HOST_IS_WINDOWS
    windows_shell_bullet = (
        "- On Windows that shell is Git Bash, not PowerShell or cmd. "
        "PowerShell syntax is not rejected there, it is MANGLED: bash expands "
        "`$_`, `$var` and `$env:NAME` itself (usually to nothing) and eats "
        "backtick escapes, so the command runs with different text than you "
        "wrote and can still exit 0. Use the bash equivalent, or "
        f"{ps1_route} and run it with `powershell -File script.ps1`.\n"
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
        "programs that command ran (for sudo, npm, uv, make and the like, the "
        "program together with what it ran), not every future command.\n"
        "- There is no stdin: commands that prompt for input will hang until "
        "the timeout. Pass non-interactive flags (e.g. --yes) where needed; "
        "interactive git flags (e.g. `git rebase -i`) are not supported.\n"
        f"- Output over {_TRUNCATE_LIMIT:,} characters keeps its head and a "
        "longer tail; the cut marker names a file with the full output"
        f"{read_back} instead of re-running. NEVER pipe a build or test command through tail/head/cat to shorten "
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
        "- Quote file paths that contain spaces."
        + (" Under Git Bash, write Windows paths with forward slashes or inside "
           "quotes so backslashes survive.\n" + windows_shell_bullet
           if windows else "\n")
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
                    f"timeout error (default {DEFAULT_TIMEOUT_S:g}, at most "
                    f"{MAX_TIMEOUT_S:g})."
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


# `cargo test | tail -20` on a command that printed nothing to the pipe reports
# tail's exit code (0) and no output — indistinguishable from a clean silent
# success. The description forbids the pipe; when it happens anyway and the
# result is the ambiguous empty one, say what it probably means.
_OUTPUT_PIPE_RE = re.compile(r"\|\s*(?:tail|head)\b")

_PIPE_ATE_OUTPUT = (
    "\nThe command pipes through tail/head: the pipe may have consumed the "
    "output and replaced the real exit code — re-run it without the pipe."
)


def _spawn_kwargs() -> dict[str, Any]:
    """Extra ``create_subprocess_exec`` kwargs so the tree can be killed later.

    POSIX: a fresh session (= fresh process group) lets ``os.killpg`` take out
    the shell *and* everything it spawned. Windows has no equivalent that
    ``asyncio`` exposes; ``kill_tree`` uses ``taskkill /T`` there instead.
    On Windows the command also starts below normal priority unless
    ``FLUID_CHILD_NICE=0`` (see ``fluid.tools.priority``); on POSIX the
    handler lowers the new group's priority right after the spawn.
    """
    if sys.platform != "win32":
        return {"start_new_session": True}
    flags = priority_creationflags()
    return {"creationflags": flags} if flags else {}


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and every descendant, then reap it.

    ``proc.kill()`` alone is not enough: the shell is just the root of a tree
    (Git Bash on Windows even nests a second bash), and killing the root
    orphans the grandchildren, which run to completion. So kill the whole
    group (POSIX) or tree (Windows), falling back to ``proc.kill()`` when the
    group is already gone.
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                env=scrub_child_env(),
            )
        except OSError:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def clamp_timeout(value: Any) -> float:
    """The timeout a call gets: the model's number held inside the bounds.

    A negative or zero timeout would kill every command at once; a huge one
    would let a hung command hold the turn for hours. Anything that is not a
    number falls back to the default.
    """
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if timeout != timeout:  # NaN
        return DEFAULT_TIMEOUT_S
    return min(max(timeout, MIN_TIMEOUT_S), MAX_TIMEOUT_S)


class _Capture:
    """The first and last ``MAX_CAPTURE_BYTES // 2`` of a command's output.

    Shared with the reader rather than local to it, so what was read before
    a timeout cancelled the reader is still there to show.
    """

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        half = MAX_CAPTURE_BYTES // 2
        if len(self.head) < half:
            take = min(len(chunk), half - len(self.head))
            self.head += chunk[:take]
            chunk = chunk[take:]
            if not chunk:
                return
        self.tail += chunk
        if len(self.tail) > 2 * half:
            self.dropped += len(self.tail) - half
            del self.tail[:-half]

    def value(self) -> bytes:
        half = MAX_CAPTURE_BYTES // 2
        if len(self.tail) > half:
            self.dropped += len(self.tail) - half
            del self.tail[:-half]
        if not self.tail:
            return bytes(self.head)
        if self.dropped:
            marker = f"\n... [{self.dropped} bytes dropped while streaming] ...\n".encode()
            return bytes(self.head) + marker + bytes(self.tail)
        return bytes(self.head + self.tail)


async def _read_capped(
    stream: asyncio.StreamReader,
    sink: SpillStream | None = None,
    capture: _Capture | None = None,
) -> bytes:
    """Drain ``stream`` keeping the first and last ``MAX_CAPTURE_BYTES // 2``.

    ``communicate()`` buffers everything a command prints, so a runaway
    command (a build in a loop, a log tail) grows the host without bound.
    Only head and tail can reach the model anyway, so the middle is dropped
    from memory as it streams past; ``sink``, when given, writes every chunk
    to the spill file so the middle stays recoverable. Each chunk reaches the
    sink and the capture with no await in between, so a cancellation lands
    between chunks, never inside one.
    """
    capture = capture if capture is not None else _Capture()
    while chunk := await stream.read(65536):
        if sink is not None:
            sink.feed(chunk)
        capture.add(chunk)
    return capture.value()


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    command = input["command"]
    timeout = clamp_timeout(input.get("timeout", DEFAULT_TIMEOUT_S))
    cwd = input.get("cwd") or ctx.cwd
    # The settings stack's ``env`` (ctx.extra["settings_env"], set by the
    # loader) rides on top of the process environment for the child only;
    # os.environ itself is never changed. The inherited part is scrubbed of
    # host secrets first (see fluid.tools.child_env); settings env is a
    # deliberate hand-over and is layered on unchanged.
    env = layered_env(ctx.extra.get("settings_env"), base=scrub_child_env())
    # Ask the child not to colourise; anything that ignores the hint gets
    # its escapes stripped from the captured output below.
    env.update(NO_COLOR="1", FORCE_COLOR="0")
    # Under Docker agent isolation the command starts through the spawn
    # runner as the agent account; otherwise all three come back unchanged.
    argv, cwd, env = agent_runner.wrap([*resolve_shell().argv_prefix, command], cwd, env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        # No stdin, as the description promises: a command that reads it
        # gets EOF instead of the user's next keystrokes.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
        **_spawn_kwargs(),
    )
    # Below the host's priority so a test run or build cannot starve the
    # host's event loop; it still gets all idle CPU. No-op on Windows (the
    # creationflags above do it there) and when FLUID_CHILD_NICE=0.
    lower_process_group_priority(proc.pid)
    # And the first thing the kernel kills when the host runs out of memory.
    raise_oom_score_adj(proc.pid)
    assert proc.stdout is not None
    capture = _Capture()
    timed_out = False
    # The spill file is the record of what ran: the stream writes the whole
    # output as it arrives (nothing on disk until it outgrows the inline cap)
    # and keeps the file on every outcome, a timeout or an exception included.
    # Only a cancelled turn deletes it, as the block unwinds.
    with SpillStream(ctx, "Bash", open_after=_TRUNCATE_LIMIT, transform=strip_ansi) as sink:
        try:
            await asyncio.wait_for(_read_capped(proc.stdout, sink, capture), timeout=timeout)
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
        finally:
            # Every way out, a cancelled turn included, kills what still runs:
            # nothing downstream would ever reap it.
            if proc.returncode is None:
                await kill_tree(proc)
        if timed_out:
            # What the command wrote just before the kill is still in the pipe.
            try:
                await asyncio.wait_for(
                    _read_capped(proc.stdout, sink, capture), timeout=_DRAIN_AFTER_KILL_S)
            except (TimeoutError, OSError):
                pass
        text = cut_streamed(
            strip_ansi(capture.value().decode(errors="replace")),
            _TRUNCATE_LIMIT, sink, head_share=_HEAD_SHARE,
            what="output up to the timeout" if timed_out else "full output",
        )
    if timed_out:
        # Say what the retry should change rather than leaving the model to
        # re-run the same command, and show what it printed before the kill:
        # the error that explains a hang is usually the last thing printed.
        advice = (
            f"command timed out after {timeout:g}s and was killed. Raise "
            "the timeout parameter for a genuinely long command, narrow "
            "the command's scope, or use Monitor for a long-running job "
            "(serves=true only for a service meant to stay up). Do not "
            "append & or poll for completion. Inspect any partial effects "
            "before retrying a command that may have changed state."
        )
        if text.strip():
            advice += "\n" + fence_shell_output(
                text, "Bash partial output (command timed out; output up to the timeout)")
        return ToolOutcome(text=advice, is_error=True)
    code = proc.returncode or 0
    if not text.strip():
        # A bare "(no output)" is the one result that says nothing about what
        # happened: a silent success and a silent failure read identically, and
        # a `gh pr merge` that already went through invites a retry. Whenever
        # there is no output to judge by, the status is the whole answer.
        note = (
            _PIPE_ATE_OUTPUT
            if code == 0 and _OUTPUT_PIPE_RE.search(command)
            else ""
        )
        return ToolOutcome(
            text=f"(no output) [exit code {code}]{note}",
            is_error=bool(code),
            soft_error=bool(code),
            shell_result=(text, code),
        )
    if code:
        return ToolOutcome(
            text=f"{text.rstrip()}\n[exit code {code}]",
            is_error=True,
            soft_error=True,
            shell_result=(text, code),
        )
    return ToolOutcome(text=text, shell_result=(text, code))
