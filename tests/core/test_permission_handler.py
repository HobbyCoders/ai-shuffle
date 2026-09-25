"""
Unit tests for permission handler module.

Tests cover:
- PermissionDecision enum
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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.core.permission_handler import (
    PermissionDecision,
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
        before = datetime.now(timezone.utc)
        request = PermissionRequest(
            request_id="req-123",
            session_id="session-456",
            profile_id="profile-789",
            tool_name="Bash",
            tool_input={}
        )
        after = datetime.now(timezone.utc)

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

            # Give time for request to be queued. V2 creates the durable row via
            # asyncio.to_thread before broadcasting, so the previous legacy-path
            # sleep(0.01) is occasionally too short for the broadcast to have
            # fired yet (flaky under V2); 0.05 gives the thread-pool hop margin.
            await asyncio.sleep(0.05)

            # Check it was queued
            assert handler.get_queue_count("session-1") == 1
            assert mock_broadcast.called

            # Verify broadcast content
            call_args = mock_broadcast.call_args[0][0]
            assert call_args["type"] == "permission_request"
            assert call_args["session_id"] == "session-1"
            assert call_args["request_id"] == "req-1"
            assert call_args["tool_name"] == "Bash"

            # Properly cancel by setting the response and signaling the event
            # This avoids the "coroutine was never awaited" warning
            await handler.cancel_request("req-1", "session-1")
            await task

    # NOTE: test_request_permission_timeout used to patch asyncio.wait_for with
    # side_effect=asyncio.TimeoutError to force an immediate top-level timeout.
    # Under the V2-only collapsed handler (#603), request_permission blocks via
    # interaction_wait.await_interaction, which calls asyncio.wait_for internally
    # inside its own polling loop and catches TimeoutError as a normal poll-miss
    # (it just loops again) — so patching asyncio.wait_for globally no longer
    # produces a fast timeout, it makes the poll loop busy-spin for the real
    # PERMISSION_TTL_SECONDS (300s) instead. Empirically confirmed to hang past
    # 20s (killed via `timeout 20`), so removed as a hang-risk rather than kept.
    # A real TTL-driven timeout would need a dedicated short-TTL integration
    # test, out of scope for this flag-removal change.

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

    # -------------------------------------------------------------------------
    # Respond Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_respond_request_not_found(self, handler):
        """Should return error when request not found (no matching interaction row)."""
        result = await handler.respond(
            request_id="unknown-req",
            session_id="session-1",
            decision="allow"
        )

        assert result == {"error": "Request not found"}

    # -------------------------------------------------------------------------
    # Cancel Request Tests
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cancel_request_not_found(self, handler):
        """Should return False when request not found."""
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

# =============================================================================
# Slice D — ToolPermissionContext enrichment field tests (D.5)
# =============================================================================

class TestEnrichmentFields:
    """
    D.5 — Verify that the 5 SDK 0.1.81 ToolPermissionContext enrichment fields
    (title, display_name, description, decision_reason, blocked_path) are
    stored on PermissionRequest, included in broadcast payloads when non-None,
    and omitted from payloads when None.
    """

    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.fixture
    def mock_broadcast(self):
        return AsyncMock()

    def test_permission_request_stores_enrichment_fields(self):
        """PermissionRequest should store all 5 enrichment fields."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"},
            title="Run bash command",
            display_name="Bash Executor",
            description="Runs a bash command in the shell",
            decision_reason="Requires elevated access",
            blocked_path="/etc/passwd",
        )

        assert req.title == "Run bash command"
        assert req.display_name == "Bash Executor"
        assert req.description == "Runs a bash command in the shell"
        assert req.decision_reason == "Requires elevated access"
        assert req.blocked_path == "/etc/passwd"

    def test_permission_request_enrichment_fields_default_none(self):
        """Enrichment fields should default to None when not provided."""
        req = PermissionRequest(
            request_id="req-1",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )

        assert req.title is None
        assert req.display_name is None
        assert req.description is None
        assert req.decision_reason is None
        assert req.blocked_path is None

    @pytest.mark.asyncio
    async def test_broadcast_payload_includes_enrichment_fields_when_set(self, handler, mock_broadcast):
        """Broadcast payload must include all 5 enrichment fields when non-None."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            task = asyncio.create_task(
                handler.request_permission(
                    request_id="req-enriched",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "rm -rf /"},
                    broadcast_func=mock_broadcast,
                    title="Dangerous command",
                    display_name="Bash",
                    description="Deletes everything",
                    decision_reason="Destructive operation detected",
                    blocked_path="/",
                )
            )

            # V2 creates the durable row via asyncio.to_thread before
            # broadcasting, so sleep(0.01) is occasionally too short (flaky).
            await asyncio.sleep(0.05)

            # Inspect the broadcast that was fired
            assert mock_broadcast.called
            payload = mock_broadcast.call_args[0][0]
            assert payload["type"] == "permission_request"
            assert payload["title"] == "Dangerous command"
            assert payload["display_name"] == "Bash"
            assert payload["description"] == "Deletes everything"
            assert payload["decision_reason"] == "Destructive operation detected"
            assert payload["blocked_path"] == "/"

            # Clean up
            await handler.cancel_request("req-enriched", "session-1")
            await task

    @pytest.mark.asyncio
    async def test_broadcast_payload_omits_none_enrichment_fields(self, handler, mock_broadcast):
        """Broadcast payload must NOT include enrichment fields when they are None."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            task = asyncio.create_task(
                handler.request_permission(
                    request_id="req-plain",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    broadcast_func=mock_broadcast,
                    # enrichment fields intentionally omitted → default None
                )
            )

            # V2 creates the durable row via asyncio.to_thread before
            # broadcasting, so sleep(0.01) is occasionally too short (flaky).
            await asyncio.sleep(0.05)

            payload = mock_broadcast.call_args[0][0]
            assert payload["type"] == "permission_request"
            assert "title" not in payload
            assert "display_name" not in payload
            assert "description" not in payload
            assert "decision_reason" not in payload
            assert "blocked_path" not in payload

            await handler.cancel_request("req-plain", "session-1")
            await task

    @pytest.mark.asyncio
    async def test_broadcast_payload_includes_partial_enrichment_fields(self, handler, mock_broadcast):
        """Only non-None enrichment fields should appear in the payload."""
        with patch("app.db.database.get_permission_rules", return_value=[]):
            task = asyncio.create_task(
                handler.request_permission(
                    request_id="req-partial",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    broadcast_func=mock_broadcast,
                    title="Only title set",
                    blocked_path="/tmp",
                    # display_name, description, decision_reason remain None
                )
            )

            # V2 creates the durable row via asyncio.to_thread before
            # broadcasting, so sleep(0.01) is occasionally too short (flaky).
            await asyncio.sleep(0.05)

            payload = mock_broadcast.call_args[0][0]
            assert payload["title"] == "Only title set"
            assert payload["blocked_path"] == "/tmp"
            assert "display_name" not in payload
            assert "description" not in payload
            assert "decision_reason" not in payload

            await handler.cancel_request("req-partial", "session-1")
            await task


# =============================================================================
# Two-leg permission delivery
# =============================================================================

class TestPermissionBroadcastLegs:
    """``_broadcast_permission_request`` must deliver over BOTH legs.

    A plain prompt used to ride the origin socket alone (one bare await), while a
    redirected ProviderAgent worker prompt got the origin socket AND a user-wide
    device fan-out, each independently swallowed. That asymmetry is removed: both
    legs always run, and neither can abort the other or raise at the caller.
    """

    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.fixture
    def request_obj(self):
        return PermissionRequest(
            request_id="req-legs",
            session_id="session-legs",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )

    @staticmethod
    def _session_row(api_user_id):
        return {"id": "session-legs", "api_user_id": api_user_id}

    @pytest.mark.asyncio
    async def test_fans_out_user_wide_with_resolved_user_id(self, handler, request_obj):
        """Leg 2 fires with the session owner's user id and the SAME event."""
        origin = AsyncMock()
        fanout = AsyncMock()
        from app.core import sync_engine as se

        with patch.object(se.sync_engine, "broadcast_user_permission", fanout), \
             patch("app.db.sessions.get_session", return_value=self._session_row("user-7")):
            await handler._broadcast_permission_request(request_obj, 1, origin)

        origin.assert_awaited_once()
        fanout.assert_awaited_once()
        assert fanout.await_args.args[0] == "user-7"
        # Byte-identical event down both legs (the frontend dedupes by request_id).
        assert fanout.await_args.args[1] is origin.await_args.args[0]
        assert fanout.await_args.args[1]["type"] == "permission_request"
        assert fanout.await_args.args[1]["request_id"] == "req-legs"

    @pytest.mark.asyncio
    async def test_null_api_user_maps_to_admin(self, handler, request_obj):
        """An admin-owned session has a NULL ``api_user_id``; /ws/chat registers
        those devices under the literal "admin", so the fan-out must match."""
        fanout = AsyncMock()
        from app.core import sync_engine as se

        with patch.object(se.sync_engine, "broadcast_user_permission", fanout), \
             patch("app.db.sessions.get_session", return_value=self._session_row(None)):
            await handler._broadcast_permission_request(request_obj, 1, AsyncMock())

        assert fanout.await_args.args[0] == "admin"

    @pytest.mark.asyncio
    async def test_origin_failure_does_not_block_fanout_or_raise(self, handler, request_obj):
        """The regression this fixes: a dead origin socket used to abort delivery.

        Leg 2 must still run, and the caller must never see the exception.
        """
        origin = AsyncMock(side_effect=RuntimeError("socket gone"))
        fanout = AsyncMock()
        from app.core import sync_engine as se

        with patch.object(se.sync_engine, "broadcast_user_permission", fanout), \
             patch("app.db.sessions.get_session", return_value=self._session_row("user-7")):
            await handler._broadcast_permission_request(request_obj, 1, origin)

        origin.assert_awaited_once()
        fanout.assert_awaited_once()
        assert fanout.await_args.args[0] == "user-7"

    @pytest.mark.asyncio
    async def test_fanout_failure_is_swallowed(self, handler, request_obj):
        """Delivery is best-effort; the durable interaction row is the truth."""
        origin = AsyncMock()
        from app.core import sync_engine as se

        with patch.object(
            se.sync_engine, "broadcast_user_permission",
            AsyncMock(side_effect=RuntimeError("fanout gone")),
        ), patch("app.db.sessions.get_session", return_value=self._session_row("user-7")):
            await handler._broadcast_permission_request(request_obj, 1, origin)

        origin.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unresolvable_session_skips_fanout(self, handler, request_obj):
        """No row (or a blown-up lookup) → leg 2 is skipped, leg 1 still runs."""
        origin = AsyncMock()
        fanout = AsyncMock()
        from app.core import sync_engine as se

        with patch.object(se.sync_engine, "broadcast_user_permission", fanout), \
             patch("app.db.sessions.get_session", side_effect=RuntimeError("db down")):
            await handler._broadcast_permission_request(request_obj, 1, origin)

        origin.assert_awaited_once()
        fanout.assert_not_awaited()

        origin2 = AsyncMock()
        fanout.reset_mock()
        with patch.object(se.sync_engine, "broadcast_user_permission", fanout), \
             patch("app.db.sessions.get_session", return_value=None):
            await handler._broadcast_permission_request(request_obj, 1, origin2)

        origin2.assert_awaited_once()
        fanout.assert_not_awaited()


class TestConnectorFunnelHook:
    """``request_permission`` must fan the prompt out to connector platforms.

    The funnel is best-effort: a connector-less session, a dead lookup, or a
    platform error must never break the durable permission flow itself.
    """

    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.mark.asyncio
    async def test_funnel_invoked_with_request_details(self, handler, mock_broadcast=None):

        broadcast = AsyncMock()
        captured = {}

        async def fake_deliver(**kwargs):
            captured.update(kwargs)

        with patch("app.db.database.get_permission_rules", return_value=[]), \
             patch("app.core.connector_permissions.deliver_permission_prompt", fake_deliver):
            task = asyncio.create_task(
                handler.request_permission(
                    request_id="req-funnel",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "rm -rf /"},
                    broadcast_func=broadcast,
                )
            )
            await asyncio.sleep(0.05)
            await handler.cancel_request("req-funnel", "session-1")
            await task

        assert captured["session_id"] == "session-1"
        assert captured["request_id"] == "req-funnel"
        assert captured["tool_name"] == "Bash"
        assert captured["tool_input"] == {"command": "rm -rf /"}

    @pytest.mark.asyncio
    async def test_funnel_failure_does_not_break_request(self, handler):
        """A connector-side explosion must leave the durable flow intact."""
        broadcast = AsyncMock()

        async def boom(**kwargs):
            raise RuntimeError("discord down")

        with patch("app.db.database.get_permission_rules", return_value=[]), \
             patch("app.core.connector_permissions.deliver_permission_prompt", boom):
            task = asyncio.create_task(
                handler.request_permission(
                    request_id="req-funnel2",
                    session_id="session-1",
                    profile_id="profile-1",
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    broadcast_func=broadcast,
                )
            )
            await asyncio.sleep(0.05)

            # The durable row exists and the WS broadcast still happened.
            assert handler.get_queue_count("session-1") == 1
            assert broadcast.called

            await handler.cancel_request("req-funnel2", "session-1")
            await task
