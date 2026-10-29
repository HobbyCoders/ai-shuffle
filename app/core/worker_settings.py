"""Typed, one-shot machine settings; never a URL, shell proxy or offline queue.

Claude sign-in is deliberately status-only. A future sign-in operation belongs in
Action with its own consent/transport contract, not in a forwarded terminal API.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, TypeAdapter, ValidationError

from app.core import application_updates, claude_cli_install, claude_cli_status, worker_availability
from app.core.auth import auth_service
from app.core.config import settings
from app.db import database
from app.db.async_database import run_sync
from app.network import control, engine, membership, runners


class Input(BaseModel):
    model_config = {"extra": "forbid"}


class ClaudeInstall(Input):
    operation: Literal["claude-install", "claude-update"]


class ClaudeKeep(Input):
    operation: Literal["claude-keep-installed"]
    keep_installed: StrictBool


class ReleaseChannel(Input):
    operation: Literal["release-channel"]
    channel: Literal["stable", "nightly"]


class AlwaysOn(Input):
    operation: Literal["always-on"]
    always_on: StrictBool | None = Field(...)


Action = Annotated[ClaudeInstall | ClaudeKeep | ReleaseChannel | AlwaysOn, Field(discriminator="operation")]


class Dispatch(Input):
    target: str = Field(min_length=1, max_length=200)
    action: Action


class StatusRequest(Input):
    operation: Literal["status"]


class ActionRequest(Input):
    operation: Literal["action"]
    request_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expires_at: StrictInt | StrictFloat
    action: Action


PEER_REQUEST = TypeAdapter(Annotated[StatusRequest | ActionRequest, Field(discriminator="operation")])


def parse_request(body):
    try:
        return PEER_REQUEST.validate_python(body)
    except ValidationError:
        # ValidationError includes input values. Do not reflect private inputs.
        raise ValueError("Invalid worker settings operation or arguments") from None


# Explicit output schemas are also the peer-response filter. Never send a CLI
# object, raw installer output, binary/config paths, credentials or env values.
class Job(BaseModel):
    kind: Literal["install", "update", "remove"]
    state: Literal["running", "succeeded", "failed"]


class ClaudeStatus(BaseModel):
    installed: StrictBool
    version: str | None = None
    keep_installed: StrictBool
    installed_by_app: StrictBool
    can_install: StrictBool
    signed_in: StrictBool | None = None
    email: str | None = None
    plan: str | None = None
    status_available: StrictBool = True
    job: Job | None = None


class ChannelStatus(BaseModel):
    channel: Literal["stable", "nightly"]
    editable: StrictBool
    restart_required: StrictBool


class MachineStatus(BaseModel):
    claude: ClaudeStatus | None = None
    github_signed_in: StrictBool | None = None
    release_channel: ChannelStatus | None = None
    # No login action is implemented/accepted. Reserved UI extension point.
    claude_sign_in_supported: Literal[False] = False


Outcome = Literal["ok", "accepted", "offline", "unsupported", "refused", "uncertain"]
Reason = Literal["done", "saved", "offline", "unsupported", "refused", "uncertain", "busy",
                 "not-installed", "not-owned", "platform", "channel-refused", "unknown-instance"]


class Result(BaseModel):
    outcome: Outcome
    reason: Reason
    status: MachineStatus | None = None


FANOUT = 8
READ_TIMEOUT = 3.0
CACHE_SECONDS = 5.0
_readings = {}
_receipts = OrderedDict()
_action_lock = asyncio.Lock()


def _result(outcome, reason=None, **fields):
    return Result(outcome=outcome, reason=reason or outcome, **fields).model_dump()


async def _cached(key, read):
    """Share in-flight reads, including a slow CLI probe; no subprocess stampede."""
    now = time.monotonic()
    entry = _readings.get(key)
    if entry is None or (entry[0].done() and now >= entry[1]):
        async def safe_read():
            try:
                return await read()
            except Exception:
                # Never echo exception/CLI output, which may contain private data.
                return None
        entry = (asyncio.create_task(safe_read()), now + CACHE_SECONDS)
        _readings[key] = entry
    try:
        return await asyncio.wait_for(asyncio.shield(entry[0]), timeout=READ_TIMEOUT)
    except TimeoutError:
        return None


def _claude():
    install = claude_cli_install.payload()
    cli = claude_cli_status.get_status()
    return ClaudeStatus(**{key: install[key] for key in (
        "installed", "version", "keep_installed", "installed_by_app", "can_install", "job")},
        signed_in=cli.signed_in, email=cli.email, plan=cli.plan,
        status_available=cli.error is None).model_dump()


async def local_status():
    claude, github, channel = await asyncio.gather(
        _cached("claude", lambda: run_sync(_claude)),
        _cached("github", lambda: run_sync(auth_service.is_github_authenticated)),
        _cached("channel", lambda: run_sync(application_updates.channel_status)),
    )
    return MachineStatus(claude=claude, github_signed_in=github, release_channel=channel).model_dump()


def _invalidate():
    for key in ("claude", "channel"):
        _readings.pop(key, None)


def _local_action(action):
    if isinstance(action, ReleaseChannel):
        # The same platform, launcher acknowledgement and downgrade checks as
        # the local Updates picker. Never write update-channel.json ourselves.
        try:
            application_updates.set_channel(action.channel)
        except ValueError:
            return _result("refused", "channel-refused")
    elif isinstance(action, ClaudeKeep):
        claude_cli_install.set_keep_installed(action.keep_installed)
    elif isinstance(action, ClaudeInstall):
        state = claude_cli_install.payload()
        if not state["can_install"]:
            return _result("unsupported", "platform")
        if ((state.get("job") and state["job"]["state"] == "running")
                or claude_cli_install._in_use()):
            return _result("refused", "busy")
        if action.operation == "claude-update" and not state["installed"]:
            return _result("refused", "not-installed")
        if state["installed"] and not state["installed_by_app"]:
            return _result("refused", "not-owned")
        try:
            claude_cli_install.start(action.operation.removeprefix("claude-"))
        except ValueError:
            return _result("refused", "busy")
    else:
        raise ValueError("Availability is shared data, not a peer operation")
    return _result("accepted", "saved" if isinstance(action, ClaudeKeep) else "done")


async def apply_once(body: ActionRequest, from_device: str):
    """Remember a receipt before dispatch; even an uncertain retry cannot execute twice."""
    now = time.time()
    if not now <= body.expires_at <= now + 15:
        raise ValueError("Request expired; offline commands are not replayed")
    if isinstance(body.action, AlwaysOn):
        raise ValueError("Availability is shared data, not a peer operation")
    key = (from_device, body.request_id)
    async with _action_lock:
        now = time.time()
        if not now <= body.expires_at <= now + 15:
            raise ValueError("Request expired; offline commands are not replayed")
        for old, (expiry, _, _) in list(_receipts.items()):
            if expiry < now:
                del _receipts[old]
        fingerprint = body.action.model_dump_json()
        if key in _receipts:
            _, previous, result = _receipts[key]
            if previous != fingerprint:
                raise ValueError("Request id already used for a different action")
            return result
        if len(_receipts) >= 1024:
            return _result("refused")
        _receipts[key] = (body.expires_at, fingerprint, _result("uncertain"))
        try:
            result = await run_sync(_local_action, body.action)
        except Exception:
            result = _result("uncertain")
        _receipts[key] = (body.expires_at, fingerprint, result)
        _invalidate()
        return result


async def peer_action(body, from_device):
    ctx = engine.context()
    member = ctx.member(from_device) if ctx else None
    if (not member or from_device == ctx.device_id or member.kind != membership.KIND_WORKER
            or member.status != membership.STATUS_ACTIVE):
        raise PermissionError("Only another active account worker may manage machine settings")
    request = parse_request(body)
    if isinstance(request, StatusRequest):
        return _result("ok", "done", status=await local_status())
    return await apply_once(request, from_device)


async def _remote(row, body, *, action="worker-settings"):
    try:
        raw = await asyncio.wait_for(control.call(row["device_id"], action, body, timeout=3), timeout=4)
        return Result.model_validate(raw).model_dump()
    except control.ControlRefused as exc:
        # control preserves the original HTTP error as its cause. 404 is the
        # old-build allow-list refusal; 401/403 are NOT "unsupported".
        cause = str(exc.__cause__ or "")
        unsupported = cause.startswith("HTTP 404") or str(exc) == "Unknown control action"
        return _result("unsupported" if unsupported else "refused")
    except control.MaybeApplied:
        return _result("uncertain")
    except control.RunnerUnreachable:
        return _result("offline")
    except Exception:
        # A timeout, invalid reply or server failure after dispatch is not safe
        # to retry automatically, even if the peer looked online beforehand.
        return _result("uncertain")


def _directory():
    listing = runners.directory()
    rows = listing["runners"]
    if not any(row["is_self"] for row in rows):
        from app.core.cluster import default_device_name
        local = database.get_install(settings.effective_instance_id)
        rows = [{"device_id": listing.get("self_device_id") or "local",
                 "device_name": (local or {}).get("device_name") or default_device_name(), "is_self": True,
                 "online": True, "capabilities": {}}, *rows]
    return rows


def _instance(row):
    if row["is_self"]:
        return settings.effective_instance_id
    if row.get("legacy_instance_id"):
        return row["legacy_instance_id"]
    aliases = [alias for alias, device in engine.instance_aliases().items() if device == row["device_id"]]
    return aliases[0] if len(aliases) == 1 else None


def _availability(row):
    instance = _instance(row)
    override = None
    if instance:
        raw = database.get_system_setting_cached(worker_availability.SETTING_PREFIX + instance)
        override = {"true": True, "false": False}.get(raw)
    desktop = worker_availability.is_desktop() if row["is_self"] else row.get("capabilities", {}).get("desktop")
    effective = override if override is not None else (not desktop if type(desktop) is bool else None)
    return {"editable": instance is not None, "always_on": effective, "always_on_override": override}


async def snapshot():
    rows = await run_sync(_directory)
    semaphore = asyncio.Semaphore(FANOUT)

    async def read(row):
        availability = await run_sync(_availability, row)
        if row["is_self"]:
            result = _result("ok", "done", status=await local_status())
        elif not row["online"]:
            result = _result("offline")
        else:
            async def read_peer():
                # Hold a slot until the underlying call ends, even if the UI's
                # short read budget has elapsed and the task is cached in flight.
                async with semaphore:
                    return await _remote(row, {"operation": "status"})
            result = await _cached("peer:" + row["device_id"], read_peer)
            result = result or _result("uncertain")
        return {**{key: row[key] for key in ("device_id", "device_name", "is_self", "online")},
                **result, "availability": availability}

    return {"workers": await asyncio.gather(*(read(row) for row in rows))}


async def dispatch(request: Dispatch):
    rows = await run_sync(_directory)
    selected = rows if request.target == "all" else [r for r in rows if r["device_id"] == request.target]
    if not selected:
        raise LookupError("Unknown worker; no action was sent")
    request_id = str(uuid.uuid4())
    body = ActionRequest(operation="action", request_id=request_id, expires_at=time.time() + 15,
                         action=request.action)
    semaphore = asyncio.Semaphore(FANOUT)

    async def send(row):
        async with semaphore:
            if isinstance(request.action, AlwaysOn):
                instance = await run_sync(_instance, row)
                if instance is None:
                    result = _result("unsupported", "unknown-instance")
                else:
                    try:
                        await run_sync(worker_availability.save_for_instance, instance, request.action.always_on)
                    except Exception:
                        result = _result("uncertain")
                    else:
                        result = _result("accepted", "saved")
            elif not row["online"]:
                result = _result("offline")
            elif row["is_self"]:
                try:
                    result = await apply_once(body, "local")
                except ValueError:
                    result = _result("refused")
            else:
                # Channel writes extend the existing typed update control. The
                # receiver shares the same receipt/validation path, no fanout.
                if isinstance(request.action, ReleaseChannel):
                    # Older update handlers predate this payload. Check the new
                    # family first, so an old build is unsupported, not an
                    # ambiguously delivered update command.
                    supported = await _cached("peer:" + row["device_id"],
                                              lambda: _remote(row, {"operation": "status"}))
                    if not supported or supported["outcome"] != "ok":
                        result = supported or _result("uncertain")
                    else:
                        result = await _remote(row, body.model_dump(), action="application-update")
                else:
                    result = await _remote(row, body.model_dump())
            _readings.pop("peer:" + row["device_id"], None)
            return {"device_id": row["device_id"], "device_name": row["device_name"], **result}

    return {"request_id": request_id, "workers": await asyncio.gather(*(send(row) for row in selected))}
