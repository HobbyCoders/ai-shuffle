"""Remembered generation choices: account preferences and the Settings migration."""
import json

import pytest

from app.core import generation_choices as choices
from app.db import database

OWNER = ("account", "origin#owner")
OTHER = ("api_user", "user-b")


@pytest.fixture(autouse=True)
def _db(mock_db, monkeypatch):
    from app.network import runners
    monkeypatch.setattr(runners, "self_device_id", lambda: "worker-a")
    return mock_db


def test_keys_follow_account_preference_conventions():
    assert choices.choice_key("image") == "generation.choice.image"
    assert choices.last_key("3d") == "generation.lastChoice.3d"
    assert choices.MIGRATED_KEY == "generation.choicesMigrated"


@pytest.mark.parametrize("value", [None, "fal", {}, {"model": "m"}, {"source": "fal", "model": ""},
                                   {"source": "fal", "model": "m", "options": "wide"},
                                   {"source": 3, "model": "m"}, {"source": "fal", "model": "m", "options": {"x": "y" * 9000}}])
def test_malformed_choices_are_ignored(value):
    assert choices.clean(value) is None


def test_clean_keeps_only_known_fields():
    value = {"source": "fal", "account": "fal-ai", "worker": "w", "model": " m ", "options": {"size": "wide"},
             "origin": "card", "extra": "dropped", "price": {"amount": "1"}}
    assert choices.clean(value) == {"source": "fal", "account": "fal-ai", "worker": "w", "model": "m",
                                    "options": {"size": "wide"}, "origin": "card"}


def test_saved_fal_settings_migrate_once_with_their_options():
    database.set_system_setting("image_model", "fal-ai/flux/dev")
    database.set_system_setting("image_aspect_ratio", "16:9")
    database.set_system_setting("image_model_params", json.dumps({"num_inference_steps": 30}))
    database.set_system_setting("video_model", "fal-ai/kling")
    database.set_system_setting("video_duration", "5")
    loaded = choices.load(OWNER)
    assert loaded["migrated"] is True
    assert loaded["remembered"]["image"] == {"source": "fal", "account": "fal-ai", "worker": "worker-a",
                                             "model": "fal-ai/flux/dev", "origin": "migrated",
                                             "options": {"aspect_ratio": "16:9", "num_inference_steps": 30}}
    assert loaded["remembered"]["video"]["options"] == {"duration": 5}
    assert set(loaded["remembered"]) == {"image", "video"}
    # A cleared choice is not brought back by the saved setting.
    choices.forget(OWNER, "image")
    assert "image" not in choices.load(OWNER)["remembered"]
    # Each account migrates on its own.
    assert choices.load(OTHER)["remembered"]["image"]["model"] == "fal-ai/flux/dev"


def test_unsaved_defaults_and_the_chatgpt_plan_setting_are_not_migrated():
    database.set_system_setting("image_model", "chatgpt-subscription/gpt-image-1")
    loaded = choices.load(OWNER)
    assert loaded == {"remembered": {}, "last": {}, "migrated": True}


def test_migration_never_overwrites_an_existing_pick():
    database.set_user_preference(*OWNER, choices.choice_key("image"),
                                 {"source": "supergrok", "account": "grok-1", "model": "grok-imagine-image"})
    database.set_system_setting("image_model", "fal-ai/flux/dev")
    assert choices.load(OWNER)["remembered"]["image"]["account"] == "grok-1"


def test_save_records_last_and_optionally_remembers():
    pick = {"source": "fal", "account": "fal-ai", "worker": "w", "model": "fal-ai/x", "options": {"size": "wide"}}
    choices.save(OWNER, "image", pick, remember=False)
    loaded = choices.load(OWNER)
    assert loaded["last"]["image"] == {**pick, "origin": "card"} and "image" not in loaded["remembered"]
    choices.save(OWNER, "image", pick, remember=True, origin="settings")
    assert choices.load(OWNER)["remembered"]["image"]["origin"] == "settings"
    with pytest.raises(ValueError):
        choices.save(OWNER, "image", {"model": "x"}, remember=True)
    with pytest.raises(ValueError):
        choices.save(OWNER, "music", pick, remember=True)


def test_save_migrates_first_so_a_later_read_cannot_overwrite_the_pick():
    database.set_system_setting("image_model", "fal-ai/flux/dev")
    choices.save(OWNER, "image", {"source": "fal", "model": "fal-ai/other"}, remember=True)
    assert choices.load(OWNER)["remembered"]["image"]["model"] == "fal-ai/other"


def test_for_session_never_raises(monkeypatch):
    from app.core import model_effort

    def broken(row):
        raise PermissionError("Worker owner identity is ambiguous")
    monkeypatch.setattr(model_effort, "_caller_preference_identity", broken)
    assert choices.for_session({"id": "s"}) == {"remembered": {}, "last": {}, "migrated": False}


def test_for_session_reads_the_api_users_own_choices(monkeypatch):
    from app.core import model_effort
    seen = []
    monkeypatch.setattr(model_effort, "_caller_preference_identity", lambda row: seen.append(row) or OTHER)
    choices.save(OTHER, "audio", {"source": "fal", "model": "fal-ai/sfx"}, remember=True)
    assert choices.for_session({"id": "s", "api_user_id": "user-b"})["remembered"]["audio"]["model"] == "fal-ai/sfx"
    assert seen == [{"id": "s", "api_user_id": "user-b"}]


def test_query_options_reads_choices_only_with_generation_tools(monkeypatch):
    from app.core import query_options
    calls = []
    monkeypatch.setattr(choices, "for_session", lambda row: calls.append(row) or {"remembered": {}, "last": {}, "migrated": True})
    assert query_options._generation_choices({}, {"id": "s"}, None)["migrated"] is False
    assert query_options._generation_choices({"image_generation": False}, {"id": "s"}, None)["migrated"] is False
    assert calls == []
    assert query_options._generation_choices({"image_generation": True}, {"id": "s"}, "user-b")["migrated"] is True
    assert calls == [{"id": "s", "api_user_id": "user-b"}]
