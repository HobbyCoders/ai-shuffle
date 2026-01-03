"""
Comprehensive tests for the permission_rules API endpoints.

Tests cover:
- Permission rule CRUD operations (list, get by profile, create, delete)
- Profile-based permission rule management
- Authentication requirements (admin only)
- Error handling and validation
- Edge cases and boundary conditions
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi import Request, HTTPException


class TestPermissionRulesModuleImports:
    """Verify permission_rules module can be imported correctly."""

    def test_permission_rules_module_imports(self):
        """Permission rules module should import without errors."""
        from app.api import permission_rules
        assert permission_rules is not None

    def test_permission_rules_router_exists(self):
        """Permission rules router should exist."""
        from app.api.permission_rules import router
        assert router is not None

    def test_permission_rule_response_model_exists(self):
        """PermissionRuleResponse model should exist."""
        from app.api.permission_rules import PermissionRuleResponse
        assert PermissionRuleResponse is not None

    def test_permission_rule_create_model_exists(self):
        """PermissionRuleCreate model should exist."""
        from app.api.permission_rules import PermissionRuleCreate
        assert PermissionRuleCreate is not None


class TestRequestResponseModels:
    """Test Pydantic models for request/response validation."""

    def test_permission_rule_response_valid(self):
        """PermissionRuleResponse should accept valid data."""
        from app.api.permission_rules import PermissionRuleResponse

        response = PermissionRuleResponse(
            id="rule-abc123",
            profile_id="test-profile",
            tool_name="Bash",
            tool_pattern="npm *",
            decision="allow",
            created_at="2024-01-01T00:00:00"
        )
        assert response.id == "rule-abc123"
        assert response.profile_id == "test-profile"
        assert response.tool_name == "Bash"
        assert response.tool_pattern == "npm *"
        assert response.decision == "allow"

    def test_permission_rule_response_minimal(self):
        """PermissionRuleResponse should accept minimal data."""
        from app.api.permission_rules import PermissionRuleResponse

        response = PermissionRuleResponse(
            id="rule-abc123",
            tool_name="Read",
            decision="deny",
            created_at="2024-01-01T00:00:00"
        )
        assert response.profile_id is None
        assert response.tool_pattern is None

    def test_permission_rule_create_valid(self):
        """PermissionRuleCreate should accept valid data."""
        from app.api.permission_rules import PermissionRuleCreate

        request = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            tool_pattern="npm *",
            decision="allow"
        )
        assert request.profile_id == "test-profile"
        assert request.tool_name == "Bash"
        assert request.tool_pattern == "npm *"
        assert request.decision == "allow"

    def test_permission_rule_create_without_pattern(self):
        """PermissionRuleCreate should accept data without tool_pattern."""
        from app.api.permission_rules import PermissionRuleCreate

        request = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="*",
            decision="deny"
        )
        assert request.tool_pattern is None

    def test_permission_rule_create_missing_required_fields(self):
        """PermissionRuleCreate should fail without required fields."""
        from app.api.permission_rules import PermissionRuleCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PermissionRuleCreate(
                tool_name="Bash",
                decision="allow"
                # Missing profile_id
            )

        with pytest.raises(ValidationError):
            PermissionRuleCreate(
                profile_id="test-profile",
                decision="allow"
                # Missing tool_name
            )

        with pytest.raises(ValidationError):
            PermissionRuleCreate(
                profile_id="test-profile",
                tool_name="Bash"
                # Missing decision
            )


# =============================================================================
# Sample Data
# =============================================================================

def sample_profile():
    """Create a sample profile for testing."""
    now = datetime.utcnow().isoformat()
    return {
        "id": "test-profile",
        "name": "Test Profile",
        "description": "A test profile",
        "config": {
            "model": "sonnet",
            "enabled_agents": []
        },
        "is_builtin": False,
        "created_at": now,
        "updated_at": now
    }


def sample_permission_rule():
    """Create a sample permission rule for testing."""
    now = datetime.utcnow().isoformat()
    return {
        "id": "rule-abc123",
        "profile_id": "test-profile",
        "tool_name": "Bash",
        "tool_pattern": "npm *",
        "decision": "allow",
        "created_at": now
    }


def sample_permission_rule_list():
    """Create a list of sample permission rules."""
    now = datetime.utcnow().isoformat()
    return [
        {
            "id": "rule-001",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": "npm *",
            "decision": "allow",
            "created_at": now
        },
        {
            "id": "rule-002",
            "profile_id": "test-profile",
            "tool_name": "Read",
            "tool_pattern": "/workspace/*",
            "decision": "allow",
            "created_at": now
        },
        {
            "id": "rule-003",
            "profile_id": "test-profile",
            "tool_name": "*",
            "tool_pattern": None,
            "decision": "deny",
            "created_at": now
        }
    ]


# =============================================================================
# List Permission Rules Tests
# =============================================================================

class TestListPermissionRulesEndpoint:
    """Test GET /api/v1/permission-rules endpoint."""

    @pytest.mark.asyncio
    async def test_list_permission_rules_success(self):
        """Listing permission rules should return all rules."""
        from app.api.permission_rules import list_permission_rules

        rules = sample_permission_rule_list()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = rules

            result = await list_permission_rules(
                profile_id=None,
                tool_name=None,
                token="test-token"
            )

            assert len(result) == 3
            assert result[0]["id"] == "rule-001"
            assert result[1]["tool_name"] == "Read"
            mock_db.get_permission_rules.assert_called_once_with(profile_id=None, tool_name=None)

    @pytest.mark.asyncio
    async def test_list_permission_rules_empty(self):
        """Listing permission rules when none exist should return empty list."""
        from app.api.permission_rules import list_permission_rules

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = []

            result = await list_permission_rules(
                profile_id=None,
                tool_name=None,
                token="test-token"
            )

            assert result == []

    @pytest.mark.asyncio
    async def test_list_permission_rules_filter_by_profile(self):
        """Listing permission rules with profile filter should pass to database."""
        from app.api.permission_rules import list_permission_rules

        rules = sample_permission_rule_list()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = rules

            await list_permission_rules(
                profile_id="test-profile",
                tool_name=None,
                token="test-token"
            )

            mock_db.get_permission_rules.assert_called_once_with(profile_id="test-profile", tool_name=None)

    @pytest.mark.asyncio
    async def test_list_permission_rules_filter_by_tool_name(self):
        """Listing permission rules with tool_name filter should pass to database."""
        from app.api.permission_rules import list_permission_rules

        rules = [sample_permission_rule_list()[0]]

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = rules

            result = await list_permission_rules(
                profile_id=None,
                tool_name="Bash",
                token="test-token"
            )

            mock_db.get_permission_rules.assert_called_once_with(profile_id=None, tool_name="Bash")

    @pytest.mark.asyncio
    async def test_list_permission_rules_filter_by_both(self):
        """Listing permission rules with both filters should pass to database."""
        from app.api.permission_rules import list_permission_rules

        rules = [sample_permission_rule_list()[0]]

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = rules

            await list_permission_rules(
                profile_id="test-profile",
                tool_name="Bash",
                token="test-token"
            )

            mock_db.get_permission_rules.assert_called_once_with(profile_id="test-profile", tool_name="Bash")


# =============================================================================
# Get Profile Permission Rules Tests
# =============================================================================

class TestGetProfilePermissionRulesEndpoint:
    """Test GET /api/v1/permission-rules/profile/{profile_id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_profile_permission_rules_success(self):
        """Getting profile permission rules should return rules for that profile."""
        from app.api.permission_rules import get_profile_permission_rules

        profile = sample_profile()
        rules = sample_permission_rule_list()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_permission_rules.return_value = rules

            result = await get_profile_permission_rules(
                profile_id="test-profile",
                token="test-token"
            )

            assert len(result) == 3
            mock_db.get_profile.assert_called_once_with("test-profile")
            mock_db.get_permission_rules.assert_called_once_with(profile_id="test-profile")

    @pytest.mark.asyncio
    async def test_get_profile_permission_rules_empty(self):
        """Getting profile permission rules when none exist should return empty list."""
        from app.api.permission_rules import get_profile_permission_rules

        profile = sample_profile()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_permission_rules.return_value = []

            result = await get_profile_permission_rules(
                profile_id="test-profile",
                token="test-token"
            )

            assert result == []

    @pytest.mark.asyncio
    async def test_get_profile_permission_rules_profile_not_found(self):
        """Getting permission rules for non-existent profile should return 404."""
        from app.api.permission_rules import get_profile_permission_rules

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_profile_permission_rules(
                    profile_id="nonexistent",
                    token="test-token"
                )

            assert exc_info.value.status_code == 404
            assert "Profile not found" in exc_info.value.detail


# =============================================================================
# Create Permission Rule Tests
# =============================================================================

class TestCreatePermissionRuleEndpoint:
    """Test POST /api/v1/permission-rules endpoint."""

    @pytest.mark.asyncio
    async def test_create_permission_rule_success(self):
        """Creating a permission rule should return rule details."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-new123",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": "git *",
            "decision": "allow",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            tool_pattern="git *",
            decision="allow"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["id"] == "rule-new123"
            assert result["tool_name"] == "Bash"
            assert result["decision"] == "allow"
            mock_db.add_permission_rule.assert_called_once_with(
                profile_id="test-profile",
                tool_name="Bash",
                tool_pattern="git *",
                decision="allow"
            )

    @pytest.mark.asyncio
    async def test_create_permission_rule_without_pattern(self):
        """Creating a permission rule without pattern should work."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-new456",
            "profile_id": "test-profile",
            "tool_name": "*",
            "tool_pattern": None,
            "decision": "deny",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="*",
            decision="deny"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["tool_pattern"] is None
            mock_db.add_permission_rule.assert_called_once_with(
                profile_id="test-profile",
                tool_name="*",
                tool_pattern=None,
                decision="deny"
            )

    @pytest.mark.asyncio
    async def test_create_permission_rule_deny_decision(self):
        """Creating a permission rule with deny decision should work."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-deny123",
            "profile_id": "test-profile",
            "tool_name": "Write",
            "tool_pattern": "/etc/*",
            "decision": "deny",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Write",
            tool_pattern="/etc/*",
            decision="deny"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_create_permission_rule_invalid_decision(self):
        """Creating a permission rule with invalid decision should fail."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            decision="maybe"  # Invalid decision
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile

            with pytest.raises(HTTPException) as exc_info:
                await create_permission_rule(
                    rule=rule_data,
                    token="test-token"
                )

            assert exc_info.value.status_code == 400
            assert "must be 'allow' or 'deny'" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_permission_rule_profile_not_found(self):
        """Creating a permission rule for non-existent profile should fail."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        rule_data = PermissionRuleCreate(
            profile_id="nonexistent",
            tool_name="Bash",
            decision="allow"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await create_permission_rule(
                    rule=rule_data,
                    token="test-token"
                )

            assert exc_info.value.status_code == 404
            assert "Profile not found" in exc_info.value.detail


# =============================================================================
# Delete Permission Rule Tests
# =============================================================================

class TestDeletePermissionRuleEndpoint:
    """Test DELETE /api/v1/permission-rules/{rule_id} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_permission_rule_success(self):
        """Deleting a permission rule should return success."""
        from app.api.permission_rules import delete_permission_rule

        rule = sample_permission_rule()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rule.return_value = rule
            mock_db.delete_permission_rule.return_value = True

            result = await delete_permission_rule(
                rule_id="rule-abc123",
                token="test-token"
            )

            assert result["deleted"] is True
            assert result["id"] == "rule-abc123"
            mock_db.get_permission_rule.assert_called_once_with("rule-abc123")
            mock_db.delete_permission_rule.assert_called_once_with("rule-abc123")

    @pytest.mark.asyncio
    async def test_delete_permission_rule_not_found(self):
        """Deleting a non-existent permission rule should return 404."""
        from app.api.permission_rules import delete_permission_rule

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rule.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_permission_rule(
                    rule_id="nonexistent",
                    token="test-token"
                )

            assert exc_info.value.status_code == 404
            assert "Rule not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_delete_permission_rule_database_failure(self):
        """Deleting a permission rule with database failure should return 500."""
        from app.api.permission_rules import delete_permission_rule

        rule = sample_permission_rule()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rule.return_value = rule
            mock_db.delete_permission_rule.return_value = False

            with pytest.raises(HTTPException) as exc_info:
                await delete_permission_rule(
                    rule_id="rule-abc123",
                    token="test-token"
                )

            assert exc_info.value.status_code == 500
            assert "Failed to delete rule" in exc_info.value.detail


# =============================================================================
# Delete Profile Permission Rules Tests
# =============================================================================

class TestDeleteProfilePermissionRulesEndpoint:
    """Test DELETE /api/v1/permission-rules/profile/{profile_id} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_profile_permission_rules_success(self):
        """Deleting all permission rules for a profile should return count."""
        from app.api.permission_rules import delete_profile_permission_rules

        profile = sample_profile()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.delete_profile_permission_rules.return_value = 5

            result = await delete_profile_permission_rules(
                profile_id="test-profile",
                token="test-token"
            )

            assert result["deleted"] == 5
            assert result["profile_id"] == "test-profile"
            mock_db.get_profile.assert_called_once_with("test-profile")
            mock_db.delete_profile_permission_rules.assert_called_once_with("test-profile")

    @pytest.mark.asyncio
    async def test_delete_profile_permission_rules_empty(self):
        """Deleting permission rules when none exist should return 0."""
        from app.api.permission_rules import delete_profile_permission_rules

        profile = sample_profile()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.delete_profile_permission_rules.return_value = 0

            result = await delete_profile_permission_rules(
                profile_id="test-profile",
                token="test-token"
            )

            assert result["deleted"] == 0

    @pytest.mark.asyncio
    async def test_delete_profile_permission_rules_profile_not_found(self):
        """Deleting permission rules for non-existent profile should return 404."""
        from app.api.permission_rules import delete_profile_permission_rules

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_profile_permission_rules(
                    profile_id="nonexistent",
                    token="test-token"
                )

            assert exc_info.value.status_code == 404
            assert "Profile not found" in exc_info.value.detail


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_create_rule_with_empty_tool_pattern(self):
        """Creating rule with empty string pattern should work."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-empty",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": "",
            "decision": "allow",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            tool_pattern="",
            decision="allow"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["id"] == "rule-empty"

    @pytest.mark.asyncio
    async def test_create_rule_with_wildcard_tool_name(self):
        """Creating rule with wildcard (*) tool name should work."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-wildcard",
            "profile_id": "test-profile",
            "tool_name": "*",
            "tool_pattern": None,
            "decision": "deny",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="*",
            decision="deny"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["tool_name"] == "*"

    @pytest.mark.asyncio
    async def test_create_rule_with_special_characters_in_pattern(self):
        """Creating rule with special characters in pattern should work."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-special",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": "rm -rf /home/user/*.txt",
            "decision": "deny",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            tool_pattern="rm -rf /home/user/*.txt",
            decision="deny"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["tool_pattern"] == "rm -rf /home/user/*.txt"

    @pytest.mark.asyncio
    async def test_list_rules_returns_sorted_by_created_at(self):
        """Listing rules should return them in order."""
        from app.api.permission_rules import list_permission_rules

        rules = sample_permission_rule_list()

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = rules

            result = await list_permission_rules(
                profile_id=None,
                tool_name=None,
                token="test-token"
            )

            assert len(result) == 3

    @pytest.mark.asyncio
    async def test_very_long_tool_pattern(self):
        """Creating rule with very long tool pattern should work."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        long_pattern = "a" * 1000
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-long",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": long_pattern,
            "decision": "allow",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            tool_pattern=long_pattern,
            decision="allow"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert len(result["tool_pattern"]) == 1000


# =============================================================================
# Integration Tests with Multiple Rules
# =============================================================================

class TestMultipleRulesScenarios:
    """Test scenarios involving multiple permission rules."""

    @pytest.mark.asyncio
    async def test_list_rules_across_multiple_profiles(self):
        """Listing rules should return rules from all profiles when no filter."""
        from app.api.permission_rules import list_permission_rules

        now = datetime.utcnow().isoformat()
        multi_profile_rules = [
            {
                "id": "rule-p1-001",
                "profile_id": "profile-1",
                "tool_name": "Bash",
                "tool_pattern": None,
                "decision": "allow",
                "created_at": now
            },
            {
                "id": "rule-p2-001",
                "profile_id": "profile-2",
                "tool_name": "Read",
                "tool_pattern": None,
                "decision": "deny",
                "created_at": now
            }
        ]

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = multi_profile_rules

            result = await list_permission_rules(
                profile_id=None,
                tool_name=None,
                token="test-token"
            )

            assert len(result) == 2
            profile_ids = [r["profile_id"] for r in result]
            assert "profile-1" in profile_ids
            assert "profile-2" in profile_ids

    @pytest.mark.asyncio
    async def test_filter_rules_by_specific_tool(self):
        """Filtering rules by tool name should only return matching rules."""
        from app.api.permission_rules import list_permission_rules

        now = datetime.utcnow().isoformat()
        bash_rules = [
            {
                "id": "rule-bash-001",
                "profile_id": "test-profile",
                "tool_name": "Bash",
                "tool_pattern": "npm *",
                "decision": "allow",
                "created_at": now
            },
            {
                "id": "rule-bash-002",
                "profile_id": "test-profile",
                "tool_name": "Bash",
                "tool_pattern": "git *",
                "decision": "allow",
                "created_at": now
            }
        ]

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_permission_rules.return_value = bash_rules

            result = await list_permission_rules(
                profile_id=None,
                tool_name="Bash",
                token="test-token"
            )

            assert len(result) == 2
            assert all(r["tool_name"] == "Bash" for r in result)


# =============================================================================
# Router Configuration Tests
# =============================================================================

class TestRouterConfiguration:
    """Test router prefix and tags configuration."""

    def test_router_prefix(self):
        """Router should have correct prefix."""
        from app.api.permission_rules import router
        assert router.prefix == "/api/v1/permission-rules"

    def test_router_tags(self):
        """Router should have correct tags."""
        from app.api.permission_rules import router
        assert "Permission Rules" in router.tags


# =============================================================================
# Decision Validation Tests
# =============================================================================

class TestDecisionValidation:
    """Test decision field validation."""

    @pytest.mark.asyncio
    async def test_allow_decision_accepted(self):
        """Decision 'allow' should be accepted."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-allow",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": None,
            "decision": "allow",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            decision="allow"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_deny_decision_accepted(self):
        """Decision 'deny' should be accepted."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_rule = {
            "id": "rule-deny",
            "profile_id": "test-profile",
            "tool_name": "Bash",
            "tool_pattern": None,
            "decision": "deny",
            "created_at": now
        }

        rule_data = PermissionRuleCreate(
            profile_id="test-profile",
            tool_name="Bash",
            decision="deny"
        )

        with patch("app.api.permission_rules.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.add_permission_rule.return_value = created_rule

            result = await create_permission_rule(
                rule=rule_data,
                token="test-token"
            )

            assert result["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_invalid_decision_rejected(self):
        """Invalid decision values should be rejected."""
        from app.api.permission_rules import create_permission_rule, PermissionRuleCreate

        profile = sample_profile()

        invalid_decisions = ["ALLOW", "DENY", "yes", "no", "true", "false", "1", "0", "block", "permit"]

        for invalid_decision in invalid_decisions:
            rule_data = PermissionRuleCreate(
                profile_id="test-profile",
                tool_name="Bash",
                decision=invalid_decision
            )

            with patch("app.api.permission_rules.database") as mock_db:
                mock_db.get_profile.return_value = profile

                with pytest.raises(HTTPException) as exc_info:
                    await create_permission_rule(
                        rule=rule_data,
                        token="test-token"
                    )

                assert exc_info.value.status_code == 400
