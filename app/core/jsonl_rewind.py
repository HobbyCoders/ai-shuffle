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
from typing import List, Dict, Any, Mapping, Optional, Sequence, Tuple
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
    message_preview: str  # First 100 chars of message
    full_message: str  # Complete message
    role: str = "user"  # "user" or "assistant" - which type of message this is
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
    remaining_user_turns: Optional[int] = None  # User message count after truncation
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _next_real_user_index(
    entries: Sequence[Mapping[str, Any]], start_index: int
) -> Optional[int]:
    """Index of the next genuine user prompt after ``start_index``.

    Skips ``user`` entries that only carry tool_result blocks (those are the
    SDK echoing tool output, not a new human turn). Returns ``None`` when no
    further user prompt exists.
    """
    for i in range(start_index + 1, len(entries)):
        entry = entries[i]
        if entry.get('type') == 'user' and entry.get('message', {}).get('role') == 'user':
            content = entry.get('message', {}).get('content', '')
            if isinstance(content, list):
                has_tool_result = any(
                    isinstance(block, dict) and block.get('type') == 'tool_result'
                    for block in content
                )
                if has_tool_result:
                    continue
            return i
    return None


def compute_rewind_keep_count(
    entries: Sequence[Mapping[str, Any]],
    target_uuid: str,
    include_response: bool,
    target_role: str,
) -> Tuple[Optional[int], Optional[str]]:
    """Pure rewind slicing: how many leading transcript entries to KEEP.

    Returns ``(keep_count, error)``. ``keep_count`` is the count of entries to
    keep (i.e. ``entries[:keep_count]``); ``keep_count == len(entries)`` means
    the target is already the tail and nothing is removed. ``error`` is a
    non-None string only when ``target_uuid`` is absent from ``entries``.

    This is the single source of truth for *where* a rewind truncates so the
    on-disk JSONL file (:meth:`JSONLRewindService.truncate_to_checkpoint`) and
    the Postgres session store
    (:func:`app.core.pg_session_store.truncate_session_store_to_checkpoint`)
    cut at identical points. ``entries`` are raw transcript dicts (each with
    ``uuid``/``type``/``message``) — JSONL lines and store ``entry`` JSONB have
    the same shape.

    Semantics (matching the long-standing JSONL behavior):

    - ``target_role == 'assistant'``: keep the assistant message and everything
      before it; drop from the next user prompt onward.
    - user target, ``include_response=True``: keep the user prompt and its
      assistant reply; drop from the *next* user prompt onward.
    - user target, ``include_response=False``: drop the target user prompt and
      everything after it.
    """
    target_index = None
    for i, entry in enumerate(entries):
        if entry.get('uuid') == target_uuid:
            target_index = i
            break

    if target_index is None:
        return None, f"Message with UUID {target_uuid} not found"

    if target_role == 'assistant' or include_response:
        truncate_before = _next_real_user_index(entries, target_index)
        if truncate_before is None:
            # Target is already the tail — nothing after it to remove.
            return len(entries), None
        return _trim_trailing_queue_ops(entries, truncate_before), None

    # User message, drop the message itself and everything after it.
    return _trim_trailing_queue_ops(entries, target_index), None


def _trim_trailing_queue_ops(
    entries: Sequence[Mapping[str, Any]],
    keep_count: int,
) -> int:
    """Shrink *keep_count* past any queue-operation entries stranded at the cut.

    A queued prompt logs its queue-operation add/remove entries immediately
    before the user entry itself, so cutting AT the user entry leaves them as
    trailing no-ops in both the JSONL and the session store (the e2e-observed
    rewind leftovers). They describe only the rewound-away message — drop them
    with it. Only applies when a real truncation happens
    (``keep_count < len(entries)``) so a no-op rewind never silently deletes
    live queue state.
    """
    if keep_count >= len(entries):
        return keep_count
    while keep_count > 0 and entries[keep_count - 1].get("type") == "queue-operation":
        keep_count -= 1
    return keep_count


def extract_entry_text(entry: Mapping[str, Any]) -> str:
    """Extract the text content from a transcript entry.

    Source-agnostic: ``entry`` is any raw transcript dict (a JSONL line, a
    session-store ``entry`` JSONB, or a fluid node projected by
    ``app.core.provider.fluid_transcript``).
    """
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


def checkpoints_from_entries(
    entries: Sequence[Mapping[str, Any]]
) -> List[Checkpoint]:
    """Build the rewind checkpoint list from raw transcript entries.

    The pure, source-agnostic core of :meth:`JSONLRewindService.get_checkpoints`
    — extracted so the fluid node-tree transcript
    (:func:`app.core.provider.fluid_transcript.get_fluid_checkpoints`) produces a
    checkpoint list with IDENTICAL population and ordering rules. That identity
    is load-bearing: the rewind content guard compares a client-side occurrence
    ordinal against this list, so any divergence in which entries qualify would
    make the guard refuse (or, worse, mis-target) a valid rewind.

    Both user and assistant messages are checkpoints:
    - rewinding to a user message removes it and everything after,
    - rewinding to an assistant message keeps that response and removes after.
    """
    checkpoints: List[Checkpoint] = []
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

        message = entry.get('message', {})
        role = message.get('role')

        # Handle user messages
        if entry_type == 'user' and role == 'user':
            # Extract message text
            text = extract_entry_text(entry)

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

            checkpoints.append(Checkpoint(
                uuid=entry.get('uuid', ''),
                index=checkpoint_index,
                message_preview=text[:100] + ('...' if len(text) > 100 else ''),
                full_message=text,
                role='user',
                timestamp=entry.get('timestamp')
            ))
            checkpoint_index += 1

        # Handle assistant messages
        elif entry_type == 'assistant' and role == 'assistant':
            # Extract message text
            text = extract_entry_text(entry)

            # Skip empty messages
            if not text:
                continue

            # Skip tool use messages (we want the final text response)
            content = message.get('content', '')
            if isinstance(content, list):
                # Check if this is purely tool_use blocks (no text)
                has_text = any(
                    isinstance(block, dict) and block.get('type') == 'text'
                    for block in content
                )
                if not has_text:
                    continue

            checkpoints.append(Checkpoint(
                uuid=entry.get('uuid', ''),
                index=checkpoint_index,
                message_preview=text[:100] + ('...' if len(text) > 100 else ''),
                full_message=text,
                role='assistant',
                timestamp=entry.get('timestamp')
            ))
            checkpoint_index += 1

    return checkpoints


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
        """Extract the text content from a message entry.

        Thin wrapper over the module-level :func:`extract_entry_text` (kept as a
        method because ``rewind_db_sync`` and the tests call it through the
        service singleton).
        """
        return extract_entry_text(entry)

    def get_checkpoints(
        self,
        sdk_session_id: str,
        working_dir: str = "/workspace"
    ) -> List[Checkpoint]:
        """
        Get all available checkpoints (user and assistant messages) for a session.

        Both user and assistant messages represent potential rewind points.
        - Rewinding to a user message removes that message and everything after
        - Rewinding to an assistant message keeps that response and removes everything after

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
        checkpoints = checkpoints_from_entries(entries)
        logger.debug(f"Found {len(checkpoints)} checkpoints for session {sdk_session_id}")
        return checkpoints

    def truncate_to_checkpoint(
        self,
        sdk_session_id: str,
        target_uuid: str,
        working_dir: str = "/workspace",
        include_response: bool = True,
        target_role: str = "user"
    ) -> RewindResult:
        """
        Truncate the JSONL file to a specific checkpoint (user or assistant message).

        This is the core rewind operation. After truncation, the next SDK resume
        will only see messages up to the target checkpoint.

        For user messages:
        - If include_response=True: keeps the assistant's response, removes everything after
        - If include_response=False: removes the user message and everything after

        For assistant messages:
        - Always keeps the assistant message and removes everything after

        Args:
            sdk_session_id: The Claude SDK session ID
            target_uuid: The UUID of the message to rewind to
            working_dir: Working directory for the project
            include_response: If True, keep the assistant's response to the target message
                            (only applies to user messages)
            target_role: The role of the target message ("user" or "assistant")

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

        # Determine where to truncate (shared with the Postgres session store so
        # JSONL and store cut at the same point — see compute_rewind_keep_count).
        keep_count, slice_error = compute_rewind_keep_count(
            entries, target_uuid, include_response, target_role
        )
        if slice_error is not None:
            return RewindResult(
                success=False,
                message="Target checkpoint not found",
                error=f"{slice_error} in JSONL"
            )

        keep_entries = entries[:keep_count]
        messages_removed = len(entries) - len(keep_entries)

        # Count remaining user turns (for updating turn_count in DB)
        remaining_user_turns = sum(
            1 for e in keep_entries
            if e.get('type') == 'user'
            and e.get('message', {}).get('role') == 'user'
            and not e.get('isMeta')
            and not isinstance(e.get('message', {}).get('content'), list)  # Skip tool_result
        )

        if messages_removed == 0:
            return RewindResult(
                success=True,
                message="Already at the target checkpoint",
                checkpoint_uuid=target_uuid,
                messages_removed=0,
                remaining_user_turns=remaining_user_turns
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

                logger.debug(f"Rewound session {sdk_session_id} to checkpoint {target_uuid}, removed {messages_removed} entries")

                return RewindResult(
                    success=True,
                    message=f"Successfully rewound to checkpoint, removed {messages_removed} messages",
                    checkpoint_uuid=target_uuid,
                    messages_removed=messages_removed,
                    remaining_user_turns=remaining_user_turns
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
            logger.debug(f"Created JSONL backup at {backup_path}")
            return backup_path
        except Exception as e:
            logger.error(f"Failed to create JSONL backup: {e}")
            return None


# Global service instance
jsonl_rewind_service = JSONLRewindService()
