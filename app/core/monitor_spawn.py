"""monitor_spawn — the host-side spawner for fluid's bare Monitor builtin.

Fluid owns the tool's SHAPE (bare name, schema, description); the host owns the
supervision (ruling A13, Task-parity). The native leg injects the callable
returned by :func:`make_fluid_monitor_spawner` as
``ToolCtx.extra["monitor_spawn"]``; without it fluid's Monitor reports itself
unavailable.

The spawner reuses :func:`app.mcp.builtin_tools._spawn_background` — the SAME
detached-subprocess registry the MCP ``Monitor``/``TaskOutput``/``TaskStop``
tools serve — so a fluid-Monitor shell shows up in ``TaskList``, is stoppable,
and is reaped with its session exactly like an MCP-Monitor one. On completion
the output tail becomes the frame ``summary`` and the terminal handoff goes to
:func:`app.core.turn_scheduler.on_task_terminal`, the sole unpark producer —
the same seam a ProviderAgent worker's completion uses — so a parked parent is
woken with the command's output instead of having to poll.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Head+tail bound for the completion summary (mirrors the app's general
# large-payload posture; the full buffer stays available via TaskOutput).
_SUMMARY_HEAD = 4_000
_SUMMARY_TAIL = 8_000

# Bound for the log-file tail folded into a summary whose buffer came back
# empty (see :func:`_log_tail`).
_LOG_TAIL_BYTES = 4_000

# Redirect / tee targets in a monitored command line. Matches `> f`, `>> f`,
# `2> f`, `1>>f` and `| tee [-a] f`; quoted paths keep their spaces. `2>&1`
# does NOT match because a target may not start with `&`.
_REDIRECT_RE = re.compile(
    r"""(?:\d?>>?|\|\s*tee(?:\s+-a)?)\s*"""
    r"""(?P<path>"[^"]+"|'[^']+'|[^\s;|&<>()]+)"""
)

# Redirect targets that are sinks, not logs.
_NULL_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "NUL"})


def monitor_receipt_text(task_id: str) -> str:
    """The receipt every Monitor spawn hands back to the model.

    Fluid's default is the bare fact ("Monitoring started (task X)."), and
    audited sessions read that as an invitation to sleep-poll in foreground
    Bash for a notification already queued for them — 9 of 10 spawns in the
    worst session, 24 minutes of polling. The receipt has to give the
    instruction, mirroring the TaskList nudge that stopped the same loop there.
    Shared with the MCP ``Monitor`` tool so both legs say the same thing.
    """
    return (
        f"Monitoring started (task {task_id}). End the turn now — you will be "
        "woken when it comes to rest."
    )


def _strip_ansi(text: str) -> str:
    """Fluid's ANSI scrubber, imported lazily so this module stays importable
    without the vendored harness on the path."""
    try:
        from fluid.tools.builtins._util import strip_ansi
    except Exception:  # noqa: BLE001 — cosmetics must never break a completion
        return text
    return strip_ansi(text)


def _summarize(lines: list) -> str:
    # Scrub BEFORE slicing: escape sequences are gibberish to the model (and
    # to the UI), and counting them against the head/tail budget evicts real
    # output. Ruling A13 makes the app the supervisor here, so fluid's own
    # supervisor-side strip never runs for these buffers.
    text = _strip_ansi("\n".join(lines))
    if len(text) <= _SUMMARY_HEAD + _SUMMARY_TAIL:
        return text
    dropped = len(text) - _SUMMARY_HEAD - _SUMMARY_TAIL
    return (
        text[:_SUMMARY_HEAD]
        + f"\n... [{dropped} chars truncated] ...\n"
        + text[-_SUMMARY_TAIL:]
    )


def _redirect_targets(command: str) -> List[str]:
    """Ordered, de-duplicated redirect/tee targets named by ``command``."""
    seen: List[str] = []
    for match in _REDIRECT_RE.finditer(command or ""):
        path = (match.group("path") or "").strip().strip("\"'")
        if not path or path in _NULL_SINKS or path in seen:
            continue
        seen.append(path)
    return seen


def _log_tail(command: str, cwd: Optional[str]) -> str:
    """A bounded tail of the log file ``command`` redirected its output into.

    The audited failure: 8 of 9 monitor notifications said "(no output; exit
    code 0)" because the monitored command sent everything to a log file. The
    buffer is legitimately empty in that case, and the one thing the parent
    wants is the end of that file. Redirect targets are the only path the
    monitor can know without guessing, so that is all this reads — the LAST one
    named (the command's own final sink) that exists and is readable.
    """
    targets = _redirect_targets(command)
    for path in reversed(targets):
        candidate = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        try:
            if not os.path.isfile(candidate):
                continue
            size = os.path.getsize(candidate)
            with open(candidate, "rb") as fh:
                if size > _LOG_TAIL_BYTES:
                    fh.seek(size - _LOG_TAIL_BYTES)
                raw = fh.read()
        except OSError:
            continue
        text = _strip_ansi(raw.decode("utf-8", errors="replace")).strip()
        if not text:
            continue
        prefix = "" if size <= _LOG_TAIL_BYTES else "... [earlier lines omitted] ...\n"
        return f"(no buffered output; tail of {path})\n{prefix}{text}"
    return ""


def make_fluid_monitor_spawner(
    session_id: "str | Callable[[], Optional[str]]",
) -> Callable[[Dict[str, Any], Any], Any]:
    """The ``ctx.extra['monitor_spawn']`` callable for fluid's bare Monitor.

    ``session_id`` mirrors :mod:`app.core.task_dispatch`: the caller's APP
    session id, possibly a zero-argument supplier resolved at CALL time (a
    brand-new native session has no id at connect time; a connect-time
    snapshot would stay empty forever).
    """

    async def _spawn(input: Dict[str, Any], ctx: Any) -> Any:
        from fluid.tools.registry import BackgroundTaskReceipt

        command = str((input or {}).get("command") or "").strip()
        if not command:
            from fluid.tools.registry import ToolOutcome

            payload = {"ok": False, "error": "command is required."}
            return ToolOutcome(text=payload["error"], ui=payload, is_error=True)

        caller_session_id: Any = session_id
        if callable(caller_session_id):
            try:
                caller_session_id = caller_session_id()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "monitor_spawn: caller session-id supplier raised",
                    exc_info=True,
                )
                caller_session_id = None

        cwd = input.get("cwd")
        from app.mcp.builtin_tools import _spawn_background

        task = await _spawn_background(command, cwd, caller_session_id)

        if caller_session_id:
            from app.core.task_registry import task_registry
            # OWN-lane admission: without this the watcher's terminal frame is
            # dropped by on_task_terminal's ownership gate as not-own work and a
            # parked parent never wakes.
            task_registry.mark_monitor_shell_task(caller_session_id, task.task_id)

        # Completion watcher: fold the output tail into the registry record and
        # fire the scheduler's terminal seam so a parked parent wakes with the
        # result. Detached — it must outlive this tool call by construction.
        if caller_session_id:
            import asyncio

            async def _on_exit() -> None:
                while task.is_running():
                    await asyncio.sleep(0.5)
                exit_code = task.exit_code
                status = "completed" if exit_code == 0 else "error"
                summary = _summarize(task.lines)
                if not summary:
                    # Empty buffer usually means the command redirected into a
                    # log file; the tail of that file is the result the parent
                    # was woken for.
                    try:
                        summary = _log_tail(command, cwd)
                    except Exception:  # noqa: BLE001 — best-effort nicety
                        logger.debug("monitor_spawn: log-tail read failed", exc_info=True)
                        summary = ""
                if not summary:
                    summary = f"(no output; exit code {exit_code})"
                try:
                    from app.core.task_registry import task_registry

                    task_registry.update_task(
                        caller_session_id, task.task_id, summary=summary
                    )
                except Exception:  # noqa: BLE001 — registry is best-effort
                    logger.debug("monitor_spawn: summary write failed", exc_info=True)
                try:
                    from app.core import turn_scheduler

                    await turn_scheduler.on_task_terminal(caller_session_id, {
                        "task_id": task.task_id,
                        "status": status,
                        "summary": summary,
                        "kind": "shell",
                    })
                except Exception:  # noqa: BLE001 — a wake failure must never raise here
                    logger.warning(
                        "monitor_spawn: terminal handoff failed for task %s",
                        task.task_id, exc_info=True,
                    )

            loop = asyncio.get_running_loop()
            watcher = loop.create_task(_on_exit())
            watcher.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        from fluid.tools.registry import ToolOutcome

        receipt = BackgroundTaskReceipt(
            task_id=task.task_id,
            description=command[:120],
        )
        # A ToolOutcome (not the bare receipt) so the app owns the RECEIPT
        # STRING too: fluid's default renders "Monitoring started (task X)."
        # and audited sessions read that as permission to keep waiting — one
        # sleep-polled in foreground Bash after 9 of 10 spawns. The instruction
        # to park has to be in the receipt itself, the way the TaskList nudge is.
        return ToolOutcome(
            text=monitor_receipt_text(task.task_id),
            background_task=receipt,
        )

    return _spawn
