"""
Unit tests for RewindManager module.

Tests cover:
- Settings file read/write operations
- Session checkpoint retrieval
- Checkpoint extraction from session data
- Text checkpoint parsing
- Rewind execution
- Input sequence building
- Pending rewind configuration and management
- Error handling and edge cases
"""

import json
import pytest
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock, mock_open

from app.core.rewind_manager import RewindManager, rewind_manager


class TestRewindManagerInit:
    """Test RewindManager initialization."""

    def test_init_default_paths(self):
        """Should initialize with default paths based on HOME env."""
        with patch.dict('os.environ', {'HOME': '/home/testuser'}):
            manager = RewindManager()

            assert manager.config_dir == Path('/home/testuser/.claude')
            assert manager.settings_file == Path('/home/testuser/.claude/settings.json')

    def test_init_fallback_home(self):
        """Should fallback to /home/appuser when HOME not set."""
        with patch.dict('os.environ', {}, clear=True):
            with patch('os.environ.get', return_value='/home/appuser'):
                manager = RewindManager()

                assert manager.config_dir == Path('/home/appuser/.claude')

    def test_global_instance_exists(self):
        """Global rewind_manager instance should exist."""
        assert rewind_manager is not None
        assert isinstance(rewind_manager, RewindManager)


class TestReadSettings:
    """Test _read_settings method."""

    def test_read_existing_settings(self, temp_dir):
        """Should read and parse existing settings file."""
        settings_data = {"key": "value", "nested": {"a": 1}}

        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps(settings_data))

        manager = RewindManager()
        manager.settings_file = settings_file

        result = manager._read_settings()

        assert result == settings_data

    def test_read_nonexistent_settings(self, temp_dir):
        """Should return empty dict when settings file doesn't exist."""
        manager = RewindManager()
        manager.settings_file = temp_dir / "nonexistent.json"

        result = manager._read_settings()

        assert result == {}

    def test_read_invalid_json_settings(self, temp_dir):
        """Should return empty dict and log warning for invalid JSON."""
        settings_file = temp_dir / "settings.json"
        settings_file.write_text("this is not valid json")

        manager = RewindManager()
        manager.settings_file = settings_file

        with patch('app.core.rewind_manager.logger') as mock_logger:
            result = manager._read_settings()

            assert result == {}
            mock_logger.warning.assert_called_once()

    def test_read_settings_io_error(self, temp_dir):
        """Should handle IOError gracefully."""
        # Create a file so exists() returns True
        settings_file = temp_dir / "settings.json"
        settings_file.write_text('{"valid": "json"}')

        manager = RewindManager()
        manager.settings_file = settings_file

        with patch('builtins.open', side_effect=IOError("Read error")):
            with patch('app.core.rewind_manager.logger') as mock_logger:
                result = manager._read_settings()

                assert result == {}
                mock_logger.warning.assert_called_once()


class TestWriteSettings:
    """Test _write_settings method."""

    def test_write_settings_success(self, temp_dir):
        """Should write settings to file successfully."""
        settings_data = {"key": "value", "number": 42}

        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = temp_dir / "settings.json"

        result = manager._write_settings(settings_data)

        assert result is True
        assert manager.settings_file.exists()

        with open(manager.settings_file) as f:
            saved = json.load(f)
        assert saved == settings_data

    def test_write_settings_creates_directory(self, temp_dir):
        """Should create config directory if it doesn't exist."""
        settings_data = {"test": True}

        new_config_dir = temp_dir / "new_config"

        manager = RewindManager()
        manager.config_dir = new_config_dir
        manager.settings_file = new_config_dir / "settings.json"

        result = manager._write_settings(settings_data)

        assert result is True
        assert new_config_dir.exists()
        assert manager.settings_file.exists()

    def test_write_settings_io_error(self, temp_dir):
        """Should return False and log error on IOError."""
        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = temp_dir / "settings.json"

        with patch('builtins.open', side_effect=IOError("Write error")):
            with patch('app.core.rewind_manager.logger') as mock_logger:
                result = manager._write_settings({"test": True})

                assert result is False
                mock_logger.error.assert_called_once()


class TestGetSessionCheckpoints:
    """Test get_session_checkpoints method."""

    def test_get_checkpoints_claude_not_found(self, temp_dir):
        """Should return error when Claude CLI is not found."""
        manager = RewindManager()

        with patch('shutil.which', return_value=None):
            result = manager.get_session_checkpoints("session-123", str(temp_dir))

            assert result["success"] is False
            assert "Claude CLI not found" in result["error"]
            assert result["checkpoints"] == []

    def test_get_checkpoints_success_json_output(self, temp_dir):
        """Should parse JSON output from Claude CLI."""
        session_data = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
                {"role": "user", "content": "How are you?"}
            ]
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(session_data)
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.get_session_checkpoints("session-123", str(temp_dir))

                assert result["success"] is True
                assert result["session_id"] == "session-123"
                assert len(result["checkpoints"]) == 2  # Only user messages

    def test_get_checkpoints_cli_failure(self, temp_dir):
        """Should handle Claude CLI failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Session not found"

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.get_session_checkpoints("session-123", str(temp_dir))

                assert result["success"] is False
                assert "Session not found" in result["error"]

    def test_get_checkpoints_timeout(self, temp_dir):
        """Should handle timeout gracefully."""
        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=subprocess.TimeoutExpired("claude", 30)):
                result = manager.get_session_checkpoints("session-123", str(temp_dir))

                assert result["success"] is False
                assert "Timeout" in result["error"]

    def test_get_checkpoints_fallback_text_parsing(self, temp_dir):
        """Should fallback to text parsing when JSON parsing fails."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "First message\nSecond message\n# Comment line\nThird message"
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.get_session_checkpoints("session-123", str(temp_dir))

                assert result["success"] is True
                # Should skip comment lines starting with #
                assert len(result["checkpoints"]) == 3

    def test_get_checkpoints_general_exception(self, temp_dir):
        """Should handle general exceptions."""
        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=Exception("Unexpected error")):
                with patch('app.core.rewind_manager.logger') as mock_logger:
                    result = manager.get_session_checkpoints("session-123", str(temp_dir))

                    assert result["success"] is False
                    assert "Unexpected error" in result["error"]
                    mock_logger.error.assert_called_once()


class TestExtractCheckpoints:
    """Test _extract_checkpoints method."""

    def test_extract_from_messages_list(self):
        """Should extract checkpoints from messages list."""
        session_data = {
            "messages": [
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"}
            ]
        }

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert len(checkpoints) == 2
        assert checkpoints[0]["index"] == 0
        assert checkpoints[0]["message"] == "First question"
        assert checkpoints[1]["index"] == 2
        assert checkpoints[1]["is_current"] is True

    def test_extract_from_conversation_list(self):
        """Should fallback to conversation field."""
        session_data = {
            "conversation": [
                {"role": "user", "content": "Hello from conversation"}
            ]
        }

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert len(checkpoints) == 1
        assert checkpoints[0]["message"] == "Hello from conversation"

    def test_extract_content_blocks(self):
        """Should extract text from content blocks."""
        session_data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part 1 "},
                        {"type": "text", "text": "Part 2"}
                    ]
                }
            ]
        }

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert len(checkpoints) == 1
        assert checkpoints[0]["full_message"] == "Part 1  Part 2"

    def test_extract_truncates_long_messages(self):
        """Should truncate long messages for display."""
        long_message = "x" * 200
        session_data = {
            "messages": [
                {"role": "user", "content": long_message}
            ]
        }

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert len(checkpoints) == 1
        assert len(checkpoints[0]["message"]) == 103  # 100 chars + "..."
        assert checkpoints[0]["full_message"] == long_message

    def test_extract_preserves_timestamp(self):
        """Should preserve timestamp from messages."""
        session_data = {
            "messages": [
                {"role": "user", "content": "Test", "timestamp": "2024-01-01T00:00:00Z"}
            ]
        }

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert checkpoints[0]["timestamp"] == "2024-01-01T00:00:00Z"

    def test_extract_empty_messages(self):
        """Should return empty list for no messages."""
        session_data = {"messages": []}

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert checkpoints == []

    def test_extract_no_user_messages(self):
        """Should return empty list when no user messages."""
        session_data = {
            "messages": [
                {"role": "assistant", "content": "Hello"},
                {"role": "system", "content": "System prompt"}
            ]
        }

        manager = RewindManager()
        checkpoints = manager._extract_checkpoints(session_data)

        assert checkpoints == []


class TestParseTextCheckpoints:
    """Test _parse_text_checkpoints method."""

    def test_parse_simple_text(self):
        """Should parse simple text lines."""
        output = "Line 1\nLine 2\nLine 3"

        manager = RewindManager()
        checkpoints = manager._parse_text_checkpoints(output)

        assert len(checkpoints) == 3
        assert checkpoints[0]["message"] == "Line 1"
        assert checkpoints[2]["is_current"] is True

    def test_parse_skips_comments(self):
        """Should skip lines starting with #."""
        output = "Valid line\n# Comment\nAnother valid"

        manager = RewindManager()
        checkpoints = manager._parse_text_checkpoints(output)

        assert len(checkpoints) == 2
        assert "Comment" not in checkpoints[0]["message"]

    def test_parse_skips_empty_lines(self):
        """Should skip empty lines."""
        output = "Line 1\n\n   \nLine 2"

        manager = RewindManager()
        checkpoints = manager._parse_text_checkpoints(output)

        assert len(checkpoints) == 2

    def test_parse_truncates_long_lines(self):
        """Should truncate lines over 100 characters."""
        output = "x" * 150

        manager = RewindManager()
        checkpoints = manager._parse_text_checkpoints(output)

        assert len(checkpoints[0]["message"]) == 103  # 100 + "..."
        assert checkpoints[0]["full_message"] == "x" * 150

    def test_parse_empty_output(self):
        """Should handle empty output."""
        manager = RewindManager()
        checkpoints = manager._parse_text_checkpoints("")

        assert checkpoints == []

    def test_parse_marks_last_as_current(self):
        """Should mark last checkpoint as current."""
        output = "First\nSecond\nThird"

        manager = RewindManager()
        checkpoints = manager._parse_text_checkpoints(output)

        assert checkpoints[0]["is_current"] is False
        assert checkpoints[1]["is_current"] is False
        assert checkpoints[2]["is_current"] is True


class TestExecuteRewind:
    """Test execute_rewind method."""

    def test_execute_rewind_claude_not_found(self, temp_dir):
        """Should return error when Claude CLI not found."""
        manager = RewindManager()

        with patch('shutil.which', return_value=None):
            result = manager.execute_rewind("session-123", 0, 1, str(temp_dir))

            assert result["success"] is False
            assert "Claude CLI not found" in result["error"]

    def test_execute_rewind_success_with_markers(self, temp_dir):
        """Should detect success from output markers."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Conversation restored to checkpoint"
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.execute_rewind("session-123", 2, 1, str(temp_dir))

                assert result["success"] is True
                assert "successfully" in result["message"]
                assert result["checkpoint_index"] == 2
                assert result["restore_option"] == 1

    def test_execute_rewind_success_rewound_marker(self, temp_dir):
        """Should detect 'rewound' marker."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Session rewound to earlier state"
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.execute_rewind("session-123", 1, 2, str(temp_dir))

                assert result["success"] is True

    def test_execute_rewind_success_zero_return_code(self, temp_dir):
        """Should succeed with zero return code even without markers."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Some other output"
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.execute_rewind("session-123", 0, 1, str(temp_dir))

                assert result["success"] is True
                assert "executed" in result["message"]

    def test_execute_rewind_failure(self, temp_dir):
        """Should detect failure from non-zero return code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: Invalid checkpoint"

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.execute_rewind("session-123", 5, 1, str(temp_dir))

                assert result["success"] is False
                assert "Invalid checkpoint" in result["error"]
                assert result["return_code"] == 1

    def test_execute_rewind_timeout(self, temp_dir):
        """Should handle timeout."""
        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=subprocess.TimeoutExpired("claude", 60)):
                result = manager.execute_rewind("session-123", 0, 1, str(temp_dir))

                assert result["success"] is False
                assert "timed out" in result["error"]

    def test_execute_rewind_exception(self, temp_dir):
        """Should handle general exceptions."""
        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', side_effect=Exception("Process error")):
                with patch('app.core.rewind_manager.logger') as mock_logger:
                    result = manager.execute_rewind("session-123", 0, 1, str(temp_dir))

                    assert result["success"] is False
                    assert "Process error" in result["error"]
                    mock_logger.error.assert_called_once()

    def test_execute_rewind_truncates_long_output(self, temp_dir):
        """Should truncate long output in response."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "x" * 1000
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.execute_rewind("session-123", 0, 1, str(temp_dir))

                assert len(result["output"]) == 500

    def test_execute_rewind_failure_with_empty_output(self, temp_dir):
        """Should handle failure with empty output."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = ""

        manager = RewindManager()

        with patch('shutil.which', return_value='/usr/bin/claude'):
            with patch('subprocess.run', return_value=mock_result):
                result = manager.execute_rewind("session-123", 0, 1, str(temp_dir))

                assert result["success"] is False
                assert "Unknown error" in result["error"]


class TestBuildRewindInput:
    """Test _build_rewind_input method."""

    def test_build_input_basic(self):
        """Should build basic input sequence."""
        manager = RewindManager()

        result = manager._build_rewind_input(0, 1)

        assert result.startswith("/rewind\n")
        assert result.endswith("\n")

    def test_build_input_checkpoint_navigation(self):
        """Should include down arrows for checkpoint navigation."""
        manager = RewindManager()

        result = manager._build_rewind_input(3, 1)

        # Should have 3 down arrows for checkpoint
        assert result.count("\x1b[B") >= 3

    def test_build_input_option_navigation(self):
        """Should include down arrows for option selection."""
        manager = RewindManager()

        result = manager._build_rewind_input(0, 3)

        # Should have 2 down arrows for option 3 (options are 1-indexed)
        down_arrows = result.count("\x1b[B")
        assert down_arrows >= 2

    def test_build_input_combined_navigation(self):
        """Should combine checkpoint and option navigation."""
        manager = RewindManager()

        result = manager._build_rewind_input(2, 4)

        # 2 down arrows for checkpoint + 3 for option 4
        down_arrows = result.count("\x1b[B")
        assert down_arrows == 5

    def test_build_input_includes_enters(self):
        """Should include enter keys for selections."""
        manager = RewindManager()

        result = manager._build_rewind_input(1, 2)

        # Count newlines (excluding the initial command newline)
        newlines = result.count("\n")
        assert newlines >= 2  # At least command newline + confirmation


class TestPendingRewind:
    """Test pending rewind configuration methods."""

    def test_get_pending_rewind_exists(self, temp_dir):
        """Should return pending rewind when configured."""
        pending_data = {
            "sessionId": "session-123",
            "checkpointIndex": 2,
            "restoreOption": 1
        }
        settings_data = {"pendingRewind": pending_data}

        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps(settings_data))

        manager = RewindManager()
        manager.settings_file = settings_file

        result = manager.get_pending_rewind()

        assert result == pending_data

    def test_get_pending_rewind_none(self, temp_dir):
        """Should return None when no pending rewind."""
        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps({"other": "data"}))

        manager = RewindManager()
        manager.settings_file = settings_file

        result = manager.get_pending_rewind()

        assert result is None

    def test_configure_pending_rewind(self, temp_dir):
        """Should configure pending rewind in settings."""
        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = temp_dir / "settings.json"

        result = manager.configure_pending_rewind(
            session_id="session-123",
            sdk_session_id="sdk-session-456",
            checkpoint_index=2,
            checkpoint_message="User question here",
            restore_option=1
        )

        assert result is True

        with open(manager.settings_file) as f:
            saved = json.load(f)

        assert "pendingRewind" in saved
        assert saved["pendingRewind"]["sessionId"] == "session-123"
        assert saved["pendingRewind"]["sdkSessionId"] == "sdk-session-456"
        assert saved["pendingRewind"]["checkpointIndex"] == 2
        assert saved["pendingRewind"]["restoreOption"] == 1
        assert "timestamp" in saved["pendingRewind"]

    def test_configure_pending_rewind_preserves_existing(self, temp_dir):
        """Should preserve existing settings when adding pending rewind."""
        existing_settings = {"existingKey": "existingValue"}
        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps(existing_settings))

        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = settings_file

        manager.configure_pending_rewind(
            session_id="s1",
            sdk_session_id="sdk1",
            checkpoint_index=0,
            checkpoint_message="Test",
            restore_option=1
        )

        with open(settings_file) as f:
            saved = json.load(f)

        assert saved["existingKey"] == "existingValue"
        assert "pendingRewind" in saved

    def test_clear_pending_rewind_exists(self, temp_dir):
        """Should clear pending rewind when it exists."""
        settings_data = {
            "pendingRewind": {"sessionId": "session-123"},
            "otherSetting": "value"
        }
        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps(settings_data))

        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = settings_file

        result = manager.clear_pending_rewind()

        assert result is True

        with open(settings_file) as f:
            saved = json.load(f)

        assert "pendingRewind" not in saved
        assert saved["otherSetting"] == "value"

    def test_clear_pending_rewind_not_exists(self, temp_dir):
        """Should return True when no pending rewind to clear."""
        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps({"other": "data"}))

        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = settings_file

        result = manager.clear_pending_rewind()

        assert result is True


class TestIntegration:
    """Integration tests combining multiple operations."""

    def test_full_rewind_workflow(self, temp_dir):
        """Test complete rewind workflow."""
        manager = RewindManager()
        manager.config_dir = temp_dir
        manager.settings_file = temp_dir / "settings.json"

        # Step 1: Configure pending rewind
        result = manager.configure_pending_rewind(
            session_id="session-123",
            sdk_session_id="sdk-session-456",
            checkpoint_index=2,
            checkpoint_message="Second user message",
            restore_option=1
        )
        assert result is True

        # Step 2: Verify pending rewind exists
        pending = manager.get_pending_rewind()
        assert pending is not None
        assert pending["sessionId"] == "session-123"

        # Step 3: Clear pending rewind
        result = manager.clear_pending_rewind()
        assert result is True

        # Step 4: Verify cleared
        pending = manager.get_pending_rewind()
        assert pending is None

    def test_multiple_checkpoint_extractions(self):
        """Test extracting checkpoints from various formats."""
        manager = RewindManager()

        # Test with messages format
        session1 = {
            "messages": [
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "Q2"}
            ]
        }
        cp1 = manager._extract_checkpoints(session1)
        assert len(cp1) == 2

        # Test with conversation format
        session2 = {
            "conversation": [
                {"role": "user", "content": "Question"},
            ]
        }
        cp2 = manager._extract_checkpoints(session2)
        assert len(cp2) == 1

        # Test with content blocks
        session3 = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part A"},
                        {"type": "image", "source": "..."},
                        {"type": "text", "text": "Part B"}
                    ]
                }
            ]
        }
        cp3 = manager._extract_checkpoints(session3)
        assert len(cp3) == 1
        assert "Part A" in cp3[0]["full_message"]
        assert "Part B" in cp3[0]["full_message"]


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_session_data(self):
        """Should handle empty session data."""
        manager = RewindManager()

        checkpoints = manager._extract_checkpoints({})
        assert checkpoints == []

    def test_whitespace_only_text_output(self):
        """Should handle whitespace-only text output."""
        manager = RewindManager()

        checkpoints = manager._parse_text_checkpoints("   \n\t\n   ")
        assert checkpoints == []

    def test_single_character_content(self):
        """Should handle single character content."""
        manager = RewindManager()

        session_data = {
            "messages": [
                {"role": "user", "content": "?"}
            ]
        }

        checkpoints = manager._extract_checkpoints(session_data)
        assert len(checkpoints) == 1
        assert checkpoints[0]["message"] == "?"

    def test_unicode_content(self):
        """Should handle Unicode content properly."""
        manager = RewindManager()

        session_data = {
            "messages": [
                {"role": "user", "content": "Hello world!"}
            ]
        }

        checkpoints = manager._extract_checkpoints(session_data)
        assert len(checkpoints) == 1

    def test_very_large_checkpoint_index(self):
        """Should handle large checkpoint index in input building."""
        manager = RewindManager()

        result = manager._build_rewind_input(100, 4)

        # Should have 100 down arrows for checkpoint + 3 for option 4
        assert result.count("\x1b[B") == 103

    def test_content_with_newlines(self):
        """Should handle content with embedded newlines."""
        manager = RewindManager()

        session_data = {
            "messages": [
                {"role": "user", "content": "Line 1\nLine 2\nLine 3"}
            ]
        }

        checkpoints = manager._extract_checkpoints(session_data)
        assert len(checkpoints) == 1
        assert "\n" in checkpoints[0]["full_message"]

    def test_mixed_content_block_types(self):
        """Should handle mixed content block types without text."""
        manager = RewindManager()

        session_data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": "base64..."},
                        {"type": "tool_result", "result": "..."}
                    ]
                }
            ]
        }

        checkpoints = manager._extract_checkpoints(session_data)
        assert len(checkpoints) == 1
        assert checkpoints[0]["message"] == ""
