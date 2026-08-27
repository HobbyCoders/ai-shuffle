"""Subprocess kernel v1 — a persistent ``python -I -S`` child over a JSON pipe.

Honestly a *soft* boundary (documented in the plan's threat model): ``-I -S``
isolation, an import allowlist, network + direct-file-I/O guards inside the
child, and a memory cap (POSIX rlimit in-child; a Job Object assigned from the
host on Windows, where rlimit is unavailable); the real enforcement is the host
permission gate reached through ``on_host_call``. The kernel namespace persists
across ``exec`` calls, so the model accumulates state; after each exec the
child checkpoints its picklable variables, and a timeout/OOM kill+restart
restores them (cell-defined functions/classes are not picklable — the exec
error reports exactly what survived and what didn't).

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

from fluid.codemode.kernel import HostCall, HostReply, KernelResult
from fluid.codemode.vfs import DEFAULT_ALLOWLIST

_RUNNER = os.path.join(os.path.dirname(__file__), "_runner.py")
_GB = 1024 * 1024 * 1024


@dataclass
class KernelConfig:
    allowlist: list[str] | None = None
    mem_limit_bytes: int = _GB
    allow_network: bool = False
    allow_fs: bool = False  # direct open()/io/pathlib I/O inside the kernel
    ref_threshold: int = 8192
    stdout_cap: int = 16384
    # Namespace checkpointing: after each exec the child pickles its picklable
    # variables; on a timeout/OOM restart the host restores them, so kernel
    # death no longer destroys accumulated data (functions/classes defined in
    # cells are not picklable and are reported as dropped).
    checkpoint: bool = True
    checkpoint_max_bytes: int = 8 * 1024 * 1024
    python: str = field(default_factory=lambda: sys.executable)

    def boot_payload(self) -> dict:
        return {
            "allowlist": list(self.allowlist) if self.allowlist else list(DEFAULT_ALLOWLIST),
            "mem_limit_bytes": self.mem_limit_bytes,
            "allow_network": self.allow_network,
            "allow_fs": self.allow_fs,
            "ref_threshold": self.ref_threshold,
            "stdout_cap": self.stdout_cap,
        }


class SubprocessKernel:
    def __init__(self, config: KernelConfig | None = None) -> None:
        self.config = config or KernelConfig()
        self._proc: asyncio.subprocess.Process | None = None
        self._vfs_sources: dict[str, str] = {}
        self._skill_sources: dict[str, str] = {}
        self._job_handle: int | None = None  # Windows Job Object (memory limit)
        self._ckpt_vars: dict[str, str] | None = None  # last namespace checkpoint
        self._ckpt_dropped: list[str] = []  # unpicklables at checkpoint time

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
        # POSIX enforces the memory limit in-child via rlimit; on Windows that
        # is a no-op, so cap the child from the host with a Job Object.
        if sys.platform == "win32" and self.config.mem_limit_bytes:
            self._job_handle = _win_assign_memory_job(
                self._proc.pid, self.config.mem_limit_bytes
            )
        await self._send({"kind": "boot", "config": self.config.boot_payload()})
        await self._read_frame()  # {"kind": "ready"}
        if self._vfs_sources or self._skill_sources:
            await self._send_vfs()

    async def _send_vfs(self) -> None:
        await self._send({
            "kind": "set_vfs",
            "sources": self._vfs_sources,
            "skills": self._skill_sources,
        })
        await self._read_frame()

    async def set_vfs(
        self, sources: dict[str, str], skills: dict[str, str] | None = None
    ) -> None:
        self._vfs_sources = dict(sources)
        if skills is not None:
            self._skill_sources = dict(skills)
        if self._proc is not None and self._proc.returncode is None:
            await self._send_vfs()

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
        job, self._job_handle = self._job_handle, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, Exception):
                pass
        if job is not None:
            _win_close_handle(job)  # KILL_ON_JOB_CLOSE reaps any survivor

    async def _restart(self) -> None:
        await self._kill()
        await self._spawn()

    # ── namespace checkpoint / restore ──
    async def _take_checkpoint(self) -> None:
        """Snapshot the child namespace after an exec (best-effort).

        A failed/oversized snapshot keeps the previous good one; a desynced
        pipe (checkpoint request timed out) forces a restart+restore so the
        frame protocol stays aligned.
        """
        try:
            await self._send({"kind": "checkpoint"})
            frame = await asyncio.wait_for(self._read_frame(), 30)
        except Exception:
            await self._restart_and_restore()
            return
        if not frame or frame.get("kind") != "checkpoint":
            await self._restart_and_restore()
            return
        vars_map = frame.get("vars") or {}
        if sum(len(v) for v in vars_map.values()) <= self.config.checkpoint_max_bytes:
            self._ckpt_vars = vars_map
            self._ckpt_dropped = list(frame.get("dropped") or [])
        # else: oversized — keep the previous checkpoint

    async def _restart_and_restore(self) -> str:
        """Restart the kernel and restore the last checkpoint.

        Returns the note appended to the exec error message, so the model
        knows exactly what state survived.
        """
        await self._restart()
        if not self.config.checkpoint or not self._ckpt_vars:
            return " (namespace lost)"
        try:
            await self._send({"kind": "restore", "vars": self._ckpt_vars})
            frame = await asyncio.wait_for(self._read_frame(), 30)
        except Exception:
            await self._restart()
            return " (namespace lost)"
        if not frame or frame.get("kind") != "restored":
            return " (namespace lost)"
        restored = list(frame.get("restored") or [])
        dropped = self._ckpt_dropped + list(frame.get("dropped") or [])
        note = f"; namespace restored from last checkpoint ({len(restored)} vars"
        if dropped:
            note += f"; not restorable: {', '.join(sorted(set(dropped)))}"
        return note + ")"

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
            except TimeoutError:
                note = await self._restart_and_restore()
                return KernelResult(
                    error=(
                        f"execution exceeded {timeout:.1f}s wall clock; "
                        f"kernel restarted{note}"
                    ),
                    host_calls=host_calls,
                )
            except (asyncio.IncompleteReadError, ConnectionError):
                note = await self._restart_and_restore()
                return KernelResult(
                    error=f"kernel died mid-exec (likely OOM / memory cap); restarted{note}",
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
                if self.config.checkpoint:
                    await self._take_checkpoint()
                return KernelResult(
                    stdout=frame.get("stdout", ""),
                    value_repr=frame.get("value_repr", ""),
                    error=frame.get("error"),
                    host_calls=host_calls,
                )
            else:  # unexpected EOF or malformed frame
                note = await self._restart_and_restore()
                return KernelResult(
                    error=f"kernel produced no result{note}", host_calls=host_calls
                )

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


# ── Windows Job Object memory limit (host-side counterpart of the POSIX rlimit
# the child applies to itself; see _runner._apply_rlimit) ─────────────────────

# Best-effort: returns the job handle to keep alive (limits die with the last
# handle), or None if anything fails — matching the rlimit path's posture.
def _win_assign_memory_job(pid: int, limit_bytes: int) -> int | None:  # pragma: no cover - win32 only
    try:
        import ctypes
        from ctypes import wintypes

        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),  # ULONG_PTR
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = limit_bytes
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            kernel32.CloseHandle(job)
            return None
        assigned = kernel32.AssignProcessToJobObject(job, hproc)
        kernel32.CloseHandle(hproc)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _win_close_handle(handle: int) -> None:  # pragma: no cover - win32 only
    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def _reply_frame(reply: HostReply) -> dict:
    if reply.denied:
        return {"kind": "host_reply", "denied": True, "message": reply.message}
    if reply.error is not None:
        return {"kind": "host_reply", "error": reply.error}
    frame = {"kind": "host_reply", "result": reply.result}
    if reply.content_id:
        # Only the success path carries it, and only when the tool claimed one —
        # the kernel keys its "already delivered this payload" map on it.
        frame["content_id"] = reply.content_id
    return frame


def _dumps(obj: dict) -> bytes:
    import json

    return json.dumps(obj).encode("utf-8")


def _loads(body: bytes) -> dict:
    import json

    return json.loads(body.decode("utf-8"))
