"""
Session Sync Service

Discovers and imports Claude Code sessions from the filesystem into the AI Shuffle database.
This keeps the database synchronized with sessions created directly in Claude Code.

Uses the app-owned local transcript helpers for filesystem discovery and metadata
extraction (title, first prompt, git branch, cwd). JSONL records are streamed;
no CLI process or credentials are accessed.

Key features:
- Discovers unimported sessions for a specific project via SDK list_sessions()
- Batch imports sessions efficiently
- Triggered on project access (session list viewing)
"""

import logging
import uuid
from typing import Dict, List, Any, Optional

from app.core.cli_sessions import list_sessions, SessionInfo

from app.core.project_utils import get_project_working_dir
from app.core.jsonl_parser import get_session_cost_from_jsonl
from app.db import database

logger = logging.getLogger(__name__)


class SessionSyncService:
    """
    Service for synchronizing Claude Code sessions with the AI Shuffle database.

    Discovers sessions from ~/.claude/projects/ that are not yet tracked in the
    database and imports them, associating them with the appropriate project.
    """

    def discover_unimported_sessions(
        self,
        project_id: str,
        working_dir: str
    ) -> List[SessionInfo]:
        """
        Discover Claude Code sessions that exist on disk but are not in the database.

        Uses the SDK's list_sessions(directory=...) which efficiently extracts
        metadata (summary, first_prompt, git_branch, cwd) from stat + head/tail
        reads without full JSONL parsing.

        Args:
            project_id: The AI Shuffle project ID
            working_dir: The working directory path for the project

        Returns:
            List of SessionInfo objects for sessions not yet in the database,
            sorted by last_modified descending (newest first).
        """
        try:
            sdk_sessions = list_sessions(directory=working_dir)
        except Exception as e:
            logger.warning(f"SDK list_sessions failed for {working_dir}: {e}")
            return []

        logger.debug(
            f"SDK list_sessions returned {len(sdk_sessions)} sessions "
            f"for project {project_id} (dir: {working_dir})"
        )

        unimported = []
        for info in sdk_sessions:
            try:
                # Check if already imported
                existing = database.get_session_by_sdk_id(info.session_id)
                if existing:
                    continue

                unimported.append(info)

            except Exception as e:
                logger.warning(f"Failed to check session {info.session_id}: {e}")

        logger.debug(f"Found {len(unimported)} unimported sessions for project {project_id}")
        return unimported

    def import_session(
        self,
        session_info: SessionInfo,
        project_id: str,
        working_dir: str,
        profile_id: str = "default"
    ) -> Optional[Dict[str, Any]]:
        """
        Import a single Claude Code session into the database.

        Uses metadata from SessionInfo (populated by SDK's list_sessions())
        for title and summary, avoiding redundant JSONL parsing.

        Args:
            session_info: SessionInfo from list_sessions() with metadata
            project_id: The AI Shuffle project ID to associate
            working_dir: The working directory for token usage parsing
            profile_id: The profile to associate (defaults to 'default')

        Returns:
            The created session dict, or None if import failed
        """
        sdk_session_id = session_info.session_id

        try:
            # Double-check not already imported
            existing = database.get_session_by_sdk_id(sdk_session_id)
            if existing:
                logger.debug(f"Session {sdk_session_id} already exists as {existing['id']}")
                return existing

            # Use SDK-provided summary (custom_title > auto-summary > first_prompt)
            title = (
                session_info.custom_title
                or session_info.summary
                or f"Session {sdk_session_id[:8]}..."
            )

            # Parse JSONL for token usage (SDK doesn't extract this in list_sessions)
            total_tokens_in = 0
            total_tokens_out = 0
            total_cost = 0.0
            try:
                usage_data = get_session_cost_from_jsonl(sdk_session_id, working_dir)
                if usage_data:
                    total_tokens_in = usage_data.get("total_tokens_in", 0)
                    total_tokens_out = usage_data.get("total_tokens_out", 0)
            except Exception as e:
                logger.warning(f"Could not get token usage from JSONL {sdk_session_id}: {e}")

            # Verify profile exists
            profile = database.get_profile(profile_id)
            if not profile:
                profile_id = "default"
                profile = database.get_profile(profile_id)
                if not profile:
                    # Fall back to first available profile
                    profiles = database.get_all_profiles()
                    if profiles:
                        profile_id = profiles[0]["id"]
                    else:
                        logger.error("No profiles available for session import")
                        return None

            # Generate new AI Shuffle session ID
            new_session_id = f"ses-{uuid.uuid4().hex[:16]}"

            # Discovery follows the SDK's millisecond contract. Retain support
            # for older seconds-valued metadata, and fall back on invalid dates.
            created_at_iso = None
            if session_info.created_at:
                try:
                    from datetime import datetime, timezone
                    timestamp = session_info.created_at
                    if abs(timestamp) >= 100_000_000_000:
                        timestamp /= 1000
                    created_at_iso = datetime.fromtimestamp(
                        timestamp, tz=timezone.utc
                    ).isoformat()
                except (ValueError, OSError, OverflowError, TypeError):
                    pass

            # Create session with all stats in a single atomic transaction
            # This prevents orphaned sessions if any part of the insert fails
            database.create_session_with_stats(
                session_id=new_session_id,
                profile_id=profile_id,
                project_id=project_id,
                title=title,
                api_user_id=None,  # Imported sessions are local/admin sessions
                sdk_session_id=sdk_session_id,
                total_cost_usd=total_cost,
                total_tokens_in=total_tokens_in,
                total_tokens_out=total_tokens_out,
                turn_count=0,  # Accurate count requires full JSONL parse; updated on first load
                created_at=created_at_iso,
            )

            logger.debug(
                f"Imported session {new_session_id} (SDK: {sdk_session_id}) "
                f"title='{title[:50]}' for project {project_id}"
            )

            return database.get_session(new_session_id)

        except Exception as e:
            logger.error(f"Failed to import session {sdk_session_id}: {e}")
            return None

    def sync_project_sessions(
        self,
        project_id: str,
        profile_id: str = "default"
    ) -> Dict[str, Any]:
        """
        Sync all unimported Claude Code sessions for a project.

        Discovers sessions from the Claude projects directory that match the
        project's working directory and imports any that aren't already in the database.

        Args:
            project_id: The AI Shuffle project ID
            profile_id: The profile to use for imported sessions

        Returns:
            Dict with: imported_count, skipped_count, errors, sessions
        """
        # Get project to find working directory
        project = database.get_project(project_id)
        if not project:
            logger.warning(f"Project not found: {project_id}")
            return {
                "imported_count": 0,
                "skipped_count": 0,
                "errors": [f"Project not found: {project_id}"],
                "sessions": []
            }

        # Build working directory path
        working_dir = get_project_working_dir(project)

        # Discover unimported sessions via SDK
        unimported = self.discover_unimported_sessions(project_id, working_dir)

        imported_sessions = []
        errors = []

        for session_info in unimported:
            try:
                session = self.import_session(
                    session_info=session_info,
                    project_id=project_id,
                    working_dir=working_dir,
                    profile_id=profile_id
                )
                if session:
                    imported_sessions.append(session)
            except Exception as e:
                error_msg = f"Failed to import {session_info.session_id}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        result = {
            "imported_count": len(imported_sessions),
            "skipped_count": max(0, len(unimported) - len(imported_sessions) - len(errors)),
            "errors": errors,
            "sessions": imported_sessions
        }

        logger.debug(
            f"Session sync for project {project_id}: "
            f"imported={result['imported_count']}, errors={len(errors)}"
        )

        return result


# Global service instance
session_sync_service = SessionSyncService()
