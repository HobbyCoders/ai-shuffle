"""The owner's Claude compaction setting: on by default, persisted, never env."""
import logging

import pytest

from app.core import claude_compaction
from app.db import database


def test_on_when_never_saved():
    assert database.get_system_setting(claude_compaction.SETTING_KEY) is None
    assert claude_compaction.read() is True


@pytest.mark.parametrize("enabled", [False, True])
def test_saved_value_is_persisted(enabled):
    claude_compaction.write(enabled)
    assert database.get_system_setting(claude_compaction.SETTING_KEY) == (
        "true" if enabled else "false"
    )
    assert claude_compaction.read() is enabled


def test_malformed_or_unreadable_value_stays_on(monkeypatch, caplog):
    database.set_system_setting(claude_compaction.SETTING_KEY, "nope")
    with caplog.at_level(logging.WARNING):
        assert claude_compaction.read() is True
    assert "Malformed Claude compaction setting" in caplog.text

    def boom(_key):
        raise RuntimeError("database down")

    monkeypatch.setattr(database, "get_system_setting", boom)
    assert claude_compaction.read() is True


def test_the_environment_has_no_say(monkeypatch):
    claude_compaction.write(False)
    for name in ("FLUID_CLAUDE_COMPACTION", "AI_SHUFFLE_CLAUDE_COMPACTION", "CLAUDE_COMPACTION"):
        monkeypatch.setenv(name, "1")
    assert claude_compaction.read() is False


def test_app_config_has_no_compaction_field():
    from app.core.config import Settings

    assert not [name for name in Settings.model_fields if "compaction" in name]
