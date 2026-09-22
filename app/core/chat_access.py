"""Chat resource grants and one-shot approvals, owned by this worker process.

Restart revokes everything, including credentials. No grants are replicated or
inherited by forks. Only the authenticated access API can grant or approve;
model tools can inspect resources and request operations, never authorize them.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import asyncssh
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool

from app.core import chat_files
from app.core.chat_files import AccessDenied

# Host-key types tried when pinning, most preferred first. RSA is one key
# under several signature names; the first that the server accepts is used.
HOST_KEY_ALGS = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                 "ecdsa-sha2-nistp521", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")
HOST_KEY_NAMES = {"ssh-ed25519": "ED25519", "ecdsa-sha2-nistp256": "ECDSA", "ecdsa-sha2-nistp384": "ECDSA",
                  "ecdsa-sha2-nistp521": "ECDSA", "rsa-sha2-512": "RSA", "rsa-sha2-256": "RSA", "ssh-rsa": "RSA"}


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["folder", "machine"]
    label: str = Field(min_length=1, max_length=80)
    minutes: int = Field(default=60, ge=1, le=480)
    path: str = Field(default="", max_length=4096)
    writable: bool = False
    host: str = Field(default="", max_length=64)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    fingerprint: str = Field(default="", max_length=100)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""))
    private_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    passphrase: SecretStr = Field(default_factory=lambda: SecretStr(""))


class GrantSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection: GrantInput | None = None
    saved_id: str = Field(default="", max_length=32)
    minutes: int = Field(default=60, ge=1, le=480)
    save_connection: StrictBool = False


@dataclass(repr=False)
class Grant:
    id: str
    session_id: str
    owner: tuple
    label: str
    kind: str
    target: str
    expires_at: float
    writable: bool = False
    root: int | None = None
    host: str = ""
    port: int = 22
    username: str = ""
    fingerprint: str = ""
    host_key_alg: str = ""
    password: str = ""
    key: object = None
    revoked: bool = False
    saved_id: str = ""
    timer: asyncio.TimerHandle | None = None
    tasks: set = field(default_factory=set)

    def public(self):
        return {key: getattr(self, key) for key in (
            "id", "label", "kind", "target", "expires_at", "writable", "fingerprint"
        )}

    def model_public(self):
        # User-entered connection details never become durable tool results.
        return {key: getattr(self, key) for key in ("id", "kind", "expires_at", "writable")}


@dataclass
class Action:
    id: str
    grant_id: str
    session_id: str
    operation: str
    arguments: dict
    expires_at: float
    decision: asyncio.Future

    def public(self):
        return {key: getattr(self, key) for key in (
            "id", "grant_id", "operation", "arguments", "expires_at"
        )}


@dataclass
class AccessRequest:
    id: str
    session_id: str
    owner: tuple
    kind: str
    purpose: str
    expires_at: float
    result: asyncio.Future
    claiming: bool = False

    def public(self):
        return {key: getattr(self, key) for key in ("id", "kind", "purpose", "expires_at")}


async def session_owner(session_id: str) -> tuple:
    from app.db import database
    from app.db.async_database import run_sync
    from app.network.control import owner_of
    row = await run_sync(database.get_session, session_id)
    if not row or row.get("mode") not in (None, "code", "chat", "voice"):
        raise AccessDenied("Resource access requires an existing Code, Chat, or Voice session.")
    if owner_of(row):
        raise AccessDenied("Manage access on the device which owns this session. Grants do not travel between devices.")
    return (row.get("owner_device_id"), row.get("instance_id"), row.get("api_user_id"))


class Broker:
    def __init__(self):
        self.grants: dict[str, Grant] = {}
        self.actions: dict[str, Action] = {}
        self.requests: dict[str, AccessRequest] = {}
        self._notifications: set[asyncio.Task] = set()

    def _notify(self, session_id: str, owner: tuple) -> None:
        async def send():
            try:
                from app.core.global_events import global_events
                # Root-layout subscribers use the authenticated global socket,
                # not the legacy session socket below. Publish only an
                # invalidation; snapshots still enforce private-access policy.
                from app.core.preference_identity import principal_preference_identity
                from app.db.async_database import run_sync
                principal = ({"kind": "api_user", "principal_id": owner[2]}
                             if owner[2] is not None else {"kind": "admin"})
                kind, identity = await run_sync(principal_preference_identity, principal)
                global_events.resources_changed(
                    f"chat-access:{session_id}", user_type=kind, user_id=identity,
                )
                from app.core.sync_engine import sync_engine
                await sync_engine._broadcast_user_message(
                    owner[2] or "admin",
                    {"type": "chat_access_changed", "session_id": session_id},
                )
            except Exception:
                logging.getLogger(__name__).debug("Access refresh notification unavailable", exc_info=True)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Synchronous shutdown has no connected UI to notify.
        task = loop.create_task(send())
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)

    async def request(self, session_id: str, kind: str, purpose: str):
        owner = await session_owner(session_id)
        if kind not in {"machine", "folder"} or not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise AccessDenied("Specify machine or folder and a short explanation of the requested access.")
        if kind == "folder" and not chat_files.available():
            raise AccessDenied("This worker cannot safely grant folders. Do not ask for a path or use a shell workaround.")
        if any(r.session_id == session_id for r in self.requests.values()):
            raise AccessDenied("A private access request is already awaiting the user.")
        request = AccessRequest(uuid.uuid4().hex, session_id, owner, kind, purpose.strip(),
                                time.time() + 240, asyncio.get_running_loop().create_future())
        self.requests[request.id] = request
        self._notify(session_id, owner)
        try:
            grant_id = await asyncio.wait_for(request.result, 240)
            if not grant_id:
                raise AccessDenied("The user declined the private access request.")
            grant = await self.get(session_id, grant_id)
            return grant.model_public()
        except asyncio.TimeoutError:
            raise AccessDenied("Private input request expired; ask the user before requesting it again.") from None
        finally:
            self.requests.pop(request.id, None)
            self._notify(session_id, owner)

    async def fulfill(self, session_id: str, request_id: str, submission: GrantSubmission):
        request = self.requests.get(request_id)
        if (not request or request.session_id != session_id or request.result.done()
                or request.expires_at <= time.time() or request.claiming):
            raise AccessDenied("This private access request is no longer available.")
        if request.owner != await session_owner(session_id):
            raise AccessDenied("Session ownership changed.")
        request.claiming = True
        grant = None
        try:
            from app.core import chat_vault
            from app.db.async_database import run_sync
            if submission.saved_id:
                if submission.connection is not None or submission.save_connection:
                    raise AccessDenied("Choose either a saved connection or new private inputs.")
                spec = await run_sync(chat_vault.load, request.owner, submission.saved_id)
            else:
                spec = submission.connection
            if spec is None or request.kind != spec.kind:
                raise AccessDenied("The requested resource type changed.")
            spec = spec.model_copy(update={"minutes": submission.minutes})
            grant = await self.create(session_id, spec)
            saved_id = submission.saved_id
            if submission.save_connection:
                # Save the key actually pinned, so later uses verify against it
                # even when the user left the fingerprint blank.
                pinned = spec.model_copy(update={"fingerprint": grant["fingerprint"]})
                saved_id = await run_sync(chat_vault.save, request.owner, pinned)
            if request.result.done() or request.expires_at <= time.time():
                raise AccessDenied("The request expired while connecting. Session access was not retained.")
            current = await self.get(session_id, grant["id"])
            current.saved_id = saved_id
            if saved_id:
                # Deletion may race the SSH handshake or the save. Attach the
                # binding before rechecking storage so deletion either revokes
                # this grant or makes this lookup fail; it cannot be resurrected.
                await run_sync(chat_vault.load, request.owner, saved_id)
                await self.get(session_id, grant["id"])
            if request.result.done() or request.expires_at <= time.time():
                raise AccessDenied("The private request expired before access was granted.")
            request.result.set_result(grant["id"])
            return {"granted": True, "saved": bool(saved_id)}
        except BaseException:
            if grant and grant["id"] in self.grants:
                self.revoke(session_id, grant["id"])
            raise
        finally:
            request.claiming = False

    async def decline(self, session_id: str, request_id: str):
        await session_owner(session_id)
        request = self.requests.get(request_id)
        if not request or request.session_id != session_id or request.result.done():
            raise AccessDenied("This private access request is no longer available.")
        request.result.set_result(None)

    async def host_key(self, host: str, port: int, alg: str) -> str | None:
        """The server's ``alg`` host-key fingerprint, read before any authentication.

        The handshake is abandoned at host-key validation, so no username
        secret, password or key is sent. ``None`` means the server has no key
        of that type.
        """
        seen: list[str] = []

        class Recorder(asyncssh.SSHClient):
            def validate_host_public_key(self, host, addr, port, key):
                seen.append(key.get_fingerprint("sha256"))
                return False

        try:
            conn = await asyncssh.connect(
                host, port, username="host-key-probe", password=None, client_keys=[],
                agent_path=None, config=None, known_hosts=([], [], []),
                client_factory=Recorder, server_host_key_algs=[alg],
                connect_timeout=10, login_timeout=10,
            )
            conn.close()  # Unreachable: the recorder rejects every key.
        except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyExchangeFailed, asyncssh.ProtocolError):
            pass
        except (asyncssh.ConnectionLost, ConnectionResetError):
            pass  # OpenSSH drops the connection when asked for a key type it lacks.
        return seen[0] if seen else None

    async def pin_host_key(self, grant: Grant, fingerprint: str) -> None:
        """Pin one of the server's host keys to ``grant``.

        With a fingerprint, any key type the server offers may match it (a
        client without known_hosts would otherwise negotiate just one type,
        often RSA, and reject a correct ED25519 fingerprint). Without one, the
        first key offered in ``HOST_KEY_ALGS`` order is trusted for this grant
        and reported back so the user can check it.
        """
        offered: list[tuple[str, str]] = []
        for alg in HOST_KEY_ALGS:
            found = await self.host_key(grant.host, grant.port, alg)
            if found is None or any(found == seen for _, seen in offered):
                continue
            offered.append((alg, found))
            if not fingerprint or hmac.compare_digest(found, fingerprint):
                grant.fingerprint, grant.host_key_alg = found, alg
                return
        if not offered:
            raise AccessDenied("The SSH server offered no supported host key.")
        keys = ", ".join(f"{HOST_KEY_NAMES[alg]} {found}" for alg, found in offered)
        raise AccessDenied(f"No host key on this server matches that fingerprint. The server offered: {keys}.")

    async def connection(self, grant: Grant):
        class PinnedHost(asyncssh.SSHClient):
            def validate_host_public_key(self, host, addr, port, key):
                return hmac.compare_digest(key.get_fingerprint("sha256"), grant.fingerprint)

        # Literal IP, no SSH config, identity discovery, forwarding, local
        # subprocess, agent, inherited environment, proxy, or known_hosts file.
        return await asyncssh.connect(
            grant.host, grant.port, username=grant.username,
            password=grant.password or None,
            client_keys=[grant.key] if grant.key else [],
            agent_path=None, config=None, known_hosts=([], [], []),
            client_factory=PinnedHost, agent_forwarding=False,
            server_host_key_algs=[grant.host_key_alg],
            preferred_auth=("publickey", "password"),
            connect_timeout=10, login_timeout=10,
        )

    async def create(self, session_id: str, spec: GrantInput) -> dict:
        owner = await session_owner(session_id)
        if sum(g.session_id == session_id for g in self.grants.values()) >= 8:
            raise AccessDenied("A session may have at most eight active grants.")
        grant = Grant(uuid.uuid4().hex, session_id, owner, spec.label.strip(),
                      spec.kind, spec.path, time.time() + spec.minutes * 60,
                      writable=spec.writable)
        try:
            if spec.kind == "folder":
                from pathlib import Path
                from app.core.config import settings
                root = Path(spec.path).resolve()
                private = settings.effective_data_dir.resolve()
                if root == private or root in private.parents or private in root.parents:
                    raise AccessDenied("The worker's private data and credential storage cannot be granted.")
                grant.root = chat_files.open_root(spec.path)
            else:
                address = ipaddress.ip_address(spec.host)
                if address.is_unspecified or address.is_multicast or address.is_link_local:
                    raise AccessDenied("Use a unicast IP address for this machine.")
                fingerprint = spec.fingerprint.strip()
                if fingerprint and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", fingerprint):
                    raise AccessDenied("A host-key fingerprint looks like SHA256: followed by 43 characters. "
                                       "Leave it blank to trust the key the server presents now.")
                if not spec.username.strip() or not (spec.password.get_secret_value() or spec.private_key.get_secret_value()):
                    raise AccessDenied("Supply an SSH username and password or private key.")
                grant.host, grant.port, grant.username = str(address), spec.port, spec.username
                await self.pin_host_key(grant, fingerprint.rstrip("="))
                grant.password = spec.password.get_secret_value()
                grant.key = asyncssh.import_private_key(
                    spec.private_key.get_secret_value(), spec.passphrase.get_secret_value() or None
                ) if spec.private_key.get_secret_value() else None
                grant.target = f"{grant.username}@{grant.host}:{grant.port}"
                grant.writable = True
                async with await self.connection(grant):
                    pass  # Verify the pinned identity and credentials before saving the grant.
            if await session_owner(session_id) != owner:
                raise AccessDenied("Session ownership changed; grant refused.")
            if sum(g.session_id == session_id for g in self.grants.values()) >= 8:
                raise AccessDenied("A session may have at most eight active grants.")
        except BaseException:
            if grant.root is not None:
                os.close(grant.root)
            raise
        self.grants[grant.id] = grant
        grant.timer = asyncio.get_running_loop().call_later(spec.minutes * 60, self.revoke, session_id, grant.id)
        self._notify(session_id, owner)
        return grant.public()

    def revoke(self, session_id: str, grant_id: str):
        grant = self.grants.get(grant_id)
        if grant is None or grant.session_id != session_id:
            raise AccessDenied("No such grant in this session.")
        self.grants.pop(grant_id)
        grant.revoked = True
        if grant.timer:
            grant.timer.cancel()
        for action in list(self.actions.values()):
            if action.grant_id == grant_id and not action.decision.done():
                action.decision.set_result(False)
        for task in list(grant.tasks):
            task.cancel()
        if grant.root is not None:
            os.close(grant.root)
            grant.root = None
        grant.password, grant.key = "", None
        self._notify(session_id, grant.owner)

    async def get(self, session_id: str, grant_id: str) -> Grant:
        grant = self.grants.get(grant_id)
        if not grant or grant.session_id != session_id:
            raise AccessDenied("No such handle in this session. Use RequestChatAccess to ask privately.")
        try:
            owner = await session_owner(session_id)
        except AccessDenied:
            self.revoke(session_id, grant_id)
            raise
        if grant.revoked or grant.expires_at <= time.time() or grant.owner != owner:
            self.revoke(session_id, grant_id)
            raise AccessDenied("Access expired or ownership changed. Request a new grant.")
        return grant

    async def snapshot(self, session_id: str):
        owner = await session_owner(session_id)
        result = []
        for grant in list(self.grants.values()):
            if grant.session_id == session_id:
                if grant.revoked or grant.expires_at <= time.time() or grant.owner != owner:
                    self.revoke(session_id, grant.id)
                else:
                    result.append(grant.public())
        from app.core import chat_vault, encryption
        from app.db.async_database import run_sync
        saved = await run_sync(chat_vault.catalog, owner, cache_empty=True)
        return {
            "saved_connections": saved,
            "vault_available": encryption.is_encryption_ready(),
            "grants": result,
            "actions": [a.public() for a in self.actions.values()
                        if a.session_id == session_id and not a.decision.done()],
            "requests": [r.public() for r in self.requests.values()
                         if r.session_id == session_id and not r.result.done()],
            "folder_available": chat_files.available(),
            "restart_revokes": True,
        }

    async def decide(self, session_id: str, action_id: str, allow: bool):
        await session_owner(session_id)
        action = self.actions.get(action_id)
        if not action or action.session_id != session_id or action.decision.done():
            raise AccessDenied("This action is no longer awaiting approval.")
        if action.expires_at <= time.time():
            action.decision.set_result(False)
            raise AccessDenied("This action expired.")
        await self.get(session_id, action.grant_id)
        action.decision.set_result(allow)

    async def approve(self, grant: Grant, operation: str, arguments: dict):
        if sum(a.session_id == grant.session_id for a in self.actions.values()) >= 8:
            raise AccessDenied("Too many pending actions; resolve them in Chat access.")
        action = Action(uuid.uuid4().hex, grant.id, grant.session_id, operation,
                        dict(arguments), min(grant.expires_at, time.time() + 240),
                        asyncio.get_running_loop().create_future())
        self.actions[action.id] = action
        self._notify(grant.session_id, grant.owner)
        try:
            allowed = await asyncio.wait_for(action.decision, max(0, action.expires_at - time.time()))
            if not allowed:
                raise AccessDenied("The action was denied or access was revoked.")
        except asyncio.TimeoutError:
            raise AccessDenied("Approval expired. Ask the user before requesting it again.") from None
        finally:
            self.actions.pop(action.id, None)
            self._notify(grant.session_id, grant.owner)
        await self.get(grant.session_id, grant.id)

    async def run(self, session_id: str, grant_id: str, operation: str, arguments: dict):
        grant = await self.get(session_id, grant_id)
        if operation == "command":
            if grant.kind != "machine":
                raise AccessDenied("A folder grant cannot run commands. Local worker Bash is unavailable.")
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip() or len(command) > 16384 or "\x00" in command:
                raise AccessDenied("Provide a command of at most 16,384 characters.")
            arguments = {"command": command}
        elif operation in ("list", "read", "write"):
            if grant.kind != "folder" or grant.root is None:
                raise AccessDenied("This operation requires a folder grant.")
            path = arguments.get("path", "")
            chat_files.parts(path, empty=operation == "list")
            arguments = {"path": path, **({"content": arguments.get("content")} if operation == "write" else {})}
            if operation == "write":
                if not grant.writable:
                    raise AccessDenied("This folder is read-only.")
                content = arguments["content"]
                if not isinstance(content, str) or len(content.encode("utf-8")) > chat_files.LIMIT:
                    raise AccessDenied("Provide UTF-8 text of at most 64 KiB.")
        else:
            raise AccessDenied("Unknown operation.")
        if operation in ("command", "write"):
            await self.approve(grant, operation, arguments)
        await self.get(session_id, grant_id)
        if operation == "command":
            task = asyncio.create_task(self.remote_command(grant, arguments["command"]))
            grant.tasks.add(task)
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=1)
                    await self.get(session_id, grant_id)
                return await task
            except asyncio.CancelledError:
                if grant.revoked:
                    raise AccessDenied("Access revoked; connection closed. Remote changes are not rolled back.") from None
                raise
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                grant.tasks.discard(task)
        # Bounded synchronous descriptor operations cannot interleave with revoke.
        if operation == "read":
            return {"content": chat_files.read(grant.root, arguments["path"])}
        if operation == "list":
            return {"entries": chat_files.listing(grant.root, arguments["path"])}
        chat_files.write(grant.root, arguments["path"], arguments["content"])
        return {"written": True}

    async def remote_command(self, grant: Grant, command: str):
        async with asyncio.timeout(60):
            async with await self.connection(grant) as conn:
                # No local shell invocation. The granted machine executes this.
                async with conn.create_process(command, encoding=None) as process:
                    async def drain(stream):
                        kept = bytearray()
                        truncated = False
                        while chunk := await stream.read(8192):
                            room = max(0, chat_files.LIMIT - len(kept))
                            kept.extend(chunk[:room])
                            truncated |= len(chunk) > room
                        return kept.decode("utf-8", errors="replace"), truncated
                    try:
                        stdout, stderr = await asyncio.gather(drain(process.stdout), drain(process.stderr))
                        await process.wait_closed()
                        return {"exit_code": process.exit_status,
                                "stdout": stdout[0], "stderr": stderr[0],
                                "truncated": stdout[1] or stderr[1]}
                    finally:
                        if process.exit_status is None:
                            process.terminate()


broker = Broker()
