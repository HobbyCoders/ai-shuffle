"""
Unit tests for Pydantic models.

Tests cover:
- Model validation
- Field constraints
- Default values
- Serialization
"""

import pytest
from pydantic import ValidationError

from app.core.models import (
    SetupRequest,
    LoginRequest,
    ChangePasswordRequest,
    ApiKeyLoginRequest,
    ApiUserRegisterRequest,
    ApiUserLoginRequest,
    ApiUserInfo,
    AuthStatus,
    SubagentDefinition,
    SystemPromptConfig,
    ProfileConfig,
    ProfileBase,
)


class TestSetupRequest:
    """Test SetupRequest model validation."""

    def test_valid_setup_request(self):
        """Should accept valid setup request."""
        req = SetupRequest(username="admin", password="password123")
        assert req.username == "admin"
        assert req.password == "password123"

    def test_username_min_length(self):
        """Username must be at least 3 characters."""
        with pytest.raises(ValidationError):
            SetupRequest(username="ab", password="password123")

    def test_username_max_length(self):
        """Username must be at most 50 characters."""
        with pytest.raises(ValidationError):
            SetupRequest(username="a" * 51, password="password123")

    def test_password_min_length(self):
        """Password must be at least 8 characters."""
        with pytest.raises(ValidationError):
            SetupRequest(username="admin", password="short")


class TestLoginRequest:
    """Test LoginRequest model."""

    def test_valid_login_request(self):
        """Should accept valid login request."""
        req = LoginRequest(username="admin", password="password")
        assert req.username == "admin"
        assert req.password == "password"

    def test_missing_username(self):
        """Should require username."""
        with pytest.raises(ValidationError):
            LoginRequest(password="password")

    def test_missing_password(self):
        """Should require password."""
        with pytest.raises(ValidationError):
            LoginRequest(username="admin")


class TestChangePasswordRequest:
    """Test ChangePasswordRequest model."""

    def test_valid_change_password(self):
        """Should accept valid password change."""
        req = ChangePasswordRequest(
            current_password="oldpass123",
            new_password="newpass123"
        )
        assert req.current_password == "oldpass123"
        assert req.new_password == "newpass123"

    def test_new_password_min_length(self):
        """New password must be at least 8 characters."""
        with pytest.raises(ValidationError):
            ChangePasswordRequest(
                current_password="oldpass123",
                new_password="short"
            )


class TestApiKeyLoginRequest:
    """Test ApiKeyLoginRequest model."""

    def test_valid_api_key_login(self):
        """Should accept valid API key login."""
        req = ApiKeyLoginRequest(api_key="sk-test-key-12345")
        assert req.api_key == "sk-test-key-12345"

    def test_missing_api_key(self):
        """Should require API key."""
        with pytest.raises(ValidationError):
            ApiKeyLoginRequest()


class TestApiUserRegisterRequest:
    """Test ApiUserRegisterRequest model."""

    def test_valid_registration(self):
        """Should accept valid registration."""
        req = ApiUserRegisterRequest(
            api_key="sk-test-key",
            username="newuser",
            password="password123"
        )
        assert req.api_key == "sk-test-key"
        assert req.username == "newuser"
        assert req.password == "password123"

    def test_username_min_length(self):
        """Username must be at least 3 characters."""
        with pytest.raises(ValidationError):
            ApiUserRegisterRequest(
                api_key="sk-test",
                username="ab",
                password="password123"
            )

    def test_password_min_length(self):
        """Password must be at least 8 characters."""
        with pytest.raises(ValidationError):
            ApiUserRegisterRequest(
                api_key="sk-test",
                username="user",
                password="short"
            )


class TestApiUserInfo:
    """Test ApiUserInfo model."""

    def test_minimal_api_user_info(self):
        """Should accept minimal required fields."""
        info = ApiUserInfo(id="user-123", name="Test User")
        assert info.id == "user-123"
        assert info.name == "Test User"
        assert info.project_id is None
        assert info.profile_ids is None

    def test_full_api_user_info(self):
        """Should accept all fields."""
        info = ApiUserInfo(
            id="user-123",
            name="Test User",
            project_id="proj-456",
            profile_ids=["profile-1", "profile-2"]
        )
        assert info.project_id == "proj-456"
        assert len(info.profile_ids) == 2


class TestAuthStatus:
    """Test AuthStatus model."""

    def test_minimal_auth_status(self):
        """Should work with minimal fields."""
        status = AuthStatus(
            authenticated=True,
            setup_required=False,
            claude_authenticated=True
        )
        assert status.authenticated is True
        assert status.is_admin is True  # Default
        assert status.username is None

    def test_full_auth_status(self):
        """Should accept all fields."""
        api_user = ApiUserInfo(id="user-1", name="API User")
        status = AuthStatus(
            authenticated=True,
            is_admin=False,
            setup_required=False,
            claude_authenticated=True,
            username="testuser",
            api_user=api_user
        )
        assert status.is_admin is False
        assert status.username == "testuser"
        assert status.api_user.name == "API User"


class TestSubagentDefinition:
    """Test SubagentDefinition model."""

    def test_valid_subagent(self):
        """Should accept valid subagent definition."""
        agent = SubagentDefinition(
            description="Helps with testing",
            prompt="You are a testing assistant"
        )
        assert agent.description == "Helps with testing"
        assert agent.prompt == "You are a testing assistant"
        assert agent.tools is None
        assert agent.model is None

    def test_subagent_with_tools(self):
        """Should accept tools list."""
        agent = SubagentDefinition(
            description="Code reviewer",
            prompt="Review code for issues",
            tools=["Read", "Grep", "Glob"]
        )
        assert agent.tools == ["Read", "Grep", "Glob"]

    def test_subagent_with_model(self):
        """Should accept model override."""
        agent = SubagentDefinition(
            description="Quick helper",
            prompt="Help quickly",
            model="haiku"
        )
        assert agent.model == "haiku"

    def test_description_required(self):
        """Description must not be empty."""
        with pytest.raises(ValidationError):
            SubagentDefinition(description="", prompt="Some prompt")

    def test_prompt_required(self):
        """Prompt must not be empty."""
        with pytest.raises(ValidationError):
            SubagentDefinition(description="Some description", prompt="")


class TestSystemPromptConfig:
    """Test SystemPromptConfig model."""

    def test_default_values(self):
        """Should have correct defaults."""
        config = SystemPromptConfig()
        assert config.type == "preset"
        assert config.preset == "claude_code"
        assert config.content is None
        assert config.append is None
        assert config.inject_env_details is False

    def test_custom_prompt(self):
        """Should accept custom prompt config."""
        config = SystemPromptConfig(
            type="custom",
            content="You are a specialized assistant",
            inject_env_details=True
        )
        assert config.type == "custom"
        assert config.content == "You are a specialized assistant"
        assert config.inject_env_details is True


class TestProfileConfig:
    """Test ProfileConfig model."""

    def test_default_values(self):
        """Should have correct defaults."""
        config = ProfileConfig()
        assert config.model == "sonnet"
        assert config.permission_mode == "default"
        assert config.max_turns is None
        assert config.allowed_tools is None
        assert config.include_partial_messages is False
        assert config.continue_conversation is False

    def test_custom_config(self):
        """Should accept custom configuration."""
        config = ProfileConfig(
            model="opus",
            permission_mode="bypassPermissions",
            max_turns=50,
            allowed_tools=["Read", "Write", "Bash"],
            disallowed_tools=["WebSearch"],
            cwd="/workspace/project"
        )
        assert config.model == "opus"
        assert config.permission_mode == "bypassPermissions"
        assert config.max_turns == 50
        assert "Read" in config.allowed_tools
        assert config.cwd == "/workspace/project"

    def test_config_with_env(self):
        """Should accept environment variables."""
        config = ProfileConfig(
            env={"API_KEY": "secret", "DEBUG": "true"}
        )
        assert config.env["API_KEY"] == "secret"

    def test_config_with_plugins(self):
        """Should accept plugin paths."""
        config = ProfileConfig(
            enabled_plugins=["/path/to/plugin1", "/path/to/plugin2"]
        )
        assert len(config.enabled_plugins) == 2


class TestProfileBase:
    """Test ProfileBase model."""

    def test_valid_profile_name(self):
        """Should accept valid profile with name and config."""
        profile = ProfileBase(name="My Profile", config=ProfileConfig())
        assert profile.name == "My Profile"
        assert profile.config is not None

    def test_name_min_length(self):
        """Name must not be empty."""
        with pytest.raises(ValidationError):
            ProfileBase(name="", config=ProfileConfig())

    def test_name_max_length(self):
        """Name must be at most 100 characters."""
        with pytest.raises(ValidationError):
            ProfileBase(name="a" * 101, config=ProfileConfig())

    def test_config_required(self):
        """Config field is required."""
        with pytest.raises(ValidationError):
            ProfileBase(name="My Profile")
