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
which can be an arbitrarily long parked permission prompt — is not. A single
host call is nonetheless bounded (``KernelConfig.host_call_timeout``, and never
below the cell's own declared timeout), so a hung MCP call cannot hold the cell
open. Time the host spent
waiting on a *human* is credited back, so an approval nobody answers for ten
minutes is never mistaken for a hung tool.

On Linux the child can run under an OS-level sandbox (bubblewrap: private
user/pid/net namespaces, an empty read-only root, a private ``/tmp``), chosen
by ``KernelConfig.os_sandbox`` (``auto``/``required``/``off``, default from
``CODE_MODE_OS_SANDBOX``); see :mod:`fluid.codemode.os_sandbox`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import struct
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fluid.codemode import os_sandbox
from fluid.codemode.kernel import HostCall, HostReply, KernelResult
from fluid.codemode.truncate import DEFAULT_LIMIT, DEFAULT_STDOUT_CAP
from fluid.codemode.vfs import DEFAULT_ALLOWLIST

_RUNNER = os.path.join(os.path.dirname(__file__), "_runner.py")
_GB = 1024 * 1024 * 1024
# Child stderr kept for the death diagnostic: this many bytes, of which the
# last few lines are quoted.
_STDERR_TAIL_BYTES = 4096
_STDERR_TAIL_LINES = 5

# Environment variables the kernel child never receives. Tools run host-side,
# so nothing in the child needs a credential; a cell that reaches ``os.environ``
# through the real builtins must find nothing worth reading there.
_SENSITIVE_ENV_RE = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE
)
_SENSITIVE_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID", "AZURE_OPENAI_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN", "HUGGINGFACE_HUB_TOKEN",
    "NPM_TOKEN", "PYPI_TOKEN", "DATABASE_URL",
})


def scrub_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` minus every variable whose name looks like a credential."""
    return {
        name: value for name, value in environ.items()
        if name.upper() not in _SENSITIVE_ENV_NAMES
        and not _SENSITIVE_ENV_RE.search(name)
    }


@dataclass
class KernelConfig:
    allowlist: list[str] | None = None
    mem_limit_bytes: int = _GB
    allow_network: bool = False
    allow_fs: bool = False  # direct open()/io/pathlib I/O inside the kernel
    ref_threshold: int = DEFAULT_LIMIT
    stdout_cap: int = DEFAULT_STDOUT_CAP
    # Namespace checkpointing: after each exec the child pickles its picklable
    # variables; on a timeout/OOM restart the host restores them, so kernel
    # death no longer destroys accumulated data (functions/classes defined in
    # cells are not picklable and are reported as dropped).
    checkpoint: bool = True
    checkpoint_max_bytes: int = 8 * 1024 * 1024
    # Wall-clock ceiling for ONE host call, minus any time spent waiting on a
    # human. The effective bound is ``max(cell timeout, this)``, so a cell that
    # asks for a longer timeout gets it; 0 or less restores the old unbounded
    # behaviour. It has to clear the longest legitimate single tool call (a
    # Bash command may run for ten minutes), which is why it is not simply the
    # cell's declared timeout — but it must exist, or a hung MCP call holds the
    # turn open until the session ends.
    host_call_timeout: float = 900.0
    python: str = field(default_factory=lambda: sys.executable)
    # OS-level sandbox for the child (``auto``/``required``/``off``; see
    # ``fluid.codemode.os_sandbox``). The host app's ``code_mode_os_sandbox``
    # setting reads the same ``CODE_MODE_OS_SANDBOX`` variable.
    os_sandbox: str = field(default_factory=os_sandbox.default_mode)

    def boot_payload(self) -> dict:
        return {
            "allowlist": list(self.allowlist) if self.allowlist else list(DEFAULT_ALLOWLIST),
            "mem_limit_bytes": self.mem_limit_bytes,
            "allow_network": self.allow_network,
            "allow_fs": self.allow_fs,
            "ref_threshold": self.ref_threshold,
            "stdout_cap": self.stdout_cap,
            # The child stops pickling once a checkpoint passes this size, so
            # one large variable costs at most this much work per cell.
            "checkpoint_max_bytes": self.checkpoint_max_bytes if self.checkpoint else 0,
        }


def _restart_notice(detail: str) -> str:
    """The "your kernel restarted" line stamped onto the next cell's result.

    Without it a restart between turns leaves no trace: the next cell raises
    ``NameError`` for a name that worked one turn earlier, and the model —
    told by run_python's own description that kernel state persists — has no
    way to read that as a restart.
    """
    if "namespace lost" in detail:
        return (
            "[kernel restarted since your last cell and the namespace could "
            "not be restored: every name bound in earlier cells is gone. "
            "Re-import the tools this cell uses (`from tools import Read, "
            "Bash`) and rebuild any variables you were relying on.]"
        )
    return (
        f"[kernel restarted since your last cell{detail}. Functions and "
        "classes defined in cells never survive a restart; if a name comes "
        "back undefined, re-import the tool or redefine it.]"
    )


class SubprocessKernel:
    def __init__(self, config: KernelConfig | None = None) -> None:
        self.config = config or KernelConfig()
        self._proc: asyncio.subprocess.Process | None = None
        self._vfs_sources: dict[str, str] = {}
        self._skill_sources: dict[str, str] = {}
        self._job_handle: int | None = None  # Windows Job Object (memory limit)
        self._ckpt_vars: dict[str, str] | None = None  # last namespace checkpoint
        self._ckpt_dropped: list[str] = []  # unpicklables at checkpoint time
        # Tool/skill stubs the namespace had imported. They never pickle as
        # data, so the restore re-imports them by name instead — which is the
        # difference between a restarted kernel the model can keep using and
        # one that answers `Bash(...)` with "Did you mean: 'hash'?".
        self._ckpt_tools: list[str] = []
        self._ckpt_skills: list[str] = []
        # A restart the model has not been told about yet. Set on every
        # restart, cleared once it has ridden out on a cell result — the exec
        # paths that already inline it in their error clear it themselves.
        self._restart_note = ""
        self._ever_started = False
        # The child's stderr, drained as it arrives (a full pipe would block
        # the child) into a bounded tail kept for the death diagnostic.
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = bytearray()
        # Per-process framing nonce (SE-2). Generated fresh on every spawn and
        # handed to the child in the boot frame; the child stamps it onto every
        # child→host frame and the host drops any frame that lacks it, so a cell
        # that reaches the raw pipe cannot mint a frame the host will act on
        # without also extracting the nonce from the child's closures.
        self._nonce = ""
        # Whether the live child runs under the OS sandbox (bwrap), and why it
        # does not when it does not.
        self.sandboxed = False
        self.sandbox_note = ""
        # ``required`` could not start the sandbox: the refusal, until a
        # spawn succeeds. The kernel is down while it is set, and every cell
        # error in that state is the host's, not the code's.
        self.refusal = ""

    # ── lifecycle ──
    async def start(self) -> None:
        try:
            await self._spawn()
        except os_sandbox.OsSandboxUnavailable as exc:
            # ``required`` and no sandbox: do not start. Every exec retries the
            # spawn and returns this refusal as the cell's error, which is
            # where the model and the operator will see it.
            self.refusal = str(exc)

    async def _spawn(self) -> None:
        # The first call runs the sandbox self-test (a short subprocess), so
        # it is kept off the event loop; later calls hit its cache.
        plan = await asyncio.to_thread(
            os_sandbox.plan_spawn,
            self.config.os_sandbox,
            python=self.config.python,
            runner=_RUNNER,
            base_env=scrub_env(os.environ),
            allow_network=self.config.allow_network,
            allow_fs=self.config.allow_fs,
        )
        try:
            await self._launch(plan)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            if not plan.sandboxed:
                raise
            # The self-test passed, yet the sandboxed child died before its
            # first frame. When bwrap itself refused (namespace exhaustion, a
            # changed AppArmor policy) that is the host, not the cell: ``auto``
            # starts this kernel without the sandbox, ``required`` refuses
            # with bwrap's reason. Anything else fails with the child's own.
            proc = self._proc
            stderr = await self._reap_unstarted(proc)
            code = proc.returncode if proc is not None else None
            await self._kill()
            if not os_sandbox.is_bwrap_refusal(code, stderr):
                raise RuntimeError(
                    "code-mode kernel failed to start under the OS sandbox: "
                    + self._death_note(proc)
                ) from exc
            lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
            plan = os_sandbox.plan_after_refusal(
                self.config.os_sandbox,
                python=self.config.python,
                runner=_RUNNER,
                base_env=scrub_env(os.environ),
                reason=" | ".join(lines[-3:]),
            )
            await self._launch(plan)

    async def _reap_unstarted(self, proc: asyncio.subprocess.Process | None) -> str:
        """Wait for a child that died before its first frame; its stderr."""
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, Exception):
                pass
        drain = self._stderr_task
        if drain is not None:
            try:
                await asyncio.wait_for(asyncio.shield(drain), timeout=2)
            except (TimeoutError, Exception):
                pass
        return self._stderr_tail.decode("utf-8", errors="replace")

    async def _launch(self, plan: os_sandbox.SpawnPlan) -> None:
        spawn_kwargs: dict = {}
        if sys.platform != "win32":
            # Own process group, so a kill reaches whatever the cell spawned
            # through an allowed route; on Windows the Job Object does this.
            # Under bwrap the group holds the outer bwrap; killing it tears
            # down the sandbox's pid namespace (``--die-with-parent``).
            spawn_kwargs["start_new_session"] = True
        self.sandboxed, self.sandbox_note = plan.sandboxed, plan.reason
        self._proc = await asyncio.create_subprocess_exec(
            *plan.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=plan.env,
            **spawn_kwargs,
        )
        self._stderr_tail = bytearray()
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(self._proc), name="fluid-kernel-stderr"
        )
        # POSIX enforces the memory limit in-child via rlimit; on Windows that
        # is a no-op, so cap the child from the host with a Job Object.
        if sys.platform == "win32" and self.config.mem_limit_bytes:
            self._job_handle = _win_assign_memory_job(
                self._proc.pid, self.config.mem_limit_bytes
            )
        self._nonce = secrets.token_hex(16)
        boot_config = {**self.config.boot_payload(), "nonce": self._nonce}
        await self._send({"kind": "boot", "config": boot_config})
        await self._read_frame()  # {"kind": "ready"}
        self._ever_started = True
        self.refusal = ""
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

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Read the child's stderr for as long as it lives, keeping the tail."""
        stream = proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                self._stderr_tail += chunk
                if len(self._stderr_tail) > _STDERR_TAIL_BYTES:
                    del self._stderr_tail[:-_STDERR_TAIL_BYTES]
        except (asyncio.CancelledError, OSError, ValueError):
            return

    def _death_note(self, proc: asyncio.subprocess.Process | None) -> str:
        """Why the child is gone, from its exit status and stderr tail.

        An exit the runner chose (a cell called ``sys.exit``), a signal, and
        a memory-cap kill are not all "likely OOM"; the status tells them
        apart, and the last lines of stderr say what the child printed on its
        way out.
        """
        code = proc.returncode if proc is not None else None
        if code is not None and self.sandboxed and code > 128:
            # bwrap reports a signalled child as 128 + signal number.
            code = 128 - code
        if code is None:
            reason = "kernel died mid-exec"
        elif code == -9:
            reason = "kernel was killed by SIGKILL (the OS or the memory cap)"
        elif code < 0:
            reason = f"kernel was killed by signal {-code}"
        else:
            reason = f"kernel exited with status {code}"
        tail = self._stderr_tail.decode("utf-8", errors="replace").strip()
        if tail:
            lines = tail.splitlines()[-_STDERR_TAIL_LINES:]
            reason += "; stderr: " + " | ".join(line.strip() for line in lines)
        return reason

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        job, self._job_handle = self._job_handle, None
        drain, self._stderr_task = self._stderr_task, None
        if proc is not None and proc.returncode is None:
            if sys.platform != "win32":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, Exception):
                pass
        if drain is not None:
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
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
        self._ckpt_tools = list(frame.get("tools") or [])
        self._ckpt_skills = list(frame.get("skills") or [])
        # The child already stopped at the budget; base64 widens the pickled
        # bytes by a third, so the wire size is allowed that much over it.
        wire_cap = self.config.checkpoint_max_bytes * 4 // 3 + 1024
        if sum(len(v) for v in vars_map.values()) <= wire_cap:
            self._ckpt_vars = vars_map
            # Both lists name variables a restore will not bring back; the
            # note the model reads after a restart must say so for each.
            self._ckpt_dropped = [
                *(frame.get("dropped") or []),
                *(frame.get("oversized") or []),
            ]
        # else: oversized — keep the previous checkpoint

    async def _restart_and_restore(self) -> str:
        """Restart the kernel and restore the last checkpoint.

        Returns the note appended to the exec error message, so the model
        knows exactly what state survived, and arms :attr:`_restart_note` for
        the NEXT cell (see :func:`_restart_notice`). When ``required`` can no
        longer start the sandbox the kernel stays down and the note says why;
        the next exec retries and returns the refusal as its error.
        """
        try:
            await self._restart()
            detail = await self._restore()
        except os_sandbox.OsSandboxUnavailable as exc:
            self._restart_note = ""
            self.refusal = str(exc)
            return f"; the kernel could not be restarted: {exc}"
        self._restart_note = _restart_notice(detail)
        return detail

    async def _restore(self) -> str:
        have = self._ckpt_vars or self._ckpt_tools or self._ckpt_skills
        if not self.config.checkpoint or not have:
            return " (namespace lost)"
        try:
            await self._send({
                "kind": "restore",
                "vars": self._ckpt_vars or {},
                "tools": self._ckpt_tools,
                "skills": self._ckpt_skills,
            })
            frame = await asyncio.wait_for(self._read_frame(), 30)
        except Exception:
            await self._restart()
            return " (namespace lost)"
        if not frame or frame.get("kind") != "restored":
            return " (namespace lost)"
        restored = list(frame.get("restored") or [])
        reimported = list(frame.get("reimported") or [])
        dropped = self._ckpt_dropped + list(frame.get("dropped") or [])
        note = f"; namespace restored from last checkpoint ({len(restored)} vars"
        if reimported:
            note += f"; re-imported: {', '.join(sorted(set(reimported)))}"
        if dropped:
            note += f"; not restorable: {', '.join(sorted(set(dropped)))}"
        return note + ")"

    # ── exec ──
    def _host_bound(self, timeout: float) -> float:
        """Seconds one host call may take, 0 for unbounded."""
        ceiling = float(self.config.host_call_timeout or 0)
        if ceiling <= 0:
            return 0.0
        return max(float(timeout), ceiling)

    async def _call_host(
        self,
        on_host_call: HostCall,
        name: str,
        kwargs: dict,
        bound: float,
        credit: Any,
    ) -> HostReply:
        """Await one host call under ``bound`` seconds of non-human wall clock.

        On expiry the call is cancelled and a ``TimeoutError`` is raised; the
        caller answers the child with an error reply, so the cell aborts with a
        traceback and its committed-call manifest instead of the kernel being
        torn down. Time already spent waiting on a human is credited back as it
        accrues, which is what keeps an in-kernel AskUser or a parked
        permission prompt from being cancelled as a hang.
        """
        if bound <= 0:
            return await on_host_call(name, kwargs)
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(on_host_call(name, kwargs))
        deadline = loop.time() + bound
        credited = 0.0
        try:
            while True:
                wait_for = deadline - loop.time()
                if wait_for > 0:
                    done, _ = await asyncio.wait({task}, timeout=wait_for)
                    if task in done:
                        return task.result()
                try:
                    spent = float(credit() or 0.0) if credit is not None else 0.0
                except Exception:  # noqa: BLE001 - a clock must never break a call
                    spent = 0.0
                if spent > credited:
                    deadline += spent - credited
                    credited = spent
                    continue
                raise TimeoutError
        finally:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait({task}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass

    async def exec(
        self,
        code: str,
        *,
        timeout: float,
        on_host_call: HostCall,
        host_wait_credit: Any = None,
    ) -> KernelResult:
        if self._proc is None or self._proc.returncode is not None:
            # The kernel died between turns (eviction, an OOM kill, a host
            # restart). Restore the checkpoint and arm the notice rather than
            # hand the next cell an empty namespace with no explanation.
            try:
                if self._ever_started:
                    await self._restart()
                    self._restart_note = _restart_notice(await self._restore())
                else:
                    await self._spawn()
            except os_sandbox.OsSandboxUnavailable as exc:
                self.refusal = str(exc)
                return KernelResult(error=str(exc))
        await self._send({"kind": "exec", "code": code})

        loop = asyncio.get_running_loop()
        host_calls = 0
        remaining = max(timeout, 0.001)
        host_bound = self._host_bound(timeout)
        while True:
            started = loop.time()
            try:
                frame = await asyncio.wait_for(self._read_frame(), remaining)
            except TimeoutError:
                note = await self._restart_and_restore()
                self._restart_note = ""  # said here; not repeated next cell
                return KernelResult(
                    error=(
                        f"execution exceeded {timeout:.1f}s wall clock; "
                        f"kernel restarted{note}"
                    ),
                    host_calls=host_calls,
                )
            except (asyncio.IncompleteReadError, ConnectionError):
                proc = self._proc
                if proc is not None and proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1)
                    except (TimeoutError, Exception):
                        pass
                reason = self._death_note(proc)
                note = await self._restart_and_restore()
                self._restart_note = ""
                return KernelResult(
                    error=f"{reason}; restarted{note}", host_calls=host_calls
                )
            except asyncio.CancelledError:
                # The turn was interrupted while the cell ran. The child is
                # still executing it and would answer the NEXT exec with this
                # cell's result, so the pipe is only kept in step by
                # restarting the kernel; the checkpoint restores its state.
                await self._restart_and_restore()
                raise
            remaining -= loop.time() - started
            if remaining <= 0:
                remaining = 0.001

            kind = frame.get("kind") if frame else None
            if kind == "host_call":
                host_calls += 1
                name = frame["name"]
                try:
                    reply = await self._call_host(
                        on_host_call, name, frame.get("kwargs") or {},
                        host_bound, host_wait_credit,
                    )
                except TimeoutError:
                    reply = HostReply(error=(
                        f"{name} did not return within {host_bound:.0f}s and "
                        "was cancelled — that is this cell's ceiling for one "
                        "tool call (time spent waiting on a human does not "
                        "count). The tool is hung or the target is not "
                        "responding: do not simply re-issue the same call. "
                        "Try a different route, or run it under a Monitor so "
                        "the wait happens outside the turn."
                    ))
                # Host-service time is not charged against the compute budget.
                await self._send(_reply_frame(reply))
            elif kind == "done":
                # Read the pending notice BEFORE checkpointing: a checkpoint
                # that fails restarts the kernel, and that restart belongs to
                # the NEXT cell's notice, not to this one's.
                notes, self._restart_note = self._restart_note, ""
                if self.config.checkpoint:
                    await self._take_checkpoint()
                return KernelResult(
                    stdout=frame.get("stdout", ""),
                    value_repr=frame.get("value_repr", ""),
                    error=frame.get("error"),
                    host_calls=host_calls,
                    notes=notes,
                )
            else:  # unexpected EOF or malformed frame
                note = await self._restart_and_restore()
                self._restart_note = ""
                return KernelResult(
                    error=f"kernel produced no result{note}", host_calls=host_calls
                )

    # ── frame IO ──
    async def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps(obj).encode("utf-8")
        self._proc.stdin.write(struct.pack(">I", len(body)) + body)
        await self._proc.stdin.drain()

    async def _read_frame(self) -> dict | None:
        assert self._proc is not None and self._proc.stdout is not None
        header = await self._proc.stdout.readexactly(4)
        (length,) = struct.unpack(">I", header)
        body = await self._proc.stdout.readexactly(length)
        frame = json.loads(body.decode("utf-8"))
        # SE-2: every genuine child→host frame carries the boot nonce. A frame
        # without it is a cell-forged frame (or a desynced pipe from interleaved
        # forged bytes); refuse to act on it and let the caller restart the
        # kernel rather than trust fabricated output or a fabricated host_call.
        if self._nonce and (not isinstance(frame, dict) or frame.get("_n") != self._nonce):
            raise ConnectionError("kernel frame failed the nonce check (forged or desynced pipe)")
        if isinstance(frame, dict):
            frame.pop("_n", None)
        return frame


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
        return {"kind": "host_reply", "error": reply.error, "error_code": reply.error_code}
    frame = {"kind": "host_reply", "result": reply.result}
    if reply.content_id:
        # Only the success path carries it, and only when the tool claimed one —
        # the kernel keys its "already delivered this payload" map on it.
        frame["content_id"] = reply.content_id
    if reply.window_of is not None:
        # Same success-only rule: the kernel checks it against the payloads it
        # already holds, so it is meaningless on a denial or an error.
        frame["window_of"] = {
            "content_id": reply.window_of.content_id,
            "start": reply.window_of.start,
            "stop": reply.window_of.stop,
        }
    return frame

