"""
Unit tests for AI tools registry module.

Tests cover:
- AI_TOOLS dictionary structure and contents
- AIToolsConfig dynamic model generation
- Edge cases and validation
"""

from pydantic import BaseModel

# Import the module under test
from app.core import ai_tools
from app.core.ai_tools import (
    AI_TOOLS,
    AIToolsConfig,
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
        categories = {tool["category"] for tool in AI_TOOLS.values()}
        expected_categories = {"image", "video", "3d"}
        assert expected_categories.issubset(set(categories)), "Missing expected categories"

    def test_image_generation_tool_exists(self):
        """Image generation tool should be defined."""
        assert "image_generation" in AI_TOOLS
        tool = AI_TOOLS["image_generation"]
        assert tool["category"] == "image"
        assert "fal-ai" in tool["providers"]

    def test_video_generation_tool_exists(self):
        """Video generation tool should be defined."""
        assert "video_generation" in AI_TOOLS
        tool = AI_TOOLS["video_generation"]
        assert tool["category"] == "video"
        assert "fal-ai" in tool["providers"]

    def test_3d_tools_exist(self):
        """3D tools should be defined."""
        expected_3d_tools = {"text_to_3d", "image_to_3d"}
        actual_3d_tools = {tool_id for tool_id, info in AI_TOOLS.items() if info["category"] == "3d"}
        assert expected_3d_tools == actual_3d_tools


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

    def test_module_level_aitools_config_is_exported(self):
        """AIToolsConfig should be accessible from module level."""
        assert hasattr(ai_tools, 'AIToolsConfig')
        assert ai_tools.AIToolsConfig is AIToolsConfig
