"""
Unit tests for AI tools registry module.

Tests cover:
- AI_TOOLS dictionary structure and contents
- PROVIDER_KEY_MAP configuration
- AIToolsConfig dynamic model generation
- Helper functions: get_tool_ids, get_tools_by_category, get_categories
- Edge cases and validation
"""

import pytest
from typing import Dict, Any
from pydantic import BaseModel

# Import the module under test
from app.core import ai_tools
from app.core.ai_tools import (
    AI_TOOLS,
    PROVIDER_KEY_MAP,
    AIToolsConfig,
    get_tool_ids,
    get_tools_by_category,
    get_categories,
    _create_ai_tools_config_class,
)


class TestAIToolsRegistry:
    """Test the AI_TOOLS dictionary structure and contents."""

    def test_ai_tools_is_dict(self):
        """AI_TOOLS should be a dictionary."""
        assert isinstance(AI_TOOLS, dict)

    def test_ai_tools_not_empty(self):
        """AI_TOOLS should contain tool definitions."""
        assert len(AI_TOOLS) > 0

    def test_all_tools_have_required_fields(self):
        """Each tool should have name, description, category, and providers."""
        required_fields = {"name", "description", "category", "providers"}

        for tool_id, tool_info in AI_TOOLS.items():
            assert isinstance(tool_info, dict), f"Tool {tool_id} should be a dict"
            missing_fields = required_fields - set(tool_info.keys())
            assert not missing_fields, f"Tool {tool_id} missing fields: {missing_fields}"

    def test_tool_names_are_strings(self):
        """Tool names should be non-empty strings."""
        for tool_id, tool_info in AI_TOOLS.items():
            assert isinstance(tool_info["name"], str), f"Tool {tool_id} name should be string"
            assert len(tool_info["name"]) > 0, f"Tool {tool_id} name should not be empty"

    def test_tool_descriptions_are_strings(self):
        """Tool descriptions should be non-empty strings."""
        for tool_id, tool_info in AI_TOOLS.items():
            assert isinstance(tool_info["description"], str), f"Tool {tool_id} description should be string"
            assert len(tool_info["description"]) > 0, f"Tool {tool_id} description should not be empty"

    def test_tool_categories_are_strings(self):
        """Tool categories should be non-empty strings."""
        for tool_id, tool_info in AI_TOOLS.items():
            assert isinstance(tool_info["category"], str), f"Tool {tool_id} category should be string"
            assert len(tool_info["category"]) > 0, f"Tool {tool_id} category should not be empty"

    def test_tool_providers_are_lists(self):
        """Tool providers should be non-empty lists of strings."""
        for tool_id, tool_info in AI_TOOLS.items():
            providers = tool_info["providers"]
            assert isinstance(providers, list), f"Tool {tool_id} providers should be a list"
            assert len(providers) > 0, f"Tool {tool_id} should have at least one provider"
            for provider in providers:
                assert isinstance(provider, str), f"Provider in {tool_id} should be a string"

    def test_expected_tool_categories_exist(self):
        """Verify expected categories are present."""
        categories = get_categories()
        expected_categories = {"image", "video", "3d"}
        assert expected_categories.issubset(set(categories)), f"Missing expected categories"

    def test_image_generation_tool_exists(self):
        """Image generation tool should be defined."""
        assert "image_generation" in AI_TOOLS
        tool = AI_TOOLS["image_generation"]
        assert tool["category"] == "image"
        assert "google-gemini" in tool["providers"]

    def test_video_generation_tool_exists(self):
        """Video generation tool should be defined."""
        assert "video_generation" in AI_TOOLS
        tool = AI_TOOLS["video_generation"]
        assert tool["category"] == "video"
        assert "google-veo" in tool["providers"]

    def test_3d_tools_exist(self):
        """3D tools should be defined."""
        expected_3d_tools = {"text_to_3d", "image_to_3d", "retexture_3d", "rig_3d", "animate_3d"}
        actual_3d_tools = {tool_id for tool_id, info in AI_TOOLS.items() if info["category"] == "3d"}
        assert expected_3d_tools == actual_3d_tools


class TestProviderKeyMap:
    """Test the PROVIDER_KEY_MAP configuration."""

    def test_provider_key_map_is_dict(self):
        """PROVIDER_KEY_MAP should be a dictionary."""
        assert isinstance(PROVIDER_KEY_MAP, dict)

    def test_provider_key_map_not_empty(self):
        """PROVIDER_KEY_MAP should contain mappings."""
        assert len(PROVIDER_KEY_MAP) > 0

    def test_all_values_are_api_key_strings(self):
        """All values should be API key field names."""
        valid_key_types = {"image_api_key", "openai_api_key", "meshy_api_key"}

        for provider, key_field in PROVIDER_KEY_MAP.items():
            assert isinstance(key_field, str), f"Key field for {provider} should be string"
            assert key_field in valid_key_types, f"Unknown key type {key_field} for {provider}"

    def test_google_providers_use_image_api_key(self):
        """Google providers should use image_api_key."""
        google_providers = ["google-gemini", "google-imagen", "google-veo", "google-gemini-video"]
        for provider in google_providers:
            assert provider in PROVIDER_KEY_MAP, f"Missing mapping for {provider}"
            assert PROVIDER_KEY_MAP[provider] == "image_api_key"

    def test_openai_providers_use_openai_api_key(self):
        """OpenAI providers should use openai_api_key."""
        openai_providers = ["openai-gpt-image", "openai-sora"]
        for provider in openai_providers:
            assert provider in PROVIDER_KEY_MAP, f"Missing mapping for {provider}"
            assert PROVIDER_KEY_MAP[provider] == "openai_api_key"

    def test_meshy_provider_uses_meshy_api_key(self):
        """Meshy provider should use meshy_api_key."""
        assert "meshy" in PROVIDER_KEY_MAP
        assert PROVIDER_KEY_MAP["meshy"] == "meshy_api_key"

    def test_all_tool_providers_have_key_mapping(self):
        """Every provider used in AI_TOOLS should have a key mapping."""
        all_providers = set()
        for tool_info in AI_TOOLS.values():
            all_providers.update(tool_info["providers"])

        for provider in all_providers:
            assert provider in PROVIDER_KEY_MAP, f"Provider {provider} missing from PROVIDER_KEY_MAP"


class TestAIToolsConfigModel:
    """Test the dynamically generated AIToolsConfig Pydantic model."""

    def test_aitools_config_is_pydantic_model(self):
        """AIToolsConfig should be a Pydantic model class."""
        assert issubclass(AIToolsConfig, BaseModel)

    def test_aitools_config_has_docstring(self):
        """AIToolsConfig should have a docstring."""
        assert AIToolsConfig.__doc__ is not None
        assert len(AIToolsConfig.__doc__) > 0

    def test_aitools_config_has_field_for_each_tool(self):
        """AIToolsConfig should have a boolean field for each tool in AI_TOOLS."""
        model_fields = AIToolsConfig.model_fields

        for tool_id in AI_TOOLS.keys():
            assert tool_id in model_fields, f"Missing field for tool {tool_id}"

    def test_aitools_config_fields_are_bool_with_false_default(self):
        """All fields should be boolean type defaulting to False."""
        for field_name, field_info in AIToolsConfig.model_fields.items():
            assert field_info.annotation == bool, f"Field {field_name} should be bool"
            assert field_info.default is False, f"Field {field_name} should default to False"

    def test_aitools_config_instantiation_with_defaults(self):
        """Should be able to instantiate with all defaults."""
        config = AIToolsConfig()

        for tool_id in AI_TOOLS.keys():
            assert getattr(config, tool_id) is False

    def test_aitools_config_instantiation_with_enabled_tools(self):
        """Should be able to enable specific tools."""
        config = AIToolsConfig(image_generation=True, video_generation=True)

        assert config.image_generation is True
        assert config.video_generation is True
        assert config.image_editing is False

    def test_aitools_config_to_dict(self):
        """Should be able to convert to dictionary."""
        config = AIToolsConfig(image_generation=True)
        config_dict = config.model_dump()

        assert isinstance(config_dict, dict)
        assert config_dict["image_generation"] is True
        assert config_dict["image_editing"] is False

    def test_aitools_config_from_dict(self):
        """Should be able to create from dictionary."""
        data = {"image_generation": True, "video_generation": True}
        config = AIToolsConfig(**data)

        assert config.image_generation is True
        assert config.video_generation is True

    def test_create_ai_tools_config_class_returns_new_class(self):
        """_create_ai_tools_config_class should create a new model class."""
        NewConfig = _create_ai_tools_config_class()

        assert issubclass(NewConfig, BaseModel)
        assert NewConfig.__name__ == "AIToolsConfig"


class TestGetToolIds:
    """Test the get_tool_ids helper function."""

    def test_get_tool_ids_returns_list(self):
        """get_tool_ids should return a list."""
        result = get_tool_ids()
        assert isinstance(result, list)

    def test_get_tool_ids_not_empty(self):
        """get_tool_ids should return non-empty list."""
        result = get_tool_ids()
        assert len(result) > 0

    def test_get_tool_ids_contains_expected_tools(self):
        """get_tool_ids should contain expected tool IDs."""
        result = get_tool_ids()
        expected = ["image_generation", "video_generation", "text_to_3d"]

        for tool_id in expected:
            assert tool_id in result

    def test_get_tool_ids_matches_ai_tools_keys(self):
        """get_tool_ids should match AI_TOOLS keys."""
        result = get_tool_ids()
        expected = list(AI_TOOLS.keys())

        assert set(result) == set(expected)

    def test_get_tool_ids_returns_strings(self):
        """get_tool_ids should return list of strings."""
        result = get_tool_ids()

        for tool_id in result:
            assert isinstance(tool_id, str)


class TestGetToolsByCategory:
    """Test the get_tools_by_category helper function."""

    def test_get_tools_by_category_returns_dict(self):
        """get_tools_by_category should return a dictionary."""
        result = get_tools_by_category("image")
        assert isinstance(result, dict)

    def test_get_image_tools(self):
        """get_tools_by_category('image') should return image tools."""
        result = get_tools_by_category("image")

        assert len(result) > 0
        for tool_id, tool_info in result.items():
            assert tool_info["category"] == "image"

    def test_get_video_tools(self):
        """get_tools_by_category('video') should return video tools."""
        result = get_tools_by_category("video")

        assert len(result) > 0
        for tool_id, tool_info in result.items():
            assert tool_info["category"] == "video"

    def test_get_3d_tools(self):
        """get_tools_by_category('3d') should return 3D tools."""
        result = get_tools_by_category("3d")

        assert len(result) > 0
        for tool_id, tool_info in result.items():
            assert tool_info["category"] == "3d"

    def test_get_nonexistent_category_returns_empty(self):
        """get_tools_by_category with nonexistent category should return empty dict."""
        result = get_tools_by_category("nonexistent_category")

        assert isinstance(result, dict)
        assert len(result) == 0

    def test_get_tools_by_category_preserves_tool_info(self):
        """get_tools_by_category should preserve complete tool info."""
        result = get_tools_by_category("image")

        for tool_id, tool_info in result.items():
            assert tool_id in AI_TOOLS
            assert tool_info == AI_TOOLS[tool_id]


class TestGetCategories:
    """Test the get_categories helper function."""

    def test_get_categories_returns_list(self):
        """get_categories should return a list."""
        result = get_categories()
        assert isinstance(result, list)

    def test_get_categories_not_empty(self):
        """get_categories should return non-empty list."""
        result = get_categories()
        assert len(result) > 0

    def test_get_categories_contains_expected(self):
        """get_categories should contain expected categories."""
        result = get_categories()
        expected = ["image", "video", "3d"]

        for category in expected:
            assert category in result

    def test_get_categories_unique(self):
        """get_categories should return unique categories only."""
        result = get_categories()

        assert len(result) == len(set(result))

    def test_get_categories_returns_strings(self):
        """get_categories should return list of strings."""
        result = get_categories()

        for category in result:
            assert isinstance(category, str)

    def test_get_categories_matches_tool_categories(self):
        """get_categories should match all categories used in AI_TOOLS."""
        result = set(get_categories())
        expected = set(tool["category"] for tool in AI_TOOLS.values())

        assert result == expected


class TestToolStructureConsistency:
    """Test consistency across tool definitions."""

    def test_tool_ids_are_snake_case(self):
        """All tool IDs should be in snake_case format."""
        for tool_id in AI_TOOLS.keys():
            # Check it's lowercase
            assert tool_id == tool_id.lower(), f"Tool ID {tool_id} should be lowercase"
            # Check no spaces
            assert " " not in tool_id, f"Tool ID {tool_id} should not contain spaces"
            # Check only contains valid chars (letters, numbers, underscores)
            assert tool_id.replace("_", "").isalnum(), f"Tool ID {tool_id} contains invalid characters"

    def test_provider_names_are_consistent(self):
        """Provider names should follow consistent format (lowercase with hyphens)."""
        all_providers = set()
        for tool_info in AI_TOOLS.values():
            all_providers.update(tool_info["providers"])

        for provider in all_providers:
            assert provider == provider.lower(), f"Provider {provider} should be lowercase"
            assert " " not in provider, f"Provider {provider} should not contain spaces"

    def test_all_tools_accessible_by_category(self):
        """All tools should be retrievable by their category."""
        for tool_id, tool_info in AI_TOOLS.items():
            category = tool_info["category"]
            category_tools = get_tools_by_category(category)

            assert tool_id in category_tools, f"Tool {tool_id} not found in category {category}"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_aitools_config_with_all_tools_enabled(self):
        """Should be able to enable all tools at once."""
        all_enabled = {tool_id: True for tool_id in AI_TOOLS.keys()}
        config = AIToolsConfig(**all_enabled)

        for tool_id in AI_TOOLS.keys():
            assert getattr(config, tool_id) is True

    def test_aitools_config_ignores_extra_fields(self):
        """Extra fields should be ignored (Pydantic default behavior)."""
        # Pydantic v2 by default ignores extra fields, so this should work
        # but the extra field should not be present on the model
        config = AIToolsConfig(nonexistent_tool=True, image_generation=True)

        assert config.image_generation is True
        assert not hasattr(config, 'nonexistent_tool') or getattr(config, 'nonexistent_tool', None) is None

    def test_aitools_config_with_non_bool_value_coerced(self):
        """Pydantic should coerce truthy/falsy values to bool."""
        config = AIToolsConfig(image_generation=1)  # int should coerce to True
        assert config.image_generation is True

        config2 = AIToolsConfig(image_generation=0)  # int 0 should coerce to False
        assert config2.image_generation is False

    def test_empty_category_search(self):
        """Searching for empty string category should return empty dict."""
        result = get_tools_by_category("")
        assert result == {}

    def test_get_tool_ids_is_fresh_list(self):
        """get_tool_ids should return a new list each time."""
        list1 = get_tool_ids()
        list2 = get_tool_ids()

        assert list1 == list2
        assert list1 is not list2  # Different objects

    def test_module_level_aitools_config_is_exported(self):
        """AIToolsConfig should be accessible from module level."""
        assert hasattr(ai_tools, 'AIToolsConfig')
        assert ai_tools.AIToolsConfig is AIToolsConfig
