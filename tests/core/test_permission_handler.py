"""
Unit tests for permission handler module.

Tests cover:
- PermissionDecision and RememberScope enums
- PermissionRequest dataclass
- PermissionRule matching logic (pattern matching for all tool types)
- PermissionHandler queue management
- Permission request/response workflow
- Rule creation and auto-resolution
- Session and profile rule management
- Timeout handling
- Cancellation of pending requests
- Edge cases and error paths

This is security-critical code - comprehensive coverage is essential.
"""

import asyncio
import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Any, List

from app.core.permission_handler import (
    PermissionDecision,
    RememberScope,
    PermissionRequest,
    PermissionRule,
    PermissionHandler,
    permission_handler,
)


# =============================================================================
# Enum Tests
# =============================================================================

class TestPermissionDecision:
    """Test PermissionDecision enum."""

    def test_allow_value(self):
        """ALLOW should have correct string value."""
        assert PermissionDecision.ALLOW.value == "allow"

    def test_deny_value(self):
        """DENY should have correct string value."""
        assert PermissionDecision.DENY.value == "deny"

    def test_enum_is_string(self):
        """PermissionDecision should be a string enum."""
        assert isinstance(PermissionDecision.ALLOW, str)
        assert PermissionDecision.ALLOW == "allow"


class TestRememberScope:
    """Test RememberScope enum."""

    def test_none_value(self):
        """NONE should have correct string value."""
        assert RememberScope.NONE.value == "none"

    def test_session_value(self):
        """SESSION should have correct string value."""
        assert RememberScope.SESSION.value == "session"

    def test_profile_value(self):
        """PROFILE should have correct string value."""
        assert RememberScope.PROFILE.value == "profile"

    def test_enum_is_string(self):
        """RememberScope should be a string enum."""
        assert isinstance(RememberScope.SESSION, str)
        assert RememberScope.SESSION == "session"


# =============================================================================
# PermissionRequest Tests
# =============================================================================

class TestPermissionRequest:
    """Test PermissionRequest dataclass."""

    def test_basic_creation(self):
        """Should create a permission request with required fields."""
        request = PermissionRequest(
            request_id="req-123",
            session_id="session-456",
            profile_id="profile-789",
            tool_name="Bash",
            tool_input={"command": "npm install"}
        )

        assert request.request_id == "req-123"
        assert request.session_id == "session-456"
        assert request.profile_id == "profile-789"
        assert request.tool_name == "Bash"
        assert request.tool_input == {"command": "npm install"}

    def test_default_created_at(self):
        """Should set created_at to current time by default."""
        before = datetime.now()
        request = PermissionRequest(
            request_id="req-123",
            session_id="session-456",
            profile_id="profile-789",
            tool_name="Bash",
            tool_input={}
        )
        after = datetime.now()

        assert before <= request.created_at <= after

    def test_default_response_event(self):
        """Should create an asyncio.Event by default."""
        request = PermissionRequest(
            request_id="req-123",
            session_id="session-456",
            profile_id="profile-789",
            tool_name="Bash",
            tool_input={}
        )

        assert isinstance(request.response_event, asyncio.Event)
        assert not request.response_event.is_set()

    def test_default_response_is_none(self):
        """Should have None response by default."""
        request = PermissionRequest(
            request_id="req-123",
            session_id="session-456",
            profile_id="profile-789",
            tool_name="Bash",
            tool_input={}
        )

        assert request.response is None


# =============================================================================
# PermissionRule Tests
# =============================================================================

class TestPermissionRule:
    """Test PermissionRule dataclass and matching logic."""

    def test_basic_creation(self):
        """Should create a permission rule with required fields."""
        rule = PermissionRule(
            id="rule-123",
            session_id="session-456",
            profile_id="profile-789",
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )

        assert rule.id == "rule-123"
        assert rule.session_id == "session-456"
        assert rule.profile_id == "profile-789"
        assert rule.tool_name == "Bash"
        assert rule.tool_pattern == "npm *"
        assert rule.decision == PermissionDecision.ALLOW

    def test_matches_tool_name_exact(self):
        """Should match when tool name matches exactly."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Bash", {"command": "ls"})
        assert not rule.matches("Read", {"file_path": "/test"})

    def test_matches_wildcard_tool_name(self):
        """Should match all tools when tool_name is '*'."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="*",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Bash", {"command": "ls"})
        assert rule.matches("Read", {"file_path": "/test"})
        assert rule.matches("AnyTool", {"foo": "bar"})

    def test_matches_without_pattern(self):
        """Should match any input when no pattern specified."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Bash", {"command": "any command"})
        assert rule.matches("Bash", {"command": "npm install"})
        assert rule.matches("Bash", {})

    def test_matches_bash_command_pattern(self):
        """Should match Bash commands using fnmatch pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Bash", {"command": "npm install"})
        assert rule.matches("Bash", {"command": "npm run build"})
        assert not rule.matches("Bash", {"command": "yarn install"})
        assert not rule.matches("Bash", {"command": "git status"})

    def test_matches_bash_command_pattern_with_glob(self):
        """Should support glob patterns for Bash commands."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="git *",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Bash", {"command": "git status"})
        assert rule.matches("Bash", {"command": "git commit -m 'msg'"})
        assert not rule.matches("Bash", {"command": "rm -rf /"})

    def test_matches_read_file_path_pattern(self):
        """Should match Read tool using file_path pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Read",
            tool_pattern="/home/user/*",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Read", {"file_path": "/home/user/test.txt"})
        assert rule.matches("Read", {"file_path": "/home/user/config.json"})
        assert not rule.matches("Read", {"file_path": "/etc/passwd"})

    def test_matches_write_file_path_pattern(self):
        """Should match Write tool using file_path pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Write",
            tool_pattern="*.py",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Write", {"file_path": "script.py"})
        assert rule.matches("Write", {"file_path": "test.py"})
        assert not rule.matches("Write", {"file_path": "config.json"})

    def test_matches_edit_file_path_pattern(self):
        """Should match Edit tool using file_path pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Edit",
            tool_pattern="/project/src/*",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Edit", {"file_path": "/project/src/main.py"})
        assert not rule.matches("Edit", {"file_path": "/other/path.py"})

    def test_matches_glob_path_pattern(self):
        """Should match Glob tool using path pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Glob",
            tool_pattern="/safe/*",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Glob", {"path": "/safe/dir"})
        assert not rule.matches("Glob", {"path": "/unsafe/dir"})

    def test_matches_grep_path_pattern(self):
        """Should match Grep tool using path pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Grep",
            tool_pattern="/allowed/*",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("Grep", {"path": "/allowed/search"})
        assert not rule.matches("Grep", {"path": "/forbidden/search"})

    def test_matches_webfetch_url_pattern(self):
        """Should match WebFetch tool using URL pattern."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="WebFetch",
            tool_pattern="https://api.github.com/*",
            decision=PermissionDecision.ALLOW
        )

        assert rule.matches("WebFetch", {"url": "https://api.github.com/users"})
        assert rule.matches("WebFetch", {"url": "https://api.github.com/repos/foo/bar"})
        assert not rule.matches("WebFetch", {"url": "https://malicious.com/attack"})

    def test_matches_generic_tool_any_string_value(self):
        """Should match generic tools against any string value in input."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="CustomTool",
            tool_pattern="safe_*",
            decision=PermissionDecision.ALLOW
        )

        # Should match any string value
        assert rule.matches("CustomTool", {"arg1": "safe_value"})
        assert rule.matches("CustomTool", {"other": "foo", "arg2": "safe_operation"})
        assert not rule.matches("CustomTool", {"arg1": "unsafe_value"})

    def test_matches_generic_tool_no_match_non_strings(self):
        """Should not match non-string values in generic tool input."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="CustomTool",
            tool_pattern="123",
            decision=PermissionDecision.ALLOW
        )

        # Number values should not match pattern
        assert not rule.matches("CustomTool", {"count": 123})
        assert not rule.matches("CustomTool", {"flag": True})

    def test_matches_empty_tool_input(self):
        """Should handle empty tool input gracefully."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )

        assert not rule.matches("Bash", {})

    def test_matches_missing_command_key(self):
        """Should handle missing expected keys in tool input."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )

        # No command key
        assert not rule.matches("Bash", {"other_key": "value"})

    def test_matches_uses_path_key_for_file_tools(self):
        """Should check both file_path and path for file-related tools."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Glob",
            tool_pattern="/test/*",
            decision=PermissionDecision.ALLOW
        )

        # Glob uses 'path' not 'file_path'
        assert rule.matches("Glob", {"path": "/test/dir"})
        # Also works with file_path for compatibility
        assert rule.matches("Glob", {"file_path": "/test/dir"})


# =============================================================================
# PermissionHandler Tests
# =============================================================================

class TestPermissionHandler:
    """Test PermissionHandler class."""

    @pytest.fixture
    def handler(self):
        """Create a fresh PermissionHandler for each test."""
        return PermissionHandler()

    @pytest.fixture
    def mock_broadcast(self):
        """Create a mock broadcast function."""
        return AsyncMock()

    # -------------------------------------------------------------------------
    # Basic State Tests
    # -------------------------------------------------------------------------

    def test_initial_state(self, handler):
        """Handler should start with empty state."""
        assert handler._pending_requests == {}
        assert handler._session_rules == {}

    def test_get_pending_requests_empty(self, handler):
        """Should return empty list for unknown session."""
        assert handler.get_pending_requests("unknown-session") == []

    def test_get_queue_count_empty(self, handler):
        """Should return 0 for unknown session."""
        assert handler.get_queue_count("unknown-session") == 0

    def test_get_session_rules_empty(self, handler):
        """Should return empty list for session without rules."""
        assert handler.get_session_rules("unknown-session") == []

    # -------------------------------------------------------------------------
    # Rule Checking Tests
    # -------------------------------------------------------------------------

    def test_check_rules_no_rules(self, handler):
        """Should return None when no rules exist."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "ls"})
            assert result is None

    def test_check_rules_session_rule_match(self, handler):
        """Should return matching session rule."""
        # Add a session rule
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )
        handler._session_rules["session-1"] = [rule]

        with patch("app.db.database.get_permission_rules", return_value=[]):
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "ls"})
            assert result is not None
            assert result.id == "rule-1"
            assert result.decision == PermissionDecision.ALLOW

    def test_check_rules_session_rule_no_match(self, handler):
        """Should not return non-matching session rule."""
        # Add a rule for different tool
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Read",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )
        handler._session_rules["session-1"] = [rule]

        with patch("app.db.database.get_permission_rules", return_value=[]):
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "ls"})
            assert result is None

    def test_check_rules_profile_rule_match(self, handler):
        """Should return matching profile rule from database."""
        db_rule = {
            "id": "db-rule-1",
            "profile_id": "profile-1",
            "tool_name": "Bash",
            "tool_pattern": None,
            "decision": "allow"
        }

        with patch("app.db.database.get_permission_rules", return_value=[db_rule]):
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "ls"})
            assert result is not None
            assert result.id == "db-rule-1"
            assert result.decision == PermissionDecision.ALLOW

    def test_check_rules_session_takes_precedence(self, handler):
        """Session rules should take precedence over profile rules."""
        # Add session rule (DENY)
        session_rule = PermissionRule(
            id="session-rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.DENY
        )
        handler._session_rules["session-1"] = [session_rule]

        # Profile rule (ALLOW) in database
        db_rule = {
            "id": "db-rule-1",
            "profile_id": "profile-1",
            "tool_name": "Bash",
            "tool_pattern": None,
            "decision": "allow"
        }

        with patch("app.db.database.get_permission_rules", return_value=[db_rule]):
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "ls"})
            # Session rule (DENY) should win
            assert result is not None
            assert result.id == "session-rule-1"
            assert result.decision == PermissionDecision.DENY

    def test_check_rules_pattern_matching(self, handler):
        """Should check pattern matching for rules."""
        # Add a rule with pattern
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )
        handler._session_rules["session-1"] = [rule]

        with patch("app.db.database.get_permission_rules", return_value=[]):
            # Should match npm commands
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "npm install"})
            assert result is not None

            # Should not match non-npm commands
            result = handler._check_rules("session-1", "profile-1", "Bash", {"command": "git status"})
            assert result is None

    # -------------------------------------------------------------------------
    # Request Permission Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_request_permission_auto_allowed_by_rule(self, handler, mock_broadcast):
        """Should auto-allow when matching ALLOW rule exists."""
        # Add allow rule
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )
        handler._session_rules["session-1"] = [rule]

        with patch("app.db.database.get_permission_rules", return_value=[]):
            result = await handler.request_permission(
                request_id="req-1",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": "ls"},
                broadcast_func=mock_broadcast
            )

        # Should be allowed, no broadcast (no queue)
        assert result.behavior == "allow"
        mock_broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_request_permission_auto_denied_by_rule(self, handler, mock_broadcast):
        """Should auto-deny when matching DENY rule exists."""
        # Add deny rule
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.DENY
        )
        handler._session_rules["session-1"] = [rule]

        with patch("app.db.database.get_permission_rules", return_value=[]):
            result = await handler.request_permission(
                request_id="req-1",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": "ls"},
                broadcast_func=mock_broadcast
            )

        # Should be denied
        assert result.behavior == "deny"
        assert "Denied by saved rule" in result.message
        mock_broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_request_permission_queued(self, handler, mock_broadcast):
        """Should queue request when no rule matches and broadcast."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            # Start the request but don't wait (will timeout immediately if we await)
            task = asyncio.create_task(
                handler.request_permission(
                    request_id="req-1",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "rm -rf /"},
                    broadcast_func=mock_broadcast
                )
            )

            # Give time for request to be queued
            await asyncio.sleep(0.01)

            # Check it was queued
            assert handler.get_queue_count("session-1") == 1
            assert mock_broadcast.called

            # Verify broadcast content
            call_args = mock_broadcast.call_args[0][0]
            assert call_args["type"] == "permission_request"
            assert call_args["request_id"] == "req-1"
            assert call_args["tool_name"] == "Bash"

            # Properly cancel by setting the response and signaling the event
            # This avoids the "coroutine was never awaited" warning
            await handler.cancel_request("req-1", "session-1")
            await task

    @pytest.mark.asyncio
    async def test_request_permission_timeout(self, handler, mock_broadcast):
        """Should timeout and deny if no response received."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            # Use a very short timeout for testing
            with patch.object(asyncio, "wait_for", side_effect=asyncio.TimeoutError):
                result = await handler.request_permission(
                    request_id="req-1",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    broadcast_func=mock_broadcast
                )

        assert result.behavior == "deny"
        assert "timed out" in result.message

        # Request should be removed from queue
        assert handler.get_queue_count("session-1") == 0

    @pytest.mark.asyncio
    async def test_request_permission_response_allow(self, handler, mock_broadcast):
        """Should return allow result when user allows."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            async def respond_after_delay():
                await asyncio.sleep(0.01)
                await handler.respond(
                    request_id="req-1",
                    session_id="session-1",
                    decision="allow"
                )

            task = asyncio.create_task(respond_after_delay())

            result = await handler.request_permission(
                request_id="req-1",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": "ls"},
                broadcast_func=mock_broadcast
            )

            await task

        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_request_permission_response_deny(self, handler, mock_broadcast):
        """Should return deny result when user denies."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            async def respond_after_delay():
                await asyncio.sleep(0.01)
                await handler.respond(
                    request_id="req-1",
                    session_id="session-1",
                    decision="deny"
                )

            task = asyncio.create_task(respond_after_delay())

            result = await handler.request_permission(
                request_id="req-1",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": "rm -rf /"},
                broadcast_func=mock_broadcast
            )

            await task

        assert result.behavior == "deny"

    @pytest.mark.asyncio
    async def test_request_permission_no_response_set(self, handler, mock_broadcast):
        """Should deny when response event is set but no response data."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            async def set_event_without_response():
                await asyncio.sleep(0.01)
                # Get the pending request and set event without setting response
                request = handler._pending_requests["session-1"]["req-1"]
                request.response_event.set()

            task = asyncio.create_task(set_event_without_response())

            result = await handler.request_permission(
                request_id="req-1",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": "ls"},
                broadcast_func=mock_broadcast
            )

            await task

        assert result.behavior == "deny"
        assert "No response received" in result.message

    # -------------------------------------------------------------------------
    # Respond Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_respond_session_not_found(self, handler):
        """Should return error when session not found."""
        result = await handler.respond(
            request_id="req-1",
            session_id="unknown-session",
            decision="allow"
        )

        assert result == {"error": "Session not found"}

    @pytest.mark.asyncio
    async def test_respond_request_not_found(self, handler):
        """Should return error when request not found."""
        handler._pending_requests["session-1"] = {}

        result = await handler.respond(
            request_id="unknown-req",
            session_id="session-1",
            decision="allow"
        )

        assert result == {"error": "Request not found"}

    @pytest.mark.asyncio
    async def test_respond_sets_allow_response(self, handler):
        """Should set allow response on request."""
        # Create a pending request
        request = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        handler._pending_requests["session-1"] = {"req-1": request}

        result = await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="allow"
        )

        assert result["resolved"] is True
        assert request.response["behavior"] == "allow"
        assert request.response_event.is_set()

    @pytest.mark.asyncio
    async def test_respond_sets_deny_response(self, handler):
        """Should set deny response on request."""
        # Create a pending request
        request = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "rm -rf /"}
        )
        handler._pending_requests["session-1"] = {"req-1": request}

        result = await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="deny"
        )

        assert result["resolved"] is True
        assert request.response["behavior"] == "deny"
        assert "denied" in request.response["message"].lower()
        assert request.response_event.is_set()

    @pytest.mark.asyncio
    async def test_respond_removes_from_pending(self, handler):
        """Should remove request from pending after response."""
        # Create a pending request
        request = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        handler._pending_requests["session-1"] = {"req-1": request}

        await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="allow"
        )

        assert "req-1" not in handler._pending_requests["session-1"]

    @pytest.mark.asyncio
    async def test_respond_remember_session_creates_rule(self, handler):
        """Should create session rule when remember=session."""
        # Create a pending request
        request = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "npm install"}
        )
        handler._pending_requests["session-1"] = {"req-1": request}

        result = await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="allow",
            remember="session",
            pattern="npm *"
        )

        assert result["resolved"] is True
        # Check session rule was created
        assert "session-1" in handler._session_rules
        assert len(handler._session_rules["session-1"]) == 1
        rule = handler._session_rules["session-1"][0]
        assert rule.tool_name == "Bash"
        assert rule.tool_pattern == "npm *"
        assert rule.decision == PermissionDecision.ALLOW

    @pytest.mark.asyncio
    async def test_respond_remember_profile_saves_to_db(self, handler):
        """Should save to database when remember=profile."""
        # Create a pending request
        request = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "git push"}
        )
        handler._pending_requests["session-1"] = {"req-1": request}

        with patch("app.db.database.add_permission_rule") as mock_add:
            result = await handler.respond(
                request_id="req-1",
                session_id="session-1",
                decision="allow",
                remember="profile",
                pattern="git *"
            )

        assert result["resolved"] is True
        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args[1]
        assert call_kwargs["profile_id"] == "profile-1"
        assert call_kwargs["tool_name"] == "Bash"
        assert call_kwargs["tool_pattern"] == "git *"
        assert call_kwargs["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_respond_remember_none_no_rule(self, handler):
        """Should not create rule when remember=none."""
        # Create a pending request
        request = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        handler._pending_requests["session-1"] = {"req-1": request}

        with patch("app.db.database.add_permission_rule") as mock_add:
            result = await handler.respond(
                request_id="req-1",
                session_id="session-1",
                decision="allow",
                remember="none"
            )

        assert result["resolved"] is True
        mock_add.assert_not_called()
        assert "session-1" not in handler._session_rules or len(handler._session_rules["session-1"]) == 0

    # -------------------------------------------------------------------------
    # Auto-resolve Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_resolve_matching_requests(self, handler):
        """Should auto-resolve matching queued requests."""
        # Create multiple pending requests
        req1 = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "npm install"}
        )
        req2 = PermissionRequest(
            request_id="req-2",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "npm run build"}
        )
        req3 = PermissionRequest(
            request_id="req-3",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "git status"}  # Should NOT match
        )
        handler._pending_requests["session-1"] = {
            "req-1": req1,
            "req-2": req2,
            "req-3": req3
        }

        # Create a rule that matches npm commands
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )

        mock_broadcast = AsyncMock()
        resolved = await handler._resolve_matching_requests("session-1", rule, mock_broadcast)

        # req-1 and req-2 should be resolved, req-3 should remain
        assert "req-1" in resolved
        assert "req-2" in resolved
        assert "req-3" not in resolved

        # Check responses were set
        assert req1.response["behavior"] == "allow"
        assert req2.response["behavior"] == "allow"
        assert req1.response_event.is_set()
        assert req2.response_event.is_set()

        # req-3 should still be pending
        assert "req-3" in handler._pending_requests["session-1"]
        assert not req3.response_event.is_set()

    @pytest.mark.asyncio
    async def test_resolve_matching_requests_deny(self, handler):
        """Should set deny response for matching requests with DENY rule."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "rm -rf /"}
        )
        handler._pending_requests["session-1"] = {"req-1": req}

        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern="rm *",
            decision=PermissionDecision.DENY
        )

        resolved = await handler._resolve_matching_requests("session-1", rule, None)

        assert "req-1" in resolved
        assert req.response["behavior"] == "deny"

    @pytest.mark.asyncio
    async def test_resolve_matching_requests_broadcasts(self, handler):
        """Should broadcast queue update when requests are resolved."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "npm install"}
        )
        handler._pending_requests["session-1"] = {"req-1": req}

        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )

        mock_broadcast = AsyncMock()
        await handler._resolve_matching_requests("session-1", rule, mock_broadcast)

        mock_broadcast.assert_called_once()
        call_args = mock_broadcast.call_args[0][0]
        assert call_args["type"] == "permission_queue_update"
        assert "req-1" in call_args["resolved_ids"]

    @pytest.mark.asyncio
    async def test_resolve_matching_requests_empty_session(self, handler):
        """Should handle session with no pending requests."""
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )

        resolved = await handler._resolve_matching_requests("unknown-session", rule, None)

        assert resolved == []

    # -------------------------------------------------------------------------
    # Get Pending Requests Tests
    # -------------------------------------------------------------------------

    def test_get_pending_requests_returns_list(self, handler):
        """Should return list of pending request data."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        handler._pending_requests["session-1"] = {"req-1": req}

        pending = handler.get_pending_requests("session-1")

        assert len(pending) == 1
        assert pending[0]["request_id"] == "req-1"
        assert pending[0]["tool_name"] == "Bash"
        assert pending[0]["tool_input"] == {"command": "ls"}
        assert "created_at" in pending[0]

    def test_get_pending_requests_multiple(self, handler):
        """Should return all pending requests for session."""
        req1 = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        req2 = PermissionRequest(
            request_id="req-2",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Read",
            tool_input={"file_path": "/test"}
        )
        handler._pending_requests["session-1"] = {"req-1": req1, "req-2": req2}

        pending = handler.get_pending_requests("session-1")

        assert len(pending) == 2
        request_ids = {p["request_id"] for p in pending}
        assert request_ids == {"req-1", "req-2"}

    # -------------------------------------------------------------------------
    # Cancel Request Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cancel_request_success(self, handler):
        """Should cancel pending request."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        handler._pending_requests["session-1"] = {"req-1": req}

        result = await handler.cancel_request("req-1", "session-1")

        assert result is True
        assert req.response["behavior"] == "deny"
        assert "cancelled" in req.response["message"].lower()
        assert req.response_event.is_set()
        assert "req-1" not in handler._pending_requests["session-1"]

    @pytest.mark.asyncio
    async def test_cancel_request_not_found(self, handler):
        """Should return False when request not found."""
        handler._pending_requests["session-1"] = {}

        result = await handler.cancel_request("unknown-req", "session-1")

        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_request_session_not_found(self, handler):
        """Should return False when session not found."""
        result = await handler.cancel_request("req-1", "unknown-session")

        assert result is False

    # -------------------------------------------------------------------------
    # Cancel All Requests Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cancel_all_requests(self, handler):
        """Should cancel all pending requests for session."""
        req1 = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        req2 = PermissionRequest(
            request_id="req-2",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Read",
            tool_input={"file_path": "/test"}
        )
        handler._pending_requests["session-1"] = {"req-1": req1, "req-2": req2}

        count = await handler.cancel_all_requests("session-1")

        assert count == 2
        assert req1.response["behavior"] == "deny"
        assert req2.response["behavior"] == "deny"
        assert req1.response_event.is_set()
        assert req2.response_event.is_set()
        assert "session-1" not in handler._pending_requests

    @pytest.mark.asyncio
    async def test_cancel_all_requests_empty_session(self, handler):
        """Should return 0 when session has no requests."""
        result = await handler.cancel_all_requests("unknown-session")

        assert result == 0

    # -------------------------------------------------------------------------
    # Session Rules Management Tests
    # -------------------------------------------------------------------------

    def test_clear_session_rules(self, handler):
        """Should clear session rules."""
        handler._session_rules["session-1"] = [
            PermissionRule(
                id="rule-1",
                session_id="session-1",
                profile_id=None,
                tool_name="Bash",
                tool_pattern=None,
                decision=PermissionDecision.ALLOW
            )
        ]

        handler.clear_session_rules("session-1")

        assert "session-1" not in handler._session_rules

    def test_clear_session_rules_unknown_session(self, handler):
        """Should not raise error for unknown session."""
        handler.clear_session_rules("unknown-session")
        # No error raised

    def test_get_session_rules(self, handler):
        """Should return session rules as dict list."""
        rule = PermissionRule(
            id="rule-1",
            session_id="session-1",
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm *",
            decision=PermissionDecision.ALLOW
        )
        handler._session_rules["session-1"] = [rule]

        rules = handler.get_session_rules("session-1")

        assert len(rules) == 1
        assert rules[0]["id"] == "rule-1"
        assert rules[0]["tool_name"] == "Bash"
        assert rules[0]["tool_pattern"] == "npm *"
        assert rules[0]["decision"] == "allow"


# =============================================================================
# Global Singleton Tests
# =============================================================================

class TestGlobalSingleton:
    """Test the global permission_handler singleton."""

    def test_singleton_exists(self):
        """Global singleton should exist."""
        assert permission_handler is not None
        assert isinstance(permission_handler, PermissionHandler)

    def test_singleton_is_permission_handler(self):
        """Global singleton should be a PermissionHandler instance."""
        assert hasattr(permission_handler, "request_permission")
        assert hasattr(permission_handler, "respond")
        assert hasattr(permission_handler, "get_pending_requests")


# =============================================================================
# Edge Cases and Security Tests
# =============================================================================

class TestEdgeCasesAndSecurity:
    """Test edge cases and security-related scenarios."""

    @pytest.fixture
    def handler(self):
        """Create a fresh PermissionHandler for each test."""
        return PermissionHandler()

    def test_rule_with_empty_pattern(self):
        """Empty pattern should match all (treated as no pattern)."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="",  # Empty string pattern
            decision=PermissionDecision.ALLOW
        )

        # Empty pattern is falsy, so should match all
        assert rule.matches("Bash", {"command": "anything"})

    def test_pattern_with_special_characters(self):
        """Should handle special fnmatch characters correctly."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="npm run [test]*",
            decision=PermissionDecision.ALLOW
        )

        # [test] matches t, e, s, or t
        assert rule.matches("Bash", {"command": "npm run test"})

    def test_dangerous_command_can_be_denied(self):
        """Dangerous commands can be caught by deny rules."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern="rm -rf *",
            decision=PermissionDecision.DENY
        )

        assert rule.matches("Bash", {"command": "rm -rf /"})
        assert rule.matches("Bash", {"command": "rm -rf /home"})
        assert not rule.matches("Bash", {"command": "rm file.txt"})

    def test_path_traversal_pattern(self):
        """Should be able to create rules for path traversal detection."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Read",
            tool_pattern="*/..*/etc/*",  # Detect path traversal to etc
            decision=PermissionDecision.DENY
        )

        # Note: fnmatch isn't ideal for security, but rules can help
        assert rule.matches("Read", {"file_path": "/home/../etc/passwd"})

    def test_concurrent_requests_isolation(self, handler):
        """Multiple sessions should be isolated."""
        req1 = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        req2 = PermissionRequest(
            request_id="req-1",  # Same request ID but different session
            session_id="session-2",
            profile_id="profile-2",
            tool_name="Bash",
            tool_input={"command": "ls"}
        )
        handler._pending_requests["session-1"] = {"req-1": req1}
        handler._pending_requests["session-2"] = {"req-1": req2}

        # Each session should see only its own requests
        assert handler.get_queue_count("session-1") == 1
        assert handler.get_queue_count("session-2") == 1

        pending1 = handler.get_pending_requests("session-1")
        pending2 = handler.get_pending_requests("session-2")
        assert len(pending1) == 1
        assert len(pending2) == 1

    @pytest.mark.asyncio
    async def test_respond_with_updated_input(self, handler, mock_broadcast=None):
        """Should preserve updated_input in allow response."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "npm install", "timeout": 300}
        )
        handler._pending_requests["session-1"] = {"req-1": req}

        await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="allow"
        )

        # The original tool_input should be in updatedInput
        assert req.response["updatedInput"] == {"command": "npm install", "timeout": 300}

    def test_rule_none_session_and_profile(self):
        """Rule with None session and profile should work."""
        rule = PermissionRule(
            id="rule-1",
            session_id=None,
            profile_id=None,
            tool_name="Bash",
            tool_pattern=None,
            decision=PermissionDecision.ALLOW
        )

        # Should still match based on tool_name
        assert rule.matches("Bash", {"command": "any"})

    @pytest.mark.asyncio
    async def test_concurrent_respond_calls(self, handler):
        """Should handle concurrent respond calls safely."""
        # Create multiple pending requests
        for i in range(5):
            req = PermissionRequest(
                request_id=f"req-{i}",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": f"cmd-{i}"}
            )
            if "session-1" not in handler._pending_requests:
                handler._pending_requests["session-1"] = {}
            handler._pending_requests["session-1"][f"req-{i}"] = req

        # Respond to all concurrently
        async def respond(req_id):
            return await handler.respond(
                request_id=req_id,
                session_id="session-1",
                decision="allow"
            )

        results = await asyncio.gather(*[respond(f"req-{i}") for i in range(5)])

        # All should have resolved
        for result in results:
            assert result["resolved"] is True

        # All requests should be removed
        assert handler.get_queue_count("session-1") == 0
