"""
JSONL Rewind Service

Direct manipulation of Claude's JSONL session files for bulletproof rewind functionality.
This bypasses the fragile PTY-based approach and directly truncates the conversation history.

How it works:
1. Claude stores all conversation history in ~/.claude/projects/[project]/[session_id].jsonl
2. Each line is a JSON object with uuid, parentUuid, type, message, etc.
3. When we truncate the file to a specific message UUID, the SDK will only see messages up to that point
4. On next resume, Claude rebuilds context from the truncated JSONL = rewound context

Key insight: The Claude Agent SDK reads JSONL fresh on each resume, so truncating the file
is equivalent to rewinding the conversation.
"""

import json
import logging
import os
import tempfile
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field, asdict

from app.core.config import settings
from app.core.jsonl_parser import get_project_dir_name

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """A checkpoint representing a point in conversation that can be rewound to"""
    uuid: str  # Message UUID from JSONL
    index: int  # Position in conversation (0-based)
    message_preview: str  # First 100 chars of user message
    full_message: str  # Complete user message
    timestamp: Optional[str] = None
    git_ref: Optional[str] = None  # Git snapshot reference (if code checkpoint exists)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RewindResult:
    """Result of a rewind operation"""
    success: bool
    message: str
    checkpoint_uuid: Optional[str] = None
    messages_removed: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class JSONLRewindService:
    """
    Service for rewinding Claude conversations by manipulating JSONL files.

    This is the bulletproof approach - no PTY, no terminal parsing, just direct file manipulation.
    """

    def __init__(self):
        self.claude_projects_dir = settings.get_claude_projects_dir

    def _get_jsonl_path(self, sdk_session_id: str, working_dir: str = "/workspace") -> Optional[Path]:
        """
        Get the path to a session's JSONL file.

        Args:
            sdk_session_id: The Claude SDK session ID (UUID)
            working_dir: The working directory used when the session was created

        Returns:
            Path to the JSONL file, or None if not found
        """
        # Try the expected project directory first
        project_dir_name = get_project_dir_name(working_dir)
        project_dir = self.claude_projects_dir / project_dir_name

        if project_dir.exists():
            jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
            if jsonl_path.exists():
                return jsonl_path

        # Search all project directories as fallback
        if self.claude_projects_dir.exists():
            for proj_dir in self.claude_projects_dir.iterdir():
                if proj_dir.is_dir():
                    jsonl_path = proj_dir / f"{sdk_session_id}.jsonl"
                    if jsonl_path.exists():
                        logger.debug(f"Found JSONL in alternate project dir: {jsonl_path}")
                        return jsonl_path

        return None

    def _parse_jsonl_entries(self, jsonl_path: Path) -> List[Dict[str, Any]]:
        """
        Parse all entries from a JSONL file.

        Returns list of parsed JSON objects, preserving order.
        """
        entries = []
        try:
            with open(jsonl_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entry['_line_num'] = line_num
                        entry['_raw_line'] = line
                        entries.append(entry)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse line {line_num}: {e}")
                        # Keep the raw line for preservation during truncation
                        entries.append({
                            '_line_num': line_num,
                            '_raw_line': line,
                            '_parse_error': str(e)
                        })
        except Exception as e:
            logger.error(f"Failed to read JSONL file {jsonl_path}: {e}")

        return entries

    def _extract_message_text(self, entry: Dict[str, Any]) -> str:
        """Extract the text content from a message entry."""
        message = entry.get('message', {})
        content = message.get('content', '')

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        text_parts.append(block.get('text', ''))
            return ' '.join(text_parts)

        return ''

    def get_checkpoints(
        self,
        sdk_session_id: str,
        working_dir: str = "/workspace"
    ) -> List[Checkpoint]:
        """
        Get all available checkpoints (user messages) for a session.

        Each user message represents a potential rewind point.

        Args:
            sdk_session_id: The Claude SDK session ID
            working_dir: Working directory for the project

        Returns:
            List of Checkpoint objects, ordered by conversation position
        """
        jsonl_path = self._get_jsonl_path(sdk_session_id, working_dir)
        if not jsonl_path:
            logger.warning(f"JSONL file not found for session {sdk_session_id}")
            return []

        entries = self._parse_jsonl_entries(jsonl_path)
        checkpoints = []
        checkpoint_index = 0

        for entry in entries:
            # Skip non-message entries
            entry_type = entry.get('type')
            if entry_type not in ('user', 'assistant'):
                continue

            # Skip meta messages (slash commands, system prompts)
            if entry.get('isMeta'):
                continue

            # Skip sidechain messages (alternate conversation branches)
            if entry.get('isSidechain'):
                continue

            # Only user messages are checkpoints (rewind points)
            message = entry.get('message', {})
            role = message.get('role')

            if entry_type == 'user' and role == 'user':
                # Extract message text
                text = self._extract_message_text(entry)

                # Skip empty messages and tool results
                if not text or text.startswith('<'):
                    continue

                # Check if content is tool_result (array with tool_result blocks)
                content = message.get('content', '')
                if isinstance(content, list):
                    has_tool_result = any(
                        isinstance(block, dict) and block.get('type') == 'tool_result'
                        for block in content
                    )
                    if has_tool_result:
                        continue

                checkpoint = Checkpoint(
                    uuid=entry.get('uuid', ''),
                    index=checkpoint_index,
                    message_preview=text[:100] + ('...' if len(text) > 100 else ''),
                    full_message=text,
                    timestamp=entry.get('timestamp')
                )
                checkpoints.append(checkpoint)
                checkpoint_index += 1

        logger.info(f"Found {len(checkpoints)} checkpoints for session {sdk_session_id}")
        return checkpoints

    def truncate_to_checkpoint(
        self,
        sdk_session_id: str,
        target_uuid: str,
        working_dir: str = "/workspace",
        include_response: bool = True
    ) -> RewindResult:
        """
        Truncate the JSONL file to a specific checkpoint (user message).

        This is the core rewind operation. After truncation, the next SDK resume
        will only see messages up to (and optionally including the response to)
        the target checkpoint.

        Args:
            sdk_session_id: The Claude SDK session ID
            target_uuid: The UUID of the user message to rewind to
            working_dir: Working directory for the project
            include_response: If True, keep the assistant's response to the target message
                            If False, truncate immediately after the user message

        Returns:
            RewindResult with success status and details
        """
        jsonl_path = self._get_jsonl_path(sdk_session_id, working_dir)
        if not jsonl_path:
            return RewindResult(
                success=False,
                message="JSONL file not found",
                error=f"No JSONL file found for session {sdk_session_id}"
            )

        entries = self._parse_jsonl_entries(jsonl_path)
        if not entries:
            return RewindResult(
                success=False,
                message="JSONL file is empty",
                error="No entries found in JSONL file"
            )

        # Find the target message index
        target_index = None
        for i, entry in enumerate(entries):
            if entry.get('uuid') == target_uuid:
                target_index = i
                break

        if target_index is None:
            return RewindResult(
                success=False,
                message="Target checkpoint not found",
                error=f"Message with UUID {target_uuid} not found in JSONL"
            )

        # Determine where to truncate
        if include_response:
            # Find the next user message after target, truncate before it
            # This keeps the assistant's response to the target message
            truncate_before = None
            for i in range(target_index + 1, len(entries)):
                entry = entries[i]
                if entry.get('type') == 'user' and entry.get('message', {}).get('role') == 'user':
                    # Skip tool results
                    content = entry.get('message', {}).get('content', '')
                    if isinstance(content, list):
                        has_tool_result = any(
                            isinstance(block, dict) and block.get('type') == 'tool_result'
                            for block in content
                        )
                        if has_tool_result:
                            continue
                    # Found the next user message
                    truncate_before = i
                    break

            if truncate_before is None:
                # Target is the last user message, nothing to truncate
                return RewindResult(
                    success=True,
                    message="Already at the target checkpoint (nothing to rewind)",
                    checkpoint_uuid=target_uuid,
                    messages_removed=0
                )

            keep_entries = entries[:truncate_before]
        else:
            # Truncate BEFORE the target user message (remove it entirely)
            # This allows the user to re-prompt with the message content in the input field
            keep_entries = entries[:target_index]

        messages_removed = len(entries) - len(keep_entries)

        if messages_removed == 0:
            return RewindResult(
                success=True,
                message="Already at the target checkpoint",
                checkpoint_uuid=target_uuid,
                messages_removed=0
            )

        # Perform atomic write: write to temp file, then rename
        try:
            # Create temp file in same directory for atomic rename
            temp_fd, temp_path = tempfile.mkstemp(
                dir=jsonl_path.parent,
                prefix='.rewind_',
                suffix='.jsonl'
            )

            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                    for entry in keep_entries:
                        # Use raw line if available (preserves exact formatting)
                        raw_line = entry.get('_raw_line')
                        if raw_line:
                            f.write(raw_line + '\n')
                        else:
                            # Fallback: re-serialize (shouldn't happen normally)
                            clean_entry = {k: v for k, v in entry.items() if not k.startswith('_')}
                            f.write(json.dumps(clean_entry) + '\n')

                # Atomic rename
                shutil.move(temp_path, jsonl_path)

                logger.info(f"Rewound session {sdk_session_id} to checkpoint {target_uuid}, removed {messages_removed} entries")

                return RewindResult(
                    success=True,
                    message=f"Successfully rewound to checkpoint, removed {messages_removed} messages",
                    checkpoint_uuid=target_uuid,
                    messages_removed=messages_removed
                )

            except Exception:
                # Clean up temp file on error
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

        except Exception as e:
            logger.error(f"Failed to truncate JSONL: {e}", exc_info=True)
            return RewindResult(
                success=False,
                message="Failed to truncate JSONL file",
                error=str(e)
            )

    def get_last_message_uuid(
        self,
        sdk_session_id: str,
        working_dir: str = "/workspace"
    ) -> Optional[str]:
        """
        Get the UUID of the last user message in the session.

        Useful for creating checkpoints before starting a new turn.

        Returns:
            UUID of last user message, or None if not found
        """
        checkpoints = self.get_checkpoints(sdk_session_id, working_dir)
        if checkpoints:
            return checkpoints[-1].uuid
        return None

    def backup_jsonl(
        self,
        sdk_session_id: str,
        working_dir: str = "/workspace"
    ) -> Optional[Path]:
        """
        Create a backup of the JSONL file before rewind.

        Returns:
            Path to backup file, or None if backup failed
        """
        jsonl_path = self._get_jsonl_path(sdk_session_id, working_dir)
        if not jsonl_path:
            return None

        backup_path = jsonl_path.with_suffix(f'.jsonl.backup.{datetime.now().strftime("%Y%m%d_%H%M%S")}')

        try:
            shutil.copy2(jsonl_path, backup_path)
            logger.info(f"Created JSONL backup at {backup_path}")
            return backup_path
        except Exception as e:
            logger.error(f"Failed to create JSONL backup: {e}")
            return None


# Global service instance
jsonl_rewind_service = JSONLRewindService()
