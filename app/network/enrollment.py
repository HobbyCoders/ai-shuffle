"""Enrollment by pairing: the ``network:join`` grant, both sides.

Inviter (an existing member)
    1. creates a pairing grant with purpose ``network:join`` (Devices page);
       the QR / link carries its endpoint, the code and its fingerprint
    2. ``receive_join`` consumes the code the joiner posts, records a pending
       join with the joiner's public keys and fingerprint
    3. an administrator compares the fingerprint on both screens and
       approves; ``approve_join`` issues the certificate and wraps the keys
    4. the joiner polls with its poll secret and picks up the response

Joiner (a fresh worker)
    ``start_enrollment`` posts its public keys to the inviter, verifies the
    inviter's fingerprint against the one the user saw, then waits for
    approval, installs the response, copies a snapshot and starts tailing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.core import pairing as pairing_core
from app.core.config import settings
from app.db import network as store
from app.db import pairing as pairing_db
from app.network import crypto, engine, identity as identity_mod, membership

logger = logging.getLogger(__name__)

JOIN_PURPOSE = "network:join"
JOIN_TTL_MINUTES = 30
POLL_INTERVAL_SECONDS = 3.0
_task: Optional[asyncio.Task] = None


class EnrollmentError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Invitation links
# ---------------------------------------------------------------------------

def build_join_link(endpoint: Optional[str], code: str) -> Optional[dict]:
    """The QR payload: endpoint, code and this worker's fingerprint."""
    ctx = engine.context()
    if ctx is None or not endpoint:
        return None
    query = urlencode({"endpoint": endpoint, "code": pairing_core.format_pairing_code(code),
                       "fp": ctx.identity.fingerprint})
    return {
        "url": f"shuffle://network/join?{query}",
        "endpoint": endpoint,
        "code": pairing_core.format_pairing_code(code),
        "fingerprint": ctx.identity.fingerprint,
        "device_name": (ctx.member(ctx.device_id).name if ctx.member(ctx.device_id) else None),
    }


def parse_join_link(value: str) -> Dict[str, str]:
    """Accept the QR url, a pasted ``endpoint code fingerprint`` or a JSON-ish dict."""
    text = (value or "").strip()
    if text.startswith("shuffle://") or "network/join?" in text:
        query = urlsplit(text.replace("shuffle://", "https://", 1)).query
        parsed = {k: v[0] for k, v in parse_qs(query).items()}
        return {"endpoint": parsed.get("endpoint", ""), "code": parsed.get("code", ""),
                "fingerprint": parsed.get("fp", "")}
    raise EnrollmentError("Not a join link")


# ---------------------------------------------------------------------------
# Inviter side
# ---------------------------------------------------------------------------

def receive_join(*, code: str, device: Dict[str, Any], inviter_fingerprint: Optional[str]) -> dict:
    """Consume a join code and park the request for approval."""
    ctx = engine.context()
    if ctx is None:
        raise EnrollmentError("This worker has no network to join")
    if inviter_fingerprint and not crypto.fingerprint_matches(ctx.identity.signing_public, inviter_fingerprint):
        raise EnrollmentError("The fingerprint you verified does not belong to this worker")
    try:
        signing_public = crypto.b64u_decode(device["signing_public"])
        encryption_public = crypto.b64u_decode(device["encryption_public"])
        name = str(device.get("name") or "").strip()[:100]
        platform = str(device.get("platform") or "").strip()[:60] or None
        kind = device.get("kind") or membership.KIND_WORKER
        legacy = (str(device.get("legacy_instance_id") or "").strip() or None)
    except (KeyError, TypeError, crypto.CryptoError) as exc:
        raise EnrollmentError("Malformed device description") from exc
    if len(signing_public) != 32 or len(encryption_public) != 32 or not name or kind not in membership.KINDS:
        raise EnrollmentError("Malformed device description")
    device_id = membership.device_id_for(signing_public)
    if device_id == ctx.device_id:
        raise EnrollmentError("A worker cannot join itself")
    grant = pairing_db.consume_grant_for_network_join(token_hash=pairing_core.hash_pairing_code(code))
    if grant is None:
        raise EnrollmentError("Join code is invalid, expired, already used, or revoked.")
    poll_secret = secrets.token_urlsafe(24)
    join_id = "join_" + secrets.token_hex(10)
    store.create_join({
        "id": join_id, "role": "inviter", "status": "pending", "device_id": device_id, "name": name,
        "platform": platform, "signing_public": device["signing_public"],
        "encryption_public": device["encryption_public"], "fingerprint": crypto.fingerprint(signing_public),
        "grant_id": grant["id"], "poll_secret_hash": _hash(poll_secret), "legacy_instance_id": legacy,
        "expires_at": (_now() + timedelta(minutes=JOIN_TTL_MINUTES)).isoformat(),
    })
    existing = ctx.member(device_id)
    if existing is not None and existing.status == membership.STATUS_ACTIVE:
        # Already a member (re-enrollment): approval is still explicit.
        logger.info("Device %s asked to join again", device_id)
    from app.core.global_events import global_events
    try:
        global_events.resources_changed("network")
    except Exception:  # noqa: BLE001
        pass
    return {
        "join_id": join_id,
        "poll_secret": poll_secret,
        "status": "pending",
        "inviter": {"device_id": ctx.device_id, "fingerprint": ctx.identity.fingerprint,
                    "name": ctx.member(ctx.device_id).name if ctx.member(ctx.device_id) else None,
                    "network_name": ctx.name},
        "joiner_fingerprint": crypto.fingerprint(signing_public),
    }


def poll_join(join_id: str, poll_secret: str) -> dict:
    join = store.get_join(join_id)
    if join is None or join["role"] != "inviter" or not secrets.compare_digest(
            join.get("poll_secret_hash") or "", _hash(poll_secret or "")):
        raise EnrollmentError("Unknown join request")
    out = {"join_id": join_id, "status": join["status"]}
    if join["status"] == "approved":
        out["response"] = join.get("response")
        # One pickup: the response carries wrapped keys, so do not keep it around.
        store.update_join(join_id, status="complete", response=None)
    elif join["status"] in ("rejected", "expired"):
        out["error"] = join.get("error") or join["status"]
    return out


def approve_join(join_id: str, *, fingerprint_confirmed: bool) -> dict:
    if not fingerprint_confirmed:
        raise EnrollmentError("Confirm the joiner's fingerprint before approving")
    join = store.get_join(join_id)
    if join is None or join["role"] != "inviter":
        raise EnrollmentError("Unknown join request")
    if join["status"] != "pending":
        raise EnrollmentError("Join request is no longer pending")
    response = engine.admit_device(
        device_id=join["device_id"], signing_public=crypto.b64u_decode(join["signing_public"]),
        encryption_public=crypto.b64u_decode(join["encryption_public"]), name=join["name"],
        kind=membership.KIND_WORKER, platform=join.get("platform"), legacy_instance_id=join.get("legacy_instance_id"))
    updated = store.update_join(join_id, status="approved", response=response)
    from app.core.global_events import global_events
    try:
        global_events.resources_changed("network")
    except Exception:  # noqa: BLE001
        pass
    return {"join_id": join_id, "status": updated["status"], "device_id": join["device_id"]}


def reject_join(join_id: str, reason: str = "rejected") -> dict:
    join = store.get_join(join_id)
    if join is None or join["role"] != "inviter":
        raise EnrollmentError("Unknown join request")
    store.update_join(join_id, status="rejected", error=reason[:200])
    return {"join_id": join_id, "status": "rejected"}


def pending_joins() -> list:
    store.expire_joins()
    return [{k: j.get(k) for k in ("id", "status", "device_id", "name", "platform", "fingerprint",
                                    "legacy_instance_id", "created_at", "expires_at")}
            for j in store.list_joins(role="inviter") if j["status"] == "pending"]


# ---------------------------------------------------------------------------
# Joiner side
# ---------------------------------------------------------------------------

def enrollment_status() -> Optional[dict]:
    joins = store.list_joins(role="joiner")
    if not joins:
        return None
    join = joins[0]
    return {k: join.get(k) for k in ("id", "status", "endpoint_url", "inviter_fingerprint", "fingerprint",
                                     "error", "created_at", "updated_at")}


async def start_enrollment(*, endpoint_url: str, code: str, inviter_fingerprint: str,
                           device_name: Optional[str] = None, legacy_instance_id: Optional[str] = None) -> dict:
    """Post our keys to the inviter and start waiting for approval in the background."""
    global _task
    if engine.context() is not None:
        raise EnrollmentError("This worker already belongs to a network")
    endpoint = pairing_core.sanitize_endpoint_url(endpoint_url)
    if not endpoint:
        raise EnrollmentError("An endpoint is required")
    if not inviter_fingerprint or len("".join(ch for ch in inviter_fingerprint if ch.isalnum())) < 32:
        raise EnrollmentError("Verify the inviting worker's fingerprint first")
    device_identity = identity_mod.load()
    name = (device_name or "").strip() or engine._default_device_name()
    for old in store.list_joins(role="joiner"):
        store.delete_join(old["id"])
    payload = {
        "code": pairing_core.normalize_pairing_code(code),
        "inviter_fingerprint": inviter_fingerprint,
        "device": {
            "signing_public": crypto.b64u_encode(device_identity.signing_public),
            "encryption_public": crypto.b64u_encode(device_identity.encryption_public),
            "name": name, "platform": identity_mod.platform_name(), "kind": membership.KIND_WORKER,
            "legacy_instance_id": legacy_instance_id or settings.effective_instance_id,
        },
    }
    if not payload["code"]:
        raise EnrollmentError("Join code must have 12 characters")
    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0) as client:
        try:
            response = await client.post(endpoint + "/api/v1/network/join", json=payload)
        except httpx.HTTPError as exc:
            raise EnrollmentError(f"Could not reach {endpoint}: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        raise EnrollmentError(str(detail or f"Inviter answered HTTP {response.status_code}"))
    data = response.json()
    inviter = data.get("inviter") or {}
    if _normalize_fp(inviter.get("fingerprint")) != _normalize_fp(inviter_fingerprint):
        raise EnrollmentError("The inviter's fingerprint does not match the one you verified")
    join_id = data["join_id"]
    store.create_join({
        "id": join_id, "role": "joiner", "status": "pending", "device_id": device_identity.device_id, "name": name,
        "endpoint_url": endpoint, "inviter_fingerprint": _normalize_fp(inviter.get("fingerprint")),
        # The joiner keeps the raw poll secret (it needs it to resume after a restart).
        "fingerprint": device_identity.fingerprint, "poll_secret_hash": data["poll_secret"],
        "legacy_instance_id": legacy_instance_id or settings.effective_instance_id,
        "expires_at": (_now() + timedelta(minutes=JOIN_TTL_MINUTES)).isoformat(),
    })
    if _task is None or _task.done():
        _task = asyncio.create_task(_wait_for_approval(join_id, endpoint, data["poll_secret"], name,
                                                       legacy_instance_id or settings.effective_instance_id))
    return {"join_id": join_id, "status": "pending", "inviter": inviter,
            "your_fingerprint": device_identity.fingerprint}


def _normalize_fp(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "").lower() if ch in "0123456789abcdef")


async def _wait_for_approval(join_id: str, endpoint: str, poll_secret: str, device_name: str,
                             legacy_instance_id: Optional[str]) -> None:
    deadline = asyncio.get_running_loop().time() + JOIN_TTL_MINUTES * 60
    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                response = await client.get(endpoint + f"/api/v1/network/join/{join_id}", params={"secret": poll_secret})
                if response.status_code >= 400:
                    store.update_join(join_id, error=f"HTTP {response.status_code}")
                    continue
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                store.update_join(join_id, error=f"{type(exc).__name__}")
                continue
            status = data.get("status")
            if status == "pending":
                continue
            if status == "approved" and data.get("response"):
                try:
                    await asyncio.to_thread(complete_enrollment, data["response"], client=None,
                                            endpoint=endpoint, device_name=device_name,
                                            legacy_instance_id=legacy_instance_id)
                    store.update_join(join_id, status="complete", error=None)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Enrollment failed")
                    store.update_join(join_id, status="failed", error=str(exc)[:200])
                return
            store.update_join(join_id, status=status or "failed", error=data.get("error"))
            return
    store.update_join(join_id, status="expired", error="Approval timed out")


def complete_enrollment(response: Dict[str, Any], *, client, endpoint: str, device_name: str,
                        legacy_instance_id: Optional[str]) -> None:
    """Install the response, copy a snapshot over the peer API, then go Ready."""
    ctx = engine.enroll_from_response(response, device_name=device_name, legacy_instance_id=legacy_instance_id)
    issuer = response.get("issuer") or {}
    issuer_id = issuer.get("device_id")
    endpoints = [endpoint] + [e for e in (issuer.get("endpoints") or []) if e != endpoint]
    if issuer_id:
        store.touch_member(issuer_id, endpoints=endpoints)
    _copy_snapshot_sync(ctx, issuer_id, endpoints)
    engine.finish_bootstrap()
    engine.publish_presence(version=settings.version)
    engine.kick()


def _copy_snapshot_sync(ctx: engine.NetworkContext, issuer_id: Optional[str], endpoints: list) -> None:
    """Synchronous snapshot copy (runs in a worker thread)."""
    from app.network import tokens
    if not issuer_id:
        raise EnrollmentError("Join response names no issuer to copy from")
    last_error = "unreachable"
    with httpx.Client(follow_redirects=False, timeout=60.0) as client:
        for base in endpoints:
            base = base.rstrip("/")
            try:
                challenge = client.post(base + "/api/v1/network/peer/auth/challenge",
                                        json={"device_id": ctx.device_id}).json()["challenge"]
                signature = tokens.sign_challenge(ctx.identity.signing_private, challenge, issuer_id)
                token_response = client.post(base + "/api/v1/network/peer/auth/token", json={
                    "device_id": ctx.device_id, "challenge": challenge, "signature": signature,
                    "incarnation": ctx.identity.incarnation, "endpoints": engine._own_endpoints(),
                    "version": settings.version, "platform": identity_mod.platform_name()})
                if token_response.status_code >= 400:
                    raise EnrollmentError(f"token HTTP {token_response.status_code}")
                headers = {"Authorization": "Bearer " + token_response.json()["token"]}
                manifest = client.get(base + "/api/v1/network/peer/snapshot", headers=headers).json()
                for collection in manifest["collections"]:
                    offset = 0
                    while True:
                        chunk = client.get(base + "/api/v1/network/peer/snapshot", headers=headers,
                                           params={"collection": collection, "offset": offset}).json()
                        engine.apply_snapshot_chunk(chunk, ctx=ctx)
                        if not chunk.get("more"):
                            break
                        offset += int(chunk.get("count") or 0)
                engine.adopt_watermarks(manifest.get("watermarks") or {}, ctx=ctx)
                return
            except (httpx.HTTPError, KeyError, ValueError, EnrollmentError, engine.ApplyError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:200]
                continue
    raise EnrollmentError("Snapshot copy failed: " + last_error)


def resume_enrollment() -> bool:
    """After a restart, keep waiting for an approval that was still pending."""
    global _task
    if engine.context() is not None:
        return False
    pending = [j for j in store.list_joins(role="joiner") if j["status"] == "pending"]
    if not pending or (_task is not None and not _task.done()):
        return False
    join = pending[0]
    _task = asyncio.create_task(_wait_for_approval(join["id"], join["endpoint_url"], join["poll_secret_hash"],
                                                   join["name"], join.get("legacy_instance_id")))
    return True


def cancel_enrollment() -> bool:
    global _task
    removed = False
    for join in store.list_joins(role="joiner"):
        store.delete_join(join["id"])
        removed = True
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    return removed
