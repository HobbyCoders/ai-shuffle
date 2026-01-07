"""
Unit tests for slash command discovery and execution.

Tests cover:
- SlashCommand dataclass and methods
- Frontmatter parsing (YAML)
- Command file parsing
- Command discovery from directories
- Command lookup by name
- Slash command input parsing
- Interactive and REST API command detection
- SDK built-in commands
- Plugin command discovery
- get_all_commands aggregation
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.core.slash_commands import (
    SlashCommand,
    parse_frontmatter,
    parse_command_file,
    discover_commands,
    _scan_commands_directory,
    get_command_by_name,
    is_slash_command,
    parse_command_input,
    is_interactive_command,
    get_interactive_command_info,
    is_rest_api_command,
    get_rest_api_command_info,
    is_sdk_builtin_command,
    get_sdk_builtin_command_info,
    discover_plugin_commands,
    get_all_commands,
    INTERACTIVE_COMMANDS,
    REST_API_COMMANDS,
    SDK_BUILTIN_COMMANDS,
)


# =============================================================================
# SlashCommand Dataclass Tests
# =============================================================================

class TestSlashCommand:
    """Test SlashCommand dataclass and its methods."""

    def test_slash_command_creation(self):
        """Should create SlashCommand with required fields."""
        cmd = SlashCommand(
            name="test",
            description="Test command",
            content="Test content",
            file_path="/path/to/test.md",
            source="project"
        )

        assert cmd.name == "test"
        assert cmd.description == "Test command"
        assert cmd.content == "Test content"
        assert cmd.file_path == "/path/to/test.md"
        assert cmd.source == "project"
        assert cmd.namespace is None
        assert cmd.allowed_tools == []
        assert cmd.argument_hint is None
        assert cmd.model is None
        assert cmd.disable_model_invocation is False

    def test_slash_command_with_all_fields(self):
        """Should create SlashCommand with all optional fields."""
        cmd = SlashCommand(
            name="advanced",
            description="Advanced command",
            content="Advanced content",
            file_path="/path/to/advanced.md",
            source="user",
            namespace="subdir",
            allowed_tools=["Read", "Write"],
            argument_hint="<file>",
            model="claude-3-opus",
            disable_model_invocation=True
        )

        assert cmd.namespace == "subdir"
        assert cmd.allowed_tools == ["Read", "Write"]
        assert cmd.argument_hint == "<file>"
        assert cmd.model == "claude-3-opus"
        assert cmd.disable_model_invocation is True

    def test_get_display_name(self):
        """Should return display name with leading slash."""
        cmd = SlashCommand(
            name="fix-bug",
            description="Fix a bug",
            content="Fix the bug",
            file_path="/test.md",
            source="project"
        )

        assert cmd.get_display_name() == "/fix-bug"

    def test_get_description_with_source_project(self):
        """Should return description with project source indicator."""
        cmd = SlashCommand(
            name="test",
            description="Test command",
            content="Content",
            file_path="/test.md",
            source="project"
        )

        result = cmd.get_description_with_source()
        assert result == "Test command (project)"

    def test_get_description_with_source_user(self):
        """Should return description with user source indicator."""
        cmd = SlashCommand(
            name="test",
            description="Test command",
            content="Content",
            file_path="/test.md",
            source="user"
        )

        result = cmd.get_description_with_source()
        assert result == "Test command (user)"

    def test_get_description_with_source_and_namespace(self):
        """Should return description with source and namespace."""
        cmd = SlashCommand(
            name="test",
            description="Test command",
            content="Content",
            file_path="/test.md",
            source="project",
            namespace="utils"
        )

        result = cmd.get_description_with_source()
        assert result == "Test command (project:utils)"

    def test_expand_prompt_no_arguments(self):
        """Should return content unchanged when no arguments."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="Do something useful",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt()
        assert result == "Do something useful"

    def test_expand_prompt_with_arguments_placeholder(self):
        """Should replace $ARGUMENTS with all arguments."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="Fix the following: $ARGUMENTS",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt("bug in login flow")
        assert result == "Fix the following: bug in login flow"

    def test_expand_prompt_with_positional_arguments(self):
        """Should replace $1, $2, etc. with positional arguments."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="Create a $1 in $2",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt("function utils.py")
        assert result == "Create a function in utils.py"

    def test_expand_prompt_mixed_arguments(self):
        """Should handle both $ARGUMENTS and positional arguments."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="Process $1 with options: $ARGUMENTS",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt("file.txt --verbose --force")
        assert result == "Process file.txt with options: file.txt --verbose --force"

    def test_expand_prompt_removes_unused_positional(self):
        """Should remove unreplaced positional arguments."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="Action: $1, Target: $2, Extra: $3",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt("create")
        assert result == "Action: create, Target: , Extra:"

    def test_expand_prompt_empty_arguments(self):
        """Should handle empty arguments string."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="All args: $ARGUMENTS, First: $1",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt("")
        assert result == "All args: , First:"

    def test_expand_prompt_strips_result(self):
        """Should strip whitespace from result."""
        cmd = SlashCommand(
            name="test",
            description="Test",
            content="  Result: $1  ",
            file_path="/test.md",
            source="project"
        )

        result = cmd.expand_prompt("value")
        assert result == "Result: value"


# =============================================================================
# Frontmatter Parsing Tests
# =============================================================================

class TestParseFrontmatter:
    """Test YAML frontmatter parsing."""

    def test_parse_frontmatter_valid(self):
        """Should parse valid YAML frontmatter."""
        content = """---
description: A test command
allowed-tools: Read, Write
model: claude-3-opus
---
This is the content."""

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter["description"] == "A test command"
        assert frontmatter["allowed-tools"] == "Read, Write"
        assert frontmatter["model"] == "claude-3-opus"
        assert remaining == "This is the content."

    def test_parse_frontmatter_no_frontmatter(self):
        """Should return empty dict when no frontmatter."""
        content = "Just regular content without frontmatter."

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}
        assert remaining == content

    def test_parse_frontmatter_unclosed(self):
        """Should return original content when frontmatter is unclosed."""
        content = """---
description: Unclosed
This is still frontmatter but missing closing ---"""

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}
        assert remaining == content

    def test_parse_frontmatter_empty_frontmatter(self):
        """Should handle empty frontmatter block."""
        content = """---
---
Content after empty frontmatter."""

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}
        assert remaining == "Content after empty frontmatter."

    def test_parse_frontmatter_invalid_yaml(self):
        """Should handle invalid YAML gracefully."""
        content = """---
invalid: yaml: syntax: error:
  - bad indent
---
Content after bad yaml."""

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter == {}  # Falls back to empty on parse error
        assert remaining == "Content after bad yaml."

    def test_parse_frontmatter_complex_yaml(self):
        """Should parse complex YAML structures."""
        content = """---
description: Complex command
allowed-tools:
  - Read
  - Write
  - Bash
disable-model-invocation: true
argument-hint: <file> <action>
---
Command content here."""

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter["description"] == "Complex command"
        assert frontmatter["allowed-tools"] == ["Read", "Write", "Bash"]
        assert frontmatter["disable-model-invocation"] is True
        assert frontmatter["argument-hint"] == "<file> <action>"

    def test_parse_frontmatter_multiline_content(self):
        """Should handle multiline content after frontmatter."""
        content = """---
description: Multiline
---
Line 1
Line 2
Line 3"""

        frontmatter, remaining = parse_frontmatter(content)

        assert frontmatter["description"] == "Multiline"
        assert remaining == "Line 1\nLine 2\nLine 3"


# =============================================================================
# Command File Parsing Tests
# =============================================================================

class TestParseCommandFile:
    """Test command file parsing."""

    def test_parse_command_file_basic(self):
        """Should parse basic command file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "fix-bug.md"
            cmd_file.write_text("""---
description: Fix a bug
---
Fix the specified bug.""")

            result = parse_command_file(cmd_file, "project")

            assert result is not None
            assert result.name == "fix-bug"
            assert result.description == "Fix a bug"
            assert result.content == "Fix the specified bug."
            assert result.source == "project"

    def test_parse_command_file_with_namespace(self):
        """Should include namespace in parsed command."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "test.md"
            cmd_file.write_text("Test content")

            result = parse_command_file(cmd_file, "project", namespace="utils")

            assert result.namespace == "utils"

    def test_parse_command_file_description_from_first_line(self):
        """Should use first line as description when not in frontmatter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "simple.md"
            cmd_file.write_text("# This is the first line\nMore content here.")

            result = parse_command_file(cmd_file, "project")

            assert result.description == "This is the first line"

    def test_parse_command_file_description_strips_markdown_headers(self):
        """Should strip markdown header symbols from description."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "header.md"
            cmd_file.write_text("### Header Description\nContent")

            result = parse_command_file(cmd_file, "project")

            assert result.description == "Header Description"

    def test_parse_command_file_long_description_truncated(self):
        """Should truncate description to 100 chars."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "long.md"
            long_line = "A" * 200
            cmd_file.write_text(long_line)

            result = parse_command_file(cmd_file, "project")

            assert len(result.description) <= 100

    def test_parse_command_file_allowed_tools_as_string(self):
        """Should parse allowed-tools as comma-separated string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "tools.md"
            cmd_file.write_text("""---
allowed-tools: Read, Write, Bash
---
Content""")

            result = parse_command_file(cmd_file, "project")

            assert result.allowed_tools == ["Read", "Write", "Bash"]

    def test_parse_command_file_allowed_tools_as_list(self):
        """Should parse allowed-tools as list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "tools.md"
            cmd_file.write_text("""---
allowed-tools:
  - Read
  - Write
---
Content""")

            result = parse_command_file(cmd_file, "project")

            assert result.allowed_tools == ["Read", "Write"]

    def test_parse_command_file_all_options(self):
        """Should parse all frontmatter options."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "full.md"
            cmd_file.write_text("""---
description: Full command
allowed-tools: Read
argument-hint: <filename>
model: claude-3-opus
disable-model-invocation: true
---
Full content""")

            result = parse_command_file(cmd_file, "project")

            assert result.description == "Full command"
            assert result.allowed_tools == ["Read"]
            assert result.argument_hint == "<filename>"
            assert result.model == "claude-3-opus"
            assert result.disable_model_invocation is True

    def test_parse_command_file_nonexistent(self):
        """Should return None for nonexistent file."""
        result = parse_command_file(Path("/nonexistent/command.md"), "project")

        assert result is None

    def test_parse_command_file_read_error(self):
        """Should return None on read error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a directory instead of a file to cause read error
            cmd_path = Path(tmpdir) / "command.md"
            cmd_path.mkdir()

            result = parse_command_file(cmd_path, "project")

            assert result is None

    def test_parse_command_file_empty_content(self):
        """Should handle empty content file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_file = Path(tmpdir) / "empty.md"
            cmd_file.write_text("")

            result = parse_command_file(cmd_file, "project")

            assert result is not None
            assert result.content == ""
            assert result.description == ""


# =============================================================================
# Command Discovery Tests
# =============================================================================

class TestDiscoverCommands:
    """Test command discovery from directories."""

    def test_discover_commands_from_project(self):
        """Should discover commands from .claude/commands/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "fix.md").write_text("Fix command")
            (commands_dir / "review.md").write_text("Review command")

            result = discover_commands(str(working_dir))

            assert len(result) == 2
            names = [cmd.name for cmd in result]
            assert "fix" in names
            assert "review" in names

    def test_discover_commands_no_commands_dir(self):
        """Should return empty list when no commands directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_commands(tmpdir)

            assert result == []

    def test_discover_commands_empty_dir(self):
        """Should return empty list for empty commands directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            result = discover_commands(str(working_dir))

            assert result == []


class TestScanCommandsDirectory:
    """Test recursive command directory scanning."""

    def test_scan_commands_directory_flat(self):
        """Should scan flat directory structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "cmd1.md").write_text("Command 1")
            (base_dir / "cmd2.md").write_text("Command 2")

            result = _scan_commands_directory(base_dir, "project")

            assert len(result) == 2

    def test_scan_commands_directory_nested(self):
        """Should scan nested directories with namespace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            subdir = base_dir / "utils"
            subdir.mkdir()

            (base_dir / "root.md").write_text("Root command")
            (subdir / "helper.md").write_text("Helper command")

            result = _scan_commands_directory(base_dir, "project")

            assert len(result) == 2

            helper_cmd = next((c for c in result if c.name == "helper"), None)
            assert helper_cmd is not None
            assert helper_cmd.namespace == "utils"

    def test_scan_commands_directory_deeply_nested(self):
        """Should scan deeply nested directories with compound namespace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            deep_dir = base_dir / "level1" / "level2"
            deep_dir.mkdir(parents=True)

            (deep_dir / "deep.md").write_text("Deep command")

            result = _scan_commands_directory(base_dir, "project")

            assert len(result) == 1
            assert result[0].namespace == "level1/level2"

    def test_scan_commands_directory_skips_hidden(self):
        """Should skip directories starting with dot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            hidden_dir = base_dir / ".hidden"
            hidden_dir.mkdir()

            (base_dir / "visible.md").write_text("Visible")
            (hidden_dir / "hidden.md").write_text("Hidden")

            result = _scan_commands_directory(base_dir, "project")

            assert len(result) == 1
            assert result[0].name == "visible"

    def test_scan_commands_directory_skips_non_md(self):
        """Should only scan .md files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "command.md").write_text("Valid")
            (base_dir / "readme.txt").write_text("Not a command")
            (base_dir / "config.yaml").write_text("Not a command")

            result = _scan_commands_directory(base_dir, "project")

            assert len(result) == 1
            assert result[0].name == "command"

    def test_scan_commands_directory_permission_error(self):
        """Should handle permission errors gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)

            with patch.object(Path, 'iterdir', side_effect=PermissionError("Access denied")):
                result = _scan_commands_directory(base_dir, "project")

                assert result == []

    def test_scan_commands_directory_generic_error(self):
        """Should handle generic errors gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)

            with patch.object(Path, 'iterdir', side_effect=Exception("Unexpected error")):
                result = _scan_commands_directory(base_dir, "project")

                assert result == []

    def test_scan_commands_directory_with_existing_namespace(self):
        """Should append to existing namespace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            subdir = base_dir / "sub"
            subdir.mkdir()

            (subdir / "cmd.md").write_text("Command")

            result = _scan_commands_directory(base_dir, "project", namespace="parent")

            assert len(result) == 1
            assert result[0].namespace == "parent/sub"


# =============================================================================
# Command Lookup Tests
# =============================================================================

class TestGetCommandByName:
    """Test command lookup by name."""

    def test_get_command_by_name_found(self):
        """Should find command by name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "target.md").write_text("Target command")

            result = get_command_by_name(str(working_dir), "target")

            assert result is not None
            assert result.name == "target"

    def test_get_command_by_name_with_slash(self):
        """Should find command when name has leading slash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "target.md").write_text("Target command")

            result = get_command_by_name(str(working_dir), "/target")

            assert result is not None
            assert result.name == "target"

    def test_get_command_by_name_not_found(self):
        """Should return None when command not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            result = get_command_by_name(str(working_dir), "nonexistent")

            assert result is None


# =============================================================================
# Slash Command Parsing Tests
# =============================================================================

class TestIsSlashCommand:
    """Test slash command detection."""

    def test_is_slash_command_valid(self):
        """Should detect valid slash commands."""
        assert is_slash_command("/fix") is True
        assert is_slash_command("/fix-bug") is True
        assert is_slash_command("/fix-bug argument") is True

    def test_is_slash_command_empty(self):
        """Should return False for empty string."""
        assert is_slash_command("") is False

    def test_is_slash_command_just_slash(self):
        """Should return False for just a slash."""
        assert is_slash_command("/") is False

    def test_is_slash_command_no_slash(self):
        """Should return False for non-slash text."""
        assert is_slash_command("fix") is False
        assert is_slash_command("hello world") is False

    def test_is_slash_command_slash_not_first(self):
        """Should return False when slash not at start."""
        assert is_slash_command("text /command") is False
        assert is_slash_command(" /command") is False


class TestParseCommandInput:
    """Test command input parsing."""

    def test_parse_command_input_simple(self):
        """Should parse simple command."""
        name, args = parse_command_input("/fix")

        assert name == "fix"
        assert args == ""

    def test_parse_command_input_with_args(self):
        """Should parse command with arguments."""
        name, args = parse_command_input("/fix-issue 123 high")

        assert name == "fix-issue"
        assert args == "123 high"

    def test_parse_command_input_single_arg(self):
        """Should parse command with single argument."""
        name, args = parse_command_input("/review myfile.py")

        assert name == "review"
        assert args == "myfile.py"

    def test_parse_command_input_not_slash_command(self):
        """Should return empty name for non-slash input."""
        name, args = parse_command_input("regular text")

        assert name == ""
        assert args == "regular text"

    def test_parse_command_input_empty(self):
        """Should handle empty input."""
        name, args = parse_command_input("")

        assert name == ""
        assert args == ""

    def test_parse_command_input_preserves_arg_spacing(self):
        """Should preserve argument spacing."""
        name, args = parse_command_input("/cmd   multi   spaced   args")

        assert name == "cmd"
        assert args == "multi   spaced   args"


# =============================================================================
# Interactive Command Tests
# =============================================================================

class TestInteractiveCommands:
    """Test interactive command detection."""

    def test_is_interactive_command_resume(self):
        """Should detect resume as interactive."""
        assert is_interactive_command("resume") is True
        assert is_interactive_command("/resume") is True

    def test_is_interactive_command_unknown(self):
        """Should return False for unknown commands."""
        assert is_interactive_command("unknown") is False
        assert is_interactive_command("fix") is False

    def test_get_interactive_command_info_resume(self):
        """Should return info for resume command."""
        info = get_interactive_command_info("resume")

        assert info is not None
        assert "description" in info
        assert info["requires_cli"] is True

    def test_get_interactive_command_info_with_slash(self):
        """Should handle command with leading slash."""
        info = get_interactive_command_info("/resume")

        assert info is not None

    def test_get_interactive_command_info_unknown(self):
        """Should return None for unknown command."""
        info = get_interactive_command_info("unknown")

        assert info is None


# =============================================================================
# REST API Command Tests
# =============================================================================

class TestRestApiCommands:
    """Test REST API command detection."""

    def test_is_rest_api_command_rewind(self):
        """Should detect rewind as REST API command."""
        assert is_rest_api_command("rewind") is True
        assert is_rest_api_command("/rewind") is True

    def test_is_rest_api_command_unknown(self):
        """Should return False for unknown commands."""
        assert is_rest_api_command("unknown") is False
        assert is_rest_api_command("fix") is False

    def test_get_rest_api_command_info_rewind(self):
        """Should return info for rewind command."""
        info = get_rest_api_command_info("rewind")

        assert info is not None
        assert "description" in info
        assert "api_endpoint" in info
        assert info["requires_session"] is True
        assert info["method"] == "direct_jsonl"

    def test_get_rest_api_command_info_with_slash(self):
        """Should handle command with leading slash."""
        info = get_rest_api_command_info("/rewind")

        assert info is not None

    def test_get_rest_api_command_info_unknown(self):
        """Should return None for unknown command."""
        info = get_rest_api_command_info("unknown")

        assert info is None


# =============================================================================
# SDK Built-in Command Tests
# =============================================================================

class TestSdkBuiltinCommands:
    """Test SDK built-in command detection."""

    def test_is_sdk_builtin_command_compact(self):
        """Should detect compact as SDK builtin."""
        assert is_sdk_builtin_command("compact") is True
        assert is_sdk_builtin_command("/compact") is True

    def test_is_sdk_builtin_command_unknown(self):
        """Should return False for unknown commands."""
        assert is_sdk_builtin_command("unknown") is False
        assert is_sdk_builtin_command("fix") is False

    def test_get_sdk_builtin_command_info_compact(self):
        """Should return info for compact command."""
        info = get_sdk_builtin_command_info("compact")

        assert info is not None
        assert "description" in info
        assert info["type"] == "sdk_builtin"

    def test_get_sdk_builtin_command_info_with_slash(self):
        """Should handle command with leading slash."""
        info = get_sdk_builtin_command_info("/compact")

        assert info is not None

    def test_get_sdk_builtin_command_info_unknown(self):
        """Should return None for unknown command."""
        info = get_sdk_builtin_command_info("unknown")

        assert info is None


# =============================================================================
# Plugin Command Discovery Tests
# =============================================================================

class TestDiscoverPluginCommands:
    """Test plugin command discovery."""

    def test_discover_plugin_commands_with_plugins(self):
        """Should discover commands from enabled plugins."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create mock plugin structure
            plugin_dir = Path(tmpdir) / "my-plugin"
            commands_dir = plugin_dir / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "action.md").write_text("""---
description: Plugin action
---
Do something""")

            mock_plugin = MagicMock()
            mock_plugin.id = "my-plugin-id"
            mock_plugin.name = "my-plugin"
            mock_plugin.has_commands = True

            mock_details = MagicMock()
            mock_details.install_path = str(plugin_dir)

            mock_service = MagicMock()
            mock_service.get_installed_plugins.return_value = [mock_plugin]
            mock_service.get_enabled_plugins.return_value = {"my-plugin-id": True}
            mock_service.get_plugin_details.return_value = mock_details

            with patch("app.core.plugin_service.get_plugin_service", return_value=mock_service):
                result = discover_plugin_commands()

                assert len(result) == 1
                assert result[0]["name"] == "my-plugin:action"
                assert result[0]["display"] == "/my-plugin:action"
                assert result[0]["type"] == "plugin"
                assert result[0]["source"] == "my-plugin-id"

    def test_discover_plugin_commands_disabled_plugin(self):
        """Should skip disabled plugins."""
        mock_plugin = MagicMock()
        mock_plugin.id = "disabled-plugin"
        mock_plugin.name = "disabled"
        mock_plugin.has_commands = True

        mock_service = MagicMock()
        mock_service.get_installed_plugins.return_value = [mock_plugin]
        mock_service.get_enabled_plugins.return_value = {"disabled-plugin": False}

        with patch("app.core.plugin_service.get_plugin_service", return_value=mock_service):
            result = discover_plugin_commands()

            assert result == []

    def test_discover_plugin_commands_no_commands(self):
        """Should skip plugins without commands."""
        mock_plugin = MagicMock()
        mock_plugin.id = "no-commands"
        mock_plugin.name = "no-commands"
        mock_plugin.has_commands = False

        mock_service = MagicMock()
        mock_service.get_installed_plugins.return_value = [mock_plugin]
        mock_service.get_enabled_plugins.return_value = {"no-commands": True}

        with patch("app.core.plugin_service.get_plugin_service", return_value=mock_service):
            result = discover_plugin_commands()

            assert result == []

    def test_discover_plugin_commands_no_details(self):
        """Should handle missing plugin details."""
        mock_plugin = MagicMock()
        mock_plugin.id = "missing-details"
        mock_plugin.name = "missing"
        mock_plugin.has_commands = True

        mock_service = MagicMock()
        mock_service.get_installed_plugins.return_value = [mock_plugin]
        mock_service.get_enabled_plugins.return_value = {"missing-details": True}
        mock_service.get_plugin_details.return_value = None

        with patch("app.core.plugin_service.get_plugin_service", return_value=mock_service):
            result = discover_plugin_commands()

            assert result == []

    def test_discover_plugin_commands_no_commands_dir(self):
        """Should handle missing commands directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir) / "no-cmd-dir"
            plugin_dir.mkdir()
            # No commands directory created

            mock_plugin = MagicMock()
            mock_plugin.id = "no-cmd-dir"
            mock_plugin.name = "no-cmd-dir"
            mock_plugin.has_commands = True

            mock_details = MagicMock()
            mock_details.install_path = str(plugin_dir)

            mock_service = MagicMock()
            mock_service.get_installed_plugins.return_value = [mock_plugin]
            mock_service.get_enabled_plugins.return_value = {"no-cmd-dir": True}
            mock_service.get_plugin_details.return_value = mock_details

            with patch("app.core.plugin_service.get_plugin_service", return_value=mock_service):
                result = discover_plugin_commands()

                assert result == []

    def test_discover_plugin_commands_error_handling(self):
        """Should handle errors gracefully."""
        with patch("app.core.plugin_service.get_plugin_service", side_effect=Exception("Plugin error")):
            result = discover_plugin_commands()

            assert result == []


# =============================================================================
# Get All Commands Tests
# =============================================================================

class TestGetAllCommands:
    """Test get_all_commands aggregation."""

    def test_get_all_commands_includes_custom(self):
        """Should include custom commands from .claude/commands/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "custom.md").write_text("""---
description: Custom command
argument-hint: <arg>
---
Custom content""")

            with patch("app.core.slash_commands.discover_plugin_commands", return_value=[]):
                result = get_all_commands(str(working_dir))

                custom_cmd = next((c for c in result if c["name"] == "custom"), None)
                assert custom_cmd is not None
                assert custom_cmd["display"] == "/custom"
                assert custom_cmd["type"] == "custom"
                assert custom_cmd["argument_hint"] == "<arg>"

    def test_get_all_commands_includes_sdk_builtin(self):
        """Should include SDK built-in commands."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.core.slash_commands.discover_plugin_commands", return_value=[]):
                result = get_all_commands(tmpdir)

                compact_cmd = next((c for c in result if c["name"] == "compact"), None)
                assert compact_cmd is not None
                assert compact_cmd["type"] == "sdk_builtin"

    def test_get_all_commands_includes_plugins(self):
        """Should include plugin commands."""
        plugin_cmd = {
            "name": "plugin:action",
            "display": "/plugin:action",
            "description": "Plugin action",
            "argument_hint": None,
            "type": "plugin",
            "source": "plugin-id"
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.core.slash_commands.discover_plugin_commands", return_value=[plugin_cmd]):
                result = get_all_commands(tmpdir)

                found = next((c for c in result if c["name"] == "plugin:action"), None)
                assert found is not None
                assert found["type"] == "plugin"

    def test_get_all_commands_sorted(self):
        """Should return commands sorted by name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir)
            commands_dir = working_dir / ".claude" / "commands"
            commands_dir.mkdir(parents=True)

            (commands_dir / "zebra.md").write_text("Z command")
            (commands_dir / "alpha.md").write_text("A command")

            with patch("app.core.slash_commands.discover_plugin_commands", return_value=[]):
                result = get_all_commands(str(working_dir))

                names = [c["name"] for c in result]
                assert names == sorted(names)

    def test_get_all_commands_excludes_interactive(self):
        """Should NOT include interactive commands (/resume)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.core.slash_commands.discover_plugin_commands", return_value=[]):
                result = get_all_commands(tmpdir)

                resume_cmd = next((c for c in result if c["name"] == "resume"), None)
                assert resume_cmd is None

    def test_get_all_commands_excludes_rest_api(self):
        """Should NOT include REST API commands (/rewind)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.core.slash_commands.discover_plugin_commands", return_value=[]):
                result = get_all_commands(tmpdir)

                rewind_cmd = next((c for c in result if c["name"] == "rewind"), None)
                assert rewind_cmd is None


# =============================================================================
# Command Constants Tests
# =============================================================================

class TestCommandConstants:
    """Test command constant dictionaries."""

    def test_interactive_commands_structure(self):
        """INTERACTIVE_COMMANDS should have correct structure."""
        assert "resume" in INTERACTIVE_COMMANDS
        assert "description" in INTERACTIVE_COMMANDS["resume"]
        assert "requires_cli" in INTERACTIVE_COMMANDS["resume"]

    def test_rest_api_commands_structure(self):
        """REST_API_COMMANDS should have correct structure."""
        assert "rewind" in REST_API_COMMANDS
        assert "description" in REST_API_COMMANDS["rewind"]
        assert "api_endpoint" in REST_API_COMMANDS["rewind"]
        assert "requires_session" in REST_API_COMMANDS["rewind"]

    def test_sdk_builtin_commands_structure(self):
        """SDK_BUILTIN_COMMANDS should have correct structure."""
        assert "compact" in SDK_BUILTIN_COMMANDS
        assert "description" in SDK_BUILTIN_COMMANDS["compact"]
        assert "type" in SDK_BUILTIN_COMMANDS["compact"]
