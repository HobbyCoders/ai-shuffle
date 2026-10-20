"""Support reports API: owner support and authenticated AI content reporting.

Docs: docs/content/api-reference/support-reports.md. The logic lives in
``app.core.support_report``. A report is built, shown, and sent only when the
reporter calls ``/send``; starting an AI diagnosis spends the owner's own AI usage
and needs ``acknowledge_ai_usage: true``. Agent previews (a preview cookie)
can read drafts but cannot start a diagnosis or send.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool

from app.api.auth import require_admin, require_auth
from app.api.pairing import _ExchangeRateLimiter
from app.core.preference_identity import request_preference_identity
from app.core import preview_access
from app.core import support_report as reports

router = APIRouter(prefix="/api/v1/support", tags=["Support reports"])

# Bound both draft creation and delivery attempts, including hub failures.
_build_limiter = _ExchangeRateLimiter(limit=5, window_seconds=3600)
_send_limiter = _ExchangeRateLimiter(limit=5, window_seconds=3600)


def _reporter(request: Request, _=Depends(require_auth)) -> str:
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot submit support reports.")
    if getattr(request.state, "api_user", None):
        raise HTTPException(403, "API keys and connectors cannot submit support reports.")
    if not getattr(request.state, "paired_client", None) and getattr(request.state, "is_admin", False):
        return "install-admin"
    kind, user_id = request_preference_identity(request)
    return f"{kind}:{user_id}"


def _rate_limit(limiter: _ExchangeRateLimiter, user_id: str) -> None:
    try:
        limiter.check(user_id)
    except HTTPException as exc:
        raise HTTPException(429, "Too many support reports. Try again in an hour.") from exc


def _writable_draft(request: Request, report_id: str, user_id: str) -> None:
    if getattr(request.state, "is_admin", False):
        return
    try:
        draft = reports.get_draft(report_id)
    except reports.ReportError as exc:
        _raise(exc)
    if draft.get("report_kind") != "ai_content" or draft.get("reporter_user_id") != user_id:
        raise HTTPException(403, "You can only submit your own AI content reports.")


class ErrorIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    source: Optional[Literal["frontend", "backend", "ws", "api"]] = None
    detail: Optional[str] = Field(None, max_length=40000)
    url: Optional[str] = Field(None, max_length=2048)
    status_code: Optional[int] = Field(None, ge=0, le=999)
    model_config = {"extra": "ignore"}


class ReportIn(BaseModel):
    # "problem": an app problem (error_id or error). "ai_content": offensive or
    # harmful AI output (excerpt, the reason in user_note, optional session_id).
    kind: Literal["problem", "ai_content"] = "problem"
    error_id: Optional[str] = Field(None, max_length=64)
    error: Optional[ErrorIn] = None
    excerpt: Optional[str] = Field(None, max_length=reports.EXCERPT_MAX)
    session_id: Optional[str] = Field(None, max_length=reports.SESSION_REF_MAX)
    user_note: Optional[str] = Field(None, max_length=reports.NOTE_MAX)
    model_config = {"extra": "forbid"}


class NoteIn(BaseModel):
    user_note: Optional[str] = Field(None, max_length=reports.NOTE_MAX)
    model_config = {"extra": "forbid"}


class CheckIn(BaseModel):
    report_id: Optional[str] = Field(None, max_length=64)
    fingerprint: Optional[str] = Field(None, max_length=64)
    model_config = {"extra": "forbid"}


class DiagnosisIn(BaseModel):
    report_id: str = Field(..., max_length=64)
    model: Optional[str] = Field(None, max_length=300)
    acknowledge_ai_usage: StrictBool
    model_config = {"extra": "forbid"}


class SendIn(BaseModel):
    confirm: StrictBool
    model_config = {"extra": "forbid"}


def _owner_action(request: Request, token=Depends(require_admin)):
    """Spending AI usage or sending a report needs the owner, not an agent preview."""
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot start a diagnosis or send a support report.")
    return token


def _raise(exc: reports.ReportError) -> NoReturn:
    raise HTTPException(exc.status, exc.message) from None


@router.get("/capabilities")
async def get_capabilities(request: Request, _=Depends(require_auth)) -> dict[str, Any]:
    caps = await asyncio.to_thread(reports.capabilities)
    admin = getattr(request.state, "is_admin", False)
    caps["can_report_ai_content"] = (preview_access.grant(request) is None
                                     and not getattr(request.state, "api_user", None))
    if not admin:
        caps.update(basic_report=False, ai_diagnosis=False, default_model=None,
                    fix_with_ai=False, fix_with_ai_reason=None, report_kinds=["ai_content"])
    return caps


@router.post("/reports")
async def build_report(body: ReportIn, request: Request, user_id=Depends(_reporter)) -> dict[str, Any]:
    """Build a basic report preview (no AI, nothing sent)."""
    if body.kind != "ai_content" and not getattr(request.state, "is_admin", False):
        raise HTTPException(403, "Owner access required")
    if body.kind == "ai_content":
        _rate_limit(_build_limiter, user_id)
        if body.error_id or body.error:
            raise HTTPException(400, "An AI content report takes an excerpt, not an error.")
        try:
            return await asyncio.to_thread(reports.build_ai_content, excerpt=body.excerpt,
                                           user_note=body.user_note, session_id=body.session_id,
                                           reporter_user_id=user_id,
                                           retention_user_id=None if getattr(request.state, "is_admin", False) else user_id)
        except reports.ReportError as exc:
            _raise(exc)
    if body.excerpt or body.session_id:
        raise HTTPException(400, "An excerpt belongs to an AI content report (kind ai_content).")
    if bool(body.error_id) == bool(body.error):
        raise HTTPException(400, "Send exactly one of error_id or error.")
    try:
        return await asyncio.to_thread(reports.build_basic, error_id=body.error_id,
                                       error=body.error.model_dump() if body.error else None, user_note=body.user_note)
    except reports.ReportError as exc:
        _raise(exc)


@router.get("/reports/{report_id}")
async def get_report(report_id: str, _=Depends(require_admin)) -> dict[str, Any]:
    try:
        return reports.preview(reports.get_draft(report_id))
    except reports.ReportError as exc:
        _raise(exc)


@router.post("/reports/{report_id}/note")
async def set_note(report_id: str, body: NoteIn, request: Request, user_id=Depends(_reporter)) -> dict[str, Any]:
    _writable_draft(request, report_id, user_id)
    try:
        return await asyncio.to_thread(reports.set_note, report_id, body.user_note)
    except reports.ReportError as exc:
        _raise(exc)


@router.post("/check")
async def check_known_issue(body: CheckIn, _=Depends(require_admin)) -> dict[str, Any]:
    """Known-issue check. Sends only the 32-hex fingerprint to the hub."""
    fingerprint = body.fingerprint
    try:
        if body.report_id:
            fingerprint = reports.get_draft(body.report_id)["fingerprint"]
        if not fingerprint:
            raise reports.ReportError(400, "Send report_id or fingerprint.")
        return await asyncio.to_thread(reports.check, fingerprint)
    except reports.ReportError as exc:
        _raise(exc)


@router.post("/diagnoses", status_code=202)
async def start_diagnosis(body: DiagnosisIn, _=Depends(_owner_action)) -> dict[str, Any]:
    """Start the AI diagnosis. Spends the owner's AI usage; needs the acknowledgement."""
    if body.acknowledge_ai_usage is not True:
        raise HTTPException(400, "An AI diagnosis uses your own AI usage. Confirm that before starting it.")
    try:
        reports.get_draft(body.report_id)
        try:
            resolved = await asyncio.to_thread(reports.resolve_model, body.model)
        except Exception as exc:  # noqa: BLE001
            raise reports.ReportError(400, f"That model is not available: {exc}") from None
        return reports.start_diagnosis(body.report_id, body.model, resolved)
    except reports.ReportError as exc:
        _raise(exc)


@router.get("/diagnoses/{run_id}")
async def get_diagnosis(run_id: str, _=Depends(require_admin)) -> dict[str, Any]:
    try:
        return reports.get_run(run_id)
    except reports.ReportError as exc:
        _raise(exc)


@router.post("/reports/{report_id}/send")
async def send_report(report_id: str, body: SendIn, request: Request, user_id=Depends(_reporter)) -> dict[str, Any]:
    """Send the previewed report. The only path that sends anything."""
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot send a support report.")
    _writable_draft(request, report_id, user_id)
    if body.confirm is not True:
        raise HTTPException(400, "Confirm sending the report.")
    try:
        if reports.get_draft(report_id).get("report_kind") == "ai_content":
            _rate_limit(_send_limiter, user_id)
        return await asyncio.to_thread(reports.send, report_id)
    except reports.ReportError as exc:
        _raise(exc)
