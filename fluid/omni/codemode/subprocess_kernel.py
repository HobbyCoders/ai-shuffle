"""Subprocess kernel v1 — a persistent ``python -I -S`` child over a JSON pipe.

Honestly a *soft* boundary (documented in the plan's threat model): ``-I -S``
isolation, an import allowlist, a network guard, and a memory rlimit inside the
child; the real enforcement is the host permission gate reached through
``on_host_call``. The kernel namespace persists across ``exec`` calls, so the
model accumulates state — until a wall-clock timeout forces a kill+restart, at
which point the namespace is lost and that is reported as the exec error.

Timeout accounting only charges *kernel compute* time: each read segment (the
time the child spends running until it either finishes or asks for a tool) is
deducted from the budget, but the time the host spends servicing a host call —
which can be an arbitrarily long parked permission prompt — is not.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
from dataclasses import dataclass, field

from omni.codemode.kernel import HostCall, HostReply, KernelResult
from omni.codemode.vfs import DEFAULT_ALLOWLIST

_RUNNER = os.path.join(os.path.dirname(__file__), "_runner.py")
_GB = 1024 * 1024 * 1024


@dataclass
class KernelConfig:
    allowlist: list[str] | None = None
    mem_limit_bytes: int = _GB
    allow_network: bool = False
    ref_threshold: int = 8192
    stdout_cap: int = 16384
    python: str = field(default_factory=lambda: sys.executable)

    def boot_payload(self) -> dict:
        return {
            "allowlist": list(self.allowlist) if self.allowlist else list(DEFAULT_ALLOWLIST),
            "mem_limit_bytes": self.mem_limit_bytes,
            "allow_network": self.allow_network,
            "ref_threshold": self.ref_threshold,
            "stdout_cap": self.stdout_cap,
        }


class SubprocessKernel:
    def __init__(self, config: KernelConfig | None = None) -> None:
        self.config = config or KernelConfig()
        self._proc: asyncio.subprocess.Process | None = None
        self._vfs_sources: dict[str, str] = {}

    # ── lifecycle ──
    async def start(self) -> None:
        await self._spawn()

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.config.python, "-I", "-S", _RUNNER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._send({"kind": "boot", "config": self.config.boot_payload()})
        await self._read_frame()  # {"kind": "ready"}
        if self._vfs_sources:
            await self._send({"kind": "set_vfs", "sources": self._vfs_sources})
            await self._read_frame()

    async def set_vfs(self, sources: dict[str, str]) -> None:
        self._vfs_sources = dict(sources)
        if self._proc is not None and self._proc.returncode is None:
            await self._send({"kind": "set_vfs", "sources": self._vfs_sources})
            await self._read_frame()

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.returncode is None:
                await self._send({"kind": "shutdown"})
        except Exception:
            pass
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

    async def _restart(self) -> None:
        await self._kill()
        await self._spawn()

    # ── exec ──
    async def exec(
        self, code: str, *, timeout: float, on_host_call: HostCall
    ) -> KernelResult:
        if self._proc is None or self._proc.returncode is not None:
            await self._spawn()
        await self._send({"kind": "exec", "code": code})

        loop = asyncio.get_running_loop()
        host_calls = 0
        remaining = max(timeout, 0.001)
        while True:
            started = loop.time()
            try:
                frame = await asyncio.wait_for(self._read_frame(), remaining)
            except asyncio.TimeoutError:
                await self._restart()
                return KernelResult(
                    error=(
                        f"execution exceeded {timeout:.1f}s wall clock; "
                        "kernel restarted (namespace lost)"
                    ),
                    host_calls=host_calls,
                )
            except (asyncio.IncompleteReadError, ConnectionError):
                await self._restart()
                return KernelResult(
                    error="kernel died mid-exec (likely OOM / rlimit); restarted",
                    host_calls=host_calls,
                )
            remaining -= loop.time() - started
            if remaining <= 0:
                remaining = 0.001

            kind = frame.get("kind") if frame else None
            if kind == "host_call":
                host_calls += 1
                reply = await on_host_call(frame["name"], frame.get("kwargs") or {})
                await self._send(_reply_frame(reply))  # host-service time is not charged
            elif kind == "done":
                return KernelResult(
                    stdout=frame.get("stdout", ""),
                    value_repr=frame.get("value_repr", ""),
                    error=frame.get("error"),
                    host_calls=host_calls,
                )
            else:  # unexpected EOF or malformed frame
                await self._restart()
                return KernelResult(error="kernel produced no result", host_calls=host_calls)

    # ── frame IO ──
    async def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = _dumps(obj)
        self._proc.stdin.write(struct.pack(">I", len(body)) + body)
        await self._proc.stdin.drain()

    async def _read_frame(self) -> dict | None:
        assert self._proc is not None and self._proc.stdout is not None
        header = await self._proc.stdout.readexactly(4)
        (length,) = struct.unpack(">I", header)
        body = await self._proc.stdout.readexactly(length)
        return _loads(body)


def _reply_frame(reply: HostReply) -> dict:
    if reply.denied:
        return {"kind": "host_reply", "denied": True, "message": reply.message}
    if reply.error is not None:
        return {"kind": "host_reply", "error": reply.error}
    return {"kind": "host_reply", "result": reply.result}


def _dumps(obj: dict) -> bytes:
    import json

    return json.dumps(obj).encode("utf-8")


def _loads(body: bytes) -> dict:
    import json

    return json.loads(body.decode("utf-8"))
