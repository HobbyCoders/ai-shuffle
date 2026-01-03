"""
Unit tests for JSONL Rewind Service.

Tests cover:
- Checkpoint and RewindResult dataclasses
- JSONL file path resolution
- JSONL entry parsing (including malformed JSON)
- Message text extraction
- Checkpoint extraction from session history
- Truncation operations (with/without response retention)
- Last message UUID retrieval
- JSONL backup creation
- Edge cases: empty files, missing files, malformed JSON
"""

import pytest
import json
import tempfile
import os
import shutil
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.core.jsonl_rewind import (
    Checkpoint,
    RewindResult,
    JSONLRewindService,
    jsonl_rewind_service,
)


# =============================================================================
# Checkpoint Dataclass Tests
# =============================================================================

class TestCheckpoint:
    """Test Checkpoint dataclass functionality."""

    def test_checkpoint_creation(self):
        """Should create checkpoint with required fields."""
        checkpoint = Checkpoint(
            uuid="test-uuid-123",
            index=0,
            message_preview="Hello world...",
            full_message="Hello world, this is a test message"
        )

        assert checkpoint.uuid == "test-uuid-123"
        assert checkpoint.index == 0
        assert checkpoint.message_preview == "Hello world..."
        assert checkpoint.full_message == "Hello world, this is a test message"
        assert checkpoint.timestamp is None
        assert checkpoint.git_ref is None
        assert checkpoint.created_at is not None

    def test_checkpoint_with_optional_fields(self):
        """Should create checkpoint with optional fields."""
        checkpoint = Checkpoint(
            uuid="test-uuid-456",
            index=1,
            message_preview="Test preview",
            full_message="Test full message",
            timestamp="2024-01-01T00:00:00Z",
            git_ref="abc123"
        )

        assert checkpoint.timestamp == "2024-01-01T00:00:00Z"
        assert checkpoint.git_ref == "abc123"

    def test_checkpoint_to_dict(self):
        """Should convert checkpoint to dictionary."""
        checkpoint = Checkpoint(
            uuid="test-uuid",
            index=2,
            message_preview="Preview",
            full_message="Full message",
            timestamp="2024-01-01T00:00:00Z"
        )

        result = checkpoint.to_dict()

        assert isinstance(result, dict)
        assert result["uuid"] == "test-uuid"
        assert result["index"] == 2
        assert result["message_preview"] == "Preview"
        assert result["full_message"] == "Full message"
        assert result["timestamp"] == "2024-01-01T00:00:00Z"
        assert "created_at" in result


# =============================================================================
# RewindResult Dataclass Tests
# =============================================================================

class TestRewindResult:
    """Test RewindResult dataclass functionality."""

    def test_success_result(self):
        """Should create successful result."""
        result = RewindResult(
            success=True,
            message="Operation completed successfully",
            checkpoint_uuid="checkpoint-123",
            messages_removed=5
        )

        assert result.success is True
        assert result.message == "Operation completed successfully"
        assert result.checkpoint_uuid == "checkpoint-123"
        assert result.messages_removed == 5
        assert result.error is None

    def test_failure_result(self):
        """Should create failure result with error."""
        result = RewindResult(
            success=False,
            message="Operation failed",
            error="File not found"
        )

        assert result.success is False
        assert result.error == "File not found"
        assert result.checkpoint_uuid is None
        assert result.messages_removed == 0

    def test_rewind_result_to_dict(self):
        """Should convert result to dictionary."""
        result = RewindResult(
            success=True,
            message="Done",
            checkpoint_uuid="uuid-123",
            messages_removed=3
        )

        result_dict = result.to_dict()

        assert isinstance(result_dict, dict)
        assert result_dict["success"] is True
        assert result_dict["message"] == "Done"
        assert result_dict["checkpoint_uuid"] == "uuid-123"
        assert result_dict["messages_removed"] == 3


# =============================================================================
# JSONLRewindService Tests
# =============================================================================

class TestJSONLRewindServiceGetJsonlPath:
    """Test JSONL path resolution."""

    def test_get_jsonl_path_found_in_expected_location(self):
        """Should find JSONL in expected project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            project_dir = projects_dir / "-workspace"
            project_dir.mkdir(parents=True)

            session_id = "test-session-123"
            jsonl_file = project_dir / f"{session_id}.jsonl"
            jsonl_file.write_text('{"test": true}')

            service = JSONLRewindService()

            with patch.object(service, 'claude_projects_dir', projects_dir):
                result = service._get_jsonl_path(session_id, "/workspace")

                assert result is not None
                assert result == jsonl_file

    def test_get_jsonl_path_not_found(self):
        """Should return None when JSONL doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir(parents=True)

            service = JSONLRewindService()

            with patch.object(service, 'claude_projects_dir', projects_dir):
                result = service._get_jsonl_path("nonexistent-session", "/workspace")

                assert result is None

    def test_get_jsonl_path_search_fallback(self):
        """Should search all project directories as fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"

            # Create JSONL in different project directory
            other_project = projects_dir / "-other-project"
            other_project.mkdir(parents=True)

            session_id = "wandering-session"
            jsonl_file = other_project / f"{session_id}.jsonl"
            jsonl_file.write_text('{"test": true}')

            service = JSONLRewindService()

            with patch.object(service, 'claude_projects_dir', projects_dir):
                result = service._get_jsonl_path(session_id, "/workspace")

                assert result is not None
                assert result == jsonl_file

    def test_get_jsonl_path_projects_dir_not_exists(self):
        """Should return None if projects directory doesn't exist."""
        service = JSONLRewindService()

        with patch.object(service, 'claude_projects_dir', Path("/nonexistent/path")):
            result = service._get_jsonl_path("any-session", "/workspace")

            assert result is None


class TestJSONLRewindServiceParseEntries:
    """Test JSONL entry parsing."""

    def test_parse_valid_entries(self):
        """Should parse valid JSONL entries."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"uuid": "1", "type": "user", "message": {"role": "user", "content": "Hello"}}\n')
            f.write('{"uuid": "2", "type": "assistant", "message": {"role": "assistant", "content": "Hi"}}\n')
            f.flush()

            service = JSONLRewindService()
            entries = service._parse_jsonl_entries(Path(f.name))

            assert len(entries) == 2
            assert entries[0]["uuid"] == "1"
            assert entries[1]["uuid"] == "2"
            assert entries[0]["_line_num"] == 1
            assert entries[1]["_line_num"] == 2
            assert "_raw_line" in entries[0]

        os.unlink(f.name)

    def test_parse_empty_file(self):
        """Should handle empty file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.flush()

            service = JSONLRewindService()
            entries = service._parse_jsonl_entries(Path(f.name))

            assert entries == []

        os.unlink(f.name)

    def test_parse_file_with_empty_lines(self):
        """Should skip empty lines."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"uuid": "1"}\n')
            f.write('\n')
            f.write('   \n')
            f.write('{"uuid": "2"}\n')
            f.flush()

            service = JSONLRewindService()
            entries = service._parse_jsonl_entries(Path(f.name))

            assert len(entries) == 2

        os.unlink(f.name)

    def test_parse_malformed_json_preserves_raw(self):
        """Should preserve malformed JSON lines with error marker."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"uuid": "1"}\n')
            f.write('not valid json\n')
            f.write('{"uuid": "2"}\n')
            f.flush()

            service = JSONLRewindService()
            entries = service._parse_jsonl_entries(Path(f.name))

            assert len(entries) == 3
            assert entries[0]["uuid"] == "1"
            assert "_parse_error" in entries[1]
            assert entries[1]["_raw_line"] == "not valid json"
            assert entries[2]["uuid"] == "2"

        os.unlink(f.name)

    def test_parse_nonexistent_file(self):
        """Should handle nonexistent file gracefully."""
        service = JSONLRewindService()
        entries = service._parse_jsonl_entries(Path("/nonexistent/file.jsonl"))

        assert entries == []


class TestJSONLRewindServiceExtractMessageText:
    """Test message text extraction."""

    def test_extract_string_content(self):
        """Should extract string content directly."""
        service = JSONLRewindService()
        entry = {"message": {"content": "Hello, world!"}}

        result = service._extract_message_text(entry)

        assert result == "Hello, world!"

    def test_extract_from_text_blocks(self):
        """Should extract text from text blocks array."""
        service = JSONLRewindService()
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Hello, "},
                    {"type": "text", "text": "world!"}
                ]
            }
        }

        result = service._extract_message_text(entry)

        assert result == "Hello,  world!"

    def test_extract_from_mixed_blocks(self):
        """Should extract only text from mixed content blocks."""
        service = JSONLRewindService()
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Start"},
                    {"type": "image", "source": "..."},
                    {"type": "text", "text": "End"}
                ]
            }
        }

        result = service._extract_message_text(entry)

        assert result == "Start End"

    def test_extract_empty_message(self):
        """Should return empty string for empty message."""
        service = JSONLRewindService()

        assert service._extract_message_text({}) == ""
        assert service._extract_message_text({"message": {}}) == ""
        assert service._extract_message_text({"message": {"content": ""}}) == ""

    def test_extract_non_string_non_list_content(self):
        """Should return empty string for unexpected content types."""
        service = JSONLRewindService()
        entry = {"message": {"content": 12345}}

        result = service._extract_message_text(entry)

        assert result == ""


class TestJSONLRewindServiceGetCheckpoints:
    """Test checkpoint extraction from JSONL."""

    def test_get_checkpoints_empty_session(self):
        """Should return empty list for nonexistent session."""
        service = JSONLRewindService()

        with patch.object(service, '_get_jsonl_path', return_value=None):
            result = service.get_checkpoints("nonexistent-session")

            assert result == []

    def test_get_checkpoints_basic(self):
        """Should extract user message checkpoints."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            # User message (should be checkpoint)
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "First message"}
            }) + '\n')
            # Assistant message (not a checkpoint)
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response"}
            }) + '\n')
            # Another user message
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Second message"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 2
                assert checkpoints[0].uuid == "user-1"
                assert checkpoints[0].index == 0
                assert checkpoints[0].full_message == "First message"
                assert checkpoints[1].uuid == "user-2"
                assert checkpoints[1].index == 1

        os.unlink(f.name)

    def test_get_checkpoints_skips_meta_messages(self):
        """Should skip meta messages (slash commands, system prompts)."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "meta-1",
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "/compact"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Real message"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 1
                assert checkpoints[0].uuid == "user-1"

        os.unlink(f.name)

    def test_get_checkpoints_skips_sidechain_messages(self):
        """Should skip sidechain messages (alternate branches)."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "side-1",
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "Sidechain message"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Main message"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 1
                assert checkpoints[0].uuid == "user-1"

        os.unlink(f.name)

    def test_get_checkpoints_skips_tool_results(self):
        """Should skip user messages that contain tool results."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            # Tool result message (should be skipped)
            f.write(json.dumps({
                "uuid": "tool-result-1",
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-123", "content": "Result"}
                    ]
                }
            }) + '\n')
            # Regular user message
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Normal message"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 1
                assert checkpoints[0].uuid == "user-1"

        os.unlink(f.name)

    def test_get_checkpoints_skips_xml_content(self):
        """Should skip messages starting with < (XML/system content)."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "xml-1",
                "type": "user",
                "message": {"role": "user", "content": "<command-name>test</command-name>"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Normal message"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 1
                assert checkpoints[0].uuid == "user-1"

        os.unlink(f.name)

    def test_get_checkpoints_message_preview_truncation(self):
        """Should truncate message preview to 100 chars."""
        long_message = "x" * 150

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": long_message}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 1
                assert len(checkpoints[0].message_preview) == 103  # 100 + "..."
                assert checkpoints[0].message_preview.endswith("...")
                assert checkpoints[0].full_message == long_message

        os.unlink(f.name)


class TestJSONLRewindServiceTruncateToCheckpoint:
    """Test JSONL truncation operations."""

    def test_truncate_file_not_found(self):
        """Should return error when JSONL file not found."""
        service = JSONLRewindService()

        with patch.object(service, '_get_jsonl_path', return_value=None):
            result = service.truncate_to_checkpoint("session-id", "target-uuid")

            assert result.success is False
            assert "not found" in result.message.lower()

    def test_truncate_empty_file(self):
        """Should return error for empty JSONL file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                result = service.truncate_to_checkpoint("session-id", "target-uuid")

                assert result.success is False
                assert "empty" in result.message.lower()

        os.unlink(f.name)

    def test_truncate_target_not_found(self):
        """Should return error when target UUID not found."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({"uuid": "other-uuid", "type": "user"}) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                result = service.truncate_to_checkpoint("session-id", "nonexistent-uuid")

                assert result.success is False
                assert "not found" in result.message.lower()

        os.unlink(f.name)

    def test_truncate_include_response_last_message(self):
        """Should report already at checkpoint when target is last user message."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Message 1"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response 1"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=True
                )

                assert result.success is True
                assert result.messages_removed == 0
                assert "nothing to rewind" in result.message.lower() or "already at" in result.message.lower()

        os.unlink(f.name)

    def test_truncate_include_response_success(self):
        """Should truncate keeping the assistant response."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Message 1"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response 1"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Message 2"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-2",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response 2"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=True
                )

                assert result.success is True
                assert result.messages_removed == 2  # user-2 and asst-2

                # Verify file contents
                with open(jsonl_path, 'r') as check_file:
                    lines = check_file.readlines()
                    assert len(lines) == 2

        os.unlink(f.name)

    def test_truncate_exclude_response_success(self):
        """Should truncate before the target user message."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Message 1"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response 1"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Message 2"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                result = service.truncate_to_checkpoint(
                    "session-id", "user-2", include_response=False
                )

                assert result.success is True
                assert result.messages_removed == 1  # Only user-2 removed

                # Verify file contents
                with open(jsonl_path, 'r') as check_file:
                    lines = check_file.readlines()
                    assert len(lines) == 2

        os.unlink(f.name)

    def test_truncate_preserves_raw_line_format(self):
        """Should preserve exact JSON formatting from original file."""
        original_line = '{"uuid":"user-1","type":"user","message":{"role":"user","content":"Test"}}'
        asst_line = '{"uuid":"asst-1","type":"assistant","message":{"role":"assistant","content":"Response"}}'
        user2_line = '{"uuid":"user-2","type":"user","message":{"role":"user","content":"Remove"}}'

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(original_line + '\n')
            f.write(asst_line + '\n')
            f.write(user2_line + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                # include_response=True keeps user-1 and asst-1, removes user-2
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=True
                )

                assert result.success is True
                assert result.messages_removed == 1

                # Read back and verify format preserved
                with open(jsonl_path, 'r') as check_file:
                    lines = check_file.readlines()
                    assert len(lines) == 2
                    # Verify exact line format is preserved
                    assert lines[0].strip() == original_line
                    assert lines[1].strip() == asst_line

        os.unlink(f.name)

    def test_truncate_skips_tool_results_in_search(self):
        """Should skip tool results when finding next user message."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Message 1"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "Test"}
                ]}
            }) + '\n')
            # Tool result (should be skipped when finding next user message)
            f.write(json.dumps({
                "uuid": "tool-result-1",
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "OK"}]
                }
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Message 2"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=True
                )

                assert result.success is True
                # Should keep user-1, asst-1, tool-result-1 and remove user-2
                assert result.messages_removed == 1

        os.unlink(f.name)

    def test_truncate_nothing_to_remove(self):
        """Should report already at checkpoint when no messages to remove (include_response=True)."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            # Only user message and response - no next user message, so nothing to truncate
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Only message"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                # With include_response=True and no next user message, nothing is removed
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=True
                )

                assert result.success is True
                assert result.messages_removed == 0
                assert "nothing to rewind" in result.message.lower() or "already at" in result.message.lower()

        os.unlink(f.name)

    def test_truncate_exclude_response_first_message(self):
        """Should truncate to empty when targeting first message with include_response=False."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "First message"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                # include_response=False means truncate BEFORE the target
                # So targeting user-1 (index 0) with include_response=False removes everything
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=False
                )

                assert result.success is True
                assert result.messages_removed == 1

                # File should now be empty
                with open(jsonl_path, 'r') as check_file:
                    content = check_file.read().strip()
                    assert content == ""

        os.unlink(f.name)


class TestJSONLRewindServiceGetLastMessageUuid:
    """Test last message UUID retrieval."""

    def test_get_last_message_uuid_success(self):
        """Should return UUID of last user message."""
        service = JSONLRewindService()

        checkpoints = [
            Checkpoint(uuid="first", index=0, message_preview="", full_message=""),
            Checkpoint(uuid="second", index=1, message_preview="", full_message=""),
            Checkpoint(uuid="last", index=2, message_preview="", full_message=""),
        ]

        with patch.object(service, 'get_checkpoints', return_value=checkpoints):
            result = service.get_last_message_uuid("session-id")

            assert result == "last"

    def test_get_last_message_uuid_no_checkpoints(self):
        """Should return None when no checkpoints exist."""
        service = JSONLRewindService()

        with patch.object(service, 'get_checkpoints', return_value=[]):
            result = service.get_last_message_uuid("session-id")

            assert result is None


class TestJSONLRewindServiceBackup:
    """Test JSONL backup functionality."""

    def test_backup_jsonl_success(self):
        """Should create backup file successfully."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"uuid": "test"}\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                backup_path = service.backup_jsonl("session-id")

                assert backup_path is not None
                assert backup_path.exists()
                assert ".backup." in str(backup_path)

                # Verify backup content matches original
                with open(backup_path, 'r') as backup_file:
                    content = backup_file.read()
                    assert '{"uuid": "test"}' in content

                # Cleanup
                backup_path.unlink()

        os.unlink(f.name)

    def test_backup_jsonl_file_not_found(self):
        """Should return None when JSONL file not found."""
        service = JSONLRewindService()

        with patch.object(service, '_get_jsonl_path', return_value=None):
            result = service.backup_jsonl("nonexistent-session")

            assert result is None

    def test_backup_jsonl_failure(self):
        """Should return None on backup failure."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"uuid": "test"}\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                with patch('shutil.copy2', side_effect=PermissionError("No permission")):
                    result = service.backup_jsonl("session-id")

                    assert result is None

        os.unlink(f.name)


class TestJSONLRewindServiceEdgeCases:
    """Test edge cases and error handling."""

    def test_large_file_handling(self):
        """Should handle files with many entries."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            # Write 1000 entries
            for i in range(1000):
                if i % 2 == 0:
                    f.write(json.dumps({
                        "uuid": f"user-{i}",
                        "type": "user",
                        "message": {"role": "user", "content": f"Message {i}"}
                    }) + '\n')
                else:
                    f.write(json.dumps({
                        "uuid": f"asst-{i}",
                        "type": "assistant",
                        "message": {"role": "assistant", "content": f"Response {i}"}
                    }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("session-id")

                # Should have 500 user message checkpoints
                assert len(checkpoints) == 500

        os.unlink(f.name)

    def test_unicode_content_handling(self):
        """Should handle unicode content in messages."""
        unicode_message = "Hello! Bonjour! Hola!"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8') as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": unicode_message}
            }, ensure_ascii=False) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("session-id")

                assert len(checkpoints) == 1
                assert checkpoints[0].full_message == unicode_message

        os.unlink(f.name)

    def test_truncate_write_failure_cleanup(self):
        """Should clean up temp file on write failure."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Test"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Remove"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                # Make shutil.move fail
                with patch('shutil.move', side_effect=PermissionError("No permission")):
                    result = service.truncate_to_checkpoint(
                        "session-id", "user-1", include_response=False
                    )

                    assert result.success is False
                    assert result.error is not None

        os.unlink(f.name)

    def test_content_with_text_blocks_array(self):
        """Should handle content as array of text blocks."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part 1 "},
                        {"type": "text", "text": "Part 2"}
                    ]
                }
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("session-id")

                assert len(checkpoints) == 1
                assert "Part 1" in checkpoints[0].full_message
                assert "Part 2" in checkpoints[0].full_message

        os.unlink(f.name)

    def test_get_checkpoints_skips_non_message_entries(self):
        """Should skip queue-operation and file-history-snapshot entries."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            # Queue operation (should be skipped)
            f.write(json.dumps({
                "uuid": "queue-1",
                "type": "queue-operation",
                "operation": "some-op"
            }) + '\n')
            # File history snapshot (should be skipped)
            f.write(json.dumps({
                "uuid": "snapshot-1",
                "type": "file-history-snapshot",
                "files": []
            }) + '\n')
            # Regular user message (should be included)
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Hello"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()

            with patch.object(service, '_get_jsonl_path', return_value=Path(f.name)):
                checkpoints = service.get_checkpoints("test-session")

                assert len(checkpoints) == 1
                assert checkpoints[0].uuid == "user-1"

        os.unlink(f.name)

    def test_truncate_exclude_response_nothing_to_remove(self):
        """Should return already at checkpoint when include_response=False removes nothing."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            # User message at index 0 preceded by system entry
            f.write(json.dumps({
                "uuid": "system-1",
                "type": "queue-operation"
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Test"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                # Targeting user-1 at index 1, include_response=False
                # Should keep entries[:1] = only system-1
                result = service.truncate_to_checkpoint(
                    "session-id", "user-1", include_response=False
                )

                assert result.success is True
                assert result.messages_removed == 1

        os.unlink(f.name)

    def test_truncate_fallback_serialization(self):
        """Should use fallback serialization when raw_line is missing."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "Test"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Remove"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            # Mock _parse_jsonl_entries to return entries without _raw_line
            original_parse = service._parse_jsonl_entries

            def mock_parse(path):
                entries = original_parse(path)
                # Remove _raw_line from entries to trigger fallback
                for entry in entries:
                    if "_raw_line" in entry:
                        del entry["_raw_line"]
                return entries

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                with patch.object(service, '_parse_jsonl_entries', side_effect=mock_parse):
                    result = service.truncate_to_checkpoint(
                        "session-id", "user-1", include_response=False
                    )

                    assert result.success is True

                    # Verify file was written (even without raw_line)
                    with open(jsonl_path, 'r') as check_file:
                        content = check_file.read().strip()
                        # Should be empty since we truncated before user-1 (index 0)
                        assert content == ""

        os.unlink(f.name)

    def test_truncate_fallback_serialization_with_content(self):
        """Should use fallback serialization and preserve content correctly."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps({
                "uuid": "user-1",
                "type": "user",
                "message": {"role": "user", "content": "First"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "asst-1",
                "type": "assistant",
                "message": {"role": "assistant", "content": "Response"}
            }) + '\n')
            f.write(json.dumps({
                "uuid": "user-2",
                "type": "user",
                "message": {"role": "user", "content": "Second"}
            }) + '\n')
            f.flush()

            service = JSONLRewindService()
            jsonl_path = Path(f.name)

            # Mock _parse_jsonl_entries to return entries without _raw_line
            original_parse = service._parse_jsonl_entries

            def mock_parse(path):
                entries = original_parse(path)
                # Remove _raw_line from entries to trigger fallback
                for entry in entries:
                    if "_raw_line" in entry:
                        del entry["_raw_line"]
                return entries

            with patch.object(service, '_get_jsonl_path', return_value=jsonl_path):
                with patch.object(service, '_parse_jsonl_entries', side_effect=mock_parse):
                    result = service.truncate_to_checkpoint(
                        "session-id", "user-1", include_response=True
                    )

                    assert result.success is True
                    assert result.messages_removed == 1

                    # Verify file was written with serialized JSON (without internal keys)
                    with open(jsonl_path, 'r') as check_file:
                        lines = check_file.readlines()
                        assert len(lines) == 2
                        # Verify content was serialized correctly
                        entry1 = json.loads(lines[0])
                        assert entry1["uuid"] == "user-1"
                        assert "_line_num" not in entry1  # Internal keys should be stripped
                        entry2 = json.loads(lines[1])
                        assert entry2["uuid"] == "asst-1"

        os.unlink(f.name)


class TestGlobalServiceInstance:
    """Test global service instance."""

    def test_global_instance_exists(self):
        """Should have a global service instance."""
        assert jsonl_rewind_service is not None
        assert isinstance(jsonl_rewind_service, JSONLRewindService)
