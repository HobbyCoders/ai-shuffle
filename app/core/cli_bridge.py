"""
CLI Bridge for interactive Claude Code commands

This module provides PTY-based interaction with the Claude Code CLI
for commands that require interactive terminal input (like /rewind).
"""

import asyncio
import logging
import os
import pty
import select
import struct
import fcntl
import termios
import signal
from typing import Optional, Dict, Any, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime
import re

logger = logging.getLogger(__name__)


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

    async def start(self, command: str = "/rewind") -> bool:
        """
        Start the Claude CLI with the specified command.

        Args:
            command: The slash command to execute (e.g., "/rewind")

        Returns:
            True if started successfully, False otherwise
        """
        if self._is_running:
            logger.warning(f"CLI bridge already running for session {self.session_id}")
            return False

        try:
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
                import pwd
                home_dir = pwd.getpwuid(os.getuid()).pw_dir
                env["HOME"] = home_dir

                # Ensure PATH includes common binary locations
                if "PATH" not in env:
                    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"

                # Verify credentials file exists (debug logging)
                creds_path = os.path.join(home_dir, ".claude", ".credentials.json")
                if not os.path.exists(creds_path):
                    # Can't use logger in child process, write to stderr
                    import sys
                    sys.stderr.write(f"WARNING: Credentials file not found at {creds_path}\n")

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

                # Start reading output
                self._read_task = asyncio.create_task(self._read_output())

                # Wait a moment for claude to start, then send the command
                await asyncio.sleep(1.0)  # Give more time for CLI to initialize
                await self.send_input(command + "\n")

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
        loop = asyncio.get_event_loop()

        try:
            while self._is_running and self._fd is not None:
                # Use select to wait for data with timeout
                readable, _, _ = select.select([self._fd], [], [], 0.1)

                if readable:
                    try:
                        data = os.read(self._fd, 4096)
                        if data:
                            output = data.decode("utf-8", errors="replace")

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
