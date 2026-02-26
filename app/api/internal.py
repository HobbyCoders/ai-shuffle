"""
Internal API endpoints

Endpoints used internally by the application.
These are not intended for external consumption.
"""

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["Internal"])
