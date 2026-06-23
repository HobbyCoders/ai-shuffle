"""
Tests for honest Claude OAuth token-status reporting.

``AuthService.is_claude_authenticated()`` intentionally returns True whenever a
non-empty ``.credentials.json`` exists (file-exists semantics, kept for
back-compat). The enriched status helpers added alongside the "Re-authenticate"
UI must instead report whether the token is actually USABLE:

    - get_claude_token_status() → {token_valid, expired, expires_at,
      has_refresh_token}
    - get_claude_auth_info()    → the above fields PLUS the legacy
      ``authenticated`` flag and paths.

The critical regression these guard against: a credential with an EXPIRED
access token and an EMPTY refresh token (unrecoverable without a fresh login)
must report ``token_valid=False`` / ``expired=True`` even though
``authenticated`` stays True — that is exactly the "401 Invalid authentication
credentials" state the UI previously mislabelled as healthy.
"""

import json
import time
from unittest.mock import patch

import pytest

import app.core.auth as auth_mod
from app.core.auth import AuthService
from app.core.oauth import DirectOAuth


def _write_creds(config_dir, *, access_token="tok", refresh_token="refresh", expires_at=None):
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"claudeAiOauth": {}}
    if access_token is not None:
        payload["claudeAiOauth"]["accessToken"] = access_token
    if refresh_token is not None:
        payload["claudeAiOauth"]["refreshToken"] = refresh_token
    if expires_at is not None:
        payload["claudeAiOauth"]["expiresAt"] = expires_at
    (config_dir / ".credentials.json").write_text(json.dumps(payload))


@pytest.fixture
def auth_with_dir(temp_dir):
    """An AuthService and a DirectOAuth both pointed at the same temp .claude dir.

    The global ``app.core.auth.direct_oauth`` is swapped for a fresh instance so
    the 60s validity cache from other tests/real home dir can't leak in.
    """
    config_dir = temp_dir / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)

    svc = AuthService()
    svc.config_dir = config_dir

    fresh_oauth = DirectOAuth(config_dir=config_dir)
    with patch.object(auth_mod, "direct_oauth", fresh_oauth):
        # _ensure_onboarding_complete writes settings.json into config_dir — fine.
        yield svc, config_dir


class TestGetClaudeTokenStatus:
    def test_valid_token_reports_valid(self, auth_with_dir):
        svc, config_dir = auth_with_dir
        future = int((time.time() + 3600) * 1000)
        _write_creds(config_dir, refresh_token="refresh", expires_at=future)

        status = svc.get_claude_token_status()

        assert status["token_valid"] is True
        assert status["expired"] is False
        assert status["expires_at"] == future
        assert status["has_refresh_token"] is True

    def test_expired_token_with_empty_refresh_reports_invalid(self, auth_with_dir):
        """The root-cause bug: expired access token + EMPTY refresh token."""
        svc, config_dir = auth_with_dir
        past = int((time.time() - 3600) * 1000)
        _write_creds(config_dir, refresh_token="", expires_at=past)

        status = svc.get_claude_token_status()

        assert status["token_valid"] is False
        assert status["expired"] is True
        assert status["has_refresh_token"] is False
        assert status["expires_at"] == past

    def test_missing_credentials_file_reports_invalid_not_expired(self, auth_with_dir):
        svc, _config_dir = auth_with_dir

        status = svc.get_claude_token_status()

        assert status["token_valid"] is False
        # No file → nothing to expire; this is the "not connected" state.
        assert status["expired"] is False
        assert status["expires_at"] is None
        assert status["has_refresh_token"] is False

    def test_expiring_within_buffer_reports_invalid(self, auth_with_dir):
        """has_valid_credentials() uses a 5-min buffer — honour it here too."""
        svc, config_dir = auth_with_dir
        soon = int((time.time() + 120) * 1000)  # 2 min → within 5-min buffer
        _write_creds(config_dir, refresh_token="refresh", expires_at=soon)

        status = svc.get_claude_token_status()

        assert status["token_valid"] is False
        assert status["expired"] is True


class TestGetClaudeAuthInfoEnriched:
    def test_valid_token_authenticated_and_valid(self, auth_with_dir):
        svc, config_dir = auth_with_dir
        future = int((time.time() + 3600) * 1000)
        _write_creds(config_dir, refresh_token="refresh", expires_at=future)

        info = svc.get_claude_auth_info()

        assert info["authenticated"] is True
        assert info["token_valid"] is True
        assert info["expired"] is False
        assert info["has_refresh_token"] is True
        # Back-compat keys preserved.
        assert "config_dir" in info
        assert "credentials_file" in info

    def test_expired_empty_refresh_authenticated_but_invalid(self, auth_with_dir):
        """The misleading-status bug, exactly: file exists → authenticated True,
        but token_valid False so the UI can show 'token expired — reconnect'."""
        svc, config_dir = auth_with_dir
        past = int((time.time() - 3600) * 1000)
        _write_creds(config_dir, refresh_token="", expires_at=past)

        info = svc.get_claude_auth_info()

        # Legacy flag stays True (file-exists semantics) — do NOT break callers.
        assert info["authenticated"] is True
        # Honest signal: token is dead.
        assert info["token_valid"] is False
        assert info["expired"] is True
        assert info["has_refresh_token"] is False

    def test_no_credentials_not_authenticated_and_invalid(self, auth_with_dir):
        svc, _config_dir = auth_with_dir

        info = svc.get_claude_auth_info()

        assert info["authenticated"] is False
        assert info["token_valid"] is False
        assert info["expired"] is False
