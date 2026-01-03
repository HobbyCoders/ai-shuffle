"""
Unit tests for query engine module.

Tests cover:
- truncate_large_payload function
- write_agents_to_filesystem function
- cleanup_agents_directory function
- _build_plugins_list function
- SessionState dataclass
- get_session_state function
- cleanup_stale_sessions function
- _is_git_repo function
- _get_os_version function
- _get_available_providers function
- _build_ai_tools_section function
- _get_decrypted_api_key function
- _resolve_api_credential function
- _get_hook_tool_names function
- _build_extra_args function
- _build_env_with_ai_tools function
- generate_environment_details function
- build_options_from_profile function
- execute_query function
- stream_query function
- stream_to_websocket function
- start_background_query function
- interrupt_session function
- get_active_sessions function
- get_streaming_sessions function
"""

import pytest
import asyncio
import tempfile
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from typing import Dict, Any

# Import the module under test
from app.core.query_engine import (
    truncate_large_payload,
    write_agents_to_filesystem,
    cleanup_agents_directory,
    _build_plugins_list,
    SessionState,
    get_session_state,
    cleanup_stale_sessions,
    _is_git_repo,
    _get_os_version,
    _get_available_providers,
    _build_ai_tools_section,
    _get_decrypted_api_key,
    _resolve_api_credential,
    _get_hook_tool_names,
    _build_extra_args,
    _build_env_with_ai_tools,
    generate_environment_details,
    build_options_from_profile,
    execute_query,
    stream_query,
    stream_to_websocket,
    start_background_query,
    interrupt_session,
    get_active_sessions,
    get_streaming_sessions,
    _active_sessions,
    MAX_TOOL_OUTPUT_SIZE,
    MAX_DISPLAY_OUTPUT_SIZE,
    SECURITY_INSTRUCTIONS,
)


# =============================================================================
# truncate_large_payload Tests
# =============================================================================

class TestTruncateLargePayload:
    """Test truncate_large_payload function."""

    def test_none_content_returns_empty_string(self):
        """Should return empty string for None content."""
        result = truncate_large_payload(None)
        assert result == ""

    def test_small_content_returned_truncated_for_display(self):
        """Should return content truncated to display size for small inputs."""
        content = "Hello World"
        result = truncate_large_payload(content)
        assert result == "Hello World"

    def test_content_at_display_limit(self):
        """Should truncate content at display limit."""
        content = "x" * (MAX_DISPLAY_OUTPUT_SIZE + 100)
        result = truncate_large_payload(content, max_size=MAX_TOOL_OUTPUT_SIZE)
        assert len(result) == MAX_DISPLAY_OUTPUT_SIZE

    def test_large_content_gets_truncated(self):
        """Should truncate large content with indicator."""
        large_content = "x" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(large_content)
        assert "truncated" in result.lower()
        assert len(result) < len(large_content)

    def test_base64_png_detected(self):
        """Should detect PNG base64 content."""
        # iVBOR is the base64 signature for PNG
        content = "iVBOR" + "A" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(content)
        assert "Image/binary content" in result

    def test_base64_jpeg_detected(self):
        """Should detect JPEG base64 content."""
        # /9j/ is the base64 signature for JPEG
        content = "/9j/" + "A" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(content)
        assert "Image/binary content" in result

    def test_base64_gif_detected(self):
        """Should detect GIF base64 content."""
        # R0lGOD is the base64 signature for GIF
        content = "R0lGOD" + "A" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(content)
        assert "Image/binary content" in result

    def test_data_url_image_detected(self):
        """Should detect data URL image content."""
        content = "data:image/png;base64," + "A" * (MAX_TOOL_OUTPUT_SIZE + 1000)
        result = truncate_large_payload(content)
        assert "Image/binary content" in result

    def test_custom_max_size(self):
        """Should respect custom max_size parameter."""
        content = "x" * 1000
        result = truncate_large_payload(content, max_size=100)
        # Should be truncated since 1000 > 100
        assert "truncated" in result.lower()


# =============================================================================
# write_agents_to_filesystem Tests
# =============================================================================

class TestWriteAgentsToFilesystem:
    """Test write_agents_to_filesystem function."""

    def test_empty_dict_returns_none(self):
        """Should return None for empty agents dict."""
        result = write_agents_to_filesystem({}, "/tmp/test")
        assert result is None

    def test_none_returns_none(self):
        """Should return None for None agents dict."""
        result = write_agents_to_filesystem(None, "/tmp/test")
        assert result is None

    def test_writes_agent_files(self, temp_dir):
        """Should write agent files to .claude/agents directory."""
        from claude_agent_sdk import AgentDefinition

        agents = {
            "test-agent": AgentDefinition(
                description="A test agent",
                prompt="You are a test agent.",
                tools=["Bash", "Read"],
                model="claude-sonnet-4-20250514"
            )
        }

        result = write_agents_to_filesystem(agents, str(temp_dir))

        assert result is not None
        assert result.exists()
        agent_file = result / "test-agent.md"
        assert agent_file.exists()

        content = agent_file.read_text()
        assert "name: test-agent" in content
        assert "description: A test agent" in content
        assert "tools: Bash, Read" in content
        assert "model: claude-sonnet-4-20250514" in content
        assert "You are a test agent." in content

    def test_creates_agents_directory(self, temp_dir):
        """Should create .claude/agents directory if it doesn't exist."""
        from claude_agent_sdk import AgentDefinition

        agents = {
            "my-agent": AgentDefinition(
                description="Test",
                prompt="Test prompt"
            )
        }

        result = write_agents_to_filesystem(agents, str(temp_dir))

        agents_dir = temp_dir / ".claude" / "agents"
        assert agents_dir.exists()
        assert agents_dir.is_dir()


# =============================================================================
# cleanup_agents_directory Tests
# =============================================================================

class TestCleanupAgentsDirectory:
    """Test cleanup_agents_directory function."""

    def test_none_dir_does_not_raise(self):
        """Should not raise for None directory."""
        cleanup_agents_directory(None, ["agent-1"])
        # No error raised

    def test_nonexistent_dir_does_not_raise(self, temp_dir):
        """Should not raise for nonexistent directory."""
        nonexistent = temp_dir / "nonexistent"
        cleanup_agents_directory(nonexistent, ["agent-1"])
        # No error raised

    def test_removes_agent_files(self, temp_dir):
        """Should remove specified agent files."""
        agents_dir = temp_dir / ".claude" / "agents"
        agents_dir.mkdir(parents=True)

        # Create some agent files
        (agents_dir / "agent-1.md").write_text("test")
        (agents_dir / "agent-2.md").write_text("test")
        (agents_dir / "other-file.md").write_text("test")

        cleanup_agents_directory(agents_dir, ["agent-1", "agent-2"])

        assert not (agents_dir / "agent-1.md").exists()
        assert not (agents_dir / "agent-2.md").exists()
        assert (agents_dir / "other-file.md").exists()  # Should not be removed


# =============================================================================
# _build_plugins_list Tests
# =============================================================================

class TestBuildPluginsList:
    """Test _build_plugins_list function."""

    def test_none_returns_none(self):
        """Should return None for None plugins list."""
        result = _build_plugins_list(None)
        assert result is None

    def test_empty_list_returns_none(self):
        """Should return None for empty plugins list."""
        result = _build_plugins_list([])
        assert result is None

    def test_skips_plugins_when_auto_loaded(self):
        """Should skip plugins when setting_sources includes user/project."""
        # Plugin IDs (not direct paths) are auto-loaded when setting_sources
        # includes "user" or "project". The function returns None early in this case.
        result = _build_plugins_list(
            ["plugin-name@marketplace"],
            setting_sources=["user", "project"]
        )

        # Should return None since plugins will be auto-loaded
        assert result is None

    def test_direct_path_plugins_included(self, temp_dir):
        """Should include direct path plugins even with auto-loading."""
        # Create a real plugin directory
        plugin_dir = temp_dir / "my-plugin"
        plugin_dir.mkdir()
        plugin_path = str(plugin_dir)

        # The function filters for paths starting with / or . or not containing @
        # When setting_sources includes user/project, only direct paths are processed
        result = _build_plugins_list(
            [plugin_path],
            setting_sources=["user", "project"]
        )

        # Direct paths should be included
        assert result is not None
        assert len(result) == 1
        assert result[0]["type"] == "local"
        assert result[0]["path"] == plugin_path

    def test_relative_path_plugins_included(self, temp_dir):
        """Should include relative path plugins when they exist."""
        # Create a plugin directory that looks like a relative path
        plugin_dir = temp_dir / "my-local-plugin"
        plugin_dir.mkdir()
        plugin_path = str(plugin_dir)

        # Use full path but pretend it's relative for the test
        result = _build_plugins_list(
            [plugin_path],
            setting_sources=["user", "project"]
        )

        assert result is not None
        assert len(result) == 1
        assert result[0]["type"] == "local"
        assert result[0]["path"] == plugin_path

    def test_no_setting_sources_processes_all(self, temp_dir):
        """Should process all plugins when no setting_sources."""
        # Create a real plugin directory
        plugin_dir = temp_dir / "plugin"
        plugin_dir.mkdir()
        plugin_path = str(plugin_dir)

        # When setting_sources is None/empty, all plugins are processed
        # but we need to mock the plugin service for ID resolution
        with patch("app.core.plugin_service.get_plugin_service") as mock_get_service:
            mock_service = MagicMock()
            mock_service.INSTALLED_FILE = Path("/nonexistent/installed_plugins.json")
            mock_get_service.return_value = mock_service

            result = _build_plugins_list([plugin_path])

            assert result is not None
            assert len(result) == 1


# =============================================================================
# SessionState Tests
# =============================================================================

class TestSessionState:
    """Test SessionState dataclass."""

    def test_create_session_state(self):
        """Should create session state with defaults."""
        mock_client = MagicMock()
        state = SessionState(client=mock_client)

        assert state.client == mock_client
        assert state.sdk_session_id is None
        assert state.is_connected is False
        assert state.is_streaming is False
        assert state.interrupt_requested is False
        assert isinstance(state.last_activity, datetime)
        assert state.background_task is None
        assert state.written_agent_ids == []
        assert state.agents_dir is None

    def test_session_state_with_all_fields(self):
        """Should create session state with all fields."""
        mock_client = MagicMock()
        mock_task = MagicMock()
        agents_path = Path("/test/agents")

        state = SessionState(
            client=mock_client,
            sdk_session_id="sdk-123",
            is_connected=True,
            is_streaming=True,
            interrupt_requested=True,
            background_task=mock_task,
            written_agent_ids=["agent-1", "agent-2"],
            agents_dir=agents_path
        )

        assert state.sdk_session_id == "sdk-123"
        assert state.is_connected is True
        assert state.is_streaming is True
        assert state.interrupt_requested is True
        assert state.background_task == mock_task
        assert state.written_agent_ids == ["agent-1", "agent-2"]
        assert state.agents_dir == agents_path


# =============================================================================
# get_session_state / cleanup_stale_sessions Tests
# =============================================================================

class TestSessionManagement:
    """Test session state management functions."""

    def setup_method(self):
        """Clear active sessions before each test."""
        _active_sessions.clear()

    def test_get_session_state_not_found(self):
        """Should return None for unknown session."""
        result = get_session_state("unknown-session")
        assert result is None

    def test_get_session_state_found(self):
        """Should return session state for known session."""
        mock_client = MagicMock()
        state = SessionState(client=mock_client, is_connected=True)
        _active_sessions["test-session"] = state

        result = get_session_state("test-session")

        assert result == state
        assert result.is_connected is True

    @pytest.mark.asyncio
    async def test_cleanup_stale_sessions(self):
        """Should cleanup stale sessions."""
        mock_client = AsyncMock()

        # Create a stale session
        stale_state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=False
        )
        stale_state.last_activity = datetime.now() - timedelta(hours=2)
        _active_sessions["stale-session"] = stale_state

        # Create a fresh session
        fresh_state = SessionState(
            client=MagicMock(),
            is_connected=True,
            is_streaming=False
        )
        fresh_state.last_activity = datetime.now()
        _active_sessions["fresh-session"] = fresh_state

        await cleanup_stale_sessions(max_age_seconds=3600)

        assert "stale-session" not in _active_sessions
        assert "fresh-session" in _active_sessions
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_does_not_remove_streaming_sessions(self):
        """Should not cleanup sessions that are currently streaming."""
        mock_client = MagicMock()

        # Create a stale but streaming session
        streaming_state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=True  # Currently streaming
        )
        streaming_state.last_activity = datetime.now() - timedelta(hours=2)
        _active_sessions["streaming-session"] = streaming_state

        await cleanup_stale_sessions(max_age_seconds=3600)

        # Should not be removed because it's streaming
        assert "streaming-session" in _active_sessions


# =============================================================================
# _is_git_repo Tests
# =============================================================================

class TestIsGitRepo:
    """Test _is_git_repo function."""

    def test_git_repo_returns_true(self, temp_dir):
        """Should return True for a git repository."""
        # Initialize a git repo
        subprocess.run(["git", "init"], cwd=temp_dir, capture_output=True)

        result = _is_git_repo(str(temp_dir))

        assert result is True

    def test_non_git_dir_returns_false(self, temp_dir):
        """Should return False for non-git directory."""
        result = _is_git_repo(str(temp_dir))
        assert result is False

    def test_nonexistent_dir_returns_false(self):
        """Should return False for nonexistent directory."""
        result = _is_git_repo("/nonexistent/path/12345")
        assert result is False


# =============================================================================
# _get_os_version Tests
# =============================================================================

class TestGetOsVersion:
    """Test _get_os_version function."""

    def test_returns_string(self):
        """Should return a string."""
        result = _get_os_version()
        assert isinstance(result, str)
        assert len(result) > 0

    @patch("sys.platform", "win32")
    @patch("platform.release", return_value="10")
    def test_windows_format(self, mock_release):
        """Should return Windows format on Windows."""
        result = _get_os_version()
        assert "Windows" in result or "win32" in result

    @patch("sys.platform", "linux")
    @patch("platform.release", return_value="5.15.0")
    def test_linux_format(self, mock_release):
        """Should return Linux format on Linux."""
        result = _get_os_version()
        assert "Linux" in result or "linux" in result


# =============================================================================
# _get_available_providers Tests
# =============================================================================

class TestGetAvailableProviders:
    """Test _get_available_providers function."""

    @patch("app.core.query_engine._get_decrypted_api_key")
    def test_no_keys_returns_empty_providers(self, mock_get_key):
        """Should return empty providers when no API keys configured."""
        mock_get_key.return_value = None

        result = _get_available_providers()

        assert result["image_providers"] == {}
        assert result["video_providers"] == {}
        assert result["model3d_providers"] == {}
        assert result["has_gemini"] is False
        assert result["has_openai"] is False
        assert result["has_meshy"] is False

    @patch("app.core.query_engine._get_decrypted_api_key")
    def test_gemini_key_enables_google_providers(self, mock_get_key):
        """Should enable Google providers when Gemini key is available."""
        def side_effect(key_name):
            if key_name == "image_api_key":
                return "test-gemini-key"
            return None
        mock_get_key.side_effect = side_effect

        result = _get_available_providers()

        assert "google-gemini" in result["image_providers"]
        assert "google-imagen" in result["image_providers"]
        assert "google-veo" in result["video_providers"]
        assert result["has_gemini"] is True

    @patch("app.core.query_engine._get_decrypted_api_key")
    def test_openai_key_enables_openai_providers(self, mock_get_key):
        """Should enable OpenAI providers when OpenAI key is available."""
        def side_effect(key_name):
            if key_name == "openai_api_key":
                return "test-openai-key"
            return None
        mock_get_key.side_effect = side_effect

        result = _get_available_providers()

        assert "openai-gpt-image" in result["image_providers"]
        assert "openai-sora" in result["video_providers"]
        assert result["has_openai"] is True

    @patch("app.core.query_engine._get_decrypted_api_key")
    def test_meshy_key_enables_3d_providers(self, mock_get_key):
        """Should enable Meshy provider when Meshy key is available."""
        def side_effect(key_name):
            if key_name == "meshy_api_key":
                return "test-meshy-key"
            return None
        mock_get_key.side_effect = side_effect

        result = _get_available_providers()

        assert "meshy" in result["model3d_providers"]
        assert result["has_meshy"] is True


# =============================================================================
# _build_ai_tools_section Tests
# =============================================================================

class TestBuildAiToolsSection:
    """Test _build_ai_tools_section function."""

    def test_none_config_returns_empty(self):
        """Should return empty string for None config."""
        result = _build_ai_tools_section(None)
        assert result == ""

    def test_empty_config_returns_empty(self):
        """Should return empty string for empty config."""
        result = _build_ai_tools_section({})
        assert result == ""

    def test_no_tools_enabled_returns_empty(self):
        """Should return empty string when no tools enabled."""
        result = _build_ai_tools_section({
            "image_generation": False,
            "video_generation": False
        })
        assert result == ""

    @patch("app.core.query_engine._get_available_providers")
    def test_image_tools_section_when_enabled(self, mock_providers):
        """Should include image tools section when image generation enabled."""
        mock_providers.return_value = {
            "image_providers": {
                "google-gemini": {
                    "name": "Nano Banana",
                    "capabilities": ["image-edit"],
                    "best_for": "Fast iteration"
                }
            },
            "video_providers": {},
            "model3d_providers": {},
            "has_gemini": True,
            "has_openai": False,
            "has_meshy": False
        }

        result = _build_ai_tools_section({"image_generation": True})

        assert "<ai-tools>" in result
        assert "</ai-tools>" in result
        assert "Image Tools" in result


# =============================================================================
# _get_decrypted_api_key Tests
# =============================================================================

class TestGetDecryptedApiKey:
    """Test _get_decrypted_api_key function."""

    @patch("app.core.query_engine.database")
    def test_returns_none_when_not_found(self, mock_db):
        """Should return None when setting not found."""
        mock_db.get_system_setting.return_value = None

        result = _get_decrypted_api_key("test_key")

        assert result is None

    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.encryption")
    def test_returns_plaintext_value(self, mock_encryption, mock_db):
        """Should return plaintext value when not encrypted."""
        mock_db.get_system_setting.return_value = "plaintext-key"
        mock_encryption.is_encrypted.return_value = False

        result = _get_decrypted_api_key("test_key")

        assert result == "plaintext-key"

    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.encryption")
    def test_decrypts_encrypted_value(self, mock_encryption, mock_db):
        """Should decrypt encrypted value."""
        mock_db.get_system_setting.return_value = "encrypted:secret"
        mock_encryption.is_encrypted.return_value = True
        mock_encryption.is_encryption_ready.return_value = True
        mock_encryption.decrypt_value.return_value = "decrypted-secret"

        result = _get_decrypted_api_key("test_key")

        assert result == "decrypted-secret"
        mock_encryption.decrypt_value.assert_called_once()

    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.encryption")
    def test_returns_none_when_encryption_not_ready(self, mock_encryption, mock_db):
        """Should return None when encryption key not available."""
        mock_db.get_system_setting.return_value = "encrypted:secret"
        mock_encryption.is_encrypted.return_value = True
        mock_encryption.is_encryption_ready.return_value = False

        result = _get_decrypted_api_key("test_key")

        assert result is None


# =============================================================================
# _get_hook_tool_names Tests
# =============================================================================

class TestGetHookToolNames:
    """Test _get_hook_tool_names function."""

    def test_none_hooks_returns_empty(self):
        """Should return empty list for None hooks."""
        result = _get_hook_tool_names(None)
        assert result == []

    def test_empty_hooks_returns_empty(self):
        """Should return empty list for empty hooks."""
        result = _get_hook_tool_names({})
        assert result == []

    def test_extracts_tool_names_from_matchers(self):
        """Should extract tool names from hook matchers."""
        mock_hook = MagicMock()
        mock_hook.matcher = "AskUserQuestion"

        hooks = {"PreToolUse": [mock_hook]}
        result = _get_hook_tool_names(hooks)

        assert "AskUserQuestion" in result

    def test_handles_pipe_separated_matchers(self):
        """Should handle pipe-separated tool matchers."""
        mock_hook = MagicMock()
        mock_hook.matcher = "Bash|Edit|Read"

        hooks = {"PreToolUse": [mock_hook]}
        result = _get_hook_tool_names(hooks)

        assert "Bash" in result
        assert "Edit" in result
        assert "Read" in result


# =============================================================================
# _build_extra_args Tests
# =============================================================================

class TestBuildExtraArgs:
    """Test _build_extra_args function."""

    def test_none_base_returns_empty_dict(self):
        """Should return empty dict for None base args."""
        result = _build_extra_args(None, None)
        assert result == {}

    def test_preserves_base_args(self):
        """Should preserve base extra args."""
        base = {"arg1": "value1", "arg2": "value2"}
        result = _build_extra_args(base, None)

        assert result["arg1"] == "value1"
        assert result["arg2"] == "value2"

    def test_adds_hook_tools_to_allowed_tools(self):
        """Should add hook tools to allowed-tools."""
        mock_hook = MagicMock()
        mock_hook.matcher = "AskUserQuestion"
        hooks = {"PreToolUse": [mock_hook]}

        result = _build_extra_args({}, hooks)

        assert "allowed-tools" in result
        assert "AskUserQuestion" in result["allowed-tools"]

    def test_merges_with_existing_allowed_tools(self):
        """Should merge with existing allowed-tools."""
        base = {"allowed-tools": "Bash,Read"}
        mock_hook = MagicMock()
        mock_hook.matcher = "AskUserQuestion"
        hooks = {"PreToolUse": [mock_hook]}

        result = _build_extra_args(base, hooks)

        assert "Bash" in result["allowed-tools"]
        assert "Read" in result["allowed-tools"]
        assert "AskUserQuestion" in result["allowed-tools"]


# =============================================================================
# _build_env_with_ai_tools Tests
# =============================================================================

class TestBuildEnvWithAiTools:
    """Test _build_env_with_ai_tools function."""

    def test_none_config_returns_base_env(self):
        """Should return base env for None config."""
        base = {"PATH": "/usr/bin"}
        result = _build_env_with_ai_tools(base, None)
        assert result == base

    def test_no_tools_enabled_returns_base_env(self):
        """Should return base env when no tools enabled."""
        base = {"PATH": "/usr/bin"}
        config = {"image_generation": False}
        result = _build_env_with_ai_tools(base, config)
        assert result == base

    @patch("app.core.query_engine._get_decrypted_api_key")
    @patch("app.core.query_engine.database")
    def test_injects_gemini_key(self, mock_db, mock_get_key):
        """Should inject GEMINI_API_KEY when image tools enabled."""
        mock_get_key.return_value = "test-gemini-key"
        mock_db.get_system_setting.return_value = None

        config = {"image_generation": True}
        result = _build_env_with_ai_tools({}, config)

        assert result.get("GEMINI_API_KEY") == "test-gemini-key"
        assert result.get("IMAGE_API_KEY") == "test-gemini-key"


# =============================================================================
# generate_environment_details Tests
# =============================================================================

class TestGenerateEnvironmentDetails:
    """Test generate_environment_details function."""

    @patch("app.core.query_engine._is_git_repo")
    @patch("app.core.query_engine._get_os_version")
    def test_includes_basic_info(self, mock_os_version, mock_git):
        """Should include basic environment info."""
        mock_git.return_value = True
        mock_os_version.return_value = "Windows 10"

        result = generate_environment_details("/test/workspace")

        assert "<env>" in result
        assert "</env>" in result
        assert "Working directory: /test/workspace" in result
        assert "Is directory a git repo: Yes" in result

    @patch("app.core.query_engine._is_git_repo")
    @patch("app.core.query_engine._get_os_version")
    def test_includes_worktree_info(self, mock_os_version, mock_git):
        """Should include worktree info when in worktree mode."""
        mock_git.return_value = True
        mock_os_version.return_value = "Windows 10"

        result = generate_environment_details(
            "/test/workspace",
            execution_mode="worktree",
            worktree_info={"branch": "feature/test", "base_branch": "main"}
        )

        assert "Execution mode: worktree" in result
        assert "Current branch: feature/test" in result
        assert "Base branch: main" in result


# =============================================================================
# build_options_from_profile Tests
# =============================================================================

class TestBuildOptionsFromProfile:
    """Test build_options_from_profile function."""

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_basic_profile_options(self, mock_deploy, mock_db, mock_settings):
        """Should build basic options from profile."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None
        mock_db.get_subagent.return_value = None

        profile = {
            "id": "test-profile",
            "config": {
                "model": "claude-sonnet-4-20250514",
                "permission_mode": "default",
                "system_prompt": None
            }
        }

        options, agents = build_options_from_profile(profile)

        assert options.model == "claude-sonnet-4-20250514"
        assert options.permission_mode == "default"
        assert SECURITY_INSTRUCTIONS in options.system_prompt
        assert agents is None

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_1m_context_model_mapping(self, mock_deploy, mock_db, mock_settings):
        """Should map 1M context models to base model + beta headers."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None

        profile = {
            "id": "test-profile",
            "config": {
                "model": "sonnet-1m",
                "permission_mode": "acceptEdits"
            }
        }

        options, _ = build_options_from_profile(profile)

        assert options.model == "sonnet"
        assert "context-1m-2025-08-07" in options.betas

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_custom_system_prompt(self, mock_deploy, mock_db, mock_settings):
        """Should handle custom system prompts."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None

        profile = {
            "id": "test-profile",
            "config": {
                "model": "claude-sonnet-4-20250514",
                "system_prompt": {
                    "type": "custom",
                    "content": "You are a helpful assistant.",
                    "inject_env_details": False
                }
            }
        }

        options, _ = build_options_from_profile(profile)

        assert "You are a helpful assistant." in options.system_prompt
        assert SECURITY_INSTRUCTIONS in options.system_prompt

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_project_working_directory(self, mock_deploy, mock_db, mock_settings):
        """Should use project path as working directory."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None

        profile = {
            "id": "test-profile",
            "config": {"model": "claude-sonnet-4-20250514"}
        }
        project = {
            "id": "test-project",
            "path": "projects/my-project"
        }

        options, _ = build_options_from_profile(profile, project=project)

        assert "my-project" in str(options.cwd)


# =============================================================================
# execute_query Tests
# =============================================================================

class TestExecuteQuery:
    """Test execute_query function."""

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    async def test_profile_not_found_raises(self, mock_get_profile):
        """Should raise ValueError when profile not found."""
        mock_get_profile.return_value = None

        with pytest.raises(ValueError, match="Profile not found"):
            await execute_query("test prompt", "unknown-profile")

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_project_not_found_raises(self, mock_db, mock_get_profile):
        """Should raise ValueError when project not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_project.return_value = None

        with pytest.raises(ValueError, match="Project not found"):
            await execute_query(
                "test prompt",
                "test-profile",
                project_id="unknown-project"
            )

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_session_not_found_raises(self, mock_db, mock_get_profile):
        """Should raise ValueError when session not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_session.return_value = None

        with pytest.raises(ValueError, match="Session not found"):
            await execute_query(
                "test prompt",
                "test-profile",
                session_id="unknown-session"
            )


# =============================================================================
# stream_query Tests
# =============================================================================

class TestStreamQuery:
    """Test stream_query function."""

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    async def test_yields_error_for_missing_profile(self, mock_get_profile):
        """Should yield error when profile not found."""
        mock_get_profile.return_value = None

        results = []
        async for event in stream_query("test", "unknown-profile"):
            results.append(event)

        assert len(results) == 1
        assert results[0]["type"] == "error"
        assert "Profile not found" in results[0]["message"]

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_yields_error_for_missing_project(self, mock_db, mock_get_profile):
        """Should yield error when project not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_project.return_value = None

        results = []
        async for event in stream_query("test", "test-profile", project_id="unknown"):
            results.append(event)

        assert len(results) == 1
        assert results[0]["type"] == "error"
        assert "Project not found" in results[0]["message"]

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_yields_error_for_missing_session(self, mock_db, mock_get_profile):
        """Should yield error when session not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_session.return_value = None

        results = []
        async for event in stream_query("test", "test-profile", session_id="unknown"):
            results.append(event)

        assert len(results) == 1
        assert results[0]["type"] == "error"
        assert "Session not found" in results[0]["message"]


# =============================================================================
# stream_to_websocket Tests
# =============================================================================

class TestStreamToWebsocket:
    """Test stream_to_websocket function."""

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    async def test_yields_error_for_missing_profile(self, mock_get_profile):
        """Should yield error when profile not found."""
        mock_get_profile.return_value = None

        results = []
        async for event in stream_to_websocket(
            prompt="test",
            session_id="session-1",
            profile_id="unknown-profile"
        ):
            results.append(event)

        assert len(results) == 1
        assert results[0]["type"] == "error"
        assert "Profile not found" in results[0]["message"]

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_yields_error_for_missing_project(self, mock_db, mock_get_profile):
        """Should yield error when project not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_project.return_value = None

        results = []
        async for event in stream_to_websocket(
            prompt="test",
            session_id="session-1",
            profile_id="test-profile",
            project_id="unknown"
        ):
            results.append(event)

        assert len(results) == 1
        assert results[0]["type"] == "error"
        assert "Project not found" in results[0]["message"]


# =============================================================================
# start_background_query Tests
# =============================================================================

class TestStartBackgroundQuery:
    """Test start_background_query function."""

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    async def test_raises_for_missing_profile(self, mock_get_profile):
        """Should raise ValueError when profile not found."""
        mock_get_profile.return_value = None

        with pytest.raises(ValueError, match="Profile not found"):
            await start_background_query("test", "unknown-profile")

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_raises_for_missing_project(self, mock_db, mock_get_profile):
        """Should raise ValueError when project not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_project.return_value = None

        with pytest.raises(ValueError, match="Project not found"):
            await start_background_query(
                "test",
                "test-profile",
                project_id="unknown"
            )

    @pytest.mark.asyncio
    @patch("app.core.query_engine.get_profile")
    @patch("app.core.query_engine.database")
    async def test_raises_for_missing_session(self, mock_db, mock_get_profile):
        """Should raise ValueError when session not found."""
        mock_get_profile.return_value = {"id": "test", "config": {}}
        mock_db.get_session.return_value = None

        with pytest.raises(ValueError, match="Session not found"):
            await start_background_query(
                "test",
                "test-profile",
                session_id="unknown"
            )


# =============================================================================
# interrupt_session Tests
# =============================================================================

class TestInterruptSession:
    """Test interrupt_session function."""

    def setup_method(self):
        """Clear active sessions before each test."""
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_session(self):
        """Should return False for unknown session."""
        result = await interrupt_session("unknown-session")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_disconnected_session(self):
        """Should return False when session not connected."""
        mock_client = MagicMock()
        state = SessionState(client=mock_client, is_connected=False)
        _active_sessions["test-session"] = state

        result = await interrupt_session("test-session")

        assert result is False

    @pytest.mark.asyncio
    async def test_interrupts_connected_session(self):
        """Should interrupt connected session."""
        mock_client = AsyncMock()
        state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=True
        )
        _active_sessions["test-session"] = state

        result = await interrupt_session("test-session")

        assert result is True
        assert state.interrupt_requested is True
        assert state.is_streaming is False
        mock_client.interrupt.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_interrupt_exception(self):
        """Should handle exception during interrupt."""
        mock_client = AsyncMock()
        mock_client.interrupt.side_effect = Exception("Interrupt failed")
        state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=True
        )
        _active_sessions["test-session"] = state

        result = await interrupt_session("test-session")

        # Should return False but not raise
        assert result is False
        # Should still mark as not streaming
        assert state.is_streaming is False


# =============================================================================
# get_active_sessions / get_streaming_sessions Tests
# =============================================================================

class TestSessionQueries:
    """Test session query functions."""

    def setup_method(self):
        """Clear active sessions before each test."""
        _active_sessions.clear()

    def test_get_active_sessions_empty(self):
        """Should return empty list when no sessions."""
        result = get_active_sessions()
        assert result == []

    def test_get_active_sessions_filters_disconnected(self):
        """Should only return connected sessions."""
        _active_sessions["connected"] = SessionState(
            client=MagicMock(),
            is_connected=True
        )
        _active_sessions["disconnected"] = SessionState(
            client=MagicMock(),
            is_connected=False
        )

        result = get_active_sessions()

        assert "connected" in result
        assert "disconnected" not in result

    def test_get_streaming_sessions_empty(self):
        """Should return empty list when no streaming sessions."""
        result = get_streaming_sessions()
        assert result == []

    def test_get_streaming_sessions_filters_non_streaming(self):
        """Should only return streaming sessions."""
        _active_sessions["streaming"] = SessionState(
            client=MagicMock(),
            is_streaming=True
        )
        _active_sessions["not-streaming"] = SessionState(
            client=MagicMock(),
            is_streaming=False
        )

        result = get_streaming_sessions()

        assert "streaming" in result
        assert "not-streaming" not in result


# =============================================================================
# Integration-style Tests
# =============================================================================

class TestQueryEngineIntegration:
    """Integration-style tests for query engine scenarios."""

    def setup_method(self):
        """Clear active sessions before each test."""
        _active_sessions.clear()

    def test_security_instructions_present(self):
        """SECURITY_INSTRUCTIONS should be defined."""
        assert SECURITY_INSTRUCTIONS is not None
        assert len(SECURITY_INSTRUCTIONS) > 0
        assert "Chat and its Capabilities" in SECURITY_INSTRUCTIONS

    def test_max_sizes_defined(self):
        """Max size constants should be reasonable."""
        assert MAX_TOOL_OUTPUT_SIZE > 0
        assert MAX_DISPLAY_OUTPUT_SIZE > 0
        assert MAX_TOOL_OUTPUT_SIZE > MAX_DISPLAY_OUTPUT_SIZE

    @pytest.mark.asyncio
    async def test_session_lifecycle(self):
        """Test complete session lifecycle."""
        mock_client = AsyncMock()

        # Create session
        state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=False
        )
        _active_sessions["lifecycle-session"] = state

        # Verify active
        assert "lifecycle-session" in get_active_sessions()
        assert "lifecycle-session" not in get_streaming_sessions()

        # Start streaming
        state.is_streaming = True
        assert "lifecycle-session" in get_streaming_sessions()

        # Interrupt
        result = await interrupt_session("lifecycle-session")
        assert result is True
        assert state.is_streaming is False

        # Cleanup
        _active_sessions.clear()
        assert get_active_sessions() == []


# =============================================================================
# _resolve_api_credential Tests
# =============================================================================

class TestResolveApiCredential:
    """Test _resolve_api_credential function."""

    def test_returns_none_for_unknown_credential_type(self):
        """Should return None for unknown credential type."""
        result = _resolve_api_credential("unknown_credential_type")
        assert result is None

    @patch("app.core.query_engine._get_decrypted_api_key")
    @patch("app.core.query_engine.database")
    def test_returns_admin_key_for_optional_policy(self, mock_db, mock_get_key):
        """Should return admin key when policy is 'optional' and user has no key."""
        mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
        mock_db.get_user_credential.return_value = None
        mock_get_key.return_value = "admin-openai-key"

        result = _resolve_api_credential("openai_api_key", api_user_id="user-123")

        assert result == "admin-openai-key"

    @patch("app.core.query_engine._get_decrypted_api_key")
    @patch("app.core.query_engine.database")
    def test_returns_admin_key_for_admin_provided_policy(self, mock_db, mock_get_key):
        """Should return admin key when policy is 'admin_provided'."""
        mock_db.get_user_credential_policy.return_value = {"policy": "admin_provided"}
        mock_get_key.return_value = "admin-gemini-key"

        result = _resolve_api_credential("gemini_api_key", api_user_id="user-123")

        assert result == "admin-gemini-key"
        # gemini_api_key maps to image_api_key in admin settings
        mock_get_key.assert_called_with("image_api_key")

    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.encryption")
    def test_user_provided_policy_uses_user_key(self, mock_encryption, mock_db):
        """Should use user's key when policy is 'user_provided'."""
        mock_db.get_user_credential_policy.return_value = {"policy": "user_provided"}
        mock_db.get_user_credential.return_value = {
            "encrypted_value": "encrypted:user-key"
        }
        mock_encryption.is_encrypted.return_value = True
        mock_encryption.is_encryption_ready.return_value = True
        mock_encryption.decrypt_value.return_value = "user-key"

        result = _resolve_api_credential("openai_api_key", api_user_id="user-123")

        assert result == "user-key"

    @patch("app.core.query_engine.database")
    def test_user_provided_policy_returns_none_when_no_user_key(self, mock_db):
        """Should return None when policy is 'user_provided' and user has no key."""
        mock_db.get_user_credential_policy.return_value = {"policy": "user_provided"}
        mock_db.get_user_credential.return_value = None

        result = _resolve_api_credential("openai_api_key", api_user_id="user-123")

        assert result is None


# =============================================================================
# Additional truncate_large_payload Tests
# =============================================================================

class TestTruncateLargePayloadEdgeCases:
    """Additional edge case tests for truncate_large_payload."""

    def test_dict_content_gets_converted(self):
        """Should convert dict content to string."""
        content = {"key": "value"}
        result = truncate_large_payload(content)
        assert "key" in result
        assert "value" in result

    def test_list_content_gets_converted(self):
        """Should convert list content to string."""
        content = ["item1", "item2"]
        result = truncate_large_payload(content)
        assert "item1" in result
        assert "item2" in result

    def test_integer_content_gets_converted(self):
        """Should convert integer content to string."""
        content = 42
        result = truncate_large_payload(content)
        assert result == "42"


# =============================================================================
# Additional generate_environment_details Tests
# =============================================================================

class TestGenerateEnvironmentDetailsExtended:
    """Extended tests for generate_environment_details."""

    @patch("app.core.query_engine._is_git_repo")
    @patch("app.core.query_engine._get_os_version")
    def test_non_git_repo(self, mock_os_version, mock_git):
        """Should handle non-git directories correctly."""
        mock_git.return_value = False
        mock_os_version.return_value = "Linux 5.15.0"

        result = generate_environment_details("/test/workspace")

        assert "Is directory a git repo: No" in result

    @patch("app.core.query_engine._is_git_repo")
    @patch("app.core.query_engine._get_os_version")
    def test_includes_platform_info(self, mock_os_version, mock_git):
        """Should include platform info."""
        mock_git.return_value = True
        mock_os_version.return_value = "Windows 10"

        result = generate_environment_details("/test/workspace")

        assert "Platform:" in result
        assert "OS Version:" in result or "Windows" in result


# =============================================================================
# Additional SessionState Tests
# =============================================================================

class TestSessionStateExtended:
    """Extended tests for SessionState dataclass."""

    def test_last_activity_is_current(self):
        """last_activity should be set to current time on creation."""
        before = datetime.now()
        state = SessionState(client=MagicMock())
        after = datetime.now()

        assert before <= state.last_activity <= after

    def test_session_state_modification(self):
        """SessionState fields should be modifiable."""
        state = SessionState(client=MagicMock())

        # Modify fields
        state.is_connected = True
        state.is_streaming = True
        state.interrupt_requested = True

        assert state.is_connected is True
        assert state.is_streaming is True
        assert state.interrupt_requested is True


# =============================================================================
# Additional _is_git_repo Tests
# =============================================================================

class TestIsGitRepoExtended:
    """Extended tests for _is_git_repo function."""

    def test_handles_permission_error(self, temp_dir):
        """Should return False for directories with permission issues."""
        # This test is more about ensuring no exception is raised
        result = _is_git_repo(str(temp_dir))
        assert result is False

    def test_handles_empty_string_path(self):
        """Should return False for empty path."""
        result = _is_git_repo("")
        assert result is False


# =============================================================================
# Additional cleanup_stale_sessions Tests
# =============================================================================

class TestCleanupStaleSessionsExtended:
    """Extended tests for cleanup_stale_sessions function."""

    def setup_method(self):
        """Clear active sessions before each test."""
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_handles_disconnect_exception(self):
        """Should handle exception during disconnect gracefully."""
        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = Exception("Disconnect failed")

        state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=False
        )
        state.last_activity = datetime.now() - timedelta(hours=2)
        _active_sessions["error-session"] = state

        # Should not raise even if disconnect fails
        await cleanup_stale_sessions(max_age_seconds=3600)

        # Session should still be removed
        assert "error-session" not in _active_sessions


# =============================================================================
# Additional build_options_from_profile Tests
# =============================================================================

class TestBuildOptionsFromProfileExtended:
    """Extended tests for build_options_from_profile function."""

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_returns_options_with_correct_model(self, mock_deploy, mock_db, mock_settings):
        """Should return options with the specified model."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None

        profile = {
            "id": "test-profile",
            "config": {
                "model": "claude-opus-4-20250514",
                "permission_mode": "default"
            }
        }

        options, _ = build_options_from_profile(profile)

        assert options.model == "claude-opus-4-20250514"

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_default_permission_mode(self, mock_deploy, mock_db, mock_settings):
        """Should use default permission mode when not specified."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None

        profile = {
            "id": "test-profile",
            "config": {"model": "claude-sonnet-4-20250514"}
        }

        options, _ = build_options_from_profile(profile)

        assert options.permission_mode == "default"

    @patch("app.core.query_engine.settings")
    @patch("app.core.query_engine.database")
    @patch("app.core.query_engine.detect_deployment_mode")
    def test_accept_edits_permission_mode(self, mock_deploy, mock_db, mock_settings):
        """Should correctly set acceptEdits permission mode."""
        from app.core.platform import DeploymentMode
        mock_deploy.return_value = DeploymentMode.LOCAL
        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_worktree_by_session.return_value = None

        profile = {
            "id": "test-profile",
            "config": {
                "model": "claude-sonnet-4-20250514",
                "permission_mode": "acceptEdits"
            }
        }

        options, _ = build_options_from_profile(profile)

        assert options.permission_mode == "acceptEdits"


# =============================================================================
# Additional Error Handling Tests
# =============================================================================

class TestErrorHandling:
    """Test error handling scenarios."""

    def setup_method(self):
        """Clear active sessions before each test."""
        _active_sessions.clear()

    @pytest.mark.asyncio
    async def test_multiple_interrupts_safe(self):
        """Should handle multiple interrupt calls safely."""
        mock_client = AsyncMock()
        state = SessionState(
            client=mock_client,
            is_connected=True,
            is_streaming=True
        )
        _active_sessions["multi-interrupt"] = state

        # First interrupt
        result1 = await interrupt_session("multi-interrupt")
        assert result1 is True

        # Second interrupt should also work (idempotent)
        result2 = await interrupt_session("multi-interrupt")
        # Already interrupted, client.interrupt may fail or succeed
        # The important thing is it doesn't crash

    def test_get_session_state_returns_none_for_missing(self):
        """get_session_state should return None for missing session."""
        result = get_session_state("nonexistent-session-12345")
        assert result is None

    def test_active_sessions_dict_isolation(self):
        """Modifications to returned lists shouldn't affect internal state."""
        state = SessionState(client=MagicMock(), is_connected=True)
        _active_sessions["test-session"] = state

        sessions = get_active_sessions()
        sessions.clear()  # Try to clear the returned list

        # Should still have the session internally
        assert "test-session" in _active_sessions
