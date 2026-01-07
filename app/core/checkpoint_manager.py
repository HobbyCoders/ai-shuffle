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

    Uses a hidden orphan branch (.claude-checkpoints) to store snapshots as
    actual commits. This is more robust than stashes because:
    - Commits persist even after stash clear/drop
    - Commits work across push/pull operations
    - Commits survive garbage collection
    - Commits can be restored even after user commits new changes

    The orphan branch is completely separate from the main history,
    so it doesn't pollute the user's commit graph.
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

    def _run_git(self, working_dir: str, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a git command and return the result."""
        return subprocess.run(
            ["git"] + args,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    def _ensure_checkpoint_branch(self, working_dir: str) -> bool:
        """Ensure the checkpoint orphan branch exists."""
        # Check if branch exists
        result = self._run_git(working_dir, ["branch", "--list", self.CHECKPOINT_BRANCH])
        if self.CHECKPOINT_BRANCH in result.stdout:
            return True

        # Create orphan branch with an initial empty commit
        # We do this by creating a tree object and committing to it
        try:
            # Create an empty tree
            result = self._run_git(working_dir, ["hash-object", "-t", "tree", "/dev/null"])
            if result.returncode != 0:
                # Fallback: write empty tree
                result = self._run_git(working_dir, ["mktree"], timeout=5)
                if result.returncode != 0:
                    # Another fallback - create tree from empty index
                    empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # Git's empty tree hash
                else:
                    empty_tree = result.stdout.strip()
            else:
                empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

            # Create initial commit on orphan branch
            result = self._run_git(
                working_dir,
                ["commit-tree", empty_tree, "-m", "Initialize Claude checkpoint branch"]
            )
            if result.returncode != 0:
                logger.warning(f"Failed to create initial commit: {result.stderr}")
                return False

            initial_commit = result.stdout.strip()

            # Create the branch pointing to this commit
            result = self._run_git(
                working_dir,
                ["branch", self.CHECKPOINT_BRANCH, initial_commit]
            )
            if result.returncode != 0:
                logger.warning(f"Failed to create checkpoint branch: {result.stderr}")
                return False

            logger.info(f"Created checkpoint branch {self.CHECKPOINT_BRANCH}")
            return True

        except Exception as e:
            logger.error(f"Failed to ensure checkpoint branch: {e}")
            return False

    def create_snapshot(self, working_dir: str, message: str = "checkpoint") -> Optional[str]:
        """
        Create a git snapshot of the current working directory state.

        This creates an actual commit on the hidden checkpoint branch,
        capturing ALL files (tracked, modified, and untracked).

        The snapshot persists even after:
        - User commits changes
        - User pushes to remote
        - User runs git stash clear
        - Garbage collection

        Returns:
            Git commit SHA if successful, None otherwise
        """
        if not self.is_git_repo(working_dir):
            logger.debug(f"Not a git repo: {working_dir}")
            return None

        try:
            # Get current HEAD for reference
            result = self._run_git(working_dir, ["rev-parse", "HEAD"])
            if result.returncode != 0:
                logger.debug("No commits in repo yet, skipping snapshot")
                return None

            current_head = result.stdout.strip()

            # Check if there are any changes (tracked or untracked)
            result = self._run_git(working_dir, ["status", "--porcelain"])
            has_changes = bool(result.stdout.strip())

            if not has_changes:
                # No changes - just return current HEAD as the checkpoint
                # This is valid because HEAD represents the exact state
                logger.debug("No uncommitted changes, using current HEAD as checkpoint")
                return current_head

            # Ensure checkpoint branch exists
            if not self._ensure_checkpoint_branch(working_dir):
                logger.warning("Could not ensure checkpoint branch, falling back to HEAD")
                return current_head

            # Create a snapshot commit on the checkpoint branch
            # We use git's low-level commands to avoid switching branches
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            commit_message = f"Claude checkpoint: {message[:100]}\n\nBase commit: {current_head[:8]}\nTimestamp: {timestamp}"

            # Step 1: Create a temporary index with ALL current files
            # Use GIT_INDEX_FILE to work with a separate index
            temp_index = os.path.join(working_dir, ".git", "claude-checkpoint-index")

            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = temp_index

            # Remove old temp index if exists
            if os.path.exists(temp_index):
                os.remove(temp_index)

            # Add all files to temp index (including untracked)
            result = subprocess.run(
                ["git", "add", "-A"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=60,
                env=env
            )
            if result.returncode != 0:
                logger.warning(f"Failed to add files to temp index: {result.stderr}")
                return current_head

            # Step 2: Write the tree from temp index
            result = subprocess.run(
                ["git", "write-tree"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=30,
                env=env
            )
            if result.returncode != 0:
                logger.warning(f"Failed to write tree: {result.stderr}")
                return current_head

            tree_sha = result.stdout.strip()

            # Step 3: Get the current tip of checkpoint branch
            result = self._run_git(working_dir, ["rev-parse", self.CHECKPOINT_BRANCH])
            parent_commit = result.stdout.strip() if result.returncode == 0 else None

            # Step 4: Create commit on checkpoint branch
            commit_args = ["commit-tree", tree_sha, "-m", commit_message]
            if parent_commit:
                commit_args.extend(["-p", parent_commit])

            result = self._run_git(working_dir, commit_args)
            if result.returncode != 0:
                logger.warning(f"Failed to create checkpoint commit: {result.stderr}")
                return current_head

            checkpoint_commit = result.stdout.strip()

            # Step 5: Update checkpoint branch to point to new commit
            result = self._run_git(
                working_dir,
                ["update-ref", f"refs/heads/{self.CHECKPOINT_BRANCH}", checkpoint_commit]
            )
            if result.returncode != 0:
                logger.warning(f"Failed to update checkpoint branch: {result.stderr}")
                return current_head

            # Clean up temp index
            if os.path.exists(temp_index):
                os.remove(temp_index)

            logger.info(f"Created git snapshot: {checkpoint_commit[:8]} for {message[:50]}")
            return checkpoint_commit

        except subprocess.TimeoutExpired:
            logger.warning("Git snapshot timed out")
            return None
        except Exception as e:
            logger.error(f"Failed to create git snapshot: {e}")
            return None

    def restore_snapshot(self, working_dir: str, git_ref: str) -> bool:
        """
        Restore the working directory to a previous git snapshot.

        This restores all files from the snapshot commit without changing
        the current branch's HEAD. It's like doing a selective checkout
        of all files from the snapshot.

        WARNING: This will discard all uncommitted changes!

        Returns:
            True if successful, False otherwise
        """
        if not self.is_git_repo(working_dir):
            return False

        try:
            # First, verify the ref exists and is a valid commit/tree
            result = self._run_git(working_dir, ["cat-file", "-t", git_ref])
            if result.returncode != 0:
                logger.error(f"Git ref {git_ref} does not exist")
                return False

            obj_type = result.stdout.strip()
            if obj_type not in ("commit", "tree"):
                logger.error(f"Git ref {git_ref} is not a commit or tree (is {obj_type})")
                return False

            # Get the tree SHA if this is a commit
            if obj_type == "commit":
                result = self._run_git(working_dir, ["rev-parse", f"{git_ref}^{{tree}}"])
                if result.returncode != 0:
                    logger.error(f"Could not get tree for commit {git_ref}")
                    return False
                tree_sha = result.stdout.strip()
            else:
                tree_sha = git_ref

            # Step 1: Clean working directory (remove untracked files)
            self._run_git(working_dir, ["clean", "-fd"])

            # Step 2: Reset index to match the snapshot tree
            result = self._run_git(working_dir, ["read-tree", "--reset", "-u", tree_sha])
            if result.returncode != 0:
                logger.error(f"Failed to read-tree: {result.stderr}")
                # Fallback: try checkout approach
                result = self._run_git(working_dir, ["checkout", git_ref, "--", "."], timeout=120)
                if result.returncode != 0:
                    logger.error(f"Fallback checkout also failed: {result.stderr}")
                    return False

            logger.info(f"Restored working directory to snapshot {git_ref[:8]}")
            return True

        except subprocess.TimeoutExpired:
            logger.warning("Git restore timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to restore git snapshot: {e}")
            return False

    def list_snapshots(self, working_dir: str, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent git snapshots from the checkpoint branch."""
        if not self.is_git_repo(working_dir):
            return []

        try:
            # Check if checkpoint branch exists
            result = self._run_git(working_dir, ["branch", "--list", self.CHECKPOINT_BRANCH])
            if self.CHECKPOINT_BRANCH not in result.stdout:
                return []

            # List commits on checkpoint branch
            result = self._run_git(
                working_dir,
                ["log", self.CHECKPOINT_BRANCH, f"--max-count={limit}",
                 "--format=%H|%s|%ai"]
            )
            if result.returncode != 0:
                return []

            snapshots = []
            for line in result.stdout.strip().split('\n'):
                if not line or '|' not in line:
                    continue
                parts = line.split('|', 2)
                if len(parts) >= 2:
                    snapshots.append({
                        'ref': parts[0],
                        'message': parts[1],
                        'timestamp': parts[2] if len(parts) > 2 else ''
                    })

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
    - Database for checkpoint metadata storage (persistent)
    """

    def __init__(self):
        self.jsonl_service = jsonl_rewind_service
        self.git_service = GitSnapshotService()

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

        # Check git availability for new snapshots
        git_repo_available = include_git and self.git_service.is_git_repo(working_dir)

        # Get stored checkpoints from database for git_ref lookup
        stored_checkpoints = database.get_session_checkpoints(session_id)
        stored_by_uuid = {cp['message_uuid']: cp for cp in stored_checkpoints}

        # Build index-to-git_ref mapping for efficient lookup of git_refs after each checkpoint
        # and find the git_ref to restore to for each checkpoint
        index_to_git_ref = {}
        for stored_cp in stored_checkpoints:
            if stored_cp.get('git_ref'):
                index_to_git_ref[stored_cp.get('message_index', 0)] = stored_cp.get('git_ref')

        # For each checkpoint index, find the git_ref to restore to (closest at or before)
        # and whether there are any changes after it
        def find_restore_git_ref(target_index: int) -> Optional[str]:
            """Find closest git_ref at or before target_index"""
            for idx in sorted(index_to_git_ref.keys(), reverse=True):
                if idx <= target_index:
                    return index_to_git_ref[idx]
            return None

        def has_changes_after(target_index: int) -> bool:
            """Check if any git_ref exists after target_index"""
            return any(idx > target_index for idx in index_to_git_ref.keys())

        # Convert to full checkpoints
        checkpoints = []
        for cp in chat_checkpoints:
            # Look up stored checkpoint for git_ref
            stored = stored_by_uuid.get(cp.uuid)
            git_ref = stored.get('git_ref') if stored else None
            cp_index = cp.index

            # Determine the git_ref to restore to for this checkpoint
            restore_git_ref = find_restore_git_ref(cp_index)

            # Determine if there are code changes after this checkpoint
            has_code_changes_after = has_changes_after(cp_index)

            # git_available is True if:
            # 1. There's a git_ref to restore to (at or before this checkpoint), OR
            # 2. Git repo is available for new snapshots (fallback)
            # AND there are changes after this checkpoint to revert
            can_restore_code = (restore_git_ref is not None or git_repo_available) and has_code_changes_after

            full_cp = {
                'id': f"{session_id}:{cp.uuid}",
                'session_id': session_id,
                'sdk_session_id': sdk_session_id,
                'message_uuid': cp.uuid,
                'message_preview': cp.message_preview,
                'full_message': cp.full_message,
                'message_index': cp_index,
                'timestamp': cp.timestamp,
                'git_available': can_restore_code,  # True if code restore is meaningful
                'git_ref': restore_git_ref,  # The git_ref we would restore TO (at or before this checkpoint)
                'has_changes_after': has_code_changes_after  # Whether there are git changes after this checkpoint
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

        # Check if we already have a checkpoint for this message UUID
        existing = database.get_checkpoint_by_message_uuid(session_id, last_uuid)
        if existing:
            logger.debug(f"Checkpoint already exists for message {last_uuid}")
            return FullCheckpoint(
                id=existing['id'],
                session_id=existing['session_id'],
                sdk_session_id=existing['sdk_session_id'],
                message_uuid=existing['message_uuid'],
                message_preview=existing.get('message_preview', ''),
                message_index=existing.get('message_index', 0),
                git_ref=existing.get('git_ref'),
                git_available=existing.get('git_available', False),
                timestamp=existing.get('created_at', datetime.now().isoformat())
            )

        # Create git snapshot if requested
        git_ref = None
        git_available = False
        if create_git_snapshot and self.git_service.is_git_repo(working_dir):
            git_ref = self.git_service.create_snapshot(
                working_dir,
                description or f"Checkpoint for session {session_id}"
            )
            git_available = git_ref is not None

        # Get message preview and index
        checkpoints = self.jsonl_service.get_checkpoints(sdk_session_id, working_dir)
        message_preview = ""
        message_index = 0
        for cp in checkpoints:
            if cp.uuid == last_uuid:
                message_preview = cp.message_preview
                message_index = cp.index
                break

        checkpoint_id = f"{session_id}:{last_uuid}:{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # Persist checkpoint to database
        database.create_checkpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            message_uuid=last_uuid,
            message_preview=message_preview,
            message_index=message_index,
            git_ref=git_ref,
            git_available=git_available
        )

        checkpoint = FullCheckpoint(
            id=checkpoint_id,
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            message_uuid=last_uuid,
            message_preview=message_preview,
            message_index=message_index,
            git_ref=git_ref,
            git_available=git_available
        )

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

                # Clean up checkpoints after the rewind point
                self._cleanup_checkpoints_after_rewind(session_id, target_uuid)
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

    def _cleanup_checkpoints_after_rewind(self, session_id: str, target_uuid: str):
        """Remove checkpoint records that are beyond the rewind point."""
        try:
            # Get the target checkpoint to find its index
            target_checkpoint = database.get_checkpoint_by_message_uuid(session_id, target_uuid)
            if target_checkpoint:
                message_index = target_checkpoint.get('message_index', 0)
                deleted = database.delete_session_checkpoints_after(session_id, message_index)
                if deleted > 0:
                    logger.info(f"Deleted {deleted} checkpoints after rewind for session {session_id}")
        except Exception as e:
            logger.error(f"Failed to cleanup checkpoints after rewind: {e}")

    def _find_git_ref_for_checkpoint(self, session_id: str, target_uuid: str) -> Optional[str]:
        """
        Find the git ref to restore to for a checkpoint.

        This looks for the most recent git_ref AT OR BEFORE the target checkpoint.
        If the target checkpoint has no git_ref, we find the closest earlier one,
        which represents the state the code was in at that point in time.

        Args:
            session_id: Our internal session ID
            target_uuid: The UUID of the target message

        Returns:
            Git ref to restore to, or None if no snapshots exist
        """
        # Get the target checkpoint to find its index
        target_checkpoint = database.get_checkpoint_by_message_uuid(session_id, target_uuid)
        if not target_checkpoint:
            return None

        # If target has a git_ref, use it directly
        if target_checkpoint.get('git_ref'):
            return target_checkpoint['git_ref']

        # Otherwise, find the most recent checkpoint with a git_ref at or before target
        target_index = target_checkpoint.get('message_index', 0)
        all_checkpoints = database.get_session_checkpoints(session_id)

        # Sort by message_index descending and find first one at or before target with git_ref
        for cp in sorted(all_checkpoints, key=lambda x: x.get('message_index', 0), reverse=True):
            cp_index = cp.get('message_index', 0)
            if cp_index <= target_index and cp.get('git_ref'):
                return cp['git_ref']

        return None

    def _find_latest_git_ref_after_checkpoint(self, session_id: str, target_uuid: str) -> Optional[str]:
        """
        Find the most recent git_ref that exists AFTER a checkpoint.

        This is used to determine if there are any code changes to revert.
        If there's a git_ref after the target checkpoint, there are changes
        that would need to be reverted.

        Args:
            session_id: Our internal session ID
            target_uuid: The UUID of the target message

        Returns:
            Latest git_ref after the target, or None if no changes after target
        """
        # Get the target checkpoint to find its index
        target_checkpoint = database.get_checkpoint_by_message_uuid(session_id, target_uuid)
        if not target_checkpoint:
            return None

        target_index = target_checkpoint.get('message_index', 0)
        all_checkpoints = database.get_session_checkpoints(session_id)

        # Find any checkpoint after target with a git_ref
        for cp in sorted(all_checkpoints, key=lambda x: x.get('message_index', 0), reverse=True):
            cp_index = cp.get('message_index', 0)
            if cp_index > target_index and cp.get('git_ref'):
                return cp['git_ref']

        return None

    def has_code_changes_after_checkpoint(self, session_id: str, target_uuid: str) -> bool:
        """
        Check if there are any code changes (git snapshots) after a checkpoint.

        This is used to determine if the "Restore code" option should be available.

        Args:
            session_id: Our internal session ID
            target_uuid: The UUID of the target message

        Returns:
            True if there are git snapshots after the target checkpoint
        """
        return self._find_latest_git_ref_after_checkpoint(session_id, target_uuid) is not None

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
