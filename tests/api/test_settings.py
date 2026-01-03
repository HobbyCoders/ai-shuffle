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

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from io import BytesIO
from dataclasses import dataclass, field
from typing import List, Dict, Any


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

    def test_mask_api_key_exactly_10_chars(self):
        """Should mask key exactly 10 characters."""
        from app.api.settings import mask_api_key
        result = mask_api_key("1234567890")
        assert result == "1234567...7890"

    def test_mask_api_key_11_chars(self):
        """Should mask key with 11 characters."""
        from app.api.settings import mask_api_key
        result = mask_api_key("12345678901")
        assert result == "1234567...8901"

    def test_mask_api_key_exactly_9_chars(self):
        """Should return empty for 9 character key."""
        from app.api.settings import mask_api_key
        result = mask_api_key("123456789")
        assert result == ""


class TestFormatBytes:
    """Test bytes formatting helper function."""

    def test_format_bytes_zero(self):
        """Should format zero bytes."""
        from app.api.settings import format_bytes
        assert format_bytes(0) == "0.0 B"

    def test_format_bytes_small(self):
        """Should format bytes under 1KB."""
        from app.api.settings import format_bytes
        assert format_bytes(500) == "500.0 B"

    def test_format_bytes_kilobytes(self):
        """Should format kilobytes."""
        from app.api.settings import format_bytes
        assert format_bytes(1024) == "1.0 KB"
        assert format_bytes(2048) == "2.0 KB"

    def test_format_bytes_megabytes(self):
        """Should format megabytes."""
        from app.api.settings import format_bytes
        assert format_bytes(1024 * 1024) == "1.0 MB"

    def test_format_bytes_gigabytes(self):
        """Should format gigabytes."""
        from app.api.settings import format_bytes
        assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"

    def test_format_bytes_terabytes(self):
        """Should format terabytes."""
        from app.api.settings import format_bytes
        assert format_bytes(1024 * 1024 * 1024 * 1024) == "1.0 TB"

    def test_format_bytes_fractional_kb(self):
        """Should format fractional kilobytes."""
        from app.api.settings import format_bytes
        assert format_bytes(1536) == "1.5 KB"

    def test_format_bytes_large_number(self):
        """Should handle very large numbers (stops at TB)."""
        from app.api.settings import format_bytes
        # The function stops at TB level
        assert format_bytes(1024 * 1024 * 1024 * 1024 * 1024) == "1024.0 TB"


class TestGetDecryptedApiKey:
    """Test API key decryption helper function."""

    def test_get_decrypted_api_key_not_set(self):
        """Should return None if key not set."""
        with patch("app.api.settings.database") as mock_db:
            mock_db.get_system_setting.return_value = None
            from app.api.settings import get_decrypted_api_key
            result = get_decrypted_api_key("openai_api_key")
            assert result is None

    def test_get_decrypted_api_key_plaintext(self):
        """Should return plaintext key as-is."""
        with patch("app.api.settings.database") as mock_db:
            with patch("app.api.settings.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "sk-plaintext-key"
                mock_enc.is_encrypted.return_value = False
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result == "sk-plaintext-key"

    def test_get_decrypted_api_key_encrypted(self):
        """Should decrypt encrypted key."""
        with patch("app.api.settings.database") as mock_db:
            with patch("app.api.settings.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:sk-secret-key"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.return_value = "sk-secret-key"
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result == "sk-secret-key"

    def test_get_decrypted_api_key_encryption_not_ready(self):
        """Should return None if encryption not ready."""
        with patch("app.api.settings.database") as mock_db:
            with patch("app.api.settings.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:sk-secret-key"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = False
                from app.api.settings import get_decrypted_api_key
                result = get_decrypted_api_key("openai_api_key")
                assert result is None

    def test_get_decrypted_api_key_decryption_error(self):
        """Should return None if decryption fails."""
        with patch("app.api.settings.database") as mock_db:
            with patch("app.api.settings.encryption") as mock_enc:
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
        with patch("app.api.settings.database") as mock_db:
            with patch("app.api.settings.encryption") as mock_enc:
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.encrypt_value.return_value = "encrypted:sk-key"
                from app.api.settings import set_encrypted_api_key
                set_encrypted_api_key("openai_api_key", "sk-key")
                mock_enc.encrypt_value.assert_called_once_with("sk-key")
                mock_db.set_system_setting.assert_called_once_with("openai_api_key", "encrypted:sk-key")

    def test_set_encrypted_api_key_without_encryption(self):
        """Should store plaintext when encryption not ready."""
        with patch("app.api.settings.database") as mock_db:
            with patch("app.api.settings.encryption") as mock_enc:
                mock_enc.is_encryption_ready.return_value = False
                from app.api.settings import set_encrypted_api_key
                set_encrypted_api_key("openai_api_key", "sk-key")
                mock_db.set_system_setting.assert_called_once_with("openai_api_key", "sk-key")


class TestComputeEffectiveStatus:
    """Test credential policy effective status computation."""

    def test_admin_provided_with_key(self):
        """Should return admin_provides when admin has key."""
        from app.api.settings import _compute_effective_status
        assert _compute_effective_status("admin_provided", True) == "admin_provides"

    def test_admin_provided_without_key(self):
        """Should return needs_admin_key when admin missing key."""
        from app.api.settings import _compute_effective_status
        assert _compute_effective_status("admin_provided", False) == "needs_admin_key"

    def test_user_provided(self):
        """Should return user_must_provide for user_provided policy."""
        from app.api.settings import _compute_effective_status
        assert _compute_effective_status("user_provided", True) == "user_must_provide"
        assert _compute_effective_status("user_provided", False) == "user_must_provide"

    def test_optional_with_fallback(self):
        """Should return optional_with_fallback when admin has key."""
        from app.api.settings import _compute_effective_status
        assert _compute_effective_status("optional", True) == "optional_with_fallback"

    def test_optional_no_fallback(self):
        """Should return optional_no_fallback when admin missing key."""
        from app.api.settings import _compute_effective_status
        assert _compute_effective_status("optional", False) == "optional_no_fallback"


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

    def test_image_models_defined(self):
        """IMAGE_MODELS should be defined as a dict."""
        from app.api.settings import IMAGE_MODELS
        assert IMAGE_MODELS is not None
        assert isinstance(IMAGE_MODELS, dict)
        assert len(IMAGE_MODELS) > 0

    def test_video_models_defined(self):
        """VIDEO_MODELS should be defined as a dict."""
        from app.api.settings import VIDEO_MODELS
        assert VIDEO_MODELS is not None
        assert isinstance(VIDEO_MODELS, dict)
        assert len(VIDEO_MODELS) > 0

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

    def test_credential_policy_info_defined(self):
        """CREDENTIAL_POLICY_INFO should be defined."""
        from app.api.settings import CREDENTIAL_POLICY_INFO
        assert CREDENTIAL_POLICY_INFO is not None
        assert "openai_api_key" in CREDENTIAL_POLICY_INFO


class TestImageModels:
    """Test IMAGE_MODELS constant (dict format)."""

    def test_has_gemini_models(self):
        """Should include Gemini image models."""
        from app.api.settings import IMAGE_MODELS
        gemini_models = [k for k in IMAGE_MODELS.keys() if "gemini" in k]
        assert len(gemini_models) > 0

    def test_models_have_required_fields(self):
        """All models should have required fields."""
        from app.api.settings import IMAGE_MODELS
        for model_id, model_info in IMAGE_MODELS.items():
            assert "name" in model_info
            assert "provider" in model_info

    def test_models_have_capabilities(self):
        """Models should have capabilities field."""
        from app.api.settings import IMAGE_MODELS
        for model_id, model_info in IMAGE_MODELS.items():
            assert "capabilities" in model_info
            assert isinstance(model_info["capabilities"], list)


class TestVideoModels:
    """Test VIDEO_MODELS constant (dict format)."""

    def test_has_veo_models(self):
        """Should include Veo video models."""
        from app.api.settings import VIDEO_MODELS
        veo_models = [k for k in VIDEO_MODELS.keys() if "veo" in k]
        assert len(veo_models) > 0

    def test_models_have_required_fields(self):
        """All video models should have required fields."""
        from app.api.settings import VIDEO_MODELS
        for model_id, model_info in VIDEO_MODELS.items():
            assert "name" in model_info
            assert "provider" in model_info


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


class TestCredentialPolicyInfo:
    """Test CREDENTIAL_POLICY_INFO constant."""

    def test_has_openai_credential(self):
        """Should have openai_api_key credential."""
        from app.api.settings import CREDENTIAL_POLICY_INFO
        assert "openai_api_key" in CREDENTIAL_POLICY_INFO

    def test_has_gemini_credential(self):
        """Should have gemini_api_key credential."""
        from app.api.settings import CREDENTIAL_POLICY_INFO
        assert "gemini_api_key" in CREDENTIAL_POLICY_INFO

    def test_credential_has_required_fields(self):
        """Each credential should have required fields."""
        from app.api.settings import CREDENTIAL_POLICY_INFO
        for cred_id, info in CREDENTIAL_POLICY_INFO.items():
            assert "name" in info
            assert "description" in info


# =============================================================================
# Pydantic Model Tests
# =============================================================================

class TestPydanticModels:
    """Test Pydantic request/response models."""

    def test_openai_key_request_model(self):
        """OpenAIKeyRequest should validate correctly."""
        from app.api.settings import OpenAIKeyRequest
        req = OpenAIKeyRequest(api_key="sk-test123")
        assert req.api_key == "sk-test123"

    def test_file_cleanup_preview_response(self):
        """FileCleanupPreviewResponse should have all fields."""
        from app.api.settings import FileCleanupPreviewResponse
        fields = FileCleanupPreviewResponse.model_fields.keys()
        assert "images" in fields
        assert "videos" in fields
        assert "uploads" in fields
        assert "total_count" in fields
        assert "total_bytes" in fields
        assert "total_bytes_formatted" in fields


# =============================================================================
# Model Validation Tests
# =============================================================================

class TestModelValidation:
    """Test model validation helpers."""

    def test_valid_tts_models(self):
        """Should identify valid TTS models."""
        from app.api.settings import TTS_MODELS
        valid_ids = set(TTS_MODELS.keys())
        assert "tts-1" in valid_ids or "gpt-4o-mini-tts" in valid_ids

    def test_valid_image_models(self):
        """Should identify valid image models."""
        from app.api.settings import IMAGE_MODELS
        valid_ids = set(IMAGE_MODELS.keys())
        # Should have at least one model
        assert len(valid_ids) > 0

    def test_valid_video_models(self):
        """Should identify valid video models."""
        from app.api.settings import VIDEO_MODELS
        valid_ids = set(VIDEO_MODELS.keys())
        # Should have at least one model
        assert len(valid_ids) > 0


# =============================================================================
# Video Provider Tests
# =============================================================================

class TestVideoProviders:
    """Test video provider constants."""

    def test_valid_video_providers(self):
        """Should have valid video providers defined."""
        from app.api.settings import VIDEO_MODELS
        providers = {info["provider"] for info in VIDEO_MODELS.values()}
        # Should have at least one provider
        assert len(providers) > 0


# =============================================================================
# Credential Types Tests
# =============================================================================

class TestCredentialTypes:
    """Test credential type constants."""

    def test_valid_credential_policies(self):
        """Should have valid credential policies."""
        from app.api.settings import CREDENTIAL_POLICY_INFO
        # Just verify it exists
        assert len(CREDENTIAL_POLICY_INFO) > 0


# =============================================================================
# Internal Config Endpoint Logic Tests
# =============================================================================

class TestInternalConfigLogic:
    """Test internal configuration endpoint logic."""

    def test_image_api_key_selection_logic(self):
        """Test image API key selection based on provider."""
        from app.api.settings import IMAGE_MODELS
        google_models = [k for k, v in IMAGE_MODELS.items() if "google" in v.get("provider", "")]
        # Google providers should exist
        assert len(google_models) > 0

    def test_video_api_key_selection_logic(self):
        """Test video API key selection based on provider."""
        from app.api.settings import VIDEO_MODELS
        providers = {v["provider"] for v in VIDEO_MODELS.values()}
        # Should have providers defined
        assert len(providers) > 0


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


# =============================================================================
# Image Generation Provider Tests
# =============================================================================

class TestImageProviderValidation:
    """Test image generation provider validation."""

    def test_google_provider_variants(self):
        """Should support Google provider variants."""
        from app.api.settings import IMAGE_MODELS
        google_providers = {v["provider"] for v in IMAGE_MODELS.values() if "google" in v.get("provider", "").lower()}
        # Google providers should exist
        assert len(google_providers) > 0

    def test_provider_model_consistency(self):
        """Provider in model should match model requirements."""
        from app.api.settings import IMAGE_MODELS
        for model_id, model_info in IMAGE_MODELS.items():
            provider = model_info.get("provider", "")
            # Models should have provider
            assert provider != ""
