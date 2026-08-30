"""
DB + API round-trip tests for the API user's preconfigured run selection
(``model`` / ``effort``) and the create/update endpoint threading.
"""

import pytest


class TestApiUserRunConfigPersistence:
    def test_create_and_read_back(self, mock_db):
        from app.db import database

        database.create_api_user(
            user_id="u-model-1",
            name="Discord Bot",
            api_key_hash="hash",
            model="opus",
            effort="high",
        )
        row = database.get_api_user("u-model-1")
        assert row["model"] == "opus"
        assert row["effort"] == "high"

    def test_create_defaults_are_null(self, mock_db):
        from app.db import database

        database.create_api_user(
            user_id="u-model-2",
            name="Plain Bot",
            api_key_hash="hash",
        )
        row = database.get_api_user("u-model-2")
        assert row["model"] is None
        assert row["effort"] is None

    def test_update_round_trip(self, mock_db):
        from app.db import database

        database.create_api_user(
            user_id="u-model-3",
            name="Updatable Bot",
            api_key_hash="hash",
        )
        database.update_api_user("u-model-3", model="fable", effort="max")
        row = database.get_api_user("u-model-3")
        assert row["model"] == "fable"
        assert row["effort"] == "max"

        # Clearing works too (NULL is a valid preconfigured value — fall back).
        database.update_api_user("u-model-3", model=None, effort=None)
        row = database.get_api_user("u-model-3")
        assert row["model"] is None
        assert row["effort"] is None


class TestApiUserRunConfigValidation:
    def test_create_rejects_bad_effort(self):
        from pydantic import ValidationError
        from app.core.models import ApiUserCreate

        with pytest.raises(ValidationError):
            ApiUserCreate(name="Bad Effort", effort="ultra")

    def test_update_rejects_bad_effort(self):
        from pydantic import ValidationError
        from app.core.models import ApiUserUpdate

        with pytest.raises(ValidationError):
            ApiUserUpdate(effort="ultra")

    def test_update_accepts_valid_effort(self):
        from app.core.models import ApiUserUpdate

        u = ApiUserUpdate(effort="xhigh", model="sonnet")
        assert u.effort == "xhigh"
        assert u.model == "sonnet"
