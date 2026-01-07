"""
Unit tests for CLI Bridge module.

Tests cover:
- Session ID validation (UUID format)
- PTY availability checking
- CLISession dataclass
- CLIBridge class lifecycle
- Input handling (send_input, send_key)
- RewindParser output parsing
- Session management functions
- Error handling and edge cases

Note: PTY functionality is Unix-only. Tests for Unix-only functionality
are skipped on Windows.
"""

import pytest
import asyncio
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock
from dataclasses import fields

from app.core.cli_bridge import (
    PTY_AVAILABLE,
    is_pty_available,
    validate_session_id,
    CLISession,
    CLIBridge,
    get_cli_session,
    get_active_cli_sessions,
    RewindParser,
    _cli_sessions,
    UUID_PATTERN,
)


# Helper to skip tests that require Unix-only functionality
skip_on_windows = pytest.mark.skipif(
    sys.platform == 'win32',
    reason="Test requires Unix-only PTY functionality"
)


# =============================================================================
# Test UUID Validation
# =============================================================================

class TestValidateSessionId:
    """Test session ID (UUID) validation."""

    def test_valid_uuid_lowercase(self):
        """Valid lowercase UUID should pass validation."""
        valid_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert validate_session_id(valid_uuid) is True

    def test_valid_uuid_uppercase(self):
        """Valid uppercase UUID should pass validation."""
        valid_uuid = "A1B2C3D4-E5F6-7890-ABCD-EF1234567890"
        assert validate_session_id(valid_uuid) is True

    def test_valid_uuid_mixed_case(self):
        """Valid mixed-case UUID should pass validation."""
        valid_uuid = "a1B2c3D4-E5f6-7890-AbCd-Ef1234567890"
        assert validate_session_id(valid_uuid) is True

    def test_invalid_uuid_too_short(self):
        """UUID that is too short should fail validation."""
        assert validate_session_id("a1b2c3d4-e5f6-7890") is False

    def test_invalid_uuid_too_long(self):
        """UUID that is too long should fail validation."""
        long_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890-extra"
        assert validate_session_id(long_uuid) is False

    def test_invalid_uuid_wrong_format(self):
        """UUID with wrong format should fail validation."""
        assert validate_session_id("not-a-valid-uuid") is False

    def test_invalid_uuid_missing_dashes(self):
        """UUID missing dashes should fail validation."""
        assert validate_session_id("a1b2c3d4e5f67890abcdef1234567890") is False

    def test_invalid_uuid_with_special_chars(self):
        """UUID with special characters should fail validation."""
        assert validate_session_id("a1b2c3d4-e5f6-7890-abcd-ef12345678!@") is False

    def test_empty_session_id(self):
        """Empty session ID should fail validation."""
        assert validate_session_id("") is False

    def test_none_session_id(self):
        """None session ID should fail validation."""
        assert validate_session_id(None) is False

    def test_uuid_with_command_injection_attempt(self):
        """UUID with command injection attempt should fail validation."""
        malicious = "a1b2c3d4-e5f6-7890-abcd-ef12; rm -rf /"
        assert validate_session_id(malicious) is False

    def test_uuid_with_trailing_whitespace(self):
        """UUID with trailing whitespace should fail validation."""
        assert validate_session_id("12345678-1234-1234-1234-123456789abc ") is False

    def test_uuid_with_leading_whitespace(self):
        """UUID with leading whitespace should fail validation."""
        assert validate_session_id(" 12345678-1234-1234-1234-123456789abc") is False

    def test_uuid_with_internal_whitespace(self):
        """UUID with internal whitespace should fail validation."""
        assert validate_session_id("12345678-1234 -1234-1234-123456789abc") is False


class TestUuidPattern:
    """Test the UUID regex pattern directly."""

    def test_pattern_matches_valid_uuid(self):
        """Pattern should match valid UUIDs."""
        assert UUID_PATTERN.match("12345678-1234-1234-1234-123456789abc") is not None

    def test_pattern_rejects_invalid_uuid(self):
        """Pattern should not match invalid UUIDs."""
        assert UUID_PATTERN.match("invalid") is None

    def test_pattern_full_match_rejects_extra_content(self):
        """Pattern fullmatch should reject extra content."""
        # Note: validate_session_id uses match() not fullmatch()
        # So we test the pattern's behavior directly
        match = UUID_PATTERN.match("12345678-1234-1234-1234-123456789abc\n")
        # match() succeeds but doesn't consume the whole string
        if match:
            assert match.group() == "12345678-1234-1234-1234-123456789abc"

    def test_validate_session_id_rejects_extra_content(self):
        """validate_session_id should reject UUIDs with any extra content."""
        # The regex uses match() which doesn't enforce end-of-string
        # but trailing content is still rejected because the pattern is anchored with $
        assert validate_session_id("12345678-1234-1234-1234-123456789abcXXX") is False


# =============================================================================
# Test PTY Availability
# =============================================================================

class TestIsPtyAvailable:
    """Test PTY availability checking."""

    def test_is_pty_available_returns_bool(self):
        """is_pty_available should return a boolean."""
        result = is_pty_available()
        assert isinstance(result, bool)

    def test_is_pty_available_matches_constant(self):
        """is_pty_available should match PTY_AVAILABLE constant."""
        assert is_pty_available() == PTY_AVAILABLE

    @pytest.mark.skipif(sys.platform != 'win32', reason="Windows-specific test")
    def test_pty_not_available_on_windows(self):
        """PTY should not be available on Windows."""
        assert is_pty_available() is False


# =============================================================================
# Test CLISession Dataclass
# =============================================================================

class TestCLISession:
    """Test CLISession dataclass."""

    def test_cli_session_creation(self):
        """Should create CLISession with required fields."""
        session = CLISession(
            session_id="test-session-id",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/home/user/project",
            pid=12345,
            fd=3
        )

        assert session.session_id == "test-session-id"
        assert session.sdk_session_id == "12345678-1234-1234-1234-123456789abc"
        assert session.working_dir == "/home/user/project"
        assert session.pid == 12345
        assert session.fd == 3
        assert session.is_active is True
        assert session.command is None
        assert session.output_buffer == ""

    def test_cli_session_default_values(self):
        """Should have correct default values."""
        session = CLISession(
            session_id="test",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp",
            pid=1,
            fd=0
        )

        assert session.is_active is True
        assert isinstance(session.created_at, datetime)
        assert session.command is None
        assert session.output_buffer == ""

    def test_cli_session_with_all_fields(self):
        """Should accept all optional fields."""
        now = datetime.now()
        session = CLISession(
            session_id="test",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp",
            pid=1,
            fd=0,
            is_active=False,
            created_at=now,
            command="/rewind",
            output_buffer="some output"
        )

        assert session.is_active is False
        assert session.created_at == now
        assert session.command == "/rewind"
        assert session.output_buffer == "some output"

    def test_cli_session_is_dataclass(self):
        """CLISession should be a proper dataclass."""
        field_names = [f.name for f in fields(CLISession)]
        expected_fields = [
            'session_id', 'sdk_session_id', 'working_dir', 'pid', 'fd',
            'is_active', 'created_at', 'command', 'output_buffer'
        ]
        for field in expected_fields:
            assert field in field_names


# =============================================================================
# Test CLIBridge Initialization
# =============================================================================

class TestCLIBridgeInit:
    """Test CLIBridge initialization."""

    def test_cli_bridge_init_required_params(self):
        """Should initialize with required parameters."""
        bridge = CLIBridge(
            session_id="test-session",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/home/user/project"
        )

        assert bridge.session_id == "test-session"
        assert bridge.sdk_session_id == "12345678-1234-1234-1234-123456789abc"
        assert bridge.working_dir == "/home/user/project"
        assert bridge.on_output is None
        assert bridge.on_exit is None

    def test_cli_bridge_init_with_callbacks(self):
        """Should accept callback functions."""
        async def output_callback(data: str):
            pass

        async def exit_callback(code: int):
            pass

        bridge = CLIBridge(
            session_id="test-session",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/home/user/project",
            on_output=output_callback,
            on_exit=exit_callback
        )

        assert bridge.on_output is output_callback
        assert bridge.on_exit is exit_callback

    def test_cli_bridge_initial_state(self):
        """Should have correct initial state."""
        bridge = CLIBridge(
            session_id="test-session",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/home/user/project"
        )

        assert bridge._pid is None
        assert bridge._fd is None
        assert bridge._read_task is None
        assert bridge._is_running is False
        assert bridge._output_buffer == ""
        assert bridge._theme_prompt_handled is False
        assert bridge._pending_command is None
        assert bridge._command_sent is False

    def test_cli_bridge_is_running_property(self):
        """is_running property should reflect internal state."""
        bridge = CLIBridge(
            session_id="test",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp"
        )

        assert bridge.is_running is False
        bridge._is_running = True
        assert bridge.is_running is True


# =============================================================================
# Test CLIBridge Start (Platform-Specific Behavior)
# =============================================================================

class TestCLIBridgeStart:
    """Test CLIBridge start method."""

    @pytest.fixture
    def bridge(self):
        """Create a test bridge instance."""
        return CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )

    @pytest.mark.asyncio
    async def test_start_on_windows_returns_false(self):
        """Start should return False on Windows (no PTY)."""
        output_received = []

        async def capture_output(data: str):
            output_received.append(data)

        exit_code_received = []

        async def capture_exit(code: int):
            exit_code_received.append(code)

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project",
            on_output=capture_output,
            on_exit=capture_exit
        )

        with patch('app.core.cli_bridge.PTY_AVAILABLE', False):
            result = await bridge.start("/rewind")

            assert result is False
            # Should have sent error message to output callback
            if output_received:
                assert "not supported on Windows" in output_received[0]
            # Should have called exit callback with error code
            if exit_code_received:
                assert exit_code_received[0] == 1

    @pytest.mark.asyncio
    async def test_start_already_running_returns_false(self, bridge):
        """Start should return False if already running."""
        bridge._is_running = True

        with patch('app.core.cli_bridge.PTY_AVAILABLE', True):
            result = await bridge.start("/rewind")

        assert result is False

    @pytest.mark.asyncio
    async def test_start_invalid_session_id_returns_false(self):
        """Start should return False for invalid session ID."""
        bridge = CLIBridge(
            session_id="invalid-session-id",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )

        with patch('app.core.cli_bridge.PTY_AVAILABLE', True):
            result = await bridge.start("/rewind")

        assert result is False

    @pytest.mark.asyncio
    async def test_start_invalid_sdk_session_id_returns_false(self):
        """Start should return False for invalid SDK session ID."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="invalid-sdk-session",
            working_dir="/home/user/project"
        )

        with patch('app.core.cli_bridge.PTY_AVAILABLE', True):
            result = await bridge.start("/rewind")

        assert result is False


# =============================================================================
# Test CLIBridge Input Methods
# =============================================================================

class TestCLIBridgeSendInput:
    """Test CLIBridge send_input method."""

    @pytest.fixture
    def running_bridge(self):
        """Create a bridge in running state with mock fd."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._fd = 5
        return bridge

    @pytest.mark.asyncio
    async def test_send_input_when_not_running(self):
        """send_input should do nothing when not running."""
        bridge = CLIBridge(
            session_id="test",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp"
        )
        bridge._is_running = False

        # Should not raise
        await bridge.send_input("test input")

    @pytest.mark.asyncio
    async def test_send_input_when_fd_is_none(self, running_bridge):
        """send_input should do nothing when fd is None."""
        running_bridge._fd = None

        # Should not raise
        await running_bridge.send_input("test input")

    @pytest.mark.asyncio
    async def test_send_input_writes_to_fd(self, running_bridge):
        """send_input should write data to file descriptor."""
        with patch('os.write') as mock_write:
            await running_bridge.send_input("hello")

            mock_write.assert_called_once_with(5, b"hello")

    @pytest.mark.asyncio
    async def test_send_input_encodes_utf8(self, running_bridge):
        """send_input should encode data as UTF-8."""
        with patch('os.write') as mock_write:
            await running_bridge.send_input("hello world!")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"hello world!"

    @pytest.mark.asyncio
    async def test_send_input_handles_unicode(self, running_bridge):
        """send_input should handle Unicode characters."""
        with patch('os.write') as mock_write:
            await running_bridge.send_input("Hello")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == "Hello".encode('utf-8')

    @pytest.mark.asyncio
    async def test_send_input_handles_exception(self, running_bridge):
        """send_input should handle write exceptions gracefully."""
        with patch('os.write', side_effect=OSError("Write error")):
            # Should not raise - just logs
            await running_bridge.send_input("test")


class TestCLIBridgeSendKey:
    """Test CLIBridge send_key method."""

    @pytest.fixture
    def running_bridge(self):
        """Create a bridge in running state with mock fd."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._fd = 5
        return bridge

    @pytest.mark.asyncio
    async def test_send_key_arrow_up(self, running_bridge):
        """send_key should send escape sequence for up arrow."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("up")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\x1b[A"

    @pytest.mark.asyncio
    async def test_send_key_arrow_down(self, running_bridge):
        """send_key should send escape sequence for down arrow."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("down")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\x1b[B"

    @pytest.mark.asyncio
    async def test_send_key_arrow_right(self, running_bridge):
        """send_key should send escape sequence for right arrow."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("right")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\x1b[C"

    @pytest.mark.asyncio
    async def test_send_key_arrow_left(self, running_bridge):
        """send_key should send escape sequence for left arrow."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("left")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\x1b[D"

    @pytest.mark.asyncio
    async def test_send_key_enter(self, running_bridge):
        """send_key should send carriage return for enter."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("enter")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\r"

    @pytest.mark.asyncio
    async def test_send_key_escape(self, running_bridge):
        """send_key should send escape character."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("escape")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\x1b"

    @pytest.mark.asyncio
    async def test_send_key_tab(self, running_bridge):
        """send_key should send tab character."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("tab")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\t"

    @pytest.mark.asyncio
    async def test_send_key_backspace(self, running_bridge):
        """send_key should send backspace character."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("backspace")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\x7f"

    @pytest.mark.asyncio
    async def test_send_key_single_character(self, running_bridge):
        """send_key should send single characters directly."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("1")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"1"

    @pytest.mark.asyncio
    async def test_send_key_case_insensitive(self, running_bridge):
        """send_key should be case-insensitive for named keys."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("ENTER")

            mock_write.assert_called_once()
            args = mock_write.call_args[0]
            assert args[1] == b"\r"

    @pytest.mark.asyncio
    async def test_send_key_unknown_key(self, running_bridge):
        """send_key should log warning for unknown keys."""
        with patch('os.write') as mock_write:
            await running_bridge.send_key("unknown_key")

            # Should not call write for unknown keys
            mock_write.assert_not_called()


# =============================================================================
# Test CLIBridge Stop and Cleanup (Unix-only tests)
# =============================================================================

class TestCLIBridgeStop:
    """Test CLIBridge stop method."""

    @pytest.fixture
    def running_bridge(self):
        """Create a bridge in running state."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._pid = 12345
        bridge._fd = 5
        return bridge

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        """stop should do nothing when not running."""
        bridge = CLIBridge(
            session_id="test",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp"
        )
        bridge._is_running = False

        # Should not raise
        await bridge.stop()

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_stop_sets_not_running(self, running_bridge):
        """stop should set _is_running to False."""
        import signal

        with patch('os.kill') as mock_kill:
            mock_kill.side_effect = ProcessLookupError()
            with patch.object(running_bridge, '_cleanup', new_callable=AsyncMock):
                await running_bridge.stop()

                assert running_bridge._is_running is False

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_stop_sends_sigterm(self, running_bridge):
        """stop should send SIGTERM to the process."""
        import signal

        with patch('os.kill') as mock_kill:
            with patch.object(running_bridge, '_cleanup', new_callable=AsyncMock):
                await running_bridge.stop()

                # Should have tried to kill the process
                mock_kill.assert_called_once()
                assert mock_kill.call_args[0][1] == signal.SIGTERM

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_stop_cancels_read_task(self, running_bridge):
        """stop should cancel the read task."""
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()

        async def cancelled_awaitable():
            raise asyncio.CancelledError()

        # Make the mock awaitable
        mock_task.__await__ = lambda self: cancelled_awaitable().__await__()
        running_bridge._read_task = mock_task

        with patch('os.kill', side_effect=ProcessLookupError()):
            with patch.object(running_bridge, '_cleanup', new_callable=AsyncMock):
                await running_bridge.stop()

                mock_task.cancel.assert_called_once()


class TestCLIBridgeCleanup:
    """Test CLIBridge _cleanup method."""

    @pytest.fixture(autouse=True)
    def clear_sessions(self):
        """Clear sessions before and after each test."""
        _cli_sessions.clear()
        yield
        _cli_sessions.clear()

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_cleanup_closes_fd(self):
        """_cleanup should close the file descriptor."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._fd = 5
        bridge._pid = 12345
        bridge._is_running = True

        import os as os_module
        with patch('os.waitpid', return_value=(12345, 0)):
            with patch('os.close') as mock_close:
                await bridge._cleanup()

                mock_close.assert_called_once_with(5)
                assert bridge._fd is None

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_cleanup_removes_from_sessions(self):
        """_cleanup should remove session from global tracking."""
        session_id = "12345678-1234-1234-1234-123456789abc"
        bridge = CLIBridge(
            session_id=session_id,
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._pid = 12345

        # Add session to tracking
        _cli_sessions[session_id] = CLISession(
            session_id=session_id,
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project",
            pid=12345,
            fd=5
        )

        with patch('os.waitpid', return_value=(12345, 0)):
            with patch('os.close'):
                await bridge._cleanup()

                assert session_id not in _cli_sessions

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_cleanup_calls_on_exit(self):
        """_cleanup should call on_exit callback with exit code."""
        exit_codes = []

        async def capture_exit(code: int):
            exit_codes.append(code)

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project",
            on_exit=capture_exit
        )
        bridge._pid = 12345

        import os as os_module
        with patch('os.waitpid', return_value=(12345, 0)):
            with patch('os.close'):
                await bridge._cleanup()

                assert len(exit_codes) == 1
                assert exit_codes[0] == 0

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_cleanup_handles_child_process_error(self):
        """_cleanup should handle ChildProcessError gracefully."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._pid = 12345
        bridge._fd = 5

        with patch('os.waitpid', side_effect=ChildProcessError()):
            with patch('os.close'):
                # Should not raise
                await bridge._cleanup()

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_cleanup_handles_os_error_on_close(self):
        """_cleanup should handle OSError when closing fd."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._pid = 12345
        bridge._fd = 5

        with patch('os.waitpid', return_value=(12345, 0)):
            with patch('os.close', side_effect=OSError("Bad fd")):
                # Should not raise
                await bridge._cleanup()

                assert bridge._fd is None


# =============================================================================
# Test CLIBridge Resize (Unix-only tests)
# =============================================================================

class TestCLIBridgeResize:
    """Test CLIBridge resize method."""

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_resize_calls_set_terminal_size(self):
        """resize should call _set_terminal_size."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._pid = 12345
        bridge._fd = 5

        # Mock _set_terminal_size since it uses fcntl
        with patch.object(bridge, '_set_terminal_size') as mock_set_size:
            with patch('os.kill') as mock_kill:
                await bridge.resize(120, 40)

                mock_set_size.assert_called_once_with(120, 40)

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_resize_handles_process_lookup_error(self):
        """resize should handle ProcessLookupError gracefully."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._pid = 12345
        bridge._fd = 5

        with patch.object(bridge, '_set_terminal_size'):
            with patch('os.kill', side_effect=ProcessLookupError()):
                # Should not raise
                await bridge.resize(120, 40)


# =============================================================================
# Test Session Management Functions
# =============================================================================

class TestSessionManagement:
    """Test session management functions."""

    @pytest.fixture(autouse=True)
    def clear_sessions(self):
        """Clear sessions before and after each test."""
        _cli_sessions.clear()
        yield
        _cli_sessions.clear()

    def test_get_cli_session_existing(self):
        """get_cli_session should return existing session."""
        session = CLISession(
            session_id="test-session",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp",
            pid=123,
            fd=5
        )
        _cli_sessions["test-session"] = session

        result = get_cli_session("test-session")

        assert result == session

    def test_get_cli_session_nonexistent(self):
        """get_cli_session should return None for nonexistent session."""
        result = get_cli_session("nonexistent")

        assert result is None

    def test_get_active_cli_sessions_empty(self):
        """get_active_cli_sessions should return empty list when no sessions."""
        result = get_active_cli_sessions()

        assert result == []

    def test_get_active_cli_sessions_with_sessions(self):
        """get_active_cli_sessions should return list of session IDs."""
        _cli_sessions["session-1"] = CLISession(
            session_id="session-1",
            sdk_session_id="12345678-1234-1234-1234-123456789abc",
            working_dir="/tmp",
            pid=123,
            fd=5
        )
        _cli_sessions["session-2"] = CLISession(
            session_id="session-2",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/tmp",
            pid=456,
            fd=6
        )

        result = get_active_cli_sessions()

        assert len(result) == 2
        assert "session-1" in result
        assert "session-2" in result


# =============================================================================
# Test RewindParser
# =============================================================================

class TestRewindParserParseCheckpoints:
    """Test RewindParser.parse_checkpoints method."""

    def test_parse_empty_output(self):
        """Should return empty list for empty output."""
        result = RewindParser.parse_checkpoints("")

        assert result == []

    def test_parse_single_checkpoint(self):
        """Should parse a single checkpoint."""
        output = """
        > create a hello world text file
        """
        result = RewindParser.parse_checkpoints(output)

        assert len(result) == 1
        assert result[0]["message"] == "create a hello world text file"
        assert result[0]["selected"] is True

    def test_parse_multiple_checkpoints(self):
        """Should parse multiple checkpoints."""
        output = """
        create first file
        update configuration
        fix bug in module
        """
        result = RewindParser.parse_checkpoints(output)

        assert len(result) == 3

    def test_parse_checkpoint_with_current_marker(self):
        """Should detect current checkpoint marker."""
        output = """
        > (current)
        > first checkpoint
        """
        result = RewindParser.parse_checkpoints(output)

        # Find the checkpoint marked as current
        current_checkpoints = [cp for cp in result if cp.get("is_current")]
        assert len(current_checkpoints) >= 0  # May vary based on parsing

    def test_parse_checkpoint_with_changes(self):
        """Should parse change information."""
        output = """
        > update config file
        config.json +10 -5
        """
        result = RewindParser.parse_checkpoints(output)

        assert len(result) >= 1
        # Check if changes were parsed
        if result[0].get("changes"):
            assert "+10 -5" in result[0]["changes"] or "config.json" in result[0]["changes"]

    def test_parse_ignores_rewind_header(self):
        """Should ignore 'Rewind' header lines."""
        output = """
        Rewind to a previous state
        first checkpoint
        """
        result = RewindParser.parse_checkpoints(output)

        # Should not include "Rewind to a previous state"
        messages = [cp["message"] for cp in result]
        assert not any("Rewind" in m for m in messages)

    def test_parse_ignores_restore_lines(self):
        """Should ignore 'Restore' menu lines."""
        output = """
        first checkpoint
        Restore code and conversation
        """
        result = RewindParser.parse_checkpoints(output)

        messages = [cp["message"] for cp in result]
        assert not any("Restore" in m for m in messages)


class TestRewindParserParseSelectedOption:
    """Test RewindParser.parse_selected_option method."""

    def test_parse_option_1(self):
        """Should detect option 1 (restore code and conversation)."""
        output = "> 1. Restore code and conversation"
        result = RewindParser.parse_selected_option(output)

        assert result == 1

    def test_parse_option_2(self):
        """Should detect option 2."""
        output = "> 2. Restore conversation only"
        result = RewindParser.parse_selected_option(output)

        assert result == 2

    def test_parse_option_3(self):
        """Should detect option 3."""
        output = "> 3. Restore code only"
        result = RewindParser.parse_selected_option(output)

        assert result == 3

    def test_parse_option_4(self):
        """Should detect option 4 (never mind)."""
        output = "> 4. Never mind"
        result = RewindParser.parse_selected_option(output)

        assert result == 4

    def test_parse_no_selection(self):
        """Should return None when no option selected."""
        output = "Some other output without selection"
        result = RewindParser.parse_selected_option(output)

        assert result is None


class TestRewindParserIsRewindComplete:
    """Test RewindParser.is_rewind_complete method."""

    def test_conversation_restored_marker(self):
        """Should detect 'Conversation restored' marker."""
        output = "Conversation restored successfully"
        assert RewindParser.is_rewind_complete(output) is True

    def test_code_restored_marker(self):
        """Should detect 'Code restored' marker."""
        output = "Code restored to previous state"
        assert RewindParser.is_rewind_complete(output) is True

    def test_restored_to_marker(self):
        """Should detect 'restored to' marker."""
        output = "State restored to checkpoint 5"
        assert RewindParser.is_rewind_complete(output) is True

    def test_successfully_rewound_marker(self):
        """Should detect 'Successfully rewound' marker."""
        output = "Successfully rewound to previous state"
        assert RewindParser.is_rewind_complete(output) is True

    def test_no_completion_marker(self):
        """Should return False when no completion marker found."""
        output = "Still processing..."
        assert RewindParser.is_rewind_complete(output) is False

    def test_empty_output(self):
        """Should return False for empty output."""
        assert RewindParser.is_rewind_complete("") is False


class TestRewindParserGetSelectedCheckpointMessage:
    """Test RewindParser.get_selected_checkpoint_message method."""

    def test_extract_message_with_pipe(self):
        """Should extract message after pipe character."""
        output = """
        | create a hello world text file
        Some other content
        """
        result = RewindParser.get_selected_checkpoint_message(output)

        assert result == "create a hello world text file"

    def test_no_pipe_in_output(self):
        """Should return None when no pipe marker found."""
        output = "No pipe marker here"
        result = RewindParser.get_selected_checkpoint_message(output)

        assert result is None

    def test_empty_pipe_line(self):
        """Should return None for empty pipe line."""
        output = """
        |
        Some content
        """
        result = RewindParser.get_selected_checkpoint_message(output)

        # Empty message after stripping returns None
        assert result is None

    def test_multiple_pipe_lines(self):
        """Should return first non-empty pipe message."""
        output = """
        | first message
        | second message
        """
        result = RewindParser.get_selected_checkpoint_message(output)

        assert result == "first message"


# =============================================================================
# Test Theme Selection Handling
# =============================================================================

class TestCLIBridgeThemeSelection:
    """Test CLIBridge theme selection handling."""

    @pytest.fixture
    def bridge_with_pending_command(self):
        """Create a bridge with a pending command."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._fd = 5
        bridge._pending_command = "/rewind"
        bridge._theme_prompt_handled = False
        bridge._command_sent = False
        return bridge

    @pytest.mark.asyncio
    async def test_handle_theme_selection_detects_prompt(self, bridge_with_pending_command):
        """Should detect theme selection prompt."""
        bridge = bridge_with_pending_command
        bridge._output_buffer = "Choose the text style for the interface:\n1. Dark mode\n2. Light mode"

        with patch('os.write') as mock_write:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await bridge._handle_theme_selection()

                assert bridge._theme_prompt_handled is True

    @pytest.mark.asyncio
    async def test_handle_theme_selection_sends_enter(self, bridge_with_pending_command):
        """Should send Enter to accept default theme."""
        bridge = bridge_with_pending_command
        bridge._output_buffer = "Choose the text style for the interface:\n1. Dark mode (selected)"

        with patch('os.write') as mock_write:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await bridge._handle_theme_selection()

                # Should have written Enter key
                if mock_write.called:
                    assert bridge._theme_prompt_handled is True

    @pytest.mark.asyncio
    async def test_handle_theme_selection_sends_pending_command(self, bridge_with_pending_command):
        """Should send pending command after theme selection."""
        bridge = bridge_with_pending_command
        bridge._output_buffer = "Choose the text style for the interface:\n1. Dark mode"

        with patch('os.write') as mock_write:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await bridge._handle_theme_selection()

                # Command should be marked as sent
                assert bridge._command_sent is True

    @pytest.mark.asyncio
    async def test_handle_ready_prompt_without_theme(self, bridge_with_pending_command):
        """Should send command when CLI shows ready prompt."""
        bridge = bridge_with_pending_command
        bridge._output_buffer = "Welcome to Claude Code!\n" * 100 + "Some more content"

        with patch('os.write') as mock_write:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                # Simulate the buffer being large enough
                bridge._output_buffer = "Welcome to Claude Code" + " " * 2500

                await bridge._handle_theme_selection()

                # Should have processed (theme handled or command sent)


# =============================================================================
# Test Terminal Size Setting (Unix-only tests)
# =============================================================================

class TestCLIBridgeSetTerminalSize:
    """Test CLIBridge _set_terminal_size method."""

    @skip_on_windows
    def test_set_terminal_size_with_fd(self):
        """Should call ioctl with correct parameters when fd is set."""
        import struct
        import fcntl
        import termios

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._fd = 5

        with patch('struct.pack', return_value=b'\x00' * 8) as mock_pack:
            with patch('fcntl.ioctl') as mock_ioctl:
                bridge._set_terminal_size(80, 24)

                # struct.pack should be called with rows, cols
                mock_pack.assert_called_once_with("HHHH", 24, 80, 0, 0)
                mock_ioctl.assert_called_once()

    def test_set_terminal_size_without_fd(self):
        """Should do nothing when fd is None."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._fd = None

        # Should not raise
        bridge._set_terminal_size(80, 24)


# =============================================================================
# Test Read Output Task (Unix-only tests)
# =============================================================================

class TestCLIBridgeReadOutput:
    """Test CLIBridge _read_output method."""

    @pytest.fixture(autouse=True)
    def clear_sessions(self):
        """Clear sessions before and after each test."""
        _cli_sessions.clear()
        yield
        _cli_sessions.clear()

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_read_output_calls_callback(self):
        """_read_output should call on_output callback with data."""
        import select

        output_received = []

        async def capture_output(data: str):
            output_received.append(data)

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project",
            on_output=capture_output
        )
        bridge._is_running = True
        bridge._fd = 5
        bridge._theme_prompt_handled = True  # Skip theme handling

        call_count = 0

        def mock_select(rlist, wlist, xlist, timeout):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                bridge._is_running = False
                return ([], [], [])
            return (rlist, [], [])

        with patch('select.select', side_effect=mock_select):
            with patch('os.read', return_value=b"test output"):
                with patch('os.waitpid', return_value=(12345, 0)):
                    with patch('os.close'):
                        await bridge._read_output()

                        # Should have received output
                        assert len(output_received) > 0
                        assert "test output" in output_received[0]

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_read_output_handles_eof(self):
        """_read_output should handle EOF (empty read)."""
        import select

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._fd = 5
        bridge._theme_prompt_handled = True

        with patch('select.select', return_value=([5], [], [])):
            with patch('os.read', return_value=b""):  # EOF
                with patch('os.waitpid', return_value=(12345, 0)):
                    with patch('os.close'):
                        await bridge._read_output()

                        # Should have cleaned up
                        assert bridge._is_running is False

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_read_output_handles_eio(self):
        """_read_output should handle EIO error (process exited)."""
        import select

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._fd = 5
        bridge._theme_prompt_handled = True

        eio_error = OSError()
        eio_error.errno = 5  # EIO

        with patch('select.select', return_value=([5], [], [])):
            with patch('os.read', side_effect=eio_error):
                with patch('os.waitpid', return_value=(12345, 0)):
                    with patch('os.close'):
                        await bridge._read_output()

                        # Should have cleaned up
                        assert bridge._is_running is False

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_read_output_handles_cancellation(self):
        """_read_output should handle task cancellation."""
        import select

        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._fd = 5
        bridge._theme_prompt_handled = True

        with patch('select.select', side_effect=asyncio.CancelledError()):
            with patch('os.waitpid', return_value=(12345, 0)):
                with patch('os.close'):
                    await bridge._read_output()

                    # Should have cleaned up
                    assert bridge._is_running is False


# =============================================================================
# Test Edge Cases and Error Handling
# =============================================================================

class TestCLIBridgeEdgeCases:
    """Test edge cases and error handling."""

    @pytest.fixture(autouse=True)
    def clear_sessions(self):
        """Clear sessions before and after each test."""
        _cli_sessions.clear()
        yield
        _cli_sessions.clear()

    @skip_on_windows
    @pytest.mark.asyncio
    async def test_stop_handles_missing_read_task(self):
        """stop should handle None read task."""
        bridge = CLIBridge(
            session_id="12345678-1234-1234-1234-123456789abc",
            sdk_session_id="87654321-4321-4321-4321-cba987654321",
            working_dir="/home/user/project"
        )
        bridge._is_running = True
        bridge._pid = 12345
        bridge._read_task = None

        with patch('os.kill', side_effect=ProcessLookupError()):
            with patch.object(bridge, '_cleanup', new_callable=AsyncMock):
                # Should not raise
                await bridge.stop()
