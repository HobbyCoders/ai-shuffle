"""
Checkpoint Manager

Coordinates checkpoint creation and rewind operations across:
1. Chat history (JSONL files)
2. Code state (Git snapshots) - optional

This provides a unified interface for the rewind feature, managing both
conversation context and file changes.
"""

import json
import logging
import subprocess
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field, asdict

from app.core.jsonl_rewind import jsonl_rewind_service, Checkpoint, RewindResult
from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)


@dataclass
class FullCheckpoint:
    """
    A full checkpoint containing both chat and code state.

    This extends the basic Checkpoint with git snapshot information.
    """
    id: str  # Unique checkpoint ID
    session_id: str  # Our internal session ID
    sdk_session_id: str  # Claude SDK session ID
    message_uuid: str  # JSONL message UUID
    message_preview: str  # Preview of user message
    message_index: int  # Position in conversation
    git_ref: Optional[str] = None  # Git commit/stash reference
    git_available: bool = False  # Whether git snapshot was created
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FullRewindResult:
    """Result of a full rewind operation (chat + code)"""
    success: bool
    message: str
    chat_rewound: bool = False
    code_rewound: bool = False
    messages_removed: int = 0
    files_restored: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GitSnapshotService:
    """
    Service for creating and restoring Git snapshots.

    Uses a hidden branch (.claude-checkpoints) to store snapshots without
    polluting the user's commit history.
    """

    CHECKPOINT_BRANCH = ".claude-checkpoints"

    def is_git_repo(self, working_dir: str) -> bool:
        """Check if the working directory is a git repository."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def create_snapshot(self, working_dir: str, message: str = "checkpoint") -> Optional[str]:
        """
        Create a git snapshot of the current working directory state.

        This creates a commit on a hidden branch without affecting the user's
        current branch or staging area.

        Returns:
            Git commit SHA if successful, None otherwise
        """
        if not self.is_git_repo(working_dir):
            logger.debug(f"Not a git repo: {working_dir}")
            return None

        try:
            # Get current branch
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )
            current_branch = result.stdout.strip()

            # Get current HEAD for reference
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                # No commits yet
                logger.debug("No commits in repo yet, skipping snapshot")
                return None

            current_head = result.stdout.strip()

            # Create a snapshot using git stash + apply approach
            # This doesn't require switching branches or modifying index
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stash_message = f"claude-checkpoint-{timestamp}: {message[:50]}"

            # Check if there are any changes to snapshot
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if not result.stdout.strip():
                # No changes, use current HEAD as checkpoint
                logger.debug("No uncommitted changes, using current HEAD as checkpoint")
                return current_head

            # Create stash with untracked files
            result = subprocess.run(
                ["git", "stash", "push", "-u", "-m", stash_message],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                logger.warning(f"Git stash failed: {result.stderr}")
                return None

            # Get the stash reference
            result = subprocess.run(
                ["git", "stash", "list", "-1"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )

            if "claude-checkpoint" not in result.stdout:
                logger.warning("Stash was not created (no changes?)")
                return current_head

            # Get the stash commit SHA
            result = subprocess.run(
                ["git", "rev-parse", "stash@{0}"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )
            stash_sha = result.stdout.strip() if result.returncode == 0 else None

            # Pop the stash to restore working directory
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if stash_sha:
                logger.info(f"Created git snapshot: {stash_sha[:8]} for {message[:50]}")
                return stash_sha

            return current_head

        except subprocess.TimeoutExpired:
            logger.warning("Git snapshot timed out")
            return None
        except Exception as e:
            logger.error(f"Failed to create git snapshot: {e}")
            return None

    def restore_snapshot(self, working_dir: str, git_ref: str) -> bool:
        """
        Restore the working directory to a previous git snapshot.

        This does a hard reset of the working directory to the snapshot state.
        WARNING: This will discard all uncommitted changes!

        Returns:
            True if successful, False otherwise
        """
        if not self.is_git_repo(working_dir):
            return False

        try:
            # First, check if the ref exists
            result = subprocess.run(
                ["git", "cat-file", "-t", git_ref],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                logger.error(f"Git ref {git_ref} does not exist")
                return False

            # Restore working directory from the snapshot
            # Use checkout to restore all files without changing HEAD
            result = subprocess.run(
                ["git", "checkout", git_ref, "--", "."],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.error(f"Git restore failed: {result.stderr}")
                return False

            # Clean untracked files that weren't in the snapshot
            # Note: This is aggressive - might want to make this optional
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            logger.info(f"Restored working directory to snapshot {git_ref[:8]}")
            return True

        except subprocess.TimeoutExpired:
            logger.warning("Git restore timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to restore git snapshot: {e}")
            return False

    def list_snapshots(self, working_dir: str, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent git snapshots (from stash or checkpoint branch)."""
        if not self.is_git_repo(working_dir):
            return []

        try:
            # List stashes that are our checkpoints
            result = subprocess.run(
                ["git", "stash", "list"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            snapshots = []
            for line in result.stdout.strip().split('\n'):
                if 'claude-checkpoint' in line:
                    # Parse stash entry: stash@{0}: On branch: message
                    parts = line.split(':', 2)
                    if len(parts) >= 3:
                        stash_ref = parts[0].strip()
                        message = parts[2].strip() if len(parts) > 2 else ''

                        # Get SHA
                        sha_result = subprocess.run(
                            ["git", "rev-parse", stash_ref],
                            cwd=working_dir,
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None

                        if sha:
                            snapshots.append({
                                'ref': sha,
                                'stash_ref': stash_ref,
                                'message': message
                            })

                        if len(snapshots) >= limit:
                            break

            return snapshots

        except Exception as e:
            logger.error(f"Failed to list git snapshots: {e}")
            return []


class CheckpointManager:
    """
    Manages checkpoints for rewind functionality.

    Coordinates between:
    - JSONLRewindService for chat history
    - GitSnapshotService for code state
    - Database for checkpoint metadata storage
    """

    def __init__(self):
        self.jsonl_service = jsonl_rewind_service
        self.git_service = GitSnapshotService()
        self._checkpoint_store: Dict[str, List[FullCheckpoint]] = {}

    def _get_working_dir(self, project_id: Optional[str]) -> str:
        """Get working directory for a project."""
        if project_id:
            project = database.get_project(project_id)
            if project:
                return str(settings.workspace_dir / project["path"])
        return str(settings.workspace_dir)

    def get_checkpoints(
        self,
        session_id: str,
        include_git: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get all available checkpoints for a session.

        Args:
            session_id: Our internal session ID
            include_git: Whether to check for git snapshot availability

        Returns:
            List of checkpoint dictionaries with chat and git info
        """
        # Get session to find SDK session ID
        session = database.get_session(session_id)
        if not session:
            logger.warning(f"Session not found: {session_id}")
            return []

        sdk_session_id = session.get('sdk_session_id')
        if not sdk_session_id:
            logger.warning(f"No SDK session ID for session: {session_id}")
            return []

        project_id = session.get('project_id')
        working_dir = self._get_working_dir(project_id)

        # Get chat checkpoints from JSONL
        chat_checkpoints = self.jsonl_service.get_checkpoints(sdk_session_id, working_dir)

        # Check git availability
        git_available = include_git and self.git_service.is_git_repo(working_dir)

        # Convert to full checkpoints
        checkpoints = []
        for cp in chat_checkpoints:
            full_cp = {
                'id': f"{session_id}:{cp.uuid}",
                'session_id': session_id,
                'sdk_session_id': sdk_session_id,
                'message_uuid': cp.uuid,
                'message_preview': cp.message_preview,
                'full_message': cp.full_message,
                'message_index': cp.index,
                'timestamp': cp.timestamp,
                'git_available': git_available,
                'git_ref': cp.git_ref  # May be populated from stored checkpoints
            }
            checkpoints.append(full_cp)

        return checkpoints

    def create_checkpoint(
        self,
        session_id: str,
        description: Optional[str] = None,
        create_git_snapshot: bool = True
    ) -> Optional[FullCheckpoint]:
        """
        Create a checkpoint at the current state.

        This should be called before each Claude turn to enable rewind.

        Args:
            session_id: Our internal session ID
            description: Optional description for the checkpoint
            create_git_snapshot: Whether to create a git snapshot

        Returns:
            FullCheckpoint if successful, None otherwise
        """
        session = database.get_session(session_id)
        if not session:
            logger.warning(f"Session not found: {session_id}")
            return None

        sdk_session_id = session.get('sdk_session_id')
        if not sdk_session_id:
            logger.debug(f"No SDK session ID yet for session: {session_id}")
            return None

        project_id = session.get('project_id')
        working_dir = self._get_working_dir(project_id)

        # Get the last message UUID from JSONL
        last_uuid = self.jsonl_service.get_last_message_uuid(sdk_session_id, working_dir)
        if not last_uuid:
            logger.debug(f"No messages in JSONL for session {session_id}")
            return None

        # Create git snapshot if requested
        git_ref = None
        git_available = False
        if create_git_snapshot and self.git_service.is_git_repo(working_dir):
            git_ref = self.git_service.create_snapshot(
                working_dir,
                description or f"Checkpoint for session {session_id}"
            )
            git_available = git_ref is not None

        # Get message preview
        checkpoints = self.jsonl_service.get_checkpoints(sdk_session_id, working_dir)
        message_preview = ""
        message_index = 0
        for cp in checkpoints:
            if cp.uuid == last_uuid:
                message_preview = cp.message_preview
                message_index = cp.index
                break

        checkpoint = FullCheckpoint(
            id=f"{session_id}:{last_uuid}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            message_uuid=last_uuid,
            message_preview=message_preview,
            message_index=message_index,
            git_ref=git_ref,
            git_available=git_available
        )

        # Store checkpoint
        if session_id not in self._checkpoint_store:
            self._checkpoint_store[session_id] = []
        self._checkpoint_store[session_id].append(checkpoint)

        logger.info(f"Created checkpoint {checkpoint.id} (git={git_available})")
        return checkpoint

    def rewind(
        self,
        session_id: str,
        target_uuid: str,
        restore_chat: bool = True,
        restore_code: bool = False,
        include_response: bool = True
    ) -> FullRewindResult:
        """
        Rewind to a specific checkpoint.

        Args:
            session_id: Our internal session ID
            target_uuid: The message UUID to rewind to
            restore_chat: Whether to truncate the JSONL (rewind conversation)
            restore_code: Whether to restore git snapshot (rewind code changes)
            include_response: Keep Claude's response to the target message

        Returns:
            FullRewindResult with details of the operation
        """
        session = database.get_session(session_id)
        if not session:
            return FullRewindResult(
                success=False,
                message="Session not found",
                error=f"Session {session_id} not found"
            )

        sdk_session_id = session.get('sdk_session_id')
        if not sdk_session_id:
            return FullRewindResult(
                success=False,
                message="No SDK session",
                error="Session has no SDK session ID"
            )

        project_id = session.get('project_id')
        working_dir = self._get_working_dir(project_id)

        chat_rewound = False
        code_rewound = False
        messages_removed = 0
        files_restored = 0
        errors = []

        # Backup JSONL before rewind
        if restore_chat:
            self.jsonl_service.backup_jsonl(sdk_session_id, working_dir)

        # Rewind chat (truncate JSONL)
        if restore_chat:
            result = self.jsonl_service.truncate_to_checkpoint(
                sdk_session_id=sdk_session_id,
                target_uuid=target_uuid,
                working_dir=working_dir,
                include_response=include_response
            )

            if result.success:
                chat_rewound = True
                messages_removed = result.messages_removed

                # Also sync our database
                self._sync_database_after_rewind(session_id, target_uuid, include_response)
            else:
                errors.append(f"Chat rewind failed: {result.error}")

        # Rewind code (restore git snapshot)
        if restore_code:
            # Find git_ref for this checkpoint
            git_ref = self._find_git_ref_for_checkpoint(session_id, target_uuid)

            if git_ref:
                if self.git_service.restore_snapshot(working_dir, git_ref):
                    code_rewound = True
                    # Count restored files (approximate)
                    files_restored = self._count_changed_files(working_dir, git_ref)
                else:
                    errors.append("Git restore failed")
            else:
                errors.append("No git snapshot found for this checkpoint")

        success = (not restore_chat or chat_rewound) and (not restore_code or code_rewound)

        return FullRewindResult(
            success=success,
            message=self._build_result_message(chat_rewound, code_rewound, messages_removed),
            chat_rewound=chat_rewound,
            code_rewound=code_rewound,
            messages_removed=messages_removed,
            files_restored=files_restored,
            error='; '.join(errors) if errors else None
        )

    def _sync_database_after_rewind(
        self,
        session_id: str,
        target_uuid: str,
        include_response: bool
    ):
        """
        Sync our local database after JSONL truncation.

        Deletes messages that were removed from the JSONL.
        This includes ALL message types: user, assistant, tool_use, tool_result.
        """
        try:
            # Get all messages from our database
            messages = database.get_session_messages(session_id)
            if not messages:
                return

            session = database.get_session(session_id)
            sdk_session_id = session.get('sdk_session_id') if session else None

            if sdk_session_id:
                project_id = session.get('project_id')
                working_dir = self._get_working_dir(project_id)

                # Get remaining checkpoints after truncation
                remaining_checkpoints = self.jsonl_service.get_checkpoints(sdk_session_id, working_dir)
                remaining_count = len(remaining_checkpoints)

                if remaining_count == 0:
                    # All checkpoints removed - delete all messages
                    for msg in messages:
                        database.delete_session_message(session_id, msg['id'])
                    logger.info(f"Deleted all {len(messages)} messages from database after rewind")
                    return

                # Count user messages in our DB
                user_messages = [m for m in messages if m.get('role') == 'user']

                # Delete messages beyond the remaining checkpoint count
                if len(user_messages) > remaining_count:
                    if include_response:
                        # Find the next user message after the ones we're keeping
                        # Everything before that next user message should be kept
                        # (this preserves the assistant response to the last kept user message)
                        next_user_idx = remaining_count
                        if next_user_idx < len(user_messages):
                            # Delete from the next user message onwards (and everything after)
                            next_user_id = user_messages[next_user_idx].get('id')
                            # Delete this message and all messages with id >= next_user_id
                            deleted = 0
                            for msg in messages:
                                if msg['id'] >= next_user_id:
                                    database.delete_session_message(session_id, msg['id'])
                                    deleted += 1
                            logger.info(f"Deleted {deleted} messages from database after rewind (kept response)")
                    else:
                        # Delete everything after the last kept user message (including its response)
                        last_kept_user_msg = user_messages[remaining_count - 1]
                        last_kept_user_id = last_kept_user_msg.get('id')
                        if last_kept_user_id:
                            deleted = database.delete_session_messages_after(session_id, last_kept_user_id)
                            logger.info(f"Deleted {deleted} messages from database after rewind")

        except Exception as e:
            logger.error(f"Failed to sync database after rewind: {e}")

    def _find_git_ref_for_checkpoint(self, session_id: str, target_uuid: str) -> Optional[str]:
        """Find the git ref associated with a checkpoint."""
        checkpoints = self._checkpoint_store.get(session_id, [])
        for cp in checkpoints:
            if cp.message_uuid == target_uuid and cp.git_ref:
                return cp.git_ref
        return None

    def _count_changed_files(self, working_dir: str, git_ref: str) -> int:
        """Count files that would be changed by restoring a git ref."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", git_ref],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return len([f for f in result.stdout.strip().split('\n') if f])
        except Exception:
            pass
        return 0

    def _build_result_message(
        self,
        chat_rewound: bool,
        code_rewound: bool,
        messages_removed: int
    ) -> str:
        """Build a human-readable result message."""
        parts = []
        if chat_rewound:
            parts.append(f"Conversation rewound ({messages_removed} messages removed)")
        if code_rewound:
            parts.append("Code restored to checkpoint")

        if parts:
            return ". ".join(parts)
        return "No changes made"


# Global instance
checkpoint_manager = CheckpointManager()
