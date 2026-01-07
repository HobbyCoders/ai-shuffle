"""
CLI Bridge for interactive Claude Code commands

This module provides PTY-based interaction with the Claude Code CLI
for commands that require interactive terminal input (like /resume).

NOTE: The /rewind command has been moved to a new bulletproof implementation
that uses direct JSONL manipulation instead of PTY-based CLI interaction.
See: app/core/jsonl_rewind.py

This module is still used for /resume and other interactive commands that
genuinely require terminal interaction.

NOTE: PTY functionality is only available on Unix-like systems.
On Windows, interactive CLI commands are not supported.
"""

import asyncio
import logging
import os
import sys
from typing import Optional, Dict, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime
import re

# PTY-related imports are Unix-only
PTY_AVAILABLE = False
if sys.platform != 'win32':
    try:
        import pty
        import select
        import struct
        import fcntl
        import termios
        import signal
        import pwd
        PTY_AVAILABLE = True
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# Pattern for validating UUID format (used for SDK session IDs)
UUID_PATTERN = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', re.IGNORECASE)


def is_pty_available() -> bool:
    """Check if PTY functionality is available on this platform."""
    return PTY_AVAILABLE


def validate_session_id(session_id: str) -> bool:
    """
    Validate that a session ID is a valid UUID format.
    This prevents command injection via malicious session IDs.
    """
    if not session_id:
        return False
    return bool(UUID_PATTERN.match(session_id))


@dataclass
class CLISession:
    """Track state for an active CLI session"""
    session_id: str  # Our session ID
    sdk_session_id: str  # Claude SDK session ID for --resume
    working_dir: str  # Working directory for the CLI
    pid: int  # Process ID
    fd: int  # File descriptor for PTY master
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    command: Optional[str] = None  # Current command being executed
    output_buffer: str = ""  # Accumulated output


# Track active CLI sessions
_cli_sessions: Dict[str, CLISession] = {}


class CLIBridge:
    """
    Bridge for interacting with Claude Code CLI via PTY.

    This allows running interactive commands like /rewind that require
    terminal input (arrow keys, Enter, etc.).

    NOTE: PTY functionality is only available on Unix-like systems.
    On Windows, this class will return errors for all operations.
    """

    def __init__(
        self,
        session_id: str,
        sdk_session_id: str,
        working_dir: str,
        on_output: Optional[Callable[[str], Awaitable[None]]] = None,
        on_exit: Optional[Callable[[int], Awaitable[None]]] = None
    ):
        self.session_id = session_id
        self.sdk_session_id = sdk_session_id
        self.working_dir = working_dir
        self.on_output = on_output
        self.on_exit = on_exit

        self._pid: Optional[int] = None
        self._fd: Optional[int] = None
        self._read_task: Optional[asyncio.Task] = None
        self._is_running = False

        # For handling Claude's theme selection prompt
        self._output_buffer: str = ""
        self._theme_prompt_handled: bool = False
        self._pending_command: Optional[str] = None
        self._command_sent: bool = False

    async def start(self, command: str = "/rewind") -> bool:
        """
        Start the Claude CLI with the specified command.

        Args:
            command: The slash command to execute (e.g., "/rewind")

        Returns:
            True if started successfully, False otherwise
        """
        # Check if PTY is available (Unix only)
        if not PTY_AVAILABLE:
            logger.error("PTY not available on this platform (Windows). Interactive CLI commands are not supported.")
            if self.on_output:
                await self.on_output("\n[ERROR] Interactive CLI commands are not supported on Windows.\n")
            if self.on_exit:
                await self.on_exit(1)
            return False

        if self._is_running:
            logger.warning(f"CLI bridge already running for session {self.session_id}")
            return False

        # Security: Validate session IDs to prevent command injection
        if not validate_session_id(self.session_id):
            logger.error(f"Invalid session_id format: {self.session_id}")
            return False
        if not validate_session_id(self.sdk_session_id):
            logger.error(f"Invalid sdk_session_id format: {self.sdk_session_id}")
            return False

        try:
            # IMPORTANT: Ensure onboarding is complete BEFORE spawning CLI
            # This prevents the Claude CLI from showing the login/onboarding wizard
            # even when credentials exist. Must be done in parent process before fork.
            from app.core.auth import auth_service
            home_dir = pwd.getpwuid(os.getuid()).pw_dir
            creds_path = os.path.join(home_dir, ".claude", ".credentials.json")

            if os.path.exists(creds_path) and os.path.getsize(creds_path) > 0:
                logger.info(f"Credentials found at {creds_path}, ensuring onboarding is complete")
                auth_service._ensure_onboarding_complete()
            else:
                logger.warning(f"Credentials not found or empty at {creds_path} - CLI may prompt for login")

            # Create PTY
            pid, fd = pty.fork()

            if pid == 0:
                # Child process - execute claude CLI
                os.chdir(self.working_dir)

                # Set up environment - ensure HOME and user directories are set
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["COLORTERM"] = "truecolor"

                # Ensure HOME is set correctly for Claude credentials
                # This is critical - Claude looks for ~/.claude/.credentials.json
                env["HOME"] = home_dir

                # Ensure PATH includes common binary locations
                if "PATH" not in env:
                    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"

                # Execute claude with --resume to use existing session
                # Then we'll send the command via stdin
                args = [
                    "claude",
                    "--resume", self.sdk_session_id,
                ]

                os.execvpe("claude", args, env)

            else:
                # Parent process
                self._pid = pid
                self._fd = fd
                self._is_running = True

                # Set non-blocking mode
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Set terminal size (80x24 is standard)
                self._set_terminal_size(80, 24)

                # Store session
                cli_session = CLISession(
                    session_id=self.session_id,
                    sdk_session_id=self.sdk_session_id,
                    working_dir=self.working_dir,
                    pid=pid,
                    fd=fd,
                    command=command
                )
                _cli_sessions[self.session_id] = cli_session

                # Start reading output with theme detection
                self._output_buffer = ""
                self._theme_prompt_handled = False
                self._pending_command = command
                self._read_task = asyncio.create_task(self._read_output())

                # The actual command will be sent after theme selection is handled
                # (see _handle_theme_selection in _read_output)

                logger.info(f"Started CLI bridge for session {self.session_id}, pid={pid}, sdk_session={self.sdk_session_id}")
                return True

        except Exception as e:
            logger.error(f"Failed to start CLI bridge: {e}", exc_info=True)
            return False

    def _set_terminal_size(self, cols: int, rows: int):
        """Set the terminal size"""
        if self._fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)

    async def _handle_theme_selection(self):
        """
        Detect and auto-confirm the theme selection prompt.

        Claude CLI v2.x shows a theme selection on first interactive launch.
        This detects that prompt and auto-presses Enter to accept the default
        (Dark mode), then sends the actual command.
        """
        # Check if we see the theme selection prompt
        # The prompt shows "Choose the text style" with numbered options
        if "Choose the text style" in self._output_buffer and "Dark mode" in self._output_buffer:
            logger.info(f"Detected theme selection prompt for session {self.session_id}, auto-confirming...")

            # Mark as handled so we don't try again
            self._theme_prompt_handled = True

            # Wait a tiny bit for the prompt to fully render
            await asyncio.sleep(0.3)

            # Send Enter to accept the default selection (Dark mode is pre-selected)
            await self.send_input("\r")

            # Wait for CLI to process the selection
            await asyncio.sleep(0.8)

            # Now send the actual command if we have one
            if self._pending_command and not self._command_sent:
                self._command_sent = True
                logger.info(f"Sending pending command: {self._pending_command}")
                await self.send_input(self._pending_command + "\n")

        # Also check for ready prompt (in case theme selection was already done)
        # The > prompt or input area indicates CLI is ready
        elif not self._command_sent and (
            # Claude shows a prompt like ╭─ when ready for input
            "╭─" in self._output_buffer or
            # Or could be past the welcome banner
            ("Welcome to Claude Code" in self._output_buffer and len(self._output_buffer) > 2000)
        ):
            self._theme_prompt_handled = True

            if self._pending_command:
                self._command_sent = True
                await asyncio.sleep(0.5)
                logger.info(f"CLI ready, sending pending command: {self._pending_command}")
                await self.send_input(self._pending_command + "\n")

    async def resize(self, cols: int, rows: int):
        """Resize the terminal"""
        self._set_terminal_size(cols, rows)
        # Send SIGWINCH to notify process of resize
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    async def send_input(self, data: str):
        """Send input to the CLI process"""
        if not self._is_running or self._fd is None:
            logger.warning(f"Cannot send input - CLI not running for session {self.session_id}")
            return

        try:
            os.write(self._fd, data.encode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to send input: {e}")

    async def send_key(self, key: str):
        """
        Send a special key to the CLI process.

        Supported keys:
        - "up", "down", "left", "right" - Arrow keys
        - "enter" - Enter/Return
        - "escape" - Escape
        - "tab" - Tab
        - "backspace" - Backspace
        - "1", "2", "3", "4" - Number keys
        """
        key_map = {
            "up": "\x1b[A",
            "down": "\x1b[B",
            "right": "\x1b[C",
            "left": "\x1b[D",
            "enter": "\r",
            "escape": "\x1b",
            "tab": "\t",
            "backspace": "\x7f",
        }

        # Handle single character keys (numbers, letters)
        if len(key) == 1:
            await self.send_input(key)
        elif key.lower() in key_map:
            await self.send_input(key_map[key.lower()])
        else:
            logger.warning(f"Unknown key: {key}")

    async def _read_output(self):
        """Read output from the CLI process and send to callback"""
        # Event loop reference available if needed for future async operations
        _loop = asyncio.get_event_loop()

        try:
            while self._is_running and self._fd is not None:
                # Use select to wait for data with timeout
                readable, _, _ = select.select([self._fd], [], [], 0.1)

                if readable:
                    try:
                        data = os.read(self._fd, 4096)
                        if data:
                            output = data.decode("utf-8", errors="replace")

                            # Accumulate output for theme detection
                            self._output_buffer += output

                            # Check for theme selection prompt and auto-confirm
                            if not self._theme_prompt_handled:
                                await self._handle_theme_selection()

                            # Store in buffer
                            if self.session_id in _cli_sessions:
                                _cli_sessions[self.session_id].output_buffer += output

                            # Send to callback
                            if self.on_output:
                                await self.on_output(output)
                        else:
                            # EOF - process exited
                            break
                    except OSError as e:
                        if e.errno == 5:  # EIO - process exited
                            break
                        raise

                # Small yield to prevent blocking
                await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            logger.info(f"Read task cancelled for session {self.session_id}")
        except Exception as e:
            logger.error(f"Error reading CLI output: {e}", exc_info=True)
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Clean up resources"""
        self._is_running = False

        exit_code = 0
        if self._pid:
            try:
                _, status = os.waitpid(self._pid, os.WNOHANG)
                exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
            except ChildProcessError:
                pass

        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        # Remove from active sessions
        if self.session_id in _cli_sessions:
            del _cli_sessions[self.session_id]

        # Notify exit
        if self.on_exit:
            await self.on_exit(exit_code)

        logger.info(f"CLI bridge cleaned up for session {self.session_id}, exit_code={exit_code}")

    async def stop(self):
        """Stop the CLI process"""
        if not self._is_running:
            return

        self._is_running = False

        # Send SIGTERM
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # Cancel read task
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        await self._cleanup()

    @property
    def is_running(self) -> bool:
        return self._is_running


def get_cli_session(session_id: str) -> Optional[CLISession]:
    """Get an active CLI session by session ID"""
    return _cli_sessions.get(session_id)


def get_active_cli_sessions() -> list:
    """Get list of active CLI session IDs"""
    return list(_cli_sessions.keys())


class RewindParser:
    """
    Parser for /rewind command output.

    Extracts checkpoint information and selected options from CLI output.
    """

    @staticmethod
    def parse_checkpoints(output: str) -> list:
        """
        Parse checkpoint list from /rewind output.

        Returns list of checkpoints:
        [
            {"index": 0, "message": "...", "changes": "+1 -0", "is_current": True},
            ...
        ]
        """
        checkpoints = []
        lines = output.split("\n")

        current_checkpoint = None
        for line in lines:
            # Match checkpoint lines like "  create a hello world text file"
            # or "> (current)"
            stripped = line.strip()

            if stripped == "> (current)" or stripped == "(current)":
                if current_checkpoint:
                    current_checkpoint["is_current"] = True
            elif stripped.startswith(">"):
                # Selected item
                text = stripped[1:].strip()
                if text and text != "(current)":
                    current_checkpoint = {
                        "index": len(checkpoints),
                        "message": text,
                        "changes": "",
                        "is_current": False,
                        "selected": True
                    }
                    checkpoints.append(current_checkpoint)
            elif stripped and not stripped.startswith("Rewind") and not stripped.startswith("Restore"):
                # Potential checkpoint or change info
                # Check if it's a change line like "hello.txt +1 -0"
                change_match = re.search(r'\+\d+\s+-\d+', stripped)
                if change_match and current_checkpoint:
                    current_checkpoint["changes"] = stripped
                elif not stripped.startswith("Enter") and not stripped.startswith("No code"):
                    # New checkpoint
                    current_checkpoint = {
                        "index": len(checkpoints),
                        "message": stripped,
                        "changes": "",
                        "is_current": False,
                        "selected": False
                    }
                    checkpoints.append(current_checkpoint)

        return checkpoints

    @staticmethod
    def parse_selected_option(output: str) -> Optional[int]:
        """
        Parse which restore option was selected (1-4).

        Returns:
            1 = Restore code and conversation
            2 = Restore conversation
            3 = Restore code
            4 = Never mind
            None = Not determined yet
        """
        # Look for the selected option marker
        if "> 1." in output or "Restore code and conversation" in output and "> 1" in output:
            return 1
        elif "> 2." in output:
            return 2
        elif "> 3." in output:
            return 3
        elif "> 4." in output or "Never mind" in output and "> 4" in output:
            return 4
        return None

    @staticmethod
    def is_rewind_complete(output: str) -> bool:
        """Check if rewind operation has completed"""
        # Look for completion indicators
        completion_markers = [
            "Conversation restored",
            "Code restored",
            "restored to",
            "Successfully rewound"
        ]
        return any(marker in output for marker in completion_markers)

    @staticmethod
    def get_selected_checkpoint_message(output: str) -> Optional[str]:
        """Extract the message text of the selected checkpoint"""
        # Look for pattern like:
        # | create a hello world text file
        lines = output.split("\n")
        for line in lines:
            if line.strip().startswith("|"):
                message = line.strip()[1:].strip()
                if message:
                    return message
        return None
