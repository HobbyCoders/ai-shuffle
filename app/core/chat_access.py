"""Chat resource grants and one-shot approvals, owned by this worker process.

Restart revokes everything, including credentials. No grants are replicated or
inherited by forks. Only the authenticated access API can grant access. Writes,
commands and state-changing API requests require one-shot approval unless the
session is in Bypass; model tools cannot grant access or change the session's
permission mode.

A subagent (a Task/Swarm child) asks for and uses access on its own session,
under the same rules, but its chat shows no private-access UI. Its requests,
one-shot approvals and grants surface in its MAIN chat instead: the top-level
chat at the root of its delegation chain on this worker (``main_session_of``).
The main chat's snapshot lists them labelled with the subagent, and every
answer is still addressed to the subagent's own session. Grants never move
between sessions: the main chat cannot use a subagent's grant, nor the reverse.
A subagent running for another worker has no main chat here; it runs in Away
mode, so its requests are refused at once and listed in its report.
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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

import asyncssh
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt

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


class SavedConnectionEdit(BaseModel):
    """The owner's change to a saved connection. ``None`` keeps a field.

    Secrets are never sent to the client, so a blank secret keeps the stored
    one and a typed secret replaces it.
    """
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=80)
    # SSH machine
    host: str | None = Field(default=None, max_length=64)
    port: StrictInt | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=128)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""))
    private_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    passphrase: SecretStr = Field(default_factory=lambda: SecretStr(""))
    # SSH host-key or HTTPS certificate fingerprint; blank keeps (see apply_saved_edit).
    fingerprint: str | None = Field(default=None, max_length=100)
    # HTTPS API service
    url: str | None = Field(default=None, max_length=2048)
    auth_header: str | None = Field(default=None, max_length=64)
    auth_scheme: str | None = Field(default=None, max_length=32)
    writable: StrictBool | None = None
    secret: SecretStr = Field(default_factory=lambda: SecretStr(""))


MACHINE_EDIT_FIELDS = frozenset({"host", "port", "username", "password", "private_key", "passphrase"})
SERVICE_EDIT_FIELDS = frozenset({"url", "auth_header", "auth_scheme", "writable", "secret"})
POSIX_USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")


def machine_address(host: str) -> str:
    """A unicast IP literal without an interface scope, normalised; never a hostname."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise AccessDenied("Enter the machine IP as an IPv4 or IPv6 literal, not a hostname, URL or credentials.") from None
    if "%" in host or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise AccessDenied("Use a unicast IP address without an interface scope for this machine.")
    return str(address)


def _same_address(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a == b


def apply_saved_edit(current: GrantInput, edit: SavedConnectionEdit) -> GrantInput:
    """The saved connection after ``edit``, validated like the access card.

    Changing a machine's host or port clears its pinned host key, unless the
    owner supplies the new one: the next connection then pins the key the
    server presents. Changing a service's address clears its certificate pin
    the same way. Username and secret changes keep the pin. Error messages
    never quote the submitted values.
    """
    sent = {name for name in edit.model_fields_set
            if getattr(edit, name) is not None and not (name in SECRET_FIELDS and not getattr(edit, name).get_secret_value())}
    other = SERVICE_EDIT_FIELDS if current.kind == "machine" else MACHINE_EDIT_FIELDS
    if sent & other:
        raise AccessDenied(f"Those settings do not apply to a saved {current.kind} connection.")
    update: dict[str, Any] = {}
    if "label" in sent:
        label = edit.label.strip()
        if not label:
            raise AccessDenied("Give the connection a name.")
        update["label"] = label
    fingerprint = (edit.fingerprint or "").strip() if "fingerprint" in sent else ""
    if current.kind == "machine":
        if "host" in sent:
            update["host"] = machine_address(edit.host.strip())
        if "port" in sent:
            update["port"] = edit.port
        if "username" in sent:
            username = edit.username.strip()
            if not POSIX_USERNAME.fullmatch(username):
                raise AccessDenied("Enter a POSIX username: letters, digits, dot, dash or underscore.")
            update["username"] = username
        if fingerprint:
            if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}=?", fingerprint):
                raise AccessDenied("A host-key fingerprint looks like SHA256: followed by 43 characters. "
                                   "Leave it blank to keep the pinned key.")
            update["fingerprint"] = fingerprint.rstrip("=")
        elif not _same_address(update.get("host", current.host), current.host) or update.get("port", current.port) != current.port:
            update["fingerprint"] = ""  # A key pinned for another address is never reused.
        password, key = edit.password.get_secret_value(), edit.private_key.get_secret_value()
        passphrase = edit.passphrase.get_secret_value()
        if password and (key or passphrase):
            raise AccessDenied("Replace the password or the private key, not both.")
        if password:
            update.update(password=edit.password, private_key=SecretStr(""), passphrase=SecretStr(""))
        elif key or passphrase:
            # A new key comes with its own passphrase (blank for none); a
            # passphrase alone replaces the stored key's passphrase.
            key = key or current.private_key.get_secret_value()
            if not key:
                raise AccessDenied("This connection signs in with a password. Enter a private key to use one.")
            try:
                asyncssh.import_private_key(key, passphrase or None)
            except (asyncssh.KeyImportError, ValueError, TypeError):
                raise AccessDenied("The private key could not be read. Check the key and its passphrase.") from None
            update.update(private_key=SecretStr(key), passphrase=SecretStr(passphrase), password=SecretStr(""))
    else:
        if "url" in sent:
            update["url"] = chat_services.parse_origin(edit.url.strip()).url
        header = edit.auth_header if "auth_header" in sent else current.auth_header
        scheme = edit.auth_scheme if "auth_scheme" in sent else current.auth_scheme
        secret = edit.secret.get_secret_value() or current.secret.get_secret_value()
        chat_services.credential_header(header, scheme, secret)
        if "auth_header" in sent:
            update["auth_header"] = header.strip()
        if "auth_scheme" in sent:
            update["auth_scheme"] = scheme.strip()
        if "writable" in sent:
            update["writable"] = edit.writable
        if "secret" in sent:
            update["secret"] = edit.secret
        if fingerprint:
            update["fingerprint"] = chat_services.certificate_pin(fingerprint)
        elif update.get("url", current.url) != current.url:
            update["fingerprint"] = ""  # A certificate pinned for another address is never reused.
    return current.model_copy(update=update)


class RequestUnavailable(AccessDenied):
    """A terminal request failure, distinct from correctable private inputs."""


class GrantSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection: GrantInput | None = None
    saved_id: str = Field(default="", max_length=32)
    minutes: int = Field(default=60, ge=1, le=480)
    save_connection: StrictBool = False
    # A plugin service request is answered with the owner's sign-in, not a
    # connection: ``sign_in`` true, and ``writable`` only where the plugin
    # declares write access.
    sign_in: StrictBool = False
    writable: StrictBool = False


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
    # A plugin service grant: the declared sign-in whose token is attached.
    plugin: Any = None  # app.core.plugin_oauth.OAuthConfig
    revoked: bool = False
    ready: bool = True
    saved_id: str = ""
    timer: asyncio.TimerHandle | None = None
    tasks: set = field(default_factory=set)

    def public(self):
        result = {key: getattr(self, key) for key in (
            "id", "label", "kind", "target", "expires_at", "writable", "fingerprint"
        )}
        if self.plugin is not None:
            result.update(plugin=self.plugin.plugin_id, service=self.plugin.service)
        return result

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
    suggested_host: str = ""
    suggested_port: int | None = None
    suggested_username: str = ""
    service_provider: str = ""
    suggested_url: str = ""
    plugin_id: str = ""
    plugin_service: str = ""
    timer: asyncio.TimerHandle | None = None

    def model_pending(self):
        return {"status": "awaiting_user", "request_id": self.id,
                "kind": self.kind, "expires_at": self.expires_at}

    def public(self):
        result = {key: getattr(self, key) for key in ("id", "kind", "purpose", "expires_at")}
        if self.suggested_path:
            result["suggested_path"] = self.suggested_path
        if self.kind == "machine":
            for key in ("suggested_host", "suggested_port", "suggested_username"):
                if getattr(self, key) not in ("", None):
                    result[key] = getattr(self, key)
        if self.kind == "service":
            result.update(status="awaiting_user", service_provider=self.service_provider,
                          suggested_url=self.suggested_url)
            if self.plugin_id:
                # The card offers "sign in" for this plugin service; its state
                # comes from GET /api/v1/plugins/oauth/status.
                result.update(plugin=self.plugin_id, service=self.plugin_service)
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
    from app.core.api_user_scope import session_api_user_id
    if not row or row.get("mode") not in (None, "code", "chat", "voice"):
        raise AccessDenied("Resource access requires an existing Code, Chat, or Voice session.")
    if owner_of(row):
        raise AccessDenied("Manage access on the device which owns this session. Grants do not travel between devices.")
    # Legacy worker rows inherit their principal from the delegation parent,
    # exactly as session authorization does; NULL is not necessarily the owner.
    return (row.get("owner_device_id"), row.get("instance_id"), session_api_user_id(row))


#: Longest delegation chain walked to find a subagent's main chat.
MAIN_CHAIN_LIMIT = 12
#: Subagent -> main chat links the broker remembers (oldest dropped first).
MAIN_LINK_LIMIT = 4096


def main_session_of(session_id: str) -> str | None:
    """The main chat whose private-access UI shows ``session_id``'s requests.

    ``None`` for a session that is not a subagent (it shows its own), and for a
    subagent whose main chat does not run on this worker (a remote Task child,
    or a missing parent). Workers name their chain's root on this worker as
    their parent, but nested chains are walked anyway.
    """
    from app.core.provider_agent import is_subagent_row
    from app.db import database
    from app.network.control import owner_of
    current, seen, principal = session_id, set(), None
    for _ in range(MAIN_CHAIN_LIMIT):
        row = database.get_session(current)
        try:
            owner = owner_from_row(row)
        except AccessDenied:
            return None
        if principal is None:
            principal = owner
        elif owner[2] != principal[2]:
            return None
        if not is_subagent_row(row):
            return None if current == session_id or owner_of(row) else current
        seen.add(current)
        current = row.get("parent_session_id")
        if not current or current in seen:
            return None
    return None


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


async def publish_change(session_id: str, api_user_id: str | None, *, saved: bool = False,
                         pending: int | None = None) -> None:
    """Tell this worker's UI for the session's principal to refetch its access panel.

    Saved connections belong to the owner, not the session, and every session's
    panel lists them: a saved-list change also invalidates the unscoped
    resource so the owner's panels on other sessions refresh.

    ``pending`` (requests and one-shot approvals waiting for the owner, a bare
    count with nothing private in it) lets the sidebar mark a session that is
    not open as needing the owner. ``None`` when unknown (an older owner).
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
        message = {"type": "chat_access_changed", "session_id": session_id}
        if pending is not None:
            message["pending"] = pending
        await sync_engine._broadcast_user_message(api_user_id or "admin", message)
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
        # Subagent session -> the main chat that shows its access (main_session_of).
        self._main: OrderedDict[str, str] = OrderedDict()

    async def _show_in_main(self, session_id: str, owner: tuple) -> None:
        """Link a subagent to its main chat once, then refresh that chat's panel.

        Runs after an item is registered and announced, so a main chat's lookup
        never delays the session's own notification. A known link was already
        announced to the main chat by that notification.
        """
        if session_id in self._main:
            return
        from app.db.async_database import run_sync
        try:
            main = await run_sync(main_session_of, session_id)
        except Exception:  # noqa: BLE001 — the subagent's own session still holds everything
            logger.debug("Private access: main chat lookup failed for %s", session_id, exc_info=True)
            return
        if not main or session_id in self._main:
            return
        self._main[session_id] = main
        while len(self._main) > MAIN_LINK_LIMIT:
            self._main.popitem(last=False)
        self._notify(session_id, owner)

    def _shown_in(self, session_id: str) -> str:
        """The chat whose panel shows this session's items: its main chat, else itself."""
        return self._main.get(session_id, session_id)

    def _notify(self, session_id: str, owner: tuple, *, saved: bool = False) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Synchronous shutdown has no connected UI to notify.
        from app.network import chat_access_routing
        main = self._main.get(session_id)
        for target in (session_id, main) if main else (session_id,):
            task = loop.create_task(publish_change(target, owner[2] if len(owner) > 2 else None, saved=saved,
                                                   pending=self.pending_count(target)))
            self._notifications.add(task)
            task.add_done_callback(self._notifications.discard)
            # Devices showing this session as a mirror refresh through their own worker.
            chat_access_routing.notify_watchers(target, saved=saved)

    def pending_count(self, session_id: str) -> int:
        """Requests and one-shot approvals waiting for the owner in this chat's panel.

        A main chat's count includes its subagents' items, which show there.
        """
        now = time.time()
        requests = sum(1 for r in self.requests.values()
                       if self._shown_in(r.session_id) == session_id and not r.result.done() and r.expires_at > now)
        actions = sum(1 for a in self.actions.values()
                      if self._shown_in(a.session_id) == session_id and not a.decision.done())
        return requests + actions

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
                      service_provider: str = "", suggested_url: str = "", plugin: str = "",
                      service: str = "", suggested_host: str = "",
                      suggested_port: int | None = None, suggested_username: str = ""):
        owner = await session_owner(session_id)
        if not isinstance(plugin, str) or not isinstance(service, str):
            raise AccessDenied("Name an installed plugin and one service it declares.")
        if plugin or service:
            if kind != "service" or not (plugin and service):
                raise AccessDenied("A plugin sign-in request needs kind service, the plugin id and the service name.")
            if service_provider or suggested_url:
                raise AccessDenied("A plugin service's address comes from the plugin; omit service_provider and suggested_url.")
            config = await _plugin_config(plugin, service)
            # The declared address, shown on the card; the plugin chose it, not the model.
            service_provider = "custom"
            suggested_url = chat_services.parse_origin(config.service_url).url
        if kind not in {"machine", "folder", "service"} or not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise AccessDenied("Specify machine, folder or service and a short explanation of the requested access.")
        if not isinstance(suggested_host, str) or not isinstance(suggested_username, str):
            raise AccessDenied("Machine hints must be an IPv4 or IPv6 literal and a POSIX username. Pass the machine IP, not a hostname.")
        if (suggested_host or suggested_port is not None or suggested_username) and kind != "machine":
            raise AccessDenied("Machine setup hints are only available for machine requests.")
        if suggested_host:
            try:
                address = ipaddress.ip_address(suggested_host)
            except ValueError:
                raise AccessDenied("Pass the machine IP as an IPv4 or IPv6 literal, not a hostname, URL or credentials.") from None
            if "%" in suggested_host or address.is_unspecified or address.is_multicast or address.is_link_local:
                raise AccessDenied("Suggest a unicast machine IP literal without an interface scope.")
        if suggested_port is not None and (type(suggested_port) is not int or not 1 <= suggested_port <= 65535):
            raise AccessDenied("A suggested machine port must be an integer from 1 to 65535.")
        if suggested_username and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", suggested_username):
            raise AccessDenied("Suggest a POSIX username, never credentials or connection strings.")
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
        if kind == "service" and not plugin:
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
                    and pending.service_provider == service_provider and pending.suggested_url == suggested_url
                    and pending.plugin_id == plugin and pending.plugin_service == service):
                try:
                    await _defer_if_away(session_id, "private service access request")
                except AccessDenied:
                    self._finish_request(pending)
                    raise
                return pending.model_pending()
            raise AccessDenied("A private access request is already awaiting the user.")
        duration = SERVICE_PREPARATION_SECONDS if kind == "service" else 240
        request = AccessRequest(uuid.uuid4().hex, session_id, owner, kind, purpose.strip(),
                                time.time() + duration, asyncio.get_running_loop().create_future(),
                                suggested_path=suggested_path, service_provider=service_provider,
                                suggested_url=suggested_url, plugin_id=plugin, plugin_service=service,
                                suggested_host=suggested_host, suggested_port=suggested_port,
                                suggested_username=suggested_username)
        self.requests[request.id] = request
        self._notify(session_id, owner)
        keep_pending = False
        try:
            await self._show_in_main(session_id, owner)
            # Registered first so watchers see it; Away mode then refuses at once.
            await _defer_if_away(session_id, f"private {kind} access request")
            if kind == "service":
                request.timer = asyncio.get_running_loop().call_later(duration, self._finish_request, request)
                keep_pending = True
                return request.model_pending()
            grant_id = await asyncio.wait_for(request.result, 240)
            if not grant_id:
                raise AccessDenied("The user declined the private access request.")
            grant = await self.get(session_id, grant_id)
            return grant.model_public()
        except asyncio.TimeoutError:
            raise AccessDenied("Private input request expired; ask the user before requesting it again.") from None
        finally:
            if not keep_pending:
                self._finish_request(request)

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
            plugin = stored = None
            if request.plugin_id or submission.sign_in:
                # Answered only with the owner's sign-in to the declared service:
                # never a typed key, a saved connection or another address.
                if (not (request.plugin_id and submission.sign_in) or submission.connection is not None
                        or submission.saved_id or submission.save_connection):
                    raise AccessDenied("This request is for a plugin's service: answer it by signing in.")
                plugin = await _plugin_config(request.plugin_id, request.plugin_service)
                spec = GrantInput(kind="service", label=f"{plugin.service} ({plugin.plugin_id})"[:80],
                                  url=plugin.service_url, writable=submission.writable)
            elif submission.saved_id:
                if submission.connection is not None or submission.save_connection:
                    raise AccessDenied("Choose either a saved connection or new private inputs.")
                spec = stored = await run_sync(chat_vault.load, request.owner, submission.saved_id)
            else:
                spec = submission.connection
            if spec is None or request.kind != spec.kind:
                raise AccessDenied("The requested resource type changed.")
            spec = spec.model_copy(update={"minutes": submission.minutes})
            grant = await self.create(session_id, spec, request=request, plugin=plugin)
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
                # Deletion or an edit may race the SSH handshake or the save.
                # Attach the binding before rechecking storage so either one
                # revokes this grant or makes this check fail; a grant on
                # deleted or replaced details cannot survive.
                latest = await run_sync(chat_vault.load, request.owner, saved_id)
                if submission.saved_id and not chat_vault.same_connection(latest, stored):
                    raise AccessDenied("This saved connection changed while connecting. Try again.")
                await self.get(session_id, grant["id"], preparing=True)
            await self._check_request(request)
            current.ready = True
            request.result.set_result(grant["id"])
            self._notify(session_id, request.owner)
            if submission.saved_id and stored.kind == "machine" and not stored.fingerprint.strip():
                # A saved machine without a pinned key (its address was
                # edited) keeps the key this first connection verified, as a
                # newly saved connection does.
                try:
                    if await run_sync(chat_vault.pin_fingerprint, request.owner, saved_id, stored, grant["fingerprint"]):
                        self._notify(session_id, request.owner, saved=True)
                except Exception as exc:  # noqa: BLE001 — the grant stands; the next use pins again
                    logger.warning("Saved connection host key not recorded: %s", type(exc).__name__)
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

    async def edit_saved(self, session_id: str, connection_id: str, edit: SavedConnectionEdit) -> dict:
        """Change one of the owner's saved connections in place.

        Grants built on it are revoked when anything but its name changes, as
        deletion does: they hold the old address and credential, and a grant
        is never silently re-pointed. A rename keeps them. The result carries
        no secret and no submitted value.
        """
        from app.core import chat_vault
        from app.db.async_database import run_sync
        owner = await session_owner(session_id)
        old, new = await run_sync(chat_vault.update, owner, connection_id,
                                  lambda current: apply_saved_edit(current, edit))
        revoked = 0
        if not chat_vault.same_connection(old.model_copy(update={"label": new.label}), new):
            for grant in list(self.grants.values()):
                if grant.owner[2:] == owner[2:] and grant.saved_id == connection_id:
                    self.revoke(grant.session_id, grant.id)
                    revoked += 1
        self._notify(session_id, owner, saved=True)
        return {"updated": True, "revoked": revoked,
                "host_key_cleared": bool(old.fingerprint) and not new.fingerprint}

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

    async def create(self, session_id: str, spec: GrantInput, *, request: AccessRequest | None = None,
                     plugin=None) -> dict:
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
                        "For Docker, use a mounted container path, not an unmounted host path."
                    ) from None
                except NotADirectoryError:
                    raise AccessDenied("Choose a folder, not a file or symbolic link.") from None
                except PermissionError:
                    raise AccessDenied("The worker cannot read this folder. Choose a folder its account can access.") from None
            elif spec.kind == "service" and plugin is not None:
                # The plugin's declared address; the token is fetched per request.
                origin = chat_services.parse_origin(plugin.service_url)
                if spec.writable and plugin.access != "write":
                    raise AccessDenied("This plugin declares read-only access to this service; grant it read-only.")
                from app.core import plugin_oauth
                from app.db.async_database import run_sync
                try:
                    state = (await run_sync(plugin_oauth.status, plugin))["state"]
                except plugin_oauth.PluginOAuthError as exc:
                    raise AccessDenied(str(exc)) from None
                if state != "signed_in":
                    raise AccessDenied(f"Sign in to {plugin.service} first, then grant access.")
                await chat_services.verify_origin(origin)
                grant.origin, grant.plugin, grant.target = origin, plugin, origin.url
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
        actions = [a.public() for a in self.actions.values()
                   if a.session_id == session_id and not a.decision.done()]
        requests = [r.public() for r in self._live_requests(session_id, owner)]
        await self._add_subagents(session_id, owner, result, actions, requests)
        return {
            "saved_connections": saved,
            "vault_available": encryption.is_encryption_ready(),
            "grants": result,
            "actions": actions,
            "requests": requests,
            "folder_available": chat_files.available(),
            "restart_revokes": True,
        }

    async def _add_subagents(self, main: str, main_owner: tuple, grants: list, actions: list, requests: list) -> None:
        """Append the items of the subagents whose access shows in ``main``.

        Each carries ``session_id`` (where the owner's answer goes) and
        ``subagent`` (its chat title), so the panel says who is asking.
        """
        children = [child for child, shown in list(self._main.items()) if shown == main and (
            any(g.session_id == child for g in self.grants.values())
            or any(a.session_id == child for a in self.actions.values())
            or any(r.session_id == child for r in self.requests.values()))]
        if not children:
            return
        from app.db import database, turns
        from app.db.async_database import run_sync
        from app.core.task_registry import task_registry
        for child in children:
            try:
                row = await run_sync(database.get_session, child)
                owner = owner_from_row(row)
                valid_link = owner[2] == main_owner[2] and await run_sync(main_session_of, child) == main
            except AccessDenied:
                valid_link = False
            if not valid_link:
                self._main.pop(child, None)
                continue
            label = {"session_id": child, "subagent": (row.get("title") or "").strip() or "Subagent"}
            grants.extend({**g.public(), **label} for g in self._live_grants(child, owner))
            # Session rows usually remain "active" after a turn ends. The durable
            # turn state and Stop latch, not the sidebar's is_active bit, decide
            # whether a subagent can still be waiting for private input.
            turn = await run_sync(turns.get_latest_run_turn, child)
            finished = turn is not None and turn["state"] in ("completed", "aborted")
            if finished:
                from app.core.provider_agent import _active_worker_background_work
                from app.core.user_question_handler import user_question_handler
                # A removed queue chip or completed follow-up can be newer than
                # the live/parked turn that still owns the request. Posted
                # questions also outlive their turn and resume it on an answer.
                finished = (not await run_sync(turns.session_has_unsettled_turns, child)
                            and not _active_worker_background_work(child)
                            and not task_registry.finished_awaiting_report(child)
                            and not await run_sync(user_question_handler.get_pending_questions, child))
            terminal = (row.get("status") in ("archived", "completed", "stopped", "failed", "error", "killed")
                        or task_registry.is_user_stopped(child)
                        or finished)
            if terminal:
                for request in list(self.requests.values()):
                    if request.session_id == child:
                        self._finish_request(request)
                for action in self.actions.values():
                    if action.session_id == child and not action.decision.done():
                        action.decision.set_result(False)
                continue
            actions.extend({**a.public(), **label} for a in self.actions.values()
                           if a.session_id == child and not a.decision.done())
            requests.extend({**r.public(), **label} for r in self._live_requests(child, owner))

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
            await self._show_in_main(grant.session_id, grant.owner)
            await _defer_if_away(grant.session_id, f"private access action: {operation}")
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
            if grant.plugin is not None:
                # Refuse before asking for approval when the plugin changed.
                await _current_plugin(grant)
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
            elif grant.plugin is not None:
                work = _plugin_http(grant, arguments)
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


async def _plugin_config(plugin_id: str, service: str):
    """The sign-in an installed, switched-on and approved plugin declares for
    ``service``; refusals are plain AccessDenied wording without secrets."""
    from app.core import plugin_oauth
    from app.db.async_database import run_sync
    try:
        return await run_sync(plugin_oauth.installed_config, plugin_id, service)
    except plugin_oauth.PluginOAuthError as exc:
        raise AccessDenied(str(exc)) from None


async def _current_plugin(grant: Grant):
    """The grant's plugin service as declared now; refused when the plugin was
    turned off or its declaration (address, endpoints, scopes, access) changed."""
    current = await _plugin_config(grant.plugin.plugin_id, grant.plugin.service)
    if current != grant.plugin:
        raise AccessDenied("The plugin's declaration for this service changed. Request access again.")
    return current


def _sign_in_problem(service: str, exc: BaseException) -> AccessDenied:
    return AccessDenied(f"The sign-in for {service} could not be used. {exc}")


async def _plugin_http(grant: Grant, arguments: dict) -> dict:
    """One request with the plugin service's access token. On a 401 the token
    is refreshed once and the request sent once more. The token is attached
    here and redacted from what is returned; it never reaches a tool result,
    an exception message or a log."""
    from app.core import plugin_oauth
    config = await _current_plugin(grant)
    request = (arguments["method"], arguments["path"], arguments["body"], arguments["content_type"])
    try:
        token = await plugin_oauth.access_token_for_use(config)
    except plugin_oauth.PluginOAuthError as exc:
        raise _sign_in_problem(config.service, exc) from None
    result = await chat_services.perform(grant.origin, ("Authorization", f"Bearer {token}"), token, *request)
    if result["status"] != 401:
        return result
    try:
        fresh = await plugin_oauth.access_token_for_use(config, force_refresh=True)
    except plugin_oauth.PluginOAuthError as exc:
        raise _sign_in_problem(config.service, exc) from None
    return await chat_services.perform(grant.origin, ("Authorization", f"Bearer {fresh}"), fresh, *request,
                                       also_redact=(token,))


async def _defer_if_away(session_id: str, action: str) -> None:
    """Away mode never approves private access: refuse at once instead of waiting 4 minutes."""
    try:
        from app.core.away_mode import defer_if_away
        deferred = await defer_if_away(session_id, action)
    except Exception:  # noqa: BLE001 — fall back to asking as usual
        return
    if deferred:
        raise AccessDenied(deferred)
