"""Failed-auth logging never includes token material and cannot flood (SEC-16),
and Discord message content stays out of INFO logs (SEC-17)."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import auth


@pytest.fixture(autouse=True)
def _fresh_log_state(monkeypatch):
    monkeypatch.setattr(auth, "_auth_fail_log_state", {})


def test_failed_session_token_is_not_logged(client, caplog):
    token = "sess-SECRETTAILzz9q"
    with caplog.at_level(logging.WARNING, logger="app.api.auth"):
        response = client.get("/api/v1/auth/github/status", cookies={"session": token})
    assert response.status_code == 401
    assert "zz9q" not in caplog.text
    assert "SECRETTAIL" not in caplog.text
    assert "session token present" in caplog.text


def test_unauthenticated_warning_is_rate_limited(caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.auth"):
        for _ in range(50):
            auth._log_auth_failure("unauthenticated", "Auth failed completely")
    assert caplog.text.count("Auth failed completely") == 1


def test_rate_limited_warning_reports_what_it_held_back(monkeypatch, caplog):
    clock = iter([0.0, 1.0, 2.0, 100.0])
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.WARNING, logger="app.api.auth"):
        for _ in range(4):
            auth._log_auth_failure("unauthenticated", "Auth failed completely")
    assert caplog.text.count("Auth failed completely") == 2
    assert "2 similar failures" in caplog.text


async def test_discord_message_content_is_not_logged_at_info(caplog):
    from app.core.connectors.discord_connector import DiscordConnector

    conn = DiscordConnector("dc-1", "discord", {}, AsyncMock(), AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=42),
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2, name="general"),
        content="my private words",
        id=7,
    )
    with caplog.at_level(logging.INFO, logger="app.core.connectors.discord_connector"):
        await conn._on_message_handler(message)
    assert "my private words" not in caplog.text
