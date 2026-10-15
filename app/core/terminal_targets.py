"""Owner-selected terminal targets. No agent tool calls this module."""
from __future__ import annotations

import asyncio
import base64
import asyncssh

from app.core import terminals as service
from app.core.terminals import TerminalError
from app.db.async_database import run_sync
from app.network import control, runners, terminal_relay


async def targets(session_id):
    rows = await run_sync(runners.directory)
    result = [{"kind": "worker", "worker_id": row["id"], "name": row["device_name"], "available": True}
              for row in rows["runners"] if not row["is_self"] and row["online"]
              and row.get("capabilities", {}).get("terminal", 0) >= 2]
    if session_id:
        from app.core.chat_access import broker, AccessDenied
        for grant in list(broker.grants.values()):
            if grant.session_id != session_id or grant.kind != "machine":
                continue
            try:
                grant = await broker.get(session_id, grant.id)
            except AccessDenied:
                continue
            if grant.writable:
                result.append({"kind": "ssh", "grant_id": grant.id, "name": grant.label or "Approved SSH machine",
                               "available": True})
    return result


class StreamTerminal(service.Terminal):
    """Remote streams share bounded scrollback, viewers and the socket protocol."""

    def __init__(self, *, session_id, who, cols, rows):
        super().__init__(shell={"name": "remote", "path": ""}, cwd="", folder_source="target",
                         session_id=session_id, created_by=who, worker_id="",
                         cols=service._clamp(cols, service.COLS_RANGE, service.DEFAULT_COLS),
                         rows=service._clamp(rows, service.ROWS_RANGE, service.DEFAULT_ROWS))
        self.task = None

    def refresh_busy(self):
        return False

    def holds_bracketed_paste(self):
        # A remote program can print mode sequences: they are not proof that
        # the remote line discipline will hold a multi-line paste unexecuted.
        return False

    def kill_now(self):
        if self.task:
            self.task.cancel()

    async def kill(self):
        self._closing = True
        self.kill_now()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)


class PeerTerminal(StreamTerminal):
    async def start(self, target, request, options):
        self.target = {"kind": "worker", "worker_id": target.worker_id}
        self.lease = terminal_relay.authorize(request, self.created_by, target.worker_id)
        self.pending_size = None
        try:
            info = (await self.call("open", options=options))["terminal"]
        except BaseException:
            terminal_relay.forget(self.lease)
            raise
        self.shell, self.cwd = info["shell"], info["cwd"]
        self.state = info["state"]
        self.task = asyncio.create_task(self.pump())
        self.task.add_done_callback(lambda _: terminal_relay.forget(self.lease))

    async def call(self, op, **body):
        try:
            result = await control.call(self.target["worker_id"], "terminal",
                                        {"lease": self.lease, "op": op, **body}, timeout=15)
        except Exception as exc:
            raise TerminalError("peer_unavailable", "The terminal worker is unavailable or owner access expired.", 503) from exc
        if "error" in result:
            error = result["error"]
            raise TerminalError(error["code"], error["message"], error["status"])
        return result

    async def pump(self):
        try:
            while True:
                if self.pending_size:
                    cols, rows = self.pending_size
                    self.pending_size = None
                    await self.call("resize", cols=cols, rows=rows)
                reply = await self.call("read")
                data = base64.b64decode(reply["data"], validate=True)
                if data:
                    self._on_output(data)
                state = reply["terminal"]
                self.busy = state["busy"]
                if state["state"] == service.STATE_EXITED:
                    self._on_exit(state["exit_code"] or 0)
                    return
        except (Exception, asyncio.CancelledError):
            self._on_exit(-1)
        finally:
            try:
                await self.call("close")
            except TerminalError:
                pass  # The recipient's lease deadline kills an unreachable PTY.
            terminal_relay.forget(self.lease)

    async def write(self, data):
        if self.state == service.STATE_EXITED:
            raise TerminalError("terminal_exited", "This terminal has exited.", 409)
        await self.call("write", data=base64.b64encode(data).decode("ascii"))

    def resize(self, cols, rows):
        self.cols = service._clamp(cols, service.COLS_RANGE, self.cols)
        self.rows = service._clamp(rows, service.ROWS_RANGE, self.rows)
        self.pending_size = (self.cols, self.rows)


class SSHTerminal(StreamTerminal):
    async def start(self, target):
        from app.core.chat_access import broker, AccessDenied
        self.broker = broker
        try:
            grant = await broker.get(self.session_id, target.grant_id)
            if grant.kind != "machine" or not grant.writable:
                raise AccessDenied("Choose a writable machine grant.")
            self.grant = grant
            self.target = {"kind": "ssh", "grant_id": grant.id}
            # Include connection setup in revocation cancellation, not only the reader.
            opening = asyncio.current_task()
            grant.tasks.add(opening)
            try:
                self.connection = await broker.connection(grant)
                try:
                    self.process = await self.connection.create_process(
                        term_type="xterm-256color", term_size=(self.cols, self.rows), encoding=None, stderr=asyncssh.STDOUT)
                except BaseException:
                    self.connection.close()
                    raise
            finally:
                grant.tasks.discard(opening)
        except AccessDenied as exc:
            raise TerminalError("grant_unavailable", "Choose an active writable SSH machine grant for this chat.", 403) from exc
        except Exception as exc:
            raise TerminalError("ssh_failed", "The approved SSH connection could not be opened.", 502) from exc
        self.shell = "ssh"
        self.state = service.STATE_LIVE
        self.task = asyncio.create_task(self.pump())
        grant.tasks.add(self.task)
        # Revocation may cancel the task before its coroutine first runs.
        self.task.add_done_callback(self._finished)

    def _finished(self, task):
        self.connection.close()
        self.grant.tasks.discard(task)
        if self.state != service.STATE_EXITED:
            self._on_exit(-1)

    async def pump(self):
        try:
            while True:
                await self.broker.get(self.session_id, self.grant.id)
                try:
                    data = await asyncio.wait_for(self.process.stdout.read(service.OUTPUT_FRAME_BYTES), 1)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break
                self._on_output(data)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.connection.close()
            self.grant.tasks.discard(asyncio.current_task())
            self._on_exit(self.process.exit_status if self.process.exit_status is not None else -1)

    async def write(self, data):
        from app.core.chat_access import AccessDenied
        try:
            await self.broker.get(self.session_id, self.grant.id)
            if self.state == service.STATE_EXITED:
                raise AccessDenied("Terminal exited")
            self.process.stdin.write(data)
            await asyncio.wait_for(self.process.stdin.drain(), service.WRITE_TIMEOUT_SECONDS)
        except Exception as exc:
            raise TerminalError("grant_unavailable", "SSH access expired or the terminal closed.", 403) from exc

    def resize(self, cols, rows):
        self.cols = service._clamp(cols, service.COLS_RANGE, self.cols)
        self.rows = service._clamp(rows, service.ROWS_RANGE, self.rows)
        if self.state != service.STATE_EXITED:
            self.process.change_terminal_size(self.cols, self.rows)


async def create(registry, body, who, request):
    target = body.target
    if registry.count >= registry.limit:
        raise TerminalError("limit_reached", "Close a terminal before opening another.", 409)
    choices = await targets(body.session_id)
    match = any(row["kind"] == target.kind and
                (row.get("worker_id") == target.worker_id if target.kind == "worker"
                 else row.get("grant_id") == target.grant_id) for row in choices)
    if not match:
        raise TerminalError("target_unavailable", "Choose an online terminal worker or an approved SSH grant.", 400)
    cls = PeerTerminal if target.kind == "worker" else SSHTerminal
    terminal = cls(session_id=body.session_id, who=who, cols=body.cols, rows=body.rows)
    # Reserve capacity across network awaits, then remove a failed open.
    if registry.count >= registry.limit:
        raise TerminalError("limit_reached", "Close a terminal before opening another.", 409)
    registry._terminals[terminal.id] = terminal
    try:
        if isinstance(terminal, PeerTerminal):
            await terminal.start(target, request, {"cwd": body.cwd, "shell": body.shell,
                                                  "cols": terminal.cols, "rows": terminal.rows})
        else:
            if body.cwd or body.shell:
                raise TerminalError("bad_target_options", "SSH opens the approved account's login shell and home folder.", 400)
            await terminal.start(target)
    except BaseException:
        registry._terminals.pop(terminal.id, None)
        raise
    return terminal
