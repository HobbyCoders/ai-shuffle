"""
Comprehensive tests for the profiles API endpoints.

Tests cover:
- Profile CRUD operations (list, get, create, update, delete)
- Profile agents management (list, create, update, delete)
- Enabled agents management (get, update, enable, disable)
- Profile export/import functionality
- Authentication requirements (auth vs admin)
- API user access restrictions
- Error handling and validation
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import Request


class TestProfilesModuleImports:
    """Verify profiles module can be imported correctly."""

    def test_profiles_module_imports(self):
        """Profiles module should import without errors."""
        from app.api import profiles
        assert profiles is not None

    def test_profiles_router_exists(self):
        """Profiles router should exist."""
        from app.api.profiles import router
        assert router is not None

    def test_subagent_response_model_exists(self):
        """SubagentResponse model should exist."""
        from app.api.profiles import SubagentResponse
        assert SubagentResponse is not None

    def test_enabled_agents_request_model_exists(self):
        """EnabledAgentsRequest model should exist."""
        from app.api.profiles import EnabledAgentsRequest
        assert EnabledAgentsRequest is not None

    def test_profile_subagent_request_model_exists(self):
        """ProfileSubagentRequest model should exist."""
        from app.api.profiles import ProfileSubagentRequest
        assert ProfileSubagentRequest is not None


class TestRequestResponseModels:
    """Test Pydantic models for request/response validation."""

    def test_subagent_response_valid(self):
        """SubagentResponse should accept valid data."""
        from app.api.profiles import SubagentResponse

        now = datetime.utcnow()
        response = SubagentResponse(
            id="test-agent",
            name="Test Agent",
            description="A test agent",
            prompt="You are a test agent",
            tools=["Bash", "Read"],
            model="sonnet",
            is_builtin=False,
            created_at=now,
            updated_at=now
        )
        assert response.id == "test-agent"
        assert response.name == "Test Agent"
        assert response.tools == ["Bash", "Read"]

    def test_subagent_response_minimal(self):
        """SubagentResponse should accept minimal data."""
        from app.api.profiles import SubagentResponse

        now = datetime.utcnow()
        response = SubagentResponse(
            id="minimal-agent",
            name="Minimal",
            description="Minimal description",
            prompt="Minimal prompt",
            created_at=now,
            updated_at=now
        )
        assert response.tools is None
        assert response.model is None
        assert response.is_builtin is False

    def test_enabled_agents_request_valid(self):
        """EnabledAgentsRequest should accept valid data."""
        from app.api.profiles import EnabledAgentsRequest

        request = EnabledAgentsRequest(
            enabled_agents=["agent-1", "agent-2", "agent-3"]
        )
        assert len(request.enabled_agents) == 3

    def test_enabled_agents_request_empty(self):
        """EnabledAgentsRequest should accept empty list."""
        from app.api.profiles import EnabledAgentsRequest

        request = EnabledAgentsRequest(enabled_agents=[])
        assert request.enabled_agents == []

    def test_profile_subagent_request_valid(self):
        """ProfileSubagentRequest should accept valid data."""
        from app.api.profiles import ProfileSubagentRequest

        request = ProfileSubagentRequest(
            name="New Agent",
            description="Agent description",
            prompt="Agent prompt",
            tools=["Bash"],
            model="opus"
        )
        assert request.name == "New Agent"
        assert request.model == "opus"

    def test_profile_subagent_request_minimal(self):
        """ProfileSubagentRequest should accept minimal data for updates."""
        from app.api.profiles import ProfileSubagentRequest

        request = ProfileSubagentRequest(
            description="Updated description",
            prompt="Updated prompt"
        )
        assert request.name is None
        assert request.tools is None


# =============================================================================
# Helper to create mock request
# =============================================================================

def create_mock_request(api_user=None):
    """Create a mock FastAPI Request object."""
    mock_request = MagicMock(spec=Request)
    mock_request.state = MagicMock()
    mock_request.state.api_user = api_user
    return mock_request


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
            "enabled_agents": ["agent-1", "agent-2"]
        },
        "is_builtin": False,
        "created_at": now,
        "updated_at": now
    }


def sample_builtin_profile():
    """Create a sample builtin profile for testing."""
    now = datetime.utcnow().isoformat()
    return {
        "id": "claude-code",
        "name": "Claude Code",
        "description": "Default coding assistant",
        "config": {
            "model": "sonnet",
            "enabled_agents": []
        },
        "is_builtin": True,
        "created_at": now,
        "updated_at": now
    }


def sample_subagent():
    """Create a sample subagent for testing."""
    now = datetime.utcnow().isoformat()
    return {
        "id": "test-subagent",
        "name": "Test Subagent",
        "description": "A test subagent",
        "prompt": "You are a test subagent",
        "tools": ["Bash", "Read"],
        "model": "sonnet",
        "is_builtin": False,
        "created_at": now,
        "updated_at": now
    }


# =============================================================================
# List Profiles Tests
# =============================================================================

class TestListProfilesEndpoint:
    """Test GET /api/v1/profiles endpoint."""

    @pytest.mark.asyncio
    async def test_list_profiles_success(self):
        """Listing profiles should return all profiles."""
        from app.api.profiles import list_profiles

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None  # Admin user
                mock_db.get_all_profiles.return_value = [profile]

                result = await list_profiles(mock_request, token="test-token")

                assert len(result) == 1
                assert result[0]["id"] == "test-profile"
                mock_db.get_all_profiles.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_profiles_empty(self):
        """Listing profiles when none exist should return empty list."""
        from app.api.profiles import list_profiles

        mock_request = create_mock_request()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_all_profiles.return_value = []

                result = await list_profiles(mock_request, token="test-token")

                assert result == []

    @pytest.mark.asyncio
    async def test_list_profiles_api_user_restricted(self):
        """API user with profile_ids should only see allowed profiles."""
        from app.api.profiles import list_profiles

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_ids": ["test-profile", "other-profile"]
                }
                mock_db.get_profile.side_effect = lambda pid: profile if pid == "test-profile" else None

                result = await list_profiles(mock_request, token="test-token")

                # Only test-profile exists, other-profile returns None
                assert len(result) == 1
                assert result[0]["id"] == "test-profile"

    @pytest.mark.asyncio
    async def test_list_profiles_api_user_unrestricted(self):
        """API user without profile_ids should see all profiles."""
        from app.api.profiles import list_profiles

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_ids": None
                }
                mock_db.get_all_profiles.return_value = [profile]

                result = await list_profiles(mock_request, token="test-token")

                assert len(result) == 1


# =============================================================================
# Get Profile Tests
# =============================================================================

class TestGetProfileEndpoint:
    """Test GET /api/v1/profiles/{profile_id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_profile_success(self):
        """Getting a profile should return profile details."""
        from app.api.profiles import get_profile

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await get_profile(mock_request, "test-profile", token="test-token")

                assert result["id"] == "test-profile"
                assert result["name"] == "Test Profile"

    @pytest.mark.asyncio
    async def test_get_profile_not_found(self):
        """Getting a non-existent profile should raise 404."""
        from app.api.profiles import get_profile
        from fastapi import HTTPException

        mock_request = create_mock_request()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await get_profile(mock_request, "nonexistent", token="test-token")

                assert exc_info.value.status_code == 404
                assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_profile_api_user_forbidden(self):
        """API user should not access profiles not in their list."""
        from app.api.profiles import get_profile
        from fastapi import HTTPException

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_ids": ["other-profile"]  # Does not include test-profile
                }
                mock_db.get_profile.return_value = profile

                with pytest.raises(HTTPException) as exc_info:
                    await get_profile(mock_request, "test-profile", token="test-token")

                assert exc_info.value.status_code == 403
                assert "access denied" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_profile_api_user_allowed(self):
        """API user should access profiles in their list."""
        from app.api.profiles import get_profile

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_ids": ["test-profile"]
                }
                mock_db.get_profile.return_value = profile

                result = await get_profile(mock_request, "test-profile", token="test-token")

                assert result["id"] == "test-profile"


# =============================================================================
# Create Profile Tests
# =============================================================================

class TestCreateProfileEndpoint:
    """Test POST /api/v1/profiles endpoint."""

    @pytest.mark.asyncio
    async def test_create_profile_success(self):
        """Creating a profile should return the new profile."""
        from app.api.profiles import create_profile
        from app.core.models import ProfileCreate, ProfileConfig

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "new-profile",
            "name": "New Profile",
            "description": "A new profile",
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None  # Doesn't exist
            mock_db.create_profile.return_value = created_profile

            request = ProfileCreate(
                id="new-profile",
                name="New Profile",
                description="A new profile",
                config=ProfileConfig(model="sonnet")
            )

            result = await create_profile(request, token="test-token")

            assert result["id"] == "new-profile"
            assert result["name"] == "New Profile"
            mock_db.create_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_profile_conflict(self):
        """Creating a profile with existing ID should raise 409."""
        from app.api.profiles import create_profile
        from app.core.models import ProfileCreate, ProfileConfig
        from fastapi import HTTPException

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile  # Already exists

            request = ProfileCreate(
                id="test-profile",
                name="Duplicate Profile",
                config=ProfileConfig(model="sonnet")
            )

            with pytest.raises(HTTPException) as exc_info:
                await create_profile(request, token="test-token")

            assert exc_info.value.status_code == 409
            assert "already exists" in exc_info.value.detail.lower()


# =============================================================================
# Update Profile Tests
# =============================================================================

class TestUpdateProfileEndpoint:
    """Test PUT /api/v1/profiles/{profile_id} endpoint."""

    @pytest.mark.asyncio
    async def test_update_profile_success(self):
        """Updating a profile should return updated profile."""
        from app.api.profiles import update_profile
        from app.core.models import ProfileUpdate

        profile = sample_profile()
        updated_profile = {**profile, "name": "Updated Name"}

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.update_profile.return_value = updated_profile

            request = ProfileUpdate(name="Updated Name")
            result = await update_profile("test-profile", request, token="test-token")

            assert result["name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_profile_not_found(self):
        """Updating a non-existent profile should raise 404."""
        from app.api.profiles import update_profile
        from app.core.models import ProfileUpdate
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            request = ProfileUpdate(name="Updated Name")

            with pytest.raises(HTTPException) as exc_info:
                await update_profile("nonexistent", request, token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_profile_with_config(self):
        """Updating profile config should work."""
        from app.api.profiles import update_profile
        from app.core.models import ProfileUpdate, ProfileConfig

        profile = sample_profile()
        updated_profile = {
            **profile,
            "config": {"model": "opus", "max_turns": 10}
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.update_profile.return_value = updated_profile

            request = ProfileUpdate(config=ProfileConfig(model="opus", max_turns=10))
            result = await update_profile("test-profile", request, token="test-token")

            assert result["config"]["model"] == "opus"

    @pytest.mark.asyncio
    async def test_update_builtin_profile(self):
        """Updating a builtin profile should work (allow_builtin=True)."""
        from app.api.profiles import update_profile
        from app.core.models import ProfileUpdate

        profile = sample_builtin_profile()
        updated_profile = {**profile, "name": "Updated Claude Code"}

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.update_profile.return_value = updated_profile

            request = ProfileUpdate(name="Updated Claude Code")
            result = await update_profile("claude-code", request, token="test-token")

            assert result["name"] == "Updated Claude Code"
            # Verify allow_builtin was passed
            call_kwargs = mock_db.update_profile.call_args[1]
            assert call_kwargs["allow_builtin"] is True


# =============================================================================
# Delete Profile Tests
# =============================================================================

class TestDeleteProfileEndpoint:
    """Test DELETE /api/v1/profiles/{profile_id} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_profile_success(self):
        """Deleting a profile should return None (204)."""
        from app.api.profiles import delete_profile

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.delete_profile.return_value = True

            result = await delete_profile("test-profile", token="test-token")

            assert result is None
            mock_db.delete_profile.assert_called_once_with("test-profile")

    @pytest.mark.asyncio
    async def test_delete_profile_not_found(self):
        """Deleting a non-existent profile should raise 404."""
        from app.api.profiles import delete_profile
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_profile("nonexistent", token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Get Profile Agents Tests
# =============================================================================

class TestGetProfileAgentsEndpoint:
    """Test GET /api/v1/profiles/{profile_id}/agents endpoint."""

    @pytest.mark.asyncio
    async def test_get_profile_agents_success(self):
        """Getting profile agents should return full subagent data."""
        from app.api.profiles import get_profile_agents

        mock_request = create_mock_request()
        profile = sample_profile()
        subagent = sample_subagent()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile
                mock_db.get_subagent.side_effect = lambda aid: subagent if aid == "agent-1" else None

                result = await get_profile_agents(mock_request, "test-profile", token="test-token")

                # Only agent-1 was found
                assert len(result) == 1
                assert result[0].id == "test-subagent"

    @pytest.mark.asyncio
    async def test_get_profile_agents_empty(self):
        """Getting profile agents for profile with no agents should return empty."""
        from app.api.profiles import get_profile_agents

        mock_request = create_mock_request()
        profile = {
            "id": "empty-profile",
            "name": "Empty",
            "config": {},  # No enabled_agents
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await get_profile_agents(mock_request, "empty-profile", token="test-token")

                assert result == []

    @pytest.mark.asyncio
    async def test_get_profile_agents_not_found(self):
        """Getting agents for non-existent profile should raise 404."""
        from app.api.profiles import get_profile_agents
        from fastapi import HTTPException

        mock_request = create_mock_request()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await get_profile_agents(mock_request, "nonexistent", token="test-token")

                assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_profile_agents_api_user_restricted(self):
        """API user with profile_id restriction should be denied."""
        from app.api.profiles import get_profile_agents
        from fastapi import HTTPException

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_id": "other-profile"  # Different profile
                }
                mock_db.get_profile.return_value = profile

                with pytest.raises(HTTPException) as exc_info:
                    await get_profile_agents(mock_request, "test-profile", token="test-token")

                assert exc_info.value.status_code == 403


# =============================================================================
# Create Profile Agent Tests
# =============================================================================

class TestCreateProfileAgentEndpoint:
    """Test POST /api/v1/profiles/{profile_id}/agents endpoint."""

    @pytest.mark.asyncio
    async def test_create_profile_agent_success(self):
        """Creating a profile agent should return the new subagent."""
        from app.api.profiles import create_profile_agent, ProfileSubagentRequest

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_subagent = {
            "id": "new-agent",
            "name": "New Agent",
            "description": "A new agent",
            "prompt": "You are a new agent",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = None  # Doesn't exist yet
            mock_db.create_subagent.return_value = created_subagent
            mock_db.update_profile.return_value = profile

            request = ProfileSubagentRequest(
                name="New Agent",
                description="A new agent",
                prompt="You are a new agent"
            )

            result = await create_profile_agent("test-profile", request, token="test-token")

            assert result.name == "New Agent"

    @pytest.mark.asyncio
    async def test_create_profile_agent_no_name(self):
        """Creating a profile agent without name should fail."""
        from app.api.profiles import create_profile_agent, ProfileSubagentRequest
        from fastapi import HTTPException

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile

            request = ProfileSubagentRequest(
                description="A new agent",
                prompt="You are a new agent"
            )

            with pytest.raises(HTTPException) as exc_info:
                await create_profile_agent("test-profile", request, token="test-token")

            assert exc_info.value.status_code == 400
            assert "name is required" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_create_profile_agent_conflict(self):
        """Creating a profile agent with existing ID should fail."""
        from app.api.profiles import create_profile_agent, ProfileSubagentRequest
        from fastapi import HTTPException

        profile = sample_profile()
        subagent = sample_subagent()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = subagent  # Already exists

            request = ProfileSubagentRequest(
                name="Test Subagent",  # Will generate id "test-subagent"
                description="A new agent",
                prompt="You are a new agent"
            )

            with pytest.raises(HTTPException) as exc_info:
                await create_profile_agent("test-profile", request, token="test-token")

            assert exc_info.value.status_code == 409
            assert "already exists" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_create_profile_agent_profile_not_found(self):
        """Creating an agent for non-existent profile should fail."""
        from app.api.profiles import create_profile_agent, ProfileSubagentRequest
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            request = ProfileSubagentRequest(
                name="New Agent",
                description="A new agent",
                prompt="You are a new agent"
            )

            with pytest.raises(HTTPException) as exc_info:
                await create_profile_agent("nonexistent", request, token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Update Profile Agent Tests
# =============================================================================

class TestUpdateProfileAgentEndpoint:
    """Test PUT /api/v1/profiles/{profile_id}/agents/{agent_name} endpoint."""

    @pytest.mark.asyncio
    async def test_update_profile_agent_by_id(self):
        """Updating a profile agent by ID should work."""
        from app.api.profiles import update_profile_agent, ProfileSubagentRequest

        profile = sample_profile()
        subagent = sample_subagent()
        updated_subagent = {**subagent, "description": "Updated description"}

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = subagent
            mock_db.update_subagent.return_value = updated_subagent

            request = ProfileSubagentRequest(
                description="Updated description",
                prompt="Updated prompt"
            )

            result = await update_profile_agent("test-profile", "test-subagent", request, token="test-token")

            assert result.description == "Updated description"

    @pytest.mark.asyncio
    async def test_update_profile_agent_by_name(self):
        """Updating a profile agent by name should work."""
        from app.api.profiles import update_profile_agent, ProfileSubagentRequest

        profile_with_agent = {
            "id": "test-profile",
            "name": "Test Profile",
            "config": {"enabled_agents": ["test-subagent"]},
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        subagent = sample_subagent()
        updated_subagent = {**subagent, "description": "Updated description"}

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile_with_agent
            # First call (by name) returns None, second call (by ID from enabled_agents) returns subagent
            mock_db.get_subagent.side_effect = lambda aid: subagent if aid == "test-subagent" else None
            mock_db.update_subagent.return_value = updated_subagent

            request = ProfileSubagentRequest(
                description="Updated description",
                prompt="Updated prompt"
            )

            result = await update_profile_agent("test-profile", "Test Subagent", request, token="test-token")

            assert result.description == "Updated description"

    @pytest.mark.asyncio
    async def test_update_profile_agent_not_found(self):
        """Updating a non-existent agent should raise 404."""
        from app.api.profiles import update_profile_agent, ProfileSubagentRequest
        from fastapi import HTTPException

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = None

            request = ProfileSubagentRequest(
                description="Updated description",
                prompt="Updated prompt"
            )

            with pytest.raises(HTTPException) as exc_info:
                await update_profile_agent("test-profile", "nonexistent", request, token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_profile_agent_profile_not_found(self):
        """Updating an agent for non-existent profile should fail."""
        from app.api.profiles import update_profile_agent, ProfileSubagentRequest
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            request = ProfileSubagentRequest(
                description="Updated description",
                prompt="Updated prompt"
            )

            with pytest.raises(HTTPException) as exc_info:
                await update_profile_agent("nonexistent", "some-agent", request, token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Delete Profile Agent Tests
# =============================================================================

class TestDeleteProfileAgentEndpoint:
    """Test DELETE /api/v1/profiles/{profile_id}/agents/{agent_name} endpoint."""

    @pytest.mark.asyncio
    async def test_delete_profile_agent_success(self):
        """Deleting a profile agent should return None (204)."""
        from app.api.profiles import delete_profile_agent

        profile_with_agent = {
            "id": "test-profile",
            "name": "Test Profile",
            "config": {"enabled_agents": ["test-subagent"]},
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        subagent = sample_subagent()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile_with_agent
            mock_db.get_subagent.return_value = subagent
            mock_db.update_profile.return_value = profile_with_agent
            mock_db.delete_subagent.return_value = True

            result = await delete_profile_agent("test-profile", "test-subagent", token="test-token")

            assert result is None
            mock_db.delete_subagent.assert_called_once_with("test-subagent")

    @pytest.mark.asyncio
    async def test_delete_profile_agent_removes_from_enabled(self):
        """Deleting a profile agent should remove it from enabled_agents."""
        from app.api.profiles import delete_profile_agent

        profile_with_agent = {
            "id": "test-profile",
            "name": "Test Profile",
            "config": {"enabled_agents": ["test-subagent", "other-agent"]},
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        subagent = sample_subagent()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile_with_agent
            mock_db.get_subagent.return_value = subagent
            mock_db.update_profile.return_value = profile_with_agent
            mock_db.delete_subagent.return_value = True

            await delete_profile_agent("test-profile", "test-subagent", token="test-token")

            # Verify update_profile was called with agent removed
            call_kwargs = mock_db.update_profile.call_args[1]
            assert "test-subagent" not in call_kwargs["config"]["enabled_agents"]

    @pytest.mark.asyncio
    async def test_delete_profile_agent_not_found(self):
        """Deleting a non-existent agent should raise 404."""
        from app.api.profiles import delete_profile_agent
        from fastapi import HTTPException

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_profile_agent("test-profile", "nonexistent", token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_profile_agent_profile_not_found(self):
        """Deleting an agent for non-existent profile should fail."""
        from app.api.profiles import delete_profile_agent
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_profile_agent("nonexistent", "some-agent", token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Get Enabled Agents Tests
# =============================================================================

class TestGetEnabledAgentsEndpoint:
    """Test GET /api/v1/profiles/{profile_id}/enabled-agents endpoint."""

    @pytest.mark.asyncio
    async def test_get_enabled_agents_success(self):
        """Getting enabled agents should return list of IDs."""
        from app.api.profiles import get_enabled_agents

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await get_enabled_agents(mock_request, "test-profile", token="test-token")

                assert result == ["agent-1", "agent-2"]

    @pytest.mark.asyncio
    async def test_get_enabled_agents_empty(self):
        """Getting enabled agents for profile with no agents should return empty."""
        from app.api.profiles import get_enabled_agents

        mock_request = create_mock_request()
        profile = {
            "id": "empty-profile",
            "name": "Empty",
            "config": {},
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await get_enabled_agents(mock_request, "empty-profile", token="test-token")

                assert result == []

    @pytest.mark.asyncio
    async def test_get_enabled_agents_not_found(self):
        """Getting enabled agents for non-existent profile should raise 404."""
        from app.api.profiles import get_enabled_agents
        from fastapi import HTTPException

        mock_request = create_mock_request()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await get_enabled_agents(mock_request, "nonexistent", token="test-token")

                assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_enabled_agents_api_user_restricted(self):
        """API user with profile_id restriction should be denied."""
        from app.api.profiles import get_enabled_agents
        from fastapi import HTTPException

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_id": "other-profile"
                }
                mock_db.get_profile.return_value = profile

                with pytest.raises(HTTPException) as exc_info:
                    await get_enabled_agents(mock_request, "test-profile", token="test-token")

                assert exc_info.value.status_code == 403


# =============================================================================
# Update Enabled Agents Tests
# =============================================================================

class TestUpdateEnabledAgentsEndpoint:
    """Test PUT /api/v1/profiles/{profile_id}/enabled-agents endpoint."""

    @pytest.mark.asyncio
    async def test_update_enabled_agents_success(self):
        """Updating enabled agents should work."""
        from app.api.profiles import update_enabled_agents, EnabledAgentsRequest

        profile = sample_profile()
        subagents = [
            {"id": "agent-3", "name": "Agent 3"},
            {"id": "agent-4", "name": "Agent 4"}
        ]

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_all_subagents.return_value = subagents
            mock_db.update_profile.return_value = profile

            request = EnabledAgentsRequest(enabled_agents=["agent-3", "agent-4"])
            result = await update_enabled_agents("test-profile", request, token="test-token")

            assert result == ["agent-3", "agent-4"]

    @pytest.mark.asyncio
    async def test_update_enabled_agents_invalid_ids(self):
        """Updating with invalid agent IDs should fail."""
        from app.api.profiles import update_enabled_agents, EnabledAgentsRequest
        from fastapi import HTTPException

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_all_subagents.return_value = [{"id": "valid-agent"}]

            request = EnabledAgentsRequest(enabled_agents=["valid-agent", "invalid-agent"])

            with pytest.raises(HTTPException) as exc_info:
                await update_enabled_agents("test-profile", request, token="test-token")

            assert exc_info.value.status_code == 400
            assert "invalid-agent" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_update_enabled_agents_empty(self):
        """Updating with empty list should work."""
        from app.api.profiles import update_enabled_agents, EnabledAgentsRequest

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_all_subagents.return_value = []
            mock_db.update_profile.return_value = profile

            request = EnabledAgentsRequest(enabled_agents=[])
            result = await update_enabled_agents("test-profile", request, token="test-token")

            assert result == []

    @pytest.mark.asyncio
    async def test_update_enabled_agents_not_found(self):
        """Updating enabled agents for non-existent profile should fail."""
        from app.api.profiles import update_enabled_agents, EnabledAgentsRequest
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            request = EnabledAgentsRequest(enabled_agents=["agent-1"])

            with pytest.raises(HTTPException) as exc_info:
                await update_enabled_agents("nonexistent", request, token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Enable Subagent Tests
# =============================================================================

class TestEnableSubagentEndpoint:
    """Test POST /api/v1/profiles/{profile_id}/enabled-agents/{subagent_id} endpoint."""

    @pytest.mark.asyncio
    async def test_enable_subagent_success(self):
        """Enabling a subagent should return success."""
        from app.api.profiles import enable_subagent

        profile = sample_profile()
        subagent = sample_subagent()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = subagent
            mock_db.update_profile.return_value = profile

            result = await enable_subagent("test-profile", "test-subagent", token="test-token")

            assert result["enabled"] is True
            assert result["subagent_id"] == "test-subagent"

    @pytest.mark.asyncio
    async def test_enable_subagent_already_enabled(self):
        """Enabling an already enabled subagent should succeed without update."""
        from app.api.profiles import enable_subagent

        profile = {
            "id": "test-profile",
            "name": "Test Profile",
            "config": {"enabled_agents": ["test-subagent"]},  # Already enabled
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        subagent = sample_subagent()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = subagent

            result = await enable_subagent("test-profile", "test-subagent", token="test-token")

            assert result["enabled"] is True
            # update_profile should not be called if already enabled
            mock_db.update_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_subagent_not_found(self):
        """Enabling a non-existent subagent should raise 404."""
        from app.api.profiles import enable_subagent
        from fastapi import HTTPException

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await enable_subagent("test-profile", "nonexistent", token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_enable_subagent_profile_not_found(self):
        """Enabling subagent for non-existent profile should fail."""
        from app.api.profiles import enable_subagent
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await enable_subagent("nonexistent", "some-agent", token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Disable Subagent Tests
# =============================================================================

class TestDisableSubagentEndpoint:
    """Test DELETE /api/v1/profiles/{profile_id}/enabled-agents/{subagent_id} endpoint."""

    @pytest.mark.asyncio
    async def test_disable_subagent_success(self):
        """Disabling a subagent should return success."""
        from app.api.profiles import disable_subagent

        profile = {
            "id": "test-profile",
            "name": "Test Profile",
            "config": {"enabled_agents": ["test-subagent", "other-agent"]},
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.update_profile.return_value = profile

            result = await disable_subagent("test-profile", "test-subagent", token="test-token")

            assert result["enabled"] is False
            assert result["subagent_id"] == "test-subagent"

    @pytest.mark.asyncio
    async def test_disable_subagent_not_enabled(self):
        """Disabling a non-enabled subagent should succeed without update."""
        from app.api.profiles import disable_subagent

        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile

            result = await disable_subagent("test-profile", "not-enabled-agent", token="test-token")

            assert result["enabled"] is False
            # update_profile should not be called if not in list
            mock_db.update_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_subagent_profile_not_found(self):
        """Disabling subagent for non-existent profile should fail."""
        from app.api.profiles import disable_subagent
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await disable_subagent("nonexistent", "some-agent", token="test-token")

            assert exc_info.value.status_code == 404


# =============================================================================
# Export Profile Tests
# =============================================================================

class TestExportProfileEndpoint:
    """Test GET /api/v1/profiles/{profile_id}/export endpoint."""

    @pytest.mark.asyncio
    async def test_export_profile_success(self):
        """Exporting a profile should return agent export data."""
        from app.api.profiles import export_profile

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await export_profile(mock_request, "test-profile", token="test-token")

                # Result is a JSONResponse, check the body
                import json
                body = json.loads(result.body)
                assert body["version"] == "1.0"
                assert body["type"] == "ai-shuffle-agent"
                assert body["agent"]["name"] == "Test Profile"

    @pytest.mark.asyncio
    async def test_export_profile_with_system_prompt(self):
        """Exporting a profile with system prompt should include it."""
        from app.api.profiles import export_profile

        mock_request = create_mock_request()
        profile = {
            "id": "custom-profile",
            "name": "Custom Profile",
            "description": "A custom profile",
            "config": {
                "model": "opus",
                "system_prompt": {
                    "type": "custom",
                    "content": "You are a custom assistant",
                    "inject_env_details": True
                },
                "allowed_tools": ["Bash", "Read"],
                "enabled_agents": ["agent-1"]
            },
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await export_profile(mock_request, "custom-profile", token="test-token")

                import json
                body = json.loads(result.body)
                assert body["agent"]["system_prompt_type"] == "custom"
                assert body["agent"]["system_prompt_content"] == "You are a custom assistant"
                assert body["agent"]["allowed_tools"] == ["Bash", "Read"]

    @pytest.mark.asyncio
    async def test_export_profile_not_found(self):
        """Exporting a non-existent profile should raise 404."""
        from app.api.profiles import export_profile
        from fastapi import HTTPException

        mock_request = create_mock_request()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = None

                with pytest.raises(HTTPException) as exc_info:
                    await export_profile(mock_request, "nonexistent", token="test-token")

                assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_export_profile_api_user_restricted(self):
        """API user with profile_id restriction should be denied."""
        from app.api.profiles import export_profile
        from fastapi import HTTPException

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "api-user-1",
                    "profile_id": "other-profile"
                }
                mock_db.get_profile.return_value = profile

                with pytest.raises(HTTPException) as exc_info:
                    await export_profile(mock_request, "test-profile", token="test-token")

                assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_export_profile_has_content_disposition(self):
        """Exported profile should have Content-Disposition header."""
        from app.api.profiles import export_profile

        mock_request = create_mock_request()
        profile = sample_profile()

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await export_profile(mock_request, "test-profile", token="test-token")

                assert "content-disposition" in result.headers
                assert "test-profile-agent.json" in result.headers["content-disposition"]


# =============================================================================
# Import Profile Tests
# =============================================================================

class TestImportProfileEndpoint:
    """Test POST /api/v1/profiles/import endpoint."""

    @pytest.mark.asyncio
    async def test_import_profile_success(self):
        """Importing a profile should return the new profile."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "imported-agent",
            "name": "Imported Agent",
            "description": "An imported agent",
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None  # ID doesn't exist
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(
                    name="Imported Agent",
                    description="An imported agent",
                    model="sonnet"
                )
            )

            result = await import_profile(request, token="test-token")

            assert result["name"] == "Imported Agent"

    @pytest.mark.asyncio
    async def test_import_profile_with_new_id(self):
        """Importing with new_id should use that ID."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "custom-id",
            "name": "Imported Agent",
            "description": None,
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                new_id="custom-id",
                agent=AgentExportData(name="Imported Agent")
            )

            result = await import_profile(request, token="test-token")

            mock_db.create_profile.assert_called_once()
            call_kwargs = mock_db.create_profile.call_args[1]
            assert call_kwargs["profile_id"] == "custom-id"

    @pytest.mark.asyncio
    async def test_import_profile_id_increment(self):
        """Importing with conflicting ID should increment."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "imported-agent-1",
            "name": "Imported Agent",
            "description": None,
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        call_count = [0]

        def mock_get_profile(profile_id):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"id": "imported-agent"}  # First call returns existing
            return None  # Second call returns None

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.side_effect = mock_get_profile
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(name="Imported Agent")
            )

            result = await import_profile(request, token="test-token")

            # Verify the profile was created with incremented ID
            call_kwargs = mock_db.create_profile.call_args[1]
            assert call_kwargs["profile_id"] == "imported-agent-1"

    @pytest.mark.asyncio
    async def test_import_profile_invalid_type(self):
        """Importing with invalid type should fail."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData
        from fastapi import HTTPException

        request = AgentImportRequest(
            version="1.0",
            type="invalid-type",
            agent=AgentExportData(name="Imported Agent")
        )

        with pytest.raises(HTTPException) as exc_info:
            await import_profile(request, token="test-token")

        assert exc_info.value.status_code == 400
        assert "invalid export type" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_import_profile_unsupported_version(self):
        """Importing with unsupported version should fail."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData
        from fastapi import HTTPException

        request = AgentImportRequest(
            version="99.0",
            type="ai-shuffle-agent",
            agent=AgentExportData(name="Imported Agent")
        )

        with pytest.raises(HTTPException) as exc_info:
            await import_profile(request, token="test-token")

        assert exc_info.value.status_code == 400
        assert "unsupported export version" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_import_profile_empty_name(self):
        """Importing with empty agent name should fail."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData
        from fastapi import HTTPException

        # AgentExportData.name is required and can't be None.
        # Test with empty string which should trigger "name is required" check
        request = AgentImportRequest(
            version="1.0",
            type="ai-shuffle-agent",
            agent=AgentExportData(name="", description="Empty name agent")
        )

        with pytest.raises(HTTPException) as exc_info:
            await import_profile(request, token="test-token")

        assert exc_info.value.status_code == 400
        assert "name is required" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_import_profile_with_custom_system_prompt(self):
        """Importing with custom system prompt should work."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "custom-prompt-agent",
            "name": "Custom Prompt Agent",
            "description": None,
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(
                    name="Custom Prompt Agent",
                    system_prompt_type="custom",
                    system_prompt_content="You are a custom agent",
                    system_prompt_inject_env=True
                )
            )

            await import_profile(request, token="test-token")

            call_kwargs = mock_db.create_profile.call_args[1]
            config = call_kwargs["config"]
            assert config["system_prompt"]["type"] == "custom"
            assert config["system_prompt"]["content"] == "You are a custom agent"

    @pytest.mark.asyncio
    async def test_import_profile_with_preset_and_append(self):
        """Importing with preset system prompt and append should work."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "preset-append-agent",
            "name": "Preset Append Agent",
            "description": None,
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(
                    name="Preset Append Agent",
                    system_prompt_type="preset",
                    system_prompt_preset="claude_code",
                    system_prompt_append="Extra instructions here"
                )
            )

            await import_profile(request, token="test-token")

            call_kwargs = mock_db.create_profile.call_args[1]
            config = call_kwargs["config"]
            assert config["system_prompt"]["type"] == "preset"
            assert config["system_prompt"]["append"] == "Extra instructions here"

    @pytest.mark.asyncio
    async def test_import_profile_too_many_conflicts(self):
        """Importing when too many similar IDs exist should fail."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData
        from fastapi import HTTPException

        with patch("app.api.profiles.database") as mock_db:
            # Always return an existing profile
            mock_db.get_profile.return_value = {"id": "exists"}

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(name="Conflict Agent")
            )

            with pytest.raises(HTTPException) as exc_info:
                await import_profile(request, token="test-token")

            assert exc_info.value.status_code == 409
            assert "too many profiles" in exc_info.value.detail.lower()


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_profile_with_empty_config(self):
        """Profile with empty config should be handled."""
        from app.api.profiles import get_enabled_agents

        mock_request = create_mock_request()
        profile = {
            "id": "empty-config",
            "name": "Empty Config",
            "config": {},
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await get_enabled_agents(mock_request, "empty-config", token="test-token")

                assert result == []

    @pytest.mark.asyncio
    async def test_export_profile_with_none_system_prompt(self):
        """Exporting profile with None system_prompt should work."""
        from app.api.profiles import export_profile

        mock_request = create_mock_request()
        profile = {
            "id": "no-prompt",
            "name": "No Prompt",
            "description": None,
            "config": {
                "model": "sonnet",
                "system_prompt": None
            },
            "is_builtin": False,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }

        with patch("app.api.profiles.database") as mock_db:
            with patch("app.api.profiles.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = None
                mock_db.get_profile.return_value = profile

                result = await export_profile(mock_request, "no-prompt", token="test-token")

                import json
                body = json.loads(result.body)
                assert body["agent"]["system_prompt_type"] == "preset"

    @pytest.mark.asyncio
    async def test_create_agent_id_generation(self):
        """Agent ID should be properly generated from name."""
        from app.api.profiles import create_profile_agent, ProfileSubagentRequest

        profile = sample_profile()
        now = datetime.utcnow().isoformat()
        created_subagent = {
            "id": "my-new-agent",
            "name": "My_New Agent",
            "description": "Description",
            "prompt": "Prompt",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = created_subagent
            mock_db.update_profile.return_value = profile

            request = ProfileSubagentRequest(
                name="My_New Agent",  # Underscores and spaces
                description="Description",
                prompt="Prompt"
            )

            await create_profile_agent("test-profile", request, token="test-token")

            # Verify create_subagent was called with properly transformed ID
            call_kwargs = mock_db.create_subagent.call_args[1]
            assert call_kwargs["subagent_id"] == "my-new-agent"

    @pytest.mark.asyncio
    async def test_import_profile_special_characters_in_name(self):
        """Importing with special characters in name should sanitize ID."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "test-agent-2024",
            "name": "Test Agent @#$% 2024!",
            "description": None,
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(name="Test Agent @#$% 2024!")
            )

            await import_profile(request, token="test-token")

            # Verify ID was sanitized
            call_kwargs = mock_db.create_profile.call_args[1]
            # Should contain only lowercase letters, numbers, and hyphens
            assert all(c.isalnum() or c == '-' for c in call_kwargs["profile_id"])

    @pytest.mark.asyncio
    async def test_import_profile_with_all_optional_fields(self):
        """Importing with all optional fields should populate config correctly."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData, AIToolsConfig

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "full-agent",
            "name": "Full Agent",
            "description": "Agent with all fields",
            "config": {"model": "opus"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(
                    name="Full Agent",
                    description="Agent with all fields",
                    model="opus",
                    allowed_tools=["Bash", "Read", "Write"],
                    disallowed_tools=["WebFetch"],
                    enabled_agents=["subagent-1", "subagent-2"],
                    ai_tools=AIToolsConfig(image_generation=True, web_search=True),
                    max_turns=20,
                    max_buffer_size=50000,
                    setting_sources=["env", "file"]
                )
            )

            await import_profile(request, token="test-token")

            call_kwargs = mock_db.create_profile.call_args[1]
            config = call_kwargs["config"]
            assert config["allowed_tools"] == ["Bash", "Read", "Write"]
            assert config["disallowed_tools"] == ["WebFetch"]
            assert config["enabled_agents"] == ["subagent-1", "subagent-2"]
            assert config["ai_tools"]["image_generation"] is True
            assert config["max_turns"] == 20
            assert config["max_buffer_size"] == 50000
            assert config["setting_sources"] == ["env", "file"]

    @pytest.mark.asyncio
    async def test_import_profile_default_id_fallback(self):
        """Importing with name that sanitizes to empty should use default ID."""
        from app.api.profiles import import_profile
        from app.core.models import AgentImportRequest, AgentExportData

        now = datetime.utcnow().isoformat()
        created_profile = {
            "id": "imported-agent",
            "name": "@#$%^",
            "description": None,
            "config": {"model": "sonnet"},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = None
            mock_db.create_profile.return_value = created_profile

            request = AgentImportRequest(
                version="1.0",
                type="ai-shuffle-agent",
                agent=AgentExportData(name="@#$%^")  # Name with only special chars
            )

            await import_profile(request, token="test-token")

            call_kwargs = mock_db.create_profile.call_args[1]
            # Should use default ID since name sanitizes to empty
            assert call_kwargs["profile_id"] == "imported-agent"

    @pytest.mark.asyncio
    async def test_delete_profile_agent_by_name_match(self):
        """Deleting agent by name when it exists in enabled_agents should work."""
        from app.api.profiles import delete_profile_agent

        now = datetime.utcnow().isoformat()
        profile = {
            "id": "test-profile",
            "name": "Test Profile",
            "config": {"enabled_agents": ["subagent-123"]},
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }
        subagent = {
            "id": "subagent-123",
            "name": "My Subagent",  # Name doesn't match ID
            "description": "A subagent",
            "prompt": "Prompt",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": now,
            "updated_at": now
        }

        with patch("app.api.profiles.database") as mock_db:
            mock_db.get_profile.return_value = profile
            # First call for direct ID lookup returns None (agent_name doesn't match an ID)
            # Second call for ID from enabled_agents returns the subagent
            call_count = [0]

            def mock_get_subagent(agent_id):
                call_count[0] += 1
                if agent_id == "My Subagent":
                    return None  # Name lookup fails
                if agent_id == "subagent-123":
                    return subagent  # ID lookup succeeds
                return None

            mock_db.get_subagent.side_effect = mock_get_subagent
            mock_db.update_profile.return_value = profile
            mock_db.delete_subagent.return_value = True

            result = await delete_profile_agent("test-profile", "My Subagent", token="test-token")

            assert result is None
            # The subagent should be found by matching the name
            mock_db.delete_subagent.assert_called_once_with("subagent-123")
