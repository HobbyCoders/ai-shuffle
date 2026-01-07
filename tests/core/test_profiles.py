"""
Unit tests for app/core/profiles.py module.

Tests cover:
- DEFAULT_PROFILE_CONFIG constant structure
- DEFAULT_TEMPLATES constant structure
- run_migrations function
- _seed_default_templates function
- get_profile function
- All code paths including edge cases and error handling
"""

import pytest
from unittest.mock import MagicMock, patch, call
from typing import Dict, Any, List


# =============================================================================
# Test DEFAULT_PROFILE_CONFIG
# =============================================================================

class TestDefaultProfileConfig:
    """Test the DEFAULT_PROFILE_CONFIG constant."""

    def test_default_profile_config_has_model(self):
        """Should have model set to sonnet."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert DEFAULT_PROFILE_CONFIG["model"] == "sonnet"

    def test_default_profile_config_has_allowed_tools(self):
        """Should have allowed_tools as empty list."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert DEFAULT_PROFILE_CONFIG["allowed_tools"] == []

    def test_default_profile_config_has_permission_mode(self):
        """Should have permission_mode set to bypassPermissions."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert DEFAULT_PROFILE_CONFIG["permission_mode"] == "bypassPermissions"

    def test_default_profile_config_has_system_prompt(self):
        """Should have system_prompt with preset type."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert DEFAULT_PROFILE_CONFIG["system_prompt"]["type"] == "preset"
        assert DEFAULT_PROFILE_CONFIG["system_prompt"]["preset"] == "claude_code"

    def test_default_profile_config_has_setting_sources(self):
        """Should have setting_sources with user and project."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert "user" in DEFAULT_PROFILE_CONFIG["setting_sources"]
        assert "project" in DEFAULT_PROFILE_CONFIG["setting_sources"]

    def test_default_profile_config_has_enabled_agents(self):
        """Should have enabled_agents as empty list."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert DEFAULT_PROFILE_CONFIG["enabled_agents"] == []

    def test_default_profile_config_is_complete(self):
        """Should have all expected keys."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        expected_keys = {
            "model",
            "allowed_tools",
            "permission_mode",
            "system_prompt",
            "setting_sources",
            "enabled_agents"
        }
        assert set(DEFAULT_PROFILE_CONFIG.keys()) == expected_keys


# =============================================================================
# Test DEFAULT_TEMPLATES
# =============================================================================

class TestDefaultTemplates:
    """Test the DEFAULT_TEMPLATES constant."""

    def test_default_templates_is_list(self):
        """Should be a list."""
        from app.core.profiles import DEFAULT_TEMPLATES
        assert isinstance(DEFAULT_TEMPLATES, list)

    def test_default_templates_has_entries(self):
        """Should have 6 default templates."""
        from app.core.profiles import DEFAULT_TEMPLATES
        assert len(DEFAULT_TEMPLATES) == 6

    def test_default_templates_have_required_fields(self):
        """Each template should have required fields."""
        from app.core.profiles import DEFAULT_TEMPLATES
        required_fields = {"id", "name", "prompt"}
        for template in DEFAULT_TEMPLATES:
            for field in required_fields:
                assert field in template, f"Template missing {field}"

    def test_default_templates_have_unique_ids(self):
        """Template IDs should be unique."""
        from app.core.profiles import DEFAULT_TEMPLATES
        ids = [t["id"] for t in DEFAULT_TEMPLATES]
        assert len(ids) == len(set(ids))

    def test_code_review_template(self):
        """Should have code review template."""
        from app.core.profiles import DEFAULT_TEMPLATES
        code_review = next((t for t in DEFAULT_TEMPLATES if t["id"] == "tmpl-code-review"), None)
        assert code_review is not None
        assert code_review["name"] == "Code Review"
        assert code_review["category"] == "coding"
        assert "icon" in code_review

    def test_explain_code_template(self):
        """Should have explain code template."""
        from app.core.profiles import DEFAULT_TEMPLATES
        explain = next((t for t in DEFAULT_TEMPLATES if t["id"] == "tmpl-explain-code"), None)
        assert explain is not None
        assert explain["name"] == "Explain Code"
        assert explain["category"] == "coding"

    def test_debug_help_template(self):
        """Should have debug help template."""
        from app.core.profiles import DEFAULT_TEMPLATES
        debug = next((t for t in DEFAULT_TEMPLATES if t["id"] == "tmpl-debug-help"), None)
        assert debug is not None
        assert debug["name"] == "Debug Help"
        assert debug["category"] == "debugging"

    def test_write_tests_template(self):
        """Should have write tests template."""
        from app.core.profiles import DEFAULT_TEMPLATES
        tests = next((t for t in DEFAULT_TEMPLATES if t["id"] == "tmpl-write-tests"), None)
        assert tests is not None
        assert tests["name"] == "Write Tests"
        assert tests["category"] == "coding"

    def test_refactor_template(self):
        """Should have refactor template."""
        from app.core.profiles import DEFAULT_TEMPLATES
        refactor = next((t for t in DEFAULT_TEMPLATES if t["id"] == "tmpl-refactor"), None)
        assert refactor is not None
        assert refactor["name"] == "Refactor Code"
        assert refactor["category"] == "coding"

    def test_documentation_template(self):
        """Should have documentation template."""
        from app.core.profiles import DEFAULT_TEMPLATES
        docs = next((t for t in DEFAULT_TEMPLATES if t["id"] == "tmpl-documentation"), None)
        assert docs is not None
        assert docs["name"] == "Write Documentation"
        assert docs["category"] == "documentation"

    def test_all_templates_have_descriptions(self):
        """All templates should have descriptions."""
        from app.core.profiles import DEFAULT_TEMPLATES
        for template in DEFAULT_TEMPLATES:
            assert "description" in template
            assert template["description"]  # Not empty

    def test_all_templates_have_icons(self):
        """All templates should have icons."""
        from app.core.profiles import DEFAULT_TEMPLATES
        for template in DEFAULT_TEMPLATES:
            assert "icon" in template
            assert template["icon"]  # Not empty


# =============================================================================
# Test run_migrations Function
# =============================================================================

class TestRunMigrations:
    """Test the run_migrations function."""

    def test_run_migrations_fixes_builtin_profiles(self):
        """Should set is_builtin=False for all profiles with is_builtin=True."""
        mock_profiles = [
            {"id": "profile-1", "is_builtin": True},
            {"id": "profile-2", "is_builtin": False},
            {"id": "profile-3", "is_builtin": True},
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = mock_profiles
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            # Should call set_profile_builtin for profiles with is_builtin=True
            calls = mock_db.set_profile_builtin.call_args_list
            assert len(calls) == 2
            mock_db.set_profile_builtin.assert_any_call("profile-1", False)
            mock_db.set_profile_builtin.assert_any_call("profile-3", False)

    def test_run_migrations_skips_non_builtin_profiles(self):
        """Should not modify profiles that already have is_builtin=False."""
        mock_profiles = [
            {"id": "profile-1", "is_builtin": False},
            {"id": "profile-2", "is_builtin": False},
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = mock_profiles
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.set_profile_builtin.assert_not_called()

    def test_run_migrations_fixes_builtin_subagents(self):
        """Should set is_builtin=False for all subagents with is_builtin=True."""
        mock_subagents = [
            {"id": "subagent-1", "is_builtin": True},
            {"id": "subagent-2", "is_builtin": False},
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = mock_subagents
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.set_subagent_builtin.assert_called_once_with("subagent-1", False)

    def test_run_migrations_skips_non_builtin_subagents(self):
        """Should not modify subagents that already have is_builtin=False."""
        mock_subagents = [
            {"id": "subagent-1", "is_builtin": False},
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = mock_subagents
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.set_subagent_builtin.assert_not_called()

    def test_run_migrations_handles_empty_profiles(self):
        """Should handle case when no profiles exist."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.set_profile_builtin.assert_not_called()
            mock_db.set_subagent_builtin.assert_not_called()

    def test_run_migrations_handles_empty_subagents(self):
        """Should handle case when no subagents exist."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.set_subagent_builtin.assert_not_called()

    def test_run_migrations_seeds_templates_when_none_exist(self):
        """Should seed default templates when no templates exist."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations, DEFAULT_TEMPLATES
            run_migrations()

            # Should create all default templates
            assert mock_db.create_template.call_count == len(DEFAULT_TEMPLATES)

    def test_run_migrations_skips_seeding_when_templates_exist(self):
        """Should not seed templates when templates already exist."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = [{"id": "existing-template"}]

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.create_template.assert_not_called()

    def test_run_migrations_processes_profiles_with_missing_is_builtin(self):
        """Should handle profiles that don't have is_builtin key (treat as falsy)."""
        mock_profiles = [
            {"id": "profile-1"},  # Missing is_builtin key
            {"id": "profile-2", "is_builtin": None},  # is_builtin is None
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = mock_profiles
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            # Should not call set_profile_builtin since is_builtin is falsy
            mock_db.set_profile_builtin.assert_not_called()

    def test_run_migrations_processes_subagents_with_missing_is_builtin(self):
        """Should handle subagents that don't have is_builtin key (treat as falsy)."""
        mock_subagents = [
            {"id": "subagent-1"},  # Missing is_builtin key
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = []
            mock_db.get_all_subagents.return_value = mock_subagents
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import run_migrations
            run_migrations()

            mock_db.set_subagent_builtin.assert_not_called()


# =============================================================================
# Test _seed_default_templates Function
# =============================================================================

class TestSeedDefaultTemplates:
    """Test the _seed_default_templates function."""

    def test_seed_templates_creates_all_defaults(self):
        """Should create all default templates when none exist."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates, DEFAULT_TEMPLATES
            _seed_default_templates()

            assert mock_db.create_template.call_count == len(DEFAULT_TEMPLATES)

    def test_seed_templates_passes_correct_parameters(self):
        """Should pass correct parameters to create_template."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates, DEFAULT_TEMPLATES
            _seed_default_templates()

            # Check first template call
            first_template = DEFAULT_TEMPLATES[0]
            call_kwargs = mock_db.create_template.call_args_list[0][1]
            assert call_kwargs["template_id"] == first_template["id"]
            assert call_kwargs["name"] == first_template["name"]
            assert call_kwargs["prompt"] == first_template["prompt"]
            assert call_kwargs["is_builtin"] is True

    def test_seed_templates_skips_when_templates_exist(self):
        """Should not create templates when any template exists."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = [
                {"id": "some-template", "name": "Existing"}
            ]

            from app.core.profiles import _seed_default_templates
            _seed_default_templates()

            mock_db.create_template.assert_not_called()

    def test_seed_templates_includes_description(self):
        """Should include description in template creation."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates, DEFAULT_TEMPLATES
            _seed_default_templates()

            for i, call_obj in enumerate(mock_db.create_template.call_args_list):
                call_kwargs = call_obj[1]
                expected_desc = DEFAULT_TEMPLATES[i].get("description")
                assert call_kwargs["description"] == expected_desc

    def test_seed_templates_includes_icon(self):
        """Should include icon in template creation."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates, DEFAULT_TEMPLATES
            _seed_default_templates()

            for i, call_obj in enumerate(mock_db.create_template.call_args_list):
                call_kwargs = call_obj[1]
                expected_icon = DEFAULT_TEMPLATES[i].get("icon")
                assert call_kwargs["icon"] == expected_icon

    def test_seed_templates_includes_category(self):
        """Should include category in template creation."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates, DEFAULT_TEMPLATES
            _seed_default_templates()

            for i, call_obj in enumerate(mock_db.create_template.call_args_list):
                call_kwargs = call_obj[1]
                expected_category = DEFAULT_TEMPLATES[i].get("category")
                assert call_kwargs["category"] == expected_category

    def test_seed_templates_sets_is_builtin_true(self):
        """Should mark all seeded templates as builtin."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates
            _seed_default_templates()

            for call_obj in mock_db.create_template.call_args_list:
                call_kwargs = call_obj[1]
                assert call_kwargs["is_builtin"] is True


# =============================================================================
# Test get_profile Function
# =============================================================================

class TestGetProfile:
    """Test the get_profile function."""

    def test_get_profile_returns_profile_when_found(self):
        """Should return profile data when profile exists."""
        expected_profile = {
            "id": "test-profile",
            "name": "Test Profile",
            "description": "A test profile",
            "config": {"model": "sonnet"},
            "is_builtin": False
        }

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = expected_profile

            from app.core.profiles import get_profile
            result = get_profile("test-profile")

            assert result == expected_profile
            mock_db.get_profile.assert_called_once_with("test-profile")

    def test_get_profile_returns_none_when_not_found(self):
        """Should return None when profile does not exist."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            from app.core.profiles import get_profile
            result = get_profile("nonexistent-profile")

            assert result is None
            mock_db.get_profile.assert_called_once_with("nonexistent-profile")

    def test_get_profile_passes_profile_id_to_database(self):
        """Should pass the correct profile_id to database."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = {"id": "my-profile"}

            from app.core.profiles import get_profile
            get_profile("my-special-profile")

            mock_db.get_profile.assert_called_once_with("my-special-profile")

    def test_get_profile_with_empty_string_id(self):
        """Should handle empty string profile_id."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            from app.core.profiles import get_profile
            result = get_profile("")

            assert result is None
            mock_db.get_profile.assert_called_once_with("")

    def test_get_profile_with_complex_config(self):
        """Should return profile with complex nested config."""
        complex_profile = {
            "id": "complex-profile",
            "name": "Complex Profile",
            "config": {
                "model": "opus",
                "system_prompt": {
                    "type": "custom",
                    "content": "You are a helpful assistant"
                },
                "allowed_tools": ["bash", "read", "write"],
                "nested": {
                    "deep": {
                        "value": 123
                    }
                }
            },
            "mcp_tools": ["tool1", "tool2"]
        }

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = complex_profile

            from app.core.profiles import get_profile
            result = get_profile("complex-profile")

            assert result == complex_profile
            assert result["config"]["model"] == "opus"
            assert result["config"]["nested"]["deep"]["value"] == 123


# =============================================================================
# Integration-style Tests
# =============================================================================

class TestMigrationsIntegration:
    """Integration tests for migrations with various scenarios."""

    def test_run_migrations_full_scenario(self):
        """Should handle a complete migration scenario."""
        mock_profiles = [
            {"id": "builtin-1", "is_builtin": True, "name": "Builtin 1"},
            {"id": "custom-1", "is_builtin": False, "name": "Custom 1"},
            {"id": "builtin-2", "is_builtin": True, "name": "Builtin 2"},
        ]
        mock_subagents = [
            {"id": "sub-1", "is_builtin": True, "name": "Subagent 1"},
            {"id": "sub-2", "is_builtin": False, "name": "Subagent 2"},
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = mock_profiles
            mock_db.get_all_subagents.return_value = mock_subagents
            mock_db.get_all_templates.return_value = []  # No templates, should seed

            from app.core.profiles import run_migrations, DEFAULT_TEMPLATES
            run_migrations()

            # Verify profile migrations
            assert mock_db.set_profile_builtin.call_count == 2
            mock_db.set_profile_builtin.assert_any_call("builtin-1", False)
            mock_db.set_profile_builtin.assert_any_call("builtin-2", False)

            # Verify subagent migrations
            assert mock_db.set_subagent_builtin.call_count == 1
            mock_db.set_subagent_builtin.assert_called_with("sub-1", False)

            # Verify template seeding
            assert mock_db.create_template.call_count == len(DEFAULT_TEMPLATES)

    def test_run_migrations_no_work_needed(self):
        """Should handle case where no migrations are needed."""
        mock_profiles = [
            {"id": "custom-1", "is_builtin": False},
            {"id": "custom-2", "is_builtin": False},
        ]
        mock_subagents = [
            {"id": "sub-1", "is_builtin": False},
        ]
        mock_templates = [
            {"id": "tmpl-1", "name": "Existing Template"},
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = mock_profiles
            mock_db.get_all_subagents.return_value = mock_subagents
            mock_db.get_all_templates.return_value = mock_templates

            from app.core.profiles import run_migrations
            run_migrations()

            # No migrations needed
            mock_db.set_profile_builtin.assert_not_called()
            mock_db.set_subagent_builtin.assert_not_called()
            mock_db.create_template.assert_not_called()

    def test_run_migrations_order_of_operations(self):
        """Should perform migrations in correct order."""
        call_order = []

        with patch("app.core.profiles.database") as mock_db:
            def track_get_profiles():
                call_order.append("get_all_profiles")
                return [{"id": "p1", "is_builtin": True}]

            def track_set_profile_builtin(*args):
                call_order.append("set_profile_builtin")
                return True

            def track_get_subagents():
                call_order.append("get_all_subagents")
                return []

            def track_get_templates():
                call_order.append("get_all_templates")
                return []

            def track_create_template(**kwargs):
                call_order.append("create_template")
                return {}

            mock_db.get_all_profiles.side_effect = track_get_profiles
            mock_db.set_profile_builtin.side_effect = track_set_profile_builtin
            mock_db.get_all_subagents.side_effect = track_get_subagents
            mock_db.get_all_templates.side_effect = track_get_templates
            mock_db.create_template.side_effect = track_create_template

            from app.core.profiles import run_migrations
            run_migrations()

            # Verify order: profiles first, then subagents, then templates
            profile_idx = call_order.index("get_all_profiles")
            subagent_idx = call_order.index("get_all_subagents")
            template_idx = call_order.index("get_all_templates")

            assert profile_idx < subagent_idx < template_idx


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_get_profile_with_special_characters_in_id(self):
        """Should handle profile IDs with special characters."""
        special_id = "profile-with-special_chars.123"
        expected = {"id": special_id, "name": "Special"}

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = expected

            from app.core.profiles import get_profile
            result = get_profile(special_id)

            assert result == expected
            mock_db.get_profile.assert_called_once_with(special_id)

    def test_get_profile_with_unicode_id(self):
        """Should handle profile IDs with unicode characters."""
        unicode_id = "profile-unicode-"
        expected = {"id": unicode_id, "name": "Unicode Profile"}

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_profile.return_value = expected

            from app.core.profiles import get_profile
            result = get_profile(unicode_id)

            assert result == expected

    def test_run_migrations_with_large_number_of_profiles(self):
        """Should handle a large number of profiles efficiently."""
        large_profile_list = [
            {"id": f"profile-{i}", "is_builtin": i % 2 == 0}
            for i in range(100)
        ]

        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_profiles.return_value = large_profile_list
            mock_db.get_all_subagents.return_value = []
            mock_db.get_all_templates.return_value = [{"id": "existing"}]

            from app.core.profiles import run_migrations
            run_migrations()

            # Should call set_profile_builtin for 50 profiles (even indices)
            assert mock_db.set_profile_builtin.call_count == 50

    def test_seed_templates_handles_all_optional_fields(self):
        """Should properly handle templates with all optional fields."""
        with patch("app.core.profiles.database") as mock_db:
            mock_db.get_all_templates.return_value = []

            from app.core.profiles import _seed_default_templates, DEFAULT_TEMPLATES
            _seed_default_templates()

            # Verify all templates are created with correct optional fields
            for i, template in enumerate(DEFAULT_TEMPLATES):
                call_kwargs = mock_db.create_template.call_args_list[i][1]
                assert call_kwargs.get("description") == template.get("description")
                assert call_kwargs.get("icon") == template.get("icon")
                assert call_kwargs.get("category") == template.get("category")


class TestModuleImports:
    """Test that module imports work correctly."""

    def test_can_import_default_profile_config(self):
        """Should be able to import DEFAULT_PROFILE_CONFIG."""
        from app.core.profiles import DEFAULT_PROFILE_CONFIG
        assert DEFAULT_PROFILE_CONFIG is not None

    def test_can_import_default_templates(self):
        """Should be able to import DEFAULT_TEMPLATES."""
        from app.core.profiles import DEFAULT_TEMPLATES
        assert DEFAULT_TEMPLATES is not None

    def test_can_import_run_migrations(self):
        """Should be able to import run_migrations function."""
        from app.core.profiles import run_migrations
        assert callable(run_migrations)

    def test_can_import_get_profile(self):
        """Should be able to import get_profile function."""
        from app.core.profiles import get_profile
        assert callable(get_profile)

    def test_can_import_seed_default_templates(self):
        """Should be able to import _seed_default_templates function."""
        from app.core.profiles import _seed_default_templates
        assert callable(_seed_default_templates)
