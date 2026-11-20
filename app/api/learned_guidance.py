"""Owner-only management of the current worker's saved prompt guidance."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fluid.context.guidance import GUIDANCE_FILE_MAX_CHARS
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_admin
from app.core.guidance_storage import (
    GuidanceConflict,
    GuidanceValidationError,
    SavedGuidance,
    delete_saved_guidance,
    edit_saved_guidance,
    list_saved_guidance,
)

logger = logging.getLogger(__name__)


def require_guidance_owner(request: Request) -> None:
    principal = require_admin(request)
    # require_admin allows preview reads; raw learned blocks are owner data.
    if principal.startswith("preview:") or getattr(request.state, "preview_grant", None) is not None:
        raise HTTPException(403, "Owner sign-in required to manage learned guidance")


class GuidanceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validate_request(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Do not send invalid request content back in validation errors.
                raise HTTPException(422, "Invalid guidance request") from exc

        return validate_request


router = APIRouter(
    prefix="/api/v1/learned-guidance", dependencies=[Depends(require_guidance_owner)],
    route_class=GuidanceRoute,
)


class GuidanceList(BaseModel):
    blocks: list[SavedGuidance]
    max_chars: int = GUIDANCE_FILE_MAX_CHARS


class EditGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except GuidanceValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except GuidanceConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        logger.warning("Learned guidance store unavailable", exc_info=True)
        raise HTTPException(503, "Learned guidance is unavailable on this worker") from exc


@router.get("")
def list_blocks() -> GuidanceList:
    with _storage_errors():
        return GuidanceList(blocks=list_saved_guidance())


@router.put("/{block_id}")
def save_block(block_id: str, body: EditGuidance) -> SavedGuidance:
    with _storage_errors():
        return edit_saved_guidance(block_id, body.expected_revision, body.content)


@router.delete("/{block_id}", status_code=204)
def remove_block(
    block_id: str,
    expected_revision: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
) -> Response:
    with _storage_errors():
        delete_saved_guidance(block_id, expected_revision)
    return Response(status_code=204)
