"""
Unit tests for JSONL history parser.

Tests cover:
- Content truncation for display
- Project directory name conversion
- Text extraction from message content
- JSONL parsing
- System content filtering
- Local command output extraction
- Agent JSONL file discovery and parsing
- Session history parsing with tool use and results
- Cost extraction from JSONL
- Session listing
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
        content = "iVBOR" + "A" * 50000
        result = jsonl_parser._truncate_for_display(content, max_size=2000)
        assert "Image/binary content" in result or "truncated" in result.lower()

    def test_truncate_base64_jpeg(self):
        """JPEG base64 content should be detected and truncated specially."""
        content = "/9j/" + "A" * 50000
        result = jsonl_parser._truncate_for_display(content, max_size=2000)
        assert "Image/binary content" in result or "truncated" in result.lower()

    def test_truncate_data_uri(self):
        """Data URI images should be detected."""
        content = "data:image/png;base64," + "A" * 50000
        result = jsonl_parser._truncate_for_display(content, max_size=2000)
        assert "Image/binary content" in result or "truncated" in result.lower()

    def test_truncate_base64_gif(self):
        """GIF base64 content should be detected."""
        content = "R0lGOD" + "A" * 50000
        result = jsonl_parser._truncate_for_display(content, max_size=2000)
        assert "Image/binary content" in result

    def test_truncate_high_alphanumeric_density(self):
        """Content with very high alphanumeric density should be detected as base64."""
        content = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" * 500
        result = jsonl_parser._truncate_for_display(content, max_size=2000)
        assert "Image/binary content" in result


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
        content = [{"type": "tool_use", "id": "123", "name": "some_tool", "input": {}}]
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

    def test_extract_with_missing_text_key(self):
        """Should handle blocks without text key."""
        content = [{"type": "text"}, {"type": "text", "text": "Hello"}]
        result = jsonl_parser.extract_text_from_content(content)
        assert result == "Hello"


class TestIsSystemContent:
    """Test system/meta content filtering."""

    def test_empty_content_is_system(self):
        """Empty content should be treated as system content."""
        assert jsonl_parser._is_system_content("") is True
        assert jsonl_parser._is_system_content(None) is True

    def test_command_name_is_system(self):
        """Command name tags should be system content."""
        assert jsonl_parser._is_system_content("<command-name>test</command-name>") is True

    def test_command_message_is_system(self):
        """Command message tags should be system content."""
        assert jsonl_parser._is_system_content("<command-message>msg</command-message>") is True

    def test_command_args_is_system(self):
        """Command args tags should be system content."""
        assert jsonl_parser._is_system_content("<command-args>args</command-args>") is True

    def test_interrupt_message_is_system(self):
        """Interrupt messages should be system content."""
        assert jsonl_parser._is_system_content("[Request interrupted by user]") is True

    def test_caveat_message_is_system(self):
        """Caveat messages should be system content."""
        assert jsonl_parser._is_system_content("Caveat: The messages below were generated...") is True

    def test_normal_content_not_system(self):
        """Normal user content should not be filtered."""
        assert jsonl_parser._is_system_content("Hello, how are you?") is False

    def test_local_command_stdout_not_system(self):
        """Local command stdout should NOT be filtered."""
        content = "<local-command-stdout>Output here</local-command-stdout>"
        assert jsonl_parser._is_system_content(content) is False


class TestExtractLocalCommandOutput:
    """Test local command output extraction."""

    def test_extract_simple_output(self):
        """Should extract content from local-command-stdout tags."""
        content = "<local-command-stdout>Command output here</local-command-stdout>"
        result = jsonl_parser._extract_local_command_output(content)
        assert result == "Command output here"

    def test_extract_multiline_output(self):
        """Should extract multiline content."""
        content = "<local-command-stdout>Line 1\nLine 2\nLine 3</local-command-stdout>"
        result = jsonl_parser._extract_local_command_output(content)
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_no_tags_returns_none(self):
        """Should return None if no tags found."""
        content = "Regular content without tags"
        result = jsonl_parser._extract_local_command_output(content)
        assert result is None

    def test_empty_tags(self):
        """Should handle empty tags."""
        content = "<local-command-stdout></local-command-stdout>"
        result = jsonl_parser._extract_local_command_output(content)
        assert result == ""

    def test_whitespace_in_output(self):
        """Should strip whitespace from extracted content."""
        content = "<local-command-stdout>   trimmed content   </local-command-stdout>"
        result = jsonl_parser._extract_local_command_output(content)
        assert result == "trimmed content"


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
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            jsonl_file.write_text('{"test": true}')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
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
            other_project = projects_dir / "-other-project"
            other_project.mkdir(parents=True)
            session_id = "wandering-session"
            jsonl_file = other_project / f"{session_id}.jsonl"
            jsonl_file.write_text('{"test": true}')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_session_jsonl_path(session_id, "/workspace")
                assert result is not None
                assert result == jsonl_file


class TestGetAgentJsonlPaths:
    """Test agent JSONL file discovery."""

    def test_find_agent_files_in_project_dir(self):
        """Should find agent files in the project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            agent_file = project_dir / "agent-abc123.jsonl"
            agent_file.write_text(json.dumps({"sessionId": session_id, "type": "user"}) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert "abc123" in result
                assert result["abc123"] == agent_file

    def test_no_agent_files_found(self):
        """Should return empty dict when no agent files exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert result == {}

    def test_agent_file_different_session_ignored(self):
        """Should ignore agent files that belong to different sessions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            agent_file = project_dir / "agent-other123.jsonl"
            agent_file.write_text(json.dumps({"sessionId": "different-session", "type": "user"}) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert "other123" not in result

    def test_fallback_search_when_project_dir_not_found(self):
        """Should search all directories when project dir doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            other_dir = projects_dir / "-other-project"
            other_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = other_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            agent_file = other_dir / "agent-xyz789.jsonl"
            agent_file.write_text(json.dumps({"sessionId": session_id}) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert "xyz789" in result

    def test_handle_invalid_agent_file(self):
        """Should handle agent files with invalid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            agent_file = project_dir / "agent-bad123.jsonl"
            agent_file.write_text('not valid json\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert "bad123" not in result


class TestParseAgentHistory:
    """Test agent history parsing."""

    def test_parse_text_blocks(self):
        """Should parse text blocks from agent history."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            entry = {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello from agent"}]},
                "uuid": "test-123",
                "timestamp": "2024-01-01T00:00:00Z"
            }
            f.write(json.dumps(entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert len(result) == 1
            assert result[0]["type"] == "text"
            assert result[0]["content"] == "Hello from agent"
        path.unlink()

    def test_parse_tool_use_blocks(self):
        """Should parse tool use blocks from agent history."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            entry = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tool-123", "name": "Bash", "input": {"command": "ls"}}]
                },
                "uuid": "test-123",
                "timestamp": "2024-01-01T00:00:00Z"
            }
            f.write(json.dumps(entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert len(result) == 1
            assert result[0]["type"] == "tool_use"
            assert result[0]["toolName"] == "Bash"
            assert result[0]["toolId"] == "tool-123"
            assert result[0]["toolStatus"] == "running"
        path.unlink()

    def test_parse_tool_results(self):
        """Should parse tool results and group with tool use."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            tool_use_entry = {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-123", "name": "Bash", "input": {"command": "ls"}}]},
                "uuid": "use-123",
                "timestamp": "2024-01-01T00:00:00Z"
            }
            f.write(json.dumps(tool_use_entry) + '\n')
            tool_result_entry = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-123", "content": "file1.txt\nfile2.txt", "is_error": False}]},
                "uuid": "result-123",
                "timestamp": "2024-01-01T00:00:01Z"
            }
            f.write(json.dumps(tool_result_entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert len(result) == 1
            assert result[0]["type"] == "tool_use"
            assert result[0]["toolResult"] == "file1.txt\nfile2.txt"
            assert result[0]["toolStatus"] == "complete"
        path.unlink()

    def test_parse_tool_result_with_error(self):
        """Should handle error tool results."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            tool_use_entry = {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-123", "name": "Bash", "input": {}}]},
                "uuid": "use-123"
            }
            f.write(json.dumps(tool_use_entry) + '\n')
            tool_result_entry = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-123", "content": "Command failed", "is_error": True}]},
                "uuid": "result-123"
            }
            f.write(json.dumps(tool_result_entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert result[0]["toolStatus"] == "error"
        path.unlink()

    def test_parse_orphan_tool_result(self):
        """Should handle tool results without matching tool use."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            tool_result_entry = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "orphan-tool-123", "content": "Some output", "is_error": False}]},
                "uuid": "result-123",
                "timestamp": "2024-01-01T00:00:00Z"
            }
            f.write(json.dumps(tool_result_entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert len(result) == 1
            assert result[0]["type"] == "tool_result"
            assert result[0]["toolId"] == "orphan-tool-123"
        path.unlink()

    def test_parse_content_list_in_tool_result(self):
        """Should extract text from content list in tool results."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            tool_result_entry = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-123", "content": [{"type": "text", "text": "Extracted text"}], "is_error": False}]},
                "uuid": "result-123"
            }
            f.write(json.dumps(tool_result_entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert result[0]["content"] == "Extracted text"
        path.unlink()

    def test_skip_non_dict_blocks(self):
        """Should skip non-dict blocks in content."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            entry = {
                "type": "assistant",
                "message": {"role": "assistant", "content": ["not a dict", {"type": "text", "text": "Valid"}]},
                "uuid": "test-123"
            }
            f.write(json.dumps(entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert len(result) == 1
            assert result[0]["content"] == "Valid"
        path.unlink()

    def test_skip_empty_text(self):
        """Should skip empty text blocks."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            entry = {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": ""}, {"type": "text", "text": "Non-empty"}]},
                "uuid": "test-123"
            }
            f.write(json.dumps(entry) + '\n')
            f.flush()
            path = Path(f.name)
            result = jsonl_parser.parse_agent_history(path)
            assert len(result) == 1
            assert result[0]["content"] == "Non-empty"
        path.unlink()


class TestParseSessionHistory:
    """Test main session history parsing."""

    def test_parse_returns_empty_for_missing_file(self):
        """Should return empty list when JSONL file not found."""
        with patch('app.core.jsonl_parser.get_session_jsonl_path') as mock_path:
            mock_path.return_value = None
            result = jsonl_parser.parse_session_history("nonexistent-session")
            assert result == []

    def test_parse_user_message(self):
        """Should parse simple user messages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entry = {"type": "user", "message": {"role": "user", "content": "Hello, Claude!"}, "uuid": "user-msg-1", "timestamp": "2024-01-01T00:00:00Z"}
            jsonl_file.write_text(json.dumps(entry) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["role"] == "user"
                assert result[0]["content"] == "Hello, Claude!"

    def test_parse_assistant_text_message(self):
        """Should parse assistant text messages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entry = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello! How can I help?"}]}, "uuid": "asst-msg-1", "timestamp": "2024-01-01T00:00:00Z"}
            jsonl_file.write_text(json.dumps(entry) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["role"] == "assistant"
                assert result[0]["type"] == "text"
                assert result[0]["content"] == "Hello! How can I help?"

    def test_parse_assistant_string_content(self):
        """Should parse assistant messages with string content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entry = {"type": "assistant", "message": {"role": "assistant", "content": "Plain string response"}, "uuid": "asst-msg-1", "timestamp": "2024-01-01T00:00:00Z"}
            jsonl_file.write_text(json.dumps(entry) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["content"] == "Plain string response"

    def test_parse_tool_use_and_result(self):
        """Should parse tool use with grouped result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            tool_use = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-abc", "name": "Bash", "input": {"command": "echo hello"}}]}, "uuid": "tool-use-1", "timestamp": "2024-01-01T00:00:00Z"}
            tool_result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-abc", "content": "hello", "is_error": False}]}, "uuid": "tool-result-1", "timestamp": "2024-01-01T00:00:01Z"}
            jsonl_file.write_text(json.dumps(tool_use) + '\n' + json.dumps(tool_result) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["type"] == "tool_use"
                assert result[0]["toolName"] == "Bash"
                assert result[0]["toolResult"] == "hello"
                assert result[0]["toolStatus"] == "complete"

    def test_skip_queue_operations(self):
        """Should skip queue-operation entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [{"type": "queue-operation", "data": {}}, {"type": "user", "message": {"role": "user", "content": "Hello"}, "uuid": "1"}]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["content"] == "Hello"

    def test_skip_file_history_snapshots(self):
        """Should skip file-history-snapshot entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [{"type": "file-history-snapshot", "files": []}, {"type": "user", "message": {"role": "user", "content": "Hello"}, "uuid": "1"}]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1

    def test_skip_meta_messages(self):
        """Should skip messages with isMeta flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [{"type": "user", "message": {"role": "user", "content": "Meta"}, "uuid": "1", "isMeta": True}, {"type": "user", "message": {"role": "user", "content": "Normal"}, "uuid": "2"}]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["content"] == "Normal"

    def test_skip_sidechain_messages(self):
        """Should skip messages with isSidechain flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [{"type": "user", "message": {"role": "user", "content": "Sidechain"}, "uuid": "1", "isSidechain": True}, {"type": "user", "message": {"role": "user", "content": "Main"}, "uuid": "2"}]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["content"] == "Main"

    def test_parse_system_messages(self):
        """Should parse system messages with subtype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entry = {"type": "system", "subtype": "compact_boundary", "content": "System message content", "uuid": "sys-1", "timestamp": "2024-01-01T00:00:00Z", "compactMetadata": {"key": "value"}}
            jsonl_file.write_text(json.dumps(entry) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["role"] == "system"
                assert result[0]["type"] == "system"
                assert result[0]["subtype"] == "compact_boundary"

    def test_parse_local_command_output(self):
        """Should parse local command output from user messages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entry = {"type": "user", "message": {"role": "user", "content": "<local-command-stdout>Command output here</local-command-stdout>"}, "uuid": "cmd-1", "timestamp": "2024-01-01T00:00:00Z"}
            jsonl_file.write_text(json.dumps(entry) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["role"] == "system"
                assert result[0]["subtype"] == "local_command"
                assert result[0]["content"] == "Command output here"


class TestGetSessionCostFromJsonl:
    """Test cost extraction from JSONL."""

    def test_extract_cost_info(self):
        """Should extract token usage from assistant messages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}], "model": "claude-3-5-sonnet-20241022", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}}, "uuid": "msg-1"},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Goodbye"}], "model": "claude-3-5-sonnet-20241022", "usage": {"input_tokens": 200, "output_tokens": 75, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 15}}, "uuid": "msg-2"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_session_cost_from_jsonl(session_id, "/workspace")
                assert result["total_tokens_in"] == 300
                assert result["total_tokens_out"] == 125
                assert result["cache_creation_tokens"] == 20
                assert result["cache_read_tokens"] == 15
                assert result["model"] == "claude-3-5-sonnet-20241022"

    def test_returns_empty_for_missing_file(self):
        """Should return empty dict when file not found."""
        with patch('app.core.jsonl_parser.get_session_jsonl_path') as mock_path:
            mock_path.return_value = None
            result = jsonl_parser.get_session_cost_from_jsonl("nonexistent")
            assert result == {}


class TestListAvailableSessions:
    """Test session listing functionality."""

    def test_list_sessions_in_project_dir(self):
        """Should list all JSONL files in project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            (project_dir / "session-1.jsonl").write_text('{"test": 1}')
            (project_dir / "session-2.jsonl").write_text('{"test": 2}')
            (project_dir / "session-3.jsonl").write_text('{"test": 3}')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.list_available_sessions("/workspace")
                assert len(result) == 3
                session_ids = [s["sdk_session_id"] for s in result]
                assert "session-1" in session_ids
                assert "session-2" in session_ids
                assert "session-3" in session_ids

    def test_returns_empty_for_nonexistent_dir(self):
        """Should return empty list when project directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.list_available_sessions("/workspace")
                assert result == []

    def test_includes_file_metadata(self):
        """Should include path, modified_at, and size_bytes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_file = project_dir / "test-session.jsonl"
            session_file.write_text('{"data": "test content here"}')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.list_available_sessions("/workspace")
                assert len(result) == 1
                assert result[0]["sdk_session_id"] == "test-session"
                assert "path" in result[0]
                assert "modified_at" in result[0]
                assert "size_bytes" in result[0]
                assert result[0]["size_bytes"] > 0


class TestSubagentTaskToolResult:
    """Test Task tool result handling for subagents."""

    def test_task_tool_result_updates_subagent(self):
        """Should update subagent message when Task tool result arrives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "task-tool-abc", "name": "Task", "input": {"subagent_type": "research", "description": "Find info", "prompt": "Search"}}]}, "uuid": "task-use", "timestamp": "2024-01-01T00:00:00Z"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "task-tool-abc", "content": "Task completed successfully"}]}, "toolUseResult": {"agentId": "agent-xyz"}, "uuid": "task-result", "timestamp": "2024-01-01T00:00:01Z"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["type"] == "subagent"
                assert result[0]["content"] == "Task completed successfully"
                assert result[0]["agentStatus"] == "completed"

    def test_task_tool_result_with_error(self):
        """Should mark subagent as error when Task fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "task-fail", "name": "Task", "input": {"subagent_type": "code", "description": "Write code", "prompt": "Code"}}]}, "uuid": "task-use"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "task-fail", "content": "Task failed", "is_error": True}]}, "uuid": "task-result"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["agentStatus"] == "error"


class TestToolResultContentVariants:
    """Test various tool result content formats."""

    def test_tool_result_with_bash_stdout_stderr(self):
        """Should parse tool results with Bash-style toolUseResult."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "bash-tool", "name": "Bash", "input": {}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "bash-tool", "content": ""}]}, "toolUseResult": {"stdout": "output text", "stderr": "error text", "is_error": False}, "uuid": "result-1"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["toolResult"] == "output text\nerror text"

    def test_tool_result_with_file_content(self):
        """Should parse tool results with Read-style toolUseResult."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "read-tool", "name": "Read", "input": {}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-tool", "content": ""}]}, "toolUseResult": {"type": "text", "file": {"filePath": "/path/to/file.txt", "content": "File contents here"}}, "uuid": "result-1"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert "File: /path/to/file.txt" in result[0]["toolResult"]
                assert "File contents here" in result[0]["toolResult"]

    def test_tool_result_with_string_toolUseResult(self):
        """Should handle string toolUseResult (error messages)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "err-tool", "name": "Bash", "input": {}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "err-tool", "content": "", "is_error": True}]}, "toolUseResult": "Error message string", "uuid": "result-1"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["toolResult"] == "Error message string"
                assert result[0]["toolStatus"] == "error"

    def test_tool_result_with_content_list(self):
        """Should handle toolUseResult with content as list of blocks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Test", "input": {}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": ""}]}, "toolUseResult": {"content": [{"type": "text", "text": "List content extracted"}]}, "uuid": "result-1"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["toolResult"] == "List content extracted"

    def test_tool_result_with_result_key(self):
        """Should handle toolUseResult with result key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Test", "input": {}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": ""}]}, "toolUseResult": {"result": "Result value here"}, "uuid": "result-1"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["toolResult"] == "Result value here"
