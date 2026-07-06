"""Tests for the durable session_store_mode default + boot warning.

Both production installs rely on SESSION_STORE_MODE=postgres_only, but the CODE
default used to be "disabled" — a footgun that silently reverts any install
missing the env var to turn-boundary persistence (a full-turn data-loss window
on a crash). The default is now "postgres_only", and check_prerequisites logs a
loud WARNING if the effective mode is "disabled" outside of tests.
"""

import logging
import os

from app.core.config import Settings, settings
from app.core import startup


class TestSessionStoreDefault:
    def test_code_default_is_postgres_only(self, monkeypatch):
        """A fresh Settings() with no SESSION_STORE_MODE env must default to the
        durable mode."""
        monkeypatch.delenv("SESSION_STORE_MODE", raising=False)
        # Build without reading the repo .env so we observe the code default.
        s = Settings(_env_file=None)
        assert s.session_store_mode == "postgres_only"

    def test_postgres_only_is_the_field_default(self):
        """Guard the literal so a future edit can't silently regress it."""
        assert Settings.model_fields["session_store_mode"].default == "postgres_only"


class TestDurabilityBootWarning:
    def test_warns_when_disabled_and_not_testing(self, monkeypatch, caplog):
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setattr(settings, "session_store_mode", "disabled", raising=False)
        with caplog.at_level(logging.WARNING, logger=startup.logger.name):
            startup._check_session_store_durability()
        assert any(
            "DURABILITY DEGRADED" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        ), "expected a loud durability WARNING when mode=disabled and not testing"

    def test_no_warning_when_postgres_only(self, monkeypatch, caplog):
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setattr(settings, "session_store_mode", "postgres_only", raising=False)
        with caplog.at_level(logging.WARNING, logger=startup.logger.name):
            startup._check_session_store_durability()
        assert not any("DURABILITY DEGRADED" in r.message for r in caplog.records)

    def test_suppressed_under_testing(self, monkeypatch, caplog):
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setattr(settings, "session_store_mode", "disabled", raising=False)
        with caplog.at_level(logging.WARNING, logger=startup.logger.name):
            startup._check_session_store_durability()
        assert not any(
            "DURABILITY DEGRADED" in r.message for r in caplog.records
        ), "warning must be suppressed under TESTING=1 (conftest sets this)"
