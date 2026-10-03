"""Support reports API: "Send report to HobbyCoders support" (owner only).

Docs: docs/content/api-reference/support-reports.md. The logic lives in
``app.core.support_report``. A report is built, shown, and sent only when the
owner calls ``/send``; starting an AI diagnosis spends the owner's own AI usage
and needs ``acknowledge_ai_usage: true``. Agent previews (a preview cookie)
can read drafts but cannot start a diagnosis or send.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool

from app.api.auth import require_admin
from app.core import preview_access
from app.core import support_report as reports

router = APIRouter(prefix="/api/v1/support", tags=["Support reports"])


class ErrorIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    source: Optional[Literal["frontend", "backend", "ws", "api"]] = None
    detail: Optional[str] = Field(None, max_length=40000)
    url: Optional[str] = Field(None, max_length=2048)
    status_code: Optional[int] = Field(None, ge=0, le=999)
    model_config = {"extra": "ignore"}


class ReportIn(BaseModel):
    error_id: Optional[str] = Field(None, max_length=64)
    error: Optional[ErrorIn] = None
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
async def get_capabilities(_=Depends(require_admin)) -> dict[str, Any]:
    return await asyncio.to_thread(reports.capabilities)


@router.post("/reports")
async def build_report(body: ReportIn, _=Depends(require_admin)) -> dict[str, Any]:
    """Build a basic report preview (no AI, nothing sent)."""
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
async def set_note(report_id: str, body: NoteIn, _=Depends(require_admin)) -> dict[str, Any]:
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
async def send_report(report_id: str, body: SendIn, _=Depends(_owner_action)) -> dict[str, Any]:
    """Send the previewed report. The only path that sends anything."""
    if body.confirm is not True:
        raise HTTPException(400, "Confirm sending the report.")
    try:
        return await asyncio.to_thread(reports.send, report_id)
    except reports.ReportError as exc:
        _raise(exc)
