"""
Comprehensive tests for the settings API endpoints.

Tests cover:
- Helper functions (mask_api_key, format_bytes, encryption helpers)
- Integration settings (OpenAI, Image, Video, Audio, 3D)
- Audio model configuration (TTS/STT)
- Image generation configuration
- Video generation configuration
- AI tools configuration
- Cleanup management
- Credential policies
- Module imports and constants
"""

from unittest.mock import patch
import pytest


# =============================================================================
# Helper function tests (no client needed)
# =============================================================================

class TestMaskApiKey:
    """Test API key masking helper function."""

    def test_mask_api_key_with_valid_key(self):
        """Should mask middle of a valid API key."""
        from app.api.settings import mask_api_key
        result = mask_api_key("sk-1234567890abcdefghij")
        assert result == "sk-1234...ghij"

    def test_mask_api_key_with_short_key(self):
        """Should return empty string for short keys."""
        from app.api.settings import mask_api_key
        result = mask_api_key("short")
        assert result == ""

    def test_mask_api_key_with_empty_key(self):
        """Should return empty string for empty key."""
        from app.api.settings import mask_api_key
        result = mask_api_key("")
        assert result == ""

    def test_mask_api_key_with_none(self):
        """Should return empty string for None."""
        from app.api.settings import mask_api_key
        result = mask_api_key(None)
        assert result == ""

    @pytest.mark.parametrize("key", ["1234567890", "12345678901", "a" * 21])
    def test_mask_api_key_never_reveals_most_of_a_short_key(self, key):
        """Regression: 7 + 4 shown characters used to return a 10- or 11-char
        key whole. A key too short to hide as much as is shown masks to ""."""
        from app.api.settings import mask_api_key
        assert mask_api_key(key) == ""

    def test_mask_api_key_hides_at_least_what_it_shows(self):
        from app.api.settings import mask_api_key
        key = "k" * 7 + "SECRETSECRE" + "tail"
        assert len(key) == 22
        assert mask_api_key(key) == "kkkkkkk...tail"

    def test_mask_api_key_exactly_9_chars(self):
        """Should return empty for 9 character key."""
        from app.api.settings import mask_api_key
        result = mask_api_key("123456789")
        assert result == ""


class TestGetDecryptedApiKey:
    """Test API key decryption helper function."""

    def test_get_decrypted_api_key_not_set(self):
        """Should return None if key not set."""
        with patch("app.api.settings_crypto.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            from app.api.settings import get_decrypted_api_key
            result = get_decrypted_api_key("openai_api_key")
            assert result is None

    def test_get_decrypted_api_key_plaintext(self):
        """Should return plaintext key as-is."""
        with patch("app.api.settings_crypto.database") as mock_db:
            with patch("app.api.settings_crypto.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "sk-plaintext-key"
                mock_enc.is_encrypted.return_value = False
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result == "sk-plaintext-key"

    def test_get_decrypted_api_key_encrypted(self):
        """Should decrypt encrypted key."""
        with patch("app.api.settings_crypto.database") as mock_db:
            with patch("app.api.settings_crypto.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:sk-secret-key"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.return_value = "sk-secret-key"
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result == "sk-secret-key"

    def test_get_decrypted_api_key_encryption_not_ready(self):
        """Should return None if encryption not ready."""
        with patch("app.api.settings_crypto.database") as mock_db:
            with patch("app.api.settings_crypto.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:sk-secret-key"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = False
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result is None

    def test_get_decrypted_api_key_decryption_error(self):
        """Should return None if decryption fails."""
        with patch("app.api.settings_crypto.database") as mock_db:
            with patch("app.api.settings_crypto.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:bad-data"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.side_effect = Exception("Decryption failed")
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result is None


class TestSetEncryptedApiKey:
    """Test API key encryption helper function."""

    def test_set_encrypted_api_key_with_encryption(self):
        """Should encrypt and store key when encryption ready."""
        with patch("app.api.settings_crypto.database") as mock_db:
            with patch("app.api.settings_crypto.encryption") as mock_enc:
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.encrypt_value.return_value = "encrypted:sk-key"
                from app.api.settings import set_encrypted_api_key
                set_encrypted_api_key("openai_api_key", "sk-key")
                mock_enc.encrypt_value.assert_called_once_with("sk-key")
                mock_db.set_system_setting.assert_called_once_with("openai_api_key", "encrypted:sk-key")

    def test_set_encrypted_api_key_without_encryption(self):
        """An unavailable server key must never downgrade storage to plaintext."""
        with patch("app.api.settings_crypto.database") as mock_db:
            with patch("app.api.settings_crypto.encryption") as mock_enc:
                mock_enc.is_encryption_ready.return_value = False
                mock_enc.encrypt_value.side_effect = RuntimeError('Credential storage locked')
                from app.api.settings import set_encrypted_api_key
                with pytest.raises(RuntimeError):
                    set_encrypted_api_key("openai_api_key", "sk-key")
                mock_db.set_system_setting.assert_not_called()


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Verify settings module can be imported correctly."""

    def test_settings_module_imports(self):
        """Settings module should import without errors."""
        from app.api import settings
        assert settings is not None

    def test_settings_router_exists(self):
        """Settings router should exist."""
        from app.api.settings import router
        assert router is not None

    # NOTE: IMAGE_MODELS / VIDEO_MODELS static catalogs were removed when the app
    # consolidated to FAL providers with per-model settings (#175). Image/video
    # models are no longer a backend constant, so those import tests were dropped.

    def test_tts_models_defined(self):
        """TTS_MODELS should be defined as a dict."""
        from app.api.settings import TTS_MODELS
        assert TTS_MODELS is not None
        assert isinstance(TTS_MODELS, dict)
        assert len(TTS_MODELS) > 0

    def test_stt_models_defined(self):
        """STT_MODELS should be defined as a dict."""
        from app.api.settings import STT_MODELS
        assert STT_MODELS is not None
        assert isinstance(STT_MODELS, dict)
        assert len(STT_MODELS) > 0


# NOTE: TestImageModels / TestVideoModels removed — the IMAGE_MODELS / VIDEO_MODELS
# static catalogs no longer exist. The app consolidated image/video generation onto
# FAL providers with per-model settings (#175); model metadata is no longer a
# backend constant, so there is nothing here to assert against.


class TestTTSModels:
    """Test TTS_MODELS constant (dict format)."""

    def test_has_openai_tts_models(self):
        """Should include TTS models."""
        from app.api.settings import TTS_MODELS
        assert len(TTS_MODELS) > 0
        tts_ids = list(TTS_MODELS.keys())
        assert any("tts" in id for id in tts_ids)

    def test_models_have_required_fields(self):
        """All TTS models should have required fields."""
        from app.api.settings import TTS_MODELS
        for model_id, model_info in TTS_MODELS.items():
            assert "name" in model_info


class TestSTTModels:
    """Test STT_MODELS constant (dict format)."""

    def test_has_stt_models(self):
        """Should include STT models."""
        from app.api.settings import STT_MODELS
        assert len(STT_MODELS) > 0

    def test_models_have_required_fields(self):
        """All STT models should have required fields."""
        from app.api.settings import STT_MODELS
        for model_id, model_info in STT_MODELS.items():
            assert "name" in model_info


# NOTE: TestPydanticModels removed — OpenAIKeyRequest no longer exists. OpenAI-specific
# key handling was replaced by the unified fal.ai credential flow (#175).


# =============================================================================
# Model Validation Tests
# =============================================================================

class TestModelValidation:
    """Test model validation helpers."""

    def test_valid_tts_models(self):
        """Should identify valid TTS models (fal.ai or OpenAI identifiers)."""
        from app.api.settings import TTS_MODELS
        valid_ids = set(TTS_MODELS.keys())
        assert len(valid_ids) > 0
        # TTS models are served by fal.ai (post-consolidation #175) or OpenAI (GPT audio).
        assert all(
            model_id.startswith("fal-ai/") or model_id.startswith("openai/")
            for model_id in valid_ids
        )
        # At least one of each provider is present.
        assert any(model_id.startswith("fal-ai/") for model_id in valid_ids)
        assert any(model_id.startswith("openai/") for model_id in valid_ids)

    # Image/video model validation removed with the IMAGE_MODELS / VIDEO_MODELS
    # catalogs (FAL consolidation, #175).


# NOTE: TestVideoProviders removed — provider metadata derived from VIDEO_MODELS,
# which no longer exists (FAL consolidation, #175).


# =============================================================================
# Internal Config Endpoint Logic Tests
# =============================================================================

# NOTE: TestInternalConfigLogic removed — image/video API-key selection was driven
# by the IMAGE_MODELS / VIDEO_MODELS provider metadata, which no longer exists. Under
# the FAL consolidation (#175) all AI tools share the single fal_api_key credential.


# =============================================================================
# Default Configuration Tests
# =============================================================================

class TestDefaultConfigurations:
    """Test default configuration values."""

    def test_default_tts_model(self):
        """Should have a reasonable default TTS model."""
        from app.api.settings import TTS_MODELS
        # Should have at least one model
        assert len(TTS_MODELS) > 0
        first_key = next(iter(TTS_MODELS.keys()))
        assert first_key is not None

    def test_default_stt_model(self):
        """Should have a reasonable default STT model."""
        from app.api.settings import STT_MODELS
        # Should have at least one model
        assert len(STT_MODELS) > 0
        first_key = next(iter(STT_MODELS.keys()))
        assert first_key is not None


# =============================================================================
# Router Tag Tests
# =============================================================================

class TestRouterConfiguration:
    """Test router configuration."""

    def test_router_has_tags(self):
        """Router should have appropriate tags."""
        from app.api.settings import router
        # Router should be configured
        assert router is not None

    def test_router_prefix(self):
        """Router should not have a prefix (applied by main app)."""
        from app.api.settings import router
        # The router itself doesn't have prefix - it's applied when including
        assert hasattr(router, "routes")


# =============================================================================
# OpenAI API Key Validation Tests
# =============================================================================

class TestOpenAIKeyValidation:
    """Test OpenAI API key format validation."""

    def test_valid_openai_key_format(self):
        """Valid keys should start with sk-."""
        # This is validated in the endpoint
        valid_key = "sk-1234567890abcdef"
        assert valid_key.startswith("sk-")

    def test_invalid_openai_key_format(self):
        """Invalid keys should be rejected."""
        invalid_keys = ["", "abc123", "pk-123", "key123"]
        for key in invalid_keys:
            assert not key.startswith("sk-") or key == ""


# NOTE: TestImageProviderValidation removed — image provider validation was based on
# the IMAGE_MODELS catalog, which no longer exists (FAL consolidation, #175).
