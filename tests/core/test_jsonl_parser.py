"""
Unit tests for JSONL history parser.

Tests cover:
- Content truncation for display
- Project directory name conversion
- Text extraction from message content
- JSONL parsing
"""

import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.core import jsonl_parser


class TestTruncateForDisplay:
    """Test content truncation functionality."""

    def test_truncate_short_content(self):
        """Short content should not be truncated."""
        content = "Hello, world!"
        result = jsonl_parser._truncate_for_display(content)
        assert result == content

    def test_truncate_empty_content(self):
        """Empty content should return empty string."""
        assert jsonl_parser._truncate_for_display("") == ""

    def test_truncate_none_content(self):
        """None content should return None."""
        assert jsonl_parser._truncate_for_display(None) is None

    def test_truncate_at_max_size(self):
        """Content exactly at max size should not be truncated."""
        content = "x" * 2000
        result = jsonl_parser._truncate_for_display(content, max_size=2000)
        assert result == content

    def test_truncate_over_max_size(self):
        """Content over max size should be truncated with message."""
        content = "x" * 3000
        result = jsonl_parser._truncate_for_display(content, max_size=2000)

        assert len(result) < len(content)
        assert "truncated" in result.lower()

    def test_truncate_base64_png(self):
        """PNG base64 content should be detected and truncated specially."""
        # PNG base64 signature
        content = "iVBOR" + "A" * 50000

        result = jsonl_parser._truncate_for_display(content, max_size=2000)

        assert "Image/binary content" in result or "truncated" in result.lower()

    def test_truncate_base64_jpeg(self):
        """JPEG base64 content should be detected and truncated specially."""
        # JPEG base64 signature
        content = "/9j/" + "A" * 50000

        result = jsonl_parser._truncate_for_display(content, max_size=2000)

        assert "Image/binary content" in result or "truncated" in result.lower()

    def test_truncate_data_uri(self):
        """Data URI images should be detected."""
        content = "data:image/png;base64," + "A" * 50000

        result = jsonl_parser._truncate_for_display(content, max_size=2000)

        assert "Image/binary content" in result or "truncated" in result.lower()


class TestGetProjectDirName:
    """Test working directory to project dir conversion."""

    def test_simple_path(self):
        """Should convert simple path."""
        result = jsonl_parser.get_project_dir_name("/workspace")
        assert result == "-workspace"

    def test_nested_path(self):
        """Should convert nested path."""
        result = jsonl_parser.get_project_dir_name("/workspace/github-projects")
        assert result == "-workspace-github-projects"

    def test_deep_nested_path(self):
        """Should convert deeply nested path."""
        result = jsonl_parser.get_project_dir_name("/home/user/projects/myapp")
        assert result == "-home-user-projects-myapp"

    def test_relative_path(self):
        """Should handle relative paths (though not typical usage)."""
        result = jsonl_parser.get_project_dir_name("workspace/project")
        assert result == "workspace-project"


class TestExtractTextFromContent:
    """Test text extraction from message content."""

    def test_extract_from_string(self):
        """Should return string content directly."""
        content = "Hello, world!"
        result = jsonl_parser.extract_text_from_content(content)
        assert result == content

    def test_extract_from_empty_string(self):
        """Empty string should return empty string."""
        result = jsonl_parser.extract_text_from_content("")
        assert result == ""

    def test_extract_from_text_block_list(self):
        """Should extract text from list of text blocks."""
        content = [
            {"type": "text", "text": "Hello, "},
            {"type": "text", "text": "world!"}
        ]
        result = jsonl_parser.extract_text_from_content(content)
        assert result == "Hello, world!"

    def test_extract_from_mixed_block_list(self):
        """Should extract only text from mixed block types."""
        content = [
            {"type": "text", "text": "Start"},
            {"type": "image", "source": "..."},
            {"type": "text", "text": "End"}
        ]
        result = jsonl_parser.extract_text_from_content(content)
        assert result == "StartEnd"

    def test_extract_from_tool_use_block(self):
        """Should handle tool_use blocks (no text extracted)."""
        content = [
            {"type": "tool_use", "id": "123", "name": "some_tool", "input": {}}
        ]
        result = jsonl_parser.extract_text_from_content(content)
        assert result == ""

    def test_extract_from_empty_list(self):
        """Empty list should return empty string."""
        result = jsonl_parser.extract_text_from_content([])
        assert result == ""

    def test_extract_from_none(self):
        """None should return empty string."""
        result = jsonl_parser.extract_text_from_content(None)
        assert result == ""

    def test_extract_from_other_type(self):
        """Other types should return empty string."""
        result = jsonl_parser.extract_text_from_content(12345)
        assert result == ""
        result = jsonl_parser.extract_text_from_content({"not": "a list"})
        assert result == ""


class TestParseJsonlFile:
    """Test JSONL file parsing."""

    def test_parse_valid_jsonl(self):
        """Should parse valid JSONL file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type": "user", "message": "Hello"}\n')
            f.write('{"type": "assistant", "message": "Hi"}\n')
            f.write('{"type": "user", "message": "Bye"}\n')
            f.flush()

            path = Path(f.name)
            results = list(jsonl_parser.parse_jsonl_file(path))

            assert len(results) == 3
            assert results[0]["type"] == "user"
            assert results[1]["type"] == "assistant"
            assert results[2]["type"] == "user"

        # Cleanup
        path.unlink()

    def test_parse_with_empty_lines(self):
        """Should skip empty lines."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type": "user"}\n')
            f.write('\n')
            f.write('   \n')
            f.write('{"type": "assistant"}\n')
            f.flush()

            path = Path(f.name)
            results = list(jsonl_parser.parse_jsonl_file(path))

            assert len(results) == 2

        path.unlink()

    def test_parse_with_invalid_json_line(self):
        """Should skip invalid JSON lines."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type": "user"}\n')
            f.write('this is not json\n')
            f.write('{"type": "assistant"}\n')
            f.flush()

            path = Path(f.name)
            results = list(jsonl_parser.parse_jsonl_file(path))

            # Should have 2 valid lines
            assert len(results) == 2

        path.unlink()

    def test_parse_nonexistent_file(self):
        """Should handle nonexistent file gracefully."""
        path = Path("/nonexistent/path/file.jsonl")
        results = list(jsonl_parser.parse_jsonl_file(path))

        assert results == []

    def test_parse_complex_json_objects(self):
        """Should parse complex nested JSON objects."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            obj = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "tool_use", "id": "123", "name": "test", "input": {"key": "value"}}
                    ]
                },
                "uuid": "test-uuid-123",
                "timestamp": "2024-01-01T00:00:00Z"
            }
            f.write(json.dumps(obj) + '\n')
            f.flush()

            path = Path(f.name)
            results = list(jsonl_parser.parse_jsonl_file(path))

            assert len(results) == 1
            assert results[0]["message"]["content"][0]["text"] == "Hello"

        path.unlink()


class TestGetSessionJsonlPath:
    """Test session JSONL path resolution."""

    def test_get_session_jsonl_path_found(self):
        """Should return path when JSONL exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create mock structure
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)

            session_id = "test-session-123"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            jsonl_file.write_text('{"test": true}')

            # Use PropertyMock to properly mock the property
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                # Configure the mock to return the projects_dir
                mock_settings.configure_mock(get_claude_projects_dir=projects_dir)

                result = jsonl_parser.get_session_jsonl_path(session_id, "/workspace")

                assert result is not None
                assert result == jsonl_file

    def test_get_session_jsonl_path_not_found(self):
        """Should return None when JSONL doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir(parents=True)

            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir

                result = jsonl_parser.get_session_jsonl_path("nonexistent-session", "/workspace")

                assert result is None

    def test_get_session_jsonl_path_search_all_projects(self):
        """Should search all project directories if not found in expected location."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"

            # Create JSONL in different project directory
            other_project = projects_dir / "-other-project"
            other_project.mkdir(parents=True)

            session_id = "wandering-session"
            jsonl_file = other_project / f"{session_id}.jsonl"
            jsonl_file.write_text('{"test": true}')

            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir

                result = jsonl_parser.get_session_jsonl_path(session_id, "/workspace")

                # Should find it in the other project directory
                assert result is not None
                assert result == jsonl_file
