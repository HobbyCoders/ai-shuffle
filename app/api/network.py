"""Worker-network API: status and administration, peer sync, enrollment.

Three audiences share the ``/api/v1/network`` prefix:

- users of this worker's UI (``require_auth`` / ``require_admin``):
  status, members, conflicts, recovery key, joining another worker
- peer workers presenting a device token (``tokens.require_device``):
  ``/peer/*`` exchange, ops, snapshot, ping
- a joining worker with a pairing code (unauthenticated, rate limited):
  ``POST /join`` and ``GET /join/{id}``

A valid one-time code admits the joiner on contact; account sign-in admits
devices through the hub. Neither needs a second approval on another screen.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.api.auth import require_admin, require_auth
from app.core.request_identity import get_client_ip
from app.api.pairing import _ExchangeRateLimiter
from app.core.config import settings
from app.db import network as store
from app.db.async_database import run_sync
from app.network import engine, enrollment, identity as identity_mod, policy, recovery, runners, tokens, transport
from app.network import endpoints as endpoints_mod
from app.api.network_usage import router as usage_budget_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/network", tags=["Network"])
router.include_router(usage_budget_router)
_join_limiter = _ExchangeRateLimiter(limit=10, window_seconds=60)
# A peer authenticates at most a few times a minute per endpoint it tries.
_challenge_limiter = _ExchangeRateLimiter(limit=60, window_seconds=60)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CreateNetwork(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    device_name: Optional[str] = Field(default=None, max_length=100)


class RenameMember(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DeviceDescription(BaseModel):
    device_id: Optional[str] = Field(default=None, max_length=64)
    signing_public: str = Field(min_length=40, max_length=64)
    encryption_public: str = Field(min_length=40, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    platform: Optional[str] = Field(default=None, max_length=60)
    kind: str = Field(default="worker", max_length=20)
    legacy_instance_id: Optional[str] = Field(default=None, max_length=200)
    # Ed25519 signature by the device's own signing key over its identity
    # fields (see ``membership.sign_device_description``).
    proof: Optional[str] = Field(default=None, min_length=64, max_length=128)


class JoinRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    device: DeviceDescription
    inviter_fingerprint: Optional[str] = Field(default=None, max_length=64)


class ApproveJoin(BaseModel):
    # Kept for older clients; the one-time code (or the account) is the authorization.
    fingerprint_confirmed: bool = True


class EnrollRequest(BaseModel):
    endpoint_url: Optional[str] = Field(default=None, max_length=2048)
    code: Optional[str] = Field(default=None, max_length=64)
    inviter_fingerprint: Optional[str] = Field(default=None, max_length=64)
    link: Optional[str] = Field(default=None, max_length=4096)
    device_name: Optional[str] = Field(default=None, max_length=100)
    # An enrolled worker may leave its own network to join the invited one;
    # the account's or inviter's records win over local copies with the same id.
    replace: bool = False


class ChallengeRequest(BaseModel):
    device_id: str = Field(min_length=4, max_length=64)


class TokenRequest(BaseModel):
    device_id: str = Field(min_length=4, max_length=64)
    challenge: str = Field(min_length=16, max_length=128)
    signature: str = Field(min_length=64, max_length=128)
    incarnation: int = Field(default=1, ge=1)
    endpoints: List[str] = Field(default_factory=list, max_length=8)
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    version: Optional[str] = Field(default=None, max_length=40)
    platform: Optional[str] = Field(default=None, max_length=40)
    name: Optional[str] = Field(default=None, max_length=100)

    # Member-advertised addresses are stored and later dialed: keep only
    # http(s) URLs with a plain host (LAN addresses are the normal case).
    @field_validator("endpoints")
    @classmethod
    def _usable_endpoints(cls, value: List[str]) -> List[str]:
        return endpoints_mod.validate_endpoints(value)


class ExchangeRequest(BaseModel):
    flush: bool = False
    watermarks: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    limit: int = Field(default=500, ge=1, le=2000)
    lane: Optional[str] = None
    incarnation: Optional[int] = Field(default=None, ge=1)
    endpoints: List[str] = Field(default_factory=list, max_length=8)
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    version: Optional[str] = Field(default=None, max_length=40)

    @field_validator("endpoints")
    @classmethod
    def _usable_endpoints(cls, value: List[str]) -> List[str]:
        return endpoints_mod.validate_endpoints(value)


class OpsRequest(BaseModel):
    ops: List[Dict[str, Any]] = Field(default_factory=list, max_length=2000)





# ---------------------------------------------------------------------------
# Status and administration
# ---------------------------------------------------------------------------

def _status() -> dict:
    ctx = engine.context()
    ident = identity_mod.load()
    base = {"device": ident.public_summary(), "enrolled": ctx is not None,
            "platform": identity_mod.platform_name(), "version": settings.version,
            "instance_id": settings.effective_instance_id}
    if ctx is None:
        base["enrollment"] = enrollment.enrollment_status()
        return base
    counts = store.count_records()
    base.update({
        "network": ctx.summary(),
        "members": sorted((_member_view(m, ctx) for m in store.list_members()),
                          key=lambda m: (not m["is_self"], m["status"] != "active", m["name"].lower())),
        "pending_joins": enrollment.pending_joins(),
        "sync": {"records": counts, "ops": store.count_ops(), "holes": store.pending_holes(),
                 "peers": transport.link_status(), "conflicts": len(store.list_conflicts(limit=1000))},
        "recovery": recovery.status(),
        "hub": _hub_status(),
        "startup_sync": _startup_sync_status(),
        "blobs": _blob_status(),
    })
    return base


def _blob_status() -> dict:
    try:
        from app.network import blobs
        return blobs.blob_status()
    except Exception:  # noqa: BLE001
        return {"missing": 0, "bytes_missing": 0}


def _startup_sync_status() -> dict | None:
    from app.network import startup_sync
    return startup_sync.status()


def _hub_status() -> dict:
    try:
        from app.network import hub
        return hub.status()
    except Exception:  # noqa: BLE001
        return {"linked": False}


def _member_view(row: dict, ctx) -> dict:
    presence = engine.presence_of(row["device_id"]) or {}
    online = transport.online_devices()
    is_self = row["device_id"] == ctx.device_id
    return {
        "device_id": row["device_id"], "name": row["name"], "kind": row["kind"], "platform": row.get("platform") or presence.get("platform"),
        "status": row["status"], "generation": row["generation"], "is_self": is_self,
        "fingerprint": _fingerprint(row), "added_by": row.get("added_by"), "created_at": row.get("created_at"),
        "last_seen": row.get("last_seen"), "endpoints": presence.get("endpoints") or row.get("endpoints") or [],
        "capabilities": presence.get("capabilities") or row.get("capabilities") or {},
        "version": presence.get("version") or row.get("version"),
        "online": is_self or row["device_id"] in online,
        "legacy_instance_id": row.get("legacy_instance_id"),
    }


def _fingerprint(row: dict) -> Optional[str]:
    from app.network import crypto
    try:
        return crypto.fingerprint(crypto.b64u_decode(row["signing_public"]))
    except Exception:  # noqa: BLE001
        return None


@router.get("/status")
async def network_status(_: str = Depends(require_auth)) -> dict:
    return await run_sync(_status)


@router.get("/policy")
async def network_policy(_: str = Depends(require_auth)) -> dict:
    return {"collections": policy.inventory(), "settings": policy.settings_policy_table()}


@router.post("/create", status_code=status.HTTP_201_CREATED)
async def create_network(body: CreateNetwork, _: str = Depends(require_admin)) -> dict:
    if engine.context() is not None:
        raise HTTPException(409, "This worker already belongs to a network")
    try:
        ctx = await run_sync(engine.create_network, body.name, device_name=body.device_name,
                             legacy_instance_id=settings.effective_instance_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await run_sync(engine.publish_presence, version=settings.version)
    return {"network": ctx.summary()}


@router.get("/members")
async def list_members(_: str = Depends(require_auth)) -> dict:
    ctx = engine.context()
    if ctx is None:
        return {"members": []}
    return {"members": await run_sync(lambda: [_member_view(m, ctx) for m in store.list_members()])}


@router.patch("/members/{device_id}")
async def rename_member(device_id: str, body: RenameMember, _: str = Depends(require_admin)) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Device name is required")
    ctx = engine.context()
    if ctx is None or ctx.member(device_id) is None:
        raise HTTPException(404, "Unknown device")
    row = await run_sync(store.rename_member, device_id, name)
    ctx = await run_sync(engine.load_context)
    if device_id == ctx.device_id:
        await run_sync(engine.publish_presence, version=settings.version)
    from app.network import hub
    hub.kick()
    return _member_view(row, ctx)


@router.delete("/members/{device_id}")
async def revoke_member(device_id: str, _: str = Depends(require_admin)) -> dict:
    try:
        result = await run_sync(engine.revoke_device, device_id)
    except engine.NotEnrolled as exc:
        raise HTTPException(404, str(exc)) from exc
    except KeyError:
        raise HTTPException(404, "Unknown device") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "rotate_provider_keys_recommended": True}


@router.get("/conflicts")
async def list_conflicts(_: str = Depends(require_admin)) -> dict:
    rows = await run_sync(store.list_conflicts)
    # Stored rows are redacted at write and at startup; this is the last guard.
    return {"conflicts": [{**row, "losing_record": engine.redact_secrets(row.get("losing_record"))} for row in rows]}


@router.delete("/conflicts/{conflict_id}", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_conflict(conflict_id: int, _: str = Depends(require_admin)) -> Response:
    if not await run_sync(store.delete_conflict, conflict_id):
        raise HTTPException(404, "Conflict not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runners")
async def list_runners(_: str = Depends(require_auth)) -> dict:
    return await run_sync(runners.directory)


class OwnerAssignment(BaseModel):
    table: str = Field(pattern="^(automations|connectors)$")
    id: str = Field(min_length=1, max_length=200)
    device_id: Optional[str] = Field(default=None, max_length=64)


@router.post("/owners")
async def assign_owner(body: OwnerAssignment, _: str = Depends(require_admin)) -> dict:
    """Hand an automation or connector to a device (None leaves it inactive)."""
    from app.network import ownership
    try:
        return await run_sync(ownership.assign, body.table, body.id, body.device_id)
    except engine.NotEnrolled as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError:
        raise HTTPException(404, "Unknown device") from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Recovery key
# ---------------------------------------------------------------------------

@router.get("/recovery")
async def recovery_status(_: str = Depends(require_admin)) -> dict:
    return await run_sync(recovery.status)


@router.post("/recovery", status_code=status.HTTP_201_CREATED)
async def create_recovery(_: str = Depends(require_admin)) -> dict:
    try:
        return await run_sync(recovery.create)
    except (engine.NotEnrolled, recovery.RecoveryError) as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------------------------------------------------------------------------
# Account (hub) device approvals
# ---------------------------------------------------------------------------

@router.get("/hub/devices")
async def hub_pending_devices(_: str = Depends(require_admin)) -> dict:
    from app.network import hub
    try:
        return {"devices": await run_sync(hub.pending_devices)}
    except hub.HubError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/hub/devices/{device_id}/approve")
async def hub_approve_device(device_id: str, body: ApproveJoin, _: str = Depends(require_admin)) -> dict:
    from app.network import hub
    try:
        return await run_sync(hub.approve_device, device_id, fingerprint_confirmed=body.fingerprint_confirmed)
    except hub.HubError as exc:
        raise HTTPException(400, str(exc)) from exc
    except engine.NotEnrolled as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/hub/devices/{device_id}/reject")
async def hub_reject_device(device_id: str, _: str = Depends(require_admin)) -> dict:
    from app.network import hub
    try:
        return await run_sync(hub.reject_device, device_id)
    except hub.HubError as exc:
        raise HTTPException(400, str(exc)) from exc


class AdoptAccountNetwork(BaseModel):
    device_name: Optional[str] = Field(default=None, max_length=100)


@router.post("/hub/adopt")
async def hub_adopt_account_network(body: AdoptAccountNetwork, _: str = Depends(require_admin)) -> dict:
    """Leave this worker's own network and join the account's (its records win)."""
    from app.network import hub
    try:
        return await hub.adopt_account_network(device_name=body.device_name)
    except hub.HubError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------------------------------------------------------------------------
# Joining another worker (this worker is the joiner)
# ---------------------------------------------------------------------------

@router.post("/enroll", status_code=status.HTTP_202_ACCEPTED)
async def enroll(body: EnrollRequest, _: str = Depends(require_admin)) -> dict:
    endpoint, code, fingerprint = body.endpoint_url, body.code, body.inviter_fingerprint
    if body.link:
        try:
            parsed = enrollment.parse_join_link(body.link)
        except enrollment.EnrollmentError as exc:
            raise HTTPException(400, str(exc)) from exc
        endpoint = endpoint or parsed["endpoint"]
        code = code or parsed["code"]
        fingerprint = fingerprint or parsed["fingerprint"]
    if not endpoint or not code:
        raise HTTPException(400, "The worker address and the join code are required")
    if engine.context() is not None and not body.replace:
        raise HTTPException(409, "This worker already belongs to a network. Confirm replacing its settings to join another.")
    try:
        return await enrollment.start_enrollment(endpoint_url=endpoint, code=code, inviter_fingerprint=fingerprint or None,
                                                 device_name=body.device_name, leave_current=body.replace)
    except enrollment.EnrollmentError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/enroll")
async def enroll_status(_: str = Depends(require_auth)) -> dict:
    return {"enrollment": await run_sync(enrollment.enrollment_status), "enrolled": engine.context() is not None}


@router.delete("/enroll", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_enroll(_: str = Depends(require_admin)) -> Response:
    await run_sync(enrollment.cancel_enrollment)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Joins arriving at this worker (this worker is the inviter)
# ---------------------------------------------------------------------------

@router.post("/join", status_code=status.HTTP_202_ACCEPTED)
async def receive_join(body: JoinRequest, request: Request) -> dict:
    client_ip = get_client_ip(request)
    _join_limiter.check(client_ip)
    try:
        result = await run_sync(enrollment.receive_join, code=body.code, device=body.device.model_dump(),
                                inviter_fingerprint=body.inviter_fingerprint)
    except enrollment.EnrollmentError as exc:
        raise HTTPException(400, str(exc)) from exc
    _join_limiter.clear(client_ip)
    return result


@router.get("/join/{join_id}")
async def poll_join(join_id: str, secret: str, request: Request) -> dict:
    _join_limiter.check(get_client_ip(request))
    try:
        return await run_sync(enrollment.poll_join, join_id, secret)
    except enrollment.EnrollmentError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/joins")
async def list_joins(_: str = Depends(require_admin)) -> dict:
    return {"joins": await run_sync(enrollment.pending_joins)}


@router.post("/joins/{join_id}/approve")
async def approve_join(join_id: str, body: ApproveJoin, _: str = Depends(require_admin)) -> dict:
    try:
        return await run_sync(enrollment.approve_join, join_id, fingerprint_confirmed=body.fingerprint_confirmed)
    except enrollment.EnrollmentError as exc:
        raise HTTPException(400, str(exc)) from exc
    except engine.NotEnrolled as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/joins/{join_id}/reject")
async def reject_join(join_id: str, _: str = Depends(require_admin)) -> dict:
    try:
        return await run_sync(enrollment.reject_join, join_id)
    except enrollment.EnrollmentError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Peer plane (device tokens)
# ---------------------------------------------------------------------------

@router.post("/peer/auth/challenge")
async def peer_challenge(body: ChallengeRequest, request: Request) -> dict:
    # Unauthenticated by nature; bounded per client address, issued only to
    # a named active member and spendable only by that member's key.
    requester = get_client_ip(request)
    _challenge_limiter.check(requester)
    member_address = False
    if tokens.at_capacity():
        # Past the global ceiling only member addresses get a challenge: one
        # the member advertises as its own endpoint is recognised here (one
        # that has spent a challenge before, inside ``tokens``).
        row = await run_sync(store.get_member, body.device_id)
        member_address = tokens.is_member_endpoint(requester, (row or {}).get("endpoints") or [])
    return {"challenge": tokens.new_challenge(body.device_id, requester, member_address=member_address),
            "expires_in": tokens.CHALLENGE_TTL_SECONDS}


@router.post("/peer/auth/token")
async def peer_token(body: TokenRequest) -> dict:
    result = tokens.issue_token(device_id=body.device_id, challenge=body.challenge, signature=body.signature)
    await run_sync(store.touch_member, body.device_id, capabilities=body.capabilities or None,
                   endpoints=body.endpoints or None, version=body.version, incarnation=body.incarnation,
                   platform=body.platform)
    transport.note_contact(body.device_id)
    return {**result, "expires_in": tokens.TOKEN_TTL_SECONDS}


@router.get("/peer/ping")
async def peer_ping(device_id: str = Depends(tokens.require_device)) -> dict:
    ctx = engine.require_context()
    transport.note_contact(device_id)
    return {"device_id": ctx.device_id, "network_id": ctx.network_id, "generation": ctx.generation,
            "incarnation": ctx.identity.incarnation, "version": settings.version,
            **(await run_sync(transport._description))}


@router.post("/peer/exchange")
async def peer_exchange(body: ExchangeRequest, device_id: str = Depends(tokens.require_device)) -> dict:
    ctx = engine.require_context()
    transport.note_contact(device_id)

    def _work():
        if body.flush:
            engine.scan_all(strict=True)
        store.touch_member(device_id, capabilities=body.capabilities or None, endpoints=body.endpoints or None,
                           version=body.version, incarnation=body.incarnation)
        ops, heads, snapshot = store.exchange_batch(body.watermarks, limit=body.limit, lane=body.lane,
                                                   exclude_origin=device_id)
        return {"ops": ops, "watermarks": heads, "generation": ctx.generation,
                "key_version": ctx.key_version, "incarnation": ctx.identity.incarnation,
                "endpoints": engine._own_endpoints(), "capabilities": transport._capabilities(),
                "version": settings.version, "snapshot_required": snapshot,
                "peer_state": 1, "flushed": body.flush}

    return await run_sync(_work)


@router.post("/peer/ops")
async def peer_ops(body: OpsRequest, device_id: str = Depends(tokens.require_device)) -> dict:
    transport.note_contact(device_id)
    result = await run_sync(engine.apply_batch, body.ops)
    if result.applied:
        transport.forward_pushed(device_id)
    return result.as_dict()


@router.get("/peer/ops/{origin}/{incarnation}/{seq}")
async def peer_op(origin: str, incarnation: int, seq: int, at_or_after: bool = False,
                  device_id: str = Depends(tokens.require_device)) -> dict:
    """One signed envelope, optionally the first retained op at or after seq.

    Rollback detection needs an op past the caller's watermark, not necessarily
    the advertised head: pruning can remove that head after another origin
    replaces its record. A bare watermark is never rollback proof.
    """
    transport.note_contact(device_id)
    if len(origin) > 64 or incarnation < 1 or seq < 1:
        raise HTTPException(400, "Invalid stream identity")

    def _work():
        with store.get_db() as conn:
            return store.get_op(conn.cursor(), origin, incarnation, seq, at_or_after=at_or_after)

    op = await run_sync(_work)
    if op is None:
        raise HTTPException(404, "No such operation")
    return {"op": op}


@router.post("/peer/control/{action}")
async def peer_control(action: str, request: Request, device_id: str = Depends(tokens.require_device)) -> dict:
    """The owner side of the runner API: turns, sessions, interrupt, prompt answers, hydrate."""
    from app.network import control
    transport.note_contact(device_id)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be an object")
    if not control.known_action(action):
        raise HTTPException(404, "Unknown control action")
    try:
        return await control.handle(action, body, from_device=device_id)
    except control.UnknownControlAction:
        raise HTTPException(404, "Unknown control action") from None
    except KeyError as exc:
        # A missing field inside a handler is a bad request, not an unknown action.
        logger.exception("Control action %s from %s failed on a missing field", action, device_id)
        raise HTTPException(400, f"Missing field: {exc.args[0] if exc.args else exc}") from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sessions/{session_id}/fork-here", status_code=status.HTTP_201_CREATED)
async def fork_session_here(session_id: str, request: Request, _: str = Depends(require_auth)) -> dict:
    """Continue a session whose owner is offline: a new local session with the mirrored transcript."""
    from app.api.sessions_helpers import require_session
    from app.network import control
    await require_session(request, session_id)  # 404 unknown, 403 outside the API user's scope
    api_user = getattr(request.state, "api_user", None)
    try:
        result = await run_sync(control.fork_here, session_id, api_user_id=(api_user or {}).get("id") if api_user else None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    engine.kick()
    return result


@router.post("/runners/{device_id}/sessions/{session_id}/hydrate")
async def hydrate_session(device_id: str, session_id: str, request: Request, _: str = Depends(require_auth)) -> dict:
    """Ask the owner to publish an old transcript so this mirror can show it."""
    from app.api.sessions_helpers import require_session
    from app.network import control
    await require_session(request, session_id)
    try:
        return await control.hydrate(device_id, session_id)
    except control.RunnerUnreachable as exc:
        raise HTTPException(503, f"The owning worker is offline: {exc}") from exc
    except (control.ControlRefused, engine.NotEnrolled) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/peer/snapshot")
async def peer_snapshot(collection: Optional[str] = None, offset: int = 0,
                        device_id: str = Depends(tokens.require_device)) -> dict:
    transport.note_contact(device_id)
    if collection is None:
        return await run_sync(engine.snapshot_manifest)
    # Retired collections answer with an empty page for joiners on older builds.
    if collection not in policy.COLLECTIONS and collection not in policy.RETIRED_COLLECTIONS:
        raise HTTPException(404, "Unknown collection")
    return await run_sync(engine.snapshot_chunk, collection, max(0, offset))


@router.post("/startup-sync")
async def desktop_startup_sync(_: str = Depends(require_admin)) -> dict:
    from app.network import startup_sync
    return await startup_sync.synchronize()


@router.post("/peer/state")
async def peer_state(device_id: str = Depends(tokens.require_device)) -> dict:
    from app.network import peer_snapshot
    try:
        return await run_sync(peer_snapshot.capture, device_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/peer/state/{transfer}/{index}")
async def peer_state_page(transfer: str, index: int, device_id: str = Depends(tokens.require_device)):
    from app.network import peer_snapshot
    try:
        raw = await run_sync(peer_snapshot.page, device_id, transfer, index)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, "Peer transfer expired; start a new transfer") from exc
    return Response(raw, media_type="application/json")


@router.get("/peer/blob/{hash_value}/parts/{index}")
async def peer_file_part(hash_value: str, index: int, device_id: str = Depends(tokens.require_device)) -> dict:
    from app.network import peer_files
    if len(hash_value) != 64 or any(c not in "0123456789abcdef" for c in hash_value):
        raise HTTPException(400, "Invalid hash")
    try:
        return await run_sync(peer_files.part, hash_value, index, device_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "File not held here") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
