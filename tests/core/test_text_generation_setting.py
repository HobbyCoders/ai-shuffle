"""The text-generation setting — one model + effort behind every internal call.

Session titles, commit messages, PR descriptions, branch names and agent
generation all run one-shot, outside any session. That binding used to live on
the built-in "Default AI" agent; it is now a pair of system settings edited
under Settings → Providers, because generation has no session and therefore no
agent whose configuration would sensibly apply.

Covers:
- ``text_generation_config`` reads the setting, splits a ``prov:`` composite
  into model + provider, and falls back to the registry default when unset;
- an invalid stored effort is ignored rather than passed downstream;
- ``_retire_default_ai_agent`` carries an existing install's Default AI model
  (and effort) onto the setting exactly once and unlocks the row for deletion,
  without deleting it or touching its config;
- startup seeds NO agent: a fresh install has an empty agent list and its
  owner creates the first one.
"""

from app.core.generation_options import (
    TEXT_GENERATION_EFFORT_KEY,
    TEXT_GENERATION_MODEL_KEY,
    text_generation_config,
)
from app.core.model_registry import DEFAULT_MODEL
from app.core.profiles import (
    RETIRED_DEFAULT_AI_ID,
    _retire_default_ai_agent,
    run_migrations,
)
from app.db import database as db


def _mk_default_ai(config, *, is_builtin=True):
    return db.create_profile(
        profile_id=RETIRED_DEFAULT_AI_ID,
        name="Default AI",
        description="Built-in profile used for AI-powered features",
        config=config,
        is_builtin=is_builtin,
    )


class TestTextGenerationConfig:
    def test_unset_falls_back_to_the_registry_default(self, mock_db):
        assert text_generation_config() == {
            "model": DEFAULT_MODEL,
            "provider": None,
            "effort": None,
        }

    def test_reads_a_native_model_and_effort(self, mock_db):
        db.set_system_setting(TEXT_GENERATION_MODEL_KEY, "haiku")
        db.set_system_setting(TEXT_GENERATION_EFFORT_KEY, "low")
        assert text_generation_config() == {
            "model": "haiku",
            "provider": None,
            "effort": "low",
        }

    def test_splits_a_provider_composite(self, mock_db):
        db.set_system_setting(TEXT_GENERATION_MODEL_KEY, "prov:op:openai/gpt-5.6")
        assert text_generation_config() == {
            "model": "openai/gpt-5.6",
            "provider": "op",
            "effort": None,
        }

    def test_ignores_a_bogus_stored_effort(self, mock_db):
        db.set_system_setting(TEXT_GENERATION_EFFORT_KEY, "turbo")
        assert text_generation_config()["effort"] is None


class TestRetireDefaultAiAgent:
    def test_carries_the_agents_model_and_effort(self, mock_db):
        _mk_default_ai({"model": "opus", "effort": "high"})
        _retire_default_ai_agent()

        assert db.get_system_setting(TEXT_GENERATION_MODEL_KEY) == "opus"
        assert db.get_system_setting(TEXT_GENERATION_EFFORT_KEY) == "high"

    def test_carries_a_provider_bound_model_as_a_composite(self, mock_db):
        _mk_default_ai({"model": "openai/gpt-5.6", "provider": "op"})
        _retire_default_ai_agent()

        assert db.get_system_setting(TEXT_GENERATION_MODEL_KEY) == "prov:op:openai/gpt-5.6"
        # And it round-trips back through the reader.
        assert text_generation_config()["provider"] == "op"

    def test_never_overwrites_a_configured_setting(self, mock_db):
        db.set_system_setting(TEXT_GENERATION_MODEL_KEY, "haiku")
        _mk_default_ai({"model": "opus"})
        _retire_default_ai_agent()

        assert db.get_system_setting(TEXT_GENERATION_MODEL_KEY) == "haiku"

    def test_unlocks_the_row_for_deletion(self, mock_db):
        """Clearing is_builtin is what puts a Delete button on the agent — the
        app no longer creates or depends on it, so its owner decides."""
        _mk_default_ai({"model": "opus"})
        _retire_default_ai_agent()

        row = db.get_profile(RETIRED_DEFAULT_AI_ID) or {}
        assert row.get("is_builtin") in (False, 0)

    def test_unlocks_even_when_it_carries_no_model(self, mock_db):
        _mk_default_ai({})
        _retire_default_ai_agent()

        row = db.get_profile(RETIRED_DEFAULT_AI_ID) or {}
        assert row.get("is_builtin") in (False, 0)
        assert db.get_system_setting(TEXT_GENERATION_MODEL_KEY) is None

    def test_keeps_the_row_and_its_config(self, mock_db):
        """Sessions reference this agent, and it may have been edited into a
        real chat agent — retiring it must not delete or rewrite anything. Its
        config.model also still backs the legacy native subagent binding."""
        _mk_default_ai({"model": "opus", "memory": "mine"})
        _retire_default_ai_agent()

        row = db.get_profile(RETIRED_DEFAULT_AI_ID) or {}
        assert row.get("config", {}).get("model") == "opus"
        assert row.get("config", {}).get("memory") == "mine"

    def test_no_agent_is_a_no_op(self, mock_db):
        _retire_default_ai_agent()
        assert db.get_system_setting(TEXT_GENERATION_MODEL_KEY) is None


def _clear_agents():
    """A genuinely fresh install. conftest seeds one agent for every test, so
    the no-agents path has to be arranged explicitly."""
    for row in db.get_all_profiles() or []:
        db.delete_profile(row["id"])


class TestNoStarterAgentIsSeeded:
    """The app ships no agents. Startup must leave an empty install empty --
    the first agent is one the user creates, not one we guessed at."""

    def test_startup_leaves_a_fresh_install_with_no_agents(self, mock_db):
        _clear_agents()
        run_migrations()

        assert db.get_all_profiles() == []

    def test_startup_leaves_an_install_that_has_agents_alone(self, mock_db):
        _clear_agents()
        db.create_profile(
            profile_id="mine", name="Mine", description=None, config={}
        )
        run_migrations()

        assert {p["id"] for p in db.get_all_profiles()} == {"mine"}

    def test_retiring_the_old_builtin_does_not_add_a_replacement(self, mock_db):
        """An upgrading install keeps exactly the agent it already had."""
        _clear_agents()
        _mk_default_ai({"model": "opus"})
        run_migrations()

        assert {p["id"] for p in db.get_all_profiles()} == {RETIRED_DEFAULT_AI_ID}
