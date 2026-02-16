"""
Tests for connector service — business logic, config handling, CRUD operations.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.core.connector_service import (
    get_sensitive_fields,
    normalize_config,
    encrypt_config,
    decrypt_config,
    mask_config,
    get_connector_type_info,
    _get_list_fields,
)


# =============================================================================
# Sensitive Fields
# =============================================================================


class TestGetSensitiveFields:
    def test_telegram_sensitive_fields(self):
        assert get_sensitive_fields("telegram") == ["bot_token"]

    def test_discord_sensitive_fields(self):
        assert get_sensitive_fields("discord") == ["bot_token"]

    def test_whatsapp_sensitive_fields(self):
        assert get_sensitive_fields("whatsapp") == ["access_token"]

    def test_slack_sensitive_fields(self):
        assert get_sensitive_fields("slack") == ["bot_token", "app_token"]

    def test_unknown_type_returns_empty(self):
        assert get_sensitive_fields("nonexistent") == []

    def test_all_14_types_have_entries(self):
        types = [
            "telegram", "discord", "whatsapp", "webhook", "slack", "line",
            "signal", "imessage", "msteams", "bluebubbles", "spotify",
            "hue", "homeassistant", "gmail",
        ]
        for t in types:
            result = get_sensitive_fields(t)
            assert isinstance(result, list), f"No entry for type: {t}"


# =============================================================================
# Config Normalization
# =============================================================================


class TestNormalizeConfig:
    def test_string_to_list_conversion(self):
        config = {"allowed_chat_ids": "123, 456, 789"}
        result = normalize_config(config, "telegram")
        assert result["allowed_chat_ids"] == ["123", "456", "789"]

    def test_already_list_unchanged(self):
        config = {"allowed_chat_ids": ["123", "456"]}
        result = normalize_config(config, "telegram")
        assert result["allowed_chat_ids"] == ["123", "456"]

    def test_empty_string_ignored(self):
        config = {"allowed_chat_ids": ""}
        result = normalize_config(config, "telegram")
        assert result["allowed_chat_ids"] == ""

    def test_none_value_ignored(self):
        config = {"allowed_chat_ids": None}
        result = normalize_config(config, "telegram")
        assert result["allowed_chat_ids"] is None

    def test_no_list_fields_returns_unchanged(self):
        config = {"some_key": "some_value"}
        result = normalize_config(config, "nonexistent_type")
        assert result == config

    def test_comma_separated_split(self):
        config = {"guild_ids": "111,222,333"}
        result = normalize_config(config, "discord")
        assert result["guild_ids"] == ["111", "222", "333"]


# =============================================================================
# Config Encryption / Decryption / Masking
# =============================================================================


class TestEncryptConfig:
    def test_encrypt_sensitive_field(self, mock_encryption):
        config = {"bot_token": "my-secret-token"}
        result = encrypt_config(config, "telegram")
        assert result["bot_token"] == "encrypted:my-secret-token"

    def test_skip_already_encrypted(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"bot_token": "encrypted:already"}
            result = encrypt_config(config, "telegram")
            assert result["bot_token"] == "encrypted:already"

    def test_non_sensitive_fields_unchanged(self, mock_encryption):
        config = {"bot_token": "secret", "allowed_chat_ids": ["123"]}
        result = encrypt_config(config, "telegram")
        assert result["allowed_chat_ids"] == ["123"]


class TestDecryptConfig:
    def test_decrypt_sensitive_field(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"bot_token": "encrypted:my-token"}
            result = decrypt_config(config, "telegram")
            assert result["bot_token"] == "my-token"

    def test_plaintext_unchanged(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=False):
            config = {"bot_token": "plaintext-token"}
            result = decrypt_config(config, "telegram")
            assert result["bot_token"] == "plaintext-token"


class TestMaskConfig:
    def test_mask_long_value(self):
        config = {"bot_token": "1234567890abcdef"}
        result = mask_config(config, "telegram")
        assert result["bot_token"] == "****cdef"

    def test_mask_short_value(self):
        config = {"bot_token": "abc"}
        result = mask_config(config, "telegram")
        assert result["bot_token"] == "****"

    def test_non_sensitive_unchanged(self):
        config = {"allowed_chat_ids": ["123"]}
        result = mask_config(config, "telegram")
        assert result["allowed_chat_ids"] == ["123"]

    def test_mask_encrypted_value(self):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"bot_token": "enc:v1:base64data"}
            result = mask_config(config, "telegram")
            assert result["bot_token"] == "****"


# =============================================================================
# _get_list_fields (uses get_connector_type_info)
# =============================================================================


class TestGetListFields:
    def test_telegram_list_fields(self):
        fields = _get_list_fields("telegram")
        assert "allowed_chat_ids" in fields

    def test_discord_list_fields(self):
        fields = _get_list_fields("discord")
        assert "guild_ids" in fields
        assert "allowed_channel_ids" in fields

    def test_unknown_type_returns_empty(self):
        fields = _get_list_fields("nonexistent")
        assert fields == []


# =============================================================================
# get_connector_type_info
# =============================================================================


class TestGetConnectorTypeInfo:
    def test_returns_14_types(self):
        types = get_connector_type_info()
        assert len(types) == 14

    def test_all_types_have_required_fields(self):
        types = get_connector_type_info()
        for t in types:
            assert "type" in t
            assert "name" in t
            assert "description" in t
            assert "config_fields" in t

    def test_type_names_match_enum(self):
        types = get_connector_type_info()
        type_names = {t["type"] for t in types}
        expected = {
            "telegram", "discord", "whatsapp", "webhook", "slack", "line",
            "signal", "imessage", "msteams", "bluebubbles", "spotify",
            "hue", "homeassistant", "gmail",
        }
        assert type_names == expected

    def test_config_fields_have_key_and_label(self):
        types = get_connector_type_info()
        for t in types:
            for field in t["config_fields"]:
                assert "key" in field, f"Missing 'key' in {t['type']}.{field}"
                assert "label" in field, f"Missing 'label' in {t['type']}.{field}"


# =============================================================================
# CRUD Integration Tests
# =============================================================================


class TestCreateConnector:
    @pytest.mark.asyncio
    async def test_create_connector_basic(self, mock_db, mock_encryption):
        from app.core.connector_service import create_connector

        result = await create_connector(
            name="Test Bot",
            connector_type="telegram",
            config={"bot_token": "test-token-12345678"},
            events=["session.complete"],
        )

        assert result is not None
        assert result["name"] == "Test Bot"
        assert result["type"] == "telegram"
        # Config should be masked in response
        assert "****" in result["config"]["bot_token"]

    @pytest.mark.asyncio
    async def test_create_connector_invalid_events(self, mock_db, mock_encryption):
        from app.core.connector_service import create_connector

        with pytest.raises(ValueError, match="Invalid event types"):
            await create_connector(
                name="Test Bot",
                connector_type="telegram",
                config={"bot_token": "test-token"},
                events=["invalid.event"],
            )

    @pytest.mark.asyncio
    async def test_create_connector_with_api_user(self, mock_db, mock_encryption):
        from app.core.connector_service import create_connector
        from app.db import database

        # Create an API user first
        database.create_api_user(
            user_id="existing-user",
            name="Existing User",
            api_key_hash="fakehash",
        )

        result = await create_connector(
            name="Test Bot",
            connector_type="telegram",
            config={"bot_token": "test-token-12345678"},
            events=[],
            api_user_id="existing-user",
        )

        assert result["api_user_id"] == "existing-user"

    @pytest.mark.asyncio
    async def test_create_connector_invalid_api_user(self, mock_db, mock_encryption):
        from app.core.connector_service import create_connector

        with pytest.raises(ValueError, match="API user not found"):
            await create_connector(
                name="Test Bot",
                connector_type="telegram",
                config={"bot_token": "test-token"},
                events=[],
                api_user_id="nonexistent",
            )


class TestTestConnector:
    @pytest.mark.asyncio
    async def test_connector_not_found(self, mock_db):
        from app.core.connector_service import test_connector

        result = await test_connector("nonexistent")
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_response_time_ms_field_name(self, mock_db, mock_encryption):
        """Verify the field name matches the Pydantic model (response_time_ms, not elapsed_ms)."""
        from app.core.connector_service import test_connector
        from app.db import database

        # Create a connector in DB
        database.create_connector(
            connector_id="test-conn",
            connector_type="webhook",
            name="Test Webhook",
            config={"mode": "inbound", "inbound_path": "test"},
            events=[],
            api_user_id=None,
        )

        result = await test_connector("test-conn")
        # Should have response_time_ms, not elapsed_ms
        assert "response_time_ms" in result
        assert "elapsed_ms" not in result
