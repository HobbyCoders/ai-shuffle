"""Browser origins this worker answers.

One allow-list feeds both the CORS middleware (app.main) and the CSRF
middleware. It is `CORS_ORIGINS` plus the Android app's asset origin: the app
ships its own build of the mobile UI, loads it from that origin and calls the
worker cross-origin with a Bearer paired-session token, so a worker on any
version must accept it without being asked to serve the UI.
"""
from __future__ import annotations

from app.core.config import settings


def configured_origins(raw: str | None = None, mobile_app_origin: str | None = None) -> list[str]:
    """`CORS_ORIGINS` split and stripped, plus the mobile app origin.

    A wildcard list is returned as-is (the wildcard branch cannot carry
    credentials anyway, and it already admits the app origin).
    """
    raw = settings.cors_origins if raw is None else raw
    mobile = settings.mobile_app_origin if mobile_app_origin is None else mobile_app_origin
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" not in origins and mobile and mobile not in origins:
        origins.append(mobile)
    return origins
