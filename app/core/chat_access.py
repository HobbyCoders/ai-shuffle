"""Chat resource grants and one-shot approvals, owned by this worker process.

Restart revokes everything, including credentials. No grants are replicated or
inherited by forks. Only the authenticated access API can grant access. Writes,
commands and state-changing API requests require one-shot approval unless the
session is in Bypass; model tools cannot grant access or change the session's
permission mode.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
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

from app.core import chat_files, chat_services, chat_shell
from app.core.chat_files import AccessDenied

# Host-key types tried when pinning, most preferred first. RSA is one key
# under several signature names; the first that the server accepts is used.
HOST_KEY_ALGS = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                 "ecdsa-sha2-nistp521", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")
HOST_KEY_NAMES = {"ssh-ed25519": "ED25519", "ecdsa-sha2-nistp256": "ECDSA", "ecdsa-sha2-nistp384": "ECDSA",
                  "ecdsa-sha2-nistp521": "ECDSA", "rsa-sha2-512": "RSA", "rsa-sha2-256": "RSA", "ssh-rsa": "RSA"}


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["folder", "machine", "service"]
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
    # HTTPS API service: the credential is attached by the broker, never shown.
    url: str = Field(default="", max_length=2048)
    auth_header: str = Field(default="Authorization", max_length=64)
    auth_scheme: str = Field(default="Bearer", max_length=32)
    secret: SecretStr = Field(default_factory=lambda: SecretStr(""))


SECRET_FIELDS = frozenset({"password", "private_key", "passphrase", "secret"})


class RequestUnavailable(AccessDenied):
    """A terminal request failure, distinct from correctable private inputs."""


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
    origin: chat_services.Origin | None = None
    header: tuple[str, str] | None = None
    secret: str = ""
    revoked: bool = False
    ready: bool = True
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
    suggested_path: str = ""
    service_provider: str = ""
    suggested_url: str = ""
    timer: asyncio.TimerHandle | None = None

    def model_pending(self):
        return {"status": "awaiting_user", "request_id": self.id,
                "kind": self.kind, "expires_at": self.expires_at}

    def public(self):
        result = {key: getattr(self, key) for key in ("id", "kind", "purpose", "expires_at")}
        if self.suggested_path:
            result["suggested_path"] = self.suggested_path
        if self.kind == "service":
            result.update(status="awaiting_user", service_provider=self.service_provider,
                          suggested_url=self.suggested_url)
        return result


logger = logging.getLogger(__name__)

# Preparation is a resumable card, not a blocked model/tool call or a grant.
SERVICE_PREPARATION_SECONDS = 24 * 3600
CLOUDFLARE_API_URL = "https://api.cloudflare.com/client/v4"

# ChatResources is a read-only listing that must answer promptly. Its one
# database read runs on this small pool instead of asyncio's shared default
# executor, which every ``run_sync``/``to_thread`` caller in the process
# queues on, and the lookup is bounded so a stall returns a clear error rather
# than holding the turn until the tool-call ceiling.
_listing_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="chat-access-list",
)
LISTING_TIMEOUT_SECONDS = 15.0
# Every listing logs its stage timings at INFO (the default production level),
# so a stall outside this function still shows when the call itself finished;
# a listing slower than this logs them at WARNING.
LISTING_SLOW_SECONDS = 2.0


async def session_owner(session_id: str, *, executor=None) -> tuple:
    from app.db import database
    if executor is None:
        from app.db.async_database import run_sync
        return owner_from_row(await run_sync(database.get_session, session_id))
    row = await asyncio.get_running_loop().run_in_executor(executor, database.get_session, session_id)
    return owner_from_row(row)


def _default_executor_load() -> str:
    """Thread and queue counts of the loop's shared default executor, for stall logs."""
    try:
        pool = getattr(asyncio.get_running_loop(), "_default_executor", None)
        if pool is None:
            return "unstarted"
        return (f"threads={len(pool._threads)} queued={pool._work_queue.qsize()} "
                f"max={pool._max_workers}")
    except Exception:  # noqa: BLE001 — diagnostics only
        return "unknown"


def owner_from_row(row: dict | None) -> tuple:
    """Apply the private-access ownership rules to an already-loaded session row."""
    from app.network.control import owner_of
    if not row or row.get("mode") not in (None, "code", "chat", "voice"):
        raise AccessDenied("Resource access requires an existing Code, Chat, or Voice session.")
    if owner_of(row):
        raise AccessDenied("Manage access on the device which owns this session. Grants do not travel between devices.")
    return (row.get("owner_device_id"), row.get("instance_id"), row.get("api_user_id"))


async def session_bypasses_confirmation(session_id: str) -> bool:
    """Use the running adapter's effective mode, never a tool's build-time mode."""
    from app.core.session_modes import permission_mode_for_session_mode
    from app.core.session_pool import get_session_pool
    from app.db import database
    from app.db.async_database import run_sync

    row = await run_sync(database.get_session, session_id)
    if not row:
        return False
    mode = row.get("permission_mode")
    state = get_session_pool().peek(session_id)
    if state is not None and state.is_connected:
        live = getattr(state.adapter, "permission_mode", None)
        # Older adapters expose the live mode through the callback references.
        mode = live if isinstance(live, str) else state.callback_refs.get("permission_mode")
    return permission_mode_for_session_mode(mode, row.get("mode")) == "bypassPermissions"


async def publish_change(session_id: str, api_user_id: str | None, *, saved: bool = False) -> None:
    """Tell this worker's UI for the session's principal to refetch its access panel.

    Saved connections belong to the owner, not the session, and every session's
    panel lists them: a saved-list change also invalidates the unscoped
    resource so the owner's panels on other sessions refresh.
    """
    resources = [f"chat-access:{session_id}"] + (["chat-access"] if saved else [])
    try:
        from app.core.global_events import global_events
        # Root-layout subscribers use the authenticated global socket, not the
        # legacy session socket below. Publish only an invalidation; snapshots
        # still enforce private-access policy.
        from app.core.preference_identity import principal_preference_identity
        from app.db.async_database import run_sync
        principal = ({"kind": "api_user", "principal_id": api_user_id}
                     if api_user_id is not None else {"kind": "admin"})
        kind, identity = await run_sync(principal_preference_identity, principal)
        global_events.resources_changed(*resources, user_type=kind, user_id=identity)
        from app.core.sync_engine import sync_engine
        await sync_engine._broadcast_user_message(
            api_user_id or "admin",
            {"type": "chat_access_changed", "session_id": session_id},
        )
    except Exception:
        logger.debug("Access refresh notification unavailable", exc_info=True)


def grant_failure(exc: BaseException) -> tuple[int, str]:
    """Status and fixed message for a failed grant. Never echoes private inputs."""
    if isinstance(exc, RequestUnavailable):
        return 409, str(exc)
    if isinstance(exc, AccessDenied):
        return 400, str(exc)
    if isinstance(exc, asyncssh.PermissionDenied):
        return 400, "SSH sign-in was rejected. Check the username and password or private key."
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return 400, "The SSH host key did not match. Verify the server fingerprint using a trusted source."
    if isinstance(exc, TimeoutError):
        return 400, "The SSH connection timed out. Check the server address, port and reachability."
    if isinstance(exc, (asyncssh.Error, ValueError, OSError)):
        logger.warning("Chat access connection failed: %s", type(exc).__name__)
        return 400, "Could not connect. Check the path or SSH address, host-key fingerprint and credentials."
    logger.warning("Chat access grant failed: %s", type(exc).__name__)
    return 500, "Private access is unavailable on this worker. Check the worker log before retrying."


class Broker:
    def __init__(self):
        self.grants: dict[str, Grant] = {}
        self.actions: dict[str, Action] = {}
        self.requests: dict[str, AccessRequest] = {}
        self._notifications: set[asyncio.Task] = set()

    def _notify(self, session_id: str, owner: tuple, *, saved: bool = False) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Synchronous shutdown has no connected UI to notify.
        task = loop.create_task(publish_change(session_id, owner[2] if len(owner) > 2 else None, saved=saved))
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)
        # Devices showing this session as a mirror refresh through their own worker.
        from app.network import chat_access_routing
        chat_access_routing.notify_watchers(session_id, saved=saved)

    def _finish_request(self, request: AccessRequest) -> None:
        if self.requests.get(request.id) is not request:
            return
        self.requests.pop(request.id)
        if request.timer:
            request.timer.cancel()
        if not request.result.done():
            request.result.set_result(None)
        self._notify(request.session_id, request.owner)

    def _live_requests(self, session_id: str, owner: tuple):
        for request in list(self.requests.values()):
            if request.session_id != session_id:
                continue
            if request.owner != owner or request.expires_at <= time.time() or request.result.done():
                self._finish_request(request)
            else:
                yield request

    async def _check_request(self, request: AccessRequest) -> None:
        self._ensure_request(request, await session_owner(request.session_id))

    def _ensure_request(self, request: AccessRequest, owner: tuple) -> None:
        if (self.requests.get(request.id) is not request or request.result.done()
                or request.expires_at <= time.time()):
            self._finish_request(request)
            raise RequestUnavailable("This private access request is no longer available.")
        if owner != request.owner:
            self._finish_request(request)
            raise RequestUnavailable("Session ownership changed.")

    async def request(self, session_id: str, kind: str, purpose: str, suggested_path: str = "",
                      service_provider: str = "", suggested_url: str = ""):
        owner = await session_owner(session_id)
        if kind not in {"machine", "folder", "service"} or not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise AccessDenied("Specify machine, folder or service and a short explanation of the requested access.")
        if not isinstance(suggested_path, str) or (suggested_path and (
            kind != "folder" or not suggested_path.startswith("/") or len(suggested_path) > 4096
            or any(c in suggested_path for c in "\0\r\n")
        )):
            raise AccessDenied("A suggested path must be an absolute folder path on the owning worker.")
        if kind == "folder" and not chat_files.available():
            raise AccessDenied("This worker cannot safely grant folders. Do not ask for a path or use a shell workaround.")
        if not isinstance(service_provider, str) or not isinstance(suggested_url, str):
            raise AccessDenied("Specify a service provider and a public HTTPS API base address.")
        if (service_provider or suggested_url) and kind != "service":
            raise AccessDenied("Service setup hints are only available for service requests.")
        if kind == "service":
            service_provider = service_provider or ("cloudflare" if suggested_url.rstrip("/") == CLOUDFLARE_API_URL else "custom")
            if service_provider not in {"cloudflare", "custom"}:
                raise AccessDenied("Choose cloudflare or custom for the service provider.")
            if suggested_url:
                origin = chat_services.parse_origin(suggested_url)
                try:
                    address = ipaddress.ip_address(origin.host)
                except ValueError:
                    address = None
                if ((address is not None and not address.is_global)
                        or origin.host == "localhost" or origin.host.endswith((".localhost", ".local"))):
                    raise AccessDenied("Only public service endpoints may be suggested; choose private addresses in the card.")
                suggested_url = origin.url
            if service_provider == "cloudflare":
                if suggested_url and suggested_url != CLOUDFLARE_API_URL:
                    raise AccessDenied("The Cloudflare preset uses its official API base address.")
                suggested_url = CLOUDFLARE_API_URL
        for pending in self._live_requests(session_id, owner):
            if (kind == "service" and pending.kind == kind and pending.purpose == purpose.strip()
                    and pending.service_provider == service_provider and pending.suggested_url == suggested_url):
                return pending.model_pending()
            raise AccessDenied("A private access request is already awaiting the user.")
        duration = SERVICE_PREPARATION_SECONDS if kind == "service" else 240
        request = AccessRequest(uuid.uuid4().hex, session_id, owner, kind, purpose.strip(),
                                time.time() + duration, asyncio.get_running_loop().create_future(),
                                suggested_path=suggested_path, service_provider=service_provider,
                                suggested_url=suggested_url)
        self.requests[request.id] = request
        self._notify(session_id, owner)
        if kind == "service":
            request.timer = asyncio.get_running_loop().call_later(duration, self._finish_request, request)
            return request.model_pending()
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
            raise RequestUnavailable("This private access request is no longer available.")
        await self._check_request(request)
        # Ownership lookup above yields: a concurrent submission may have claimed it.
        if request.claiming:
            raise RequestUnavailable("This private access request is no longer available.")
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
            grant = await self.create(session_id, spec, request=request)
            await self._check_request(request)
            saved_id = submission.saved_id
            if submission.save_connection:
                # Save the key actually pinned, so later uses verify against it
                # even when the user left the fingerprint blank.
                pinned = spec.model_copy(update={"fingerprint": grant["fingerprint"]})
                saved_id = await run_sync(chat_vault.save, request.owner, pinned)
                self._notify(session_id, request.owner, saved=True)
            await self._check_request(request)
            current = await self.get(session_id, grant["id"], preparing=True)
            current.saved_id = saved_id
            if saved_id:
                # Deletion may race the SSH handshake or the save. Attach the
                # binding before rechecking storage so deletion either revokes
                # this grant or makes this lookup fail; it cannot be resurrected.
                await run_sync(chat_vault.load, request.owner, saved_id)
                await self.get(session_id, grant["id"], preparing=True)
            await self._check_request(request)
            current.ready = True
            request.result.set_result(grant["id"])
            self._notify(session_id, request.owner)
            if request.kind == "service":
                self._finish_request(request)
            return {"granted": True, "saved": bool(saved_id)}
        except BaseException:
            if grant and grant["id"] in self.grants:
                self.revoke(session_id, grant["id"])
            raise
        finally:
            request.claiming = False

    async def forget(self, session_id: str, connection_id: str):
        """Delete one of the owner's saved connections and revoke grants that use it."""
        from app.core import chat_vault
        from app.db.async_database import run_sync
        owner = await session_owner(session_id)
        await run_sync(chat_vault.delete, owner, connection_id)
        for grant in list(self.grants.values()):
            if grant.owner[2:] == owner[2:] and grant.saved_id == connection_id:
                self.revoke(grant.session_id, grant.id)
        # Clients no longer poll empty snapshots, so tell every panel of this
        # owner (on any session) that the saved list changed.
        self._notify(session_id, owner, saved=True)

    async def decline(self, session_id: str, request_id: str):
        request = self.requests.get(request_id)
        if not request or request.session_id != session_id or request.result.done():
            raise RequestUnavailable("This private access request is no longer available.")
        await self._check_request(request)
        request.result.set_result(None)
        if request.kind == "service":
            self._finish_request(request)

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

    async def create(self, session_id: str, spec: GrantInput, *, request: AccessRequest | None = None) -> dict:
        owner = await session_owner(session_id)
        if sum(g.session_id == session_id for g in self.grants.values()) >= 8:
            raise AccessDenied("A session may have at most eight active grants.")
        grant = Grant(uuid.uuid4().hex, session_id, owner, spec.label.strip(),
                      spec.kind, spec.path, time.time() + spec.minutes * 60,
                      writable=spec.writable, ready=request is None)
        try:
            if spec.kind == "folder":
                from pathlib import Path
                from app.core.config import settings
                root = Path(spec.path).resolve()
                private = settings.effective_data_dir.resolve()
                if root == private or root in private.parents or private in root.parents:
                    raise AccessDenied("The worker's private data and credential storage cannot be granted.")
                try:
                    grant.root = chat_files.open_root(spec.path)
                except FileNotFoundError:
                    raise AccessDenied(
                        "Folder not found on the worker that runs this chat. Choose an existing folder. "
                        "For Docker or Unraid, use a mounted container path, not an unmounted host path."
                    ) from None
                except NotADirectoryError:
                    raise AccessDenied("Choose a folder, not a file or symbolic link.") from None
                except PermissionError:
                    raise AccessDenied("The worker cannot read this folder. Choose a folder its account can access.") from None
            elif spec.kind == "service":
                origin = chat_services.parse_origin(spec.url.strip())
                header = chat_services.credential_header(
                    spec.auth_header, spec.auth_scheme, spec.secret.get_secret_value())
                # Only the owner's private submission can select the certificate.
                pin = chat_services.certificate_pin(spec.fingerprint)
                if pin:
                    await chat_services.verify_origin(origin, pin)
                else:
                    await chat_services.verify_origin(origin)
                grant.fingerprint = pin
                grant.origin, grant.header = origin, header
                grant.secret = spec.secret.get_secret_value()
                grant.target = origin.url
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
                raise RequestUnavailable("Session ownership changed; grant refused.")
            if request is not None:
                # Check without yielding before publication: cancelled preparation
                # must never produce even a briefly usable handle.
                self._ensure_request(request, owner)
            if sum(g.session_id == session_id for g in self.grants.values()) >= 8:
                raise AccessDenied("A session may have at most eight active grants.")
        except BaseException:
            if grant.root is not None:
                os.close(grant.root)
            raise
        self.grants[grant.id] = grant
        grant.timer = asyncio.get_running_loop().call_later(spec.minutes * 60, self.revoke, session_id, grant.id)
        if grant.ready:
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
        grant.header, grant.secret = None, ""
        self._notify(session_id, grant.owner)

    async def get(self, session_id: str, grant_id: str, *, preparing: bool = False) -> Grant:
        grant = self.grants.get(grant_id)
        if not grant or grant.session_id != session_id or (not grant.ready and not preparing):
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

    def _live_grants(self, session_id: str, owner: tuple) -> list[Grant]:
        """This session's usable grants; expired or re-owned ones are revoked."""
        live = []
        for grant in list(self.grants.values()):
            if grant.session_id == session_id:
                if grant.revoked or grant.expires_at <= time.time() or grant.owner != owner:
                    self.revoke(session_id, grant.id)
                elif grant.ready:
                    live.append(grant)
        return live

    async def resources(self, session_id: str) -> dict:
        """The model-facing grant listing (ChatResources). Never asks the user.

        Unlike ``snapshot`` it skips the saved-connection catalogue, which the
        tool never returned, so its only blocking step is the session-owner
        read. That read runs on the listing's own pool under
        ``LISTING_TIMEOUT_SECONDS``, so neither a pending RequestChatAccess nor
        a busy shared executor can hold it. Timing logs carry no addresses,
        labels or credentials.
        """
        started = time.monotonic()
        try:
            async with asyncio.timeout(LISTING_TIMEOUT_SECONDS):
                owner = await session_owner(session_id, executor=_listing_executor)
        except TimeoutError:
            logger.warning(
                "ChatResources listing timed out: session=%s owner_lookup=%.2fs default_executor=%s",
                session_id, time.monotonic() - started, _default_executor_load(),
            )
            raise AccessDenied(
                f"Listing access handles timed out after {LISTING_TIMEOUT_SECONDS:g} seconds on this "
                "worker. No access was requested or changed; try ChatResources again shortly."
            ) from None
        except AccessDenied:
            raise
        except Exception as exc:
            logger.warning("ChatResources owner lookup failed: session=%s error=%s",
                           session_id, type(exc).__name__)
            raise AccessDenied(
                "Listing access handles failed: this worker could not read the session. "
                "No access was requested or changed."
            ) from None
        looked_up = time.monotonic()
        grants = [grant.model_public() for grant in self._live_grants(session_id, owner)]
        folder_available = chat_files.available()
        finished = time.monotonic()
        total = finished - started
        log = logger.warning if total >= LISTING_SLOW_SECONDS else logger.info
        log("ChatResources listing: session=%s grants=%d owner_lookup=%.3fs grants_and_probe=%.3fs "
            "total=%.3fs default_executor=%s", session_id, len(grants), looked_up - started,
            finished - looked_up, total, _default_executor_load())
        result = {"grants": grants, "folder_available": folder_available}
        pending = [r.model_pending() for r in self._live_requests(session_id, owner) if r.kind == "service"]
        if pending:
            result["pending_requests"] = pending
        return result

    async def snapshot(self, session_id: str, row: dict | None = None):
        # The access API has just loaded and authorized this session row; reuse
        # it so each panel refresh reads the session once, not twice. Callers
        # without a row still load it here.
        owner = owner_from_row(row) if row is not None else await session_owner(session_id)
        result = [grant.public() for grant in self._live_grants(session_id, owner)]
        from app.core import chat_vault, encryption
        from app.db.async_database import run_sync
        saved = await run_sync(chat_vault.catalog, owner, cache_empty=True)
        return {
            "saved_connections": saved,
            "vault_available": encryption.is_encryption_ready(),
            "grants": result,
            "actions": [a.public() for a in self.actions.values()
                        if a.session_id == session_id and not a.decision.done()],
            "requests": [r.public() for r in self._live_requests(session_id, owner)],
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
        if action.decision.done():
            raise AccessDenied("This action is no longer awaiting approval.")
        action.decision.set_result(allow)

    async def resolve_mode_actions(self, session_id: str):
        """Release existing confirmations after an owner-applied switch to Bypass."""
        pending = [a for a in self.actions.values()
                   if a.session_id == session_id and not a.decision.done()]
        if not pending or not await session_bypasses_confirmation(session_id):
            return
        for action in pending:
            try:
                await self.decide(session_id, action.id, True)
            except AccessDenied:
                # Expired/revoked/answered actions must never be revived.
                continue

    async def approve(self, grant: Grant, operation: str, arguments: dict):
        if await session_bypasses_confirmation(grant.session_id):
            return
        if sum(a.session_id == grant.session_id for a in self.actions.values()) >= 8:
            raise AccessDenied("Too many pending actions; resolve them in Chat access.")
        action = Action(uuid.uuid4().hex, grant.id, grant.session_id, operation,
                        dict(arguments), min(grant.expires_at, time.time() + 240),
                        asyncio.get_running_loop().create_future())
        self.actions[action.id] = action
        self._notify(grant.session_id, grant.owner)
        try:
            # A persisted-mode lookup can complete with an old snapshot after
            # a mode-change sweep already ran. Recheck with the action visible
            # so switching to Bypass cannot strand a newly queued confirmation.
            await self.resolve_mode_actions(grant.session_id)
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
            if grant.kind not in ("machine", "folder") or (grant.kind == "folder" and grant.root is None):
                raise AccessDenied("Commands need a machine grant or a folder grant. Local worker Bash is unavailable.")
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip() or len(command) > 16384 or "\x00" in command:
                raise AccessDenied("Provide a command of at most 16,384 characters.")
            arguments = {"command": command}
            if grant.kind == "folder":
                # Refuse before asking the user to approve a command that cannot run.
                await chat_shell.ensure_available()
                # Re-read after the await: revocation closes grant.root.
                grant = await self.get(session_id, grant_id)
                await chat_shell.ensure_folder(grant.root)
        elif operation == "http":
            if grant.kind != "service" or grant.origin is None:
                raise AccessDenied("This operation requires an HTTPS service grant.")
            method = arguments.get("method")
            method = method.upper() if isinstance(method, str) else ""
            if method not in chat_services.READ_METHODS | chat_services.WRITE_METHODS:
                raise AccessDenied("Use GET, HEAD, POST, PUT, PATCH or DELETE.")
            path = arguments.get("path")
            chat_services.request_url(grant.origin, path)
            body = arguments.get("body") or ""
            content_type = arguments.get("content_type") or ""
            if not isinstance(body, str) or len(body.encode("utf-8")) > chat_files.LIMIT:
                raise AccessDenied("Provide a UTF-8 request body of at most 64 KiB.")
            if body and method in chat_services.READ_METHODS:
                raise AccessDenied(f"{method} requests cannot carry a body.")
            if not isinstance(content_type, str) or (
                    content_type and not chat_services.CONTENT_TYPE.fullmatch(content_type)):
                raise AccessDenied("Provide a content type such as application/json.")
            if method in chat_services.WRITE_METHODS and not grant.writable:
                raise AccessDenied("This service grant is read-only: only GET and HEAD are allowed.")
            # The approval card shows exactly what is sent; the credential is never an argument.
            arguments = {"method": method, "path": path, "body": body,
                         "content_type": (content_type or "application/json") if body else ""}
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
        if operation in ("command", "write") or (
                operation == "http" and arguments["method"] in chat_services.WRITE_METHODS):
            await self.approve(grant, operation, arguments)
        await self.get(session_id, grant_id)
        if operation in ("command", "http"):
            root = None
            if operation == "command" and grant.kind == "folder":
                # Duplicated before any await: revocation closes grant.root, and
                # a reused descriptor number must never reach the sandbox.
                root = os.dup(grant.root)
                work = chat_shell.run(root, grant.writable, arguments["command"])
            elif operation == "command":
                work = self.remote_command(grant, arguments["command"])
            else:
                work = chat_services.perform(grant.origin, grant.header, grant.secret, arguments["method"],
                                             arguments["path"], arguments["body"], arguments["content_type"],
                                             pin=grant.fingerprint)
            task = asyncio.create_task(work)
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
                if root is not None:
                    os.close(root)
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
