"""Remembered generation choices and live quotes for the in-chat choice card.

The choices are account preferences (``app.core.generation_choices``). Quotes
run the same declared-option validation and pricing the generation tools use;
nothing is reserved or submitted here.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.api.preferences import require_preference_write
from app.core import generation_choices
from app.core.preference_identity import request_preference_identity
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/generation", tags=["Generation choices"])

_QUOTE_PROMPT = "A preview of this choice."


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
    """Remembered and last picks per kind, after the one-time Settings migration."""
    def load():
        return {**generation_choices.load(_identity(request)), "worker": _here(),
                "kinds": list(generation_choices.KINDS)}
    return await run_sync(load)


@router.put("/choices/{kind}")
async def save_choice(kind: str, body: SaveChoiceRequest, request: Request,
                      token: str = Depends(require_preference_write)) -> dict:
    """Record a pick as the last choice and, with ``remember``, reuse it next time."""
    _kind(kind)
    try:
        choice = await run_sync(lambda: generation_choices.save(
            _identity(request), kind, body.choice.model_dump(exclude_none=True),
            remember=body.remember, origin=body.origin))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"kind": kind, "choice": choice, "remembered": body.remember}


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


@router.post("/quote")
async def quote(body: QuoteRequest, request: Request, token: str = Depends(require_auth)) -> dict:
    """Validate options against the model's declared schema and price them."""
    from app.core.generation_resolver import matches, normalize_source, price_total
    from app.services.generation_sources import Sources

    kind = _kind(body.kind)
    choice = body.choice.model_dump(exclude_none=True)
    backend = Sources(None, _context(request))
    prompt = (body.prompt or "").strip() or _QUOTE_PROMPT
    source = normalize_source(choice.get("account") or choice.get("source"))
    candidates = await backend.catalog(kind, source=source, model=choice["model"], limit=1, prompt=prompt,
                                       worker=choice.get("worker"))
    candidates = [c for c in candidates if c.available and matches(c, {k: v for k, v in choice.items() if k != "source"})]
    if len(candidates) != 1:
        return {"ok": False, "error": "This account or model is not available right now."}
    candidate = candidates[0]
    try:
        prepared = await backend.prepare(candidate, {**candidate.options, **body.choice.options}, prompt)
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
