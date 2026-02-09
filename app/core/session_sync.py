"""
Session Sync Service

Discovers and imports Claude Code sessions from the filesystem into the AI Shuffle database.
This keeps the database synchronized with sessions created directly in Claude Code.

Key features:
- Discovers unimported sessions for a specific project
- Batch imports sessions efficiently
- Triggered on project access (session list viewing)
"""

import logging
import uuid
from pathlib import Path
from typing import Dict, List, Any, Optional

from app.core.config import settings
from app.core.project_utils import get_project_working_dir
from app.core.jsonl_parser import (
    get_project_dir_name,
    get_session_cost_from_jsonl,
    _get_session_title_and_preview,
)
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
    ) -> List[Dict[str, Any]]:
        """
        Discover Claude Code sessions that exist on disk but are not in the database.

        Args:
            project_id: The AI Shuffle project ID
            working_dir: The working directory path for the project

        Returns:
            List of session info dicts with: sdk_session_id, jsonl_path, modified_at, size_bytes
        """
        claude_projects_dir = settings.get_claude_projects_dir
        project_dir_name = get_project_dir_name(working_dir)
        project_dir = claude_projects_dir / project_dir_name

        logger.debug(f"Discovering sessions for project {project_id} in {project_dir}")

        unimported = []

        if not project_dir.exists():
            logger.debug(f"Project directory does not exist: {project_dir}")
            return unimported

        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                # Skip agent files (subagent logs)
                if jsonl_file.name.startswith("agent-"):
                    continue

                sdk_session_id = jsonl_file.stem

                # Check if already imported
                existing = database.get_session_by_sdk_id(sdk_session_id)
                if existing:
                    logger.debug(f"Session {sdk_session_id} already imported as {existing['id']}")
                    continue

                stat = jsonl_file.stat()
                unimported.append({
                    "sdk_session_id": sdk_session_id,
                    "jsonl_path": jsonl_file,
                    "modified_at": stat.st_mtime,
                    "size_bytes": stat.st_size
                })

            except Exception as e:
                logger.warning(f"Failed to check session file {jsonl_file}: {e}")

        # Sort by modified time, newest first
        unimported.sort(key=lambda x: x["modified_at"], reverse=True)

        logger.debug(f"Found {len(unimported)} unimported sessions for project {project_id}")
        return unimported

    def import_session(
        self,
        sdk_session_id: str,
        jsonl_path: Path,
        project_id: str,
        working_dir: str,
        profile_id: str = "default"
    ) -> Optional[Dict[str, Any]]:
        """
        Import a single Claude Code session into the database.

        Args:
            sdk_session_id: The Claude SDK session ID (UUID)
            jsonl_path: Path to the JSONL file
            project_id: The AI Shuffle project ID to associate
            working_dir: The working directory for token usage parsing
            profile_id: The profile to associate (defaults to 'default')

        Returns:
            The created session dict, or None if import failed
        """
        try:
            # Double-check not already imported
            existing = database.get_session_by_sdk_id(sdk_session_id)
            if existing:
                logger.debug(f"Session {sdk_session_id} already exists as {existing['id']}")
                return existing

            # Get title and message count from JSONL
            title, preview, message_count = _get_session_title_and_preview(jsonl_path)
            if not title:
                title = f"Session {sdk_session_id[:8]}..."

            # Parse JSONL for token usage
            try:
                usage_data = get_session_cost_from_jsonl(sdk_session_id, working_dir)
                total_tokens_in = usage_data.get("total_tokens_in", 0) if usage_data else 0
                total_tokens_out = usage_data.get("total_tokens_out", 0) if usage_data else 0
                total_cost = 0.0  # JSONL doesn't contain cost info
            except Exception as e:
                logger.warning(f"Could not get token usage from JSONL {sdk_session_id}: {e}")
                total_tokens_in = 0
                total_tokens_out = 0
                total_cost = 0.0

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
                turn_count=message_count
            )

            logger.debug(
                f"Imported session {new_session_id} (SDK: {sdk_session_id}) "
                f"with {message_count} messages for project {project_id}"
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

        # Discover unimported sessions
        unimported = self.discover_unimported_sessions(project_id, working_dir)

        imported_sessions = []
        errors = []

        for session_info in unimported:
            try:
                session = self.import_session(
                    sdk_session_id=session_info["sdk_session_id"],
                    jsonl_path=session_info["jsonl_path"],
                    project_id=project_id,
                    working_dir=working_dir,
                    profile_id=profile_id
                )
                if session:
                    imported_sessions.append(session)
            except Exception as e:
                error_msg = f"Failed to import {session_info['sdk_session_id']}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        result = {
            "imported_count": len(imported_sessions),
            "skipped_count": len(unimported) - len(imported_sessions) - len(errors),
            "errors": errors,
            "sessions": imported_sessions
        }

        logger.debug(
            f"Session sync for project {project_id}: "
            f"imported={result['imported_count']}, errors={len(errors)}"
        )

        return result

    def sync_all_projects(
        self,
        profile_id: str = "default",
        limit_projects: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Sync Claude Code sessions across all known projects.

        Iterates through all projects in the database and imports any
        unimported Claude Code sessions for each. This is useful for
        populating sessions in views that don't filter by project.

        Args:
            profile_id: The profile to use for imported sessions
            limit_projects: Optional limit on number of projects to sync (for performance)

        Returns:
            Dict with: total_imported, total_skipped, projects_synced, errors
        """
        all_projects = database.get_all_projects()

        if limit_projects:
            all_projects = all_projects[:limit_projects]

        total_imported = 0
        total_skipped = 0
        projects_synced = 0
        all_errors = []

        for project in all_projects:
            try:
                result = self.sync_project_sessions(
                    project_id=project["id"],
                    profile_id=profile_id
                )
                total_imported += result["imported_count"]
                total_skipped += result["skipped_count"]
                projects_synced += 1

                if result["errors"]:
                    all_errors.extend(result["errors"])

            except Exception as e:
                error_msg = f"Failed to sync project {project['id']}: {e}"
                logger.error(error_msg)
                all_errors.append(error_msg)

        logger.debug(
            f"Global session sync: {total_imported} imported across "
            f"{projects_synced} projects, {len(all_errors)} errors"
        )

        return {
            "total_imported": total_imported,
            "total_skipped": total_skipped,
            "projects_synced": projects_synced,
            "errors": all_errors
        }


# Global service instance
session_sync_service = SessionSyncService()
