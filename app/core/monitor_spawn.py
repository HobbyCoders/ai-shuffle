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
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Head+tail bound for the completion summary (mirrors the app's general
# large-payload posture; the full buffer stays available via TaskOutput).
_SUMMARY_HEAD = 4_000
_SUMMARY_TAIL = 8_000


def _summarize(lines: list) -> str:
    text = "\n".join(lines)
    if len(text) <= _SUMMARY_HEAD + _SUMMARY_TAIL:
        return text
    dropped = len(text) - _SUMMARY_HEAD - _SUMMARY_TAIL
    return (
        text[:_SUMMARY_HEAD]
        + f"\n... [{dropped} chars truncated] ...\n"
        + text[-_SUMMARY_TAIL:]
    )


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
                summary = _summarize(task.lines) or (
                    f"(no output; exit code {exit_code})"
                )
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

        return BackgroundTaskReceipt(
            task_id=task.task_id,
            description=command[:120],
        )

    return _spawn
