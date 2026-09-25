"""
Tests for connector service — business logic, config handling, CRUD operations.
"""

import types

import pytest

pytestmark = pytest.mark.usefixtures("bundled_connector_catalog")
from unittest.mock import patch, MagicMock

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
        assert get_sensitive_fields("whatsapp") == ["access_token", "app_secret"]

    def test_slack_sensitive_fields(self):
        assert get_sensitive_fields("slack") == ["bot_token", "app_token"]

    def test_unknown_type_returns_empty(self):
        assert get_sensitive_fields("nonexistent") == []

    def test_all_15_types_have_entries(self):
        types = [
            "telegram", "discord", "whatsapp", "webhook", "slack", "line",
            "signal", "imessage", "msteams", "bluebubbles", "spotify",
            "hue", "homeassistant", "gmail", "plaid",
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
    def test_discord_bot_display_name_preserves_connection_identity(self):
        discord = next(t for t in get_connector_type_info() if t["type"] == "discord")
        assert discord["name"] == "Discord Bot"
        assert discord["plugin_id"] == "discord@shuffle-connectors"
        assert any(
            field["key"] == "bot_token" and field["required"]
            for field in discord["config_fields"]
        )

    def test_returns_15_types(self):
        types = get_connector_type_info()
        assert len(types) == 15

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
            "hue", "homeassistant", "gmail", "plaid",
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


# =============================================================================
# Discord Guided-Setup Schema Metadata
# =============================================================================


class TestDiscordSchemaMetadata:
    def _discord_fields(self):
        types_ = get_connector_type_info()
        discord = next(t for t in types_ if t["type"] == "discord")
        return {f["key"]: f for f in discord["config_fields"]}

    def test_guild_ids_has_picker(self):
        assert self._discord_fields()["guild_ids"]["picker"] == "discord_guilds"

    def test_channel_ids_has_picker(self):
        assert self._discord_fields()["allowed_channel_ids"]["picker"] == "discord_channels"

    def test_command_prefix_has_presets(self):
        assert self._discord_fields()["command_prefix"]["presets"] == ["!", "?", "."]

    def test_other_types_have_no_picker(self):
        """Additive keys must not leak onto other connector types."""
        types_ = get_connector_type_info()
        for t in types_:
            if t["type"] == "discord":
                continue
            for f in t["config_fields"]:
                assert "picker" not in f


# =============================================================================
# discover_resources (Discord discovery)
# =============================================================================


class _FakeTextChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name


class _FakeVoiceChannel:
    """A non-text channel that discovery must filter out."""
    def __init__(self, cid, name):
        self.id = cid
        self.name = name


def _make_fake_discord(client_factory):
    """Build a stand-in for the ``discord`` module used by discord_connector.

    Exception classes are plain ``Exception`` subclasses so the tests can raise
    them and the ``except`` clauses in ``discover_resources`` still catch them.
    """
    class FakeLoginFailure(Exception):
        pass

    class FakeForbidden(Exception):
        pass

    class FakeHTTPException(Exception):
        pass

    intents = MagicMock()
    intents.none.return_value = MagicMock()

    return types.SimpleNamespace(
        Client=client_factory,
        Intents=intents,
        LoginFailure=FakeLoginFailure,
        Forbidden=FakeForbidden,
        HTTPException=FakeHTTPException,
        TextChannel=_FakeTextChannel,
    ), FakeLoginFailure, FakeForbidden, FakeHTTPException


class TestDiscoverResources:
    @pytest.mark.asyncio
    async def test_discover_happy_path(self):
        from app.core.connectors import discord_connector

        closed = {"value": False}

        class FakeClient:
            def __init__(self, intents=None):
                self.user = types.SimpleNamespace(id=111, name="TestBot")

            async def login(self, token):
                assert token == "tok"

            async def application_info(self):
                return types.SimpleNamespace(id=222)

            async def fetch_guilds(self, *, limit=200):
                for g in [types.SimpleNamespace(id=333, name="Server A")]:
                    yield g

            async def fetch_guild(self, gid):
                async def fetch_channels():
                    return [
                        _FakeTextChannel(444, "general"),
                        _FakeVoiceChannel(445, "Voice"),  # filtered out
                    ]
                return types.SimpleNamespace(fetch_channels=fetch_channels)

            async def close(self):
                closed["value"] = True

        fake_discord, *_ = _make_fake_discord(FakeClient)
        with patch.object(discord_connector, "discord", fake_discord), \
             patch.object(discord_connector, "DISCORD_AVAILABLE", True):
            result = await discord_connector.discover_resources("tok")

        assert result == {
            "bot": {"id": "111", "name": "TestBot"},
            "application_id": "222",
            "guilds": [
                {
                    "id": "333",
                    "name": "Server A",
                    "channels": [{"id": "444", "name": "general"}],
                    "error": None,
                }
            ],
        }
        # All IDs are strings (snowflakes overflow JS numbers).
        assert isinstance(result["bot"]["id"], str)
        assert isinstance(result["guilds"][0]["channels"][0]["id"], str)
        # Client is always closed.
        assert closed["value"] is True

    @pytest.mark.asyncio
    async def test_discover_invalid_token_raises_valueerror(self):
        from app.core.connectors import discord_connector

        holder = {}

        class FakeClient:
            def __init__(self, intents=None):
                self.user = None

            async def login(self, token):
                raise holder["LoginFailure"]("bad")

            async def close(self):
                holder["closed"] = True

        fake_discord, LoginFailure, _, _ = _make_fake_discord(FakeClient)
        holder["LoginFailure"] = LoginFailure
        with patch.object(discord_connector, "discord", fake_discord), \
             patch.object(discord_connector, "DISCORD_AVAILABLE", True):
            with pytest.raises(ValueError, match="Invalid bot token"):
                await discord_connector.discover_resources("tok")
        assert holder.get("closed") is True

    @pytest.mark.asyncio
    async def test_discover_forbidden_guild_included_with_error(self):
        from app.core.connectors import discord_connector

        holder = {}

        class FakeClient:
            def __init__(self, intents=None):
                self.user = types.SimpleNamespace(id=1, name="B")

            async def login(self, token):
                pass

            async def application_info(self):
                return types.SimpleNamespace(id=2)

            async def fetch_guilds(self, *, limit=200):
                for g in [types.SimpleNamespace(id=9, name="Locked")]:
                    yield g

            async def fetch_guild(self, gid):
                raise holder["Forbidden"]("no access")

            async def close(self):
                pass

        fake_discord, _, Forbidden, _ = _make_fake_discord(FakeClient)
        holder["Forbidden"] = Forbidden
        with patch.object(discord_connector, "discord", fake_discord), \
             patch.object(discord_connector, "DISCORD_AVAILABLE", True):
            result = await discord_connector.discover_resources("tok")

        guild = result["guilds"][0]
        assert guild["channels"] == []
        assert guild["error"] == "no access"

    @pytest.mark.asyncio
    async def test_discover_raises_when_sdk_missing(self):
        from app.core.connectors import discord_connector

        with patch.object(discord_connector, "DISCORD_AVAILABLE", False):
            with pytest.raises(RuntimeError, match="discord.py not installed"):
                await discord_connector.discover_resources("tok")
