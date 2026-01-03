"""
Unit tests for search API endpoints.

Tests cover:
- Advanced search functionality
- Search suggestions
- Query validation (regex, date formats)
- Code block searching
- Snippet highlighting
- Authentication requirements
- API user filtering
- Pagination
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


class TestSearchModuleImports:
    """Verify search module can be imported correctly."""

    def test_search_module_imports(self):
        """Search module should import without errors."""
        from app.api import search
        assert search is not None

    def test_search_router_exists(self):
        """Search router should exist."""
        from app.api.search import router
        assert router is not None

    def test_search_models_exist(self):
        """Search models should be defined."""
        from app.api.search import SearchMatch, AdvancedSearchResult
        assert SearchMatch is not None
        assert AdvancedSearchResult is not None


class TestExtractCodeBlocks:
    """Test code block extraction from markdown."""

    def test_extract_single_code_block(self):
        """Should extract a single code block."""
        from app.api.search import _extract_code_blocks
        content = "Some text\n```python\nprint('hello')\n```\nMore text"
        result = _extract_code_blocks(content)
        assert len(result) == 1
        assert "print('hello')" in result[0]

    def test_extract_multiple_code_blocks(self):
        """Should extract multiple code blocks."""
        from app.api.search import _extract_code_blocks
        content = """
```python
def foo():
    pass
```

Some text

```javascript
function bar() {}
```
"""
        result = _extract_code_blocks(content)
        assert len(result) == 2
        assert "def foo()" in result[0]
        assert "function bar()" in result[1]

    def test_extract_code_block_without_language(self):
        """Should extract code blocks without language specifier."""
        from app.api.search import _extract_code_blocks
        content = "Text\n```\nsome code\n```\nMore text"
        result = _extract_code_blocks(content)
        assert len(result) == 1
        assert "some code" in result[0]

    def test_no_code_blocks(self):
        """Should return empty list when no code blocks."""
        from app.api.search import _extract_code_blocks
        content = "Just plain text with no code blocks"
        result = _extract_code_blocks(content)
        assert result == []

    def test_empty_content(self):
        """Should handle empty content."""
        from app.api.search import _extract_code_blocks
        result = _extract_code_blocks("")
        assert result == []


class TestHighlightSnippet:
    """Test snippet highlighting functionality."""

    def test_highlight_simple_match(self):
        """Should highlight a simple match."""
        from app.api.search import _highlight_snippet
        text = "This is a test string with the word hello in it"
        result = _highlight_snippet(text, "hello")
        assert "**hello**" in result

    def test_highlight_case_insensitive(self):
        """Should match case-insensitively."""
        from app.api.search import _highlight_snippet
        text = "This contains HELLO in uppercase"
        result = _highlight_snippet(text, "hello")
        assert "**HELLO**" in result

    def test_highlight_with_context(self):
        """Should include context around match."""
        from app.api.search import _highlight_snippet
        # Long text to test context limiting
        text = "A" * 100 + " target " + "B" * 100
        result = _highlight_snippet(text, "target", context_chars=20)
        # Should have ellipsis on both sides due to truncation
        assert "..." in result
        assert "**target**" in result

    def test_highlight_no_match(self):
        """Should return truncated text when no match."""
        from app.api.search import _highlight_snippet
        text = "This text does not contain the search term"
        result = _highlight_snippet(text, "xyz123")
        # Should return original (or truncated) without highlight
        assert "**" not in result

    def test_highlight_regex_match(self):
        """Should handle regex patterns."""
        from app.api.search import _highlight_snippet
        text = "Error code: E123 in the log"
        result = _highlight_snippet(text, r"E\d+", is_regex=True)
        assert "**E123**" in result

    def test_highlight_empty_query(self):
        """Should handle empty query."""
        from app.api.search import _highlight_snippet
        text = "Some text here"
        result = _highlight_snippet(text, "")
        assert result == text[:200] if len(text) > 200 else text

    def test_highlight_empty_text(self):
        """Should handle empty text."""
        from app.api.search import _highlight_snippet
        result = _highlight_snippet("", "query")
        assert result == ""

    def test_highlight_invalid_regex(self):
        """Should handle invalid regex gracefully."""
        from app.api.search import _highlight_snippet
        text = "Some text with content"
        # Invalid regex pattern
        result = _highlight_snippet(text, "[invalid", is_regex=True)
        # Should not crash, returns something
        assert result is not None

    def test_highlight_long_text_truncation(self):
        """Should truncate very long text."""
        from app.api.search import _highlight_snippet
        text = "x" * 1000
        result = _highlight_snippet(text, "nomatch")
        assert len(result) <= 200


class TestSearchInContent:
    """Test content search functionality."""

    def test_search_simple_text(self):
        """Should find simple text match."""
        from app.api.search import _search_in_content
        assert _search_in_content("Hello world", "hello") is True
        assert _search_in_content("Hello world", "goodbye") is False

    def test_search_case_insensitive(self):
        """Should search case-insensitively."""
        from app.api.search import _search_in_content
        assert _search_in_content("HELLO WORLD", "hello") is True
        assert _search_in_content("hello world", "HELLO") is True

    def test_search_regex(self):
        """Should support regex search."""
        from app.api.search import _search_in_content
        assert _search_in_content("Error: E404 not found", r"E\d+", is_regex=True) is True
        assert _search_in_content("Error: XYZ not found", r"E\d+", is_regex=True) is False

    def test_search_invalid_regex(self):
        """Should handle invalid regex by treating as literal."""
        from app.api.search import _search_in_content
        # Invalid regex should be treated as literal search
        content = "Text with [bracket"
        result = _search_in_content(content, "[bracket", is_regex=True)
        assert result is True

    def test_search_in_code_only(self):
        """Should search only in code blocks when in_code_only=True."""
        from app.api.search import _search_in_content
        content = """
Some text with foo

```python
bar = "value"
```
"""
        # foo is in regular text, not code
        assert _search_in_content(content, "foo", in_code_only=True) is False
        # bar is in code block
        assert _search_in_content(content, "bar", in_code_only=True) is True

    def test_search_in_code_only_no_blocks(self):
        """Should return False when in_code_only but no code blocks."""
        from app.api.search import _search_in_content
        content = "Plain text without any code blocks"
        assert _search_in_content(content, "text", in_code_only=True) is False

    def test_search_empty_content(self):
        """Should return False for empty content."""
        from app.api.search import _search_in_content
        assert _search_in_content("", "query") is False
        assert _search_in_content(None, "query") is False

    def test_search_empty_query(self):
        """Should return False for empty query."""
        from app.api.search import _search_in_content
        assert _search_in_content("content", "") is False
        assert _search_in_content("content", None) is False


class TestSearchSessionJsonl:
    """Test JSONL session search functionality."""

    def test_search_session_no_sdk_id(self):
        """Should return empty list when session has no sdk_session_id."""
        from app.api.search import _search_session_jsonl
        session = {"id": "test", "title": "Test"}
        result = _search_session_jsonl(session, "query")
        assert result == []

    @patch("app.api.search.get_session_jsonl_path")
    def test_search_session_no_jsonl_path(self, mock_get_path):
        """Should return empty list when JSONL path not found."""
        from app.api.search import _search_session_jsonl
        mock_get_path.return_value = None
        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "query")
        assert result == []

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_with_matches(self, mock_parse, mock_get_path):
        """Should find matches in JSONL content."""
        from app.api.search import _search_session_jsonl

        # Setup mocks
        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Hello world"},
                "timestamp": "2024-01-01T00:00:00Z"
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "Hi there, hello to you!"},
                "timestamp": "2024-01-01T00:01:00Z"
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        assert len(result) == 2
        assert result[0].role == "user"
        assert result[1].role == "assistant"

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_skips_meta_messages(self, mock_parse, mock_get_path):
        """Should skip meta and sidechain messages."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "hello meta"},
            },
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "hello sidechain"},
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "hello real message"},
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        # Only the regular message should match
        assert len(result) == 1
        assert "real message" in result[0].snippet

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_skips_queue_operations(self, mock_parse, mock_get_path):
        """Should skip queue-operation and file-history-snapshot entries."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {"type": "queue-operation", "data": "hello"},
            {"type": "file-history-snapshot", "content": "hello"},
            {
                "type": "user",
                "message": {"role": "user", "content": "hello normal"},
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        assert len(result) == 1

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_limits_matches(self, mock_parse, mock_get_path):
        """Should limit matches per session to 10."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        # Create 20 matching messages
        mock_parse.return_value = [
            {
                "type": "user",
                "message": {"role": "user", "content": f"hello {i}"},
            }
            for i in range(20)
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        assert len(result) == 10  # Should be limited

    @patch("app.api.search.database")
    @patch("app.api.search.settings")
    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_with_project(self, mock_parse, mock_get_path, mock_settings, mock_db):
        """Should use project working directory when available."""
        from app.api.search import _search_session_jsonl

        mock_settings.workspace_dir = Path("/workspace")
        mock_db.get_project.return_value = {"path": "my-project"}
        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {
                "type": "user",
                "message": {"role": "user", "content": "hello"},
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123", "project_id": "proj-1"}
        result = _search_session_jsonl(session, "hello")

        assert len(result) == 1
        # Verify get_project was called
        mock_db.get_project.assert_called_once_with("proj-1")

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_handles_list_content(self, mock_parse, mock_get_path):
        """Should handle content as list of blocks."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world!"}
                    ]
                },
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        assert len(result) == 1

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_code_only(self, mock_parse, mock_get_path):
        """Should search only in code blocks when in_code_only=True."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "Here is code:\n```python\nfoo = 'bar'\n```\nAnd text with baz"
                },
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}

        # foo is in code block
        result = _search_session_jsonl(session, "foo", in_code_only=True)
        assert len(result) == 1

        # baz is outside code block
        result = _search_session_jsonl(session, "baz", in_code_only=True)
        assert len(result) == 0

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_handles_exception(self, mock_parse, mock_get_path):
        """Should handle exceptions gracefully and return empty list."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.side_effect = Exception("Parse error")

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        # Should return empty list instead of raising
        assert result == []

    @patch("app.api.search.get_session_jsonl_path")
    @patch("app.api.search.parse_jsonl_file")
    def test_search_session_empty_content(self, mock_parse, mock_get_path):
        """Should skip messages with empty content."""
        from app.api.search import _search_session_jsonl

        mock_get_path.return_value = Path("/fake/path.jsonl")
        mock_parse.return_value = [
            {
                "type": "user",
                "message": {"role": "user", "content": ""},
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "hello world"},
            }
        ]

        session = {"id": "test", "sdk_session_id": "sdk-123"}
        result = _search_session_jsonl(session, "hello")

        # Should only find the message with actual content
        assert len(result) == 1


class TestAdvancedSearchEndpoint:
    """Test the /api/v1/search endpoint."""

    @pytest.fixture
    def search_client(self):
        """Create a TestClient with mocked auth using dependency overrides."""
        from app.main import app
        from app.api.auth import require_auth

        # Override the auth dependency
        def mock_require_auth():
            return "test-token"

        app.dependency_overrides[require_auth] = mock_require_auth

        # Also mock get_api_user_from_request
        with patch("app.api.search.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = None  # Admin user (no restrictions)
            with TestClient(app) as client:
                yield client

        # Clean up
        app.dependency_overrides.clear()

    @pytest.fixture
    def unauthenticated_client(self):
        """Create a TestClient without auth mocking (for auth tests)."""
        from app.main import app
        # Ensure no overrides are active
        app.dependency_overrides.clear()
        with TestClient(app) as client:
            yield client

    def test_search_requires_auth(self, unauthenticated_client):
        """Should require authentication."""
        response = unauthenticated_client.get("/api/v1/search?q=test")
        assert response.status_code == 401

    def test_search_requires_query(self, search_client):
        """Should require query parameter."""
        response = search_client.get("/api/v1/search")
        assert response.status_code == 422  # Validation error

    def test_search_rejects_empty_query(self, search_client):
        """Should reject empty query."""
        response = search_client.get("/api/v1/search?q=")
        assert response.status_code == 422

    @patch("app.api.search.database")
    def test_search_basic_query(self, mock_db, search_client):
        """Should perform basic search."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get("/api/v1/search?q=test")
        assert response.status_code == 200
        assert response.json() == []

    def test_search_invalid_regex(self, search_client):
        """Should reject invalid regex pattern."""
        response = search_client.get("/api/v1/search?q=[invalid&regex=true")
        assert response.status_code == 400
        assert "Invalid regex" in response.json()["detail"]

    @patch("app.api.search.database")
    def test_search_valid_regex(self, mock_db, search_client):
        """Should accept valid regex pattern."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get("/api/v1/search?q=test.*pattern&regex=true")
        assert response.status_code == 200

    def test_search_invalid_start_date(self, search_client):
        """Should reject invalid start_date format."""
        response = search_client.get("/api/v1/search?q=test&start_date=invalid")
        assert response.status_code == 400
        assert "start_date" in response.json()["detail"]

    def test_search_invalid_end_date(self, search_client):
        """Should reject invalid end_date format."""
        response = search_client.get("/api/v1/search?q=test&end_date=not-a-date")
        assert response.status_code == 400
        assert "end_date" in response.json()["detail"]

    @patch("app.api.search.database")
    def test_search_valid_date_range(self, mock_db, search_client):
        """Should accept valid date range."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get(
            "/api/v1/search?q=test&start_date=2024-01-01&end_date=2024-12-31"
        )
        assert response.status_code == 200

    @patch("app.api.search.database")
    @patch("app.api.search._search_session_jsonl")
    def test_search_with_matches(self, mock_search_jsonl, mock_db, search_client):
        """Should return search results."""
        from app.api.search import SearchMatch

        mock_db.get_sessions.return_value = [
            {
                "id": "session-1",
                "title": "Test Session",
                "created_at": "2024-01-15T10:00:00Z",
                "updated_at": "2024-01-15T11:00:00Z",
                "profile_id": "profile-1",
                "project_id": None,
                "sdk_session_id": "sdk-123"
            }
        ]
        mock_db.get_all_profiles.return_value = [
            {"id": "profile-1", "name": "Default Profile"}
        ]
        mock_search_jsonl.return_value = [
            SearchMatch(message_index=0, snippet="Found **test** here", role="user")
        ]

        response = search_client.get("/api/v1/search?q=test")
        assert response.status_code == 200

        results = response.json()
        assert len(results) == 1
        assert results[0]["session_id"] == "session-1"
        assert results[0]["match_count"] == 1

    @patch("app.api.search.database")
    @patch("app.api.search._search_session_jsonl")
    def test_search_title_match_only(self, mock_search_jsonl, mock_db, search_client):
        """Should include sessions where title matches but no content matches."""
        mock_db.get_sessions.return_value = [
            {
                "id": "session-1",
                "title": "Testing API endpoints",
                "created_at": "2024-01-15T10:00:00Z",
                "updated_at": "2024-01-15T11:00:00Z",
                "profile_id": None,
                "project_id": None,
                "sdk_session_id": "sdk-123"
            }
        ]
        mock_db.get_all_profiles.return_value = []
        mock_search_jsonl.return_value = []  # No content matches

        response = search_client.get("/api/v1/search?q=testing")
        assert response.status_code == 200

        results = response.json()
        assert len(results) == 1
        # Title match should add a match entry with role="title"
        assert results[0]["matches"][0]["role"] == "title"

    @patch("app.api.search.database")
    def test_search_pagination(self, mock_db, search_client):
        """Should support pagination parameters."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get("/api/v1/search?q=test&limit=10&offset=5")
        assert response.status_code == 200

    def test_search_limit_validation(self, search_client):
        """Should validate limit bounds."""
        # Limit too high
        response = search_client.get("/api/v1/search?q=test&limit=101")
        assert response.status_code == 422

        # Limit too low
        response = search_client.get("/api/v1/search?q=test&limit=0")
        assert response.status_code == 422

    def test_search_offset_validation(self, search_client):
        """Should validate offset bounds."""
        # Negative offset
        response = search_client.get("/api/v1/search?q=test&offset=-1")
        assert response.status_code == 422

    @patch("app.api.search.database")
    @patch("app.api.search._search_session_jsonl")
    def test_search_date_filter_start(self, mock_search_jsonl, mock_db, search_client):
        """Should filter sessions by start date."""
        mock_db.get_sessions.return_value = [
            {
                "id": "session-old",
                "title": "Old Session",
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T11:00:00Z",
                "profile_id": None,
                "project_id": None,
            },
            {
                "id": "session-new",
                "title": "New Session",
                "created_at": "2024-06-15T10:00:00Z",
                "updated_at": "2024-06-15T11:00:00Z",
                "profile_id": None,
                "project_id": None,
            }
        ]
        mock_db.get_all_profiles.return_value = []
        mock_search_jsonl.return_value = []

        # Filter for sessions after June
        response = search_client.get("/api/v1/search?q=Session&start_date=2024-06-01")
        assert response.status_code == 200

        results = response.json()
        # Only the new session should match
        assert len(results) == 1
        assert results[0]["session_id"] == "session-new"

    @patch("app.api.search.database")
    @patch("app.api.search._search_session_jsonl")
    def test_search_date_filter_end(self, mock_search_jsonl, mock_db, search_client):
        """Should filter sessions by end date."""
        mock_db.get_sessions.return_value = [
            {
                "id": "session-old",
                "title": "Old Session",
                "created_at": "2024-01-01T10:00:00Z",
                "updated_at": "2024-01-01T11:00:00Z",
                "profile_id": None,
                "project_id": None,
            },
            {
                "id": "session-new",
                "title": "New Session",
                "created_at": "2024-06-15T10:00:00Z",
                "updated_at": "2024-06-15T11:00:00Z",
                "profile_id": None,
                "project_id": None,
            }
        ]
        mock_db.get_all_profiles.return_value = []
        mock_search_jsonl.return_value = []

        # Filter for sessions before February
        response = search_client.get("/api/v1/search?q=Session&end_date=2024-02-01")
        assert response.status_code == 200

        results = response.json()
        # Only the old session should match
        assert len(results) == 1
        assert results[0]["session_id"] == "session-old"

    @patch("app.api.search.database")
    def test_search_profile_filter(self, mock_db, search_client):
        """Should filter by profile_id."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get("/api/v1/search?q=test&profile_id=profile-123")
        assert response.status_code == 200

        # Verify get_sessions was called with profile_id
        mock_db.get_sessions.assert_called_once()
        call_kwargs = mock_db.get_sessions.call_args[1]
        assert call_kwargs.get("profile_id") == "profile-123"

    @patch("app.api.search.database")
    def test_search_project_filter(self, mock_db, search_client):
        """Should filter by project_id."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get("/api/v1/search?q=test&project_id=project-456")
        assert response.status_code == 200

        mock_db.get_sessions.assert_called_once()
        call_kwargs = mock_db.get_sessions.call_args[1]
        assert call_kwargs.get("project_id") == "project-456"

    @patch("app.api.search.database")
    def test_search_in_code_only_param(self, mock_db, search_client):
        """Should pass in_code_only parameter."""
        mock_db.get_sessions.return_value = []
        mock_db.get_all_profiles.return_value = []

        response = search_client.get("/api/v1/search?q=test&in_code_only=true")
        assert response.status_code == 200


class TestSearchSuggestionsEndpoint:
    """Test the /api/v1/search/suggestions endpoint."""

    @pytest.fixture
    def search_client(self):
        """Create a TestClient with mocked auth using dependency overrides."""
        from app.main import app
        from app.api.auth import require_auth

        def mock_require_auth():
            return "test-token"

        app.dependency_overrides[require_auth] = mock_require_auth

        with patch("app.api.search.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = None  # Admin user
            with TestClient(app) as client:
                yield client

        app.dependency_overrides.clear()

    @pytest.fixture
    def unauthenticated_client(self):
        """Create a TestClient without auth mocking."""
        from app.main import app
        app.dependency_overrides.clear()
        with TestClient(app) as client:
            yield client

    def test_suggestions_requires_auth(self, unauthenticated_client):
        """Should require authentication."""
        response = unauthenticated_client.get("/api/v1/search/suggestions?q=test")
        assert response.status_code == 401

    def test_suggestions_requires_query(self, search_client):
        """Should require query parameter."""
        response = search_client.get("/api/v1/search/suggestions")
        assert response.status_code == 422

    @patch("app.api.search.database")
    def test_suggestions_basic(self, mock_db, search_client):
        """Should return suggestions based on session titles."""
        mock_db.get_sessions.return_value = [
            {"title": "Testing the API"},
            {"title": "Test driven development"},
            {"title": "Unrelated title"},
        ]

        response = search_client.get("/api/v1/search/suggestions?q=test")
        assert response.status_code == 200

        data = response.json()
        assert "suggestions" in data
        # Should find titles containing "test"
        assert len(data["suggestions"]) == 2
        assert "Testing the API" in data["suggestions"]
        assert "Test driven development" in data["suggestions"]

    @patch("app.api.search.database")
    def test_suggestions_case_insensitive(self, mock_db, search_client):
        """Should match case-insensitively."""
        mock_db.get_sessions.return_value = [
            {"title": "UPPERCASE TEST"},
            {"title": "lowercase test"},
        ]

        response = search_client.get("/api/v1/search/suggestions?q=TEST")
        assert response.status_code == 200

        data = response.json()
        assert len(data["suggestions"]) == 2

    @patch("app.api.search.database")
    def test_suggestions_limit(self, mock_db, search_client):
        """Should respect limit parameter."""
        mock_db.get_sessions.return_value = [
            {"title": f"Test title {i}"}
            for i in range(20)
        ]

        response = search_client.get("/api/v1/search/suggestions?q=test&limit=5")
        assert response.status_code == 200

        data = response.json()
        assert len(data["suggestions"]) == 5

    @patch("app.api.search.database")
    def test_suggestions_no_duplicates(self, mock_db, search_client):
        """Should not return duplicate suggestions."""
        mock_db.get_sessions.return_value = [
            {"title": "Same Title"},
            {"title": "Same Title"},
            {"title": "Same Title"},
        ]

        response = search_client.get("/api/v1/search/suggestions?q=same")
        assert response.status_code == 200

        data = response.json()
        assert len(data["suggestions"]) == 1
        assert data["suggestions"][0] == "Same Title"

    @patch("app.api.search.database")
    def test_suggestions_skips_null_titles(self, mock_db, search_client):
        """Should skip sessions with null titles."""
        mock_db.get_sessions.return_value = [
            {"title": None},
            {"title": "Valid Test Title"},
            {"title": ""},
        ]

        response = search_client.get("/api/v1/search/suggestions?q=test")
        assert response.status_code == 200

        data = response.json()
        assert len(data["suggestions"]) == 1

    def test_suggestions_limit_bounds(self, search_client):
        """Should validate limit bounds."""
        # Limit too high
        response = search_client.get("/api/v1/search/suggestions?q=test&limit=25")
        assert response.status_code == 422

        # Limit too low
        response = search_client.get("/api/v1/search/suggestions?q=test&limit=0")
        assert response.status_code == 422


class TestApiUserRestrictions:
    """Test API user-specific restrictions."""

    @patch("app.api.search.database")
    def test_api_user_project_restriction(self, mock_db):
        """Should restrict to API user's project."""
        from app.main import app
        from app.api.auth import require_auth

        def mock_require_auth():
            return "api-token"

        app.dependency_overrides[require_auth] = mock_require_auth

        with patch("app.api.search.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "api-user-1",
                "username": "testuser",
                "project_id": "restricted-project"
            }
            mock_db.get_sessions.return_value = []
            mock_db.get_all_profiles.return_value = []

            with TestClient(app) as client:
                response = client.get("/api/v1/search?q=test")
                assert response.status_code == 200

                # Verify project_id was passed to get_sessions
                call_kwargs = mock_db.get_sessions.call_args[1]
                assert call_kwargs.get("project_id") == "restricted-project"

        app.dependency_overrides.clear()

    @patch("app.api.search.database")
    def test_api_user_profile_restriction(self, mock_db):
        """Should restrict to API user's profile."""
        from app.main import app
        from app.api.auth import require_auth

        def mock_require_auth():
            return "api-token"

        app.dependency_overrides[require_auth] = mock_require_auth

        with patch("app.api.search.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "api-user-1",
                "username": "testuser",
                "profile_id": "restricted-profile"
            }
            mock_db.get_sessions.return_value = []
            mock_db.get_all_profiles.return_value = []

            with TestClient(app) as client:
                response = client.get("/api/v1/search?q=test")
                assert response.status_code == 200

                call_kwargs = mock_db.get_sessions.call_args[1]
                assert call_kwargs.get("profile_id") == "restricted-profile"

        app.dependency_overrides.clear()

    @patch("app.api.search.database")
    def test_api_user_id_filter(self, mock_db):
        """Should filter by API user ID."""
        from app.main import app
        from app.api.auth import require_auth

        def mock_require_auth():
            return "api-token"

        app.dependency_overrides[require_auth] = mock_require_auth

        with patch("app.api.search.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "api-user-1",
                "username": "testuser"
            }
            mock_db.get_sessions.return_value = []
            mock_db.get_all_profiles.return_value = []

            with TestClient(app) as client:
                response = client.get("/api/v1/search?q=test")
                assert response.status_code == 200

                call_kwargs = mock_db.get_sessions.call_args[1]
                assert call_kwargs.get("api_user_id") == "api-user-1"

        app.dependency_overrides.clear()

    @patch("app.api.search.database")
    def test_api_user_overrides_query_params(self, mock_db):
        """API user restrictions should override query parameters."""
        from app.main import app
        from app.api.auth import require_auth

        def mock_require_auth():
            return "api-token"

        app.dependency_overrides[require_auth] = mock_require_auth

        with patch("app.api.search.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "api-user-1",
                "username": "testuser",
                "project_id": "user-project"
            }
            mock_db.get_sessions.return_value = []
            mock_db.get_all_profiles.return_value = []

            with TestClient(app) as client:
                # Try to query a different project
                response = client.get("/api/v1/search?q=test&project_id=other-project")
                assert response.status_code == 200

                # Should use API user's project, not the query param
                call_kwargs = mock_db.get_sessions.call_args[1]
                assert call_kwargs.get("project_id") == "user-project"

        app.dependency_overrides.clear()


class TestSearchResponseModel:
    """Test search response model structure."""

    def test_search_match_model(self):
        """SearchMatch model should have correct fields."""
        from app.api.search import SearchMatch

        match = SearchMatch(
            message_index=5,
            snippet="test snippet",
            role="assistant",
            timestamp="2024-01-01T00:00:00Z"
        )

        assert match.message_index == 5
        assert match.snippet == "test snippet"
        assert match.role == "assistant"
        assert match.timestamp == "2024-01-01T00:00:00Z"

    def test_search_match_optional_timestamp(self):
        """SearchMatch timestamp should be optional."""
        from app.api.search import SearchMatch

        match = SearchMatch(
            message_index=0,
            snippet="test",
            role="user"
        )

        assert match.timestamp is None

    def test_advanced_search_result_model(self):
        """AdvancedSearchResult model should have correct fields."""
        from app.api.search import AdvancedSearchResult, SearchMatch

        result = AdvancedSearchResult(
            session_id="session-123",
            session_title="Test Session",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T12:00:00Z",
            profile_id="profile-1",
            profile_name="Default",
            project_id="project-1",
            matches=[
                SearchMatch(message_index=0, snippet="test", role="user")
            ],
            match_count=1
        )

        assert result.session_id == "session-123"
        assert result.session_title == "Test Session"
        assert result.profile_name == "Default"
        assert len(result.matches) == 1
        assert result.match_count == 1

    def test_advanced_search_result_optional_fields(self):
        """AdvancedSearchResult optional fields should default properly."""
        from app.api.search import AdvancedSearchResult

        result = AdvancedSearchResult(
            session_id="session-123",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T12:00:00Z"
        )

        assert result.session_title is None
        assert result.profile_id is None
        assert result.profile_name is None
        assert result.project_id is None
        assert result.matches == []
        assert result.match_count == 0
