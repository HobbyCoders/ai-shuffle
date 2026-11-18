"""Versioned unified-generation actions over authenticated worker controls.

Version 2 adds reported-cost overrun accounting to catalog/prepare/reserve/execute.
Older peers must be updated; requests never fall back to an old lane.
Credentials remain on the holding worker. Paid submissions are never replayed.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from uuid import uuid4

from app.core.generation_resolver import Candidate, RunContext, price_total, same_price
from app.db.async_database import run_sync
from app.network import control, engine, runners

from app.core.runner_capabilities import GENERATION_VERSION

WIRE_VERSION = GENERATION_VERSION
ACTION = "generation"
# Discovery must not hold a tool call on a slow peer; submissions keep theirs.
CATALOG_TIMEOUT = 15


def require_version(peer):
    version = (peer.get("capabilities") or {}).get("generation", 0)
    if type(version) is not int or version < WIRE_VERSION:
        raise ValueError("Update that worker before using unified generation; it uses an older generation protocol.")


async def request(worker, operation, **body):
    peer = await run_sync(runners.find, worker)
    if not peer or not peer.get("online"):
        raise ValueError("The account-holding worker is unavailable. No other worker was used.")
    require_version(peer)
    try:
        timeout = {"execute": 300, "catalog": CATALOG_TIMEOUT}.get(operation, 60)
        response = await control.call(worker, ACTION, {"version": WIRE_VERSION, "operation": operation, **body}, timeout=timeout)
    except control.RunnerUnreachable:
        raise ValueError("The account-holding worker did not confirm the outcome. Do not automatically retry a generation submission.") from None
    except control.ControlRefused:
        raise ValueError("The account-holding worker refused unified generation. Check its account or update that worker; no fallback was used.") from None
    if type(response.get("version")) is not int or response["version"] != WIRE_VERSION:
        raise ValueError("Update that worker before using unified generation; its reply uses an older generation protocol.")
    return response


async def catalog(kind, *, source=None, model=None, query=None, limit=15, prompt=None, worker=None, operation=None,
                  listing_prices=True):
    """Ask online peers (or only ``worker``) in parallel, each with a short timeout.

    ``operation`` travels as ``media_operation``; an older peer ignores it and
    lists rows for a text call, so its reply is dropped for any other
    operation unless a model was named (any category matches a named model).
    """
    from app.core import generation_operations
    text_only = generation_operations.resolve(kind, operation).native
    # Sent only when off; a peer that ignores it just lists cached prices.
    extra = {} if listing_prices else {"listing_prices": False}

    async def one(peer):
        try:
            response = await request(peer["device_id"], "catalog", kind=kind, source=source, model=model, query=query,
                                     limit=limit, prompt=prompt, media_operation=operation, **extra)
        except ValueError as exc:
            # Surface old/unreachable workers rather than silently treating their
            # accounts as absent. No account identities are advertised in presence.
            return [Candidate(source or "unavailable", source or peer["device_id"], peer["device_id"],
                              model or "unavailable", kind, False, available=False, reason=str(exc))]
        found = []
        try:
            for item in response.get("candidates", []):
                candidate = Candidate.from_wire(item)
                if candidate.worker != peer["device_id"] or candidate.kind != kind:
                    raise ValueError("Invalid generation catalog from the account-holding worker.")
                found.append(candidate)
        except (ValueError, TypeError, AttributeError):
            # One malformed reply makes that worker unavailable, not the search.
            return [Candidate(source or "unavailable", source or peer["device_id"], peer["device_id"],
                              model or "unavailable", kind, False, available=False,
                              reason="Invalid generation catalog from that worker; nothing from it was used.")]
        if not text_only and not model and response.get("media_operation") != operation:
            # An older peer ignored the operation: its rows are for a text call.
            return []
        return found

    peers = [p for p in await run_sync(runners.other_workers)
             if p.get("online") and (worker is None or p.get("device_id") == worker)]
    return [c for found in await asyncio.gather(*(one(p) for p in peers)) for c in found]


async def fal_pages(kind, *, operation, query=None, offset=0, limit=20, worker=None):
    """Ask online peers (or only ``worker``) in parallel for one page of fal models.

    Returns ``(served, stale)``. ``served`` holds ``(candidates, total,
    details, worker)`` from each peer that serves fal for ``kind``; its total
    may be 0 (nothing matches there). A peer without fal is left out, so it
    never hides one that has it. ``stale`` names peers whose older code lists
    fal but cannot page or filter by operation: they need an update.
    """
    peers = [p for p in await run_sync(runners.other_workers)
             if p.get("online") and (worker is None or p.get("device_id") == worker)]

    async def one(peer):
        device = peer["device_id"]
        try:
            response = await request(device, "catalog", kind=kind, source="fal", query=query,
                                     limit=max(1, min(limit, 50)), media_operation=operation,
                                     page={"offset": offset, "limit": limit})
            found = [Candidate.from_wire(item) for item in response.get("candidates", [])]
        except (ValueError, TypeError, AttributeError):
            return None
        page = response.get("page")
        if not isinstance(page, dict):
            # Older code answered with its plain catalog: one unfiltered first page.
            return "stale" if any(c.source == "fal" for c in found) else None
        if response.get("media_operation") != operation or type(page.get("total")) is not int:
            return "stale"
        # A peer before ``serves`` sent total 0 when it had no fal.
        if page.get("serves") is False or (page.get("serves") is not True and page["total"] == 0):
            return None
        if any(c.source != "fal" or c.kind != kind or c.worker != device for c in found):
            return None
        details = page.get("details") if isinstance(page.get("details"), dict) else {}
        return found, page["total"], details, device

    served, stale = [], []
    for peer, answer in zip(peers, await asyncio.gather(*(one(p) for p in peers))):
        if answer == "stale":
            stale.append(peer.get("device_name") or peer["device_id"])
        elif answer is not None:
            served.append(answer)
    return served, stale


async def catalog_page(kind, *, operation, query=None, offset=0, limit=20, worker=None):
    """One page of fal models from one peer that serves fal for ``kind``.

    ``worker`` pins the page to the worker that served the first one, so
    "Show more" never mixes two catalogs. Unpinned, the first peer (by device
    ID) with matches answers, else one with an empty page. Returns
    ``(candidates, total, details, worker, notice)``; ``total`` None means no
    peer serves fal, and ``notice`` says which workers need an update.
    """
    served, stale = await fal_pages(kind, operation=operation, query=query, offset=offset, limit=limit, worker=worker)
    notice = (f"Update {', '.join(stale)} to browse {'its' if len(stale) == 1 else 'their'} fal.ai models here."
              if stale else None)
    if not served:
        return [], None, {}, None, notice
    found, total, details, device = min(served, key=lambda s: (s[1] == 0, s[3]))
    return found, total, details, device, notice


async def prepare(candidate, options, prompt, context):
    response = await request(candidate.worker, "prepare", kind=candidate.kind, source=candidate.account,
                             model=candidate.model, options=options, prompt=prompt, session_id=context.session_id, chat_mode=context.chat_mode)
    prepared = Candidate.from_wire(response.get("candidate"))
    if any(getattr(prepared, key) != getattr(candidate, key) for key in ("worker", "account", "source", "kind", "model")):
        raise ValueError("The account-holding worker changed the selected source.")
    return prepared


async def reserve(amount):
    ctx = await run_sync(engine.load_context)
    if ctx is not None and ctx.founder_device_id != ctx.device_id:
        response = await request(ctx.founder_device_id, "reserve", amount=str(amount))
        return response.get("reserved") is True
    from app.core import generation_budget
    return await run_sync(generation_budget.reserve, amount)


async def record_overrun(amount):
    ctx = await run_sync(engine.load_context)
    if ctx is not None and ctx.founder_device_id != ctx.device_id:
        response = await request(ctx.founder_device_id, "record-overrun", amount=str(amount))
        if response.get("recorded") is not True:
            raise ValueError("The network founder did not confirm reported generation cost accounting.")
        return
    from app.core import generation_budget
    await run_sync(generation_budget.record_overrun, amount)


async def execute(candidate, prompt, context):
    response = await request(candidate.worker, "execute", kind=candidate.kind, source=candidate.account,
                             model=candidate.model, options=candidate.options, prompt=prompt,
                             price=candidate.price, session_id=context.session_id, chat_mode=context.chat_mode, command_id=str(uuid4()))
    return response["result"]


def claim(command_id):
    # Durable at-most-once claim before provider submission. A crash or dropped
    # response leaves it claimed, not eligible to submit the paid request again.
    from app.db.base import get_db
    from uuid import UUID
    try:
        command_id = str(UUID(command_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid generation command id.") from None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO system_settings (key,value) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING key",
                       ("network_generation_request:" + command_id, "submitted"))
        return cursor.fetchone() is not None


async def handle(body, from_device):
    from app.core.config import settings
    from app.mcp.node_worker import NodeWorker
    from app.services.generation_sources import Sources
    from app.core.generation_resolver import KINDS

    if type(body.get("version")) is not int or body["version"] != WIRE_VERSION:
        raise ValueError("Update that worker before using unified generation; generation protocol version 2 is required.")
    operation = body.get("operation")
    if operation in ("reserve", "record-overrun"):
        ctx = await run_sync(engine.load_context)
        if ctx and ctx.founder_device_id != ctx.device_id:
            raise ValueError("Generation budget requests must go to the network founder.")
        if operation == "record-overrun":
            await record_overrun(body.get("amount"))
            return {"version": WIRE_VERSION, "recorded": True}
        return {"version": WIRE_VERSION, "reserved": await reserve(body.get("amount"))}
    kind = body.get("kind")
    if kind not in KINDS:
        raise ValueError("Unknown generation kind.")
    context = RunContext(session_id=body.get("session_id"), owner=True, chat_mode=body.get("chat_mode") is True)
    worker = NodeWorker(str(settings.effective_tools_dir / "dist" / "worker.js"),
                        cwd=str(settings.effective_workspace_dir))
    backend = Sources(worker, context, local_only=True, serve_peer=True)
    limit = body.get("limit", 15)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Generation catalog limit must be between 1 and 50.")
    media_operation = body.get("media_operation")
    from app.core import generation_operations
    if media_operation is not None and (not isinstance(media_operation, str)
                                        or media_operation not in generation_operations.OPERATIONS):
        raise ValueError("Unknown generation operation.")
    page = body.get("page")
    if operation == "catalog" and isinstance(page, dict):
        offset, size = page.get("offset", 0), page.get("limit", 20)
        if type(offset) is not int or type(size) is not int or offset < 0 or not 1 <= size <= 50:
            raise ValueError("Invalid generation catalog page.")
        served = await backend.fal_page(kind, operation=media_operation, query=body.get("query"),
                                        offset=offset, limit=size)
        found, total, details = served or ([], 0, {})
        # ``serves`` tells a worker without fal for this kind from an empty page.
        return {"version": WIRE_VERSION, "candidates": [asdict(c) for c in found], "media_operation": media_operation,
                "page": {"total": total, "offset": offset, "details": details, "serves": served is not None}}
    # prepare/execute price the one candidate fresh, so they skip listing rates.
    listing_prices = operation == "catalog" and body.get("listing_prices") is not False
    candidates = await backend.local_catalog(kind, source=body.get("source"), model=body.get("model"), query=body.get("query"),
                                             limit=limit, prompt=body.get("prompt"), operation=media_operation,
                                             listing_prices=listing_prices)
    if kind == "image":
        # The holding worker applies its own ChatGPT-plan image rule (#1796):
        # it never spends on a fal image its own generateImage would refuse.
        from app.core.provider import subscription_images as sub
        from app.core.generation_resolver import normalize_source
        if await run_sync(sub.plan_images_configured):
            if operation != "catalog" and normalize_source(body.get("source")) == "fal":
                raise ValueError(sub.fal_image_refusal())
            candidates = [c for c in candidates if c.source != "fal"]
    if operation == "catalog":
        return {"version": WIRE_VERSION, "candidates": [asdict(c) for c in candidates], "media_operation": media_operation}
    if operation not in ("prepare", "execute") or len(candidates) != 1:
        raise ValueError("No unique matching generation source/account and model on this worker.")
    candidate = candidates[0]
    if not candidate.available or candidate.source in ("chatgpt", "supergrok") and not candidate.verified:
        raise ValueError(candidate.reason or "Subscription combination is not verified.")
    options, prompt = body.get("options"), body.get("prompt")
    if not isinstance(options, dict) or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise ValueError("Invalid generation prompt or options.")
    candidate = await backend.prepare(candidate, options, prompt)
    if operation == "prepare":
        return {"version": WIRE_VERSION, "candidate": asdict(candidate)}
    if price_total(candidate.price) is None or not same_price(candidate.price, body.get("price")):
        raise ValueError("The generation price changed or is unavailable. Request a new quote before submitting.")
    if not await run_sync(claim, body.get("command_id")):
        raise ValueError("This generation command was already submitted; its outcome may be unknown. No retry was made.")
    if not await reserve(price_total(candidate.price)):
        return {"version": WIRE_VERSION, "result": {"success": False, "error": "The daily spending cap would be exceeded."}}
    if candidate.source == "fal":
        key = await backend.fal_key()
        if not key:
            raise ValueError("fal.ai is disabled or its account is unavailable.")
        worker = NodeWorker(str(settings.effective_tools_dir / "dist" / "worker.js"),
                            env={"FAL_KEY": key, "FAL_API_KEY": key}, cwd=str(settings.effective_workspace_dir))
        backend.node = worker
    try:
        output = await backend.execute_local(candidate, prompt, context)
    finally:
        await worker.shutdown()
    return {"version": WIRE_VERSION, "result": output}
