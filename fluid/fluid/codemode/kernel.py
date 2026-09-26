"""Kernel protocol — the pluggable execution boundary for code-mode.

A ``Kernel`` runs model-authored Python in an isolated, *persistent* namespace
and mediates every tool call back to the host. v1 is the subprocess kernel
(``subprocess_kernel.py``); a container/gVisor kernel is a roadmap item behind
this same protocol.

The protocol is deliberately tiny: ``start`` / ``exec`` / ``stop``. ``exec``
takes an ``on_host_call`` coroutine — the kernel invokes it for each in-sandbox
tool call and blocks the kernel until it returns a ``HostReply``. That callback
is where the host runs the SAME permission/hook gate the JSON tool path uses,
which is the whole point: prompt injection inside the kernel is modeled as
arbitrary code, so the hard gate lives host-side, never in the kernel.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fluid.tools.registry import ContentWindow, ToolImage


@dataclass(frozen=True)
class HostReply:
    """Host's answer to one in-kernel tool call.

    Exactly one of the three outcomes applies:
    - ``result``  — the tool's text output (delivered to the kernel);
    - ``error``   — the tool failed / was unknown (raised as ``ToolError``);
    - ``denied``  — the permission gate rejected it (raised as ``HostCallDenied``).

    ``images`` carries any binary image output the tool produced (a ``Read`` of a
    PNG, an MCP screenshot). It stays **host-side**: the kernel only ever receives
    ``result`` text — in-sandbox the call returns the ``[image: …]`` caption — and
    the host attaches the images to the enclosing cell's tool result so the model
    can actually see them.

    ``content_id`` is the tool's opaque identity for ``result`` (see
    ``ToolOutcome.content_id``): equal ids mean byte-identical payloads. The
    kernel uses it to recognise a repeat read of an unchanged file and answer
    with a pointer at the ``ResultRef`` it already holds.

    ``window_of`` (see ``ToolOutcome.window_of``) extends that to a *narrower*
    match: ``result`` is a slice of a bigger payload, so if the kernel is
    holding that bigger payload the window can be sliced out of it instead of
    fetched again.
    """

    result: str | None = None
    error: str | None = None
    denied: bool = False
    message: str | None = None
    images: tuple[ToolImage, ...] = ()
    content_id: str | None = None
    window_of: ContentWindow | None = None
    error_code: str | None = None
    shell_exit_code: int | None = None


@dataclass(frozen=True)
class KernelResult:
    """Outcome of one ``exec``.

    ``value_repr`` is the repr of the final expression (REPL-style); ``error``
    holds a one-line exception summary if the cell raised. ``host_calls`` counts
    the tool calls made — used for the "task completed using ≥N tools" exit
    criterion and for observability.

    ``notes`` is kernel-lifecycle news the cell itself cannot see: it carries
    the "this kernel restarted since your last cell" notice, which is the only
    warning a model gets that names bound in earlier turns may be gone. It
    rides out even on a cell that succeeded.
    """

    stdout: str = ""
    value_repr: str = ""
    error: str | None = None
    host_calls: int = 0
    notes: str = ""


# The host-call callback: (tool_name, kwargs) -> HostReply. Awaited by the
# kernel; its time is not charged against the cell's *compute* budget, but a
# single call is still bounded (``KernelConfig.host_call_timeout``), so a hung
# MCP call cannot hold the cell, and the turn, open.
HostCall = Callable[[str, dict], Awaitable[HostReply]]

# Seconds the host has spent so far in THIS cell waiting on a human (a parked
# permission prompt, an MRTR question). The kernel credits it back against the
# host-call bound, so an approval a user takes ten minutes to answer never
# looks like a hung tool. Optional: without it, human waits are charged.
HostWaitCredit = Callable[[], float]


@runtime_checkable
class Kernel(Protocol):
    async def start(self) -> None: ...

    async def exec(
        self,
        code: str,
        *,
        timeout: float,
        on_host_call: HostCall,
        host_wait_credit: HostWaitCredit | None = None,
    ) -> KernelResult: ...

    async def stop(self) -> None: ...
