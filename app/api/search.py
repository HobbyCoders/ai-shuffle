"""
Advanced search API routes with filters
"""

import re
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Query, status, Request
from pydantic import BaseModel

from app.db import database
from app.api.auth import require_auth, get_api_user_from_request
from app.core.config import settings
from app.core.jsonl_parser import (
    get_session_jsonl_path,
    parse_jsonl_file,
    extract_text_from_content,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/search", tags=["Search"])


class SearchMatch(BaseModel):
    """A single match within a session"""
    message_index: int
    snippet: str
    role: str
    timestamp: Optional[str] = None


class AdvancedSearchResult(BaseModel):
    """Search result with session info and matches"""
    session_id: str
    session_title: Optional[str] = None
    created_at: str
    updated_at: str
    profile_id: Optional[str] = None
    profile_name: Optional[str] = None
    project_id: Optional[str] = None
    matches: List[SearchMatch] = []
    match_count: int = 0


def _extract_code_blocks(content: str) -> List[str]:
    """
    Extract code blocks from markdown content.
    Returns list of code block contents (without the ``` markers).
    """
    # Match code blocks with optional language identifier
    pattern = r'```(?:\w+)?\n?([\s\S]*?)```'
    matches = re.findall(pattern, content)
    return matches


def _highlight_snippet(text: str, query: str, is_regex: bool = False, context_chars: int = 50) -> str:
    """
    Create a snippet around the match with context.
    Returns the snippet with the match highlighted using **bold**.
    """
    if not text or not query:
        return text[:200] if text else ""

    try:
        if is_regex:
            # Find the first match position
            match = re.search(query, text, re.IGNORECASE)
            if match:
                start = match.start()
                end = match.end()
                # matched_text available for future highlighting features
                _matched_text = match.group()
            else:
                return text[:200] if len(text) > 200 else text
        else:
            # Case-insensitive search
            lower_text = text.lower()
            lower_query = query.lower()
            start = lower_text.find(lower_query)
            if start == -1:
                return text[:200] if len(text) > 200 else text
            end = start + len(query)
            # matched_text available for future use (e.g., highlighting)
            _matched_text = text[start:end]

        # Extract snippet with context
        snippet_start = max(0, start - context_chars)
        snippet_end = min(len(text), end + context_chars)

        # Adjust to word boundaries
        if snippet_start > 0:
            # Find the next space after snippet_start
            space_pos = text.find(' ', snippet_start)
            if space_pos != -1 and space_pos < start:
                snippet_start = space_pos + 1

        if snippet_end < len(text):
            # Find the last space before snippet_end
            space_pos = text.rfind(' ', end, snippet_end)
            if space_pos != -1:
                snippet_end = space_pos

        snippet = text[snippet_start:snippet_end]

        # Add ellipsis if truncated
        prefix = "..." if snippet_start > 0 else ""
        suffix = "..." if snippet_end < len(text) else ""

        # Highlight the match in the snippet
        # Recalculate position in snippet
        match_start_in_snippet = start - snippet_start
        match_end_in_snippet = end - snippet_start

        if match_start_in_snippet >= 0 and match_end_in_snippet <= len(snippet):
            highlighted = (
                snippet[:match_start_in_snippet] +
                "**" + snippet[match_start_in_snippet:match_end_in_snippet] + "**" +
                snippet[match_end_in_snippet:]
            )
            return f"{prefix}{highlighted}{suffix}"

        return f"{prefix}{snippet}{suffix}"

    except Exception as e:
        logger.warning(f"Error highlighting snippet: {e}")
        return text[:200] if len(text) > 200 else text


def _search_in_content(
    content: str,
    query: str,
    is_regex: bool = False,
    in_code_only: bool = False
) -> bool:
    """
    Check if query matches in content.
    If in_code_only is True, only search within code blocks.
    """
    if not content or not query:
        return False

    search_text = content
    if in_code_only:
        # Only search in code blocks
        code_blocks = _extract_code_blocks(content)
        if not code_blocks:
            return False
        search_text = "\n".join(code_blocks)

    try:
        if is_regex:
            return bool(re.search(query, search_text, re.IGNORECASE))
        else:
            return query.lower() in search_text.lower()
    except re.error:
        # Invalid regex, treat as literal
        return query.lower() in search_text.lower()


def _search_session_jsonl(
    session: Dict[str, Any],
    query: str,
    is_regex: bool = False,
    in_code_only: bool = False
) -> List[SearchMatch]:
    """
    Search through a session's JSONL file for matches.
    Returns list of matches with snippets.
    """
    matches: List[SearchMatch] = []

    sdk_session_id = session.get("sdk_session_id")
    if not sdk_session_id:
        return matches

    # Get working dir from project
    working_dir = "/workspace"
    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            working_dir = str(settings.workspace_dir / project["path"])

    # Get JSONL path
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        return matches

    try:
        msg_index = 0
        for entry in parse_jsonl_file(jsonl_path):
            entry_type = entry.get("type")

            # Skip non-message entries
            if entry_type in ("queue-operation", "file-history-snapshot"):
                continue

            # Skip meta messages
            if entry.get("isMeta") or entry.get("isSidechain"):
                continue

            message_data = entry.get("message", {})
            role = message_data.get("role", "")
            content = message_data.get("content")
            timestamp = entry.get("timestamp")

            # Extract text content
            if isinstance(content, str):
                text_content = content
            elif isinstance(content, list):
                text_content = extract_text_from_content(content)
            else:
                text_content = ""

            # Skip empty content
            if not text_content:
                msg_index += 1
                continue

            # Check for match
            if _search_in_content(text_content, query, is_regex, in_code_only):
                # Determine what text to use for snippet
                if in_code_only:
                    code_blocks = _extract_code_blocks(text_content)
                    for block in code_blocks:
                        if _search_in_content(block, query, is_regex, False):
                            snippet = _highlight_snippet(block, query, is_regex)
                            matches.append(SearchMatch(
                                message_index=msg_index,
                                snippet=snippet,
                                role=role,
                                timestamp=timestamp
                            ))
                            break
                else:
                    snippet = _highlight_snippet(text_content, query, is_regex)
                    matches.append(SearchMatch(
                        message_index=msg_index,
                        snippet=snippet,
                        role=role,
                        timestamp=timestamp
                    ))

            msg_index += 1

            # Limit matches per session for performance
            if len(matches) >= 10:
                break

    except Exception as e:
        logger.error(f"Error searching session {session.get('id')}: {e}")

    return matches


@router.get("", response_model=List[AdvancedSearchResult])
async def advanced_search(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    start_date: Optional[str] = Query(None, description="Filter by start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="Filter by end date (YYYY-MM-DD)"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    in_code_only: bool = Query(False, description="Search only in code blocks"),
    regex: bool = Query(False, description="Treat query as regex"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    token: str = Depends(require_auth)
):
    """
    Advanced search across all sessions with filters.

    Searches through JSONL files for message content.
    Supports:
    - Date range filtering
    - Profile filtering
    - Project filtering
    - Code-only search (only in code blocks)
    - Regex search

    Returns sessions with matching snippets.
    """
    api_user = get_api_user_from_request(request)

    # Validate regex if provided
    if regex:
        try:
            re.compile(q)
        except re.error as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex pattern: {str(e)}"
            )

    # Parse dates if provided
    date_filter_start = None
    date_filter_end = None
    if start_date:
        try:
            date_filter_start = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid start_date format. Use YYYY-MM-DD"
            )
    if end_date:
        try:
            date_filter_end = datetime.strptime(end_date, "%Y-%m-%d")
            # Include the full end date
            date_filter_end = date_filter_end.replace(hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid end_date format. Use YYYY-MM-DD"
            )

    # Force API user restrictions
    filter_api_user_id = None
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        if api_user.get("profile_id"):
            profile_id = api_user["profile_id"]
        filter_api_user_id = api_user["id"]

    # Get all sessions (with basic filters)
    # We need to search through all sessions, so get a larger batch
    sessions = database.get_sessions(
        project_id=project_id,
        profile_id=profile_id,
        api_user_id=filter_api_user_id,
        limit=500,  # Get more sessions for thorough search
        offset=0
    )

    # Get profile names for results
    profiles = {p["id"]: p["name"] for p in database.get_all_profiles()}

    results: List[AdvancedSearchResult] = []
    sessions_searched = 0

    for session in sessions:
        # Apply date filters
        if date_filter_start or date_filter_end:
            session_created = datetime.fromisoformat(session["created_at"].replace("Z", "+00:00"))
            # Make timezone-naive for comparison
            if session_created.tzinfo:
                session_created = session_created.replace(tzinfo=None)

            if date_filter_start and session_created < date_filter_start:
                continue
            if date_filter_end and session_created > date_filter_end:
                continue

        # Search session title first
        title = session.get("title") or ""
        title_match = False
        if title and _search_in_content(title, q, regex, False):
            title_match = True

        # Search session JSONL content
        matches = _search_session_jsonl(session, q, regex, in_code_only)

        # Include session if title matched or content matched
        if title_match or matches:
            # If title matched but no content matches, add a title match indicator
            if title_match and not matches:
                matches.append(SearchMatch(
                    message_index=-1,
                    snippet=_highlight_snippet(title, q, regex),
                    role="title",
                    timestamp=session.get("created_at")
                ))

            results.append(AdvancedSearchResult(
                session_id=session["id"],
                session_title=session.get("title"),
                created_at=session["created_at"],
                updated_at=session["updated_at"],
                profile_id=session.get("profile_id"),
                profile_name=profiles.get(session.get("profile_id")),
                project_id=session.get("project_id"),
                matches=matches,
                match_count=len(matches)
            ))

        sessions_searched += 1

        # Apply offset and limit
        if len(results) >= offset + limit:
            break

    # Apply pagination
    paginated_results = results[offset:offset + limit]

    logger.info(f"Advanced search '{q}': {len(paginated_results)} results from {sessions_searched} sessions searched")

    return paginated_results


@router.get("/suggestions")
async def search_suggestions(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query prefix"),
    limit: int = Query(10, ge=1, le=20),
    token: str = Depends(require_auth)
):
    """
    Get search suggestions based on recent session titles and queries.
    """
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    filter_api_user_id = None
    if api_user:
        filter_api_user_id = api_user["id"]

    # Get recent sessions with matching titles
    sessions = database.get_sessions(
        api_user_id=filter_api_user_id,
        limit=100,
        offset=0
    )

    suggestions = []
    query_lower = q.lower()

    for session in sessions:
        title = session.get("title")
        if title and query_lower in title.lower():
            if title not in suggestions:
                suggestions.append(title)
                if len(suggestions) >= limit:
                    break

    return {"suggestions": suggestions}
