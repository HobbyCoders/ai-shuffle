"""Live editing of a voice session's worker execution context."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.core.generation_options import VALID_EFFORTS
from app.core.query_options import decode_provider_model
from app.db import database
from app.db.async_database import run_sync


router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])

# These are the values exposed by the normal composer. Keep this list aligned
# with QueryOverrides / ControlsDock rather than accepting arbitrary SDK modes.
VALID_PERMISSION_MODES = (
    "default",
    "auto",
    "acceptEdits",
    "plan",
    "bypassPermissions",
)


class VoiceContextPatch(BaseModel):
    """Sparse changes to the worker context owned by a voice session."""

    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None
    context_window: Optional[int] = Field(None, gt=0)
    fast: Optional[bool] = None


class VoiceContextResponse(BaseModel):
    session_id: str
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None
    context_window: Optional[int] = None
    fast: Optional[bool] = None


def _validate_patch(body: VoiceContextPatch) -> None:
    provided = body.model_fields_set

    if "model" in provided and body.model is not None:
        model = body.model.strip()
        if not model:
            raise HTTPException(status_code=400, detail="model must not be empty.")
        decoded = decode_provider_model(model)
        if model.startswith("prov:") and decoded is None:
            raise HTTPException(
                status_code=400,
                detail="model must be a valid provider/model selection.",
            )
        if decoded is not None:
            from app.core.provider_agent import validate_provider_model

            validation = validate_provider_model(
                decoded[0], decoded[1], require_servable=False
            )
            if not validation.ok:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        validation.reason
                        or f"Model {decoded[1]!r} is not available on that provider."
                    ),
                )

    if "effort" in provided and body.effort is not None:
        effort = body.effort.strip()
        if effort not in VALID_EFFORTS:
            raise HTTPException(
                status_code=400,
                detail=f"effort must be one of {list(VALID_EFFORTS)}",
            )

    if "permission_mode" in provided and body.permission_mode is not None:
        permission_mode = body.permission_mode.strip()
        if permission_mode not in VALID_PERMISSION_MODES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "permission_mode must be one of "
                    f"{list(VALID_PERMISSION_MODES)}"
                ),
            )


def _persist_patch(session_id: str, body: VoiceContextPatch) -> dict:
    """Persist supplied fields and return a fresh row.

    ``preserve_provider`` is essential for voice rows: ``provider_id`` funds the
    OpenAI Realtime call, while ``model`` selects the text worker dispatched by
    that call. A provider/model composite must therefore not rebind the voice
    row's realtime provider as it does for an ordinary code session.
    """
    provided = body.model_fields_set
    values = body.model_dump()
    kwargs = {name: values[name] for name in provided}
    if "model" in kwargs and kwargs["model"] is not None:
        kwargs["model"] = kwargs["model"].strip()
    if "effort" in kwargs and kwargs["effort"] is not None:
        kwargs["effort"] = kwargs["effort"].strip()
    if "permission_mode" in kwargs and kwargs["permission_mode"] is not None:
        kwargs["permission_mode"] = kwargs["permission_mode"].strip()

    if kwargs:
        database.set_session_run_binding(
            session_id,
            preserve_provider=True,
            **kwargs,
        )
    return database.get_session(session_id) or {}


@router.patch(
    "/sessions/{session_id}/context",
    response_model=VoiceContextResponse,
)
async def update_voice_session_context(
    request: Request,
    session_id: str,
    body: VoiceContextPatch,
    token: str = Depends(require_auth),
):
    """Update the execution context used by workers dispatched from a live call."""
    session = await require_session(request, session_id)
    if session.get("mode") != "voice":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice session not found.",
        )

    _validate_patch(body)
    updated = await run_sync(_persist_patch, session_id, body)
    return VoiceContextResponse(
        session_id=session_id,
        model=updated.get("model"),
        effort=updated.get("effort"),
        permission_mode=updated.get("permission_mode"),
        context_window=updated.get("context_window"),
        fast=updated.get("fast"),
    )
