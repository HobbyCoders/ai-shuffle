"""Admin-only import of legacy provider settings into Fluid-native paths."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.auth import require_admin
from app.core.settings_import_service import (
    DEFAULT_PROVIDERS,
    InvalidImportSelection,
    InvalidImportSource,
    StaleImportPlan,
    apply_import,
    preview_import,
)

router = APIRouter(prefix="/api/settings/import", tags=["Settings import"])


class SettingsImportPreviewRequest(BaseModel):
    source_home: str | None = None
    providers: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDERS))


class SettingsImportApplyRequest(BaseModel):
    token: str = Field(..., min_length=1)
    selected_item_ids: list[str] = Field(default_factory=list)
    source_home: str | None = None
    providers: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDERS))


@router.post("/preview")
async def preview_provider_settings(
    request: SettingsImportPreviewRequest,
    _: str = Depends(require_admin),
):
    try:
        return preview_import(source_home=request.source_home, providers=request.providers)
    except InvalidImportSource as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/apply")
async def apply_provider_settings(
    request: SettingsImportApplyRequest,
    _: str = Depends(require_admin),
):
    try:
        return apply_import(
            token=request.token,
            selected_item_ids=request.selected_item_ids,
            source_home=request.source_home,
            providers=request.providers,
        )
    except (InvalidImportSource, InvalidImportSelection) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StaleImportPlan as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/claude/preview", include_in_schema=False)
async def preview_claude_settings(
    request: SettingsImportPreviewRequest,
    _: str = Depends(require_admin),
):
    try:
        return preview_import(source_home=request.source_home, providers=["claude"])
    except InvalidImportSource as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/claude/apply", include_in_schema=False)
async def apply_claude_settings(
    request: SettingsImportApplyRequest,
    _: str = Depends(require_admin),
):
    try:
        return apply_import(
            token=request.token,
            selected_item_ids=request.selected_item_ids,
            source_home=request.source_home,
            providers=["claude"],
        )
    except (InvalidImportSource, InvalidImportSelection) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StaleImportPlan as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
