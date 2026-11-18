"""Remembered generation choices and live quotes for the in-chat choice card.

The choices are account preferences (``app.core.generation_choices``). Quotes
run the same declared-option validation and pricing the generation tools use;
nothing is reserved or submitted here.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import require_admin, require_auth
from app.api.preferences import require_preference_write
from app.core import generation_choices
from app.core.preference_identity import request_preference_identity
from app.db.async_database import run_sync
from app.services import fal_pricing

router = APIRouter(prefix="/api/v1/generation", tags=["Generation choices"])

_QUOTE_PROMPT = "A preview of this choice."
# Remembering waits for a fresh quote: fal's bounded fresh read (its 429
# retries included) plus the model lookup and schema read around it. Past it
# the pick is kept for this time only and says so; no cached rate is used.
_SAVE_QUOTE_TIMEOUT_SECONDS = fal_pricing.FRESH_READ_SECONDS + 10


class ChoiceBody(BaseModel):
    source: Optional[str] = Field(None, max_length=512)
    account: Optional[str] = Field(None, max_length=512)
    worker: Optional[str] = Field(None, max_length=512)
    model: str = Field(..., min_length=1, max_length=512)
    options: dict[str, Any] = Field(default_factory=dict)


class SaveChoiceRequest(BaseModel):
    choice: ChoiceBody
    remember: bool = True
    origin: str = Field("card", pattern="^(card|settings)$")


class QuoteRequest(BaseModel):
    kind: str
    choice: ChoiceBody
    prompt: Optional[str] = Field(None, max_length=32000)


def _kind(kind: str) -> str:
    if kind not in generation_choices.KINDS:
        raise HTTPException(404, "Unknown generation kind.")
    return kind


def _identity(request: Request) -> tuple[str, str]:
    try:
        return request_preference_identity(request)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


def _context(request: Request):
    from app.core.generation_resolver import RunContext

    api_user = getattr(request.state, "api_user", None)
    return RunContext(unattended=False, owner=api_user is None,
                      api_user_id=api_user["id"] if api_user else None)


def _here() -> str:
    from app.network import runners
    return runners.self_device_id() or "local"


@router.get("/choices")
async def list_choices(request: Request, token: str = Depends(require_auth)) -> dict:
    """Remembered and last picks per kind, after the one-time Settings migration.

    The migration writes and broadcasts preferences, so an agent-preview cookie
    only reads (the same rule as ``require_preference_write``).
    """
    from app.core import preview_access

    read_only = preview_access.grant(request) is not None

    def load():
        return {**generation_choices.load(_identity(request), run_migration=not read_only), "worker": _here(),
                "kinds": list(generation_choices.KINDS)}
    return await run_sync(load)


@router.put("/choices/{kind}")
async def save_choice(kind: str, body: SaveChoiceRequest, request: Request,
                      token: str = Depends(require_preference_write)) -> dict:
    """Record a pick as the last choice and, with ``remember``, reuse it next time.

    A pick is remembered only after a successful server-side quote priced it;
    otherwise it is kept as the last choice (this pick only) and says why.
    """
    _kind(kind)
    quoted = await _save_time_quote(kind, body.choice, request, token)
    remember = body.remember and quoted["priced"]
    try:
        choice = await run_sync(lambda: generation_choices.save(
            _identity(request), kind, body.choice.model_dump(exclude_none=True),
            remember=remember, origin=body.origin))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if body.remember and not remember:
        why = (quoted["quote_error"] or "This choice is unavailable right now.") \
            if quoted["unattended_skip_reason"] == "stored_choice_unavailable" else "This choice has no verified price."
        quoted["warning"] = f"{why} It was not remembered and is used for this pick only."
    return {"kind": kind, "choice": choice, "remembered": remember, **quoted}


async def _save_time_quote(kind: str, choice: ChoiceBody, request: Request, token: str) -> dict:
    return await save_time_quote(kind, choice.model_dump(exclude_none=True), _context(request))


async def save_time_quote(kind: str, choice: dict, context) -> dict:
    """A fresh server-side quote for a pick that was just saved (also used by
    the agent's ``generationPreferences`` before it remembers a pick).

    Never a client-supplied price. Failure to quote does not lose the pick or
    imply it is free; it reports what unattended runs will do with it.
    """
    from app.core.generation_resolver import UNKNOWN_PRICE
    try:
        quoted = await asyncio.wait_for(quote_choice(kind, choice, context), timeout=_SAVE_QUOTE_TIMEOUT_SECONDS)
    except TimeoutError:
        quoted = {"ok": False, "error": "The saved choice's quote timed out. Its worker or provider may be unavailable."}
    except Exception:  # noqa: BLE001 - optional provider quote must not lose a saved pick
        quoted = {"ok": False, "error": "A fresh quote for this saved choice is unavailable right now."}
    eligible = bool(quoted.get("ok") and quoted.get("priced"))
    quote_error = quoted.get("error")
    if not quoted.get("ok"):
        skip_reason = "stored_choice_unavailable"
        warning = (quote_error or "This saved choice is unavailable right now.") + " Unattended runs will skip it."
    else:
        skip_reason = "stored_choice_unpriced"
        warning = "This saved choice has no verified price. Unattended runs will skip it until a price is available."
    return {"price": quoted.get("price") or dict(UNKNOWN_PRICE), "priced": eligible, "quote_error": quote_error,
            "unattended_skip_reason": None if eligible else skip_reason,
            "warning": None if eligible else warning}


@router.delete("/choices/{kind}")
async def forget_choice(kind: str, request: Request, token: str = Depends(require_preference_write)) -> dict:
    """Ask again next time for this kind."""
    _kind(kind)
    forgotten = await run_sync(lambda: generation_choices.forget(_identity(request), kind))
    return {"kind": kind, "forgotten": forgotten}


@router.get("/options/{kind}")
async def list_options(kind: str, request: Request, query: Optional[str] = None, limit: int = 6,
                       token: str = Depends(require_auth)) -> dict:
    """Priced options for a kind across accounts (the Settings picker)."""
    from app.core.generation_resolver import Resolver
    from app.services.generation_sources import Sources

    _kind(kind)
    limit = max(1, min(limit, 12))
    catalog = await Resolver(Sources(None, _context(request))).catalog(kind, query=query or None, limit=limit)
    return {"kind": kind, "options": catalog.get("options", [])[: limit * 2]}


def _operation(kind: str, operation: Optional[str]):
    from app.core import generation_operations
    try:
        return generation_operations.resolve(_kind(kind), operation)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _status(entry: dict) -> str:
    from app.core.generation_resolver import price_total
    return ("unavailable" if not entry["available"] else
            "unpriced" if price_total(entry["price"]) is None else "available")


async def _fal_provider(backend, kind: str, op) -> dict:
    """fal's provider row from credential, operation and catalog presence on
    each worker; never from one model's schema or price."""
    counts: dict[str, int] = {}
    if await backend.fal_serves(kind):
        counts[backend.local_worker()] = len(await backend.fal_models(kind, operation=op.id))
    stale: list[str] = []
    if backend.context.owner and not backend.local_only:
        from app.network.generation import fal_pages
        served, stale = await fal_pages(kind, operation=op.id, limit=1)
        for _, total, _, device in served:
            counts.setdefault(device, total)
    workers = [worker for worker, count in counts.items() if count]
    row = {"source": "fal", "account": "fal-ai", "workers": workers, "available": bool(workers),
           "native": False, "reason": None}
    if workers:
        row["model_count"] = max(counts.values())
    elif counts:
        row["reason"] = f"fal.ai has no {op.label.lower()} models right now."
    elif stale:
        row["reason"] = f"Update {', '.join(stale)} to use {'its' if len(stale) == 1 else 'their'} fal.ai models here."
    else:
        row["reason"] = "No fal.ai API key on any worker. Add one in Settings, Providers."
    if kind == "image":
        from app.core.provider import subscription_images as sub
        if await run_sync(sub.plan_images_configured):
            # generateImage refuses fal while images use the ChatGPT plan (#1796).
            row.update(available=False, reason="Image generation is set to the ChatGPT plan, so fal.ai is not used for images.")
            row.pop("model_count", None)
    return row


@router.get("/providers/{kind}")
async def list_providers(kind: str, request: Request, operation: Optional[str] = None,
                         token: str = Depends(require_auth)) -> dict:
    """The providers/accounts that can perform ``operation`` (default: the
    kind's text operation), merged across workers, unavailable ones with
    their reason. Pick one, then page its models with ``/models/{kind}``."""
    import asyncio
    from app.services.generation_sources import Sources

    op = _operation(kind, operation)
    backend = Sources(None, _context(request))
    entries, fal = await asyncio.gather(backend.catalog(kind, operation=op.id, limit=1, fal=False),
                                        _fal_provider(backend, kind, op))
    providers: dict[tuple, dict] = {("fal",): fal}
    for entry in entries:
        if entry.source == "fal":
            continue  # fal's row comes from _fal_provider
        key = (entry.source, entry.account)
        row = providers.setdefault(key, {
            "source": entry.source, "account": entry.account, "workers": [], "available": False, "reason": None,
            "native": True})
        if entry.worker not in row["workers"]:
            row["workers"].append(entry.worker)
        if entry.available:
            row["available"], row["reason"] = True, None
            row["model"] = entry.model
        elif not row["available"] and not row["reason"]:
            row["reason"] = entry.reason
    return {"kind": kind, "operation": op.id, "operation_label": op.label,
            "providers": sorted(providers.values(), key=lambda p: (not p["available"], p["source"] != "fal"))}


@router.get("/models/{kind}")
async def list_models(kind: str, request: Request, source: str = "fal", operation: Optional[str] = None,
                      query: Optional[str] = None, offset: int = 0, limit: int = 20, worker: Optional[str] = None,
                      token: str = Depends(require_auth)) -> dict:
    """One page of one provider's models for ``operation``, searched server-side.

    fal rows carry fal's rate and, once a model's schema is cached, a price;
    ``POST /quote`` prices the picked options. Nothing is submitted. fal pages
    come from one worker, named in ``worker``: pass it back for the next page.
    ``notice`` says which workers need an update before they can be browsed.
    """
    from app.core.generation_resolver import normalize_source
    from app.services.generation_sources import Sources

    op = _operation(kind, operation)
    offset, limit = max(0, offset), max(1, min(limit, 50))
    query = (query or "").strip()[:200] or None
    backend = Sources(None, _context(request))
    source = normalize_source(source) or "fal"
    details: dict = {}
    served_by, notice = None, None
    if source == "fal":
        here = backend.local_worker()
        local = (await backend.fal_page(kind, operation=op.id, query=query, offset=offset, limit=limit)
                 if worker in (None, here) else None)
        if local is not None:
            (entries, total, details), served_by = local, here
        elif backend.context.owner and worker != here:
            from app.network.generation import catalog_page
            entries, total, details, served_by, notice = await catalog_page(
                kind, operation=op.id, query=query, offset=offset, limit=limit, worker=worker)
            total = total or 0
        else:
            entries, total = [], 0
    else:
        found = await backend.catalog(kind, source=source, operation=op.id, limit=1)
        entries = [e for e in found if e.source != "fal" and (source in (e.source, e.account))]
        if query:
            entries = [e for e in entries if query.casefold() in f"{e.source} {e.account} {e.model}".casefold()]
        total = len(entries)
        entries = entries[offset:offset + limit]
    models = []
    for entry in entries:
        view = entry.view()
        models.append({**view, **details.get(entry.model, {}), "status": _status(view)})
    return {"kind": kind, "operation": op.id, "source": source, "query": query, "models": models,
            "total": total, "offset": offset, "limit": limit, "has_more": offset + len(entries) < total,
            "worker": served_by, "notice": notice}


@router.get("/catalog")
async def catalog(request: Request, kind: Optional[str] = None, source: Optional[str] = None,
                  model: Optional[str] = None, query: Optional[str] = None, limit: int = 15,
                  operation: Optional[str] = None, worker: Optional[str] = None,
                  token: str = Depends(require_auth)) -> dict:
    """All connected accounts/holding workers, including unavailable routes.

    Limit is per kind/source, capped at 50; use query/model to narrow the fal
    catalog, and ``worker`` to ask only the worker that holds a browsed row.
    Prices are live estimates for default options, not admission.
    No generation is submitted. Favourites never imply consent to generate.
    """
    import asyncio
    from app.core.generation_resolver import Resolver, price_total
    from app.services.generation_sources import Sources

    kinds = [_kind(kind)] if kind else list(generation_choices.KINDS)
    if operation is not None and not kind:
        raise HTTPException(422, "operation needs a kind.")
    op = _operation(kinds[0], operation).id if operation else None
    limit = max(1, min(limit, 50))
    resolver = Resolver(Sources(None, _context(request)))
    catalogs = await asyncio.gather(*(
        resolver.catalog(k, source=source, model=model, query=query, limit=limit, operation=op, worker=worker or None)
        for k in kinds
    ))
    entries = []
    for result in catalogs:
        for entry in result.get("options", []):
            status = (
                "unavailable" if not entry["available"] else
                "unpriced" if price_total(entry["price"]) is None else "available"
            )
            entries.append({**entry, "status": status})
    saved = await run_sync(lambda: generation_choices.favourites(_identity(request)))
    return {"entries": entries, "favourites": saved, "kinds": kinds, "limit": limit}


@router.put("/favourites/{kind}")
async def save_favourite(kind: str, body: ChoiceBody, request: Request,
                         token: str = Depends(require_preference_write)) -> dict:
    _kind(kind)
    try:
        choice = await run_sync(lambda: generation_choices.set_favourite(
            _identity(request), kind, body.model_dump(exclude_none=True)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"kind": kind, "favourite": choice}


@router.delete("/favourites/{kind}")
async def delete_favourite(kind: str, request: Request,
                           token: str = Depends(require_preference_write)) -> dict:
    _kind(kind)
    await run_sync(lambda: generation_choices.set_favourite(_identity(request), kind, None))
    return {"kind": kind, "favourite": None}


async def _job(job_id: str) -> str:
    # Automations are owner-only (the routes require admin); preference values
    # remain scoped to the authenticated caller's identity, never a supplied one.
    try:
        return await run_sync(generation_choices.validate_job_id, job_id)
    except ValueError:
        raise HTTPException(404, "Unknown job.") from None


@router.get("/jobs/{job_id}/choices")
async def job_choices(job_id: str, request: Request, token: str = Depends(require_admin)) -> dict:
    job_id = await _job(job_id)
    try:
        choices = await run_sync(lambda: generation_choices.load_job(_identity(request), job_id))
    except ValueError:
        raise HTTPException(503, "The job's generation choices could not be read.") from None
    return {"job_id": job_id, "choices": choices}


@router.put("/jobs/{job_id}/choices/{kind}", dependencies=[Depends(require_admin)])
async def save_job_choice(job_id: str, kind: str, body: ChoiceBody, request: Request,
                          token: str = Depends(require_preference_write)) -> dict:
    """Pin a pick for a job's unattended runs. Like remembering, it is written
    only after a successful server-side quote priced it (nothing is
    submitted); otherwise 409 says why and the job keeps what it had."""
    _kind(kind)
    job_id = await _job(job_id)
    pinned = generation_choices.clean(body.model_dump(exclude_none=True))
    if pinned is None or not pinned.get("account") or not pinned.get("worker"):
        raise HTTPException(422, "A job choice must pin its account, worker and model.")
    quoted = await _save_time_quote(kind, body, request, token)
    if not quoted["priced"]:
        why = (quoted["quote_error"] or "This choice is unavailable right now.") \
            if quoted["unattended_skip_reason"] == "stored_choice_unavailable" else "This choice has no verified price."
        raise HTTPException(409, f"{why} It was not pinned; unattended runs skip a step without a priced pick.")
    try:
        choice = await run_sync(lambda: generation_choices.save_job(
            _identity(request), job_id, kind, body.model_dump(exclude_none=True)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"job_id": job_id, "kind": kind, "choice": choice, **quoted}


@router.delete("/jobs/{job_id}/choices/{kind}", dependencies=[Depends(require_admin)])
async def delete_job_choice(job_id: str, kind: str, request: Request,
                            token: str = Depends(require_preference_write)) -> dict:
    _kind(kind)
    job_id = await _job(job_id)
    await run_sync(lambda: generation_choices.save_job(_identity(request), job_id, kind, None))
    return {"job_id": job_id, "kind": kind, "choice": None}


@router.post("/quote")
async def quote(body: QuoteRequest, request: Request, token: str = Depends(require_auth)) -> dict:
    """Validate options against the model's declared schema and price them."""
    return await quote_choice(_kind(body.kind), body.choice.model_dump(exclude_none=True), _context(request), body.prompt)


async def quote_choice(kind: str, choice: dict, context, prompt: str | None = None) -> dict:
    from app.core.generation_resolver import matches, normalize_source, price_total
    from app.services.generation_sources import Sources

    backend = Sources(None, context)
    prompt = (prompt or "").strip() or _QUOTE_PROMPT
    source = normalize_source(choice.get("account") or choice.get("source"))
    # Priced fresh by prepare below; no listing rate (cache or its lock) first.
    candidates = await backend.catalog(kind, source=source, model=choice["model"], limit=1, prompt=prompt,
                                       worker=choice.get("worker"), listing_prices=False)
    candidates = [c for c in candidates if c.available and matches(c, {k: v for k, v in choice.items() if k != "source"})]
    if len(candidates) != 1:
        return {"ok": False, "error": "This account or model is not available right now."}
    candidate = candidates[0]
    try:
        prepared = await backend.prepare(candidate, {**candidate.options, **(choice.get("options") or {})}, prompt)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "price": candidate.price}
    price = prepared.price
    if prepared.source == "supergrok" and kind == "video":
        from app.core import native_generation_settings as native
        from app.core.provider.native_video import estimate_price
        try:
            price = estimate_price(native.NativeVideoSettings(model=prepared.model, **prepared.options))
        except (TypeError, ValueError):
            price = candidate.price
    return {"ok": True, "price": price, "options": prepared.options,
            "priced": price_total(price) is not None}
