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

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

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

    def test_windows_path_with_space(self):
        """Spaces are replaced with dashes to match the Claude CLI's encoding.

        A cwd like D:\\Projects\\new folder is stored by the CLI under the
        project dir D--Projects-new-folder (space -> dash). Preserving the
        space would point at a non-existent / ghost dir.
        """
        result = jsonl_parser.get_project_dir_name(r"D:\Projects\new folder")
        assert result == "D--Projects-new-folder"

    def test_posix_path_with_space(self):
        """Spaces in POSIX paths are also replaced with dashes."""
        result = jsonl_parser.get_project_dir_name("/workspace/new folder")
        assert result == "-workspace-new-folder"


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
    """Test system/meta content filtering.

    Note: The _is_system_content function was simplified to only check for
    empty/whitespace content. Slash commands are now parsed separately via
    _parse_slash_command(), and other system content is shown as-is.
    """

    def test_empty_content_is_system(self):
        """Empty content should be treated as system content."""
        assert jsonl_parser._is_system_content("") is True
        assert jsonl_parser._is_system_content(None) is True

    def test_whitespace_only_is_system(self):
        """Whitespace-only content should be treated as system content."""
        assert jsonl_parser._is_system_content("   ") is True
        assert jsonl_parser._is_system_content("\n\t") is True

    def test_command_tags_not_system(self):
        """Command tags are NOT filtered as system content - they're parsed separately."""
        # These are displayed as-is or parsed via _parse_slash_command
        assert jsonl_parser._is_system_content("<command-name>test</command-name>") is False
        assert jsonl_parser._is_system_content("<command-message>msg</command-message>") is False
        assert jsonl_parser._is_system_content("<command-args>args</command-args>") is False

    def test_interrupt_message_not_system(self):
        """Interrupt messages are NOT filtered - they are shown to user."""
        assert jsonl_parser._is_system_content("[Request interrupted by user]") is False

    def test_caveat_message_not_system(self):
        """Caveat messages are NOT filtered - they are shown to user."""
        assert jsonl_parser._is_system_content("Caveat: The messages below were generated...") is False

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

    def test_find_agent_files_in_nested_subagents_dir(self):
        """Should find agent files in the CLI 2.1.x nested layout:
        <project_dir>/<sid>/subagents/agent-*.jsonl."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            # Main transcript stays flat (project-dir resolution unchanged)
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            # Subagent transcript lives in nested <sid>/subagents/ dir
            subagents_dir = project_dir / session_id / "subagents"
            subagents_dir.mkdir(parents=True)
            agent_file = subagents_dir / "agent-ad54465c88efd6b1a.jsonl"
            agent_file.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "isSidechain": True,
                    "agentId": "ad54465c88efd6b1a",
                    "type": "user",
                }) + '\n'
            )
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert "ad54465c88efd6b1a" in result
                assert result["ad54465c88efd6b1a"] == agent_file

    def test_nested_agent_file_different_session_ignored(self):
        """Should ignore nested agent files whose sessionId mismatches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            subagents_dir = project_dir / session_id / "subagents"
            subagents_dir.mkdir(parents=True)
            agent_file = subagents_dir / "agent-nope.jsonl"
            agent_file.write_text(
                json.dumps({"sessionId": "different-session"}) + '\n'
            )
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert "nope" not in result

    def test_ghost_dir_falls_back_to_real_dir_for_nested_agent(self):
        """Regression (Fix B): when the COMPUTED project dir exists but is a
        "ghost" lacking the session's main file, resolution must still fall back
        to the REAL dir (containing <sid>.jsonl + nested subagent) and find the
        nested agent. The ghost dir merely existing must NOT short-circuit the
        fallback.

        Mirrors the real-world bug: working_dir 'D:\\Projects\\new folder'
        previously mapped to a ghost dir 'D--Projects-new folder' (space
        preserved) that get_hooks_jsonl_path had created but which holds no
        transcripts, while the CLI stored everything under the dash-encoded
        'D--Projects-new-folder'. Here we force the mismatch by mapping the
        working_dir to a ghost name and placing the session under a different
        ("real") dir so the test exercises Fix B independently of Fix A.
        """
        jsonl_parser._agent_paths_cache.clear()
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            session_id = "test-session-123"

            # GHOST dir: matches the name get_project_dir_name() computes for the
            # working_dir below, EXISTS, but has NO <sid>.jsonl (no transcripts).
            working_dir = "/ghost"
            ghost_name = jsonl_parser.get_project_dir_name(working_dir)
            ghost_dir = projects_dir / ghost_name
            ghost_dir.mkdir(parents=True)

            # REAL dir: a differently-named project dir that actually holds the
            # session's main file and the nested subagent transcript.
            real_dir = projects_dir / "-real-project"
            real_dir.mkdir(parents=True)
            session_file = real_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            subagents_dir = real_dir / session_id / "subagents"
            subagents_dir.mkdir(parents=True)
            agent_file = subagents_dir / "agent-ad54465c88efd6b1a.jsonl"
            agent_file.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "isSidechain": True,
                    "agentId": "ad54465c88efd6b1a",
                    "type": "user",
                }) + '\n'
            )

            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, working_dir)
                assert "ad54465c88efd6b1a" in result
                assert result["ad54465c88efd6b1a"] == agent_file

    def test_find_both_flat_and_nested_agent_files(self):
        """Should discover BOTH a flat agent-*.jsonl and a nested
        <sid>/subagents/agent-*.jsonl together."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session-123"
            session_file = project_dir / f"{session_id}.jsonl"
            session_file.write_text('{"test": true}\n')
            # Flat (legacy) layout
            flat_agent = project_dir / "agent-a.jsonl"
            flat_agent.write_text(json.dumps({"sessionId": session_id}) + '\n')
            # Nested (current) layout
            subagents_dir = project_dir / session_id / "subagents"
            subagents_dir.mkdir(parents=True)
            nested_agent = subagents_dir / "agent-b.jsonl"
            nested_agent.write_text(json.dumps({"sessionId": session_id}) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result = jsonl_parser.get_agent_jsonl_paths(session_id, "/workspace")
                assert result["a"] == flat_agent
                assert result["b"] == nested_agent
                assert len(result) == 2


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
            result, usage = jsonl_parser.parse_session_history("nonexistent-session")
            assert result == []
            assert usage == {}

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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["type"] == "tool_use"
                assert result[0]["toolName"] == "Bash"
                assert result[0]["toolResult"] == "hello"
                assert result[0]["toolStatus"] == "complete"
                # #1 stable id: on the MAIN session path the tool message id is
                # `tool-{tool_id}` (matching the live store), NOT
                # `tool-{uuid}-{tool_id}` — so the card keeps a stable id across
                # the streaming → persisted handoff and the frontend dedup works.
                assert result[0]["id"] == "tool-tool-abc"
                assert "tool-use-1" not in result[0]["id"], "id must not depend on the JSONL uuid"

    def test_parse_tool_result_reconstructs_screenshot(self):
        """An MCP image block in a tool_result is reconstructed as a
        toolScreenshot data URI so reloaded sessions show the screenshot
        below the tool call, exactly like the live store does."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            tool_use = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-shot", "name": "mcp__browser__browser_screenshot", "input": {}}]}, "uuid": "tool-use-1", "timestamp": "2024-01-01T00:00:00Z"}
            tool_result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-shot", "content": [
                {"type": "text", "text": "screenshot of page"},
                {"type": "image", "data": "iVBORscreenshot", "mimeType": "image/png"},
            ], "is_error": False}]}, "uuid": "tool-result-1", "timestamp": "2024-01-01T00:00:01Z"}
            jsonl_file.write_text(json.dumps(tool_use) + '\n' + json.dumps(tool_result) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["type"] == "tool_use"
                assert result[0]["toolScreenshot"] == "data:image/png;base64,iVBORscreenshot"
                # Text is still extracted for the result body.
                assert "screenshot of page" in result[0]["toolResult"]

    def test_parse_tool_result_without_image_has_no_screenshot(self):
        """Plain tool results must not carry a toolScreenshot key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            tool_use = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-abc", "name": "Bash", "input": {}}]}, "uuid": "tool-use-1", "timestamp": "2024-01-01T00:00:00Z"}
            tool_result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-abc", "content": "ok", "is_error": False}]}, "uuid": "tool-result-1", "timestamp": "2024-01-01T00:00:01Z"}
            jsonl_file.write_text(json.dumps(tool_use) + '\n' + json.dumps(tool_result) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert "toolScreenshot" not in result[0]

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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["agentStatus"] == "error"


    def test_agent_tool_name_creates_subagent(self):
        """Should create subagent message for 'Agent' tool name (SDK >=0.1.48)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "agent-tool-xyz", "name": "Agent", "input": {"subagent_type": "Explore", "description": "Search codebase", "prompt": "Find files"}}]}, "uuid": "agent-use", "timestamp": "2024-01-01T00:00:00Z"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "agent-tool-xyz", "content": "Agent completed successfully"}]}, "toolUseResult": {"agentId": "agent-abc"}, "uuid": "agent-result", "timestamp": "2024-01-01T00:00:01Z"}
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert len(result) == 1
                assert result[0]["type"] == "subagent"
                assert result[0]["content"] == "Agent completed successfully"
                assert result[0]["agentStatus"] == "completed"
                assert result[0]["agentType"] == "Explore"


class TestBackgroundedSubagentStatus:
    """A backgrounded sub-agent's launch-ACK must NOT mark it complete on reload.

    Regression for the premature-✓ bug: in postgres_only mode the reload path
    rebuilds sub-agents via ``_transform_entries``; a run_in_background sub-agent's
    first tool_result is only the launch-ACK while it keeps running, so the card
    must stay 'running' until its terminal <task-notification> arrives.
    """

    @staticmethod
    def _parse(entries):
        msgs, _usage = jsonl_parser._transform_entries(entries, "sdk-sess", "/workspace")
        return [m for m in msgs if m.get("type") == "subagent"]

    @staticmethod
    def _task_use(tool_id="task-bg-1", run_in_background=True):
        inp = {"subagent_type": "general-purpose", "description": "Map WS events", "prompt": "map"}
        if run_in_background:
            inp["run_in_background"] = True
        return {"type": "assistant", "uuid": f"use-{tool_id}", "message": {"role": "assistant",
                "content": [{"type": "tool_use", "id": tool_id, "name": "Task", "input": inp}]}}

    @staticmethod
    def _launch_ack(tool_id="task-bg-1",
                    text=("Agent launched successfully and is running in the background "
                          "with ID: agent_x. You will be notified automatically when it finishes.")):
        return {"type": "user", "uuid": f"ack-{tool_id}",
                "toolUseResult": {"agentId": f"agent-{tool_id}"},
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}

    @staticmethod
    def _notification(tool_id="task-bg-1", status="completed"):
        body = (f"<task-notification>\n<task-id>bg</task-id>\n"
                f"<tool-use-id>{tool_id}</tool-use-id>\n<status>{status}</status>\n"
                f"<summary>x</summary>\n</task-notification>")
        return {"type": "user", "uuid": f"notif-{tool_id}-{status}",
                "message": {"role": "user", "content": body}}

    def test_launch_ack_keeps_running_via_flag(self):
        """run_in_background launch-ACK, no notification yet → 'running', not premature ✓."""
        subs = self._parse([self._task_use(), self._launch_ack()])
        assert len(subs) == 1
        assert subs[0]["agentStatus"] == "running"

    def test_launch_ack_keeps_running_via_content_sniff(self):
        """ACK text alone (run_in_background flag not recorded) → still 'running'."""
        subs = self._parse([self._task_use(run_in_background=False), self._launch_ack()])
        assert subs[0]["agentStatus"] == "running"

    def test_task_notification_flips_to_completed(self):
        """Terminal task-notification (completed) flips the card to 'completed' on reload."""
        subs = self._parse([self._task_use(), self._launch_ack(),
                             self._notification(status="completed")])
        assert subs[0]["agentStatus"] == "completed"

    def test_task_notification_failed_marks_error(self):
        """Terminal task-notification (failed) → 'error'."""
        subs = self._parse([self._task_use(), self._launch_ack(),
                             self._notification(status="failed")])
        assert subs[0]["agentStatus"] == "error"

    def test_foreground_subagent_still_completes(self):
        """Regression guard: a NON-backgrounded sub-agent's real result still completes."""
        subs = self._parse([
            self._task_use(run_in_background=False),
            {"type": "user", "uuid": "fg-res",
             "toolUseResult": {"agentId": "agent-fg"},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "task-bg-1",
                  "content": "Here is the full mapping report with no ack phrases."}]}},
        ])
        assert subs[0]["agentStatus"] == "completed"

    def test_dispatch_rejection_marks_error(self):
        """A non-is_error ok:false dispatcher rejection is terminal → 'error'.

        The launch never spawned a worker, so no <task-notification> follows —
        without this the card replays 'completed' (or spins forever live).
        """
        subs = self._parse([
            self._task_use(run_in_background=False),
            {"type": "user", "uuid": "rej-res",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "task-bg-1",
                  "content": '{"ok": false, "error": "Model \'sonnet\' is not curated."}'}]}},
        ])
        assert subs[0]["agentStatus"] == "error"


class TestTaskNotificationEnrichment:
    """`_apply_task_notification_status` recovers usage + result on reload.

    A backgrounded sub-agent that finished while the parent was PARKED persists
    its terminal notification as a <task-notification> user message. On reload the
    card must show its status, token/tool/duration stats AND the result text —
    not a bare checkmark.
    """

    @staticmethod
    def _msgs(tool_id="task-1", content="", type_="subagent"):
        messages = [{
            "type": type_, "toolId": tool_id, "content": content,
            "agentStatus": "running",
        }]
        return messages, {tool_id: 0}

    @staticmethod
    def _body(tool_id="task-1", status="completed", summary="short summary",
              result="FULL RESULT TEXT", tokens="1234", tool_uses="5",
              duration="6789", include_usage=True, include_result=True):
        usage = ""
        if include_usage:
            usage = (f"  <usage>\n"
                     f"    <subagent_tokens>{tokens}</subagent_tokens>\n"
                     f"    <tool_uses>{tool_uses}</tool_uses>\n"
                     f"    <duration_ms>{duration}</duration_ms>\n"
                     f"  </usage>\n")
        result_xml = f"  <result>{result}</result>\n" if include_result else ""
        return (f"<task-notification>\n"
                f"  <tool-use-id>{tool_id}</tool-use-id>\n"
                f"  <status>{status}</status>\n"
                f"  <summary>{summary}</summary>\n"
                f"{result_xml}"
                f"{usage}"
                f"</task-notification>")

    def test_usage_fields_populate_agent_usage(self):
        messages, index = self._msgs()
        jsonl_parser._apply_task_notification_status(self._body(), messages, index)
        assert messages[0]["agentUsage"] == {
            "total_tokens": 1234, "tool_uses": 5, "duration_ms": 6789,
        }

    def test_result_sets_content_truncated_to_2000(self):
        messages, index = self._msgs()
        big = "R" * 5000
        jsonl_parser._apply_task_notification_status(
            self._body(result=big), messages, index)
        assert messages[0]["content"] == "R" * 2000

    def test_summary_fallback_when_no_result(self):
        messages, index = self._msgs()
        jsonl_parser._apply_task_notification_status(
            self._body(include_result=False, summary="the summary"), messages, index)
        assert messages[0]["content"] == "the summary"

    def test_missing_usage_leaves_no_agent_usage_key_and_content_untouched(self):
        messages, index = self._msgs(content="")
        body = self._body(include_usage=False, include_result=False, summary="")
        jsonl_parser._apply_task_notification_status(body, messages, index)
        assert "agentUsage" not in messages[0]
        # No result and empty summary → content stays as it was.
        assert messages[0]["content"] == ""

    def test_blank_usage_values_omitted(self):
        messages, index = self._msgs()
        body = self._body(tokens="", tool_uses="", duration="")
        jsonl_parser._apply_task_notification_status(body, messages, index)
        assert "agentUsage" not in messages[0]

    def test_error_status_mapping_unchanged(self):
        for status in ("failed", "stopped", "error"):
            messages, index = self._msgs()
            jsonl_parser._apply_task_notification_status(
                self._body(status=status), messages, index)
            assert messages[0]["agentStatus"] == "error"
        messages, index = self._msgs()
        jsonl_parser._apply_task_notification_status(
            self._body(status="completed"), messages, index)
        assert messages[0]["agentStatus"] == "completed"

    def test_non_subagent_target_untouched(self):
        messages, index = self._msgs(type_="tool_use")
        jsonl_parser._apply_task_notification_status(self._body(), messages, index)
        assert "agentUsage" not in messages[0]
        assert messages[0]["content"] == ""


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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
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
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                assert result[0]["toolResult"] == "Result value here"


class TestBackgroundBashStatus:
    """Backgrounded Bash must stay 'running' on reload, not flip to 'complete'."""

    def test_helper_input_flag(self):
        assert jsonl_parser._is_background_ack_result(
            {"toolName": "Bash", "toolInput": {"run_in_background": True}},
            "Command running in the background with ID: bash_1",
        ) is True

    def test_helper_ack_text_only(self):
        assert jsonl_parser._is_background_ack_result(
            {"toolName": "Bash", "toolInput": {}},
            "Command running in the background with ID: bash_2",
        ) is True

    def test_helper_foreground_completes(self):
        assert jsonl_parser._is_background_ack_result(
            {"toolName": "Bash", "toolInput": {}}, "just normal output"
        ) is False

    def test_helper_non_bash_ignored(self):
        # Even a Read with background-ish text must not be held back.
        assert jsonl_parser._is_background_ack_result(
            {"toolName": "Read", "toolInput": {"run_in_background": True}},
            "running in the background",
        ) is False

    def test_end_to_end_background_bash_stays_running(self):
        """Full parse: a backgrounded Bash whose result is the ack stays running."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "bg-tool", "name": "Bash", "input": {"command": "sleep 100", "run_in_background": True}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "bg-tool", "content": "Command running in the background with ID: bash_5"}]}, "uuid": "result-1"},
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                tool_msg = next(m for m in result if m.get("type") == "tool_use")
                assert tool_msg["toolStatus"] == "running"

    def test_end_to_end_foreground_bash_completes(self):
        """Full parse: a normal foreground Bash still completes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)
            session_id = "test-session"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            entries = [
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "fg-tool", "name": "Bash", "input": {"command": "echo hi"}}]}, "uuid": "use-1"},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "fg-tool", "content": "hi"}]}, "uuid": "result-1"},
            ]
            jsonl_file.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
            with patch('app.core.jsonl_parser.settings') as mock_settings:
                mock_settings.get_claude_projects_dir = projects_dir
                result, _usage = jsonl_parser.parse_session_history(session_id, "/workspace")
                tool_msg = next(m for m in result if m.get("type") == "tool_use")
                assert tool_msg["toolStatus"] == "complete"


class TestAgentChildScreenshots:
    """Subagent transcript image results attach toolScreenshot to the child."""

    IMAGE_BLOCK = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="},
    }

    def test_child_tool_result_image_sets_tool_screenshot(self):
        entries = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "shot-1",
                 "name": "mcp__browser__browser_screenshot", "input": {}}]},
             "uuid": "u1"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "shot-1", "content": [
                    self.IMAGE_BLOCK,
                    {"type": "text", "text": "1280x720"}]}]},
             "uuid": "u2"},
        ]
        children = jsonl_parser.transform_agent_entries(entries)
        child = next(c for c in children if c.get("toolId") == "shot-1")
        assert child["toolScreenshot"] == "data:image/jpeg;base64,aGVsbG8="
        assert child["toolStatus"] == "complete"
        assert child["toolResult"] == "1280x720"

    def test_child_tool_result_without_image_has_no_screenshot(self):
        entries = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
             "uuid": "u1"},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]},
             "uuid": "u2"},
        ]
        children = jsonl_parser.transform_agent_entries(entries)
        child = next(c for c in children if c.get("toolId") == "t1")
        assert "toolScreenshot" not in child

    def test_unmatched_tool_result_image_sets_screenshot_on_fallback_child(self):
        entries = [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "orphan-1", "content": [
                    self.IMAGE_BLOCK]}]},
             "uuid": "u1"},
        ]
        children = jsonl_parser.transform_agent_entries(entries)
        child = next(c for c in children if c.get("toolId") == "orphan-1")
        assert child["type"] == "tool_result"
        assert child["toolScreenshot"] == "data:image/jpeg;base64,aGVsbG8="
