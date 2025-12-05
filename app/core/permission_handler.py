"""
Permission Handler for Tool Requests

Manages tool permission requests with a queue system that supports:
- Multiple concurrent permission requests per session
- Batch approval/denial when rules match multiple queued requests
- Persistent permission rules (session-level and profile-level)
- Pattern matching for tool inputs (e.g., "Bash:npm*")
"""

import asyncio
import logging
import re
import fnmatch
from typing import Optional, Dict, Any, List, Literal, Union
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from app.db import database

logger = logging.getLogger(__name__)


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class RememberScope(str, Enum):
    NONE = "none"           # Don't remember
    SESSION = "session"     # Remember for this session only
    PROFILE = "profile"     # Remember for all sessions with this profile


@dataclass
class PermissionRequest:
    """A pending permission request for a tool use"""
    request_id: str
    session_id: str
    profile_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)

    # Async event for waiting on response
    response_event: asyncio.Event = field(default_factory=asyncio.Event)
    response: Optional[Dict[str, Any]] = None


@dataclass
class PermissionRule:
    """A saved permission rule"""
    id: str
    session_id: Optional[str]  # None = applies to all sessions
    profile_id: Optional[str]  # None = applies to all profiles
    tool_name: str
    tool_pattern: Optional[str]  # Optional pattern for tool input matching
    decision: PermissionDecision
    created_at: datetime = field(default_factory=datetime.now)

    def matches(self, tool_name: str, tool_input: Dict[str, Any]) -> bool:
        """Check if this rule matches the given tool use"""
        if self.tool_name != tool_name and self.tool_name != "*":
            return False

        if not self.tool_pattern:
            return True

        # Pattern matching based on tool type
        return self._match_pattern(tool_input)

    def _match_pattern(self, tool_input: Dict[str, Any]) -> bool:
        """Match pattern against tool input"""
        pattern = self.tool_pattern

        # Handle different tool types
        if self.tool_name == "Bash":
            command = tool_input.get("command", "")
            return fnmatch.fnmatch(command, pattern)

        elif self.tool_name in ("Read", "Write", "Edit", "Glob"):
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
            return fnmatch.fnmatch(file_path, pattern)

        elif self.tool_name == "Grep":
            path = tool_input.get("path", "")
            return fnmatch.fnmatch(path, pattern)

        elif self.tool_name == "WebFetch":
            url = tool_input.get("url", "")
            return fnmatch.fnmatch(url, pattern)

        # Generic: try to match against any string value in input
        for value in tool_input.values():
            if isinstance(value, str) and fnmatch.fnmatch(value, pattern):
                return True

        return False


class PermissionHandler:
    """
    Handles permission requests for tool uses with queue support.

    Key features:
    - Queue multiple permission requests per session
    - When a rule is created (e.g., "Allow Always"), automatically resolve
      all queued requests that match the new rule
    - Support session-level and profile-level rules
    - WebSocket integration for real-time permission UI
    """

    def __init__(self):
        # Pending requests by session_id
        self._pending_requests: Dict[str, Dict[str, PermissionRequest]] = {}

        # In-memory session rules (cleared when session ends)
        self._session_rules: Dict[str, List[PermissionRule]] = {}

        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    async def request_permission(
        self,
        request_id: str,
        session_id: str,
        profile_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        broadcast_func: callable
    ) -> Union[PermissionResultAllow, PermissionResultDeny]:
        """
        Request permission for a tool use.

        This will:
        1. Check saved rules first (session and profile level)
        2. If no rule matches, queue the request and notify frontend
        3. Wait for user response

        Args:
            request_id: Unique ID for this request
            session_id: The chat session ID
            profile_id: The profile ID for this session
            tool_name: Name of the tool being requested
            tool_input: Tool input parameters
            broadcast_func: Async function to broadcast to WebSocket

        Returns:
            PermissionResultAllow or PermissionResultDeny
        """
        # Check saved rules first
        rule = self._check_rules(session_id, profile_id, tool_name, tool_input)
        if rule:
            logger.info(f"Permission for {tool_name} auto-resolved by rule: {rule.decision}")
            if rule.decision == PermissionDecision.ALLOW:
                return PermissionResultAllow(updated_input=tool_input)
            else:
                return PermissionResultDeny(message="Denied by saved rule")

        # Create pending request
        request = PermissionRequest(
            request_id=request_id,
            session_id=session_id,
            profile_id=profile_id,
            tool_name=tool_name,
            tool_input=tool_input
        )

        async with self._lock:
            if session_id not in self._pending_requests:
                self._pending_requests[session_id] = {}
            self._pending_requests[session_id][request_id] = request

        # Get current queue position
        queue_position = len(self._pending_requests.get(session_id, {}))

        # Broadcast permission request to frontend
        await broadcast_func({
            "type": "permission_request",
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "queue_position": queue_position,
            "queue_total": queue_position
        })

        logger.info(f"Permission request queued: {request_id} for {tool_name}")

        # Wait for response (with timeout)
        try:
            await asyncio.wait_for(request.response_event.wait(), timeout=300.0)  # 5 min timeout
        except asyncio.TimeoutError:
            # Remove from queue on timeout
            async with self._lock:
                if session_id in self._pending_requests:
                    self._pending_requests[session_id].pop(request_id, None)
            return PermissionResultDeny(message="Permission request timed out")

        # Return the response - convert from stored dict to proper type
        if request.response:
            if request.response.get("behavior") == "allow":
                return PermissionResultAllow(updated_input=request.response.get("updatedInput", tool_input))
            else:
                return PermissionResultDeny(message=request.response.get("message", "Permission denied"))
        else:
            return PermissionResultDeny(message="No response received")

    async def respond(
        self,
        request_id: str,
        session_id: str,
        decision: Literal["allow", "deny"],
        remember: Optional[Literal["none", "session", "profile"]] = None,
        pattern: Optional[str] = None,
        broadcast_func: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Respond to a permission request.

        If remember is set, creates a rule and auto-resolves matching queued requests.

        Args:
            request_id: The request to respond to
            session_id: The session ID
            decision: "allow" or "deny"
            remember: Scope to remember the decision
            pattern: Optional pattern for the rule (e.g., "npm *" for Bash)
            broadcast_func: Function to broadcast queue updates

        Returns:
            Info about resolved requests
        """
        async with self._lock:
            if session_id not in self._pending_requests:
                return {"error": "Session not found"}

            request = self._pending_requests[session_id].get(request_id)
            if not request:
                return {"error": "Request not found"}

            # Set the response
            if decision == "allow":
                request.response = {"behavior": "allow", "updatedInput": request.tool_input}
            else:
                request.response = {"behavior": "deny", "message": "User denied permission"}

            # Signal the waiting coroutine
            request.response_event.set()

            # Remove from pending
            del self._pending_requests[session_id][request_id]

            # Create rule if remembering
            resolved_requests = []
            if remember and remember != "none":
                rule = PermissionRule(
                    id=f"rule-{datetime.now().timestamp()}",
                    session_id=session_id if remember == "session" else None,
                    profile_id=request.profile_id if remember == "profile" else None,
                    tool_name=request.tool_name,
                    tool_pattern=pattern,
                    decision=PermissionDecision(decision)
                )

                # Save rule
                if remember == "session":
                    if session_id not in self._session_rules:
                        self._session_rules[session_id] = []
                    self._session_rules[session_id].append(rule)
                else:
                    # Save to database for profile-level persistence
                    database.add_permission_rule(
                        profile_id=request.profile_id,
                        tool_name=rule.tool_name,
                        tool_pattern=rule.tool_pattern,
                        decision=rule.decision.value
                    )

                # Auto-resolve matching queued requests
                resolved_requests = await self._resolve_matching_requests(
                    session_id, rule, broadcast_func
                )

        return {
            "resolved": True,
            "auto_resolved_count": len(resolved_requests),
            "auto_resolved_ids": resolved_requests
        }

    async def _resolve_matching_requests(
        self,
        session_id: str,
        rule: PermissionRule,
        broadcast_func: Optional[callable]
    ) -> List[str]:
        """Resolve all queued requests that match the new rule"""
        resolved_ids = []

        if session_id not in self._pending_requests:
            return resolved_ids

        # Find all matching requests
        requests_to_resolve = []
        for req_id, req in list(self._pending_requests[session_id].items()):
            if rule.matches(req.tool_name, req.tool_input):
                requests_to_resolve.append((req_id, req))

        # Resolve them
        for req_id, req in requests_to_resolve:
            if rule.decision == PermissionDecision.ALLOW:
                req.response = {"behavior": "allow", "updatedInput": req.tool_input}
            else:
                req.response = {"behavior": "deny", "message": "Denied by rule"}

            req.response_event.set()
            del self._pending_requests[session_id][req_id]
            resolved_ids.append(req_id)

            logger.info(f"Auto-resolved request {req_id} for {req.tool_name} by new rule")

        # Broadcast updated queue if any were resolved
        if resolved_ids and broadcast_func:
            await broadcast_func({
                "type": "permission_queue_update",
                "resolved_ids": resolved_ids,
                "remaining_count": len(self._pending_requests.get(session_id, {}))
            })

        return resolved_ids

    def _check_rules(
        self,
        session_id: str,
        profile_id: str,
        tool_name: str,
        tool_input: Dict[str, Any]
    ) -> Optional[PermissionRule]:
        """Check if any saved rule matches this tool use"""
        # Check session-level rules first (more specific)
        if session_id in self._session_rules:
            for rule in self._session_rules[session_id]:
                if rule.matches(tool_name, tool_input):
                    return rule

        # Check profile-level rules from database
        db_rules = database.get_permission_rules(profile_id=profile_id)
        for rule_data in db_rules:
            rule = PermissionRule(
                id=rule_data["id"],
                session_id=None,
                profile_id=rule_data["profile_id"],
                tool_name=rule_data["tool_name"],
                tool_pattern=rule_data.get("tool_pattern"),
                decision=PermissionDecision(rule_data["decision"])
            )
            if rule.matches(tool_name, tool_input):
                return rule

        return None

    def get_pending_requests(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all pending permission requests for a session"""
        if session_id not in self._pending_requests:
            return []

        return [
            {
                "request_id": req.request_id,
                "tool_name": req.tool_name,
                "tool_input": req.tool_input,
                "created_at": req.created_at.isoformat()
            }
            for req in self._pending_requests[session_id].values()
        ]

    def get_queue_count(self, session_id: str) -> int:
        """Get count of pending requests for a session"""
        return len(self._pending_requests.get(session_id, {}))

    async def cancel_request(self, request_id: str, session_id: str) -> bool:
        """Cancel a pending permission request"""
        async with self._lock:
            if session_id not in self._pending_requests:
                return False

            request = self._pending_requests[session_id].get(request_id)
            if not request:
                return False

            # Set denial response and signal
            request.response = {"behavior": "deny", "message": "Request cancelled"}
            request.response_event.set()
            del self._pending_requests[session_id][request_id]

            return True

    async def cancel_all_requests(self, session_id: str) -> int:
        """Cancel all pending requests for a session"""
        async with self._lock:
            if session_id not in self._pending_requests:
                return 0

            count = len(self._pending_requests[session_id])
            for request in self._pending_requests[session_id].values():
                request.response = {"behavior": "deny", "message": "Session cancelled"}
                request.response_event.set()

            del self._pending_requests[session_id]
            return count

    def clear_session_rules(self, session_id: str):
        """Clear session-level rules when session ends"""
        self._session_rules.pop(session_id, None)

    def get_session_rules(self, session_id: str) -> List[Dict[str, Any]]:
        """Get session-level rules"""
        if session_id not in self._session_rules:
            return []

        return [
            {
                "id": rule.id,
                "tool_name": rule.tool_name,
                "tool_pattern": rule.tool_pattern,
                "decision": rule.decision.value
            }
            for rule in self._session_rules[session_id]
        ]


# Global singleton instance
permission_handler = PermissionHandler()
