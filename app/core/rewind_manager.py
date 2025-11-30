"""
Rewind Manager for non-interactive rewind operations.

This module provides a settings-based approach to rewind functionality,
similar to how Claude Code authentication works via settings.json.
Instead of spawning an interactive PTY, it:
1. Reads conversation checkpoints from the session
2. Configures rewind in settings.json
3. Lets Claude handle the rewind on next interaction
"""

import json
import logging
import os
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class RewindManager:
    """
    Non-interactive rewind via settings configuration.

    This approach mirrors how Claude Code auth works:
    - Read/write to ~/.claude/settings.json
    - Configure rewind parameters
    - Let Claude CLI handle the actual rewind
    """

    def __init__(self):
        """Initialize rewind manager"""
        home = Path(os.environ.get('HOME', '/home/appuser'))
        self.config_dir = home / '.claude'
        self.settings_file = self.config_dir / 'settings.json'

    def _read_settings(self) -> Dict[str, Any]:
        """Read current settings from settings.json"""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Could not read settings.json: {e}")
        return {}

    def _write_settings(self, settings_data: Dict[str, Any]) -> bool:
        """Write settings to settings.json"""
        try:
            # Ensure directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)

            with open(self.settings_file, 'w') as f:
                json.dump(settings_data, f, indent=2)
            return True
        except IOError as e:
            logger.error(f"Could not write settings.json: {e}")
            return False

    def get_session_checkpoints(self, sdk_session_id: str, working_dir: str) -> Dict[str, Any]:
        """
        Get available checkpoints for a session by running claude --print.

        This uses the Claude CLI to get session information including
        checkpoints that can be rewound to.
        """
        try:
            # Find claude executable
            claude_cmd = shutil.which('claude')
            if not claude_cmd:
                return {
                    "success": False,
                    "error": "Claude CLI not found",
                    "checkpoints": []
                }

            home_env = os.environ.get('HOME', str(Path.home()))

            # Run claude with --print to get session info
            # Using --resume to target specific session and --print to get JSON output
            result = subprocess.run(
                [claude_cmd, '--resume', sdk_session_id, '--print'],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=working_dir,
                env={**os.environ, 'HOME': home_env}
            )

            if result.returncode != 0:
                logger.warning(f"Claude --print failed: {result.stderr}")
                return {
                    "success": False,
                    "error": result.stderr or "Failed to get session info",
                    "checkpoints": []
                }

            # Parse the output - it should be JSON with conversation history
            try:
                session_data = json.loads(result.stdout)
                checkpoints = self._extract_checkpoints(session_data)
                return {
                    "success": True,
                    "checkpoints": checkpoints,
                    "session_id": sdk_session_id
                }
            except json.JSONDecodeError:
                # Fallback: parse text output
                checkpoints = self._parse_text_checkpoints(result.stdout)
                return {
                    "success": True,
                    "checkpoints": checkpoints,
                    "session_id": sdk_session_id
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Timeout getting session info",
                "checkpoints": []
            }
        except Exception as e:
            logger.error(f"Error getting checkpoints: {e}")
            return {
                "success": False,
                "error": str(e),
                "checkpoints": []
            }

    def _extract_checkpoints(self, session_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract checkpoints from session JSON data"""
        checkpoints = []

        # Look for messages in the session data
        messages = session_data.get('messages', [])
        if not messages:
            messages = session_data.get('conversation', [])

        for i, msg in enumerate(messages):
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, list):
                    # Handle content blocks
                    text_parts = [c.get('text', '') for c in content if c.get('type') == 'text']
                    content = ' '.join(text_parts)

                # Truncate long messages
                display_content = content[:100] + '...' if len(content) > 100 else content

                checkpoints.append({
                    "index": i,
                    "message": display_content,
                    "full_message": content,
                    "timestamp": msg.get('timestamp'),
                    "is_current": i == len(messages) - 1
                })

        return checkpoints

    def _parse_text_checkpoints(self, output: str) -> List[Dict[str, Any]]:
        """Parse checkpoints from text output (fallback)"""
        checkpoints = []
        lines = output.strip().split('\n')

        for i, line in enumerate(lines):
            line = line.strip()
            if line and not line.startswith('#'):
                checkpoints.append({
                    "index": i,
                    "message": line[:100] + '...' if len(line) > 100 else line,
                    "full_message": line,
                    "is_current": False
                })

        if checkpoints:
            checkpoints[-1]["is_current"] = True

        return checkpoints

    def execute_rewind(
        self,
        sdk_session_id: str,
        checkpoint_index: int,
        restore_option: int,
        working_dir: str
    ) -> Dict[str, Any]:
        """
        Execute a rewind operation using the Claude CLI.

        This runs `claude --resume <session_id>` and programmatically
        sends the rewind command with the specified options.

        Args:
            sdk_session_id: The Claude SDK session ID
            checkpoint_index: Index of the checkpoint to rewind to
            restore_option: 1=code+conversation, 2=conversation, 3=code, 4=cancel
            working_dir: Working directory for the operation

        Returns:
            Dict with success status and result details
        """
        try:
            # Find claude executable
            claude_cmd = shutil.which('claude')
            if not claude_cmd:
                return {
                    "success": False,
                    "error": "Claude CLI not found"
                }

            home_env = os.environ.get('HOME', str(Path.home()))

            # Build the input sequence for non-interactive rewind
            # Format: /rewind, then navigate to checkpoint, then select option
            # We'll use stdin to send these commands

            # Calculate how many "down" presses needed to reach checkpoint
            # and which option number to select
            input_sequence = self._build_rewind_input(checkpoint_index, restore_option)

            logger.info(f"Executing rewind: session={sdk_session_id}, checkpoint={checkpoint_index}, option={restore_option}")

            # Run claude with input piped in
            result = subprocess.run(
                [claude_cmd, '--resume', sdk_session_id],
                input=input_sequence,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=working_dir,
                env={**os.environ, 'HOME': home_env, 'TERM': 'dumb'}
            )

            # Check for success indicators in output
            output = result.stdout + result.stderr

            if any(marker in output.lower() for marker in ['restored', 'rewound', 'success']):
                return {
                    "success": True,
                    "message": "Rewind completed successfully",
                    "checkpoint_index": checkpoint_index,
                    "restore_option": restore_option,
                    "output": output[-500:] if len(output) > 500 else output
                }
            elif result.returncode == 0:
                return {
                    "success": True,
                    "message": "Rewind command executed",
                    "checkpoint_index": checkpoint_index,
                    "restore_option": restore_option,
                    "output": output[-500:] if len(output) > 500 else output
                }
            else:
                return {
                    "success": False,
                    "error": f"Rewind failed: {output[-500:]}" if output else "Unknown error",
                    "return_code": result.returncode
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Rewind operation timed out"
            }
        except Exception as e:
            logger.error(f"Error executing rewind: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }

    def _build_rewind_input(self, checkpoint_index: int, restore_option: int) -> str:
        """
        Build the input sequence for the rewind command.

        This creates a string that when piped to claude CLI will:
        1. Send /rewind command
        2. Navigate to the correct checkpoint (using arrow keys or numbers)
        3. Select the restore option
        """
        # Start with /rewind command
        parts = ["/rewind\n"]

        # Wait a moment, then navigate to checkpoint
        # Use down arrow key to move through checkpoints
        # \x1b[B is the ANSI escape code for down arrow
        for _ in range(checkpoint_index):
            parts.append("\x1b[B")  # Down arrow

        # Press Enter to select checkpoint
        parts.append("\n")

        # Select restore option (1-4)
        # Navigate to the option if needed
        for _ in range(restore_option - 1):
            parts.append("\x1b[B")  # Down arrow

        # Press Enter to confirm
        parts.append("\n")

        return "".join(parts)

    def get_pending_rewind(self) -> Optional[Dict[str, Any]]:
        """Check if there's a pending rewind configuration"""
        settings = self._read_settings()
        return settings.get('pendingRewind')

    def configure_pending_rewind(
        self,
        session_id: str,
        sdk_session_id: str,
        checkpoint_index: int,
        checkpoint_message: str,
        restore_option: int
    ) -> bool:
        """
        Configure a pending rewind in settings.

        This approach stores the rewind intent in settings.json,
        which could be picked up by Claude on the next interaction
        (if Claude supported this mechanism - for now we execute immediately).
        """
        settings = self._read_settings()

        settings['pendingRewind'] = {
            'sessionId': session_id,
            'sdkSessionId': sdk_session_id,
            'checkpointIndex': checkpoint_index,
            'checkpointMessage': checkpoint_message,
            'restoreOption': restore_option,
            'timestamp': datetime.now().isoformat()
        }

        return self._write_settings(settings)

    def clear_pending_rewind(self) -> bool:
        """Clear any pending rewind configuration"""
        settings = self._read_settings()

        if 'pendingRewind' in settings:
            del settings['pendingRewind']
            return self._write_settings(settings)

        return True


# Global instance
rewind_manager = RewindManager()
