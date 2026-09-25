"""Helpers and fixtures copied across several API test files.

A plain module: import what you need explicitly. ``app`` (the sessions router
alone) needs the importing file to define its own ``mock_database`` fixture;
the file's own ``client`` fixture wraps it.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from app.api.auth import require_auth
from app.api.sessions import router as sessions_router


def fake_request(api_user=None):
    """A stand-in ``Request`` carrying only ``state.api_user``."""
    return SimpleNamespace(state=SimpleNamespace(api_user=api_user))


@pytest.fixture
def mock_api_user():
    """Patch the sessions API's user lookup; ``None`` means admin."""
    with patch("app.api.sessions.get_api_user_from_request") as mock, \
            patch("app.api.sessions_helpers.get_api_user_from_request", new=mock):
        mock.return_value = None  # admin
        yield mock


@pytest.fixture
def app(mock_database, mock_api_user):
    """The sessions router alone, with auth overridden."""
    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    yield app


@pytest.fixture(autouse=True)
def not_streaming():
    """No lease-valid active run, so the streaming gate never blocks."""
    with patch("app.core.streaming_status.has_active_run", return_value=False):
        yield
